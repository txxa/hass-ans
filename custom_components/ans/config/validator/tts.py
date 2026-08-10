"""TTS settings schema validation and runtime TTS entity validation."""

from __future__ import annotations

import logging
import re
from typing import Any, cast

import voluptuous as vol
from homeassistant.core import HomeAssistant

from ...const import (
    RCPT_CONFIG_TTS_MESSAGE_FORMAT_KEY,
    RCPT_CONFIG_TTS_SSML_ENABLED_KEY,
    RCPT_CONFIG_TTS_VOLUME_DAYTIME_KEY,
    RCPT_CONFIG_TTS_VOLUME_EVENING_KEY,
    RCPT_CONFIG_TTS_VOLUME_MANAGEMENT_ENABLED_KEY,
    RCPT_CONFIG_TTS_VOLUME_MORNING_KEY,
    RCPT_CONFIG_TTS_VOLUME_NIGHT_KEY,
    RCPT_CONFIG_TTS_VOLUME_OVERRIDE_CRITICALITIES_KEY,
    RCPT_CONFIG_TTS_VOLUME_OVERRIDE_LEVEL_KEY,
    TTS_DEFAULT_SSML_ENABLED,
    TTS_DEFAULT_VOLUME_MANAGEMENT_ENABLED,
)
from ...models import NotificationCriticality

_LOGGER = logging.getLogger(__name__)


def validate_recipient_tts_settings_schema(data: dict) -> dict:
    """Validate TTS settings against a schema.

    Validates volume levels (0-100), criticality overrides, and message format.

    Args:
        data: TTS settings dictionary from config flow

    Returns:
        Validated TTS settings dictionary

    Raises:
        vol.Invalid: If validation fails

    """
    # Valid message formats
    valid_formats = ["title_and_message", "message_only", "title_only"]

    schema = vol.Schema(
        {
            vol.Required(RCPT_CONFIG_TTS_MESSAGE_FORMAT_KEY): vol.In(valid_formats),
            vol.Optional(
                RCPT_CONFIG_TTS_SSML_ENABLED_KEY,
                default=TTS_DEFAULT_SSML_ENABLED,
            ): bool,
            vol.Optional(
                RCPT_CONFIG_TTS_VOLUME_MANAGEMENT_ENABLED_KEY,
                default=TTS_DEFAULT_VOLUME_MANAGEMENT_ENABLED,
            ): bool,
            vol.Required(RCPT_CONFIG_TTS_VOLUME_MORNING_KEY): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=100)
            ),
            vol.Required(RCPT_CONFIG_TTS_VOLUME_DAYTIME_KEY): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=100)
            ),
            vol.Required(RCPT_CONFIG_TTS_VOLUME_EVENING_KEY): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=100)
            ),
            vol.Required(RCPT_CONFIG_TTS_VOLUME_NIGHT_KEY): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=100)
            ),
            vol.Optional(
                RCPT_CONFIG_TTS_VOLUME_OVERRIDE_CRITICALITIES_KEY, default=[]
            ): vol.All(
                [vol.In([c.value for c in NotificationCriticality])],
                vol.Length(min=0),
            ),
            vol.Required(RCPT_CONFIG_TTS_VOLUME_OVERRIDE_LEVEL_KEY): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=100)
            ),
        },
        extra=vol.PREVENT_EXTRA,
    )

    return cast(dict[str, Any], schema(data))


async def validate_tts_service(hass: HomeAssistant, value: str) -> str | None:
    """Validate TTS engine entity ID format and runtime existence (PRIORITY 1 - Security).

    Ensures the selected TTS entity:
    1. Follows the correct ``tts.<name>`` format
    2. Actually exists in the Home Assistant state machine at runtime
    3. Contains no malicious patterns

    Parameters
    ----------
    hass : HomeAssistant
        Home Assistant instance for runtime validation.
    value : str
        TTS entity ID (e.g., ``"tts.piper"`` or ``"tts.cloud"``).

    Returns
    -------
    str | None
        Validated entity ID, or ``None`` if disabled.

    Raises
    ------
    vol.Invalid
        If the entity ID format is invalid or the entity doesn't exist.

    """
    # Handle "None" string (user deselected TTS)
    if value == "None" or value is None or value == "":
        return None

    # Validate format: must start with "tts."
    if not value.startswith("tts."):
        raise vol.Invalid(f"TTS entity must start with 'tts.' (got: {value})")

    # Validate format: must contain only alphanumeric, underscore, and dot
    if not re.match(r"^tts\.[a-z0-9_]+$", value):
        raise vol.Invalid(
            f"Invalid TTS entity format: {value}. "
            "Must contain only lowercase letters, numbers, and underscores after 'tts.'"
        )

    # Runtime validation: entity must exist in the state machine
    state = hass.states.get(value)
    if state is None:
        available = ", ".join(sorted(hass.states.async_entity_ids("tts"))) or "none"
        raise vol.Invalid(f"TTS entity '{value}' not found. Available: {available}")

    _LOGGER.debug("TTS entity validation passed: %s", value)
    return value
