"""File-based persistent storage for delivery states and attempts.

Stores delivery state and attempt history in JSON files in Home Assistant's
`.storage/` directory. Provides restart-resilience for notification delivery
tracking and retry recovery.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from homeassistant.core import HomeAssistant

from .const import STORAGE_DELIVERY_ATTEMPTS_FILE, STORAGE_DELIVERY_STATES_FILE
from .models import Attempt, DeliveryStatus
from .persistence import AttemptStore, DeliveryState, DeliveryStateStore

_LOGGER = logging.getLogger(__name__)


class JsonFileDeliveryStateStore(DeliveryStateStore):
    """File-based delivery state persistence using JSON.

    Stores delivery state snapshots in Home Assistant's `.storage/` directory.
    Each delivery task gets a persistent record with its terminal state, retry
    schedule, and audit trail.

    Format: `<storage_dir>/ans_delivery_states.json`
    Structure: {
        "<job_id>": {
            "status": "SUCCESS|FILTERED|RATE_LIMITED|PERMANENT_FAIL|...",
            "attempt_count": int,
            "last_error": str | null,
            "created_at": ISO timestamp,
            "updated_at": ISO timestamp,
            "retry_scheduled_at": ISO timestamp | null,
            "retry_reason": str | null,
            "last_attempt": {...attempt details...}
        }
    }
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize file-based state store.

        Args:
            hass: Home Assistant instance for storage path resolution.

        """
        self._hass = hass
        self._storage_path = Path(hass.config.path(".storage"))
        self._file_path = self._storage_path / STORAGE_DELIVERY_STATES_FILE
        self._lock = None
        # In-memory cache for the file content to reduce I/O
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_loaded = False

    async def load(self, job_id: UUID) -> DeliveryState | None:
        """Load delivery state for a job.

        Args:
            job_id: Job identifier to load.

        Returns:
            DeliveryState if found, None otherwise.

        """
        await self._ensure_cache_loaded()
        job_key = str(job_id)

        if job_key not in self._cache:
            return None

        data = self._cache[job_key]
        return DeliveryState(
            job_id=UUID(job_key),
            status=DeliveryStatus(data.get("status")),
            attempt_count=data.get("attempt_count", 0),
            last_error=data.get("last_error"),
        )

    async def persist_filtered(self, job_id: UUID, reason: str | None = None) -> None:
        """Persist notification as filtered.

        Args:
            job_id: Job identifier.
            reason: Filter reason for audit trail.

        """
        await self._update_state(
            job_id,
            DeliveryStatus.FILTERED,
            error=reason,
        )

    async def persist_rate_limited(self, job_id: UUID) -> None:
        """Persist notification as rate-limited.

        Args:
            job_id: Job identifier.

        """
        await self._update_state(job_id, DeliveryStatus.RATE_LIMITED)

    async def persist_success(self, job_id: UUID, attempt: Attempt) -> None:
        """Persist successful delivery.

        Args:
            job_id: Job identifier.
            attempt: Delivery attempt details.

        """
        await self._update_state(
            job_id,
            DeliveryStatus.SUCCESS,
            attempt_count=attempt.attempt_number,
            attempt_data=self._serialize_attempt(attempt),
        )

    async def persist_transient_failure(self, job_id: UUID, attempt: Attempt) -> None:
        """Persist transient delivery failure.

        Args:
            job_id: Job identifier.
            attempt: Delivery attempt details.

        """
        await self._update_state(
            job_id,
            DeliveryStatus.TRANSIENT_FAIL,
            error=attempt.error,
            attempt_count=attempt.attempt_number,
            attempt_data=self._serialize_attempt(attempt),
        )

    async def persist_permanent_failure(
        self,
        job_id: UUID,
        attempt: Attempt | None = None,
        error: str | None = None,
    ) -> None:
        """Persist permanent delivery failure.

        Args:
            job_id: Job identifier.
            attempt: Delivery attempt details (optional).
            error: Error message if no attempt.

        """
        await self._update_state(
            job_id,
            DeliveryStatus.PERMANENT_FAIL,
            error=error or (attempt.error if attempt else None),
            attempt_count=attempt.attempt_number if attempt else 0,
            attempt_data=self._serialize_attempt(attempt) if attempt else None,
        )

    async def schedule_retry(
        self,
        job_id: UUID,
        run_at: datetime,
        reason: str | None = None,
    ) -> None:
        """Schedule a retry for a delivery task.

        Args:
            job_id: Job identifier.
            run_at: When to retry (ISO timestamp).
            reason: Retry reason (e.g., "RATE_LIMITED", "TRANSIENT_FAILURE").

        """
        await self._ensure_cache_loaded()
        job_key = str(job_id)

        if job_key not in self._cache:
            self._cache[job_key] = {
                "status": DeliveryStatus.RATE_LIMITED.value,
                "created_at": datetime.now(UTC).isoformat(),
                "attempt_count": 0,
            }

        self._cache[job_key].update(
            {
                "retry_scheduled_at": run_at.isoformat(),
                "retry_reason": reason,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )

        await self._write_cache()

    async def cleanup_completed(self, before: datetime) -> int:
        """Clean up completed delivery records older than cutoff.

        Removes terminal states (SUCCESS, PERMANENT_FAIL, etc.) that are
        older than the specified datetime.

        Args:
            before: Remove records updated before this datetime.

        Returns:
            Number of records removed.

        """
        await self._ensure_cache_loaded()
        before_iso = before.isoformat()
        removed_count = 0

        # Terminal statuses that can be cleaned
        terminal_statuses = {
            DeliveryStatus.SUCCESS.value,
            DeliveryStatus.PERMANENT_FAIL.value,
            DeliveryStatus.FILTERED.value,
        }

        keys_to_remove = []
        for job_key, data in self._cache.items():
            if (
                data.get("status") in terminal_statuses
                and data.get("updated_at", "") < before_iso
            ):
                keys_to_remove.append(job_key)

        for job_key in keys_to_remove:
            del self._cache[job_key]
            removed_count += 1

        if removed_count > 0:
            await self._write_cache()
            _LOGGER.info("Cleaned up %d delivery records", removed_count)

        return removed_count

    def get_pending_retries(self) -> list[tuple[UUID, datetime]]:
        """Get all pending retries scheduled for execution.

        Called during startup to recover retry state across restarts.

        Returns:
            List of (job_id, scheduled_run_time) tuples for tasks awaiting retry.

        """
        if not self._cache_loaded:
            _LOGGER.warning("Cache not loaded; pending retries may be incomplete")
            return []

        pending = []
        for job_key, data in self._cache.items():
            if data.get("retry_scheduled_at"):
                try:
                    run_at = datetime.fromisoformat(data["retry_scheduled_at"])
                    pending.append((UUID(job_key), run_at))
                except (ValueError, KeyError):
                    _LOGGER.warning("Invalid retry schedule for job %s", job_key)

        return pending

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_cache_loaded(self) -> None:
        """Load file into cache if not already loaded."""
        if self._cache_loaded:
            return

        try:
            if self._file_path.exists():

                def _load() -> dict:
                    return json.loads(self._file_path.read_text())

                # Run file I/O in executor to avoid blocking
                self._cache = await self._hass.async_add_executor_job(_load)
            else:
                self._cache = {}
            self._cache_loaded = True
        except Exception as exc:
            _LOGGER.error("Failed to load delivery states: %s", exc)
            self._cache = {}
            self._cache_loaded = True

    async def _write_cache(self) -> None:
        """Write cache to file."""
        try:
            self._storage_path.mkdir(parents=True, exist_ok=True)

            def _write() -> None:
                self._file_path.write_text(json.dumps(self._cache, indent=2))

            await self._hass.async_add_executor_job(_write)
        except Exception as exc:
            _LOGGER.error("Failed to persist delivery states: %s", exc)

    async def _update_state(
        self,
        job_id: UUID,
        status: DeliveryStatus,
        error: str | None = None,
        attempt_count: int | None = None,
        attempt_data: dict[str, Any] | None = None,
    ) -> None:
        """Update job state in cache and write to file.

        Args:
            job_id: Job identifier.
            status: New delivery status.
            error: Error message if applicable.
            attempt_count: Number of attempts made.
            attempt_data: Serialized attempt details.

        """
        await self._ensure_cache_loaded()
        job_key = str(job_id)

        if job_key not in self._cache:
            self._cache[job_key] = {
                "created_at": datetime.now(UTC).isoformat(),
            }

        self._cache[job_key].update(
            {
                "status": status.value,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )

        if error:
            self._cache[job_key]["last_error"] = error
        if attempt_count is not None:
            self._cache[job_key]["attempt_count"] = attempt_count
        if attempt_data:
            self._cache[job_key]["last_attempt"] = attempt_data

        await self._write_cache()

    @staticmethod
    def _serialize_attempt(attempt: Attempt) -> dict[str, Any]:
        """Convert Attempt to JSON-serializable dict.

        Args:
            attempt: Attempt object to serialize.

        Returns:
            Dictionary suitable for JSON storage.

        """
        return {
            "attempt_id": str(attempt.attempt_id),
            "attempt_number": attempt.attempt_number,
            "status": attempt.status.value,
            "started_at": attempt.started_at.isoformat(),
            "ended_at": attempt.ended_at.isoformat() if attempt.ended_at else None,
            "endpoint": attempt.endpoint,
            "remote_id": attempt.remote_id,
            "error": attempt.error,
        }


class JsonFileAttemptStore(AttemptStore):
    """File-based attempt history persistence using JSON.

    Stores complete delivery attempt history for idempotency and audit.

    Format: `<storage_dir>/ans_delivery_attempts.json`
    Structure: {
        "<job_id>": [
            {
                "attempt_id": str,
                "attempt_number": int,
                "idempotency_key": str,
                "status": str,
                "started_at": ISO timestamp,
                ...
            }
        ]
    }
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize file-based attempt store.

        Args:
            hass: Home Assistant instance for storage path resolution.

        """
        self._hass = hass
        self._storage_path = Path(hass.config.path(".storage"))
        self._file_path = self._storage_path / STORAGE_DELIVERY_ATTEMPTS_FILE
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._cache_loaded = False

    async def create(self, attempt: Attempt) -> None:
        """Store a new delivery attempt.

        Args:
            attempt: Attempt to store.

        """
        await self._ensure_cache_loaded()
        job_key = str(attempt.job_id)

        if job_key not in self._cache:
            self._cache[job_key] = []

        self._cache[job_key].append(self._serialize_attempt(attempt))
        await self._write_cache()

    async def update(self, attempt: Attempt) -> None:
        """Update an existing attempt.

        Args:
            attempt: Attempt with updated fields.

        """
        await self._ensure_cache_loaded()
        job_key = str(attempt.job_id)

        if job_key not in self._cache:
            await self.create(attempt)
            return

        # Find and update attempt by ID
        for i, stored in enumerate(self._cache[job_key]):
            if stored["attempt_id"] == str(attempt.attempt_id):
                self._cache[job_key][i] = self._serialize_attempt(attempt)
                await self._write_cache()
                return

        # If not found, append
        self._cache[job_key].append(self._serialize_attempt(attempt))
        await self._write_cache()

    async def next_attempt_number(self, job_id: UUID) -> int:
        """Get the next attempt number for a job.

        Args:
            job_id: Job identifier.

        Returns:
            Next attempt number (1-indexed).

        """
        await self._ensure_cache_loaded()
        job_key = str(job_id)
        attempts = self._cache.get(job_key, [])
        return len(attempts) + 1

    async def count(self, job_id: UUID) -> int:
        """Count total attempts for a job.

        Args:
            job_id: Job identifier.

        Returns:
            Number of attempts made.

        """
        await self._ensure_cache_loaded()
        job_key = str(job_id)
        return len(self._cache.get(job_key, []))

    async def cleanup_old_attempts(self, before: datetime) -> int:
        """Clean up old attempt records for completed jobs.

        Removes attempt history for jobs where all attempts are older than
        the specified datetime. This prevents the attempts file from growing
        unboundedly while preserving recent audit trails.

        Args:
            before: Remove attempts older than this datetime.

        Returns:
            Number of attempt records removed.

        """
        await self._ensure_cache_loaded()
        before_iso = before.isoformat()
        removed_count = 0

        keys_to_remove = []
        for job_key, attempts in self._cache.items():
            if not attempts:
                # Empty job lists can be cleaned immediately
                keys_to_remove.append(job_key)
                continue

            # Check if ALL attempts for this job are older than cutoff
            all_old = all(
                attempt.get("ended_at", attempt.get("started_at", "")) < before_iso
                for attempt in attempts
            )

            if all_old:
                keys_to_remove.append(job_key)

        # Remove old job records
        for job_key in keys_to_remove:
            attempt_count = len(self._cache.get(job_key, []))
            del self._cache[job_key]
            removed_count += attempt_count

        if removed_count > 0:
            await self._write_cache()
            _LOGGER.info("Cleaned up %d old attempt records", removed_count)

        return removed_count

    def get_attempts(self, job_id: UUID) -> list[Attempt]:
        """Get all attempts for a job (in-memory, may be incomplete).

        Args:
            job_id: Job identifier.

        Returns:
            List of Attempt objects.

        """
        if not self._cache_loaded:
            return []

        job_key = str(job_id)
        attempts = []
        for data in self._cache.get(job_key, []):
            try:
                attempts.append(self._deserialize_attempt(data))
            except Exception as exc:
                _LOGGER.warning("Failed to deserialize attempt: %s", exc)
        return attempts

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_cache_loaded(self) -> None:
        """Load file into cache if not already loaded."""
        if self._cache_loaded:
            return

        try:
            if self._file_path.exists():

                def _load() -> dict:
                    return json.loads(self._file_path.read_text())

                self._cache = await self._hass.async_add_executor_job(_load)
            else:
                self._cache = {}
            self._cache_loaded = True
        except Exception as exc:
            _LOGGER.error("Failed to load delivery attempts: %s", exc)
            self._cache = {}
            self._cache_loaded = True

    async def _write_cache(self) -> None:
        """Write cache to file."""
        try:
            self._storage_path.mkdir(parents=True, exist_ok=True)

            def _write() -> None:
                self._file_path.write_text(json.dumps(self._cache, indent=2))

            await self._hass.async_add_executor_job(_write)
        except Exception as exc:
            _LOGGER.error("Failed to persist delivery attempts: %s", exc)

    @staticmethod
    def _serialize_attempt(attempt: Attempt) -> dict[str, Any]:
        """Convert Attempt to JSON-serializable dict."""
        return {
            "attempt_id": str(attempt.attempt_id),
            "job_id": str(attempt.job_id),
            "attempt_number": attempt.attempt_number,
            "idempotency_key": attempt.idempotency_key,
            "status": attempt.status.value,
            "started_at": attempt.started_at.isoformat(),
            "ended_at": attempt.ended_at.isoformat() if attempt.ended_at else None,
            "endpoint": attempt.endpoint,
            "remote_id": attempt.remote_id,
            "error": attempt.error,
            "meta": attempt.meta,
        }

    @staticmethod
    def _deserialize_attempt(data: dict[str, Any]) -> Attempt:
        """Reconstruct Attempt from JSON dict."""
        return Attempt(
            attempt_id=UUID(data["attempt_id"]),
            job_id=UUID(data["job_id"]),
            attempt_number=data["attempt_number"],
            idempotency_key=data["idempotency_key"],
            status=DeliveryStatus(data["status"]),
            started_at=datetime.fromisoformat(data["started_at"]),
            ended_at=datetime.fromisoformat(data["ended_at"])
            if data.get("ended_at")
            else None,
            endpoint=data.get("endpoint"),
            remote_id=data.get("remote_id"),
            error=data.get("error"),
            meta=data.get("meta", {}),
        )
