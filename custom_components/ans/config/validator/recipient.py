"""Recipient definition, basic settings, and DND validation."""

from __future__ import annotations

from typing import Any, cast

import voluptuous as vol

from ...const import (
    CONFIG_VERSION_KEY,
    RCPT_CONFIG_BLOCKED_SOURCES_PATTERN_KEY,
    RCPT_CONFIG_DND_ALLOWED_CRITICALITIES_KEY,
    RCPT_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY,
    RCPT_CONFIG_DND_ALLOWED_TYPES_KEY,
    RCPT_CONFIG_DND_ENABLED_KEY,
    RCPT_CONFIG_DND_END_KEY,
    RCPT_CONFIG_DND_END_MISSING_KEY,
    RCPT_CONFIG_DND_START_END_EQUALS_KEY,
    RCPT_CONFIG_DND_START_KEY,
    RCPT_CONFIG_DND_START_MISSING_KEY,
    RCPT_CONFIG_DND_TIMES_KEY,
    RCPT_CONFIG_EMAIL_KEY,
    RCPT_CONFIG_ID_KEY,
    RCPT_CONFIG_NAME_KEY,
    RCPT_CONFIG_NOTIFICATION_TYPES_KEY,
    RCPT_CONFIG_PHONE_KEY,
    RCPT_CONFIG_RATE_LIMIT_KEY,
    RCPT_CONFIG_RETRY_ATTEMPTS_KEY,
    RCPT_CONFIG_TYPE_KEY,
    RCPT_CONFIG_USER_KEY,
    RCPT_MAX_RETRY_ATTEMPTS,
)
from ...models import (
    NotificationCriticality,
    NotificationType,
    RecipientConfig,
    RecipientData,
    RecipientType,
)
from .channels import _validate_recipient_channel_mapping
from .common import (
    EMAIL_PATTERN,
    PHONE_PATTERN,
    TIME_PATTERN,
    FieldValidationError,
    ValidationContext,
    _time_to_sec,
    _validate_email_format,
    _validate_non_negative_integer,
    _validate_phone_format,
    _validate_regex_pattern,
    _validate_string_or_none,
    _validate_time_format,
)


def _validate_recipient_basic_settings(
    retry_attempts: int,
    rate_limit: int,
    notification_types: list[Any],
    blocked_sources_pattern: str | None,
    validation_context: ValidationContext | None = None,
) -> None:
    """Validate basic identity settings consistency."""
    # Validate retry attempts (hard-coded maximum)
    try:
        max_val = RCPT_MAX_RETRY_ATTEMPTS
        placeholders = {"min": "0", "max": str(max_val)}
        _validate_non_negative_integer(retry_attempts, max_val)
    except ValueError as e:
        raise FieldValidationError(
            RCPT_CONFIG_RETRY_ATTEMPTS_KEY, str(e), placeholders
        ) from e
    except TypeError as e:
        raise FieldValidationError(RCPT_CONFIG_RETRY_ATTEMPTS_KEY, str(e)) from e

    # Validate rate limit
    try:
        max_val = None
        placeholders = {"min": "0"}
        if validation_context:
            max_val = validation_context.get_max_recipient_rate_limit()
            placeholders["max"] = str(max_val)
        _validate_non_negative_integer(rate_limit, max_val)
    except ValueError as e:
        raise FieldValidationError(
            RCPT_CONFIG_RATE_LIMIT_KEY, str(e), placeholders
        ) from e
    except TypeError as e:
        raise FieldValidationError(RCPT_CONFIG_RATE_LIMIT_KEY, str(e)) from e

    # Validate notification types
    from ...models import NotificationType  # noqa: PLC0415

    for notification_type in notification_types:
        if isinstance(notification_type, str):
            try:
                notification_type = NotificationType(notification_type)
            except ValueError as e:
                raise FieldValidationError(
                    RCPT_CONFIG_NOTIFICATION_TYPES_KEY,
                    f"Invalid notification type '{notification_type}'.",
                ) from e
        if not isinstance(notification_type, NotificationType):
            raise FieldValidationError(
                RCPT_CONFIG_NOTIFICATION_TYPES_KEY,
                f"Invalid notification type '{notification_type}'.",
            )

    # Validate blocked sources pattern if provided
    if blocked_sources_pattern:
        try:
            _validate_regex_pattern(blocked_sources_pattern)
        except ValueError as e:
            raise FieldValidationError(
                RCPT_CONFIG_BLOCKED_SOURCES_PATTERN_KEY, str(e)
            ) from e
        except TypeError as e:
            raise FieldValidationError(
                RCPT_CONFIG_BLOCKED_SOURCES_PATTERN_KEY, str(e)
            ) from e


def validate_recipient_consistency(
    recipient_data: RecipientData, recipient_config: RecipientConfig
) -> None:
    """Validate that a RecipientConfig's channels are compatible with its RecipientData type.

    Args:
        recipient_data: The recipient metadata (carries the type).
        recipient_config: The recipient delivery configuration (carries channel lists).

    Raises:
        FieldValidationError: If channels are incompatible with the recipient type.

    """
    _validate_recipient_channel_mapping(
        recipient_config.channels_low,
        recipient_config.channels_medium,
        recipient_config.channels_high,
        recipient_config.channels_critical,
        recipient_type=recipient_data.type,
    )


def _validate_recipient_dnd_settings(  # noqa: C901
    dnd_enabled: bool,
    dnd_start: str | None,
    dnd_end: str | None,
    dnd_allowed_sources_pattern: str | None,
    dnd_allowed_criticalities: list[str] | None = None,
    dnd_allowed_types: list[str] | None = None,
) -> None:
    """Validate DND settings consistency."""
    s_sec = None
    e_sec = None

    if not isinstance(dnd_enabled, bool):
        raise FieldValidationError(RCPT_CONFIG_DND_ENABLED_KEY, "Must be a boolean.")

    if dnd_enabled:
        # When DND is enabled, start and end times must be set
        if dnd_start is None or dnd_end is None:
            raise FieldValidationError(
                RCPT_CONFIG_DND_TIMES_KEY,
                "Start and end times must be set when DND is enabled.",
            )

    # Validate start time if provided
    if dnd_start:
        # Validate time format
        try:
            _validate_time_format(dnd_start)
        except ValueError as e:
            raise FieldValidationError(RCPT_CONFIG_DND_START_KEY, str(e)) from e
        except TypeError as e:
            raise FieldValidationError(RCPT_CONFIG_DND_START_KEY, str(e)) from e
        # Convert to seconds for comparison
        try:
            s_sec = _time_to_sec(dnd_start)
        except (ValueError, TypeError, IndexError) as e:
            raise FieldValidationError(
                RCPT_CONFIG_DND_START_KEY,
                "Invalid time format, must be HH:MM:SS.",
            ) from e

    # Validate end time if provided
    if dnd_end:
        # Validate time format
        try:
            _validate_time_format(dnd_end)
        except ValueError as e:
            raise FieldValidationError(RCPT_CONFIG_DND_END_KEY, str(e)) from e
        except TypeError as e:
            raise FieldValidationError(RCPT_CONFIG_DND_END_KEY, str(e)) from e
        # Convert to seconds for comparison
        try:
            e_sec = _time_to_sec(dnd_end)
        except (ValueError, TypeError, IndexError) as e:
            raise FieldValidationError(
                RCPT_CONFIG_DND_END_KEY,
                "Invalid time format, must be HH:MM:SS.",
            ) from e

    # If both times are provided, ensure they are not the same
    if s_sec and e_sec:
        if s_sec == e_sec:
            raise FieldValidationError(
                RCPT_CONFIG_DND_START_END_EQUALS_KEY,
                "Start and end times cannot be the same.",
            )

    # Validate allowed sources pattern if provided
    if dnd_allowed_sources_pattern:
        try:
            _validate_regex_pattern(dnd_allowed_sources_pattern)
        except ValueError as e:
            raise FieldValidationError(
                RCPT_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY, str(e)
            ) from e
        except TypeError as e:
            raise FieldValidationError(
                RCPT_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY, str(e)
            ) from e

    # Validate allowed criticalities if provided
    if dnd_allowed_criticalities is not None:
        if not isinstance(dnd_allowed_criticalities, list):
            raise FieldValidationError(
                RCPT_CONFIG_DND_ALLOWED_CRITICALITIES_KEY,
                "Must be a list of criticality values.",
            )
        # Validate each criticality value exists in the enum
        valid_criticalities = {c.value for c in NotificationCriticality}
        for criticality in dnd_allowed_criticalities:
            if not isinstance(criticality, str):
                raise FieldValidationError(
                    RCPT_CONFIG_DND_ALLOWED_CRITICALITIES_KEY,
                    "Each criticality must be a string.",
                )
            if criticality not in valid_criticalities:
                raise FieldValidationError(
                    RCPT_CONFIG_DND_ALLOWED_CRITICALITIES_KEY,
                    f"Invalid criticality '{criticality}'. Must be one of: {', '.join(sorted(valid_criticalities))}.",
                )

    # Validate allowed types if provided
    if dnd_allowed_types is not None:
        if not isinstance(dnd_allowed_types, list):
            raise FieldValidationError(
                RCPT_CONFIG_DND_ALLOWED_TYPES_KEY,
                "Must be a list of notification types.",
            )
        # Validate each type value exists in the enum
        valid_types = {t.value for t in NotificationType}
        for notification_type in dnd_allowed_types:
            if not isinstance(notification_type, str):
                raise FieldValidationError(
                    RCPT_CONFIG_DND_ALLOWED_TYPES_KEY,
                    "Each notification type must be a string.",
                )
            if notification_type not in valid_types:
                raise FieldValidationError(
                    RCPT_CONFIG_DND_ALLOWED_TYPES_KEY,
                    f"Invalid notification type '{notification_type}'. Must be one of: {', '.join(sorted(valid_types))}.",
                )


def validate_recipient_definition_schema(identity: dict) -> dict:
    """Validate the identity against a schema."""
    schema = vol.Schema(
        {
            vol.Optional(RCPT_CONFIG_USER_KEY): vol.Any(str, None),
            vol.Required(RCPT_CONFIG_NAME_KEY): vol.All(str),
            vol.Optional(RCPT_CONFIG_EMAIL_KEY): vol.Any(
                None, vol.Match(EMAIL_PATTERN)
            ),
            vol.Optional(RCPT_CONFIG_PHONE_KEY): vol.Any(
                None, vol.Match(PHONE_PATTERN)
            ),
        },
        extra=vol.PREVENT_EXTRA,
    )
    return cast(dict[str, Any], schema(identity))


def validate_recipient_basic_settings_schema(
    settings: dict, validation_context: ValidationContext
) -> dict:
    """Validate the basic identity settings against a schema."""
    from ...models import NotificationType  # noqa: PLC0415

    # Voluptuous based form validation
    schema = vol.Schema(
        {
            vol.Required(RCPT_CONFIG_RETRY_ATTEMPTS_KEY): vol.All(
                int,
                vol.Range(min=0, max=RCPT_MAX_RETRY_ATTEMPTS),
            ),
            vol.Required(RCPT_CONFIG_RATE_LIMIT_KEY): vol.All(
                int,
                vol.Range(min=0, max=validation_context.get_max_recipient_rate_limit()),
            ),
            vol.Required(RCPT_CONFIG_NOTIFICATION_TYPES_KEY): vol.All(
                [vol.In([t.value for t in NotificationType])], vol.Length(min=1)
            ),
            vol.Optional(RCPT_CONFIG_BLOCKED_SOURCES_PATTERN_KEY): vol.All(
                str, _validate_regex_pattern
            ),
        },
        extra=vol.PREVENT_EXTRA,
    )
    return cast(dict[str, Any], schema(settings))


def validate_recipient_dnd_settings_schema(data: dict) -> dict:
    """Validate the DND settings against a schema."""
    schema = vol.Schema(
        {
            vol.Required(RCPT_CONFIG_DND_ENABLED_KEY): vol.All(bool),
            vol.Optional(RCPT_CONFIG_DND_START_KEY): vol.Match(TIME_PATTERN),
            vol.Optional(RCPT_CONFIG_DND_END_KEY): vol.Match(TIME_PATTERN),
            vol.Optional(RCPT_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY): vol.All(
                str, _validate_regex_pattern
            ),
            vol.Required(RCPT_CONFIG_DND_ALLOWED_CRITICALITIES_KEY): vol.All(
                [vol.In([c.value for c in NotificationCriticality])],
                vol.Length(min=0),
            ),
            vol.Required(RCPT_CONFIG_DND_ALLOWED_TYPES_KEY): vol.All(
                [vol.In([t.value for t in NotificationType])], vol.Length(min=0)
            ),
        },
        extra=vol.PREVENT_EXTRA,
    )
    validated_schema = cast(dict[str, Any], schema(data))

    if validated_schema.get(RCPT_CONFIG_DND_ENABLED_KEY):
        if not validated_schema.get(RCPT_CONFIG_DND_START_KEY):
            raise FieldValidationError(
                RCPT_CONFIG_DND_START_MISSING_KEY,
                "Start time must be set when DND is enabled.",
            )
        if not validated_schema.get(RCPT_CONFIG_DND_END_KEY):
            raise FieldValidationError(
                RCPT_CONFIG_DND_END_MISSING_KEY,
                "End time must be set when DND is enabled.",
            )
    dnd_start = validated_schema.get(RCPT_CONFIG_DND_START_KEY)
    dnd_end = validated_schema.get(RCPT_CONFIG_DND_END_KEY)
    if dnd_start is not None and dnd_end is not None:
        if _time_to_sec(dnd_start) == _time_to_sec(dnd_end):
            raise FieldValidationError(
                "base",
                "Start and end times cannot be the same.",
                translation_key=RCPT_CONFIG_DND_START_END_EQUALS_KEY,
            )

    return validated_schema


def validate_recipient(recipient: RecipientData) -> None:
    """Validate the identity."""

    # Validate id
    if not recipient.id:
        raise FieldValidationError(RCPT_CONFIG_ID_KEY, "ID cannot be empty.")
    try:
        _validate_string_or_none(recipient.id)
    except TypeError as e:
        raise FieldValidationError(RCPT_CONFIG_ID_KEY, str(e)) from e

    # Validate type
    if not isinstance(recipient.type, RecipientType):
        raise FieldValidationError(
            RCPT_CONFIG_TYPE_KEY,
            "Type must be a string or IdentityType enum.",
        )

    # Validate name
    if not recipient.name:
        raise FieldValidationError(RCPT_CONFIG_NAME_KEY, "Name cannot be empty.")
    try:
        _validate_string_or_none(recipient.name)
    except TypeError as e:
        raise FieldValidationError(RCPT_CONFIG_NAME_KEY, str(e)) from e

    # Validate email format
    if recipient.email:
        try:
            _validate_email_format(recipient.email)
        except ValueError as e:
            raise FieldValidationError(RCPT_CONFIG_EMAIL_KEY, str(e)) from e
        except TypeError as e:
            raise FieldValidationError(RCPT_CONFIG_EMAIL_KEY, str(e)) from e

    # Validate phone format
    if recipient.phone:
        try:
            _validate_phone_format(recipient.phone)
        except ValueError as e:
            raise FieldValidationError(RCPT_CONFIG_PHONE_KEY, str(e)) from e
        except TypeError as e:
            raise FieldValidationError(RCPT_CONFIG_PHONE_KEY, str(e)) from e

    # Validate config version
    if not isinstance(recipient.version, int) or recipient.version < 1:
        raise FieldValidationError(CONFIG_VERSION_KEY, "Must be a positive integer.")


def validate_recipient_config(config: RecipientConfig) -> None:
    """Validate the user configuration."""

    # Validate basic settings
    _validate_recipient_basic_settings(
        config.retry_attempts,
        config.rate_limit,
        config.notification_types,
        config.blocked_sources_regex,
    )
    # Validate channel mapping
    _validate_recipient_channel_mapping(
        config.channels_low,
        config.channels_medium,
        config.channels_high,
        config.channels_critical,
    )
    # Validate DND settings
    _validate_recipient_dnd_settings(
        config.dnd_enabled,
        config.dnd_start,
        config.dnd_end,
        config.dnd_allowed_sources_regex,
        config.dnd_allowed_criticalities,
        config.dnd_allowed_types,
    )

    # Validate config version
    if not isinstance(config.version, int) or config.version < 1:
        raise FieldValidationError(CONFIG_VERSION_KEY, "Must be a positive integer.")
