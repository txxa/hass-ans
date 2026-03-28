"""Unit tests for VolumeController."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.util import dt as dt_util

from ..channels.volume_controller import VOLUME_SCALE, VolumeController
from ..exceptions import TTSVolumeControlError
from ..models.notification import NotificationCriticality
from ..models.recipient import TTSSettings


def _make_controller() -> tuple[VolumeController, MagicMock]:
    hass = MagicMock()
    volume_registry = MagicMock()
    volume_registry.capture_volume_intent = AsyncMock()
    volume_registry.complete_intent = AsyncMock()
    volume_registry.restore_volume = AsyncMock()
    ctrl = VolumeController(hass=hass, volume_registry=volume_registry)
    return ctrl, volume_registry


def _make_tts_settings(**overrides) -> TTSSettings:
    defaults = {
        "volume_morning": 40,
        "volume_daytime": 50,
        "volume_evening": 35,
        "volume_night": 20,
        "volume_override_level": 80,
        "volume_override_criticalities": [],
        "message_format": "title_and_message",
        "trailing_silence_ms": 0,
    }
    defaults.update(overrides)
    return TTSSettings(**defaults)


# ---------------------------------------------------------------------------
# calculate_target_volume — time-based selection
# ---------------------------------------------------------------------------


def test_volume_morning():
    ctrl, _ = _make_controller()
    now_mock = MagicMock()
    now_mock.hour = 7
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(volume_morning=40)
        vol = ctrl.calculate_target_volume(
            NotificationCriticality.LOW, settings, "media_player.test"
        )
    assert vol == 40 / VOLUME_SCALE


def test_volume_daytime():
    ctrl, _ = _make_controller()
    now_mock = MagicMock()
    now_mock.hour = 12  # daytime
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(volume_daytime=50)
        vol = ctrl.calculate_target_volume(NotificationCriticality.LOW, settings)
    assert vol == 50 / VOLUME_SCALE


def test_volume_evening():
    ctrl, _ = _make_controller()
    now_mock = MagicMock()
    now_mock.hour = 19  # evening
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(volume_evening=35)
        vol = ctrl.calculate_target_volume(NotificationCriticality.LOW, settings)
    assert vol == 35 / VOLUME_SCALE


def test_volume_night_late():
    ctrl, _ = _make_controller()
    now_mock = MagicMock()
    now_mock.hour = 23  # night
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(volume_night=20)
        vol = ctrl.calculate_target_volume(NotificationCriticality.LOW, settings)
    assert vol == 20 / VOLUME_SCALE


def test_volume_night_early():
    ctrl, _ = _make_controller()
    now_mock = MagicMock()
    now_mock.hour = 3  # early morning (still night)
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(volume_night=20)
        vol = ctrl.calculate_target_volume(NotificationCriticality.LOW, settings)
    assert vol == 20 / VOLUME_SCALE


# ---------------------------------------------------------------------------
# calculate_target_volume — criticality override
# ---------------------------------------------------------------------------


def test_volume_override_takes_priority_over_time():
    ctrl, _ = _make_controller()
    now_mock = MagicMock()
    now_mock.hour = 12  # daytime
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(
            volume_daytime=50,
            volume_override_level=80,
            volume_override_criticalities=["CRITICAL"],
        )
        vol = ctrl.calculate_target_volume(
            NotificationCriticality.CRITICAL, settings, "media_player.test"
        )
    assert vol == 80 / VOLUME_SCALE  # override, not daytime


def test_volume_no_override_when_criticality_not_in_list():
    ctrl, _ = _make_controller()
    now_mock = MagicMock()
    now_mock.hour = 12
    with patch.object(dt_util, "now", return_value=now_mock):
        settings = _make_tts_settings(
            volume_daytime=50,
            volume_override_level=80,
            volume_override_criticalities=["CRITICAL"],
        )
        vol = ctrl.calculate_target_volume(NotificationCriticality.LOW, settings)
    assert vol == 50 / VOLUME_SCALE  # LOW not in override list → time-based


# ---------------------------------------------------------------------------
# calculate_target_volume — None settings
# ---------------------------------------------------------------------------


def test_volume_none_settings_uses_defaults():
    ctrl, _ = _make_controller()
    now_mock = MagicMock()
    now_mock.hour = 12
    with patch.object(dt_util, "now", return_value=now_mock):
        vol = ctrl.calculate_target_volume(NotificationCriticality.LOW, None)
    # Just check it returns a float in range
    assert 0.0 <= vol <= 1.0


# ---------------------------------------------------------------------------
# apply_volume
# ---------------------------------------------------------------------------


async def test_apply_volume_calls_capture_and_set():
    ctrl, vol_reg = _make_controller()
    with patch.object(ctrl, "_set_volume", AsyncMock()) as mock_set:
        await ctrl.apply_volume("media_player.test", 0.5)
    vol_reg.capture_volume_intent.assert_called_once_with(
        "media_player.test", override_volume=0.5
    )


async def test_apply_volume_clears_intent_on_failure():
    ctrl, vol_reg = _make_controller()

    async def _fail(*a, **kw):
        raise TTSVolumeControlError("Set failed")

    with patch.object(ctrl, "_set_volume", side_effect=_fail):
        try:
            await ctrl.apply_volume("media_player.test", 0.5)
        except TTSVolumeControlError:
            pass
    vol_reg.complete_intent.assert_called_once_with("media_player.test")


# ---------------------------------------------------------------------------
# safe_restore_volume
# ---------------------------------------------------------------------------


async def test_safe_restore_volume_succeeds():
    ctrl, vol_reg = _make_controller()
    await ctrl.safe_restore_volume("media_player.test")
    vol_reg.restore_volume.assert_called_once_with("media_player.test")


async def test_safe_restore_volume_swallows_exception(caplog):
    ctrl, vol_reg = _make_controller()
    vol_reg.restore_volume.side_effect = Exception("Restore failed")
    # Should not raise
    await ctrl.safe_restore_volume("media_player.test")
    assert "Failed to restore volume" in caplog.text
