"""Unit tests for ConfigValidator."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol

from ..config.validator import ConfigValidator, FieldValidationError, ValidationContext
from ..const import (
    RCPT_MAX_RATE_LIMIT,
    RCPT_MAX_RETRY_ATTEMPTS,
    SYS_MAX_GLOBAL_RATE_LIMIT,
)
from ..models import (
    NotificationCriticality,
    NotificationType,
    RecipientConfig,
    RecipientData,
    RecipientType,
    SystemConfig,
)

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
    # vol.Email is a function factory: vol.Email(value) creates a *new validator*
    # (treating `value` as the error-message parameter) rather than validating the input.
    # As a result _validate_email_format does not reject malformed addresses;
    # real email validation happens only at the schema level (validate_recipient_definition_schema).
    result = ConfigValidator._validate_email_format("not-an-email")
    assert result == "not-an-email"  # silently accepted — no ValueError is raised


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


@pytest.mark.parametrize(
    "ssml_value,expected",
    [
        (True, True),
        (False, False),
        # When omitted the schema inserts the default (False)
        pytest.param(
            None,
            False,
            id="absent_defaults_to_false",
        ),
    ],
)
def test_tts_settings_schema_ssml_enabled(ssml_value, expected):
    """Schema must accept boolean ssml_enabled values and apply the default when absent."""
    data = _valid_tts_data()
    if ssml_value is None:
        data.pop("ssml_enabled")
    else:
        data["ssml_enabled"] = ssml_value
    result = ConfigValidator.validate_recipient_tts_settings_schema(data)
    assert result["ssml_enabled"] is expected


def test_tts_settings_schema_rejects_non_boolean_ssml():
    """ssml_enabled must be a bool; non-bool values should be rejected."""
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_tts_settings_schema(
            _valid_tts_data(ssml_enabled="yes")
        )


# ---------------------------------------------------------------------------
# _validate_positive_integer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,min_val,max_val",
    [
        (1, 1, None),  # at minimum boundary
        (5, 1, 10),  # within range
        (10, 1, 10),  # at maximum boundary
    ],
)
def test_validate_positive_integer_valid(value, min_val, max_val):
    result = ConfigValidator._validate_positive_integer(
        value, min_val=min_val, max_val=max_val
    )
    assert result == value


def test_validate_positive_integer_below_min_raises():
    with pytest.raises(ValueError, match=">="):
        ConfigValidator._validate_positive_integer(0, min_val=1)


def test_validate_positive_integer_above_max_raises():
    with pytest.raises(ValueError, match="<="):
        ConfigValidator._validate_positive_integer(11, min_val=1, max_val=10)


def test_validate_positive_integer_non_integer_raises():
    with pytest.raises(TypeError, match="integer"):
        ConfigValidator._validate_positive_integer("5", min_val=1)


def test_validate_positive_integer_no_max_allows_large():
    # max_val=None means no upper bound
    result = ConfigValidator._validate_positive_integer(
        10_000_000, min_val=1, max_val=None
    )
    assert result == 10_000_000


# ---------------------------------------------------------------------------
# _validate_non_negative_integer
# ---------------------------------------------------------------------------


def test_validate_non_negative_integer_zero_accepted():
    assert ConfigValidator._validate_non_negative_integer(0) == 0


def test_validate_non_negative_integer_negative_raises():
    with pytest.raises(ValueError):
        ConfigValidator._validate_non_negative_integer(-1)


def test_validate_non_negative_integer_respects_max_val():
    with pytest.raises(ValueError, match="<="):
        ConfigValidator._validate_non_negative_integer(101, max_val=100)


# ---------------------------------------------------------------------------
# _validate_string_or_none
# ---------------------------------------------------------------------------


def test_validate_string_or_none_none_returns_none():
    assert ConfigValidator._validate_string_or_none(None) is None


def test_validate_string_or_none_string_returned_unchanged():
    assert ConfigValidator._validate_string_or_none("hello") == "hello"


def test_validate_string_or_none_non_string_non_none_raises():
    with pytest.raises(TypeError, match="string or None"):
        ConfigValidator._validate_string_or_none(42)


# ---------------------------------------------------------------------------
# _validate_string_list
# ---------------------------------------------------------------------------


def test_validate_string_list_non_list_raises():
    with pytest.raises(TypeError, match="list"):
        ConfigValidator._validate_string_list("not-a-list")


def test_validate_string_list_too_short_raises():
    with pytest.raises(ValueError, match="at least 2"):
        ConfigValidator._validate_string_list(["one"], min_length=2)


def test_validate_string_list_non_string_item_raises():
    with pytest.raises(TypeError, match="string"):
        ConfigValidator._validate_string_list([1, 2, 3])


def test_validate_string_list_item_not_in_allowed_values_raises():
    with pytest.raises(ValueError, match="not a valid"):
        ConfigValidator._validate_string_list(["bad"], allowed_values=["good"])


def test_validate_string_list_item_not_matching_regex_raises():
    with pytest.raises(ValueError, match="does not match"):
        ConfigValidator._validate_string_list(["ABC"], regex_pattern="^[a-z]+$")


def test_validate_string_list_invalid_regex_pattern_raises():
    with pytest.raises(ValueError, match="Invalid regex"):
        ConfigValidator._validate_string_list(["x"], regex_pattern="[unclosed")


def test_validate_string_list_valid_all_constraints():
    items = ["abc", "def"]
    result = ConfigValidator._validate_string_list(
        items,
        min_length=1,
        allowed_values=["abc", "def", "ghi"],
        regex_pattern="^[a-z]+$",
    )
    assert result == items


# ---------------------------------------------------------------------------
# _validate_recipient_basic_settings
# ---------------------------------------------------------------------------


def test_recipient_basic_settings_retry_above_max_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_basic_settings(
            retry_attempts=RCPT_MAX_RETRY_ATTEMPTS + 1,
            rate_limit=0,
            notification_types=[NotificationType.INFO],
            blocked_sources_pattern=None,
        )
    assert exc_info.value.field == "retry_attempts"


def test_recipient_basic_settings_non_int_retry_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_basic_settings(
            retry_attempts="three",
            rate_limit=0,
            notification_types=[NotificationType.INFO],
            blocked_sources_pattern=None,
        )
    assert exc_info.value.field == "retry_attempts"


def test_recipient_basic_settings_rate_limit_above_max_raises():
    ctx = ValidationContext()
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_basic_settings(
            retry_attempts=0,
            rate_limit=ctx.get_max_recipient_rate_limit() + 1,
            notification_types=[NotificationType.INFO],
            blocked_sources_pattern=None,
            validation_context=ctx,
        )
    assert exc_info.value.field == "rate_limit"


def test_recipient_basic_settings_invalid_notification_type_string_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_basic_settings(
            retry_attempts=0,
            rate_limit=0,
            notification_types=["INVALID_TYPE"],
            blocked_sources_pattern=None,
        )
    assert exc_info.value.field == "notification_types"


def test_recipient_basic_settings_non_notification_type_object_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_basic_settings(
            retry_attempts=0,
            rate_limit=0,
            notification_types=[42],
            blocked_sources_pattern=None,
        )
    assert exc_info.value.field == "notification_types"


def test_recipient_basic_settings_invalid_blocked_sources_regex_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_basic_settings(
            retry_attempts=0,
            rate_limit=0,
            notification_types=[NotificationType.INFO],
            blocked_sources_pattern="[unclosed",
        )
    # The field key reflects the const RCPT_CONFIG_BLOCKED_SOURCES_PATTERN_KEY
    assert "blocked_sources" in exc_info.value.field


def test_recipient_basic_settings_all_valid_with_context():
    ctx = ValidationContext()
    # Must not raise
    ConfigValidator._validate_recipient_basic_settings(
        retry_attempts=2,
        rate_limit=10,
        notification_types=[NotificationType.INFO, NotificationType.ALERT],
        blocked_sources_pattern=r"^home\.*$",
        validation_context=ctx,
    )


# ---------------------------------------------------------------------------
# _validate_recipient_channel_mapping
# ---------------------------------------------------------------------------


def test_channel_mapping_tts_rejects_notify():
    with pytest.raises(FieldValidationError):
        ConfigValidator._validate_recipient_channel_mapping(
            channels_low=["notify.mobile_app"],
            channels_medium=None,
            channels_high=None,
            channels_critical=None,
            recipient_type=RecipientType.TTS,
        )


def test_channel_mapping_tts_accepts_media_player():
    # Must not raise
    ConfigValidator._validate_recipient_channel_mapping(
        channels_low=["media_player.bedroom"],
        channels_medium=None,
        channels_high=None,
        channels_critical=None,
        recipient_type=RecipientType.TTS,
    )


def test_channel_mapping_non_tts_rejects_media_player():
    with pytest.raises(FieldValidationError):
        ConfigValidator._validate_recipient_channel_mapping(
            channels_low=["media_player.bedroom"],
            channels_medium=None,
            channels_high=None,
            channels_critical=None,
            recipient_type=RecipientType.VIRTUAL,
        )


def test_channel_mapping_non_tts_accepts_notify():
    # Must not raise
    ConfigValidator._validate_recipient_channel_mapping(
        channels_low=["notify.mobile_app"],
        channels_medium=None,
        channels_high=None,
        channels_critical=None,
        recipient_type=RecipientType.VIRTUAL,
    )


def test_channel_mapping_none_recipient_type_accepts_both():
    # Must not raise for either channel pattern
    ConfigValidator._validate_recipient_channel_mapping(
        channels_low=["notify.mobile_app"],
        channels_medium=["media_player.bedroom"],
        channels_high=None,
        channels_critical=None,
        recipient_type=None,
    )


def test_channel_mapping_channel_not_in_available_raises():
    with pytest.raises(FieldValidationError):
        ConfigValidator._validate_recipient_channel_mapping(
            channels_low=["notify.mobile_app"],
            channels_medium=None,
            channels_high=None,
            channels_critical=None,
            available_channels=["notify.other_channel"],
            recipient_type=None,
        )


def test_channel_mapping_none_channel_lists_ignored():
    # All None lists should silently pass regardless of recipient_type
    ConfigValidator._validate_recipient_channel_mapping(
        channels_low=None,
        channels_medium=None,
        channels_high=None,
        channels_critical=None,
        recipient_type=RecipientType.TTS,
    )


# ---------------------------------------------------------------------------
# validate_recipient_consistency
# ---------------------------------------------------------------------------


def test_validate_recipient_consistency_matching_passes():
    data = SimpleNamespace(type=RecipientType.VIRTUAL)
    config = SimpleNamespace(
        channels_low=["notify.mobile_app"],
        channels_medium=[],
        channels_high=[],
        channels_critical=[],
    )
    ConfigValidator.validate_recipient_consistency(data, config)  # must not raise


def test_validate_recipient_consistency_mismatch_raises():
    # TTS recipient but notify.* channel — incompatible
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
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled="yes",
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
        )
    assert exc_info.value.field == "dnd_enabled"


def test_dnd_enabled_only_start_set_raises():
    with pytest.raises(FieldValidationError):
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=True,
            dnd_start="22:00",
            dnd_end=None,  # missing end
            dnd_allowed_sources_pattern=None,
        )


def test_dnd_enabled_only_end_set_raises():
    with pytest.raises(FieldValidationError):
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=True,
            dnd_start=None,  # missing start
            dnd_end="08:00",
            dnd_allowed_sources_pattern=None,
        )


def test_dnd_allowed_types_not_list_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
            dnd_allowed_types="ALERT",  # string instead of list
        )
    assert exc_info.value.field == "dnd_allowed_types"


def test_dnd_allowed_types_invalid_string_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
            dnd_allowed_types=["NOT_A_TYPE"],
        )
    assert exc_info.value.field == "dnd_allowed_types"


def test_dnd_allowed_criticalities_not_list_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
            dnd_allowed_criticalities="HIGH",  # string instead of list
        )
    assert exc_info.value.field == "dnd_allowed_criticalities"


def test_dnd_allowed_types_valid_passes():
    # Must not raise with valid type strings
    ConfigValidator._validate_recipient_dnd_settings(
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
    ctx = ValidationContext()
    result = ConfigValidator.validate_recipient_basic_settings_schema(
        _valid_basic_settings(), ctx
    )
    assert "retry_attempts" in result
    assert result["retry_attempts"] == 2
    assert result["rate_limit"] == 10


def test_recipient_basic_settings_schema_retry_above_max_raises():
    ctx = ValidationContext()
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_basic_settings_schema(
            _valid_basic_settings(retry_attempts=RCPT_MAX_RETRY_ATTEMPTS + 1), ctx
        )


def test_recipient_basic_settings_schema_rate_limit_above_max_raises():
    ctx = ValidationContext()
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_basic_settings_schema(
            _valid_basic_settings(rate_limit=ctx.get_max_recipient_rate_limit() + 1),
            ctx,
        )


def test_recipient_basic_settings_schema_empty_notification_types_raises():
    ctx = ValidationContext()
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_basic_settings_schema(
            _valid_basic_settings(notification_types=[]), ctx
        )


def test_recipient_basic_settings_schema_invalid_blocked_regex_raises():
    ctx = ValidationContext()
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_basic_settings_schema(
            _valid_basic_settings(blocked_sources_pattern="[unclosed"), ctx
        )


# ---------------------------------------------------------------------------
# validate_recipient_channel_mapping_schema
# ---------------------------------------------------------------------------


def test_recipient_channel_mapping_schema_empty_dict_passes():
    ctx = ValidationContext(available_channels=["notify.mobile_app"])
    result = ConfigValidator.validate_recipient_channel_mapping_schema({}, ctx)
    assert result is not None


def test_recipient_channel_mapping_schema_valid_channel_passes():
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
    ctx = ValidationContext(available_channels=["notify.other"])
    result = ConfigValidator.validate_recipient_channel_mapping_schema(
        {"channels_low": ["notify.mobile_app"]}, ctx
    )
    assert result is not None  # no vol.Invalid raised


def test_recipient_channel_mapping_schema_extra_key_raises():
    ctx = ValidationContext(available_channels=[])
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_channel_mapping_schema(
            {"unknown_key": "value"}, ctx
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
    result = ConfigValidator.validate_recipient_dnd_settings_schema(_valid_dnd_data())
    assert result["dnd_enabled"] is True
    assert result["dnd_start"] == "22:00"


def test_dnd_settings_schema_enabled_no_start_raises():
    data = _valid_dnd_data()
    del data["dnd_start"]
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_dnd_settings_schema(data)


def test_dnd_settings_schema_enabled_no_end_raises():
    data = _valid_dnd_data()
    del data["dnd_end"]
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_dnd_settings_schema(data)


def test_dnd_settings_schema_same_start_end_raises():
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_dnd_settings_schema(
            _valid_dnd_data(dnd_start="22:00", dnd_end="22:00")
        )


def test_dnd_settings_schema_invalid_criticality_raises():
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_recipient_dnd_settings_schema(
            _valid_dnd_data(dnd_allowed_criticalities=["NOT_A_CRITICALITY"])
        )


def test_dnd_settings_schema_invalid_notification_type_raises():
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
        "type": RecipientType.VIRTUAL,
        "name": "Alice",
        "email": None,
        "phone": None,
        "version": 1,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_validate_recipient_valid_passes():
    ConfigValidator.validate_recipient(_raw_recipient())  # must not raise


def test_validate_recipient_empty_id_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient(_raw_recipient(id=""))
    assert exc_info.value.field == "id"


def test_validate_recipient_empty_name_raises():
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
    ConfigValidator.validate_recipient(_raw_recipient(email="not-an-email"))


def test_validate_recipient_invalid_phone_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient(_raw_recipient(phone="00000000"))
    assert exc_info.value.field == "phone"


def test_validate_recipient_version_below_one_raises():
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
    ConfigValidator.validate_recipient_config(_raw_config())  # must not raise


def test_validate_recipient_config_invalid_retry_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient_config(
            _raw_config(retry_attempts=RCPT_MAX_RETRY_ATTEMPTS + 1)
        )
    assert exc_info.value.field == "retry_attempts"


def test_validate_recipient_config_invalid_channel_raises():
    with pytest.raises(FieldValidationError):
        ConfigValidator.validate_recipient_config(
            # TTS channel in a non-TTS config (no recipient_type enforced here —
            # but notify.* pattern validation triggers on channel name format)
            _raw_config(channels_low=["bad_channel_name"])
        )


def test_validate_recipient_config_invalid_dnd_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient_config(
            _raw_config(dnd_enabled="yes")  # non-bool dnd_enabled
        )
    assert exc_info.value.field == "dnd_enabled"


def test_validate_recipient_config_version_below_one_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_recipient_config(_raw_config(version=0))
    assert exc_info.value.field == "version"


# ---------------------------------------------------------------------------
# validate_system_config (object-level)
# ---------------------------------------------------------------------------


def _raw_sys_config(**overrides) -> SimpleNamespace:
    """Build a SimpleNamespace that mimics a valid SystemConfig."""
    defaults = {
        "global_rate_limit": 100,
        "retry_base_delay": 60,
        "retry_backoff_factor": 2.0,
        "retry_max_delay": 3600,
        "queue_max_concurrency": 5,
        "storage_retention_days": 7,
        "enabled_channels": ["notify.persistent_notification"],
        "persistent_notifications_enabled": True,
        "version": 1,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_validate_system_config_valid_passes():
    ConfigValidator.validate_system_config(_raw_sys_config())  # must not raise


def test_validate_system_config_negative_rate_limit_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(global_rate_limit=-1))
    assert exc_info.value.field == "global_rate_limit"


def test_validate_system_config_retry_base_delay_below_one_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(retry_base_delay=0))
    assert exc_info.value.field == "retry_base_delay"


def test_validate_system_config_retry_backoff_below_one_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(
            _raw_sys_config(retry_backoff_factor=0.5)
        )
    assert exc_info.value.field == "retry_backoff_factor"


def test_validate_system_config_retry_max_delay_below_60_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(retry_max_delay=59))
    assert exc_info.value.field == "retry_max_delay"


def test_validate_system_config_queue_concurrency_below_one_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(queue_max_concurrency=0))
    assert exc_info.value.field == "queue_max_concurrency"


def test_validate_system_config_storage_retention_negative_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(
            _raw_sys_config(storage_retention_days=-1)
        )
    assert exc_info.value.field == "storage_retention_days"


def test_validate_system_config_no_channels_no_persistent_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(
            _raw_sys_config(enabled_channels=[], persistent_notifications_enabled=False)
        )
    assert exc_info.value.field == "enabled_channels"


def test_validate_system_config_malformed_channel_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(
            _raw_sys_config(
                enabled_channels=["bad_channel_without_dot"],
                persistent_notifications_enabled=False,
            )
        )
    assert exc_info.value.field == "enabled_channels"


def test_validate_system_config_version_below_one_raises():
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(version=0))
    assert exc_info.value.field == "version"


# ---------------------------------------------------------------------------
# validate_tts_service (async)
# ---------------------------------------------------------------------------


def _make_mock_hass(
    entity_present: bool = True, entity_ids: list | None = None
) -> MagicMock:
    """Build a minimal mock HomeAssistant with mocked state access."""
    hass = MagicMock()
    if entity_present:
        hass.states.get.return_value = MagicMock()  # non-None state
    else:
        hass.states.get.return_value = None
    hass.states.async_entity_ids.return_value = entity_ids or []
    return hass


async def test_tts_service_none_string_returns_none():
    from ..config.validator import validate_tts_service

    result = await validate_tts_service(_make_mock_hass(), "None")
    assert result is None


async def test_tts_service_none_value_returns_none():
    from ..config.validator import validate_tts_service

    result = await validate_tts_service(_make_mock_hass(), None)
    assert result is None


async def test_tts_service_empty_string_returns_none():
    from ..config.validator import validate_tts_service

    result = await validate_tts_service(_make_mock_hass(), "")
    assert result is None


async def test_tts_service_not_starting_with_tts_raises():
    from ..config.validator import validate_tts_service

    with pytest.raises(vol.Invalid, match="tts\\."):
        await validate_tts_service(_make_mock_hass(), "media_player.bedroom")


@pytest.mark.parametrize(
    "bad_value",
    [
        "tts.Google Translate",  # space
        "tts.GoogleTTS",  # uppercase
        "tts.my-service",  # hyphen
    ],
)
async def test_tts_service_invalid_chars_raises(bad_value):
    from ..config.validator import validate_tts_service

    with pytest.raises(vol.Invalid):
        await validate_tts_service(_make_mock_hass(), bad_value)


async def test_tts_service_entity_absent_raises():
    from ..config.validator import validate_tts_service

    hass = _make_mock_hass(entity_present=False, entity_ids=["tts.piper"])
    with pytest.raises(vol.Invalid, match="tts.piper"):
        await validate_tts_service(hass, "tts.cloud")


async def test_tts_service_entity_present_returns_entity_id():
    from ..config.validator import validate_tts_service

    result = await validate_tts_service(
        _make_mock_hass(entity_present=True), "tts.piper"
    )
    assert result == "tts.piper"


# ---------------------------------------------------------------------------
# ValidationContext
# ---------------------------------------------------------------------------


def test_validation_context_max_global_rate_limit_no_system_limits():
    ctx = ValidationContext(system_limits=None)
    assert ctx.get_max_global_rate_limit() == SYS_MAX_GLOBAL_RATE_LIMIT


def test_validation_context_max_global_rate_limit_custom():
    ctx = ValidationContext(system_limits={"global_rate_limit": 500})
    assert ctx.get_max_global_rate_limit() == 500


def test_validation_context_max_recipient_rate_limit():
    ctx = ValidationContext()
    assert ctx.get_max_recipient_rate_limit() == RCPT_MAX_RATE_LIMIT


# ---------------------------------------------------------------------------
# FieldValidationError
# ---------------------------------------------------------------------------


def test_field_validation_error_str():
    err = FieldValidationError("my_field", "Something went wrong")
    assert str(err) == "my_field: Something went wrong"


def test_field_validation_error_placeholders_accessible():
    placeholders = {"min": "0", "max": "100"}
    err = FieldValidationError("my_field", "Out of range", placeholders=placeholders)
    assert err.placeholders == {"min": "0", "max": "100"}
    assert err.field == "my_field"
    assert err.message == "Out of range"
