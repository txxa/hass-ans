"""Channel mapping validation (per-criticality channel lists)."""

from __future__ import annotations

from typing import Any, cast

import voluptuous as vol

from ...const import RCPT_CONFIG_CHANNELS_KEY
from ...models import RecipientType
from .common import FieldValidationError, ValidationContext, _validate_string_list


def _validate_recipient_channel_mapping(
    channels_low: list[str] | None,
    channels_medium: list[str] | None,
    channels_high: list[str] | None,
    channels_critical: list[str] | None,
    available_channels: list[str] | None = None,
    recipient_type=None,
) -> None:
    """Validate channel mapping consistency."""
    from ...models import NotificationCriticality  # noqa: PLC0415

    # Choose allowed channel pattern based on recipient type.
    # TTS recipients use media_player.* channels; all others use notify.* channels.
    # When recipient_type is None (e.g. called from __post_init__), both are allowed.
    if recipient_type is None:
        regex_pattern = "^(notify|media_player)\\.[a-zA-Z0-9_]+$"
    elif recipient_type == RecipientType.TTS:
        regex_pattern = "^media_player\\.[a-zA-Z0-9_]+$"
    else:
        regex_pattern = "^notify\\.[a-zA-Z0-9_]+$"

    # Validate each channel list
    for channels, level in [
        (channels_low, NotificationCriticality.LOW.value.lower()),
        (channels_medium, NotificationCriticality.MEDIUM.value.lower()),
        (channels_high, NotificationCriticality.HIGH.value.lower()),
        (channels_critical, NotificationCriticality.CRITICAL.value.lower()),
    ]:
        if channels is not None:
            try:
                _validate_string_list(
                    channels,
                    min_length=0,
                    allowed_values=available_channels,
                    regex_pattern=regex_pattern,
                )
            except ValueError as e:
                raise FieldValidationError(
                    f"{RCPT_CONFIG_CHANNELS_KEY}_{level}", str(e)
                ) from e
            except TypeError as e:
                raise FieldValidationError(
                    f"{RCPT_CONFIG_CHANNELS_KEY}_{level}", str(e)
                ) from e


def validate_recipient_channel_mapping_schema(
    settings: dict, validation_context: ValidationContext
) -> dict:
    """Validate the identity channel mapping against a schema."""
    from ...models import NotificationCriticality  # noqa: PLC0415

    criticality_levels = [c.value for c in NotificationCriticality]

    schema_dict = {}

    for crit in criticality_levels:
        key = f"{RCPT_CONFIG_CHANNELS_KEY}_{crit.lower()}"
        schema_dict[vol.Optional(key)] = vol.Any(
            vol.All([vol.In(validation_context.available_channels)], vol.Length(min=1)),
            vol.Length(min=0),
        )

    schema = vol.Schema(
        schema_dict,
        extra=vol.PREVENT_EXTRA,
    )

    return cast(dict[str, Any], schema(settings))
