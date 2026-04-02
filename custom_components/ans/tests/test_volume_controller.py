"""Unit tests for volume calculation and management (migrated from VolumeController)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.util import dt as dt_util

from ..channels.tts_mediaplayer import (
    VOLUME_SCALE,
    TTSMediaPlayerAdapter,
    _calculate_target_volume,
)
from ..exceptions import TTSVolumeControlError
from ..models.notification import NotificationCriticality
from ..models.recipient import TTSSettings
from ..persistence.volume_restoration import VolumeRestorationRegistry


def _make_tts_settings(**overrides) -> TTSSettings:
    defaults = {
        "volume_morning": 40,
        "volume_daytime": 50,
        "volume_evening": 35,
        "volume_night": 20,
        "volume_override_level": 80,
        "volume_override_criticalities": [],
        "message_format": "title_and_message",
    }
    defaults.update(overrides)
    return TTSSettings(**defaults)


def _make_registry() -> VolumeRestorationRegistry:
    """Return a VolumeRestorationRegistry with a MagicMock hass."""
    hass = MagicMock()
    return VolumeRestorationRegistry(hass)


def _make_adapter() -> tuple[TTSMediaPlayerAdapter, MagicMock]:
    """Return (adapter, volume_registry_mock) with minimal wiring."""
    hass = MagicMock()
    config_repo = MagicMock()
    volume_registry = MagicMock()
    volume_registry.restore_volume = AsyncMock()
    adapter = TTSMediaPlayerAdapter(
        hass=hass,
        entity_name="test",
        config_repo=config_repo,
        volume_registry=volume_registry,
        delivery_lock=asyncio.Lock(),
    )
    return adapter, volume_registry


# ---------------------------------------------------------------------------
# _calculate_target_volume — time-based selection
# ---------------------------------------------------------------------------


def test_volume_morning():
    now_mock = MagicMock()
    now_mock.hour = 7
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(volume_morning=40)
        vol = _calculate_target_volume(
            NotificationCriticality.LOW, settings, "media_player.test"
        )
    assert vol == 40 / VOLUME_SCALE


def test_volume_daytime():
    now_mock = MagicMock()
    now_mock.hour = 12  # daytime
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(volume_daytime=50)
        vol = _calculate_target_volume(NotificationCriticality.LOW, settings)
    assert vol == 50 / VOLUME_SCALE


def test_volume_evening():
    now_mock = MagicMock()
    now_mock.hour = 19  # evening
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(volume_evening=35)
        vol = _calculate_target_volume(NotificationCriticality.LOW, settings)
    assert vol == 35 / VOLUME_SCALE


def test_volume_night_late():
    now_mock = MagicMock()
    now_mock.hour = 23  # night
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(volume_night=20)
        vol = _calculate_target_volume(NotificationCriticality.LOW, settings)
    assert vol == 20 / VOLUME_SCALE


def test_volume_night_early():
    now_mock = MagicMock()
    now_mock.hour = 3  # early morning (still night)
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(volume_night=20)
        vol = _calculate_target_volume(NotificationCriticality.LOW, settings)
    assert vol == 20 / VOLUME_SCALE


# ---------------------------------------------------------------------------
# _calculate_target_volume — criticality override
# ---------------------------------------------------------------------------


def test_volume_override_takes_priority_over_time():
    now_mock = MagicMock()
    now_mock.hour = 12  # daytime
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(
            volume_daytime=50,
            volume_override_level=80,
            volume_override_criticalities=["CRITICAL"],
        )
        vol = _calculate_target_volume(
            NotificationCriticality.CRITICAL, settings, "media_player.test"
        )
    assert vol == 80 / VOLUME_SCALE  # override, not daytime


def test_volume_no_override_when_criticality_not_in_list():
    now_mock = MagicMock()
    now_mock.hour = 12
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(
            volume_daytime=50,
            volume_override_level=80,
            volume_override_criticalities=["CRITICAL"],
        )
        vol = _calculate_target_volume(NotificationCriticality.LOW, settings)
    assert vol == 50 / VOLUME_SCALE  # LOW not in override list → time-based


# ---------------------------------------------------------------------------
# _calculate_target_volume — None settings
# ---------------------------------------------------------------------------


def test_volume_none_settings_uses_defaults():
    now_mock = MagicMock()
    now_mock.hour = 12
    with patch.object(dt_util, "now", return_value=now_mock):
        vol = _calculate_target_volume(NotificationCriticality.LOW, None)
    # Just check it returns a float in range
    assert 0.0 <= vol <= 1.0


# ---------------------------------------------------------------------------
# VolumeRestorationRegistry.apply_volume
# ---------------------------------------------------------------------------


async def test_apply_volume_calls_capture_and_set():
    registry = _make_registry()
    with (
        patch.object(registry, "capture_volume_intent", AsyncMock()) as mock_capture,
        patch.object(registry, "_set_volume", AsyncMock()),
        patch.object(registry, "record_volume_set_time", MagicMock()),
    ):
        await registry.apply_volume("media_player.test", 0.5)
    mock_capture.assert_called_once_with("media_player.test", override_volume=0.5)


async def test_apply_volume_clears_intent_on_failure():
    registry = _make_registry()

    async def _fail(*a, **kw):
        raise TTSVolumeControlError("Set failed")

    with (
        patch.object(registry, "capture_volume_intent", AsyncMock()),
        patch.object(registry, "_set_volume", side_effect=_fail),
        patch.object(registry, "complete_intent", AsyncMock()) as mock_complete,
    ):
        try:
            await registry.apply_volume("media_player.test", 0.5)
        except TTSVolumeControlError:
            pass
    mock_complete.assert_called_once_with("media_player.test")


# ---------------------------------------------------------------------------
# TTSMediaPlayerAdapter._safe_restore_volume
# ---------------------------------------------------------------------------


async def test_safe_restore_volume_succeeds():
    adapter, vol_reg = _make_adapter()
    await adapter._safe_restore_volume("media_player.test")
    vol_reg.restore_volume.assert_called_once_with("media_player.test")


async def test_safe_restore_volume_swallows_exception(caplog):
    adapter, vol_reg = _make_adapter()
    vol_reg.restore_volume.side_effect = Exception("Restore failed")
    # Should not raise
    await adapter._safe_restore_volume("media_player.test")
    assert "Failed to restore volume" in caplog.text
