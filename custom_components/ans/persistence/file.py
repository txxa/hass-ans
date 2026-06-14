"""Event-based persistent storage for ANS notification system.

Redesigned persistence layer with intuitive structure:
- ans_notifications.json: Registry of all notifications sent
- ans_delivery_attempts.json: Log of all delivery attempts
- ans_retry_queue.json: Active retry queue for recovery

This design makes it easy to:
- Query delivery history
- Analyze per-channel performance
- Track notification lifecycle
- Debug delivery issues
"""

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from homeassistant.core import HomeAssistant

from custom_components.ans.const import (
    SYS_STORAGE_ACKNOWLEDGEMENTS_FILE,
    SYS_STORAGE_ATTEMPTS_FILE,
    SYS_STORAGE_NOTIFICATIONS_FILE,
    SYS_STORAGE_RETRIES_FILE,
)

from ..models import Attempt

if TYPE_CHECKING:
    from ..models import NotificationDeliveryTask

_LOGGER = logging.getLogger(__name__)


class NotificationRegistry:
    """Registry of all notifications sent through ANS.

    Stores one entry per notification_id with:
    - notification_id, source, triggered_at
    - payload (title, message, metadata)
    - recipients list with their channels

    This is notification-level tracking (user intent), not task-level.
    """

    def __init__(self, hass: HomeAssistant, enabled: bool = True) -> None:
        """Initialize the notification registry.

        Args:
            hass: Home Assistant instance.
            enabled: Whether audit logging is enabled.

        """
        self._hass = hass
        self._enabled = enabled
        self._storage_path = Path(
            hass.config.path(".storage", SYS_STORAGE_NOTIFICATIONS_FILE)
        )
        self._notifications: list[dict] = []
        self._loaded = False

    async def _load(self) -> None:
        """Load notifications from JSON file."""
        if self._loaded:
            return

        try:
            if self._storage_path.exists():

                def _read():
                    with open(self._storage_path, encoding="utf-8") as f:
                        return json.load(f)

                self._notifications = await self._hass.async_add_executor_job(_read)
                _LOGGER.debug("Loaded %d notifications", len(self._notifications))
        except (OSError, json.JSONDecodeError) as e:
            _LOGGER.error("Failed to load notifications: %s", e)
            self._notifications = []

        self._loaded = True

    async def _save(self) -> None:
        """Save notifications to JSON file."""
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)

            def _write():
                with open(self._storage_path, "w", encoding="utf-8") as f:
                    json.dump(self._notifications, f, indent=2)

            await self._hass.async_add_executor_job(_write)
        except OSError as e:
            _LOGGER.error("Failed to save notifications: %s", e)

    async def register_notification(
        self,
        notification_id: str,
        source: str,
        triggered_at: datetime,
        payload: dict,
        recipients: list[dict],
    ) -> None:
        """Register a new notification.

        Args:
            notification_id: Unique notification identifier.
            source: Notification source.
            triggered_at: When notification was triggered.
            payload: Notification payload (title, message, type, criticality, metadata).
            recipients: List of {recipient_id, channels} dicts.

        """
        if not self._enabled:
            return

        await self._load()

        # Check if already registered (idempotency)
        if any(n["notification_id"] == notification_id for n in self._notifications):
            _LOGGER.debug(
                "Notification %s already registered, skipping", notification_id
            )
            return

        notification = {
            "notification_id": notification_id,
            "source": source,
            "triggered_at": triggered_at.isoformat(),
            "payload": payload,
            "recipients": recipients,
        }

        self._notifications.append(notification)
        await self._save()
        _LOGGER.debug(
            "Registered notification %s with %d recipients",
            notification_id,
            len(recipients),
        )

    async def get_notification(self, notification_id: str) -> dict | None:
        """Get notification by notification_id."""
        await self._load()
        return next(
            (n for n in self._notifications if n["notification_id"] == notification_id),
            None,
        )

    async def cleanup_old(self, before: datetime) -> int:
        """Remove notifications older than given timestamp."""
        if not self._enabled:
            return 0

        await self._load()

        original_count = len(self._notifications)

        # Clean up old notifications
        self._notifications = [
            n
            for n in self._notifications
            if datetime.fromisoformat(n["triggered_at"]) >= before
        ]

        removed = original_count - len(self._notifications)
        if removed > 0:
            await self._save()
            _LOGGER.debug("Cleaned up %d old notifications", removed)

        return removed


class DeliveryAttemptLog:
    """Log of all delivery attempts.

    Stores one entry per delivery attempt with:
    - attempt_id, job_id, channel_id, recipient_id
    - attempt_number, started_at, ended_at
    - status, error, remote_id
    - response_time_ms
    """

    def __init__(self, hass: HomeAssistant, enabled: bool = True) -> None:
        """Initialize the delivery attempt log.

        Args:
            hass: Home Assistant instance.
            enabled: Whether audit logging is enabled.

        """
        self._hass = hass
        self._enabled = enabled
        self._storage_path = Path(
            hass.config.path(".storage", SYS_STORAGE_ATTEMPTS_FILE)
        )
        self._attempts: list[dict] = []
        self._loaded = False

    async def _load(self) -> None:
        """Load attempts from JSON file."""
        if self._loaded:
            return

        try:
            if self._storage_path.exists():

                def _read():
                    with open(self._storage_path, encoding="utf-8") as f:
                        return json.load(f)

                self._attempts = await self._hass.async_add_executor_job(_read)
                _LOGGER.debug("Loaded %d delivery attempts", len(self._attempts))
        except (OSError, json.JSONDecodeError) as e:
            _LOGGER.error("Failed to load delivery attempts: %s", e)
            self._attempts = []

        self._loaded = True

    async def _save(self) -> None:
        """Save attempts to JSON file."""
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)

            def _write():
                with open(self._storage_path, "w", encoding="utf-8") as f:
                    json.dump(self._attempts, f, indent=2)

            await self._hass.async_add_executor_job(_write)
        except OSError as e:
            _LOGGER.error("Failed to save delivery attempts: %s", e)

    async def log_attempt(
        self, attempt: Attempt, task: "NotificationDeliveryTask"
    ) -> None:
        """Log a delivery attempt (only if audit logging enabled)."""
        if not self._enabled:
            return

        await self._load()

        attempt_record = {
            "attempt_id": str(attempt.attempt_id),
            "job_id": str(attempt.job_id),
            "notification_id": task.payload.notification_id,  # Link to notification
            "channel_id": task.channel_info.id,
            "recipient_id": task.recipient_id,
            "attempt_number": attempt.attempt_number,
            "started_at": attempt.started_at.isoformat(),
            "ended_at": attempt.ended_at.isoformat() if attempt.ended_at else None,
            "status": attempt.status.value,
            "endpoint": attempt.endpoint,
            "remote_id": attempt.remote_id,
            "error": attempt.error,
            "response_time_ms": (
                int((attempt.ended_at - attempt.started_at).total_seconds() * 1000)
                if attempt.ended_at
                else None
            ),
        }

        self._attempts.append(attempt_record)
        await self._save()

    async def get_attempts_for_job(self, job_id: UUID) -> list[dict]:
        """Get all attempts for a specific job."""
        await self._load()
        return [a for a in self._attempts if a["job_id"] == str(job_id)]

    async def get_attempts_for_notification(self, notification_id: str) -> list[dict]:
        """Get all attempts for a specific notification."""
        await self._load()
        return [a for a in self._attempts if a["notification_id"] == notification_id]

    async def count_attempts(self, job_id: UUID) -> int:
        """Count attempts for a job."""
        await self._load()
        return sum(1 for a in self._attempts if a["job_id"] == str(job_id))

    async def get_next_attempt_number(self, job_id: UUID) -> int:
        """Get next attempt number for a job."""
        count = await self.count_attempts(job_id)
        return count + 1

    async def cleanup_old(self, before: datetime) -> int:
        """Remove attempts older than given timestamp."""
        if not self._enabled:
            return 0

        await self._load()

        original_count = len(self._attempts)
        self._attempts = [
            a
            for a in self._attempts
            if datetime.fromisoformat(a["started_at"]) >= before
        ]

        removed = original_count - len(self._attempts)
        if removed > 0:
            await self._save()
            _LOGGER.debug("Cleaned up %d old delivery attempts", removed)

        return removed


class RetryQueue:
    """Active retry queue for recovery.

    Stores pending retries with:
    - job_id, scheduled_at, reason
    - task_snapshot (full task data for recovery)

    This file is small (only pending retries) and enables fast startup recovery.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the retry queue."""
        self._hass = hass
        self._storage_path = Path(
            hass.config.path(".storage", SYS_STORAGE_RETRIES_FILE)
        )
        self._retries: list[dict] = []
        self._loaded = False

    async def _load(self) -> None:
        """Load retry queue from JSON file."""
        if self._loaded:
            return

        try:
            if self._storage_path.exists():

                def _read():
                    with open(self._storage_path, encoding="utf-8") as f:
                        return json.load(f)

                self._retries = await self._hass.async_add_executor_job(_read)
                _LOGGER.debug("Loaded %d pending retries", len(self._retries))
        except (OSError, json.JSONDecodeError) as e:
            _LOGGER.error("Failed to load retry queue: %s", e)
            self._retries = []

        self._loaded = True

    async def _save(self) -> None:
        """Save retry queue to JSON file."""
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)

            def _write():
                with open(self._storage_path, "w", encoding="utf-8") as f:
                    json.dump(self._retries, f, indent=2)

            await self._hass.async_add_executor_job(_write)
        except OSError as e:
            _LOGGER.error("Failed to save retry queue: %s", e)

    async def schedule_retry(
        self,
        job_id: UUID,
        scheduled_at: datetime,
        reason: str,
        task: "NotificationDeliveryTask",
    ) -> None:
        """Schedule a retry."""
        await self._load()

        # Remove existing retry for this job if any
        self._retries = [r for r in self._retries if r["job_id"] != str(job_id)]

        retry_entry = {
            "job_id": str(job_id),
            "notification_id": task.payload.notification_id,  # Link for debugging
            "scheduled_at": scheduled_at.isoformat(),
            "reason": reason,
            "task_snapshot": task.to_dict(),
        }

        self._retries.append(retry_entry)
        await self._save()

    async def remove_retry(self, job_id: UUID) -> None:
        """Remove a retry from the queue (after completion)."""
        await self._load()

        self._retries = [r for r in self._retries if r["job_id"] != str(job_id)]
        await self._save()

    async def get_pending_retries(self) -> list[tuple[UUID, datetime, dict]]:
        """Get all pending retries with their task snapshots."""
        await self._load()

        pending = []
        for retry in self._retries:
            try:
                job_id = UUID(retry["job_id"])
                scheduled_at = datetime.fromisoformat(retry["scheduled_at"])
                task_snapshot = retry["task_snapshot"]
                pending.append((job_id, scheduled_at, task_snapshot))
            except (ValueError, KeyError) as e:
                _LOGGER.warning("Invalid retry entry: %s", e)

        return pending

    async def cleanup_old(self, before: datetime) -> int:
        """Remove stale retries older than given timestamp."""
        await self._load()

        original_count = len(self._retries)
        self._retries = [
            r
            for r in self._retries
            if datetime.fromisoformat(r["scheduled_at"]) >= before
        ]

        removed = original_count - len(self._retries)
        if removed > 0:
            await self._save()
            _LOGGER.debug("Cleaned up %d stale retries", removed)

        return removed


class AcknowledgementRegistry:
    """Registry of notification acknowledgements.

    Each record tracks the full lifecycle of an acknowledgement:

    - ``status: "pending"``  — notification delivered; awaiting user interaction.
    - ``status: "acknowledged"`` — user interacted; acknowledgement recorded.

    Schema per record::

        {
            "notification_id": "<ANS UUID string>",
            "channel_id":      "<delivery channel, e.g. notify.mobile_app_X>",
            "status":          "pending" | "acknowledged",
            "delivered_at":    "<ISO-8601 UTC>" ,   # set when pending
            "acknowledged_at": "<ISO-8601 UTC>",    # set when acknowledged
        }

    Older records that pre-date the status field are treated as
    ``status: "acknowledged"`` for backward compatibility.

    All mutation methods are serialised by an :class:`asyncio.Lock` and use an
    atomic rename-based write strategy to prevent file corruption.
    """

    def __init__(self, hass: HomeAssistant, enabled: bool = True) -> None:
        """Initialize the acknowledgement registry.

        Args:
            hass: Home Assistant instance.
            enabled: When False all methods are no-ops (no file I/O).

        """
        self._hass = hass
        self._enabled = enabled
        self._storage_path = Path(
            hass.config.path(".storage", SYS_STORAGE_ACKNOWLEDGEMENTS_FILE)
        )
        self._acks: list[dict] = []
        self._loaded = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load(self) -> None:
        """Load acknowledgements from JSON file (idempotent; guarded by _loaded flag)."""
        if self._loaded:
            return

        try:
            if self._storage_path.exists():

                def _read():
                    with open(self._storage_path, encoding="utf-8") as f:
                        return json.load(f)

                self._acks = await self._hass.async_add_executor_job(_read)
                _LOGGER.debug("Loaded %d acknowledgements", len(self._acks))
        except (OSError, json.JSONDecodeError) as e:
            _LOGGER.error("Failed to load acknowledgements: %s", e)
            self._acks = []

        self._loaded = True

    def _write_atomic(self, data: list) -> None:
        """Serialize *data* to the storage path using an atomic tmp-file + rename.

        Called from an executor thread via :meth:`_save`.
        """
        parent = self._storage_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self._storage_path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    async def _save(self) -> None:
        """Persist the current in-memory list atomically.

        Must only be called while ``self._lock`` is held so that the snapshot
        passed to the executor is consistent.
        """
        data = list(self._acks)  # snapshot inside the lock; safe to pass to thread
        try:
            await self._hass.async_add_executor_job(self._write_atomic, data)
        except OSError as e:
            _LOGGER.error("Failed to save acknowledgements: %s", e)

    @staticmethod
    def _record_timestamp(record: dict) -> datetime:
        """Return the most relevant datetime for retention purposes.

        For acknowledged records the ``acknowledged_at`` field is used;
        for pending records ``delivered_at`` is used.  Falls back to
        ``datetime.max`` (UTC-aware) so records with missing timestamps
        are never cleaned up accidentally.
        """

        ts = record.get("acknowledged_at") or record.get("delivered_at")
        if ts:
            return datetime.fromisoformat(ts)
        return datetime.max.replace(tzinfo=UTC)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def mark_pending(
        self,
        notification_id: str,
        channel_id: str,
        delivered_at: datetime,
        mobile_tag: str | None = None,
    ) -> bool:
        """Record that a notification has been delivered and awaits acknowledgement.

        Args:
            notification_id: ANS notification UUID (string).
            channel_id: Delivery channel (e.g. ``notify.mobile_app_my_phone``).
            delivered_at: UTC datetime when delivery succeeded.
            mobile_tag: Effective ``data.tag`` sent to the mobile device.  Only
                stored when it differs from *notification_id* (i.e. the caller
                used ``channel_data.tag`` to set a custom tag).  Persisted so
                the custom-tag → notification_id mapping survives HA restarts.

        Returns:
            True if a new pending record was created; False if a record for
            this notification_id already exists (pending or acknowledged).

        """
        if not self._enabled:
            return False

        async with self._lock:
            await self._load()
            for existing in self._acks:
                if existing["notification_id"] == notification_id:
                    # Backfill mobile_tag on the existing pending record when the
                    # new delivery provides one that the record is missing.  This
                    # happens when persistent_notification delivers first (no tag)
                    # and mobile delivers second — without this, the custom-tag →
                    # notification_id mapping is lost across HA restarts.
                    if (
                        mobile_tag
                        and mobile_tag != notification_id
                        and existing.get("status") == "pending"
                        and not existing.get("mobile_tag")
                    ):
                        existing["mobile_tag"] = mobile_tag
                        await self._save()
                    return False
            record: dict = {
                "notification_id": notification_id,
                "channel_id": channel_id,
                "status": "pending",
                "delivered_at": delivered_at.isoformat(),
            }
            if mobile_tag and mobile_tag != notification_id:
                record["mobile_tag"] = mobile_tag
            self._acks.append(record)
            await self._save()
        return True

    async def get_pending_channel_ids(self) -> dict[str, str]:
        """Return a mapping of ``{notification_id: channel_id}`` for every pending record.

        Used on startup to restore in-memory eligibility from persisted state
        so that notifications delivered before an HA restart remain acknowledgeable.

        Returns:
            Dict mapping notification_id strings to their delivery channel_id.

        """
        if not self._enabled:
            return {}

        async with self._lock:
            await self._load()
            return {
                a["notification_id"]: a["channel_id"]
                for a in self._acks
                if a.get("status") == "pending"
            }

    async def get_pending_mobile_tags(self) -> dict[str, str]:
        """Return ``{mobile_tag: notification_id}`` for pending records with a custom tag.

        Used on startup to restore the custom-tag → notification_id mapping so
        that notifications delivered before an HA restart (whose ``channel_data.tag``
        differs from the UUID) can still be acknowledged after restart.

        Returns:
            Dict mapping custom mobile tag strings to their notification_id UUID.

        """
        if not self._enabled:
            return {}

        async with self._lock:
            await self._load()
            return {
                a["mobile_tag"]: a["notification_id"]
                for a in self._acks
                if a.get("status") == "pending" and a.get("mobile_tag")
            }

    async def record_acknowledgement(
        self,
        notification_id: str,
        channel_id: str,
        acknowledged_at: datetime,
    ) -> bool:
        """Transition a pending record to acknowledged, or create an acknowledged record.

        When a ``pending`` record exists for *notification_id* it is updated
        in-place (``status`` → ``"acknowledged"``, ``acknowledged_at`` set).
        When no prior record exists an acknowledged record is written directly
        for robustness (e.g. if ``mark_pending`` was skipped).
        A second call for an already-acknowledged notification returns ``False``
        without modifying storage.

        Args:
            notification_id: ANS notification UUID (string).
            channel_id: Channel through which the acknowledgement arrived.
            acknowledged_at: UTC datetime when the acknowledgement was received.

        Returns:
            True if the notification was newly acknowledged; False if it was
            already acknowledged (idempotency guard).

        """
        if not self._enabled:
            return False

        async with self._lock:
            await self._load()

            for record in self._acks:
                if record["notification_id"] == notification_id:
                    # Treat legacy records (no status field) as acknowledged.
                    if record.get("status", "acknowledged") == "acknowledged":
                        return False
                    # Transition pending → acknowledged in-place.
                    record["status"] = "acknowledged"
                    record["acknowledged_at"] = acknowledged_at.isoformat()
                    await self._save()
                    return True

            # No prior record — write an acknowledged record directly.
            self._acks.append(
                {
                    "notification_id": notification_id,
                    "channel_id": channel_id,
                    "status": "acknowledged",
                    "acknowledged_at": acknowledged_at.isoformat(),
                }
            )
            await self._save()
        return True

    async def is_acknowledged(self, notification_id: str) -> bool:
        """Return True if the notification has been acknowledged.

        Args:
            notification_id: ANS notification UUID (string).

        """
        if not self._enabled:
            return False

        async with self._lock:
            await self._load()
        return any(
            a["notification_id"] == notification_id
            and a.get("status", "acknowledged") == "acknowledged"
            for a in self._acks
        )

    async def get_acknowledgement(self, notification_id: str) -> dict | None:
        """Return the acknowledgement record for the given notification, or None.

        Only returns records with ``status: "acknowledged"``.

        Args:
            notification_id: ANS notification UUID (string).

        """
        if not self._enabled:
            return None

        async with self._lock:
            await self._load()
        return next(
            (
                a
                for a in self._acks
                if a["notification_id"] == notification_id
                and a.get("status", "acknowledged") == "acknowledged"
            ),
            None,
        )

    async def cleanup_old(self, before: datetime) -> int:
        """Remove records whose effective timestamp is older than *before*.

        For acknowledged records the ``acknowledged_at`` timestamp is used;
        for pending records ``delivered_at`` is used.

        Args:
            before: UTC cutoff; records timestamped before this are removed.

        Returns:
            Number of records removed.

        """
        if not self._enabled:
            return 0

        async with self._lock:
            await self._load()
            original_count = len(self._acks)
            self._acks = [a for a in self._acks if self._record_timestamp(a) >= before]
            removed = original_count - len(self._acks)
            if removed > 0:
                await self._save()
                _LOGGER.debug("Cleaned up %d old acknowledgements", removed)

        return removed
