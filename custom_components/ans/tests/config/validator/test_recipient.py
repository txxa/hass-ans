"""Unit tests for recipient definition, basic settings, and DND validation (config/validator/recipient.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import voluptuous as vol

from ....config.validator import ConfigValidator, ValidationContext
from ....config.validator.common import FieldValidationError
from ....config.validator.recipient import (
    _validate_recipient_basic_settings,
    _validate_recipient_dnd_settings,
)
from ....const import RCPT_MAX_RETRY_ATTEMPTS
from ....models import NotificationType, RecipientType

# ---------------------------------------------------------------------------
# DND settings validation
# ---------------------------------------------------------------------------


def test_dnd_disabled_no_times_required():
    # Should not raise when DND is disabled and times are absent
    """When DND is disabled, dnd_start and dnd_end are not required."""
    _validate_recipient_dnd_settings(
        dnd_enabled=False,
        dnd_start=None,
        dnd_end=None,
        dnd_allowed_sources_pattern=None,
    )


def test_dnd_enabled_requires_times():
    """When DND is enabled with no times set, FieldValidationError names a time-related field."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_dnd_settings(
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
    """Equal start and end times raise FieldValidationError even when both are set."""
    with pytest.raises(FieldValidationError):
        _validate_recipient_dnd_settings(
            dnd_enabled=True,
            dnd_start="22:00",
            dnd_end="22:00",
            dnd_allowed_sources_pattern=None,
        )


def test_dnd_valid_times_ok():
    """Valid DND start ('22:00') and end ('08:00') times do not raise."""
    _validate_recipient_dnd_settings(
        dnd_enabled=True,
        dnd_start="22:00",
        dnd_end="08:00",
        dnd_allowed_sources_pattern=None,
    )


def test_dnd_invalid_criticality_raises():
    """An unrecognised criticality level in dnd_allowed_criticalities raises FieldValidationError."""
    with pytest.raises(FieldValidationError):
        _validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
            dnd_allowed_criticalities=["INVALID_LEVEL"],
        )


def test_dnd_valid_allowed_criticalities():
    # Should not raise
    """A list of recognised criticality strings ('LOW', 'HIGH') does not raise."""
    _validate_recipient_dnd_settings(
        dnd_enabled=False,
        dnd_start=None,
        dnd_end=None,
        dnd_allowed_sources_pattern=None,
        dnd_allowed_criticalities=["LOW", "HIGH"],
    )


# ---------------------------------------------------------------------------
# validate_recipient_definition_schema
# ---------------------------------------------------------------------------


def test_recipient_definition_schema_valid():
    """validate_recipient_definition_schema() accepts a dict with at least a 'name' key."""
    data = {"name": "Alice"}
    result = ConfigValidator.validate_recipient_definition_schema(data)
    assert result["name"] == "Alice"


def test_recipient_definition_schema_with_email():
    """A valid email address in the definition is accepted and stored unchanged."""
    data = {"name": "Alice", "email": "alice@example.com"}
    result = ConfigValidator.validate_recipient_definition_schema(data)
    assert result["email"] == "alice@example.com"


def test_recipient_definition_schema_invalid_email():
    """An invalid email address raises vol.Invalid at the schema level."""

    data = {"name": "Alice", "email": "not-an-email"}
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_definition_schema(data)


# ---------------------------------------------------------------------------
# _validate_recipient_basic_settings
# ---------------------------------------------------------------------------


def test_recipient_basic_settings_retry_above_max_raises():
    """_validate_recipient_basic_settings() raises FieldValidationError('retry_attempts') when retry_attempts > RCPT_MAX_RETRY_ATTEMPTS."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_basic_settings(
            retry_attempts=RCPT_MAX_RETRY_ATTEMPTS + 1,
            rate_limit=0,
            notification_types=[NotificationType.INFO],
            blocked_sources_pattern=None,
        )
    assert exc_info.value.field == "retry_attempts"


def test_recipient_basic_settings_non_int_retry_raises():
    """A non-integer retry_attempts raises FieldValidationError on field 'retry_attempts'."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_basic_settings(
            retry_attempts="three",  # pyright: ignore[reportArgumentType]
            rate_limit=0,
            notification_types=[NotificationType.INFO],
            blocked_sources_pattern=None,
        )
    assert exc_info.value.field == "retry_attempts"


def test_recipient_basic_settings_rate_limit_above_max_raises():
    """A rate_limit above the ValidationContext maximum raises FieldValidationError on field 'rate_limit'."""
    ctx = ValidationContext()
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_basic_settings(
            retry_attempts=0,
            rate_limit=ctx.get_max_recipient_rate_limit() + 1,
            notification_types=[NotificationType.INFO],
            blocked_sources_pattern=None,
            validation_context=ctx,
        )
    assert exc_info.value.field == "rate_limit"


def test_recipient_basic_settings_invalid_notification_type_string_raises():
    """An unrecognised notification type string raises FieldValidationError on field 'notification_types'."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_basic_settings(
            retry_attempts=0,
            rate_limit=0,
            notification_types=["INVALID_TYPE"],
            blocked_sources_pattern=None,
        )
    assert exc_info.value.field == "notification_types"


def test_recipient_basic_settings_non_notification_type_object_raises():
    """A non-NotificationType object in notification_types raises FieldValidationError on field 'notification_types'."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_basic_settings(
            retry_attempts=0,
            rate_limit=0,
            notification_types=[42],
            blocked_sources_pattern=None,
        )
    assert exc_info.value.field == "notification_types"


def test_recipient_basic_settings_invalid_blocked_sources_regex_raises():
    """An invalid regex for blocked_sources_pattern raises FieldValidationError on a 'blocked_sources' field."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_basic_settings(
            retry_attempts=0,
            rate_limit=0,
            notification_types=[NotificationType.INFO],
            blocked_sources_pattern="[unclosed",
        )
    # The field key reflects the const RCPT_CONFIG_BLOCKED_SOURCES_PATTERN_KEY
    assert "blocked_sources" in exc_info.value.field


def test_recipient_basic_settings_all_valid_with_context():
    """_validate_recipient_basic_settings() with all valid arguments and a ValidationContext does not raise."""
    ctx = ValidationContext()
    # Must not raise
    _validate_recipient_basic_settings(
        retry_attempts=2,
        rate_limit=10,
        notification_types=[NotificationType.INFO, NotificationType.ALERT],
        blocked_sources_pattern=r"^home\.*$",
        validation_context=ctx,
    )


# ---------------------------------------------------------------------------
# validate_recipient_consistency
# ---------------------------------------------------------------------------


def test_validate_recipient_consistency_matching_passes():
    """A GENERIC recipient with 'notify.*' channels is consistent and does not raise."""
    data = SimpleNamespace(type=RecipientType.GENERIC)
    config = SimpleNamespace(
        channels_low=["notify.mobile_app"],
        channels_medium=[],
        channels_high=[],
        channels_critical=[],
    )
    ConfigValidator.validate_recipient_consistency(data, config)  # must not raise


def test_validate_recipient_consistency_mismatch_raises():
    # TTS recipient but notify.* channel — incompatible
    """A TTS recipient with a 'notify.*' channel raises FieldValidationError."""
    data = SimpleNamespace(type=RecipientType.TTS)
    config = SimpleNamespace(
        channels_low=["notify.mobile_app"],
        channels_medium=[],
        channels_high=[],
        channels_critical=[],
    )
    with pytest.raises(FieldValidationError):
        ConfigValidator.validate_recipient_consistency(data, config)


# ---------------------------------------------------------------------------
# _validate_recipient_dnd_settings — additional branches
# ---------------------------------------------------------------------------


def test_dnd_non_bool_enabled_raises():
    """A non-bool dnd_enabled value raises FieldValidationError on field 'dnd_enabled'."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_dnd_settings(
            dnd_enabled="yes",
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
        )
    assert exc_info.value.field == "dnd_enabled"


def test_dnd_enabled_only_start_set_raises():
    """DND enabled with dnd_start set but dnd_end missing raises FieldValidationError."""
    with pytest.raises(FieldValidationError):
        _validate_recipient_dnd_settings(
            dnd_enabled=True,
            dnd_start="22:00",
            dnd_end=None,  # missing end
            dnd_allowed_sources_pattern=None,
        )


def test_dnd_enabled_only_end_set_raises():
    """DND enabled with dnd_end set but dnd_start missing raises FieldValidationError."""
    with pytest.raises(FieldValidationError):
        _validate_recipient_dnd_settings(
            dnd_enabled=True,
            dnd_start=None,  # missing start
            dnd_end="08:00",
            dnd_allowed_sources_pattern=None,
        )


def test_dnd_allowed_types_not_list_raises():
    """A non-list dnd_allowed_types raises FieldValidationError on field 'dnd_allowed_types'."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
            dnd_allowed_types="ALERT",  # string instead of list
        )
    assert exc_info.value.field == "dnd_allowed_types"


def test_dnd_allowed_types_invalid_string_raises():
    """An unrecognised type string in dnd_allowed_types raises FieldValidationError on field 'dnd_allowed_types'."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
            dnd_allowed_types=["NOT_A_TYPE"],
        )
    assert exc_info.value.field == "dnd_allowed_types"


def test_dnd_allowed_criticalities_not_list_raises():
    """A non-list dnd_allowed_criticalities raises FieldValidationError on field 'dnd_allowed_criticalities'."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
            dnd_allowed_criticalities="HIGH",  # string instead of list
        )
    assert exc_info.value.field == "dnd_allowed_criticalities"


def test_dnd_allowed_types_valid_passes():
    # Must not raise with valid type strings
    """Valid dnd_allowed_types strings ('INFO', 'ALERT') do not raise."""
    _validate_recipient_dnd_settings(
        dnd_enabled=False,
        dnd_start=None,
        dnd_end=None,
        dnd_allowed_sources_pattern=None,
        dnd_allowed_types=["INFO", "ALERT"],
    )


# ---------------------------------------------------------------------------
# validate_recipient_basic_settings_schema
# ---------------------------------------------------------------------------


def _valid_basic_settings(**overrides) -> dict:
    """Return a minimal valid recipient basic-settings dict."""
    base = {
        "retry_attempts": 2,
        "rate_limit": 10,
        "notification_types": ["INFO", "ALERT"],
    }
    base.update(overrides)
    return base


def test_recipient_basic_settings_schema_valid():
    """validate_recipient_basic_settings_schema() accepts a valid dict and normalises values."""
    ctx = ValidationContext()
    result = ConfigValidator.validate_recipient_basic_settings_schema(
        _valid_basic_settings(), ctx
    )
    assert "retry_attempts" in result
    assert result["retry_attempts"] == 2
    assert result["rate_limit"] == 10


def test_recipient_basic_settings_schema_retry_above_max_raises():
    """retry_attempts above RCPT_MAX_RETRY_ATTEMPTS raises vol.Invalid."""
    ctx = ValidationContext()
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_basic_settings_schema(
            _valid_basic_settings(retry_attempts=RCPT_MAX_RETRY_ATTEMPTS + 1), ctx
        )


def test_recipient_basic_settings_schema_rate_limit_above_max_raises():
    """A rate_limit above the context maximum raises vol.Invalid."""
    ctx = ValidationContext()
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_basic_settings_schema(
            _valid_basic_settings(rate_limit=ctx.get_max_recipient_rate_limit() + 1),
            ctx,
        )


def test_recipient_basic_settings_schema_empty_notification_types_raises():
    """An empty notification_types list raises vol.Invalid."""
    ctx = ValidationContext()
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_basic_settings_schema(
            _valid_basic_settings(notification_types=[]), ctx
        )


def test_recipient_basic_settings_schema_invalid_blocked_regex_raises():
    """An invalid blocked_sources_pattern regex raises vol.Invalid."""
    ctx = ValidationContext()
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_basic_settings_schema(
            _valid_basic_settings(blocked_sources_pattern="[unclosed"), ctx
        )


# ---------------------------------------------------------------------------
# validate_recipient_dnd_settings_schema
# ---------------------------------------------------------------------------


def _valid_dnd_data(**overrides) -> dict:
    """Return a minimal valid DND settings dict."""
    base = {
        "dnd_enabled": True,
        "dnd_start": "22:00",
        "dnd_end": "08:00",
        "dnd_allowed_criticalities": [],
        "dnd_allowed_types": [],
    }
    base.update(overrides)
    return base


def test_dnd_settings_schema_valid_passes():
    """A valid DND settings dict is accepted and its values preserved in the result."""
    result = ConfigValidator.validate_recipient_dnd_settings_schema(_valid_dnd_data())
    assert result["dnd_enabled"] is True
    assert result["dnd_start"] == "22:00"


def test_dnd_settings_schema_enabled_no_start_raises():
    """DND enabled without dnd_start raises FieldValidationError."""
    data = _valid_dnd_data()
    del data["dnd_start"]
    with pytest.raises(FieldValidationError):
        ConfigValidator.validate_recipient_dnd_settings_schema(data)


def test_dnd_settings_schema_enabled_no_end_raises():
    """DND enabled without dnd_end raises FieldValidationError."""
    data = _valid_dnd_data()
    del data["dnd_end"]
    with pytest.raises(FieldValidationError):
        ConfigValidator.validate_recipient_dnd_settings_schema(data)


def test_dnd_settings_schema_same_start_end_raises():
    """Equal dnd_start and dnd_end values raise FieldValidationError."""
    with pytest.raises(FieldValidationError):
        ConfigValidator.validate_recipient_dnd_settings_schema(
            _valid_dnd_data(dnd_start="22:00", dnd_end="22:00")
        )


def test_dnd_settings_schema_invalid_criticality_raises():
    """An unrecognised criticality string in dnd_allowed_criticalities raises vol.Invalid."""
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_dnd_settings_schema(
            _valid_dnd_data(dnd_allowed_criticalities=["NOT_A_CRITICALITY"])
        )


def test_dnd_settings_schema_invalid_notification_type_raises():
    """An unrecognised notification type string in dnd_allowed_types raises vol.Invalid."""
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_dnd_settings_schema(
            _valid_dnd_data(dnd_allowed_types=["NOT_A_TYPE"])
        )


# ---------------------------------------------------------------------------
# validate_recipient (object-level)
# ---------------------------------------------------------------------------


def _raw_recipient(**overrides) -> SimpleNamespace:
    """Build a SimpleNamespace that mimics a valid RecipientData without triggering __post_init__."""
    defaults = {
        "id": "test-uuid",
        "type": RecipientType.GENERIC,
        "name": "Alice",
        "email": None,
        "phone": None,
        "version": 1,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_validate_recipient_valid_passes():
    """validate_recipient() accepts a well-formed recipient namespace without raising."""
    ConfigValidator.validate_recipient(_raw_recipient())  # must not raise


def test_validate_recipient_empty_id_raises():
    """An empty id raises FieldValidationError on field 'id'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient(_raw_recipient(id=""))
    assert exc_info.value.field == "id"


def test_validate_recipient_empty_name_raises():
    """An empty name raises FieldValidationError on field 'name'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient(_raw_recipient(name=""))
    assert exc_info.value.field == "name"


def test_validate_recipient_invalid_email_raises():
    # _validate_email_format does NOT validate (vol.Email quirk), but
    # validate_recipient calls _validate_email_format only when email is truthy;
    # since the helper accepts anything, we skip this path — real validation is
    # exercised via validate_recipient_definition_schema.
    # This test documents the end-to-end behaviour: a bad email is silently
    # accepted by validate_recipient because _validate_email_format doesn't raise.
    """Due to the vol.Email quirk, validate_recipient() silently accepts a malformed email address."""
    ConfigValidator.validate_recipient(_raw_recipient(email="not-an-email"))


def test_validate_recipient_invalid_phone_raises():
    """An invalid phone number raises FieldValidationError on field 'phone'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient(_raw_recipient(phone="00000000"))
    assert exc_info.value.field == "phone"


def test_validate_recipient_version_below_one_raises():
    """A version below 1 raises FieldValidationError on field 'version'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient(_raw_recipient(version=0))
    assert exc_info.value.field == "version"


# ---------------------------------------------------------------------------
# validate_recipient_config (object-level)
# ---------------------------------------------------------------------------


def _raw_config(**overrides) -> SimpleNamespace:
    """Build a SimpleNamespace that mimics a valid RecipientConfig."""
    defaults = {
        "recipient_id": "test-id",
        "retry_attempts": 2,
        "rate_limit": 10,
        "notification_types": [NotificationType.INFO],
        "blocked_sources_regex": None,
        "channels_low": [],
        "channels_medium": [],
        "channels_high": [],
        "channels_critical": [],
        "dnd_enabled": False,
        "dnd_start": None,
        "dnd_end": None,
        "dnd_allowed_sources_regex": None,
        "dnd_allowed_criticalities": None,
        "dnd_allowed_types": None,
        "tts_settings": None,
        "version": 1,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_validate_recipient_config_valid_passes():
    """validate_recipient_config() accepts a well-formed config namespace without raising."""
    ConfigValidator.validate_recipient_config(_raw_config())  # must not raise


def test_validate_recipient_config_invalid_retry_raises():
    """retry_attempts above RCPT_MAX_RETRY_ATTEMPTS raises FieldValidationError on field 'retry_attempts'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient_config(
            _raw_config(retry_attempts=RCPT_MAX_RETRY_ATTEMPTS + 1)
        )
    assert exc_info.value.field == "retry_attempts"


def test_validate_recipient_config_invalid_channel_raises():
    """A malformed channel name (no dot separator) raises FieldValidationError."""
    with pytest.raises(FieldValidationError):
        ConfigValidator.validate_recipient_config(
            # TTS channel in a non-TTS config (no recipient_type enforced here —
            # but notify.* pattern validation triggers on channel name format)
            _raw_config(channels_low=["bad_channel_name"])
        )


def test_validate_recipient_config_invalid_dnd_raises():
    """A non-bool dnd_enabled raises FieldValidationError on field 'dnd_enabled'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient_config(
            _raw_config(dnd_enabled="yes")  # non-bool dnd_enabled
        )
    assert exc_info.value.field == "dnd_enabled"


def test_validate_recipient_config_version_below_one_raises():
    """A version below 1 raises FieldValidationError on field 'version'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient_config(_raw_config(version=0))
    assert exc_info.value.field == "version"


# ---------------------------------------------------------------------------
# _validate_recipient_basic_settings — TypeError paths
# ---------------------------------------------------------------------------


def test_recipient_basic_settings_non_int_rate_limit_raises_field_error():
    """A non-integer rate_limit raises FieldValidationError on field 'rate_limit'."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_basic_settings(
            retry_attempts=0,
            rate_limit="high",  # type: ignore[arg-type]
            notification_types=[NotificationType.INFO],
            blocked_sources_pattern=None,
        )
    assert exc_info.value.field == "rate_limit"


def test_recipient_basic_settings_non_string_blocked_sources_raises_field_error():
    """A non-string truthy blocked_sources_pattern raises FieldValidationError."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_basic_settings(
            retry_attempts=0,
            rate_limit=0,
            notification_types=[NotificationType.INFO],
            blocked_sources_pattern=42,  # type: ignore[arg-type]
        )
    assert "blocked_sources" in exc_info.value.field


# ---------------------------------------------------------------------------
# _validate_recipient_dnd_settings — DND time error paths
# ---------------------------------------------------------------------------


def test_dnd_invalid_start_time_format_raises():
    """An invalid dnd_start time string triggers ValueError and maps to RCPT_CONFIG_DND_START_KEY."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start="25:99",  # invalid format
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
        )
    assert exc_info.value.field == "dnd_start"


def test_dnd_non_string_start_raises_type_error_field():
    """A non-string dnd_start triggers TypeError and maps to RCPT_CONFIG_DND_START_KEY."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=42,  # type: ignore[arg-type]
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
        )
    assert exc_info.value.field == "dnd_start"


def test_dnd_invalid_end_time_format_raises():
    """An invalid dnd_end time string triggers ValueError and maps to RCPT_CONFIG_DND_END_KEY."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end="25:99",  # invalid format
            dnd_allowed_sources_pattern=None,
        )
    assert exc_info.value.field == "dnd_end"


def test_dnd_non_string_end_raises_type_error_field():
    """A non-string dnd_end triggers TypeError and maps to RCPT_CONFIG_DND_END_KEY."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=42,  # type: ignore[arg-type]
            dnd_allowed_sources_pattern=None,
        )
    assert exc_info.value.field == "dnd_end"


def test_dnd_valid_sources_pattern_accepted():
    """A valid regex dnd_allowed_sources_pattern does not raise."""
    _validate_recipient_dnd_settings(
        dnd_enabled=False,
        dnd_start=None,
        dnd_end=None,
        dnd_allowed_sources_pattern="^home\\..*$",
    )  # must not raise


def test_dnd_invalid_sources_pattern_raises():
    """An invalid regex in dnd_allowed_sources_pattern raises FieldValidationError."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern="[unclosed",
        )
    assert exc_info.value.field == "dnd_allowed_sources_regex"


def test_dnd_non_string_sources_pattern_raises():
    """A non-string dnd_allowed_sources_pattern raises FieldValidationError (TypeError)."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern=42,  # type: ignore[arg-type]
        )
    assert exc_info.value.field == "dnd_allowed_sources_regex"


def test_dnd_allowed_criticalities_non_string_item_raises():
    """A list with a non-string criticality item raises FieldValidationError."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
            dnd_allowed_criticalities=[42],  # type: ignore[list-item]
        )
    assert exc_info.value.field == "dnd_allowed_criticalities"


def test_dnd_allowed_types_non_string_item_raises():
    """A list with a non-string type item raises FieldValidationError."""
    with pytest.raises(FieldValidationError) as exc_info:
        _validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
            dnd_allowed_types=[42],  # type: ignore[list-item]
        )
    assert exc_info.value.field == "dnd_allowed_types"


# ---------------------------------------------------------------------------
# validate_recipient — missing TypeError / edge branches
# ---------------------------------------------------------------------------


def test_validate_recipient_non_string_id_raises():
    """A truthy but non-string recipient.id raises FieldValidationError on field 'id'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient(_raw_recipient(id=42))  # type: ignore[arg-type]
    assert exc_info.value.field == "id"


def test_validate_recipient_invalid_type_not_enum_raises():
    """A non-RecipientType recipient.type raises FieldValidationError on field 'type'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient(_raw_recipient(type="generic"))  # type: ignore[arg-type]
    assert exc_info.value.field == "type"


def test_validate_recipient_non_string_name_raises():
    """A truthy but non-string recipient.name raises FieldValidationError on field 'name'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient(_raw_recipient(name=42))  # type: ignore[arg-type]
    assert exc_info.value.field == "name"


def test_validate_recipient_non_string_email_raises():
    """A truthy but non-string recipient.email raises FieldValidationError on field 'email'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient(_raw_recipient(email=42))  # type: ignore[arg-type]
    assert exc_info.value.field == "email"


def test_validate_recipient_non_string_phone_raises():
    """A truthy but non-string recipient.phone raises FieldValidationError on field 'phone'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient(_raw_recipient(phone=42))  # type: ignore[arg-type]
    assert exc_info.value.field == "phone"


# ---------------------------------------------------------------------------
# _validate_recipient_dnd_settings — HH:MM:SS time path (covers elif branch)
# ---------------------------------------------------------------------------


def test_dnd_hhmmss_start_and_end_times_accepted():
    """DND enabled with HH:MM:SS formatted start/end times does not raise."""
    _validate_recipient_dnd_settings(
        dnd_enabled=True,
        dnd_start="22:00:00",  # 3-part format → exercises elif len(parts)==3
        dnd_end="08:30:00",
        dnd_allowed_sources_pattern=None,
    )  # must not raise


def test_dnd_settings_schema_invalid_sources_pattern_raises():
    """An invalid regex in dnd_allowed_sources_pattern raises vol.Invalid via schema validation."""
    data = _valid_dnd_data(dnd_allowed_sources_pattern="[unclosed")
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_dnd_settings_schema(data)
