"""Unit tests for volume calculation and management (migrated from VolumeController)."""

from __future__ import annotations

import asyncio
import contextlib
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
    """Return a TTSSettings instance with sensible defaults, allowing field overrides."""
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
    """_calculate_target_volume() returns volume_morning / VOLUME_SCALE when the hour is in the morning window."""
    now_mock = MagicMock()
    now_mock.hour = 7
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(volume_morning=40)
        vol = _calculate_target_volume(
            NotificationCriticality.LOW, settings, "media_player.test"
        )
    assert vol == 40 / VOLUME_SCALE


def test_volume_daytime():
    """_calculate_target_volume() returns volume_daytime / VOLUME_SCALE at midday."""
    now_mock = MagicMock()
    now_mock.hour = 12  # daytime
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(volume_daytime=50)
        vol = _calculate_target_volume(NotificationCriticality.LOW, settings)
    assert vol == 50 / VOLUME_SCALE


def test_volume_evening():
    """_calculate_target_volume() returns volume_evening / VOLUME_SCALE in the evening window."""
    now_mock = MagicMock()
    now_mock.hour = 19  # evening
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(volume_evening=35)
        vol = _calculate_target_volume(NotificationCriticality.LOW, settings)
    assert vol == 35 / VOLUME_SCALE


def test_volume_night_late():
    """_calculate_target_volume() returns volume_night / VOLUME_SCALE for late-night hours (hour=23)."""
    now_mock = MagicMock()
    now_mock.hour = 23  # night
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(volume_night=20)
        vol = _calculate_target_volume(NotificationCriticality.LOW, settings)
    assert vol == 20 / VOLUME_SCALE


def test_volume_night_early():
    """_calculate_target_volume() returns volume_night / VOLUME_SCALE for early-morning hours (hour=3)."""
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
    """When criticality is in volume_override_criticalities, the override level takes priority over time-based volume."""
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
    """When the criticality is not in volume_override_criticalities, the time-based volume is returned."""
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
    """When settings=None, _calculate_target_volume() returns a float in the valid [0.0, 1.0] range."""
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
    """apply_volume() calls capture_volume_intent() with the entity_id and override_volume before setting."""
    registry = _make_registry()
    with (
        patch.object(registry, "capture_volume_intent", AsyncMock()) as mock_capture,
        patch.object(registry, "_set_volume", AsyncMock()),
        patch.object(registry, "record_volume_set_time", MagicMock()),
    ):
        await registry.apply_volume("media_player.test", 0.5)
    mock_capture.assert_called_once_with("media_player.test", override_volume=0.5)


async def test_apply_volume_clears_intent_on_failure():
    """apply_volume() calls complete_intent() to clean up when _set_volume() raises TTSVolumeControlError."""
    registry = _make_registry()

    async def _fail(*a, **kw):
        """Raise TTSVolumeControlError to simulate a failed set_volume call."""
        raise TTSVolumeControlError("Set failed")

    with (
        patch.object(registry, "capture_volume_intent", AsyncMock()),
        patch.object(registry, "_set_volume", side_effect=_fail),
        patch.object(registry, "complete_intent", AsyncMock()) as mock_complete,
        contextlib.suppress(TTSVolumeControlError),
    ):
        await registry.apply_volume("media_player.test", 0.5)
    mock_complete.assert_called_once_with("media_player.test")


# ---------------------------------------------------------------------------
# TTSMediaPlayerAdapter._safe_restore_volume
# ---------------------------------------------------------------------------


async def test_safe_restore_volume_succeeds():
    """_safe_restore_volume() calls volume_registry.restore_volume() with the entity_id."""
    adapter, vol_reg = _make_adapter()
    await adapter._safe_restore_volume("media_player.test")
    vol_reg.restore_volume.assert_called_once_with("media_player.test")


async def test_safe_restore_volume_swallows_exception(caplog):
    """_safe_restore_volume() does not raise when restore_volume() raises; logs 'Failed to restore volume'."""
    adapter, vol_reg = _make_adapter()
    vol_reg.restore_volume.side_effect = Exception("Restore failed")
    # Should not raise
    await adapter._safe_restore_volume("media_player.test")
    assert "Failed to restore volume" in caplog.text
