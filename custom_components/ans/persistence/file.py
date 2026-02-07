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

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from homeassistant.core import HomeAssistant

from custom_components.ans.const import (
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
            "task_snapshot": {
                "recipient_id": task.recipient_id,
                "channel_info": {
                    "id": task.channel_info.id,
                    "label": task.channel_info.label,
                    "integration": task.channel_info.integration,
                },
                "payload": {
                    "title": task.payload.title,
                    "message": task.payload.message,
                    "notification_id": task.payload.notification_id,
                    "timestamp": task.payload.created_at.isoformat(),
                    "metadata": task.payload.metadata,
                },
                "policy": {
                    "rate_limit": task.policy.rate_limit,
                    "rate_limit_window": task.policy.rate_limit_window,
                    "retry_attempts": task.policy.retry_attempts,
                },
                "contact_info": {
                    "email": task.contact_info.email_address,
                    "phone": task.contact_info.phone_number,
                },
            },
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
