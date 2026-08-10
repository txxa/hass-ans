"""Unit tests for TTS settings schema and runtime TTS entity validation (config/validator/tts.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import voluptuous as vol

from ....config.validator import ConfigValidator, validate_tts_service

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
