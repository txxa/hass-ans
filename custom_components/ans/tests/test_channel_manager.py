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
        state = MagicMock()
        state.attributes = {
            "supported_features": media_player_features,
            "friendly_name": eid,
        }
        return state if eid in mp_ids else None

    hass.states.get.side_effect = _get_state
    return hass


def _make_deps():
    deps = MagicMock()
    deps.config_repo = MagicMock()
    deps.volume_registry = MagicMock()
    return deps


def _make_manager(hass=None, deps=None) -> ChannelManager:
    hass = hass or _make_hass()
    deps = deps or _make_deps()
    mgr = ChannelManager(hass, deps)
    return mgr


def _dummy_adapter(hass, info) -> MagicMock:
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
    return factory


# ── Initial state ─────────────────────────────────────────────────────────────


class TestInitialState:
    def test_no_records_on_creation(self):
        mgr = _make_manager()
        assert mgr.count_detected() == 0
        assert mgr.count_active() == 0

    def test_get_adapter_unknown_channel_returns_none(self):
        mgr = _make_manager()
        assert mgr.get_adapter("notify.unknown") is None

    def test_get_info_unknown_channel_returns_none(self):
        mgr = _make_manager()
        assert mgr.get_info("notify.unknown") is None


# ── Detection helpers ──────────────────────────────────────────────────────────


class TestDetection:
    def test_detects_notify_services(self):
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        infos = mgr._detect_notification_channels()
        ids = [i.id for i in infos]
        assert "notify.mobile_app_phone" in ids

    def test_excludes_reserved_service_names(self):
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
        hass = _make_hass(
            media_player_ids=["media_player.living_room"],
            media_player_features=REQUIRED_MP_FEATURES,
        )
        mgr = _make_manager(hass)
        infos = mgr._detect_media_players()
        assert any(i.id == "media_player.living_room" for i in infos)

    def test_skips_media_player_without_required_features(self):
        hass = _make_hass(
            media_player_ids=["media_player.tv"],
            media_player_features=0x0001,  # not REQUIRED_MP_FEATURES
        )
        mgr = _make_manager(hass)
        infos = mgr._detect_media_players()
        assert not infos

    def test_media_player_has_tts_scope(self):
        hass = _make_hass(
            media_player_ids=["media_player.kitchen"],
            media_player_features=REQUIRED_MP_FEATURES,
        )
        mgr = _make_manager(hass)
        infos = mgr._detect_media_players()
        assert infos[0].scope == ChannelScope.TTS


# ── Sync: ACTIVE status ───────────────────────────────────────────────────────


class TestSyncActive:
    async def test_detected_enabled_with_factory_becomes_active(self):
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        await mgr.sync(["notify.mobile_app_phone"])
        assert mgr.count_active() == 1
        assert mgr.get_adapter("notify.mobile_app_phone") is not None

    async def test_existing_active_adapter_is_reused(self):
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
    async def test_detected_not_enabled_becomes_detected(self):
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        await mgr.sync([])  # detected but not enabled
        assert mgr.get_adapter("notify.mobile_app_phone") is None
        assert mgr.count_detected() >= 1


# ── Sync: INACTIVE status ─────────────────────────────────────────────────────


class TestSyncInactive:
    async def test_enabled_no_factory_becomes_inactive(self):
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        # No factory registered for this channel
        await mgr.sync(["notify.mobile_app_phone"])
        record = mgr.get_record("notify.mobile_app_phone")
        assert record is not None
        assert record.status == ChannelStatus.INACTIVE


# ── Sync: STALE status ────────────────────────────────────────────────────────


class TestSyncStale:
    async def test_previously_active_disappears_becomes_stale(self):
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
    def test_static_adapter_initialized_at_startup(self):
        hass = _make_hass()
        mgr = _make_manager(hass)
        factory = _dummy_factory(
            "notify.persistent_notification", adapter_type=AdapterType.STATIC
        )
        mgr.register_factory(factory)
        mgr.initialize_static_adapters()
        assert mgr.count_active() == 1

    async def test_static_adapter_not_overwritten_on_sync(self):
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
    async def test_finalize_clears_setup_flag(self):
        mgr = _make_manager()
        assert mgr._setup_in_progress is True
        await mgr.finalize_setup()
        assert mgr._setup_in_progress is False

    async def test_finalize_triggers_pending_resync(self):
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        mgr._last_enabled = ["notify.mobile_app_phone"]
        mgr._pending_resync = True

        await mgr.finalize_setup()
        assert mgr._pending_resync is False

    async def test_no_pending_resync_no_extra_sync(self):
        hass = _make_hass()
        mgr = _make_manager(hass)
        mgr._pending_resync = False
        # Should complete without error
        await mgr.finalize_setup()
        assert mgr._setup_in_progress is False


# ── Counting helpers ──────────────────────────────────────────────────────────


class TestCounting:
    async def test_count_active_excludes_detected_and_inactive(self):
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
    async def test_cleanup_all_clears_records(self):
        hass = _make_hass(notify_services={"mobile_app_phone": {}})
        mgr = _make_manager(hass)
        mgr.register_factory(_dummy_factory("notify.mobile_app_phone"))
        await mgr.sync(["notify.mobile_app_phone"])
        assert mgr.count_active() == 1

        await mgr.cleanup_all()
        assert len(mgr._records) == 0
