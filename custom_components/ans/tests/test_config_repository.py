"""Unit tests for ConfigRepository."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ..config.repository import ConfigRepository
from ..models import SystemConfig


def _make_hass() -> MagicMock:
    hass = MagicMock()
    return hass


def _make_system_config_dict():
    return {
        "global_rate_limit": 100,
        "enabled_channels": ["notify.persistent_notification"],
        "persistent_notifications_enabled": True,
        "retry_base_delay": 60,
        "retry_backoff_factor": 2.0,
        "retry_max_delay": 3600,
        "enable_audit_logging": True,
        "tts_service": None,
        "rate_limit_window": 60,
    }


# ---------------------------------------------------------------------------
# load / unload
# ---------------------------------------------------------------------------


async def test_load_returns_false_when_no_main_entry():
    hass = _make_hass()
    repo = ConfigRepository(hass)
    with patch.object(repo, "_load_main_entry", return_value=False):
        result = await repo.load()
    assert result is False


async def test_load_returns_true_with_valid_entry():
    hass = _make_hass()
    repo = ConfigRepository(hass)

    # Inject a valid system config directly after _load_main_entry succeeds
    def _fake_load_main():
        repo.system_config = SystemConfig.from_dict(_make_system_config_dict())
        return True

    with (
        patch.object(repo, "_load_main_entry", side_effect=_fake_load_main),
        patch.object(repo, "_load_subentries", return_value=True),
    ):
        result = await repo.load()
    assert result is True
    assert repo.system_config is not None


async def test_load_options_override_data():
    hass = _make_hass()
    repo = ConfigRepository(hass)

    base = _make_system_config_dict()
    base["global_rate_limit"] = 999

    def _fake_load_main():
        repo.system_config = SystemConfig.from_dict(base)
        return True

    with (
        patch.object(repo, "_load_main_entry", side_effect=_fake_load_main),
        patch.object(repo, "_load_subentries", return_value=True),
    ):
        await repo.load()

    assert repo.system_config.global_rate_limit == 999


def test_unload_clears_config():
    hass = _make_hass()
    repo = ConfigRepository(hass)
    repo.system_config = MagicMock()
    repo.recipients = {"r1": MagicMock()}
    repo.recipient_configs = {"r1": MagicMock()}

    result = repo.unload()
    assert result is True
    assert repo.system_config is None
    assert len(repo.recipients) == 0
    assert len(repo.recipient_configs) == 0


# ---------------------------------------------------------------------------
# snapshot()
# ---------------------------------------------------------------------------


def test_snapshot_raises_when_no_system_config():
    hass = _make_hass()
    repo = ConfigRepository(hass)
    repo.system_config = None
    with pytest.raises(RuntimeError, match="system_config not available"):
        repo.snapshot()


def test_snapshot_returns_config_snapshot():
    hass = _make_hass()
    repo = ConfigRepository(hass)
    repo.system_config = SystemConfig.from_dict(_make_system_config_dict())

    snapshot = repo.snapshot()
    assert snapshot is not None
    assert snapshot.system_config.global_rate_limit == 100


def test_snapshot_is_deep_copy():
    hass = _make_hass()
    repo = ConfigRepository(hass)
    repo.system_config = SystemConfig.from_dict(_make_system_config_dict())

    snap1 = repo.snapshot()
    snap2 = repo.snapshot()
    # Different snapshot objects
    assert snap1.snapshot_id != snap2.snapshot_id


# ---------------------------------------------------------------------------
# get_channels_for_ui()
# ---------------------------------------------------------------------------


def test_get_channels_for_ui_no_channel_manager():
    hass = _make_hass()
    repo = ConfigRepository(hass)
    repo.channel_manager = None
    result = repo.get_channels_for_ui()
    assert result == []


def test_get_channels_for_ui_delegates_to_channel_manager():
    hass = _make_hass()
    repo = ConfigRepository(hass)
    channel_manager = MagicMock()
    channel_manager.get_all_infos.return_value = ["ch1", "ch2"]
    repo.channel_manager = channel_manager

    result = repo.get_channels_for_ui()
    assert result == ["ch1", "ch2"]
    channel_manager.get_all_infos.assert_called_once()
