"""Shared validation primitives, patterns, and context used across validator domains."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol

from ...const import (
    RCPT_MAX_RATE_LIMIT,
    SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY,
    SYS_MAX_GLOBAL_RATE_LIMIT,
)

# Time format regex pattern
TIME_PATTERN = r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$"
PHONE_PATTERN = r"^\+?[1-9]\d{1,14}$"  # E.164 format
EMAIL_PATTERN = r"^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*@(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$"  # RFC 5322


class FieldValidationError(Exception):
    """Custom exception for field validation errors."""

    def __init__(
        self,
        field: str,
        message: str,
        placeholders: dict[str, str] | None = None,
        translation_key: str | None = None,
    ):
        """Initialize with field name and error message.

        Args:
            field: The name of the form field that failed validation.
            message: Human-readable description of the error (used for logging).
            placeholders: Optional placeholder values for the translation string.
            translation_key: The HA translation key for the errors dict value.
                Defaults to the field name when not set, which matches the HA
                convention where the field name equals the translation key.
                Set explicitly only when they differ (e.g. field='base' but
                translation key='dnd_start_end_equals').

        """
        self.field = field
        self.message = message
        self.placeholders = placeholders
        self.translation_key = translation_key if translation_key is not None else field

    def __str__(self):
        """Format error message."""
        return f"{self.field}: {self.message}"


@dataclass
class ValidationContext:
    """Context object containing validation constraints and system limits."""

    system_limits: dict[str, Any] | None = None
    available_channels: list[str] = field(default_factory=list)

    def get_max_global_rate_limit(self) -> int:
        """Get maximum global rate limit from system limits."""
        if self.system_limits:
            return self.system_limits.get(
                SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY, SYS_MAX_GLOBAL_RATE_LIMIT
            )
        return SYS_MAX_GLOBAL_RATE_LIMIT

    def get_max_recipient_rate_limit(self) -> int:
        """Get maximum recipient rate limit from system limits."""
        return RCPT_MAX_RATE_LIMIT


def _time_to_sec(time: str) -> int:
    """Convert time string (HH:MM or HH:MM:SS) to seconds since midnight."""
    parts = time.split(":")
    if len(parts) == 2:
        h, m = parts
        s = 0
    elif len(parts) == 3:
        h, m, s = parts
    else:
        raise ValueError("Invalid time format, must be HH:MM or HH:MM:SS.")
    try:
        h = int(h)
        m = int(m)
        s = int(s)
    except (ValueError, TypeError) as e:
        raise ValueError("Time components must be integers.") from e
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
        raise ValueError("Time values out of range.")
    return h * 3600 + m * 60 + s


def _validate_positive_integer(
    value: Any,
    min_val: int = 1,
    max_val: int | None = None,
) -> int:
    """Validate that value is a positive integer within optional bounds."""
    if not isinstance(value, int):
        raise TypeError("Value must be an integer.")

    if value < min_val:
        raise ValueError(f"Value must be >= {min_val}.")

    if max_val is not None and value > max_val:
        raise ValueError(f"Value must be <= {max_val}.")

    return value


def _validate_non_negative_integer(
    value: Any,
    max_val: int | None = None,
) -> int:
    """Validate that value is a non-negative integer within optional bounds."""
    return _validate_positive_integer(value, min_val=0, max_val=max_val)


def _validate_string_or_none(value: Any) -> str | None:
    """Validate that value is a string or None."""
    if value is not None and not isinstance(value, str):
        raise TypeError("Value must be a string or None.")
    return value


def _validate_string_list(
    value: Any,
    min_length: int = 0,
    allowed_values: list[str] | None = None,
    regex_pattern: str | None = None,
) -> list[str]:
    """Validate that value is a list of strings with optional constraints."""
    if not isinstance(value, list):
        raise TypeError("Value must be a list of strings.")

    if len(value) < min_length:
        raise ValueError(f"Value must contain at least {min_length} items.")

    # Compile regex pattern if provided
    regex = None
    if regex_pattern:
        try:
            regex = re.compile(regex_pattern)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}") from e

    for item in value:
        if not isinstance(item, str):
            raise TypeError("Each item in value must be a string.")

        if allowed_values and item not in allowed_values:
            raise ValueError(f"'{item}' is not a valid value.")

        if regex and not regex.match(item):
            raise ValueError(
                f"'{item}' does not match the required pattern: {regex_pattern}"
            )

    return value


def _validate_time_format(value: Any) -> str | None:
    """Validate time format (HH:MM and HH:MM:SS)."""
    if value is None:
        return None

    if not isinstance(value, str):
        raise TypeError("Value must be a string.")

    if not re.match(TIME_PATTERN, value):
        raise ValueError("Value must be in HH:MM or HH:MM:SS 24-hour format.")

    return value


def _validate_email_format(value: Any) -> str | None:
    """Validate email format."""
    if value is None:
        return None

    if not isinstance(value, str):
        raise TypeError("Value must be a string.")

    try:
        vol.Email(value)
    except vol.Invalid as e:
        raise ValueError(f"Invalid value: {e}") from e

    return value


def _validate_phone_format(value: Any) -> str | None:
    """Validate phone number format (E.164)."""
    if value is None:
        return None

    if not isinstance(value, str):
        raise TypeError("Value must be a string.")

    if not re.match(PHONE_PATTERN, value):
        raise ValueError("Value must be in E.164 format (e.g., +1234567890).")

    return value


def _validate_regex_pattern(value: str) -> str:
    """Validate regex pattern."""
    if not isinstance(value, str):
        raise TypeError("Value must be a string.")

    try:
        re.compile(value)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern '{value}': {e}") from e

    return value


def _validate_boolean(value: Any) -> bool:
    """Validate boolean value."""
    if not isinstance(value, bool):
        raise TypeError("Value must be a boolean.")
    return value
