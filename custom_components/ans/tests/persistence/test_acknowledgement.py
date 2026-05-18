"""Unit tests for AcknowledgementRegistry persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from ...persistence.file import AcknowledgementRegistry


def _make_hass() -> MagicMock:
    """Return a mock hass whose async_add_executor_job runs functions synchronously."""
    hass = MagicMock()

    async def _executor(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    hass.async_add_executor_job = _executor
    return hass


def _now() -> datetime:
    return datetime.now(UTC)


def _registry(tmp_path, enabled: bool = True) -> AcknowledgementRegistry:
    hass = _make_hass()
    reg = AcknowledgementRegistry(hass, enabled=enabled)
    reg._storage_path = tmp_path / "ans_acknowledgements.json"
    return reg


class TestAcknowledgementRegistry:
    """Verify AcknowledgementRegistry: record, idempotency, query, cleanup, persistence."""

    async def test_record_new_acknowledgement_returns_true(self, tmp_path):
        """record_acknowledgement() returns True for a previously unseen notification_id."""
        reg = _registry(tmp_path)
        result = await reg.record_acknowledgement("nid-1", "mobile_app", _now())
        assert result is True

    async def test_record_duplicate_returns_false(self, tmp_path):
        """A second record_acknowledgement() call for the same ID returns False."""
        reg = _registry(tmp_path)
        await reg.record_acknowledgement("nid-1", "mobile_app", _now())
        result = await reg.record_acknowledgement("nid-1", "mobile_app", _now())
        assert result is False

    async def test_record_duplicate_does_not_overwrite(self, tmp_path):
        """The original acknowledgement record is not overwritten by a duplicate call."""
        reg = _registry(tmp_path)
        original_time = _now()
        await reg.record_acknowledgement("nid-1", "mobile_app", original_time)
        await reg.record_acknowledgement(
            "nid-1", "mobile_app", _now() + timedelta(hours=1)
        )

        ack = await reg.get_acknowledgement("nid-1")
        assert ack is not None
        assert ack["acknowledged_at"] == original_time.isoformat()

    async def test_is_acknowledged_true_after_record(self, tmp_path):
        """is_acknowledged() returns True after a successful record_acknowledgement()."""
        reg = _registry(tmp_path)
        await reg.record_acknowledgement("nid-2", "mobile_app", _now())
        assert await reg.is_acknowledged("nid-2") is True

    async def test_is_acknowledged_false_before_record(self, tmp_path):
        """is_acknowledged() returns False for a notification that has never been acked."""
        reg = _registry(tmp_path)
        assert await reg.is_acknowledged("unknown-nid") is False

    async def test_get_acknowledgement_returns_correct_data(self, tmp_path):
        """get_acknowledgement() returns a dict with the correct fields."""
        reg = _registry(tmp_path)
        acked_at = _now()
        await reg.record_acknowledgement(
            "nid-3", "notify.persistent_notification", acked_at
        )

        ack = await reg.get_acknowledgement("nid-3")
        assert ack is not None
        assert ack["notification_id"] == "nid-3"
        assert ack["channel_id"] == "notify.persistent_notification"
        assert ack["acknowledged_at"] == acked_at.isoformat()

    async def test_get_acknowledgement_returns_none_for_unknown(self, tmp_path):
        """get_acknowledgement() returns None for a notification that was never acked."""
        reg = _registry(tmp_path)
        assert await reg.get_acknowledgement("missing") is None

    async def test_cleanup_removes_old_records(self, tmp_path):
        """cleanup_old() removes records whose acknowledged_at is before the cutoff."""
        reg = _registry(tmp_path)
        old_time = _now() - timedelta(days=10)
        await reg.record_acknowledgement("old-nid", "mobile_app", old_time)

        cutoff = _now() - timedelta(days=1)
        removed = await reg.cleanup_old(cutoff)

        assert removed == 1
        assert await reg.is_acknowledged("old-nid") is False

    async def test_cleanup_keeps_recent_records(self, tmp_path):
        """cleanup_old() retains records whose acknowledged_at is on or after the cutoff."""
        reg = _registry(tmp_path)
        recent_time = _now() - timedelta(hours=1)
        await reg.record_acknowledgement("recent-nid", "mobile_app", recent_time)

        cutoff = _now() - timedelta(days=1)
        removed = await reg.cleanup_old(cutoff)

        assert removed == 0
        assert await reg.is_acknowledged("recent-nid") is True

    async def test_persistence_survives_reload(self, tmp_path):
        """An acknowledgement saved by one instance is visible after loading a new one."""
        reg1 = _registry(tmp_path)
        acked_at = _now()
        await reg1.record_acknowledgement("nid-persist", "mobile_app", acked_at)

        # Second instance reads from the same file
        reg2 = _registry(tmp_path)
        assert await reg2.is_acknowledged("nid-persist") is True
        ack = await reg2.get_acknowledgement("nid-persist")
        assert ack is not None
        assert ack["acknowledged_at"] == acked_at.isoformat()

    async def test_disabled_mode_record_returns_false(self, tmp_path):
        """When enabled=False, record_acknowledgement() is a no-op returning False."""
        reg = _registry(tmp_path, enabled=False)
        result = await reg.record_acknowledgement("nid-x", "mobile_app", _now())
        assert result is False

    async def test_disabled_mode_is_acknowledged_returns_false(self, tmp_path):
        """When enabled=False, is_acknowledged() always returns False."""
        reg = _registry(tmp_path, enabled=False)
        assert await reg.is_acknowledged("nid-x") is False

    async def test_disabled_mode_get_acknowledgement_returns_none(self, tmp_path):
        """When enabled=False, get_acknowledgement() always returns None."""
        reg = _registry(tmp_path, enabled=False)
        assert await reg.get_acknowledgement("nid-x") is None

    async def test_disabled_mode_cleanup_returns_zero(self, tmp_path):
        """When enabled=False, cleanup_old() is a no-op returning 0."""
        reg = _registry(tmp_path, enabled=False)
        removed = await reg.cleanup_old(_now())
        assert removed == 0

    async def test_corrupted_file_handled_gracefully(self, tmp_path):
        """A corrupted JSON file is treated as empty — no exception is raised."""
        storage_path = tmp_path / "ans_acknowledgements.json"
        storage_path.write_text("not valid json", encoding="utf-8")

        reg = _registry(tmp_path)
        # Should not raise; falls back to empty state
        assert await reg.is_acknowledged("any") is False

    async def test_multiple_notifications_are_independent(self, tmp_path):
        """Multiple separate notifications can be acked independently."""
        reg = _registry(tmp_path)
        await reg.record_acknowledgement("nid-a", "mobile_app", _now())
        await reg.record_acknowledgement(
            "nid-b", "notify.persistent_notification", _now()
        )

        assert await reg.is_acknowledged("nid-a") is True
        assert await reg.is_acknowledged("nid-b") is True
        assert await reg.is_acknowledged("nid-c") is False

    async def test_cleanup_returns_count_of_removed(self, tmp_path):
        """cleanup_old() returns the exact number of records removed."""
        reg = _registry(tmp_path)
        old = _now() - timedelta(days=10)
        await reg.record_acknowledgement("nid-1", "mobile_app", old)
        await reg.record_acknowledgement("nid-2", "mobile_app", old)
        await reg.record_acknowledgement("nid-3", "mobile_app", _now())

        cutoff = _now() - timedelta(days=1)
        removed = await reg.cleanup_old(cutoff)
        assert removed == 2
