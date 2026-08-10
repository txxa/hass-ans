"""Unit tests for shared validation primitives (config/validator/common.py)."""

from __future__ import annotations

import pytest

from ....config.validator.common import (
    FieldValidationError,
    ValidationContext,
    _time_to_sec,
    _validate_boolean,
    _validate_email_format,
    _validate_non_negative_integer,
    _validate_phone_format,
    _validate_positive_integer,
    _validate_regex_pattern,
    _validate_string_list,
    _validate_string_or_none,
    _validate_time_format,
)
from ....const import RCPT_MAX_RATE_LIMIT, SYS_MAX_GLOBAL_RATE_LIMIT

# ---------------------------------------------------------------------------
# Time format validation
# ---------------------------------------------------------------------------


def test_validate_time_format_valid_hhmm():
    """A time in HH:MM format ('08:30') is valid and returned unchanged."""
    result = _validate_time_format("08:30")
    assert result == "08:30"


def test_validate_time_format_valid_hhmmss():
    """A time in HH:MM:SS format ('22:00:00') is valid and returned unchanged."""
    result = _validate_time_format("22:00:00")
    assert result == "22:00:00"


def test_validate_time_format_invalid_raises():
    """An invalid time string ('25:99') raises ValueError containing 'HH:MM'."""
    with pytest.raises(ValueError, match="HH:MM"):
        _validate_time_format("25:99")


def test_validate_time_format_none_returns_none():
    """_validate_time_format(None) returns None."""
    assert _validate_time_format(None) is None


# ---------------------------------------------------------------------------
# Email format validation
# ---------------------------------------------------------------------------


def test_validate_email_valid():
    """_validate_email_format('user@example.com') returns the address unchanged."""
    result = _validate_email_format("user@example.com")
    assert result == "user@example.com"


def test_validate_email_invalid_is_accepted_by_helper():
    # vol.Email is a function factory: vol.Email(value) creates a *new validator*
    # (treating `value` as the error-message parameter) rather than validating the input.
    # As a result _validate_email_format does not reject malformed addresses;
    # real email validation happens only at the schema level (validate_recipient_definition_schema).
    """Due to a vol.Email quirk, _validate_email_format() does not reject malformed addresses."""
    result = _validate_email_format("not-an-email")
    assert result == "not-an-email"  # silently accepted — no ValueError is raised


def test_validate_email_none_returns_none():
    """_validate_email_format(None) returns None."""
    assert _validate_email_format(None) is None


# ---------------------------------------------------------------------------
# Phone format validation
# ---------------------------------------------------------------------------


def test_validate_phone_valid_e164():
    """_validate_phone_format('+1234567890') returns the number unchanged."""
    result = _validate_phone_format("+1234567890")
    assert result == "+1234567890"


def test_validate_phone_invalid_starts_with_zero():
    # PHONE_PATTERN is r"^\+?[1-9]\d{1,14}$" — leading 0 is not allowed
    """A phone number starting with '0' raises ValueError containing 'E.164'."""
    with pytest.raises(ValueError, match="E.164"):
        _validate_phone_format("0123456789")


def test_validate_phone_none_returns_none():
    """_validate_phone_format(None) returns None."""
    assert _validate_phone_format(None) is None


# ---------------------------------------------------------------------------
# Regex pattern validation
# ---------------------------------------------------------------------------


def test_validate_regex_valid():
    """_validate_regex_pattern() returns a valid regex pattern unchanged."""
    result = _validate_regex_pattern("^home.*$")
    assert result == "^home.*$"


def test_validate_regex_invalid_raises():
    """An unclosed character class raises ValueError containing 'Invalid regex'."""
    with pytest.raises(ValueError, match="Invalid regex"):
        _validate_regex_pattern("[unclosed")


# ---------------------------------------------------------------------------
# _validate_boolean
# ---------------------------------------------------------------------------


def test_validate_boolean_true():
    """_validate_boolean(True) returns True."""
    assert _validate_boolean(True) is True


def test_validate_boolean_false():
    """_validate_boolean(False) returns False."""
    assert _validate_boolean(False) is False


def test_validate_boolean_invalid():
    """_validate_boolean('yes') raises TypeError containing 'boolean'."""
    with pytest.raises(TypeError, match="boolean"):
        _validate_boolean("yes")


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
    result = _validate_positive_integer(value, min_val=min_val, max_val=max_val)
    assert result == value


def test_validate_positive_integer_below_min_raises():
    """A value below min_val raises ValueError containing '>='."""
    with pytest.raises(ValueError, match=">="):
        _validate_positive_integer(0, min_val=1)


def test_validate_positive_integer_above_max_raises():
    """A value above max_val raises ValueError containing '<='."""
    with pytest.raises(ValueError, match="<="):
        _validate_positive_integer(11, min_val=1, max_val=10)


def test_validate_positive_integer_non_integer_raises():
    """A non-integer value raises TypeError containing 'integer'."""
    with pytest.raises(TypeError, match="integer"):
        _validate_positive_integer("5", min_val=1)


def test_validate_positive_integer_no_max_allows_large():
    # max_val=None means no upper bound
    """When max_val=None there is no upper bound and very large values are accepted."""
    result = _validate_positive_integer(10_000_000, min_val=1, max_val=None)
    assert result == 10_000_000


# ---------------------------------------------------------------------------
# _validate_non_negative_integer
# ---------------------------------------------------------------------------


def test_validate_non_negative_integer_zero_accepted():
    """_validate_non_negative_integer(0) returns 0 (zero is non-negative)."""
    assert _validate_non_negative_integer(0) == 0


def test_validate_non_negative_integer_negative_raises():
    """_validate_non_negative_integer(-1) raises ValueError."""
    with pytest.raises(ValueError):
        _validate_non_negative_integer(-1)


def test_validate_non_negative_integer_respects_max_val():
    """A value above max_val raises ValueError containing '<='."""
    with pytest.raises(ValueError, match="<="):
        _validate_non_negative_integer(101, max_val=100)


# ---------------------------------------------------------------------------
# _validate_string_or_none
# ---------------------------------------------------------------------------


def test_validate_string_or_none_none_returns_none():
    """_validate_string_or_none(None) returns None."""
    assert _validate_string_or_none(None) is None


def test_validate_string_or_none_string_returned_unchanged():
    """_validate_string_or_none('hello') returns 'hello' unchanged."""
    assert _validate_string_or_none("hello") == "hello"


def test_validate_string_or_none_non_string_non_none_raises():
    """A non-string, non-None value raises TypeError containing 'string or None'."""
    with pytest.raises(TypeError, match="string or None"):
        _validate_string_or_none(42)


# ---------------------------------------------------------------------------
# _validate_string_list
# ---------------------------------------------------------------------------


def test_validate_string_list_non_list_raises():
    """A non-list argument raises TypeError containing 'list'."""
    with pytest.raises(TypeError, match="list"):
        _validate_string_list("not-a-list")


def test_validate_string_list_too_short_raises():
    """A list shorter than min_length raises ValueError containing 'at least 2'."""
    with pytest.raises(ValueError, match="at least 2"):
        _validate_string_list(["one"], min_length=2)


def test_validate_string_list_non_string_item_raises():
    """A list containing a non-string item raises TypeError containing 'string'."""
    with pytest.raises(TypeError, match="string"):
        _validate_string_list([1, 2, 3])


def test_validate_string_list_item_not_in_allowed_values_raises():
    """An item absent from allowed_values raises ValueError containing 'not a valid'."""
    with pytest.raises(ValueError, match="not a valid"):
        _validate_string_list(["bad"], allowed_values=["good"])


def test_validate_string_list_item_not_matching_regex_raises():
    """An item not matching regex_pattern raises ValueError containing 'does not match'."""
    with pytest.raises(ValueError, match="does not match"):
        _validate_string_list(["ABC"], regex_pattern="^[a-z]+$")


def test_validate_string_list_invalid_regex_pattern_raises():
    """An invalid regex_pattern raises ValueError containing 'Invalid regex'."""
    with pytest.raises(ValueError, match="Invalid regex"):
        _validate_string_list(["x"], regex_pattern="[unclosed")


def test_validate_string_list_valid_all_constraints():
    """A valid list satisfying all constraints (min_length, allowed_values, regex) is returned unchanged."""
    items = ["abc", "def"]
    result = _validate_string_list(
        items,
        min_length=1,
        allowed_values=["abc", "def", "ghi"],
        regex_pattern="^[a-z]+$",
    )
    assert result == items


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
# _time_to_sec — private helper
# ---------------------------------------------------------------------------

# The private helper is tested directly to cover branches that can never be
# reached through the public validators (because those first apply
# _validate_time_format which only passes valid HH:MM / HH:MM:SS strings).


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
        _validate_time_format(42)


def test_validate_email_format_non_string_raises_type_error():
    """_validate_email_format(42) raises TypeError containing 'string'."""
    with pytest.raises(TypeError, match="string"):
        _validate_email_format(42)


def test_validate_phone_format_non_string_raises_type_error():
    """_validate_phone_format(42) raises TypeError containing 'string'."""
    with pytest.raises(TypeError, match="string"):
        _validate_phone_format(42)


def test_validate_regex_pattern_non_string_raises_type_error():
    """_validate_regex_pattern(42) raises TypeError containing 'string'."""
    with pytest.raises(TypeError, match="string"):
        _validate_regex_pattern(42)
