"""Unit tests for persistence layer: NotificationRegistry, DeliveryAttemptLog, RetryQueue."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

from ..persistence.file import DeliveryAttemptLog, NotificationRegistry, RetryQueue


def _make_hass(storage_path: str = "/tmp/ans_test/.storage") -> MagicMock:  # noqa: S108
    """Return a mock hass with async_add_executor_job that calls functions synchronously and config.path() returning joined path segments."""
    hass = MagicMock()
    hass.config.path = MagicMock(side_effect=lambda *args: "/".join(args))

    async def _executor(fn, *args, **kwargs):
        """Run a synchronous function inline, simulating an executor job."""
        return fn(*args, **kwargs)

    hass.async_add_executor_job = _executor
    return hass


def _now() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(UTC)


# ===========================================================================
# NotificationRegistry
# ===========================================================================


class TestNotificationRegistry:
    """Verify the file-backed NotificationRegistry: register, idempotency, get, cleanup_old, and disabled no-op behaviour."""

    async def test_register_notification_new(self, tmp_path):
        """register_notification() stores a new notification that is retrievable by ID."""
        hass = _make_hass()
        hass.config.path.return_value = str(tmp_path / "notifications.json")
        registry = NotificationRegistry(hass, enabled=True)

        # Patch storage path directly
        registry._storage_path = tmp_path / "notifications.json"

        triggered_at = _now()
        await registry.register_notification(
            notification_id="notif-1",
            source="test",
            triggered_at=triggered_at,
            payload={"title": "T", "message": "M"},
            recipients=[{"recipient_id": "r1", "channels": ["notify.pn"]}],
        )

        registered = await registry.get_notification("notif-1")
        assert registered is not None
        assert registered["notification_id"] == "notif-1"
        assert registered["source"] == "test"

    async def test_register_notification_idempotent(self, tmp_path):
        """Calling register_notification() multiple times with the same notification_id stores only one entry."""
        hass = _make_hass()
        registry = NotificationRegistry(hass, enabled=True)
        registry._storage_path = tmp_path / "notifications.json"

        for _ in range(3):
            await registry.register_notification(
                notification_id="notif-1",
                source="test",
                triggered_at=_now(),
                payload={},
                recipients=[],
            )

        # Only stored once
        assert len(registry._notifications) == 1

    async def test_register_notification_disabled_is_noop(self, tmp_path):
        """When enabled=False, register_notification() stores nothing."""
        hass = _make_hass()
        registry = NotificationRegistry(hass, enabled=False)
        registry._storage_path = tmp_path / "notifications.json"

        await registry.register_notification(
            notification_id="notif-1",
            source="test",
            triggered_at=_now(),
            payload={},
            recipients=[],
        )

        assert len(registry._notifications) == 0

    async def test_get_notification_not_found(self, tmp_path):
        """get_notification() returns None when the notification_id does not exist."""
        hass = _make_hass()
        registry = NotificationRegistry(hass, enabled=True)
        registry._storage_path = tmp_path / "notifications.json"

        result = await registry.get_notification("does-not-exist")
        assert result is None

    async def test_cleanup_old_removes_old_records(self, tmp_path):
        """cleanup_old() removes notifications whose triggered_at is before the cutoff and retains newer ones."""
        hass = _make_hass()
        registry = NotificationRegistry(hass, enabled=True)
        registry._storage_path = tmp_path / "notifications.json"

        old_time = _now() - timedelta(days=40)
        new_time = _now()

        await registry.register_notification("old-notif", "test", old_time, {}, [])
        await registry.register_notification("new-notif", "test", new_time, {}, [])

        cutoff = _now() - timedelta(days=30)
        removed = await registry.cleanup_old(cutoff)

        assert removed == 1
        assert await registry.get_notification("old-notif") is None
        assert await registry.get_notification("new-notif") is not None

    async def test_cleanup_old_disabled_returns_zero(self, tmp_path):
        """When enabled=False, cleanup_old() is a no-op that returns 0."""
        hass = _make_hass()
        registry = NotificationRegistry(hass, enabled=False)
        registry._storage_path = tmp_path / "notifications.json"

        removed = await registry.cleanup_old(_now())
        assert removed == 0

    async def test_load_from_json_file(self, tmp_path):
        """Notifications written to disk are loaded and accessible via get_notification() on the next registry."""
        hass = _make_hass()
        storage_file = tmp_path / "notifications.json"
        storage_file.write_text(
            json.dumps(
                [
                    {
                        "notification_id": "from-file",
                        "source": "disk",
                        "triggered_at": _now().isoformat(),
                        "payload": {},
                        "recipients": [],
                    }
                ]
            )
        )
        registry = NotificationRegistry(hass, enabled=True)
        registry._storage_path = storage_file

        result = await registry.get_notification("from-file")
        assert result is not None
        assert result["source"] == "disk"


# ===========================================================================
# DeliveryAttemptLog
# ===========================================================================


class TestDeliveryAttemptLog:
    """Verify the file-backed DeliveryAttemptLog: attempt numbering, count, log_attempt no-op when disabled, and cleanup_old."""

    async def test_get_next_attempt_number_starts_at_one(self, tmp_path):
        """get_next_attempt_number() returns 1 when no attempts have been logged for the job."""
        hass = _make_hass()
        log = DeliveryAttemptLog(hass, enabled=True)
        log._storage_path = tmp_path / "attempts.json"
        job_id = uuid4()

        n = await log.get_next_attempt_number(job_id)
        assert n == 1

    async def test_get_next_attempt_number_increments(self, tmp_path):
        """get_next_attempt_number() returns max(existing attempt_number) + 1."""
        hass = _make_hass()
        log = DeliveryAttemptLog(hass, enabled=True)
        log._storage_path = tmp_path / "attempts.json"
        job_id = uuid4()

        # Manually inject an attempt record
        log._loaded = True
        log._attempts = [
            {
                "attempt_id": str(uuid4()),
                "job_id": str(job_id),
                "notification_id": "notif-1",
                "channel_id": "notify.pn",
                "recipient_id": "r1",
                "attempt_number": 1,
                "started_at": _now().isoformat(),
                "ended_at": None,
                "status": "SUCCESS",
                "endpoint": None,
                "remote_id": None,
                "error": None,
                "response_time_ms": None,
            }
        ]

        n = await log.get_next_attempt_number(job_id)
        assert n == 2

    async def test_count_attempts_empty(self, tmp_path):
        """count_attempts() returns 0 when no attempts have been logged for the job."""
        hass = _make_hass()
        log = DeliveryAttemptLog(hass, enabled=True)
        log._storage_path = tmp_path / "attempts.json"
        job_id = uuid4()

        count = await log.count_attempts(job_id)
        assert count == 0

    async def test_disabled_log_attempt_is_noop(self, tmp_path):
        """When enabled=False, log_attempt() stores nothing."""
        hass = _make_hass()
        log = DeliveryAttemptLog(hass, enabled=False)
        log._storage_path = tmp_path / "attempts.json"

        attempt = MagicMock()
        task = MagicMock()
        await log.log_attempt(attempt, task)

        assert len(log._attempts) == 0

    async def test_cleanup_old_attempts(self, tmp_path):
        """cleanup_old() removes attempt records whose started_at is before the cutoff and retains newer ones."""
        hass = _make_hass()
        log = DeliveryAttemptLog(hass, enabled=True)
        log._storage_path = tmp_path / "attempts.json"
        log._loaded = True

        old_time = (_now() - timedelta(days=40)).isoformat()
        new_time = _now().isoformat()

        log._attempts = [
            {"started_at": old_time, "job_id": "j1"},
            {"started_at": new_time, "job_id": "j2"},
        ]

        cutoff = _now() - timedelta(days=30)
        removed = await log.cleanup_old(cutoff)
        assert removed == 1


# ===========================================================================
# RetryQueue
# ===========================================================================


class TestRetryQueue:
    """Verify the file-backed RetryQueue: schedule, replace, remove, get_pending_retries, and cleanup_old."""

    async def test_schedule_retry_adds_entry(self, tmp_path):
        """schedule_retry() stores a retry entry that is retrievable via get_pending_retries()."""
        hass = _make_hass()
        queue = RetryQueue(hass)
        queue._storage_path = tmp_path / "retries.json"
        job_id = uuid4()

        task = MagicMock()
        task.payload.notification_id = "notif-1"
        task.to_dict.return_value = {"job_id": str(job_id)}

        scheduled_at = _now() + timedelta(minutes=5)
        await queue.schedule_retry(job_id, scheduled_at, "transient", task)

        pending = await queue.get_pending_retries()
        assert len(pending) == 1
        assert pending[0][0] == job_id

    async def test_schedule_retry_replaces_existing(self, tmp_path):
        """A second call to schedule_retry() with the same job_id replaces the existing entry (only one entry remains)."""
        hass = _make_hass()
        queue = RetryQueue(hass)
        queue._storage_path = tmp_path / "retries.json"
        job_id = uuid4()

        task = MagicMock()
        task.payload.notification_id = "notif-1"
        task.to_dict.return_value = {"job_id": str(job_id)}

        scheduled_at = _now() + timedelta(minutes=5)
        await queue.schedule_retry(job_id, scheduled_at, "reason1", task)
        await queue.schedule_retry(job_id, scheduled_at, "reason2", task)

        # Still only one entry for this job
        assert len(queue._retries) == 1

    async def test_remove_retry(self, tmp_path):
        """remove_retry() deletes the retry entry so get_pending_retries() returns an empty list."""
        hass = _make_hass()
        queue = RetryQueue(hass)
        queue._storage_path = tmp_path / "retries.json"
        job_id = uuid4()

        task = MagicMock()
        task.payload.notification_id = "notif-1"
        task.to_dict.return_value = {"job_id": str(job_id)}

        scheduled_at = _now() + timedelta(minutes=5)
        await queue.schedule_retry(job_id, scheduled_at, "reason", task)
        await queue.remove_retry(job_id)

        pending = await queue.get_pending_retries()
        assert len(pending) == 0

    async def test_cleanup_old_retries(self, tmp_path):
        """cleanup_old() removes retry entries whose scheduled_at is before the cutoff and retains future ones."""
        hass = _make_hass()
        queue = RetryQueue(hass)
        queue._storage_path = tmp_path / "retries.json"
        queue._loaded = True

        old_time = (_now() - timedelta(days=40)).isoformat()
        new_time = (_now() + timedelta(hours=1)).isoformat()

        queue._retries = [
            {
                "job_id": str(uuid4()),
                "notification_id": "notif-1",
                "scheduled_at": old_time,
                "reason": "old",
                "task_snapshot": {},
            },
            {
                "job_id": str(uuid4()),
                "notification_id": "notif-2",
                "scheduled_at": new_time,
                "reason": "new",
                "task_snapshot": {},
            },
        ]

        cutoff = _now()
        removed = await queue.cleanup_old(cutoff)
        assert removed == 1
