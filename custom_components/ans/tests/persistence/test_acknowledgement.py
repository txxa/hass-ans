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


class TestAcknowledgementRegistryStateMachine:
    """Verify the pending → acknowledged state-transition semantics."""

    async def test_mark_pending_creates_pending_record(self, tmp_path):
        """mark_pending() writes a record with status='pending'."""
        reg = _registry(tmp_path)
        result = await reg.mark_pending("nid-p", "notify.mobile_app_phone", _now())
        assert result is True
        # Pending record is NOT visible as acknowledged.
        assert await reg.is_acknowledged("nid-p") is False
        assert await reg.get_acknowledgement("nid-p") is None

    async def test_mark_pending_returns_false_for_duplicate(self, tmp_path):
        """A second mark_pending() call for the same notification_id returns False."""
        reg = _registry(tmp_path)
        await reg.mark_pending("nid-p2", "notify.mobile_app_phone", _now())
        result = await reg.mark_pending("nid-p2", "notify.mobile_app_phone", _now())
        assert result is False

    async def test_mark_pending_returns_false_when_already_acknowledged(self, tmp_path):
        """mark_pending() returns False when a notification is already acknowledged."""
        reg = _registry(tmp_path)
        await reg.record_acknowledgement("nid-a", "mobile_app", _now())
        result = await reg.mark_pending("nid-a", "notify.mobile_app_phone", _now())
        assert result is False

    async def test_get_pending_channel_ids_returns_only_pending(self, tmp_path):
        """get_pending_channel_ids() returns only pending records, not acknowledged ones."""
        reg = _registry(tmp_path)
        await reg.mark_pending("nid-pend", "notify.mobile_app_phone", _now())
        await reg.record_acknowledgement("nid-acked", "mobile_app", _now())

        pending = await reg.get_pending_channel_ids()
        assert "nid-pend" in pending
        assert pending["nid-pend"] == "notify.mobile_app_phone"
        assert "nid-acked" not in pending

    async def test_get_pending_channel_ids_empty_when_none(self, tmp_path):
        """get_pending_channel_ids() returns an empty dict when there are no pending records."""
        reg = _registry(tmp_path)
        assert await reg.get_pending_channel_ids() == {}

    async def test_record_acknowledgement_transitions_pending(self, tmp_path):
        """record_acknowledgement() transitions a pending record to acknowledged in-place."""
        reg = _registry(tmp_path)
        delivered = _now()
        await reg.mark_pending("nid-tr", "notify.mobile_app_phone", delivered)

        acked_at = _now()
        result = await reg.record_acknowledgement("nid-tr", "mobile_app", acked_at)
        assert result is True

        assert await reg.is_acknowledged("nid-tr") is True
        ack = await reg.get_acknowledgement("nid-tr")
        assert ack is not None
        assert ack["acknowledged_at"] == acked_at.isoformat()
        assert ack.get("status") == "acknowledged"

        # Must no longer appear in pending.
        pending = await reg.get_pending_channel_ids()
        assert "nid-tr" not in pending

    async def test_record_acknowledgement_idempotent_after_transition(self, tmp_path):
        """A second record_acknowledgement() after transition returns False."""
        reg = _registry(tmp_path)
        await reg.mark_pending("nid-idem", "notify.mobile_app_phone", _now())
        await reg.record_acknowledgement("nid-idem", "mobile_app", _now())

        result = await reg.record_acknowledgement("nid-idem", "mobile_app", _now())
        assert result is False

    async def test_record_acknowledgement_without_prior_pending(self, tmp_path):
        """record_acknowledgement() creates an acknowledged record even with no pending record."""
        reg = _registry(tmp_path)
        result = await reg.record_acknowledgement("nid-direct", "mobile_app", _now())
        assert result is True
        assert await reg.is_acknowledged("nid-direct") is True

    async def test_pending_record_survives_reload(self, tmp_path):
        """A pending record written by one instance is visible after loading a fresh one."""
        reg1 = _registry(tmp_path)
        delivered = _now()
        await reg1.mark_pending("nid-reload", "notify.mobile_app_phone", delivered)

        reg2 = _registry(tmp_path)
        pending = await reg2.get_pending_channel_ids()
        assert "nid-reload" in pending
        assert pending["nid-reload"] == "notify.mobile_app_phone"

    async def test_cleanup_pending_uses_delivered_at(self, tmp_path):
        """cleanup_old() removes stale pending records using their delivered_at timestamp."""
        reg = _registry(tmp_path)
        old_delivered = _now() - timedelta(days=10)
        await reg.mark_pending("nid-stale", "notify.mobile_app_phone", old_delivered)

        cutoff = _now() - timedelta(days=1)
        removed = await reg.cleanup_old(cutoff)
        assert removed == 1

        pending = await reg.get_pending_channel_ids()
        assert "nid-stale" not in pending

    async def test_disabled_mode_mark_pending_returns_false(self, tmp_path):
        """When enabled=False, mark_pending() is a no-op returning False."""
        reg = _registry(tmp_path, enabled=False)
        result = await reg.mark_pending("nid-x", "notify.mobile_app_phone", _now())
        assert result is False

    async def test_disabled_mode_get_pending_channel_ids_returns_empty(self, tmp_path):
        """When enabled=False, get_pending_channel_ids() always returns {}."""
        reg = _registry(tmp_path, enabled=False)
        assert await reg.get_pending_channel_ids() == {}

    async def test_atomic_write_produces_valid_json(self, tmp_path):
        """After mark_pending and record_acknowledgement, the storage file is valid JSON."""
        import json  # noqa: PLC0415

        reg = _registry(tmp_path)
        await reg.mark_pending("nid-json", "notify.mobile_app_phone", _now())
        await reg.record_acknowledgement("nid-json", "mobile_app", _now())

        storage_file = tmp_path / "ans_acknowledgements.json"
        assert storage_file.exists()
        data = json.loads(storage_file.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        assert len(data) == 1
        record = data[0]
        assert record["notification_id"] == "nid-json"
        assert record["status"] == "acknowledged"
        assert "acknowledged_at" in record


# ---------------------------------------------------------------------------
# mobile_tag support (custom channel_data.tag correlation)
# ---------------------------------------------------------------------------


class TestMobileTagPersistence:
    """Tests for the mobile_tag field added to support custom channel_data.tag values."""

    async def test_mark_pending_with_custom_mobile_tag_stores_field(self, tmp_path):
        """mark_pending() stores mobile_tag in the record when it differs from notification_id."""
        import json  # noqa: PLC0415

        reg = _registry(tmp_path)
        await reg.mark_pending(
            "uuid-1",
            "notify.mobile_app_phone",
            _now(),
            mobile_tag="garage-door",
        )

        storage_file = tmp_path / "ans_acknowledgements.json"
        data = json.loads(storage_file.read_text(encoding="utf-8"))
        assert data[0]["mobile_tag"] == "garage-door"

    async def test_mark_pending_without_mobile_tag_omits_field(self, tmp_path):
        """mark_pending() without mobile_tag does not add the field to the record."""
        import json  # noqa: PLC0415

        reg = _registry(tmp_path)
        await reg.mark_pending("uuid-2", "notify.mobile_app_phone", _now())

        storage_file = tmp_path / "ans_acknowledgements.json"
        data = json.loads(storage_file.read_text(encoding="utf-8"))
        assert "mobile_tag" not in data[0]

    async def test_mark_pending_mobile_tag_equals_notification_id_omits_field(
        self, tmp_path
    ):
        """mark_pending() omits mobile_tag when it equals notification_id (no custom tag)."""
        import json  # noqa: PLC0415

        reg = _registry(tmp_path)
        await reg.mark_pending(
            "uuid-3",
            "notify.mobile_app_phone",
            _now(),
            mobile_tag="uuid-3",  # same as notification_id
        )

        storage_file = tmp_path / "ans_acknowledgements.json"
        data = json.loads(storage_file.read_text(encoding="utf-8"))
        assert "mobile_tag" not in data[0]

    async def test_get_pending_mobile_tags_returns_mapping(self, tmp_path):
        """get_pending_mobile_tags() returns {mobile_tag: notification_id} for pending records."""
        reg = _registry(tmp_path)
        await reg.mark_pending(
            "uuid-4",
            "notify.mobile_app_phone",
            _now(),
            mobile_tag="ha-update",
        )

        result = await reg.get_pending_mobile_tags()
        assert result == {"ha-update": "uuid-4"}

    async def test_get_pending_mobile_tags_excludes_acknowledged(self, tmp_path):
        """get_pending_mobile_tags() does not include records that have been acknowledged."""
        reg = _registry(tmp_path)
        await reg.mark_pending(
            "uuid-5",
            "notify.mobile_app_phone",
            _now(),
            mobile_tag="update-tag",
        )
        await reg.record_acknowledgement("uuid-5", "mobile_app", _now())

        result = await reg.get_pending_mobile_tags()
        assert result == {}

    async def test_get_pending_mobile_tags_excludes_records_without_mobile_tag(
        self, tmp_path
    ):
        """get_pending_mobile_tags() excludes pending records that have no mobile_tag."""
        reg = _registry(tmp_path)
        await reg.mark_pending("uuid-6", "notify.mobile_app_phone", _now())

        result = await reg.get_pending_mobile_tags()
        assert result == {}

    async def test_get_pending_mobile_tags_empty_when_disabled(self, tmp_path):
        """get_pending_mobile_tags() returns {} when registry is disabled."""
        reg = _registry(tmp_path, enabled=False)
        result = await reg.get_pending_mobile_tags()
        assert result == {}

    async def test_mobile_tag_mapping_survives_reload(self, tmp_path):
        """Custom tag → notification_id mapping is persisted and visible after registry reload."""
        reg1 = _registry(tmp_path)
        await reg1.mark_pending(
            "uuid-7",
            "notify.mobile_app_phone",
            _now(),
            mobile_tag="persistent-tag",
        )

        reg2 = _registry(tmp_path)
        result = await reg2.get_pending_mobile_tags()
        assert result == {"persistent-tag": "uuid-7"}

    async def test_mark_pending_backfills_mobile_tag_on_existing_pending_record(
        self, tmp_path
    ):
        """When a channel (e.g. persistent_notification) already created a pending record without a mobile_tag, a subsequent mark_pending call from mobile_app that provides a custom tag backfills it onto the existing record so the mapping survives restarts."""
        import json  # noqa: PLC0415

        reg = _registry(tmp_path)
        # First delivery: persistent_notification — no mobile_tag
        result1 = await reg.mark_pending(
            "uuid-backfill",
            "notify.persistent_notification",
            _now(),
        )
        assert result1 is True

        # Second delivery: mobile_app with a custom tag
        result2 = await reg.mark_pending(
            "uuid-backfill",
            "notify.mobile_app_phone",
            _now(),
            mobile_tag="motion-front-door",
        )
        assert result2 is False  # no new record created

        # The existing record should now carry mobile_tag
        storage_file = tmp_path / "ans_acknowledgements.json"
        data = json.loads(storage_file.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["mobile_tag"] == "motion-front-door"
        assert data[0]["channel_id"] == "notify.persistent_notification"

    async def test_mark_pending_does_not_overwrite_existing_mobile_tag(self, tmp_path):
        """mark_pending() does not overwrite a mobile_tag that is already set on the record."""
        import json  # noqa: PLC0415

        reg = _registry(tmp_path)
        await reg.mark_pending(
            "uuid-nowipe",
            "notify.mobile_app_phone",
            _now(),
            mobile_tag="original-tag",
        )
        # Second call with a different tag — should NOT overwrite
        await reg.mark_pending(
            "uuid-nowipe",
            "notify.mobile_app_phone",
            _now(),
            mobile_tag="different-tag",
        )

        storage_file = tmp_path / "ans_acknowledgements.json"
        data = json.loads(storage_file.read_text(encoding="utf-8"))
        assert data[0]["mobile_tag"] == "original-tag"

    async def test_mark_pending_backfill_visible_via_get_pending_mobile_tags(
        self, tmp_path
    ):
        """After backfill, get_pending_mobile_tags() returns the tag → notification_id mapping."""
        reg = _registry(tmp_path)
        await reg.mark_pending("uuid-tags", "notify.persistent_notification", _now())
        await reg.mark_pending(
            "uuid-tags",
            "notify.mobile_app_phone",
            _now(),
            mobile_tag="door-alert",
        )

        result = await reg.get_pending_mobile_tags()
        assert result == {"door-alert": "uuid-tags"}

    async def test_mark_pending_no_backfill_when_already_acknowledged(self, tmp_path):
        """mark_pending() does not backfill mobile_tag when the record is already acknowledged."""
        import json  # noqa: PLC0415

        reg = _registry(tmp_path)
        await reg.record_acknowledgement(
            "uuid-acked", "notify.persistent_notification", _now()
        )

        # Call mark_pending with a mobile_tag — the record is acknowledged, not pending
        result = await reg.mark_pending(
            "uuid-acked",
            "notify.mobile_app_phone",
            _now(),
            mobile_tag="some-tag",
        )
        assert result is False

        # The acknowledged record must NOT gain a mobile_tag
        storage_file = tmp_path / "ans_acknowledgements.json"
        data = json.loads(storage_file.read_text(encoding="utf-8"))
        assert "mobile_tag" not in data[0]
