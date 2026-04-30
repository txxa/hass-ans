"""Tests for ChannelManager sync and lookup behaviour."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.ans.channels.base import (
    AdapterFactory,
    AdapterType,
    ChannelStatus,
)
from custom_components.ans.channels.channel_manager import ChannelManager
from custom_components.ans.const import REQUIRED_MP_FEATURES
from custom_components.ans.models import ChannelScope

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
