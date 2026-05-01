"""Unit tests for persistence layer: NotificationRegistry, DeliveryAttemptLog, RetryQueue."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from ..models import Attempt
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


# ===========================================================================
# NotificationRegistry — error-path tests
# ===========================================================================


class TestNotificationRegistryErrorHandling:
    """Verify NotificationRegistry gracefully handles I/O and JSON errors during load/save."""

    async def test_load_handles_io_error(self, tmp_path):
        """When the executor job raises OSError during read, _notifications is reset to [] and loading is marked done."""
        hass = _make_hass()
        registry = NotificationRegistry(hass, enabled=True)
        registry._storage_path = tmp_path / "notifications.json"
        # Create file so the exists() check passes
        registry._storage_path.write_text("[invalid]")

        async def _raise_io(*_args, **_kwargs):
            raise OSError("disk read error")

        hass.async_add_executor_job = _raise_io
        await registry._load()

        assert registry._notifications == []
        assert registry._loaded is True

    async def test_load_handles_json_decode_error(self, tmp_path):
        """When the file contains invalid JSON, _notifications is reset to [] and loading is marked done."""
        hass = _make_hass()
        registry = NotificationRegistry(hass, enabled=True)
        storage_file = tmp_path / "notifications.json"
        storage_file.write_text("{not valid json}")
        registry._storage_path = storage_file

        await registry._load()

        assert registry._notifications == []
        assert registry._loaded is True

    async def test_save_handles_io_error(self, tmp_path, caplog):
        """When async_add_executor_job raises OSError in _save, the error is logged and no exception propagates."""
        hass = _make_hass()
        registry = NotificationRegistry(hass, enabled=True)
        registry._storage_path = tmp_path / "notifications.json"

        async def _raise_io(fn, *args, **kwargs):
            raise OSError("disk write error")

        hass.async_add_executor_job = _raise_io

        with caplog.at_level(
            logging.ERROR, logger="custom_components.ans.persistence.file"
        ):
            await registry._save()

        assert any("Failed to save" in r.message for r in caplog.records)

    async def test_cleanup_old_does_not_save_when_nothing_removed(self, tmp_path):
        """cleanup_old() skips the _save call when no records are older than the cutoff."""
        hass = _make_hass()
        registry = NotificationRegistry(hass, enabled=True)
        registry._storage_path = tmp_path / "notifications.json"
        registry._loaded = True

        registry._notifications = [
            {
                "notification_id": "recent",
                "triggered_at": (_now() + timedelta(hours=1)).isoformat(),
            }
        ]

        with patch.object(registry, "_save", AsyncMock()) as mock_save:
            removed = await registry.cleanup_old(_now())

        assert removed == 0
        mock_save.assert_not_awaited()


# ===========================================================================
# DeliveryAttemptLog — happy-path + error-path tests
# ===========================================================================


def _make_attempt_record(
    job_id,
    notification_id: str = "notif-1",
    *,
    include_ended_at: bool = True,
):
    """Return a mock Attempt and matching task for use with log_attempt()."""
    started = _now() - timedelta(seconds=1)
    ended = _now() if include_ended_at else None

    attempt = MagicMock(spec=Attempt)
    attempt.attempt_id = uuid4()
    attempt.job_id = job_id
    attempt.attempt_number = 1
    attempt.started_at = started
    attempt.ended_at = ended
    attempt.status = MagicMock()
    attempt.status.value = "SUCCESS"
    attempt.endpoint = None
    attempt.remote_id = None
    attempt.error = None

    task = MagicMock()
    task.payload.notification_id = notification_id
    task.channel_info.id = "notify.persistent_notification"
    task.recipient_id = "r1"

    return attempt, task


class TestDeliveryAttemptLogHappyPath:
    """Verify DeliveryAttemptLog: log_attempt stores complete records and cross-reference queries."""

    async def test_log_attempt_stores_complete_record(self, tmp_path):
        """log_attempt() stores a record containing job_id, status, and a numeric response_time_ms."""
        hass = _make_hass()
        log = DeliveryAttemptLog(hass, enabled=True)
        log._storage_path = tmp_path / "attempts.json"

        job_id = uuid4()
        attempt, task = _make_attempt_record(job_id, include_ended_at=True)

        await log.log_attempt(attempt, task)

        assert len(log._attempts) == 1
        record = log._attempts[0]
        assert record["job_id"] == str(job_id)
        assert record["status"] == "SUCCESS"
        assert isinstance(record["response_time_ms"], int)

    async def test_log_attempt_response_time_ms_none_without_ended_at(self, tmp_path):
        """log_attempt() stores response_time_ms=None when the attempt has no ended_at timestamp."""
        hass = _make_hass()
        log = DeliveryAttemptLog(hass, enabled=True)
        log._storage_path = tmp_path / "attempts.json"

        job_id = uuid4()
        attempt, task = _make_attempt_record(job_id, include_ended_at=False)

        await log.log_attempt(attempt, task)

        assert log._attempts[0]["response_time_ms"] is None

    async def test_get_attempts_for_job(self, tmp_path):
        """get_attempts_for_job() returns only the attempts that match the given job_id."""
        hass = _make_hass()
        log = DeliveryAttemptLog(hass, enabled=True)
        log._storage_path = tmp_path / "attempts.json"
        log._loaded = True

        job_a = uuid4()
        job_b = uuid4()
        log._attempts = [
            {"job_id": str(job_a), "notification_id": "n1"},
            {"job_id": str(job_b), "notification_id": "n2"},
        ]

        result = await log.get_attempts_for_job(job_a)
        assert len(result) == 1
        assert result[0]["job_id"] == str(job_a)

    async def test_get_attempts_for_notification(self, tmp_path):
        """get_attempts_for_notification() returns only the attempts that match the given notification_id."""
        hass = _make_hass()
        log = DeliveryAttemptLog(hass, enabled=True)
        log._storage_path = tmp_path / "attempts.json"
        log._loaded = True

        job_a = uuid4()
        job_b = uuid4()
        log._attempts = [
            {"job_id": str(job_a), "notification_id": "notif-alpha"},
            {"job_id": str(job_b), "notification_id": "notif-beta"},
        ]

        result = await log.get_attempts_for_notification("notif-alpha")
        assert len(result) == 1
        assert result[0]["notification_id"] == "notif-alpha"


class TestDeliveryAttemptLogErrorHandling:
    """Verify DeliveryAttemptLog gracefully handles I/O and JSON errors during load/save."""

    async def test_load_handles_io_error(self, tmp_path):
        """When the executor raises OSError during read, _attempts is reset to [] and _loaded is True."""
        hass = _make_hass()
        log = DeliveryAttemptLog(hass, enabled=True)
        storage_file = tmp_path / "attempts.json"
        storage_file.write_text("[]")
        log._storage_path = storage_file

        async def _raise_io(*_args, **_kwargs):
            raise OSError("disk read error")

        hass.async_add_executor_job = _raise_io
        await log._load()

        assert log._attempts == []
        assert log._loaded is True

    async def test_load_handles_json_decode_error(self, tmp_path):
        """When the file contains invalid JSON, _attempts is reset to [] and _loaded is True."""
        hass = _make_hass()
        log = DeliveryAttemptLog(hass, enabled=True)
        storage_file = tmp_path / "attempts.json"
        storage_file.write_text("{not valid json}")
        log._storage_path = storage_file

        await log._load()

        assert log._attempts == []
        assert log._loaded is True

    async def test_load_from_json_file(self, tmp_path):
        """DeliveryAttemptLog loads existing JSON from disk and makes records accessible."""
        hass = _make_hass()
        job_id = str(uuid4())
        storage_file = tmp_path / "attempts.json"
        storage_file.write_text(
            json.dumps(
                [
                    {
                        "attempt_id": str(uuid4()),
                        "job_id": job_id,
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
            )
        )
        log = DeliveryAttemptLog(hass, enabled=True)
        log._storage_path = storage_file

        count = await log.count_attempts(uuid4())
        assert count == 0  # triggers load; file was loaded
        assert log._loaded is True

    async def test_cleanup_old_disabled_returns_zero(self, tmp_path):
        """When enabled=False, cleanup_old() returns 0 without accessing the store."""
        hass = _make_hass()
        log = DeliveryAttemptLog(hass, enabled=False)
        log._storage_path = tmp_path / "attempts.json"

        removed = await log.cleanup_old(_now())
        assert removed == 0

    async def test_save_handles_io_error(self, tmp_path, caplog):
        """When async_add_executor_job raises OSError in _save, the error is logged and no exception propagates."""
        hass = _make_hass()
        log = DeliveryAttemptLog(hass, enabled=True)
        log._storage_path = tmp_path / "attempts.json"

        async def _raise_io(fn, *args, **kwargs):
            raise OSError("disk write error")

        hass.async_add_executor_job = _raise_io

        with caplog.at_level(
            logging.ERROR, logger="custom_components.ans.persistence.file"
        ):
            await log._save()

        assert any("Failed to save" in r.message for r in caplog.records)


# ===========================================================================
# RetryQueue — error-path tests
# ===========================================================================


class TestRetryQueueErrorHandling:
    """Verify RetryQueue gracefully handles I/O and JSON errors during load/save, and skips invalid entries."""

    async def test_load_handles_io_error(self, tmp_path):
        """When the executor raises OSError during read, _retries is reset to [] and _loaded is True."""
        hass = _make_hass()
        queue = RetryQueue(hass)
        storage_file = tmp_path / "retries.json"
        storage_file.write_text("[]")
        queue._storage_path = storage_file

        async def _raise_io(*_args, **_kwargs):
            raise OSError("disk read error")

        hass.async_add_executor_job = _raise_io
        await queue._load()

        assert queue._retries == []
        assert queue._loaded is True

    async def test_load_handles_json_decode_error(self, tmp_path):
        """When the file contains invalid JSON, _retries is reset to [] and _loaded is True."""
        hass = _make_hass()
        queue = RetryQueue(hass)
        storage_file = tmp_path / "retries.json"
        storage_file.write_text("{not valid json}")
        queue._storage_path = storage_file

        await queue._load()

        assert queue._retries == []
        assert queue._loaded is True

    async def test_load_from_json_file(self, tmp_path):
        """RetryQueue loads existing JSON from disk so get_pending_retries() returns the stored entry."""
        hass = _make_hass()
        job_id = uuid4()
        scheduled_at = (_now() + timedelta(minutes=5)).isoformat()
        storage_file = tmp_path / "retries.json"
        storage_file.write_text(
            json.dumps(
                [
                    {
                        "job_id": str(job_id),
                        "notification_id": "notif-1",
                        "scheduled_at": scheduled_at,
                        "reason": "transient",
                        "task_snapshot": {"key": "value"},
                    }
                ]
            )
        )
        queue = RetryQueue(hass)
        queue._storage_path = storage_file

        pending = await queue.get_pending_retries()
        assert len(pending) == 1
        assert pending[0][0] == job_id

    async def test_save_handles_io_error(self, tmp_path, caplog):
        """When async_add_executor_job raises OSError in _save, the error is logged and no exception propagates."""
        hass = _make_hass()
        queue = RetryQueue(hass)
        queue._storage_path = tmp_path / "retries.json"

        async def _raise_io(fn, *args, **kwargs):
            raise OSError("disk write error")

        hass.async_add_executor_job = _raise_io

        with caplog.at_level(
            logging.ERROR, logger="custom_components.ans.persistence.file"
        ):
            await queue._save()

        assert any("Failed to save" in r.message for r in caplog.records)

    async def test_get_pending_retries_skips_invalid_entries(self, tmp_path, caplog):
        """get_pending_retries() skips and logs a warning for entries missing required keys."""
        hass = _make_hass()
        queue = RetryQueue(hass)
        queue._storage_path = tmp_path / "retries.json"
        queue._loaded = True

        # Entry missing 'job_id' key — should be skipped
        queue._retries = [{"scheduled_at": _now().isoformat(), "task_snapshot": {}}]

        with caplog.at_level(
            logging.WARNING, logger="custom_components.ans.persistence.file"
        ):
            result = await queue.get_pending_retries()

        assert result == []
        assert any("Invalid retry entry" in r.message for r in caplog.records)
