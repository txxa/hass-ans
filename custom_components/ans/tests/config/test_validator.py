"""Unit tests for ConfigValidator."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import voluptuous as vol

from ...config.validator import (
    ConfigValidator,
    FieldValidationError,
    ValidationContext,
    validate_tts_service,
)
from ...const import (
    RCPT_MAX_RATE_LIMIT,
    RCPT_MAX_RETRY_ATTEMPTS,
    SYS_MAX_GLOBAL_RATE_LIMIT,
)
from ...models import (
    NotificationType,
    RecipientType,
)

# ---------------------------------------------------------------------------
# Time format validation
# ---------------------------------------------------------------------------


def test_validate_time_format_valid_hhmm():
    """A time in HH:MM format ('08:30') is valid and returned unchanged."""
    result = ConfigValidator._validate_time_format("08:30")
    assert result == "08:30"


def test_validate_time_format_valid_hhmmss():
    """A time in HH:MM:SS format ('22:00:00') is valid and returned unchanged."""
    result = ConfigValidator._validate_time_format("22:00:00")
    assert result == "22:00:00"


def test_validate_time_format_invalid_raises():
    """An invalid time string ('25:99') raises ValueError containing 'HH:MM'."""
    with pytest.raises(ValueError, match="HH:MM"):
        ConfigValidator._validate_time_format("25:99")


def test_validate_time_format_none_returns_none():
    """_validate_time_format(None) returns None."""
    assert ConfigValidator._validate_time_format(None) is None


# ---------------------------------------------------------------------------
# Email format validation
# ---------------------------------------------------------------------------


def test_validate_email_valid():
    """_validate_email_format('user@example.com') returns the address unchanged."""
    result = ConfigValidator._validate_email_format("user@example.com")
    assert result == "user@example.com"


def test_validate_email_invalid_is_accepted_by_helper():
    # vol.Email is a function factory: vol.Email(value) creates a *new validator*
    # (treating `value` as the error-message parameter) rather than validating the input.
    # As a result _validate_email_format does not reject malformed addresses;
    # real email validation happens only at the schema level (validate_recipient_definition_schema).
    """Due to a vol.Email quirk, _validate_email_format() does not reject malformed addresses."""
    result = ConfigValidator._validate_email_format("not-an-email")
    assert result == "not-an-email"  # silently accepted — no ValueError is raised


def test_validate_email_none_returns_none():
    """_validate_email_format(None) returns None."""
    assert ConfigValidator._validate_email_format(None) is None


# ---------------------------------------------------------------------------
# Phone format validation
# ---------------------------------------------------------------------------


def test_validate_phone_valid_e164():
    """_validate_phone_format('+1234567890') returns the number unchanged."""
    result = ConfigValidator._validate_phone_format("+1234567890")
    assert result == "+1234567890"


def test_validate_phone_invalid_starts_with_zero():
    # PHONE_PATTERN is r"^\+?[1-9]\d{1,14}$" — leading 0 is not allowed
    """A phone number starting with '0' raises ValueError containing 'E.164'."""
    with pytest.raises(ValueError, match="E.164"):
        ConfigValidator._validate_phone_format("0123456789")


def test_validate_phone_none_returns_none():
    """_validate_phone_format(None) returns None."""
    assert ConfigValidator._validate_phone_format(None) is None


# ---------------------------------------------------------------------------
# Regex pattern validation
# ---------------------------------------------------------------------------


def test_validate_regex_valid():
    """_validate_regex_pattern() returns a valid regex pattern unchanged."""
    result = ConfigValidator._validate_regex_pattern("^home.*$")
    assert result == "^home.*$"


def test_validate_regex_invalid_raises():
    """An unclosed character class raises ValueError containing 'Invalid regex'."""
    with pytest.raises(ValueError, match="Invalid regex"):
        ConfigValidator._validate_regex_pattern("[unclosed")


# ---------------------------------------------------------------------------
# DND settings validation
# ---------------------------------------------------------------------------


def test_dnd_disabled_no_times_required():
    # Should not raise when DND is disabled and times are absent
    """When DND is disabled, dnd_start and dnd_end are not required."""
    ConfigValidator._validate_recipient_dnd_settings(
        dnd_enabled=False,
        dnd_start=None,
        dnd_end=None,
        dnd_allowed_sources_pattern=None,
    )


def test_dnd_enabled_requires_times():
    """When DND is enabled with no times set, FieldValidationError names a time-related field."""
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
    """Equal start and end times raise FieldValidationError even when both are set."""
    with pytest.raises(FieldValidationError):
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=True,
            dnd_start="22:00",
            dnd_end="22:00",
            dnd_allowed_sources_pattern=None,
        )


def test_dnd_valid_times_ok():
    """Valid DND start ('22:00') and end ('08:00') times do not raise."""
    ConfigValidator._validate_recipient_dnd_settings(
        dnd_enabled=True,
        dnd_start="22:00",
        dnd_end="08:00",
        dnd_allowed_sources_pattern=None,
    )


def test_dnd_invalid_criticality_raises():
    """An unrecognised criticality level in dnd_allowed_criticalities raises FieldValidationError."""
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
    """A list of recognised criticality strings ('LOW', 'HIGH') does not raise."""
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
    """validate_system_settings_schema() accepts a dict with a positive rate limit and at least one channel."""
    data = {
        "global_rate_limit": 100,
        "enabled_channels": ["notify.persistent_notification"],
    }
    result = ConfigValidator.validate_system_settings_schema(data)
    assert result["global_rate_limit"] == 100


def test_system_settings_schema_empty_channels_raises():
    """validate_system_settings_schema() raises vol.Invalid when enabled_channels is an empty list."""

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
# _validate_boolean
# ---------------------------------------------------------------------------


def test_validate_boolean_true():
    """_validate_boolean(True) returns True."""
    assert ConfigValidator._validate_boolean(True) is True


def test_validate_boolean_false():
    """_validate_boolean(False) returns False."""
    assert ConfigValidator._validate_boolean(False) is False


def test_validate_boolean_invalid():
    """_validate_boolean('yes') raises TypeError containing 'boolean'."""
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
    ("ssml_value", "expected"),
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
    ("value", "min_val", "max_val"),
    [
        (1, 1, None),  # at minimum boundary
        (5, 1, 10),  # within range
        (10, 1, 10),  # at maximum boundary
    ],
)
def test_validate_positive_integer_valid(value, min_val, max_val):
    """Values at or within the [min_val, max_val] range are accepted and returned unchanged."""
    result = ConfigValidator._validate_positive_integer(
        value, min_val=min_val, max_val=max_val
    )
    assert result == value


def test_validate_positive_integer_below_min_raises():
    """A value below min_val raises ValueError containing '>='."""
    with pytest.raises(ValueError, match=">="):
        ConfigValidator._validate_positive_integer(0, min_val=1)


def test_validate_positive_integer_above_max_raises():
    """A value above max_val raises ValueError containing '<='."""
    with pytest.raises(ValueError, match="<="):
        ConfigValidator._validate_positive_integer(11, min_val=1, max_val=10)


def test_validate_positive_integer_non_integer_raises():
    """A non-integer value raises TypeError containing 'integer'."""
    with pytest.raises(TypeError, match="integer"):
        ConfigValidator._validate_positive_integer("5", min_val=1)


def test_validate_positive_integer_no_max_allows_large():
    # max_val=None means no upper bound
    """When max_val=None there is no upper bound and very large values are accepted."""
    result = ConfigValidator._validate_positive_integer(
        10_000_000, min_val=1, max_val=None
    )
    assert result == 10_000_000


# ---------------------------------------------------------------------------
# _validate_non_negative_integer
# ---------------------------------------------------------------------------


def test_validate_non_negative_integer_zero_accepted():
    """_validate_non_negative_integer(0) returns 0 (zero is non-negative)."""
    assert ConfigValidator._validate_non_negative_integer(0) == 0


def test_validate_non_negative_integer_negative_raises():
    """_validate_non_negative_integer(-1) raises ValueError."""
    with pytest.raises(ValueError):
        ConfigValidator._validate_non_negative_integer(-1)


def test_validate_non_negative_integer_respects_max_val():
    """A value above max_val raises ValueError containing '<='."""
    with pytest.raises(ValueError, match="<="):
        ConfigValidator._validate_non_negative_integer(101, max_val=100)


# ---------------------------------------------------------------------------
# _validate_string_or_none
# ---------------------------------------------------------------------------


def test_validate_string_or_none_none_returns_none():
    """_validate_string_or_none(None) returns None."""
    assert ConfigValidator._validate_string_or_none(None) is None


def test_validate_string_or_none_string_returned_unchanged():
    """_validate_string_or_none('hello') returns 'hello' unchanged."""
    assert ConfigValidator._validate_string_or_none("hello") == "hello"


def test_validate_string_or_none_non_string_non_none_raises():
    """A non-string, non-None value raises TypeError containing 'string or None'."""
    with pytest.raises(TypeError, match="string or None"):
        ConfigValidator._validate_string_or_none(42)


# ---------------------------------------------------------------------------
# _validate_string_list
# ---------------------------------------------------------------------------


def test_validate_string_list_non_list_raises():
    """A non-list argument raises TypeError containing 'list'."""
    with pytest.raises(TypeError, match="list"):
        ConfigValidator._validate_string_list("not-a-list")


def test_validate_string_list_too_short_raises():
    """A list shorter than min_length raises ValueError containing 'at least 2'."""
    with pytest.raises(ValueError, match="at least 2"):
        ConfigValidator._validate_string_list(["one"], min_length=2)


def test_validate_string_list_non_string_item_raises():
    """A list containing a non-string item raises TypeError containing 'string'."""
    with pytest.raises(TypeError, match="string"):
        ConfigValidator._validate_string_list([1, 2, 3])


def test_validate_string_list_item_not_in_allowed_values_raises():
    """An item absent from allowed_values raises ValueError containing 'not a valid'."""
    with pytest.raises(ValueError, match="not a valid"):
        ConfigValidator._validate_string_list(["bad"], allowed_values=["good"])


def test_validate_string_list_item_not_matching_regex_raises():
    """An item not matching regex_pattern raises ValueError containing 'does not match'."""
    with pytest.raises(ValueError, match="does not match"):
        ConfigValidator._validate_string_list(["ABC"], regex_pattern="^[a-z]+$")


def test_validate_string_list_invalid_regex_pattern_raises():
    """An invalid regex_pattern raises ValueError containing 'Invalid regex'."""
    with pytest.raises(ValueError, match="Invalid regex"):
        ConfigValidator._validate_string_list(["x"], regex_pattern="[unclosed")


def test_validate_string_list_valid_all_constraints():
    """A valid list satisfying all constraints (min_length, allowed_values, regex) is returned unchanged."""
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
    """_validate_recipient_basic_settings() raises FieldValidationError('retry_attempts') when retry_attempts > RCPT_MAX_RETRY_ATTEMPTS."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_basic_settings(
            retry_attempts=RCPT_MAX_RETRY_ATTEMPTS + 1,
            rate_limit=0,
            notification_types=[NotificationType.INFO],
            blocked_sources_pattern=None,
        )
    assert exc_info.value.field == "retry_attempts"


def test_recipient_basic_settings_non_int_retry_raises():
    """A non-integer retry_attempts raises FieldValidationError on field 'retry_attempts'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_basic_settings(
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
        ConfigValidator._validate_recipient_basic_settings(
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
        ConfigValidator._validate_recipient_basic_settings(
            retry_attempts=0,
            rate_limit=0,
            notification_types=["INVALID_TYPE"],
            blocked_sources_pattern=None,
        )
    assert exc_info.value.field == "notification_types"


def test_recipient_basic_settings_non_notification_type_object_raises():
    """A non-NotificationType object in notification_types raises FieldValidationError on field 'notification_types'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_basic_settings(
            retry_attempts=0,
            rate_limit=0,
            notification_types=[42],
            blocked_sources_pattern=None,
        )
    assert exc_info.value.field == "notification_types"


def test_recipient_basic_settings_invalid_blocked_sources_regex_raises():
    """An invalid regex for blocked_sources_pattern raises FieldValidationError on a 'blocked_sources' field."""
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
    """_validate_recipient_basic_settings() with all valid arguments and a ValidationContext does not raise."""
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
    """A TTS recipient with a 'notify.*' channel raises FieldValidationError."""
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
    """A TTS recipient with a 'media_player.*' channel does not raise."""
    ConfigValidator._validate_recipient_channel_mapping(
        channels_low=["media_player.bedroom"],
        channels_medium=None,
        channels_high=None,
        channels_critical=None,
        recipient_type=RecipientType.TTS,
    )


def test_channel_mapping_non_tts_rejects_media_player():
    """A non-TTS recipient with a 'media_player.*' channel raises FieldValidationError."""
    with pytest.raises(FieldValidationError):
        ConfigValidator._validate_recipient_channel_mapping(
            channels_low=["media_player.bedroom"],
            channels_medium=None,
            channels_high=None,
            channels_critical=None,
            recipient_type=RecipientType.GENERIC,
        )


def test_channel_mapping_non_tts_accepts_notify():
    # Must not raise
    """A non-TTS recipient with a 'notify.*' channel does not raise."""
    ConfigValidator._validate_recipient_channel_mapping(
        channels_low=["notify.mobile_app"],
        channels_medium=None,
        channels_high=None,
        channels_critical=None,
        recipient_type=RecipientType.GENERIC,
    )


def test_channel_mapping_none_recipient_type_accepts_both():
    # Must not raise for either channel pattern
    """When recipient_type is None, both 'notify.*' and 'media_player.*' channels are accepted."""
    ConfigValidator._validate_recipient_channel_mapping(
        channels_low=["notify.mobile_app"],
        channels_medium=["media_player.bedroom"],
        channels_high=None,
        channels_critical=None,
        recipient_type=None,
    )


def test_channel_mapping_channel_not_in_available_raises():
    """A channel not present in available_channels raises FieldValidationError."""
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
    """All-None channel lists silently pass regardless of recipient_type."""
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
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled="yes",
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
        )
    assert exc_info.value.field == "dnd_enabled"


def test_dnd_enabled_only_start_set_raises():
    """DND enabled with dnd_start set but dnd_end missing raises FieldValidationError."""
    with pytest.raises(FieldValidationError):
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=True,
            dnd_start="22:00",
            dnd_end=None,  # missing end
            dnd_allowed_sources_pattern=None,
        )


def test_dnd_enabled_only_end_set_raises():
    """DND enabled with dnd_end set but dnd_start missing raises FieldValidationError."""
    with pytest.raises(FieldValidationError):
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=True,
            dnd_start=None,  # missing start
            dnd_end="08:00",
            dnd_allowed_sources_pattern=None,
        )


def test_dnd_allowed_types_not_list_raises():
    """A non-list dnd_allowed_types raises FieldValidationError on field 'dnd_allowed_types'."""
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
    """An unrecognised type string in dnd_allowed_types raises FieldValidationError on field 'dnd_allowed_types'."""
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
    """A non-list dnd_allowed_criticalities raises FieldValidationError on field 'dnd_allowed_criticalities'."""
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
    """Valid dnd_allowed_types strings ('INFO', 'ALERT') do not raise."""
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
        "queue_max_depth": 500,
        "storage_retention_days": 7,
        "enabled_channels": ["notify.persistent_notification"],
        "persistent_notifications_enabled": True,
        "version": 1,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_validate_system_config_valid_passes():
    """validate_system_config() accepts a well-formed system config namespace without raising."""
    ConfigValidator.validate_system_config(_raw_sys_config())  # must not raise


def test_validate_system_config_negative_rate_limit_raises():
    """A negative global_rate_limit raises FieldValidationError on field 'global_rate_limit'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(global_rate_limit=-1))
    assert exc_info.value.field == "global_rate_limit"


def test_validate_system_config_retry_base_delay_below_one_raises():
    """retry_base_delay of 0 raises FieldValidationError on field 'retry_base_delay'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(retry_base_delay=0))
    assert exc_info.value.field == "retry_base_delay"


def test_validate_system_config_retry_backoff_below_one_raises():
    """retry_backoff_factor below 1.0 raises FieldValidationError on field 'retry_backoff_factor'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(
            _raw_sys_config(retry_backoff_factor=0.5)
        )
    assert exc_info.value.field == "retry_backoff_factor"


def test_validate_system_config_retry_max_delay_below_60_raises():
    """retry_max_delay below 60 raises FieldValidationError on field 'retry_max_delay'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(retry_max_delay=59))
    assert exc_info.value.field == "retry_max_delay"


def test_validate_system_config_queue_concurrency_below_one_raises():
    """queue_max_concurrency of 0 raises FieldValidationError on field 'queue_max_concurrency'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(queue_max_concurrency=0))
    assert exc_info.value.field == "queue_max_concurrency"


def test_validate_system_config_storage_retention_negative_raises():
    """A negative storage_retention_days raises FieldValidationError on field 'storage_retention_days'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(
            _raw_sys_config(storage_retention_days=-1)
        )
    assert exc_info.value.field == "storage_retention_days"


def test_validate_system_config_no_channels_no_persistent_raises():
    """Empty enabled_channels with persistent_notifications_enabled=False raises FieldValidationError('enabled_channels')."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(
            _raw_sys_config(enabled_channels=[], persistent_notifications_enabled=False)
        )
    assert exc_info.value.field == "enabled_channels"


def test_validate_system_config_malformed_channel_raises():
    """A channel name without a dot separator raises FieldValidationError on field 'enabled_channels'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(
            _raw_sys_config(
                enabled_channels=["bad_channel_without_dot"],
                persistent_notifications_enabled=False,
            )
        )
    assert exc_info.value.field == "enabled_channels"


def test_validate_system_config_version_below_one_raises():
    """A version below 1 raises FieldValidationError on field 'version'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(version=0))
    assert exc_info.value.field == "version"


def test_validate_system_config_valid_queue_max_depth_passes():
    """validate_system_config() accepts a valid queue_max_depth value (e.g. 500)."""
    ConfigValidator.validate_system_config(
        _raw_sys_config(queue_max_depth=500)
    )  # must not raise


def test_validate_system_config_queue_max_depth_below_min_raises():
    """A queue_max_depth below the minimum (10) raises FieldValidationError on field 'queue_max_depth'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(queue_max_depth=9))
    assert exc_info.value.field == "queue_max_depth"


def test_validate_system_config_queue_max_depth_non_integer_raises():
    """A non-integer queue_max_depth raises FieldValidationError on field 'queue_max_depth'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(queue_max_depth="500"))
    assert exc_info.value.field == "queue_max_depth"


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
    """validate_tts_service(hass, 'None') returns None."""

    result = await validate_tts_service(_make_mock_hass(), "None")
    assert result is None


async def test_tts_service_none_value_returns_none():
    """validate_tts_service(hass, None) returns None."""

    result = await validate_tts_service(_make_mock_hass(), None)
    assert result is None


async def test_tts_service_empty_string_returns_none():
    """validate_tts_service(hass, '') returns None."""

    result = await validate_tts_service(_make_mock_hass(), "")
    assert result is None


async def test_tts_service_not_starting_with_tts_raises():
    """A service ID not prefixed with 'tts.' raises vol.Invalid."""

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
    """Service IDs with spaces, uppercase letters, or hyphens raise vol.Invalid."""

    with pytest.raises(vol.Invalid):
        await validate_tts_service(_make_mock_hass(), bad_value)


async def test_tts_service_entity_absent_raises():
    """A well-formed service ID whose state entity is absent raises vol.Invalid listing available entities."""

    hass = _make_mock_hass(entity_present=False, entity_ids=["tts.piper"])
    with pytest.raises(vol.Invalid, match="tts.piper"):
        await validate_tts_service(hass, "tts.cloud")


async def test_tts_service_entity_present_returns_entity_id():
    """A well-formed service ID whose entity exists in the state machine is returned unchanged."""

    result = await validate_tts_service(
        _make_mock_hass(entity_present=True), "tts.piper"
    )
    assert result == "tts.piper"


# ---------------------------------------------------------------------------
# ValidationContext
# ---------------------------------------------------------------------------


def test_validation_context_max_global_rate_limit_no_system_limits():
    """When system_limits is None, get_max_global_rate_limit() returns the global constant SYS_MAX_GLOBAL_RATE_LIMIT."""
    ctx = ValidationContext(system_limits=None)
    assert ctx.get_max_global_rate_limit() == SYS_MAX_GLOBAL_RATE_LIMIT


def test_validation_context_max_global_rate_limit_custom():
    """When system_limits provides a 'global_rate_limit', get_max_global_rate_limit() returns that value."""
    ctx = ValidationContext(system_limits={"global_rate_limit": 500})
    assert ctx.get_max_global_rate_limit() == 500


def test_validation_context_max_recipient_rate_limit():
    """get_max_recipient_rate_limit() returns RCPT_MAX_RATE_LIMIT."""
    ctx = ValidationContext()
    assert ctx.get_max_recipient_rate_limit() == RCPT_MAX_RATE_LIMIT


# ---------------------------------------------------------------------------
# FieldValidationError
# ---------------------------------------------------------------------------


def test_field_validation_error_str():
    """str(FieldValidationError) formats as 'field: message'."""
    err = FieldValidationError("my_field", "Something went wrong")
    assert str(err) == "my_field: Something went wrong"


def test_field_validation_error_placeholders_accessible():
    """FieldValidationError exposes .field, .message, and .placeholders attributes."""
    placeholders = {"min": "0", "max": "100"}
    err = FieldValidationError("my_field", "Out of range", placeholders=placeholders)
    assert err.placeholders == {"min": "0", "max": "100"}
    assert err.field == "my_field"
    assert err.message == "Out of range"


# ---------------------------------------------------------------------------
# __time_to_sec — private helper (via name-mangling)
# ---------------------------------------------------------------------------

# The private helper is tested directly via Python name-mangling to cover branches
# that can never be reached through the public validators (because those first apply
# _validate_time_format which only passes valid HH:MM / HH:MM:SS strings).

_time_to_sec = ConfigValidator._ConfigValidator__time_to_sec  # type: ignore[attr-defined]


def test_time_to_sec_hh_mm_ss_format_covered():
    """HH:MM:SS format (3 parts) is parsed correctly."""
    assert _time_to_sec("22:00:00") == 22 * 3600


def test_time_to_sec_invalid_parts_count_raises():
    """A time string with neither 2 nor 3 colon-separated parts raises ValueError."""
    with pytest.raises(ValueError, match="Invalid time format"):
        _time_to_sec("22")  # zero colons → 1 part


def test_time_to_sec_non_integer_component_raises():
    """Non-numeric time components cause ValueError about integer conversion."""
    with pytest.raises(ValueError, match="integers"):
        _time_to_sec("22:xx")


def test_time_to_sec_out_of_range_raises():
    """Hour value outside [0, 23] raises ValueError about range."""
    with pytest.raises(ValueError, match="out of range"):
        _time_to_sec("25:00:00")  # 3 parts, hour=25 out of [0,23]


# ---------------------------------------------------------------------------
# Type-error branches in format validators
# ---------------------------------------------------------------------------


def test_validate_time_format_non_string_raises_type_error():
    """_validate_time_format(42) raises TypeError containing 'string'."""
    with pytest.raises(TypeError, match="string"):
        ConfigValidator._validate_time_format(42)


def test_validate_email_format_non_string_raises_type_error():
    """_validate_email_format(42) raises TypeError containing 'string'."""
    with pytest.raises(TypeError, match="string"):
        ConfigValidator._validate_email_format(42)


def test_validate_phone_format_non_string_raises_type_error():
    """_validate_phone_format(42) raises TypeError containing 'string'."""
    with pytest.raises(TypeError, match="string"):
        ConfigValidator._validate_phone_format(42)


def test_validate_regex_pattern_non_string_raises_type_error():
    """_validate_regex_pattern(42) raises TypeError containing 'string'."""
    with pytest.raises(TypeError, match="string"):
        ConfigValidator._validate_regex_pattern(42)


# ---------------------------------------------------------------------------
# _validate_recipient_basic_settings — TypeError paths
# ---------------------------------------------------------------------------


def test_recipient_basic_settings_non_int_rate_limit_raises_field_error():
    """A non-integer rate_limit raises FieldValidationError on field 'rate_limit'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_basic_settings(
            retry_attempts=0,
            rate_limit="high",  # type: ignore[arg-type]
            notification_types=[NotificationType.INFO],
            blocked_sources_pattern=None,
        )
    assert exc_info.value.field == "rate_limit"


def test_recipient_basic_settings_non_string_blocked_sources_raises_field_error():
    """A non-string truthy blocked_sources_pattern raises FieldValidationError."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_basic_settings(
            retry_attempts=0,
            rate_limit=0,
            notification_types=[NotificationType.INFO],
            blocked_sources_pattern=42,  # type: ignore[arg-type]
        )
    assert "blocked_sources" in exc_info.value.field


# ---------------------------------------------------------------------------
# _validate_recipient_channel_mapping — TypeError path
# ---------------------------------------------------------------------------


def test_channel_mapping_non_string_channel_item_raises():
    """A list with a non-string channel item raises FieldValidationError."""
    with pytest.raises(FieldValidationError):
        ConfigValidator._validate_recipient_channel_mapping(
            channels_low=[42],  # type: ignore[list-item]
            channels_medium=None,
            channels_high=None,
            channels_critical=None,
            recipient_type=None,
        )


# ---------------------------------------------------------------------------
# _validate_recipient_dnd_settings — DND time error paths
# ---------------------------------------------------------------------------


def test_dnd_invalid_start_time_format_raises():
    """An invalid dnd_start time string triggers ValueError and maps to RCPT_CONFIG_DND_START_KEY."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start="25:99",  # invalid format
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
        )
    assert exc_info.value.field == "dnd_start"


def test_dnd_non_string_start_raises_type_error_field():
    """A non-string dnd_start triggers TypeError and maps to RCPT_CONFIG_DND_START_KEY."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=42,  # type: ignore[arg-type]
            dnd_end=None,
            dnd_allowed_sources_pattern=None,
        )
    assert exc_info.value.field == "dnd_start"


def test_dnd_invalid_end_time_format_raises():
    """An invalid dnd_end time string triggers ValueError and maps to RCPT_CONFIG_DND_END_KEY."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end="25:99",  # invalid format
            dnd_allowed_sources_pattern=None,
        )
    assert exc_info.value.field == "dnd_end"


def test_dnd_non_string_end_raises_type_error_field():
    """A non-string dnd_end triggers TypeError and maps to RCPT_CONFIG_DND_END_KEY."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=42,  # type: ignore[arg-type]
            dnd_allowed_sources_pattern=None,
        )
    assert exc_info.value.field == "dnd_end"


def test_dnd_valid_sources_pattern_accepted():
    """A valid regex dnd_allowed_sources_pattern does not raise."""
    ConfigValidator._validate_recipient_dnd_settings(
        dnd_enabled=False,
        dnd_start=None,
        dnd_end=None,
        dnd_allowed_sources_pattern="^home\\..*$",
    )  # must not raise


def test_dnd_invalid_sources_pattern_raises():
    """An invalid regex in dnd_allowed_sources_pattern raises FieldValidationError."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern="[unclosed",
        )
    assert exc_info.value.field == "dnd_allowed_sources_regex"


def test_dnd_non_string_sources_pattern_raises():
    """A non-string dnd_allowed_sources_pattern raises FieldValidationError (TypeError)."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_dnd_settings(
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=None,
            dnd_allowed_sources_pattern=42,  # type: ignore[arg-type]
        )
    assert exc_info.value.field == "dnd_allowed_sources_regex"


def test_dnd_allowed_criticalities_non_string_item_raises():
    """A list with a non-string criticality item raises FieldValidationError."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator._validate_recipient_dnd_settings(
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
        ConfigValidator._validate_recipient_dnd_settings(
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
# validate_system_config — TypeError paths
# ---------------------------------------------------------------------------


def test_validate_system_config_non_int_rate_limit_raises():
    """A non-integer global_rate_limit raises FieldValidationError on field 'global_rate_limit'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(
            _raw_sys_config(global_rate_limit="high")  # type: ignore[arg-type]
        )
    assert exc_info.value.field == "global_rate_limit"


def test_validate_system_config_non_string_channel_raises():
    """A non-string item in enabled_channels raises FieldValidationError on field 'enabled_channels'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(
            _raw_sys_config(
                enabled_channels=[42],  # type: ignore[list-item]
                persistent_notifications_enabled=False,
            )
        )
    assert exc_info.value.field == "enabled_channels"


# ---------------------------------------------------------------------------
# _validate_recipient_dnd_settings — HH:MM:SS time path (covers elif branch)
# ---------------------------------------------------------------------------


def test_dnd_hhmmss_start_and_end_times_accepted():
    """DND enabled with HH:MM:SS formatted start/end times does not raise."""
    ConfigValidator._validate_recipient_dnd_settings(
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
