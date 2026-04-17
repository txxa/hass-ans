"""Unit tests for config forms (schema factories in forms.py)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import voluptuous as vol

from ..config.forms import (
    detect_tts_integrations,
    get_recipient_basic_settings_schema,
    get_recipient_criticality_channel_mapping_schema,
    get_recipient_definition_schema,
    get_system_options_schema,
)
from ..config.validator import ValidationContext
from ..const import (
    RCPT_CONFIG_CHANNELS_KEY,
    RCPT_CONFIG_CONFIGURED_CHANNELS_KEY,
    RCPT_CONFIG_CRITICALITY_LEVELS_KEY,
    RCPT_CONFIG_EMAIL_KEY,
    RCPT_CONFIG_NAME_KEY,
    RCPT_CONFIG_PHONE_KEY,
    SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY,
    SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY,
    SYS_CONFIG_RETRY_BASE_DELAY_KEY,
    SYS_CONFIG_RETRY_MAX_DELAY_KEY,
    SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY,
    SYS_DEFAULT_GLOBAL_RATE_LIMIT,
    SYS_DEFAULT_RETRY_BACKOFF_FACTOR,
    SYS_DEFAULT_RETRY_BASE_DELAY_SECONDS,
    SYS_DEFAULT_RETRY_MAX_DELAY_SECONDS,
    SYS_MAX_GLOBAL_RATE_LIMIT,
    SYS_STORAGE_DEFAULT_FILE_RETENTION_DAYS,
)
from ..models import NotificationCriticality, RecipientType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _schema_key_strings(schema: vol.Schema) -> set[str]:
    """Return all string key names present in a voluptuous Schema."""
    keys: set[str] = set()
    for k in schema.schema:
        # vol.Required / vol.Optional carry the key in .schema attribute
        raw = getattr(k, "schema", k)
        keys.add(str(raw))
    return keys


def _valid_options_dict(**overrides) -> dict:
    """Return a minimal valid dict for get_system_options_schema."""
    base = {
        SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY: SYS_DEFAULT_GLOBAL_RATE_LIMIT,
        SYS_CONFIG_RETRY_BASE_DELAY_KEY: SYS_DEFAULT_RETRY_BASE_DELAY_SECONDS,
        SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY: SYS_DEFAULT_RETRY_BACKOFF_FACTOR,
        SYS_CONFIG_RETRY_MAX_DELAY_KEY: SYS_DEFAULT_RETRY_MAX_DELAY_SECONDS,
        "queue_max_concurrency": 5,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# get_system_options_schema
# ---------------------------------------------------------------------------


def test_system_options_schema_with_retention_valid():
    """Valid dict including storage_retention_days passes."""
    schema = get_system_options_schema(defaults={}, include_audit_retention=True)
    data = _valid_options_dict()
    data[SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY] = (
        SYS_STORAGE_DEFAULT_FILE_RETENTION_DAYS
    )
    result = schema(data)
    assert result[SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY] == SYS_DEFAULT_GLOBAL_RATE_LIMIT


def test_system_options_schema_with_retention_rejects_out_of_range():
    """Rate limit out of range raises vol.Invalid."""
    schema = get_system_options_schema(defaults={}, include_audit_retention=True)
    data = _valid_options_dict(
        **{
            SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY: SYS_MAX_GLOBAL_RATE_LIMIT + 1,
            SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY: SYS_STORAGE_DEFAULT_FILE_RETENTION_DAYS,
        }
    )
    with pytest.raises(vol.Invalid):
        schema(data)


def test_system_options_schema_without_retention_has_no_retention_key():
    """When include_audit_retention=False the storage_retention_days key is absent."""
    schema = get_system_options_schema(defaults={}, include_audit_retention=False)
    key_names = _schema_key_strings(schema)
    assert SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY not in key_names


def test_system_options_schema_without_retention_valid():
    """Valid dict (no retention field) passes when include_audit_retention=False."""
    schema = get_system_options_schema(defaults={}, include_audit_retention=False)
    result = schema(_valid_options_dict())
    assert (
        result[SYS_CONFIG_RETRY_BASE_DELAY_KEY] == SYS_DEFAULT_RETRY_BASE_DELAY_SECONDS
    )


@pytest.mark.parametrize(
    "field,bad_value",
    [
        (SYS_CONFIG_RETRY_BASE_DELAY_KEY, 0),  # below minimum of 1
        (SYS_CONFIG_RETRY_MAX_DELAY_KEY, 59),  # below minimum of 60
        ("queue_max_concurrency", 0),  # below minimum of 1
    ],
)
def test_system_options_schema_rejects_invalid_values(field, bad_value):
    """Out-of-range values for numeric fields raise vol.Invalid."""
    schema = get_system_options_schema(defaults={}, include_audit_retention=False)
    data = _valid_options_dict(**{field: bad_value})
    with pytest.raises(vol.Invalid):
        schema(data)


# ---------------------------------------------------------------------------
# get_recipient_definition_schema
# ---------------------------------------------------------------------------


def test_recipient_definition_schema_non_tts_has_email_and_phone():
    """Non-TTS schema includes email and phone optional fields."""
    schema = get_recipient_definition_schema(
        defaults={}, recipient_type=RecipientType.GENERIC
    )
    key_names = _schema_key_strings(schema)
    assert RCPT_CONFIG_EMAIL_KEY in key_names
    assert RCPT_CONFIG_PHONE_KEY in key_names


def test_recipient_definition_schema_tts_omits_email_and_phone():
    """TTS schema omits email and phone fields."""
    schema = get_recipient_definition_schema(
        defaults={}, recipient_type=RecipientType.TTS
    )
    key_names = _schema_key_strings(schema)
    assert RCPT_CONFIG_EMAIL_KEY not in key_names
    assert RCPT_CONFIG_PHONE_KEY not in key_names


def test_recipient_definition_schema_name_always_present():
    """The name field is required regardless of recipient type."""
    for rtype in (RecipientType.GENERIC, RecipientType.TTS):
        schema = get_recipient_definition_schema(defaults={}, recipient_type=rtype)
        assert RCPT_CONFIG_NAME_KEY in _schema_key_strings(schema)


def test_recipient_definition_schema_tts_valid_minimal():
    """TTS schema accepts a dict with only the name field."""
    schema = get_recipient_definition_schema(
        defaults={}, recipient_type=RecipientType.TTS
    )
    result = schema({RCPT_CONFIG_NAME_KEY: "My TTS"})
    assert result[RCPT_CONFIG_NAME_KEY] == "My TTS"


# ---------------------------------------------------------------------------
# get_recipient_basic_settings_schema
# ---------------------------------------------------------------------------


def test_recipient_basic_settings_schema_has_expected_keys():
    """Schema contains rate_limit, retry_attempts, notification_types, and optional blocked pattern."""
    ctx = ValidationContext()
    schema = get_recipient_basic_settings_schema(
        defaults={}, validation_context=ctx, values={}
    )
    key_names = _schema_key_strings(schema)
    assert "rate_limit" in key_names
    assert "retry_attempts" in key_names
    assert "notification_types" in key_names


def test_recipient_basic_settings_schema_rate_limit_out_of_range_raises():
    """Rate limit above max raises vol.Invalid."""
    ctx = ValidationContext()
    schema = get_recipient_basic_settings_schema(
        defaults={}, validation_context=ctx, values={}
    )
    with pytest.raises(vol.Invalid):
        schema(
            {
                "rate_limit": ctx.get_max_recipient_rate_limit() + 1,
                "retry_attempts": 2,
                "notification_types": [],
            }
        )


def test_recipient_basic_settings_schema_retry_out_of_range_raises():
    """retry_attempts above max raises vol.Invalid."""
    from ..const import RCPT_MAX_RETRY_ATTEMPTS

    ctx = ValidationContext()
    schema = get_recipient_basic_settings_schema(
        defaults={}, validation_context=ctx, values={}
    )
    with pytest.raises(vol.Invalid):
        schema(
            {
                "rate_limit": 10,
                "retry_attempts": RCPT_MAX_RETRY_ATTEMPTS + 1,
                "notification_types": [],
            }
        )


# ---------------------------------------------------------------------------
# get_recipient_criticality_channel_mapping_schema
# ---------------------------------------------------------------------------


def test_criticality_channel_mapping_schema_has_one_key_per_criticality():
    """Schema has exactly one key per NotificationCriticality level."""
    values = {
        RCPT_CONFIG_CRITICALITY_LEVELS_KEY: [
            {"value": c.value} for c in NotificationCriticality
        ],
        RCPT_CONFIG_CONFIGURED_CHANNELS_KEY: [],
    }
    schema = get_recipient_criticality_channel_mapping_schema(
        defaults={}, values=values
    )
    key_names = _schema_key_strings(schema)

    for crit in NotificationCriticality:
        expected_key = f"{RCPT_CONFIG_CHANNELS_KEY}_{crit.value.lower()}"
        assert expected_key in key_names, f"Missing key: {expected_key}"


def test_criticality_channel_mapping_schema_no_criticalities_produces_empty_schema():
    """With no criticality_levels values, the schema has no channel keys."""
    schema = get_recipient_criticality_channel_mapping_schema(defaults={}, values={})
    assert len(_schema_key_strings(schema)) == 0


# ---------------------------------------------------------------------------
# detect_tts_integrations
# ---------------------------------------------------------------------------


def test_detect_tts_integrations_returns_select_options():
    """Each TTS entity is returned as a SelectOptionDict with value and label."""
    hass = MagicMock()
    entity_ids = ["tts.piper", "tts.cloud_say"]

    with patch(
        "ans.channels.channel_manager.detect_tts_entities",
        return_value=entity_ids,
    ):
        results = detect_tts_integrations(hass)

    assert len(results) == 2
    values = [r["value"] for r in results]
    assert "tts.piper" in values
    assert "tts.cloud_say" in values


def test_detect_tts_integrations_formats_label_from_entity_id():
    """Labels are derived from the entity ID by stripping 'tts.' prefix."""
    hass = MagicMock()

    with patch(
        "ans.channels.channel_manager.detect_tts_entities",
        return_value=["tts.my_engine"],
    ):
        results = detect_tts_integrations(hass)

    assert len(results) == 1
    # removeprefix("tts.").replace("_", " ").title() → "My Engine"
    assert results[0]["label"] == "My Engine"
    assert results[0]["value"] == "tts.my_engine"


def test_detect_tts_integrations_empty_when_no_entities():
    """Returns an empty list when no TTS entities are found."""
    hass = MagicMock()

    with patch(
        "ans.channels.channel_manager.detect_tts_entities",
        return_value=[],
    ):
        results = detect_tts_integrations(hass)

    assert results == []
