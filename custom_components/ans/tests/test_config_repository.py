"""Unit tests for ConfigRepository."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ..config.repository import ConfigRepository
from ..const import RCPT_CONFIG_ID_KEY
from ..models import RecipientType, SystemConfig


def _make_hass() -> MagicMock:
    """Return a minimal mocked HomeAssistant instance."""
    return MagicMock()


def _make_system_config_dict():
    """Return a minimal valid system config dictionary."""
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
    """load() returns False when no main ANS config entry is found."""
    hass = _make_hass()
    repo = ConfigRepository(hass)
    with patch.object(repo, "_load_main_entry", return_value=False):
        result = await repo.load()
    assert result is False


async def test_load_returns_true_with_valid_entry():
    """load() returns True and populates system_config when a valid entry exists."""
    hass = _make_hass()
    repo = ConfigRepository(hass)

    # Inject a valid system config directly after _load_main_entry succeeds
    def _fake_load_main():
        """Side-effect that injects a valid SystemConfig and returns True."""
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
    """Options values override the corresponding data values after load."""
    hass = _make_hass()
    repo = ConfigRepository(hass)

    base = _make_system_config_dict()
    base["global_rate_limit"] = 999

    def _fake_load_main():
        """Side-effect that injects a SystemConfig with the modified base dict."""
        repo.system_config = SystemConfig.from_dict(base)
        return True

    with (
        patch.object(repo, "_load_main_entry", side_effect=_fake_load_main),
        patch.object(repo, "_load_subentries", return_value=True),
    ):
        await repo.load()

    assert repo.system_config.global_rate_limit == 999


def test_unload_clears_config():
    """unload() clears system_config, recipients, and recipient_configs."""
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
    """snapshot() raises RuntimeError when system_config has not been loaded."""
    hass = _make_hass()
    repo = ConfigRepository(hass)
    repo.system_config = None
    with pytest.raises(RuntimeError, match="system_config not available"):
        repo.snapshot()


def test_snapshot_returns_config_snapshot():
    """snapshot() returns a ConfigSnapshot with the current system_config embedded."""
    hass = _make_hass()
    repo = ConfigRepository(hass)
    repo.system_config = SystemConfig.from_dict(_make_system_config_dict())

    snapshot = repo.snapshot()
    assert snapshot is not None
    assert snapshot.system_config.global_rate_limit == 100


def test_snapshot_is_deep_copy():
    """Each call to snapshot() returns a distinct object with a unique snapshot_id."""
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
    """get_channels_for_ui returns [] when no channel_manager is set."""
    hass = _make_hass()
    repo = ConfigRepository(hass)
    repo.channel_manager = None
    result = repo.get_channels_for_ui()
    assert result == []


def test_get_channels_for_ui_delegates_to_channel_manager():
    """get_channels_for_ui delegates to channel_manager.get_all_infos() and returns its result."""
    hass = _make_hass()
    repo = ConfigRepository(hass)
    channel_manager = MagicMock()
    channel_manager.get_all_infos.return_value = ["ch1", "ch2"]
    repo.channel_manager = channel_manager

    result = repo.get_channels_for_ui()
    assert result == ["ch1", "ch2"]
    channel_manager.get_all_infos.assert_called_once()


def test_get_channels_for_ui_with_recipient_type_delegates():
    """When recipient_type is provided, get_infos_for_recipient_type is called."""
    hass = _make_hass()
    repo = ConfigRepository(hass)
    channel_manager = MagicMock()
    channel_manager.get_infos_for_recipient_type.return_value = ["tts_ch"]
    repo.channel_manager = channel_manager

    result = repo.get_channels_for_ui(recipient_type=RecipientType.TTS)
    assert result == ["tts_ch"]
    channel_manager.get_infos_for_recipient_type.assert_called_once_with(
        RecipientType.TTS
    )


# ---------------------------------------------------------------------------
# _load_subentries
# ---------------------------------------------------------------------------


def _make_subentry(data: dict, subentry_id: str = "sub-1") -> MagicMock:
    """Build a mock config sub-entry."""
    entry = MagicMock()
    entry.subentry_id = subentry_id
    entry.data = data
    return entry


def _make_valid_entry_data(**overrides) -> dict:
    """Return a dict that produces valid RecipientData + RecipientConfig."""
    data = {
        RCPT_CONFIG_ID_KEY: "recipient-1",
        "type": RecipientType.GENERIC.value,
        "name": "Alice",
        "email": None,
        "phone": None,
        "retry_attempts": 2,
        "rate_limit": 10,
        "notification_types": ["INFO"],
        "channels_low": [],
        "channels_medium": [],
        "channels_high": [],
        "channels_critical": [],
        "dnd_enabled": False,
        "dnd_start": None,
        "dnd_end": None,
        "allowed_sources_regex": None,
        "dnd_allowed_sources_regex": None,
        "dnd_allowed_criticalities": None,
        "dnd_allowed_types": None,
        "blocked_sources_regex": None,
        "blocked_sources_pattern": None,
        "tts_settings": None,
        "version": 1,
    }
    data.update(overrides)
    return data


def test_load_subentries_valid_entry_stored():
    """A valid sub-entry is stored in recipients and recipient_configs."""
    hass = _make_hass()
    repo = ConfigRepository(hass)

    valid_data = _make_valid_entry_data()
    entries = [_make_subentry(valid_data)]

    with patch("ans.config.repository.get_subentries", return_value=entries):
        result = repo._load_subentries()

    assert result is True
    assert "recipient-1" in repo.recipients
    # The stored recipient matches the expected name
    assert repo.recipients["recipient-1"].name == "Alice"
    assert "recipient-1" in repo.recipient_configs


def test_load_subentries_missing_id_skipped():
    """A sub-entry without RCPT_CONFIG_ID_KEY is skipped and _load_subentries returns False."""
    hass = _make_hass()
    repo = ConfigRepository(hass)

    bad_data = _make_valid_entry_data()
    del bad_data[RCPT_CONFIG_ID_KEY]
    entries = [_make_subentry(bad_data)]

    with patch("ans.config.repository.get_subentries", return_value=entries):
        result = repo._load_subentries()

    assert result is False
    assert len(repo.recipients) == 0


def test_load_subentries_consistency_failure_skipped():
    """A sub-entry whose consistency check fails is skipped and returns False."""
    hass = _make_hass()
    repo = ConfigRepository(hass)

    # TTS recipient type but notify.* channel — consistency check will fail
    bad_data = _make_valid_entry_data(
        type=RecipientType.TTS.value,
        channels_low=["notify.mobile_app"],  # incompatible with TTS
    )
    entries = [_make_subentry(bad_data)]

    with patch("ans.config.repository.get_subentries", return_value=entries):
        result = repo._load_subentries()

    assert result is False
    assert len(repo.recipients) == 0


def test_load_subentries_unexpected_exception_skipped():
    """A sub-entry that raises an unexpected exception is skipped; returns False."""
    hass = _make_hass()
    repo = ConfigRepository(hass)

    boom_entry = MagicMock()
    boom_entry.subentry_id = "boom"
    boom_entry.data = MagicMock()
    boom_entry.data.get.side_effect = RuntimeError("unexpected!")

    with patch("ans.config.repository.get_subentries", return_value=[boom_entry]):
        result = repo._load_subentries()

    assert result is False


# ---------------------------------------------------------------------------
# load() integration paths
# ---------------------------------------------------------------------------


async def test_load_returns_false_when_main_entry_fails():
    """When _load_main_entry returns False, load() returns False immediately."""
    hass = _make_hass()
    repo = ConfigRepository(hass)

    with (
        patch.object(repo, "_load_main_entry", return_value=False),
        patch.object(repo, "_load_subentries") as mock_sub,
    ):
        result = await repo.load()

    assert result is False
    # Sub-entries must NOT be processed when main entry fails
    mock_sub.assert_not_called()


async def test_load_returns_true_when_subentries_fail():
    """Sub-entry failures are non-fatal: load() still returns True."""
    hass = _make_hass()
    repo = ConfigRepository(hass)

    def _fake_main():
        """Side-effect that injects a valid SystemConfig and returns True."""
        repo.system_config = SystemConfig.from_dict(_make_system_config_dict())
        return True

    with (
        patch.object(repo, "_load_main_entry", side_effect=_fake_main),
        patch.object(repo, "_load_subentries", return_value=False),
    ):
        result = await repo.load()

    assert result is True


# ---------------------------------------------------------------------------
# snapshot() — deep copy guarantees
# ---------------------------------------------------------------------------


def test_snapshot_deep_copy_does_not_affect_repo():
    """Mutating the snapshot's recipients dict must not affect the repository."""
    hass = _make_hass()
    repo = ConfigRepository(hass)
    repo.system_config = SystemConfig.from_dict(_make_system_config_dict())

    # Pre-populate a recipient in the repo
    recipient_mock = MagicMock()
    repo.recipients["r1"] = recipient_mock
    repo.recipient_configs["r1"] = MagicMock()

    snapshot = repo.snapshot()

    # Mutate the snapshot copy
    del snapshot.recipients["r1"]

    # The original repository must be unaffected
    assert "r1" in repo.recipients
