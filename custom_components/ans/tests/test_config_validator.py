"""Unit tests for ConfigValidator."""

from __future__ import annotations

import pytest

from ..config.validator import ConfigValidator, FieldValidationError

# ---------------------------------------------------------------------------
# Time format validation
# ---------------------------------------------------------------------------


def test_validate_time_format_valid_hhmm():
    result = ConfigValidator._validate_time_format("08:30")
    assert result == "08:30"


def test_validate_time_format_valid_hhmmss():
    result = ConfigValidator._validate_time_format("22:00:00")
    assert result == "22:00:00"


def test_validate_time_format_invalid_raises():
    with pytest.raises(ValueError, match="HH:MM"):
        ConfigValidator._validate_time_format("25:99")


def test_validate_time_format_none_returns_none():
    assert ConfigValidator._validate_time_format(None) is None


# ---------------------------------------------------------------------------
# Email format validation
# ---------------------------------------------------------------------------


def test_validate_email_valid():
    result = ConfigValidator._validate_email_format("user@example.com")
    assert result == "user@example.com"


def test_validate_email_invalid_is_accepted_by_helper():
    # _validate_email_format uses vol.Email(value) which wraps without validating;
    # actual email validation happens in the schema-level methods.
    result = ConfigValidator._validate_email_format("not-an-email")
    assert result == "not-an-email"


def test_validate_email_none_returns_none():
    assert ConfigValidator._validate_email_format(None) is None


# ---------------------------------------------------------------------------
# Phone format validation
# ---------------------------------------------------------------------------


def test_validate_phone_valid_e164():
    result = ConfigValidator._validate_phone_format("+1234567890")
    assert result == "+1234567890"


def test_validate_phone_invalid_starts_with_zero():
    # PHONE_PATTERN is r"^\+?[1-9]\d{1,14}$" — leading 0 is not allowed
    with pytest.raises(ValueError, match="E.164"):
        ConfigValidator._validate_phone_format("0123456789")


def test_validate_phone_none_returns_none():
    assert ConfigValidator._validate_phone_format(None) is None


# ---------------------------------------------------------------------------
# Regex pattern validation
# ---------------------------------------------------------------------------


def test_validate_regex_valid():
    result = ConfigValidator._validate_regex_pattern("^home.*$")
    assert result == "^home.*$"


def test_validate_regex_invalid_raises():
    with pytest.raises(ValueError, match="Invalid regex"):
        ConfigValidator._validate_regex_pattern("[unclosed")


# ---------------------------------------------------------------------------
# DND settings validation
# ---------------------------------------------------------------------------


def test_dnd_disabled_no_times_required():
    # Should not raise when DND is disabled and times are absent
    ConfigValidator._validate_recipient_dnd_settings(
        dnd_enabled=False,
        dnd_start=None,
        dnd_end=None,
        dnd_allowed_sources_pattern=None,
    )


def test_dnd_enabled_requires_times():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=True,
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
        )
    assert (
        "times" in exc_info.value.field.lower()
        or "start" in exc_info.value.field.lower()
        or "end" in exc_info.value.field.lower()
    )


def test_dnd_same_start_end_raises():
    with pytest.raises(FieldValidationError):
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=True,
            dnd_start="22:00",
            dnd_end="22:00",
            dnd_allowed_sources_pattern=None,
        )


def test_dnd_valid_times_ok():
    ConfigValidator._validate_recipient_dnd_settings(
        dnd_enabled=True,
        dnd_start="22:00",
        dnd_end="08:00",
        dnd_allowed_sources_pattern=None,
    )


def test_dnd_invalid_criticality_raises():
    with pytest.raises(FieldValidationError):
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
            dnd_allowed_criticalities=["INVALID_LEVEL"],
        )


def test_dnd_valid_allowed_criticalities():
    # Should not raise
    ConfigValidator._validate_recipient_dnd_settings(
        dnd_enabled=False,
        dnd_start=None,
        dnd_end=None,
        dnd_allowed_sources_pattern=None,
        dnd_allowed_criticalities=["LOW", "HIGH"],
    )


# ---------------------------------------------------------------------------
# validate_system_settings_schema
# ---------------------------------------------------------------------------


def test_system_settings_schema_valid():
    data = {
        "global_rate_limit": 100,
        "enabled_channels": ["notify.persistent_notification"],
    }
    result = ConfigValidator.validate_system_settings_schema(data)
    assert result["global_rate_limit"] == 100


def test_system_settings_schema_empty_channels_raises():
    import voluptuous as vol

    data = {
        "global_rate_limit": 100,
        "enabled_channels": [],
    }
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_system_settings_schema(data)


# ---------------------------------------------------------------------------
# validate_recipient_definition_schema
# ---------------------------------------------------------------------------


def test_recipient_definition_schema_valid():
    data = {"name": "Alice"}
    result = ConfigValidator.validate_recipient_definition_schema(data)
    assert result["name"] == "Alice"


def test_recipient_definition_schema_with_email():
    data = {"name": "Alice", "email": "alice@example.com"}
    result = ConfigValidator.validate_recipient_definition_schema(data)
    assert result["email"] == "alice@example.com"


def test_recipient_definition_schema_invalid_email():
    import voluptuous as vol

    data = {"name": "Alice", "email": "not-an-email"}
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_definition_schema(data)


# ---------------------------------------------------------------------------
# _validate_boolean
# ---------------------------------------------------------------------------


def test_validate_boolean_true():
    assert ConfigValidator._validate_boolean(True) is True


def test_validate_boolean_false():
    assert ConfigValidator._validate_boolean(False) is False


def test_validate_boolean_invalid():
    with pytest.raises(TypeError, match="boolean"):
        ConfigValidator._validate_boolean("yes")


# ---------------------------------------------------------------------------
# validate_recipient_tts_settings_schema — ssml_enabled preload regression
# ---------------------------------------------------------------------------


def _valid_tts_data(**overrides) -> dict:
    """Return a minimal valid TTS settings dict."""
    base = {
        "volume_morning": 40,
        "volume_daytime": 50,
        "volume_evening": 35,
        "volume_night": 20,
        "volume_override_criticalities": [],
        "volume_override_level": 80,
        "message_format": "message_only",
        "ssml_enabled": False,
    }
    base.update(overrides)
    return base


def test_tts_settings_schema_accepts_ssml_enabled_true():
    """Schema must accept and pass through ssml_enabled=True."""
    result = ConfigValidator.validate_recipient_tts_settings_schema(
        _valid_tts_data(ssml_enabled=True)
    )
    assert result["ssml_enabled"] is True


def test_tts_settings_schema_accepts_ssml_enabled_false():
    """Schema must accept and pass through ssml_enabled=False."""
    result = ConfigValidator.validate_recipient_tts_settings_schema(
        _valid_tts_data(ssml_enabled=False)
    )
    assert result["ssml_enabled"] is False


def test_tts_settings_schema_ssml_defaults_to_false_when_absent():
    """When ssml_enabled is omitted the schema default (False) is applied."""
    data = _valid_tts_data()
    data.pop("ssml_enabled")
    result = ConfigValidator.validate_recipient_tts_settings_schema(data)
    assert result["ssml_enabled"] is False


def test_tts_settings_schema_rejects_non_boolean_ssml():
    """ssml_enabled must be a bool; non-bool values should be rejected."""
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_tts_settings_schema(
            _valid_tts_data(ssml_enabled="yes")
        )
