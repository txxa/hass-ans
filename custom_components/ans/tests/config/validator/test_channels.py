"""Unit tests for channel mapping validation (config/validator/channels.py)."""

from __future__ import annotations

import pytest
import voluptuous as vol

from ....config.validator import ConfigValidator, ValidationContext
from ....config.validator.channels import _validate_recipient_channel_mapping
from ....config.validator.common import FieldValidationError
from ....models import RecipientType

# ---------------------------------------------------------------------------
# _validate_recipient_channel_mapping
# ---------------------------------------------------------------------------


def test_channel_mapping_tts_rejects_notify():
    """A TTS recipient with a 'notify.*' channel raises FieldValidationError."""
    with pytest.raises(FieldValidationError):
        _validate_recipient_channel_mapping(
            channels_low=["notify.mobile_app"],
            channels_medium=None,
            channels_high=None,
            channels_critical=None,
            recipient_type=RecipientType.TTS,
        )


def test_channel_mapping_tts_accepts_media_player():
    # Must not raise
    """A TTS recipient with a 'media_player.*' channel does not raise."""
    _validate_recipient_channel_mapping(
        channels_low=["media_player.bedroom"],
        channels_medium=None,
        channels_high=None,
        channels_critical=None,
        recipient_type=RecipientType.TTS,
    )


def test_channel_mapping_non_tts_rejects_media_player():
    """A non-TTS recipient with a 'media_player.*' channel raises FieldValidationError."""
    with pytest.raises(FieldValidationError):
        _validate_recipient_channel_mapping(
            channels_low=["media_player.bedroom"],
            channels_medium=None,
            channels_high=None,
            channels_critical=None,
            recipient_type=RecipientType.GENERIC,
        )


def test_channel_mapping_non_tts_accepts_notify():
    # Must not raise
    """A non-TTS recipient with a 'notify.*' channel does not raise."""
    _validate_recipient_channel_mapping(
        channels_low=["notify.mobile_app"],
        channels_medium=None,
        channels_high=None,
        channels_critical=None,
        recipient_type=RecipientType.GENERIC,
    )


def test_channel_mapping_none_recipient_type_accepts_both():
    # Must not raise for either channel pattern
    """When recipient_type is None, both 'notify.*' and 'media_player.*' channels are accepted."""
    _validate_recipient_channel_mapping(
        channels_low=["notify.mobile_app"],
        channels_medium=["media_player.bedroom"],
        channels_high=None,
        channels_critical=None,
        recipient_type=None,
    )


def test_channel_mapping_channel_not_in_available_raises():
    """A channel not present in available_channels raises FieldValidationError."""
    with pytest.raises(FieldValidationError):
        _validate_recipient_channel_mapping(
            channels_low=["notify.mobile_app"],
            channels_medium=None,
            channels_high=None,
            channels_critical=None,
            available_channels=["notify.other_channel"],
            recipient_type=None,
        )


def test_channel_mapping_none_channel_lists_ignored():
    # All None lists should silently pass regardless of recipient_type
    """All-None channel lists silently pass regardless of recipient_type."""
    _validate_recipient_channel_mapping(
        channels_low=None,
        channels_medium=None,
        channels_high=None,
        channels_critical=None,
        recipient_type=RecipientType.TTS,
    )


# ---------------------------------------------------------------------------
# validate_recipient_channel_mapping_schema
# ---------------------------------------------------------------------------


def test_recipient_channel_mapping_schema_empty_dict_passes():
    """validate_recipient_channel_mapping_schema({}, ctx) accepts an empty mapping without raising."""
    ctx = ValidationContext(available_channels=["notify.mobile_app"])
    result = ConfigValidator.validate_recipient_channel_mapping_schema({}, ctx)
    assert result is not None


def test_recipient_channel_mapping_schema_valid_channel_passes():
    """A channel present in available_channels is accepted and stored in the result."""
    ctx = ValidationContext(available_channels=["notify.mobile_app"])
    result = ConfigValidator.validate_recipient_channel_mapping_schema(
        {"channels_low": ["notify.mobile_app"]}, ctx
    )
    assert result["channels_low"] == ["notify.mobile_app"]


def test_recipient_channel_mapping_schema_channel_not_available_accepts():
    # NOTE: The schema uses vol.Any(vol.All([vol.In(available)], min_len=1), vol.Length(min=0)).
    # Because vol.Length(min=0) accepts *any* list (length >= 0), the fallback branch
    # makes invalid channels silently pass.  This test documents the current (known)
    # behaviour rather than the desired behaviour.
    """Due to the vol.Length(min=0) fallback branch, unavailable channels are silently accepted (documented behaviour)."""
    ctx = ValidationContext(available_channels=["notify.other"])
    result = ConfigValidator.validate_recipient_channel_mapping_schema(
        {"channels_low": ["notify.mobile_app"]}, ctx
    )
    assert result is not None  # no vol.Invalid raised


def test_recipient_channel_mapping_schema_extra_key_raises():
    """An unknown key in the channel mapping dict raises vol.Invalid."""
    ctx = ValidationContext(available_channels=[])
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_channel_mapping_schema(
            {"unknown_key": "value"}, ctx
        )


# ---------------------------------------------------------------------------
# _validate_recipient_channel_mapping — TypeError path
# ---------------------------------------------------------------------------


def test_channel_mapping_non_string_channel_item_raises():
    """A list with a non-string channel item raises FieldValidationError."""
    with pytest.raises(FieldValidationError):
        _validate_recipient_channel_mapping(
            channels_low=[42],  # type: ignore[list-item]
            channels_medium=None,
            channels_high=None,
            channels_critical=None,
            recipient_type=None,
        )
