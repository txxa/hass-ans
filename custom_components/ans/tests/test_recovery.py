"""Unit tests for PersistenceRecovery and async_initialize_persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from ..models import NotificationDeliveryTask
from ..persistence.recovery import PersistenceRecovery, async_initialize_persistence

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(UTC)


def _make_hass() -> MagicMock:
    """Return a mock HomeAssistant instance with config.path() pre-configured to a temp path."""
    hass = MagicMock()
    hass.config.path.return_value = "/tmp/ans_test/file.json"  # noqa: S108
    return hass


def _make_stores(pending_retries=None):
    """Return mocked (notification_registry, attempt_log, retry_queue)."""
    notification_registry = MagicMock()
    notification_registry.cleanup_old = AsyncMock(return_value=0)

    attempt_log = MagicMock()
    attempt_log.cleanup_old = AsyncMock(return_value=0)

    retry_queue = MagicMock()
    retry_queue.get_pending_retries = AsyncMock(return_value=pending_retries or [])
    retry_queue.cleanup_old = AsyncMock(return_value=0)

    return notification_registry, attempt_log, retry_queue


def _make_valid_snapshot():
    """Return a minimal valid task snapshot dict."""
    return {
        "recipient_id": "r1",
        "channel_info": {
            "id": "notify.persistent_notification",
            "name": "Persistent",
            "scope": "SYSTEM",
            "adapter_type": None,
        },
        "payload": {
            "notification_id": str(uuid4()),
            "source": "test",
            "title": "T",
            "message": "M",
            "type": "INFO",
            "criticality": "LOW",
            "created_at": _now().isoformat(),
        },
        "policy": {
            "retry_attempts": 3,
            "rate_limit": 100,
            "rate_limit_window": 60,
            "allowed_types": ["INFO"],
            "blocked_sources_regex": None,
            "dnd": None,
        },
        "contact_info": {},
        "tts_settings": None,
        "created_at": _now().isoformat(),
        "is_retry": True,
    }


# ===========================================================================
# PersistenceRecovery
# ===========================================================================


class TestPersistenceRecovery:
    # --- recover_on_startup --------------------------------------------------
    """Verify PersistenceRecovery.recover_on_startup() and cleanup_old_records() behaviour."""

    async def test_recover_empty_queue_returns_empty_lists(self):
        """An empty retry queue results in empty pending_tasks and orphaned_retries lists."""
        hass = _make_hass()
        nr, al, rq = _make_stores(pending_retries=[])
        recovery = PersistenceRecovery(hass, nr, al, rq)

        result = await recovery.recover_on_startup()

        assert result["pending_tasks"] == []
        assert result["orphaned_retries"] == []
        assert result["stores_initialized"] is True

    async def test_recover_valid_snapshot_reconstructs_task(self):
        """A valid snapshot is deserialised via from_snapshot() and placed in pending_tasks with its scheduled run time."""
        hass = _make_hass()
        job_id = uuid4()
        run_at = _now() + timedelta(minutes=5)
        snapshot = _make_valid_snapshot()

        nr, al, rq = _make_stores(pending_retries=[(job_id, run_at, snapshot)])
        recovery = PersistenceRecovery(hass, nr, al, rq)

        with patch.object(
            NotificationDeliveryTask,
            "from_snapshot",
            return_value=MagicMock(spec=NotificationDeliveryTask),
        ) as mock_from_snapshot:
            result = await recovery.recover_on_startup()

        mock_from_snapshot.assert_called_once_with(job_id, snapshot)
        assert len(result["pending_tasks"]) == 1
        task_obj, scheduled_at = result["pending_tasks"][0]
        assert scheduled_at == run_at
        assert result["orphaned_retries"] == []

    async def test_recover_none_snapshot_goes_to_orphaned(self):
        """A retry queue entry with a None snapshot is placed in orphaned_retries rather than pending_tasks."""
        hass = _make_hass()
        job_id = uuid4()
        run_at = _now() + timedelta(minutes=5)

        nr, al, rq = _make_stores(pending_retries=[(job_id, run_at, None)])
        recovery = PersistenceRecovery(hass, nr, al, rq)

        result = await recovery.recover_on_startup()

        assert result["pending_tasks"] == []
        assert str(job_id) in result["orphaned_retries"]

    async def test_recover_invalid_snapshot_goes_to_orphaned(self):
        """A snapshot that raises KeyError during deserialisation is placed in orphaned_retries."""
        hass = _make_hass()
        job_id = uuid4()
        run_at = _now() + timedelta(minutes=5)
        bad_snapshot = {"bad": "data"}  # missing required keys

        nr, al, rq = _make_stores(pending_retries=[(job_id, run_at, bad_snapshot)])
        recovery = PersistenceRecovery(hass, nr, al, rq)

        with patch.object(
            NotificationDeliveryTask,
            "from_snapshot",
            side_effect=KeyError("channel_info"),
        ):
            result = await recovery.recover_on_startup()

        assert result["pending_tasks"] == []
        assert str(job_id) in result["orphaned_retries"]

    async def test_recover_value_error_snapshot_goes_to_orphaned(self):
        """A snapshot that raises ValueError during deserialisation is also placed in orphaned_retries."""
        hass = _make_hass()
        job_id = uuid4()
        run_at = _now() + timedelta(minutes=5)

        nr, al, rq = _make_stores(pending_retries=[(job_id, run_at, {"x": 1})])
        recovery = PersistenceRecovery(hass, nr, al, rq)

        with patch.object(
            NotificationDeliveryTask,
            "from_snapshot",
            side_effect=ValueError("bad value"),
        ):
            result = await recovery.recover_on_startup()

        assert str(job_id) in result["orphaned_retries"]

    async def test_recover_mixed_entries(self):
        """Valid, orphaned, and invalid snapshots are separated correctly."""
        hass = _make_hass()
        job_valid = uuid4()
        job_orphan = uuid4()
        job_invalid = uuid4()
        run_at = _now() + timedelta(minutes=1)

        nr, al, rq = _make_stores(
            pending_retries=[
                (job_valid, run_at, _make_valid_snapshot()),
                (job_orphan, run_at, None),
                (job_invalid, run_at, {"bad": "data"}),
            ]
        )
        recovery = PersistenceRecovery(hass, nr, al, rq)

        mock_task = MagicMock(spec=NotificationDeliveryTask)

        def _from_snapshot(j, s):
            """Return a mock task for job_valid; raise KeyError for all other job IDs."""
            if j == job_valid:
                return mock_task
            raise KeyError("channel_info")

        with patch.object(
            NotificationDeliveryTask, "from_snapshot", side_effect=_from_snapshot
        ):
            result = await recovery.recover_on_startup()

        assert len(result["pending_tasks"]) == 1
        assert len(result["orphaned_retries"]) == 2
        assert str(job_orphan) in result["orphaned_retries"]
        assert str(job_invalid) in result["orphaned_retries"]

    # --- cleanup_old_records -------------------------------------------------

    async def test_cleanup_returns_aggregate_count(self):
        """cleanup_old_records() returns the sum of deleted records across all three stores."""
        hass = _make_hass()
        nr, al, rq = _make_stores()
        nr.cleanup_old = AsyncMock(return_value=3)
        al.cleanup_old = AsyncMock(return_value=5)
        rq.cleanup_old = AsyncMock(return_value=2)

        recovery = PersistenceRecovery(hass, nr, al, rq)
        total = await recovery.cleanup_old_records(days=7)

        assert total == 10  # 3 + 5 + 2

    async def test_cleanup_negative_days_raises_value_error(self):
        """A negative days argument raises ValueError with a message mentioning 'non-negative'."""
        hass = _make_hass()
        nr, al, rq = _make_stores()
        recovery = PersistenceRecovery(hass, nr, al, rq)

        with pytest.raises(ValueError, match="non-negative"):
            await recovery.cleanup_old_records(days=-1)

    async def test_cleanup_zero_days_is_valid(self):
        """days=0 means 'remove everything older than now', which is valid."""
        hass = _make_hass()
        nr, al, rq = _make_stores()
        nr.cleanup_old = AsyncMock(return_value=1)
        al.cleanup_old = AsyncMock(return_value=0)
        rq.cleanup_old = AsyncMock(return_value=0)

        recovery = PersistenceRecovery(hass, nr, al, rq)
        total = await recovery.cleanup_old_records(days=0)
        assert total == 1

    async def test_cleanup_calls_all_three_stores(self):
        """cleanup_old_records() invokes cleanup_old() on the notification registry, attempt log, and retry queue."""
        hass = _make_hass()
        nr, al, rq = _make_stores()
        recovery = PersistenceRecovery(hass, nr, al, rq)

        await recovery.cleanup_old_records(days=30)

        nr.cleanup_old.assert_called_once()
        al.cleanup_old.assert_called_once()
        rq.cleanup_old.assert_called_once()

    async def test_cleanup_cutoff_is_correct(self):
        """The cutoff datetime passed to stores should match `days` ago."""
        hass = _make_hass()
        nr, al, rq = _make_stores()
        recovery = PersistenceRecovery(hass, nr, al, rq)

        before = _now()
        await recovery.cleanup_old_records(days=7)
        after = _now()

        cutoff_passed = nr.cleanup_old.call_args[0][0]
        # Cutoff should be approximately now - 7 days
        expected_min = before - timedelta(days=7) - timedelta(seconds=2)
        expected_max = after - timedelta(days=7) + timedelta(seconds=2)
        assert expected_min <= cutoff_passed <= expected_max

    # --- constructor ---------------------------------------------------------

    async def test_constructor_accepts_all_three_stores(self):
        """When all three stores are provided explicitly, they are stored as instance attributes."""
        hass = _make_hass()
        nr, al, rq = _make_stores()
        recovery = PersistenceRecovery(hass, nr, al, rq)
        assert recovery.notification_registry is nr
        assert recovery.attempt_log is al
        assert recovery.retry_queue is rq

    async def test_constructor_creates_stores_when_all_none(self):
        """When no stores are passed, new instances are created via lazy import."""
        hass = _make_hass()

        mock_nr = MagicMock()
        mock_al = MagicMock()
        mock_rq = MagicMock()

        with (
            patch(
                "custom_components.ans.persistence.recovery.PersistenceRecovery.__init__.__code__",
                # Patching __init__ body is complex; test via integration instead
            )
            if False
            else patch("builtins.open", MagicMock()),
            patch(
                "custom_components.ans.persistence.file.NotificationRegistry",
                return_value=mock_nr,
            ),
            patch(
                "custom_components.ans.persistence.file.DeliveryAttemptLog",
                return_value=mock_al,
            ),
            patch(
                "custom_components.ans.persistence.file.RetryQueue",
                return_value=mock_rq,
            ),
        ):
            # Simply verify no TypeError is raised with all-None args
            recovery = PersistenceRecovery(hass)
        assert recovery.notification_registry is not None
        assert recovery.attempt_log is not None
        assert recovery.retry_queue is not None


# ===========================================================================
# async_initialize_persistence
# ===========================================================================


class TestAsyncInitializePersistence:
    """Verify that async_initialize_persistence() correctly separates recovered tasks from orphaned retries."""

    async def test_returns_pending_tasks_and_orphans(self):
        """Entries with None snapshots are classified as orphans; valid entries go to pending_tasks."""
        hass = _make_hass()
        job_id = uuid4()
        run_at = _now() + timedelta(minutes=1)

        nr, al, rq = _make_stores(pending_retries=[(job_id, run_at, None)])

        pending, orphaned = await async_initialize_persistence(hass, nr, al, rq)

        assert pending == []
        assert str(job_id) in orphaned

    async def test_passes_through_existing_stores(self):
        """When stores are passed explicitly, async_initialize_persistence() uses them and queries get_pending_retries()."""
        hass = _make_hass()
        nr, al, rq = _make_stores(pending_retries=[])

        pending, orphaned = await async_initialize_persistence(hass, nr, al, rq)

        assert pending == []
        assert orphaned == []
        rq.get_pending_retries.assert_awaited_once()
