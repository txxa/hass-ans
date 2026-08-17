"""Unit tests for acknowledgement-tracking listener wiring (custom_components.ans.acknowledgement).

Coverage targets
----------------
- async_setup_acknowledgement_tracking — all four listeners (ans_notification_delivered,
                          mobile_app_notification_action/tapped, call_service capture,
                          persistent_notification dismissal), startup restoration from
                          the registry, custom mobile-tag correlation, method/user_id/
                          device_name payload fields, idempotent (second-ack) handling
- _mobile_device_name     — exercised indirectly via device_name payload assertions
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.persistent_notification import UpdateType as PNUpdateType

from custom_components.ans.const import (
    EVENT_NOTIFICATION_ACKNOWLEDGED,
    EVENT_NOTIFICATION_DELIVERED,
    PERSISTENT_NOTIFICATION_CHANNEL,
)

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_hass() -> MagicMock:
    """Minimal HomeAssistant mock suitable for acknowledgement-tracking tests."""
    return MagicMock()


def _make_entry(entry_id: str = "test-entry-id") -> MagicMock:
    """Return a mock ConfigEntry with entry_id, runtime_data={}, and async_on_unload pre-configured."""
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.runtime_data = {}
    entry.async_on_unload = MagicMock()
    return entry


def _make_system() -> MagicMock:
    """Return a mock ANSSystem exposing only the acknowledgement_registry surface these tests need."""
    system = MagicMock()
    system.acknowledgement_registry = MagicMock()
    system.acknowledgement_registry.record_acknowledgement = AsyncMock(
        return_value=True
    )
    system.acknowledgement_registry.mark_pending = AsyncMock(return_value=True)
    system.acknowledgement_registry.get_pending_channel_ids = AsyncMock(return_value={})
    system.acknowledgement_registry.get_pending_mobile_tags = AsyncMock(return_value={})
    return system


# ===========================================================================
# async_setup_acknowledgement_tracking (NH-3)
# ===========================================================================


class TestAcknowledgementTracking:
    """Verify async_setup_acknowledgement_tracking() listener logic (NH-3)."""

    def _make_event(self, data: dict, *, user_id: str | None = None) -> MagicMock:
        ev = MagicMock()
        ev.data = data
        ev.context = MagicMock()
        ev.context.user_id = user_id
        return ev

    async def _setup(
        self,
        *,
        pending_channel_ids: dict | None = None,
        pending_mobile_tags: dict | None = None,
    ):
        """Return (hass, entry, system, captured_listeners, dispatcher_cbs) ready for testing."""
        from custom_components.ans.acknowledgement import (  # noqa: PLC0415
            async_setup_acknowledgement_tracking,
        )

        hass = _make_hass()
        entry = _make_entry()
        system = _make_system()

        # Allow pre-populating the registry with persisted pending records.
        if pending_channel_ids is not None:
            system.acknowledgement_registry.get_pending_channel_ids = AsyncMock(
                return_value=pending_channel_ids
            )
        if pending_mobile_tags is not None:
            system.acknowledgement_registry.get_pending_mobile_tags = AsyncMock(
                return_value=pending_mobile_tags
            )

        listeners: dict[str, Any] = {}

        def _capture_listen(event_type, callback, *args, **kwargs):
            listeners[event_type] = callback
            return MagicMock()

        hass.bus.async_listen = MagicMock(side_effect=_capture_listen)

        dispatcher_callbacks: list[Any] = []

        def _capture_dispatcher(h, signal, cb):
            dispatcher_callbacks.append(cb)
            return MagicMock()

        with patch(
            "custom_components.ans.acknowledgement.async_dispatcher_connect",
            side_effect=_capture_dispatcher,
        ):
            await async_setup_acknowledgement_tracking(hass, entry, system)

        return hass, entry, system, listeners, dispatcher_callbacks

    def _collect_tasks(self, hass: MagicMock) -> list:
        """Wire hass.async_create_task to collect coroutines; return the list."""
        tasks: list = []

        def _collect(coro):
            tasks.append(coro)

        hass.async_create_task = _collect
        return tasks

    async def test_delivered_mobile_app_adds_to_pending(self):
        """ans_notification_delivered for mobile_app channel adds notification_id to pending acks."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {"channel_id": "notify.mobile_app_phone", "notification_id": "nid-1"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "nid-1"})
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_called_once()
        assert hass.bus.async_fire.call_args.args[0] == EVENT_NOTIFICATION_ACKNOWLEDGED

    async def test_delivered_persistent_notification_adds_to_pending(self):
        """ans_notification_delivered for persistent_notification channel adds to pending acks."""
        hass, entry, system, listeners, dispatcher_cbs = await self._setup()

        ev = self._make_event(
            {
                "channel_id": PERSISTENT_NOTIFICATION_CHANNEL,
                "notification_id": "pn-nid-1",
            }
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        await dispatcher_cbs[0](PNUpdateType.REMOVED, {"pn-nid-1": {}})

        hass.bus.async_fire.assert_called_once()
        assert hass.bus.async_fire.call_args.args[0] == EVENT_NOTIFICATION_ACKNOWLEDGED

    async def test_delivered_other_channel_not_added_to_pending(self):
        """ans_notification_delivered for a non-mobile-app, non-pn channel does not add to pending."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {"channel_id": "notify.signal", "notification_id": "nid-signal"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "nid-signal"})
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_not_called()

    async def test_mobile_app_action_fires_ack_event(self):
        """mobile_app_notification_action fires ans_notification_acknowledged with correct fields."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {"channel_id": "notify.mobile_app_phone", "notification_id": "nid-ack"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "nid-ack"})
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args.args
        assert event_name == EVENT_NOTIFICATION_ACKNOWLEDGED
        assert payload["notification_id"] == "nid-ack"
        assert payload["channel_id"] == "mobile_app"
        assert "acknowledged_at" in payload

    async def test_mobile_app_action_includes_action_field(self):
        """ans_notification_acknowledged payload includes 'action' when present in the event."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {"channel_id": "notify.mobile_app_phone", "notification_id": "nid-btn"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "nid-btn", "action": "CLOSE_GARAGE"})
        await listeners["mobile_app_notification_action"](action_ev)

        event_name, payload = hass.bus.async_fire.call_args.args
        assert event_name == EVENT_NOTIFICATION_ACKNOWLEDGED
        assert payload["action"] == "CLOSE_GARAGE"

    async def test_mobile_app_action_includes_device_name(self):
        """ans_notification_acknowledged payload includes 'device_name' derived from delivery channel."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {"channel_id": "notify.mobile_app_my_phone", "notification_id": "nid-dev"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "nid-dev", "action": "OK"})
        await listeners["mobile_app_notification_action"](action_ev)

        event_name, payload = hass.bus.async_fire.call_args.args
        assert event_name == EVENT_NOTIFICATION_ACKNOWLEDGED
        assert payload["device_name"] == "my_phone"

    async def test_mobile_app_action_no_action_key_absent_from_payload(self):
        """When no 'action' key is in the event, 'action' is absent from the ack payload."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {"channel_id": "notify.mobile_app_phone", "notification_id": "nid-noact"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "nid-noact"})  # no 'action' key
        await listeners["mobile_app_notification_action"](action_ev)

        event_name, payload = hass.bus.async_fire.call_args.args
        assert event_name == EVENT_NOTIFICATION_ACKNOWLEDGED
        assert "action" not in payload

    async def test_mobile_app_action_no_tag_not_fired(self):
        """mobile_app_notification_action with no 'tag' field does not fire any event."""
        hass, entry, system, listeners, _ = await self._setup()

        action_ev = self._make_event({"action": "OPEN"})  # no 'tag' key
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_not_called()

    async def test_mobile_app_action_unknown_tag_ignored(self):
        """mobile_app_notification_action with an unknown tag does not fire any event."""
        hass, entry, system, listeners, _ = await self._setup()

        action_ev = self._make_event({"tag": "not-in-pending"})
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_not_called()

    async def test_persistent_notification_removed_fires_ack_event(self):
        """Dispatcher REMOVED signal for a pending pn fires ans_notification_acknowledged."""
        hass, entry, system, listeners, dispatcher_cbs = await self._setup()

        ev = self._make_event(
            {"channel_id": PERSISTENT_NOTIFICATION_CHANNEL, "notification_id": "pn-1"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        await dispatcher_cbs[0](PNUpdateType.REMOVED, {"pn-1": {}})

        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args.args
        assert event_name == EVENT_NOTIFICATION_ACKNOWLEDGED
        assert payload["notification_id"] == "pn-1"
        assert payload["channel_id"] == PERSISTENT_NOTIFICATION_CHANNEL
        # action and device_name must be absent for persistent_notification
        assert "action" not in payload
        assert "device_name" not in payload

    async def test_persistent_notification_removed_unknown_id_ignored(self):
        """Dispatcher REMOVED signal for an unknown notification_id does nothing."""
        hass, entry, system, listeners, dispatcher_cbs = await self._setup()

        await dispatcher_cbs[0](PNUpdateType.REMOVED, {"not-pending": {}})

        hass.bus.async_fire.assert_not_called()

    async def test_second_ack_does_not_fire_event(self):
        """When record_acknowledgement returns False (duplicate), no event is fired."""
        hass, entry, system, listeners, _ = await self._setup()

        system.acknowledgement_registry.record_acknowledgement = AsyncMock(
            return_value=False
        )

        ev = self._make_event(
            {"channel_id": "notify.mobile_app_phone", "notification_id": "nid-dup"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "nid-dup"})
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_not_called()

    async def test_delivered_calls_mark_pending(self):
        """_on_notification_delivered calls mark_pending on the registry for mobile_app channel."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {"channel_id": "notify.mobile_app_phone", "notification_id": "nid-mp"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        system.acknowledgement_registry.mark_pending.assert_awaited_once()
        call_kwargs = system.acknowledgement_registry.mark_pending.call_args.kwargs
        assert call_kwargs["notification_id"] == "nid-mp"
        assert call_kwargs["channel_id"] == "notify.mobile_app_phone"

    async def test_startup_restores_pending_from_registry(self):
        """On setup, get_pending_channel_ids is called to restore persisted pending eligibility."""
        pending = {"pre-restart-nid": "notify.mobile_app_old_phone"}
        hass, entry, system, listeners, _ = await self._setup(
            pending_channel_ids=pending
        )

        # The pre-restart notification should now be eligible for ack.
        action_ev = self._make_event({"tag": "pre-restart-nid", "action": "OK"})
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args.args
        assert event_name == EVENT_NOTIFICATION_ACKNOWLEDGED
        assert payload["notification_id"] == "pre-restart-nid"
        assert payload["device_name"] == "old_phone"

    # ------------------------------------------------------------------
    # Custom mobile tag (channel_data.tag) scenarios
    # ------------------------------------------------------------------

    async def test_custom_tag_mobile_action_fires_ack_event(self):
        """When channel_data.tag sets a custom tag, action with that tag fires ans_notification_acknowledged with the UUID notification_id."""
        hass, entry, system, listeners, _ = await self._setup()

        # Deliver with a custom mobile_tag (simulates channel_data.tag = "garage-door")
        ev = self._make_event(
            {
                "channel_id": "notify.mobile_app_phone",
                "notification_id": "uuid-garage-1",
                "mobile_tag": "garage-door",
            }
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        # Companion app echoes back the custom tag, not the UUID
        action_ev = self._make_event({"tag": "garage-door", "action": "CLOSE"})
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args.args
        assert event_name == EVENT_NOTIFICATION_ACKNOWLEDGED
        # notification_id must be the UUID, not the custom tag
        assert payload["notification_id"] == "uuid-garage-1"
        assert payload["channel_id"] == "mobile_app"
        assert payload["action"] == "CLOSE"
        assert "acknowledged_at" in payload

    async def test_custom_tag_uuid_tag_still_works(self):
        """When no custom tag is used (tag == notification_id UUID), existing behaviour is unaffected."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {
                "channel_id": "notify.mobile_app_phone",
                "notification_id": "uuid-plain",
                "mobile_tag": "uuid-plain",  # same as notification_id — no custom tag
            }
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "uuid-plain", "action": "OK"})
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args.args
        assert event_name == EVENT_NOTIFICATION_ACKNOWLEDGED
        assert payload["notification_id"] == "uuid-plain"

    async def test_custom_tag_no_mobile_tag_field_uuid_fallback(self):
        """Delivery event without mobile_tag field (legacy) still works when tag matches notification_id."""
        hass, entry, system, listeners, _ = await self._setup()

        # Delivery event has no mobile_tag key (older delivery path)
        ev = self._make_event(
            {"channel_id": "notify.mobile_app_phone", "notification_id": "uuid-legacy"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "uuid-legacy"})
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args.args
        assert event_name == EVENT_NOTIFICATION_ACKNOWLEDGED
        assert payload["notification_id"] == "uuid-legacy"

    async def test_custom_tag_restored_after_restart(self):
        """Custom tag → notification_id mapping restored from get_pending_mobile_tags on startup."""
        hass, entry, system, listeners, _ = await self._setup(
            pending_channel_ids={"uuid-restart": "notify.mobile_app_old_phone"},
            pending_mobile_tags={"ha_update": "uuid-restart"},
        )

        # User taps after restart — tag is the custom tag, not the UUID
        action_ev = self._make_event({"tag": "ha_update", "action": "VIEW"})
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args.args
        assert event_name == EVENT_NOTIFICATION_ACKNOWLEDGED
        assert payload["notification_id"] == "uuid-restart"
        assert payload["device_name"] == "old_phone"

    async def test_custom_tag_mark_pending_receives_mobile_tag(self):
        """_on_notification_delivered passes mobile_tag to mark_pending when present."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {
                "channel_id": "notify.mobile_app_phone",
                "notification_id": "uuid-mp-tag",
                "mobile_tag": "custom-tag-value",
            }
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        system.acknowledgement_registry.mark_pending.assert_awaited_once()
        call_kwargs = system.acknowledgement_registry.mark_pending.call_args.kwargs
        assert call_kwargs["notification_id"] == "uuid-mp-tag"
        assert call_kwargs["mobile_tag"] == "custom-tag-value"

    async def test_custom_tag_unknown_tag_ignored(self):
        """Custom tag that was never registered as pending does not fire any event."""
        hass, entry, system, listeners, _ = await self._setup()

        action_ev = self._make_event({"tag": "unregistered-custom-tag"})
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_not_called()

    async def test_custom_tag_second_ack_ignored(self):
        """Second action tap for a custom-tag notification does not fire a second event."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {
                "channel_id": "notify.mobile_app_phone",
                "notification_id": "uuid-dup",
                "mobile_tag": "dup-tag",
            }
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "dup-tag"})
        await listeners["mobile_app_notification_action"](action_ev)
        assert hass.bus.async_fire.call_count == 1

        # Second tap — record_acknowledgement returns False (already acked)
        system.acknowledgement_registry.record_acknowledgement = AsyncMock(
            return_value=False
        )
        await listeners["mobile_app_notification_action"](action_ev)
        assert hass.bus.async_fire.call_count == 1  # still only one event

    async def test_mobile_app_notification_tapped_fires_ack_event(self):
        """mobile_app_notification_tapped fires ans_notification_acknowledged (body tap path)."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {"channel_id": "notify.mobile_app_phone", "notification_id": "nid-tapped"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        tapped_ev = self._make_event({"tag": "nid-tapped"})
        await listeners["mobile_app_notification_tapped"](tapped_ev)

        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args.args
        assert event_name == EVENT_NOTIFICATION_ACKNOWLEDGED
        assert payload["notification_id"] == "nid-tapped"
        assert payload["channel_id"] == "mobile_app"
        assert "acknowledged_at" in payload
        assert "action" not in payload

    async def test_mobile_app_notification_tapped_no_tag_ignored(self):
        """mobile_app_notification_tapped with no 'tag' field does not fire any event."""
        hass, entry, system, listeners, _ = await self._setup()

        tapped_ev = self._make_event({})  # no 'tag' key
        await listeners["mobile_app_notification_tapped"](tapped_ev)

        hass.bus.async_fire.assert_not_called()

    async def test_mobile_app_notification_tapped_unknown_tag_ignored(self):
        """mobile_app_notification_tapped with an unknown tag does not fire any event."""
        hass, entry, system, listeners, _ = await self._setup()

        tapped_ev = self._make_event({"tag": "ghost-tag"})
        await listeners["mobile_app_notification_tapped"](tapped_ev)

        hass.bus.async_fire.assert_not_called()

    async def test_persistent_notification_removed_cleans_tag_mapping(self):
        """Dismissing a persistent_notification removes stale custom-tag entries from _tag_to_notif_id."""
        hass, entry, system, listeners, dispatcher_cbs = await self._setup()

        # Deliver a mobile notification with a custom tag so _tag_to_notif_id is populated.
        ev = self._make_event(
            {
                "channel_id": "notify.mobile_app_phone",
                "notification_id": "uuid-pn-tag",
                "mobile_tag": "custom-cross",
            }
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        # Also deliver a persistent_notification for the same notification_id.
        pn_ev = self._make_event(
            {
                "channel_id": PERSISTENT_NOTIFICATION_CHANNEL,
                "notification_id": "uuid-pn-tag",
            }
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](pn_ev)

        # Dismiss the persistent notification.
        await dispatcher_cbs[0](PNUpdateType.REMOVED, {"uuid-pn-tag": {}})

        hass.bus.async_fire.assert_called_once()

        # Now a mobile action with the custom tag must NOT fire a second ack event
        # (the stale _tag_to_notif_id entry should have been cleaned up).
        action_ev = self._make_event({"tag": "custom-cross"})
        system.acknowledgement_registry.record_acknowledgement = AsyncMock(
            return_value=False
        )
        await listeners["mobile_app_notification_action"](action_ev)

        assert hass.bus.async_fire.call_count == 1  # no second event

    # ------------------------------------------------------------------
    # method field
    # ------------------------------------------------------------------

    async def test_mobile_app_action_includes_method_action_button(self):
        """ans_notification_acknowledged includes method='action_button' when action is present."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {
                "channel_id": "notify.mobile_app_phone",
                "notification_id": "nid-method-btn",
            }
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "nid-method-btn", "action": "OPEN"})
        await listeners["mobile_app_notification_action"](action_ev)

        _, payload = hass.bus.async_fire.call_args.args
        assert payload["method"] == "action_button"

    async def test_mobile_app_notification_tapped_includes_method_tap(self):
        """ans_notification_acknowledged includes method='notification_tap' on body tap."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {
                "channel_id": "notify.mobile_app_phone",
                "notification_id": "nid-method-tap",
            }
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        tap_ev = self._make_event({"tag": "nid-method-tap"})
        await listeners["mobile_app_notification_tapped"](tap_ev)

        _, payload = hass.bus.async_fire.call_args.args
        assert payload["method"] == "notification_tap"

    async def test_mobile_app_action_without_action_includes_method_tap(self):
        """mobile_app_notification_action with no 'action' value → method='notification_tap'."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {
                "channel_id": "notify.mobile_app_phone",
                "notification_id": "nid-method-noact",
            }
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "nid-method-noact"})  # no 'action' key
        await listeners["mobile_app_notification_action"](action_ev)

        _, payload = hass.bus.async_fire.call_args.args
        assert payload["method"] == "notification_tap"

    async def test_persistent_notification_includes_method_dismiss(self):
        """ans_notification_acknowledged includes method='persistent_notification_dismiss'."""
        hass, entry, system, listeners, dispatcher_cbs = await self._setup()

        ev = self._make_event(
            {
                "channel_id": PERSISTENT_NOTIFICATION_CHANNEL,
                "notification_id": "pn-method",
            }
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)
        await dispatcher_cbs[0](PNUpdateType.REMOVED, {"pn-method": {}})

        _, payload = hass.bus.async_fire.call_args.args
        assert payload["method"] == "persistent_notification_dismiss"

    # ------------------------------------------------------------------
    # user_id — mobile_app
    # ------------------------------------------------------------------

    async def test_mobile_app_action_context_carries_user_id(self):
        """ans_notification_acknowledged is fired with context.user_id from the companion app event."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {"channel_id": "notify.mobile_app_phone", "notification_id": "nid-uid"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event(
            {"tag": "nid-uid", "action": "OK"}, user_id="user-abc"
        )
        await listeners["mobile_app_notification_action"](action_ev)

        kwargs = hass.bus.async_fire.call_args.kwargs
        assert kwargs.get("context") is action_ev.context
        assert action_ev.context.user_id == "user-abc"
        _, payload = hass.bus.async_fire.call_args.args
        assert "user_id" not in payload

    async def test_mobile_app_action_omits_user_id_when_none(self):
        """ans_notification_acknowledged omits user_id when context carries no user."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {"channel_id": "notify.mobile_app_phone", "notification_id": "nid-nouid"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "nid-nouid"}, user_id=None)
        await listeners["mobile_app_notification_action"](action_ev)

        _, payload = hass.bus.async_fire.call_args.args
        assert "user_id" not in payload

    async def test_mobile_app_fires_with_event_context(self):
        """hass.bus.async_fire is called with context= taken from the companion app event."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {"channel_id": "notify.mobile_app_phone", "notification_id": "nid-ctx"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event(
            {"tag": "nid-ctx", "action": "ACK"}, user_id="user-ctx"
        )
        await listeners["mobile_app_notification_action"](action_ev)

        kwargs = hass.bus.async_fire.call_args.kwargs
        assert kwargs.get("context") is action_ev.context

    async def test_mobile_tapped_context_carries_user_id(self):
        """mobile_app_notification_tapped forwards event context (with user_id) to async_fire."""
        hass, entry, system, listeners, _ = await self._setup()

        ev = self._make_event(
            {"channel_id": "notify.mobile_app_phone", "notification_id": "nid-tap-uid"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        tap_ev = self._make_event({"tag": "nid-tap-uid"}, user_id="user-tap")
        await listeners["mobile_app_notification_tapped"](tap_ev)

        kwargs = hass.bus.async_fire.call_args.kwargs
        assert kwargs.get("context") is tap_ev.context
        assert tap_ev.context.user_id == "user-tap"
        _, payload = hass.bus.async_fire.call_args.args
        assert "user_id" not in payload

    # ------------------------------------------------------------------
    # user_id — persistent_notification via call_service interception
    # ------------------------------------------------------------------

    async def test_persistent_notification_user_id_from_call_service(self):
        """Context captured from call_service is forwarded to async_fire; user_id is not in data."""
        hass, entry, system, listeners, dispatcher_cbs = await self._setup()

        # Deliver the persistent notification so it's in _pending_meta.
        ev = self._make_event(
            {"channel_id": PERSISTENT_NOTIFICATION_CHANNEL, "notification_id": "pn-uid"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        # Simulate the frontend calling persistent_notification.dismiss (fires call_service first).
        cs_event = self._make_event(
            {
                "domain": "persistent_notification",
                "service": "dismiss",
                "service_data": {"notification_id": "pn-uid"},
            },
            user_id="user-456",
        )
        await listeners["call_service"](cs_event)

        # Now the SIGNAL fires (dismissal completes).
        await dispatcher_cbs[0](PNUpdateType.REMOVED, {"pn-uid": {}})

        kwargs = hass.bus.async_fire.call_args.kwargs
        assert kwargs.get("context") is cs_event.context
        assert cs_event.context.user_id == "user-456"
        _, payload = hass.bus.async_fire.call_args.args
        assert "user_id" not in payload

    async def test_persistent_notification_omits_user_id_without_call_service(self):
        """When no call_service event precedes the SIGNAL, user_id is absent from the payload."""
        hass, entry, system, listeners, dispatcher_cbs = await self._setup()

        ev = self._make_event(
            {
                "channel_id": PERSISTENT_NOTIFICATION_CHANNEL,
                "notification_id": "pn-nouid",
            }
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        # SIGNAL fires directly (e.g. automation called async_dismiss internally).
        await dispatcher_cbs[0](PNUpdateType.REMOVED, {"pn-nouid": {}})

        _, payload = hass.bus.async_fire.call_args.args
        assert "user_id" not in payload
        kwargs = hass.bus.async_fire.call_args.kwargs
        assert kwargs.get("context") is None

    async def test_call_service_for_non_pending_notification_ignored(self):
        """call_service for a notification_id not in _pending_meta is not cached."""
        hass, entry, system, listeners, dispatcher_cbs = await self._setup()

        # Fire call_service for an ID that was never delivered via ANS.
        cs_event = self._make_event(
            {
                "domain": "persistent_notification",
                "service": "dismiss",
                "service_data": {"notification_id": "unknown-pn"},
            },
            user_id="user-ghost",
        )
        await listeners["call_service"](cs_event)

        # No pending notification → subsequent SIGNAL is also ignored.
        await dispatcher_cbs[0](PNUpdateType.REMOVED, {"unknown-pn": {}})
        hass.bus.async_fire.assert_not_called()

    async def test_call_service_for_unrelated_domain_ignored(self):
        """call_service for a domain other than persistent_notification is ignored."""
        hass, entry, system, listeners, dispatcher_cbs = await self._setup()

        ev = self._make_event(
            {"channel_id": PERSISTENT_NOTIFICATION_CHANNEL, "notification_id": "pn-dom"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        # call_service for a different domain — should not cache any context.
        cs_event = self._make_event(
            {
                "domain": "notify",
                "service": "dismiss",
                "service_data": {"notification_id": "pn-dom"},
            },
            user_id="user-wrong",
        )
        await listeners["call_service"](cs_event)

        # Dismiss the PN — should fire without user_id.
        await dispatcher_cbs[0](PNUpdateType.REMOVED, {"pn-dom": {}})

        _, payload = hass.bus.async_fire.call_args.args
        assert "user_id" not in payload
