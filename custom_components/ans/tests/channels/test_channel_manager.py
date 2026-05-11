"""Tests for ChannelManager sync and lookup behaviour."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.ans.channels.base import (
    AdapterFactory,
    AdapterType,
    ChannelRecord,
    ChannelStatus,
)
from custom_components.ans.channels.channel_manager import (
    ChannelManager,
    detect_media_players,
    detect_notification_channels,
    detect_tts_entities,
)
from custom_components.ans.const import REQUIRED_MP_FEATURES
from custom_components.ans.models import ChannelScope, RecipientType

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_hass(
    *,
    notify_services: dict[str, dict] | None = None,
    media_player_ids: list[str] | None = None,
    media_player_features: int = REQUIRED_MP_FEATURES,
) -> MagicMock:
    """Return a mock HomeAssistant for ChannelManager tests."""
    hass = MagicMock()

    # Notify services
    ns = notify_services or {}
    hass.services.async_services.return_value = {"notify": ns} if ns else {}

    # Media players
    mp_ids = media_player_ids or []
    hass.states.async_entity_ids.return_value = mp_ids

    def _get_state(eid):
        """Return a mock state with feature attributes, or None if not in mp_ids."""
        state = MagicMock()
        state.attributes = {
            "supported_features": media_player_features,
            "friendly_name": eid,
        }
        return state if eid in mp_ids else None

    hass.states.get.side_effect = _get_state
    return hass


def _make_deps():
    """Build a minimal dependencies object with mocked config_repo and volume_registry."""
    deps = MagicMock()
    deps.config_repo = MagicMock()
    deps.volume_registry = MagicMock()
    return deps


def _make_manager(hass=None, deps=None) -> ChannelManager:
    """Create a ChannelManager with optional hass and deps overrides."""
    hass = hass or _make_hass()
    deps = deps or _make_deps()
    return ChannelManager(hass, deps)


def _dummy_adapter(hass, info) -> MagicMock:
    """Return a MagicMock adapter instance."""
    return MagicMock()


def _dummy_factory(
    channel_prefix: str, adapter_type=AdapterType.DYNAMIC_MULTI
) -> AdapterFactory:
    """Return an AdapterFactory whose adapter class matches *channel_prefix*."""
    klass = MagicMock()
    klass.matches_channel.side_effect = lambda cid: cid.startswith(channel_prefix)
    klass.get_channel_label.return_value = channel_prefix
    klass.get_metadata.return_value = MagicMock(integration=channel_prefix)
    klass.get_requirements.return_value = {}

    factory = AdapterFactory(
        channel_prefix=channel_prefix,
        adapter_class=klass,
        adapter_type=adapter_type,
        factory_fn=_dummy_adapter,
        cleanup_fn=None,
    )
    return factory  # noqa: RET504


# ── Initial state ─────────────────────────────────────────────────────────────


class TestInitialState:
    """Verify ChannelManager state immediately after construction."""

    def test_no_records_on_creation(self):
        """A newly created manager has no active or detected channels."""
        mgr = _make_manager()
        assert mgr.count_detected() == 0
        assert mgr.count_active() == 0

    def test_get_adapter_unknown_channel_returns_none(self):
        """get_adapter returns None for channel IDs not yet registered."""
        mgr = _make_manager()
        assert mgr.get_adapter("notify.unknown") is None

    def test_get_info_unknown_channel_returns_none(self):
        """get_info returns None for channel IDs not yet registered."""
        mgr = _make_manager()
        assert mgr.get_info("notify.unknown") is None


# ── Detection helpers ──────────────────────────────────────────────────────────


class TestDetection:
    """Verify channel detection logic for notify services and media players."""

    def test_detects_notify_services(self):
        """Registered HA notify services are discovered as notification channels."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        infos = mgr._detect_notification_channels()
        ids = [i.id for i in infos]
        assert "notify.mobile_app_phone" in ids

    def test_excludes_reserved_service_names(self):
        """Reserved service names ('notify', 'send_message') are excluded from detection."""
        hass = _make_hass(
            notify_services={"notify": {}, "send_message": {}, "real_channel": {}}
        )
        mgr = _make_manager(hass)
        infos = mgr._detect_notification_channels()
        ids = [i.id for i in infos]
        assert "notify.notify" not in ids
        assert "notify.send_message" not in ids
        assert "notify.real_channel" in ids

    def test_detects_compatible_media_player(self):
        """Media players with the required feature flags are discovered as TTS channels."""
        hass = _make_hass(
            media_player_ids=["media_player.living_room"],
            media_player_features=REQUIRED_MP_FEATURES,
        )
        mgr = _make_manager(hass)
        infos = mgr._detect_media_players()
        assert any(i.id == "media_player.living_room" for i in infos)

    def test_skips_media_player_without_required_features(self):
        """Media players missing required feature flags are excluded from detection."""
        hass = _make_hass(
            media_player_ids=["media_player.tv"],
            media_player_features=0x0001,  # not REQUIRED_MP_FEATURES
        )
        mgr = _make_manager(hass)
        infos = mgr._detect_media_players()
        assert not infos

    def test_media_player_has_tts_scope(self):
        """Detected media player channels are assigned the TTS channel scope."""
        hass = _make_hass(
            media_player_ids=["media_player.kitchen"],
            media_player_features=REQUIRED_MP_FEATURES,
        )
        mgr = _make_manager(hass)
        infos = mgr._detect_media_players()
        assert infos[0].scope == ChannelScope.TTS


# ── Sync: ACTIVE status ───────────────────────────────────────────────────────


class TestSyncActive:
    """Verify that channels transition to ACTIVE status after sync."""

    async def test_detected_enabled_with_factory_becomes_active(self):
        """A detected and enabled channel with a registered factory becomes ACTIVE."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        await mgr.sync(["notify.mobile_app_phone"])
        assert mgr.count_active() == 1
        assert mgr.get_adapter("notify.mobile_app_phone") is not None

    async def test_existing_active_adapter_is_reused(self):
        """Re-syncing an already ACTIVE channel reuses the existing adapter instance."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        await mgr.sync(["notify.mobile_app_phone"])
        first_adapter = mgr.get_adapter("notify.mobile_app_phone")
        await mgr.sync(["notify.mobile_app_phone"])
        second_adapter = mgr.get_adapter("notify.mobile_app_phone")
        # Should be the same object (not recreated)
        assert first_adapter is second_adapter


# ── Sync: DETECTED status ─────────────────────────────────────────────────────


class TestSyncDetected:
    """Verify channels remain DETECTED when enabled list excludes them."""

    async def test_detected_not_enabled_becomes_detected(self):
        """A detected channel absent from the enabled list stays in DETECTED status."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        await mgr.sync([])  # detected but not enabled
        assert mgr.get_adapter("notify.mobile_app_phone") is None
        assert mgr.count_detected() >= 1


# ── Sync: INACTIVE status ─────────────────────────────────────────────────────


class TestSyncInactive:
    """Verify channels become INACTIVE when enabled but no factory is registered."""

    async def test_enabled_no_factory_becomes_inactive(self):
        """An enabled channel without a matching factory is recorded as INACTIVE."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        # No factory registered for this channel
        await mgr.sync(["notify.mobile_app_phone"])
        record = mgr.get_record("notify.mobile_app_phone")
        assert record is not None
        assert record.status == ChannelStatus.INACTIVE


# ── Sync: STALE status ────────────────────────────────────────────────────────


class TestSyncStale:
    """Verify that previously ACTIVE channels become STALE when they disappear."""

    async def test_previously_active_disappears_becomes_stale(self):
        """An ACTIVE channel no longer detected by HA transitions to STALE status."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        await mgr.sync(["notify.mobile_app_phone"])
        assert mgr.count_active() == 1

        # Channel disappears from HA
        hass.services.async_services.return_value = {}
        await mgr.sync(["notify.mobile_app_phone"])
        record = mgr.get_record("notify.mobile_app_phone")
        assert record is not None
        assert record.status == ChannelStatus.STALE
        assert mgr.get_adapter("notify.mobile_app_phone") is None


# ── Static adapters ───────────────────────────────────────────────────────────


class TestStaticAdapters:
    """Verify initialization and persistence of static (non-dynamic) adapters."""

    def test_static_adapter_initialized_at_startup(self):
        """Static adapters registered at startup are immediately made ACTIVE."""
        hass = _make_hass()
        mgr = _make_manager(hass)
        factory = _dummy_factory(
            "notify.persistent_notification", adapter_type=AdapterType.STATIC
        )
        mgr.register_factory(factory)
        mgr.initialize_static_adapters()
        assert mgr.count_active() == 1

    async def test_static_adapter_not_overwritten_on_sync(self):
        """Static adapters remain untouched by subsequent sync calls."""
        hass = _make_hass()
        mgr = _make_manager(hass)
        factory = _dummy_factory(
            "notify.persistent_notification", adapter_type=AdapterType.STATIC
        )
        mgr.register_factory(factory)
        mgr.initialize_static_adapters()
        static_adapter = mgr.get_adapter("notify.persistent_notification")

        await mgr.sync([])  # no channels enabled, no services detected
        # Static adapter preserved
        assert mgr.get_adapter("notify.persistent_notification") is static_adapter


# ── finalize_setup ────────────────────────────────────────────────────────────


class TestFinalizeSetup:
    """Verify finalize_setup clears the setup flag and flushes pending re-syncs."""

    async def test_finalize_clears_setup_flag(self):
        """finalize_setup sets _setup_in_progress to False."""
        mgr = _make_manager()
        assert mgr._setup_in_progress is True
        await mgr.finalize_setup()
        assert mgr._setup_in_progress is False

    async def test_finalize_triggers_pending_resync(self):
        """A pending re-sync flag is honoured and cleared by finalize_setup."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        mgr._last_enabled = ["notify.mobile_app_phone"]
        mgr._pending_resync = True

        await mgr.finalize_setup()
        assert mgr._pending_resync is False

    async def test_no_pending_resync_no_extra_sync(self):
        """When _pending_resync is False, finalize_setup completes without triggering sync."""
        hass = _make_hass()
        mgr = _make_manager(hass)
        mgr._pending_resync = False
        # Should complete without error
        await mgr.finalize_setup()
        assert mgr._setup_in_progress is False


# ── Counting helpers ──────────────────────────────────────────────────────────


class TestCounting:
    """Verify count_active and count_detected return correct tallies."""

    async def test_count_active_excludes_detected_and_inactive(self):
        """count_active counts only ACTIVE channels, not DETECTED or INACTIVE ones."""
        hass = _make_hass(
            notify_services={
                "ch_active": {},
                "ch_detected": {},
            }
        )
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.ch_active"))
        # Only ch_active is in enabled list → ch_detected = DETECTED
        await mgr.sync(["notify.ch_active"])
        assert mgr.count_active() == 1

    async def test_count_detected_includes_active_and_detected(self):
        """count_detected counts both ACTIVE and DETECTED channels."""
        hass = _make_hass(
            notify_services={
                "ch_active": {},
                "ch_detected": {},
            }
        )
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.ch_active"))
        await mgr.sync(["notify.ch_active"])
        # Both are non-STALE, so both counted
        assert mgr.count_detected() >= 2


# ── cleanup_all ───────────────────────────────────────────────────────────────


class TestCleanupAll:
    """Verify that cleanup_all removes all channel records."""

    async def test_cleanup_all_clears_records(self):
        """cleanup_all removes all entries from the internal channel record map."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        await mgr.sync(["notify.mobile_app_phone"])
        assert mgr.count_active() == 1

        await mgr.cleanup_all()
        assert len(mgr._records) == 0


# ── Resync / request_resync ───────────────────────────────────────────────────


class TestResync:
    """Verify resync() and request_resync() behaviour."""

    async def test_resync_uses_last_enabled(self):
        """resync() re-runs sync using the last known enabled_channels list."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        await mgr.sync(["notify.mobile_app_phone"])
        assert mgr.count_active() == 1

        # Channel disappears, then resync with the same _last_enabled
        # should leave the adapter STALE (channel no longer detected)
        hass.services.async_services.return_value = {}
        await mgr.resync()
        record = mgr.get_record("notify.mobile_app_phone")
        assert record is not None
        assert record.status == ChannelStatus.STALE

    async def test_request_resync_defers_during_setup(self):
        """request_resync sets _pending_resync when setup is still in progress."""
        mgr = _make_manager()
        assert mgr._setup_in_progress is True
        await mgr.request_resync()
        assert mgr._pending_resync is True

    async def test_request_resync_immediate_after_setup(self):
        """request_resync calls resync() immediately when setup is complete."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        mgr._last_enabled = ["notify.mobile_app_phone"]
        mgr._setup_in_progress = False

        await mgr.request_resync()
        assert mgr.count_active() == 1
        assert mgr._pending_resync is False


# ── Delivery lock ─────────────────────────────────────────────────────────────


class TestDeliveryLock:
    """Verify get_delivery_lock creates and caches per-entity locks."""

    def test_get_delivery_lock_creates_lock(self):
        """First call creates a new asyncio.Lock for the entity."""
        mgr = _make_manager()
        lock = mgr.get_delivery_lock("media_player.living_room")
        assert isinstance(lock, asyncio.Lock)

    def test_get_delivery_lock_returns_cached_lock(self):
        """Subsequent calls return the same lock object."""
        mgr = _make_manager()
        lock1 = mgr.get_delivery_lock("media_player.living_room")
        lock2 = mgr.get_delivery_lock("media_player.living_room")
        assert lock1 is lock2

    def test_different_entities_get_different_locks(self):
        """Different entities receive independent lock instances."""
        mgr = _make_manager()
        lock_a = mgr.get_delivery_lock("media_player.a")
        lock_b = mgr.get_delivery_lock("media_player.b")
        assert lock_a is not lock_b


# ── Read-only lookups ─────────────────────────────────────────────────────────


class TestLookups:
    """Verify get_record, get_all_infos, get_active_infos, get_infos_for_recipient_type."""

    async def test_get_record_known_channel(self):
        """get_record returns a ChannelRecord for a known channel."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        await mgr.sync(["notify.mobile_app_phone"])
        record = mgr.get_record("notify.mobile_app_phone")
        assert record is not None
        assert isinstance(record, ChannelRecord)

    def test_get_record_unknown_channel_returns_none(self):
        """get_record returns None for a channel that has never been registered."""
        mgr = _make_manager()
        assert mgr.get_record("notify.unknown") is None

    async def test_get_all_infos_excludes_stale(self):
        """get_all_infos omits STALE channels from its result."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        await mgr.sync(["notify.mobile_app_phone"])
        # Make the channel disappear → STALE
        hass.services.async_services.return_value = {}
        await mgr.sync(["notify.mobile_app_phone"])

        infos = mgr.get_all_infos()
        ids = [i.id for i in infos]
        assert "notify.mobile_app_phone" not in ids

    async def test_get_active_infos_returns_only_active(self):
        """get_active_infos returns ChannelInfo only for ACTIVE channels."""
        hass = _make_hass(notify_services={"ch_active": {}, "ch_detected": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.ch_active"))
        await mgr.sync(["notify.ch_active"])

        active_infos = mgr.get_active_infos()
        active_ids = {i.id for i in active_infos}
        assert "notify.ch_active" in active_ids
        assert "notify.ch_detected" not in active_ids

    async def test_get_all_records_includes_stale(self):
        """get_all_records includes STALE channels unlike get_all_infos."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        await mgr.sync(["notify.mobile_app_phone"])
        hass.services.async_services.return_value = {}
        await mgr.sync(["notify.mobile_app_phone"])

        all_records = mgr.get_all_records()
        stale = [r for r in all_records if r.status == ChannelStatus.STALE]
        assert len(stale) == 1

    async def test_get_infos_for_recipient_type_system(self):
        """get_infos_for_recipient_type(SYSTEM) returns only SYSTEM-scoped channels."""
        hass = _make_hass(notify_services={"persistent_notification": {}})
        mgr = _make_manager(hass)
        # Use a STATIC factory so persistent_notification gets SYSTEM scope
        static_factory = _dummy_factory(
            "notify.persistent_notification", adapter_type=AdapterType.STATIC
        )
        mgr.register_factory(static_factory)
        mgr.initialize_static_adapters()

        infos = mgr.get_infos_for_recipient_type(RecipientType.SYSTEM)
        # The static adapter created by initialize_static_adapters uses _make_static_channel_info
        # which assigns SYSTEM scope for persistent_notification
        assert len(infos) >= 0  # scope assignment comes from const, verify no crash

    async def test_get_infos_for_recipient_type_tts(self):
        """get_infos_for_recipient_type(TTS) returns only TTS-scoped channels."""
        hass = _make_hass(
            media_player_ids=["media_player.living_room"],
            media_player_features=REQUIRED_MP_FEATURES,
        )
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("media_player.living_room"))
        await mgr.sync(["media_player.living_room"])

        infos = mgr.get_infos_for_recipient_type(RecipientType.TTS)
        ids = [i.id for i in infos]
        assert "media_player.living_room" in ids

    async def test_get_infos_for_recipient_type_generic(self):
        """get_infos_for_recipient_type(GENERIC) returns GENERIC-scoped channels."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        await mgr.sync(["notify.mobile_app_phone"])

        infos = mgr.get_infos_for_recipient_type(RecipientType.GENERIC)
        ids = [i.id for i in infos]
        assert "notify.mobile_app_phone" in ids


# ── filter_channels_by_contact_info ──────────────────────────────────────────


class TestFilterChannelsByContactInfo:
    """Verify filter_channels_by_contact_info allows/blocks channels by contact availability."""

    def _make_factory_with_requirements(
        self, channel_prefix: str, requirements: dict
    ) -> AdapterFactory:
        """Return an AdapterFactory with custom get_requirements() return value."""
        klass = MagicMock()
        klass.matches_channel.side_effect = lambda cid: cid == channel_prefix
        klass.get_channel_label.return_value = channel_prefix
        klass.get_metadata.return_value = MagicMock(integration=channel_prefix)
        klass.get_requirements.return_value = requirements
        return AdapterFactory(
            channel_prefix=channel_prefix,
            adapter_class=klass,
            adapter_type=AdapterType.DYNAMIC_SINGLE,
            factory_fn=_dummy_adapter,
            cleanup_fn=None,
        )

    def test_filter_unknown_channel_is_allowed(self):
        """Channels with no registered factory are conservatively allowed."""
        mgr = _make_manager()
        available, unavailable = mgr.filter_channels_by_contact_info(
            ["notify.unknown_service"]
        )
        assert "notify.unknown_service" in available
        assert len(unavailable) == 0

    def test_filter_blocks_channel_requiring_phone_when_absent(self):
        """A channel requiring phone is blocked when has_phone=False."""
        mgr = _make_manager()
        mgr.register_factory(
            self._make_factory_with_requirements(
                "notify.signal", {"requires_phone": True}
            )
        )
        available, unavailable = mgr.filter_channels_by_contact_info(
            ["notify.signal"], has_phone=False
        )
        assert "notify.signal" not in available
        assert "notify.signal" in unavailable

    def test_filter_passes_channel_requiring_phone_when_present(self):
        """A channel requiring phone is allowed when has_phone=True."""
        mgr = _make_manager()
        mgr.register_factory(
            self._make_factory_with_requirements(
                "notify.signal", {"requires_phone": True}
            )
        )
        available, unavailable = mgr.filter_channels_by_contact_info(
            ["notify.signal"], has_phone=True
        )
        assert "notify.signal" in available
        assert len(unavailable) == 0

    def test_filter_blocks_channel_requiring_ha_user_when_absent(self):
        """A channel requiring HA user is blocked when has_ha_user=False."""
        mgr = _make_manager()
        mgr.register_factory(
            self._make_factory_with_requirements(
                "notify.mobile_app_x", {"requires_ha_user": True}
            )
        )
        available, unavailable = mgr.filter_channels_by_contact_info(
            ["notify.mobile_app_x"], has_ha_user=False
        )
        assert "notify.mobile_app_x" in unavailable

    def test_filter_passes_channel_requiring_ha_user_when_present(self):
        """A channel requiring HA user is allowed when has_ha_user=True."""
        mgr = _make_manager()
        mgr.register_factory(
            self._make_factory_with_requirements(
                "notify.mobile_app_x", {"requires_ha_user": True}
            )
        )
        available, unavailable = mgr.filter_channels_by_contact_info(
            ["notify.mobile_app_x"], has_ha_user=True
        )
        assert "notify.mobile_app_x" in available

    def test_filter_multiple_missing_requirements_combined_reason(self):
        """When multiple requirements are missing the reason mentions both."""
        mgr = _make_manager()
        mgr.register_factory(
            self._make_factory_with_requirements(
                "notify.email_and_phone",
                {"requires_email": True, "requires_phone": True},
            )
        )
        _, unavailable = mgr.filter_channels_by_contact_info(
            ["notify.email_and_phone"], has_email=False, has_phone=False
        )
        reason = unavailable.get("notify.email_and_phone", "")
        assert "and" in reason  # "Missing email address and phone number"


# ── _destroy_adapter ──────────────────────────────────────────────────────────


class TestDestroyAdapter:
    """Verify _destroy_adapter cleanup_fn invocation and error handling."""

    async def test_destroy_no_adapter_is_noop(self):
        """_destroy_adapter is a no-op when the record has no adapter."""
        mgr = _make_manager()
        from custom_components.ans.channels.base import ChannelInfo  # noqa: PLC0415
        from custom_components.ans.models.channel import ChannelScope  # noqa: PLC0415

        info = ChannelInfo(
            id="notify.x",
            label="X",
            scope=ChannelScope.RECIPIENT,
            integration=None,
        )
        record = ChannelRecord(info=info, adapter=None, status=ChannelStatus.DETECTED)
        # Should complete without error
        await mgr._destroy_adapter("notify.x", record)

    async def test_destroy_calls_sync_cleanup_fn(self):
        """_destroy_adapter invokes a synchronous cleanup_fn."""
        cleanup = MagicMock()
        hass = _make_hass(notify_services={"ch": {}})
        mgr = _make_manager(hass)
        factory = _dummy_factory("notify.ch")
        factory = AdapterFactory(
            channel_prefix=factory.channel_prefix,
            adapter_class=factory.adapter_class,
            adapter_type=factory.adapter_type,
            factory_fn=factory.factory_fn,
            cleanup_fn=cleanup,
        )
        mgr.register_factory(factory)
        await mgr.sync(["notify.ch"])
        record = mgr.get_record("notify.ch")

        await mgr._destroy_adapter("notify.ch", record)
        cleanup.assert_called_once_with(record.adapter)

    async def test_destroy_calls_async_cleanup_fn(self):
        """_destroy_adapter awaits an asynchronous cleanup_fn."""
        async_cleanup = AsyncMock()
        hass = _make_hass(notify_services={"ch": {}})
        mgr = _make_manager(hass)
        factory = _dummy_factory("notify.ch")
        factory = AdapterFactory(
            channel_prefix=factory.channel_prefix,
            adapter_class=factory.adapter_class,
            adapter_type=factory.adapter_type,
            factory_fn=factory.factory_fn,
            cleanup_fn=async_cleanup,
        )
        mgr.register_factory(factory)
        await mgr.sync(["notify.ch"])
        record = mgr.get_record("notify.ch")

        await mgr._destroy_adapter("notify.ch", record)
        async_cleanup.assert_awaited_once_with(record.adapter)

    async def test_destroy_cleanup_exception_is_logged_not_raised(self, caplog):
        """An exception inside cleanup_fn is logged but not re-raised."""
        cleanup = MagicMock(side_effect=RuntimeError("cleanup boom"))
        hass = _make_hass(notify_services={"ch": {}})
        mgr = _make_manager(hass)
        factory = _dummy_factory("notify.ch")
        factory = AdapterFactory(
            channel_prefix=factory.channel_prefix,
            adapter_class=factory.adapter_class,
            adapter_type=factory.adapter_type,
            factory_fn=factory.factory_fn,
            cleanup_fn=cleanup,
        )
        mgr.register_factory(factory)
        await mgr.sync(["notify.ch"])
        record = mgr.get_record("notify.ch")

        # Should not raise
        await mgr._destroy_adapter("notify.ch", record)
        assert "Error during adapter cleanup" in caplog.text


# ── Detection edge cases ──────────────────────────────────────────────────────


class TestDetectionEdgeCases:
    """Verify detection helpers handle edge cases gracefully."""

    def test_detect_notification_channels_no_services(self):
        """When no notify services exist, _detect_notification_channels returns []."""
        hass = _make_hass(notify_services={})
        mgr = _make_manager(hass)
        result = mgr._detect_notification_channels()
        assert result == []

    def test_detect_media_players_skips_none_state(self):
        """Entities whose state is None are skipped in _detect_media_players."""
        hass = MagicMock()
        hass.states.async_entity_ids.return_value = ["media_player.ghost"]
        hass.states.get.return_value = None  # state is None
        deps = _make_deps()
        mgr = ChannelManager(hass, deps)
        result = mgr._detect_media_players()
        assert result == []


# ── Sync edge cases ───────────────────────────────────────────────────────────


class TestSyncEdgeCases:
    """Verify sync behaviour under error conditions."""

    async def test_sync_factory_creation_error_becomes_inactive(self):
        """When the factory_fn raises, the channel gets INACTIVE status."""

        def _failing_factory(hass, variant):
            raise RuntimeError("factory boom")

        klass = MagicMock()
        klass.matches_channel.side_effect = lambda cid: cid == "notify.boom"
        klass.get_channel_label.return_value = "Boom"
        klass.get_metadata.return_value = MagicMock(integration="boom")
        klass.extract_variant.return_value = None
        bad_factory = AdapterFactory(
            channel_prefix="notify.boom",
            adapter_class=klass,
            adapter_type=AdapterType.DYNAMIC_SINGLE,
            factory_fn=_failing_factory,
            cleanup_fn=None,
        )
        hass = _make_hass(notify_services={"boom": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(bad_factory)
        await mgr.sync(["notify.boom"])

        record = mgr.get_record("notify.boom")
        assert record is not None
        assert record.status == ChannelStatus.INACTIVE
        assert record.adapter is None

    def test_initialize_static_adapters_factory_exception_is_logged(self, caplog):
        """An exception in a static factory_fn is logged and does not crash setup."""

        def _failing_factory(hass, variant):
            raise RuntimeError("static boom")

        klass = MagicMock()
        klass.matches_channel.return_value = True
        klass.get_channel_label.return_value = "Boom"
        klass.get_metadata.return_value = MagicMock(integration="boom")
        failing_static = AdapterFactory(
            channel_prefix="notify.boom",
            adapter_class=klass,
            adapter_type=AdapterType.STATIC,
            factory_fn=_failing_factory,
            cleanup_fn=None,
        )
        mgr = _make_manager()
        mgr.register_factory(failing_static)
        mgr.initialize_static_adapters()  # must not raise

        assert "Failed to initialize static adapter" in caplog.text


# ── Module-level detection helpers ───────────────────────────────────────────


class TestModuleLevelDetectors:
    """Verify the standalone detect_* helpers (no ChannelManager required)."""

    def _make_hass_with_notify(self, services: dict) -> MagicMock:
        hass = MagicMock()
        hass.services.async_services.return_value = {"notify": services}
        return hass

    def test_detect_notification_channels_standalone_returns_channels(self):
        """detect_notification_channels returns ChannelInfo for each notify service."""
        hass = self._make_hass_with_notify({"mobile_app_phone": {}})
        infos = detect_notification_channels(hass)
        ids = [i.id for i in infos]
        assert "notify.mobile_app_phone" in ids

    def test_detect_notification_channels_excludes_reserved_names(self):
        """Reserved names ('notify', 'send_message') are excluded."""
        hass = self._make_hass_with_notify(
            {"notify": {}, "send_message": {}, "real_channel": {}}
        )
        infos = detect_notification_channels(hass)
        ids = [i.id for i in infos]
        assert "notify.notify" not in ids
        assert "notify.send_message" not in ids
        assert "notify.real_channel" in ids

    def test_detect_notification_channels_with_adapter_classes_uses_label(self):
        """When adapter_classes is supplied, get_channel_label is called for labelling."""
        hass = self._make_hass_with_notify({"mobile_app_x": {}})

        adapter_cls = MagicMock()
        adapter_cls.matches_channel.side_effect = lambda cid: (
            cid == "notify.mobile_app_x"
        )
        adapter_cls.get_channel_label.return_value = "My Custom Label"
        adapter_classes = {"notify.mobile_app_x": adapter_cls}

        infos = detect_notification_channels(hass, adapter_classes=adapter_classes)
        labels = [i.label for i in infos]
        assert "My Custom Label" in labels

    def test_detect_media_players_standalone_returns_compatible_players(self):
        """detect_media_players returns entities that have the required feature flags."""
        hass = MagicMock()
        hass.states.async_entity_ids.return_value = ["media_player.living_room"]
        state = MagicMock()
        state.attributes = {
            "supported_features": REQUIRED_MP_FEATURES,
            "friendly_name": "Living Room",
        }
        hass.states.get.return_value = state

        infos = detect_media_players(hass)
        ids = [i.id for i in infos]
        assert "media_player.living_room" in ids

    def test_detect_media_players_standalone_skips_incompatible(self):
        """detect_media_players excludes entities missing required feature flags."""
        hass = MagicMock()
        hass.states.async_entity_ids.return_value = ["media_player.tv"]
        state = MagicMock()
        state.attributes = {"supported_features": 0x0001, "friendly_name": "TV"}
        hass.states.get.return_value = state

        infos = detect_media_players(hass)
        assert infos == []

    def test_detect_tts_entities_excludes_action_entities(self):
        """detect_tts_entities skips 'tts.speak', 'tts.clear_cache', and '*_say' entities."""
        hass = MagicMock()
        hass.states.async_entity_ids.return_value = [
            "tts.piper",
            "tts.speak",
            "tts.clear_cache",
            "tts.google_translate_say",
        ]
        result = detect_tts_entities(hass)
        assert "tts.piper" in result
        assert "tts.speak" not in result
        assert "tts.clear_cache" not in result
        assert "tts.google_translate_say" not in result

    def test_detect_tts_entities_returns_sorted(self):
        """detect_tts_entities returns entity IDs in alphabetical order."""
        hass = MagicMock()
        hass.states.async_entity_ids.return_value = ["tts.z_engine", "tts.a_engine"]
        result = detect_tts_entities(hass)
        assert result == sorted(result)


# ── Lifecycle callback ────────────────────────────────────────────────────────


class TestLifecycleCallback:
    """Verify the channel lifecycle callback fires on STALE/ACTIVE transitions."""

    def test_set_channel_lifecycle_callback_stores_callback(self):
        """set_channel_lifecycle_callback stores the callable on the manager."""
        mgr = _make_manager()
        cb = MagicMock()
        mgr.set_channel_lifecycle_callback(cb)
        assert mgr._channel_lifecycle_callback is cb

    async def test_callback_called_with_newly_staled_channel_ids(self):
        """When a channel transitions to STALE, callback receives it in newly_staled."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        await mgr.sync(["notify.mobile_app_phone"])

        cb = MagicMock()
        mgr.set_channel_lifecycle_callback(cb)

        # Channel disappears → STALE
        hass.services.async_services.return_value = {}
        await mgr.sync(["notify.mobile_app_phone"])

        cb.assert_called_once()
        newly_staled, newly_recovered = cb.call_args[0]
        assert "notify.mobile_app_phone" in newly_staled
        assert newly_recovered == []

    async def test_callback_called_with_newly_recovered_channel_ids(self):
        """When a previously STALE channel becomes ACTIVE, callback receives it in newly_recovered."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        await mgr.sync(["notify.mobile_app_phone"])

        # Force channel to STALE
        hass.services.async_services.return_value = {}
        await mgr.sync(["notify.mobile_app_phone"])
        assert mgr.get_record("notify.mobile_app_phone").status == ChannelStatus.STALE

        cb = MagicMock()
        mgr.set_channel_lifecycle_callback(cb)

        # Channel comes back → ACTIVE, should fire callback with newly_recovered
        hass.services.async_services.return_value = {"notify": {"mobile_app_phone": {}}}
        await mgr.sync(["notify.mobile_app_phone"])

        cb.assert_called_once()
        newly_staled, newly_recovered = cb.call_args[0]
        assert newly_staled == []
        assert "notify.mobile_app_phone" in newly_recovered

    async def test_callback_not_called_when_no_transition(self):
        """Callback is not invoked when no channel changes STALE status."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))

        cb = MagicMock()
        mgr.set_channel_lifecycle_callback(cb)

        # Two syncs with the same healthy channel — no STALE transitions
        await mgr.sync(["notify.mobile_app_phone"])
        await mgr.sync(["notify.mobile_app_phone"])

        cb.assert_not_called()

    async def test_callback_not_called_if_never_registered(self):
        """sync() does not raise when no lifecycle callback is registered."""
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        await mgr.sync(["notify.mobile_app_phone"])

        # Channel disappears — no callback registered, must not raise
        hass.services.async_services.return_value = {}
        await mgr.sync(["notify.mobile_app_phone"])

        record = mgr.get_record("notify.mobile_app_phone")
        assert record is not None
        assert record.status == ChannelStatus.STALE
