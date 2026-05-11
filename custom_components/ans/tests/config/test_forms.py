"""Unit tests for config forms (schema factories in forms.py)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import voluptuous as vol

from ...config.forms import (
    detect_tts_integrations,
    get_recipient_basic_settings_schema,
    get_recipient_criticality_channel_mapping_schema,
    get_recipient_definition_schema,
    get_system_config_schema,
    get_system_options_schema,
)
from ...config.validator import ValidationContext
from ...const import (
    RCPT_CONFIG_CHANNELS_KEY,
    RCPT_CONFIG_CONFIGURED_CHANNELS_KEY,
    RCPT_CONFIG_CRITICALITY_LEVELS_KEY,
    RCPT_CONFIG_EMAIL_KEY,
    RCPT_CONFIG_NAME_KEY,
    RCPT_CONFIG_PHONE_KEY,
    RCPT_MAX_RETRY_ATTEMPTS,
    SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY,
    SYS_CONFIG_ENABLED_CHANNELS_KEY,
    SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY,
    SYS_CONFIG_QUEUE_MAX_DEPTH_KEY,
    SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY,
    SYS_CONFIG_RETRY_BASE_DELAY_KEY,
    SYS_CONFIG_RETRY_MAX_DELAY_KEY,
    SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY,
    SYS_CONFIG_TTS_SERVICE_KEY,
    SYS_DEFAULT_GLOBAL_RATE_LIMIT,
    SYS_DEFAULT_QUEUE_MAX_DEPTH,
    SYS_DEFAULT_RETRY_BACKOFF_FACTOR,
    SYS_DEFAULT_RETRY_BASE_DELAY_SECONDS,
    SYS_DEFAULT_RETRY_MAX_DELAY_SECONDS,
    SYS_MAX_GLOBAL_RATE_LIMIT,
    SYS_STORAGE_DEFAULT_FILE_RETENTION_DAYS,
)
from ...models import NotificationCriticality, RecipientType

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
        SYS_CONFIG_QUEUE_MAX_DEPTH_KEY: SYS_DEFAULT_QUEUE_MAX_DEPTH,
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
    ("field", "bad_value"),
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


# ---------------------------------------------------------------------------
# get_system_config_schema
# ---------------------------------------------------------------------------


class TestGetSystemConfigSchema:
    """Tests for get_system_config_schema (system-wide config form schema builder)."""

    def test_with_rate_limits_includes_global_rate_limit_key(self):
        """When include_rate_limits=True the schema contains global_rate_limit."""
        schema = get_system_config_schema(
            defaults={}, values={}, include_rate_limits=True
        )
        assert SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY in _schema_key_strings(schema)

    def test_without_rate_limits_excludes_global_rate_limit_key(self):
        """When include_rate_limits=False the schema omits global_rate_limit."""
        schema = get_system_config_schema(
            defaults={}, values={}, include_rate_limits=False
        )
        assert SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY not in _schema_key_strings(schema)

    def test_always_includes_enabled_channels_key(self):
        """The enabled_channels key is always present regardless of other flags."""
        for rate_limits in (True, False):
            schema = get_system_config_schema(
                defaults={}, values={}, include_rate_limits=rate_limits
            )
            assert SYS_CONFIG_ENABLED_CHANNELS_KEY in _schema_key_strings(schema)

    def test_with_tts_services_adds_tts_service_key(self):
        """When values contains 'tts_services', the tts_service selector is added."""
        tts_service = [{"value": "tts.piper", "label": "Piper"}]
        schema = get_system_config_schema(
            defaults={},
            values={"tts_services": tts_service},
            include_rate_limits=False,
        )
        assert SYS_CONFIG_TTS_SERVICE_KEY in _schema_key_strings(schema)

    def test_without_tts_services_omits_tts_service_key(self):
        """When values has no 'tts_services' entry the tts_service key is absent."""
        schema = get_system_config_schema(
            defaults={}, values={}, include_rate_limits=False
        )
        assert SYS_CONFIG_TTS_SERVICE_KEY not in _schema_key_strings(schema)

    def test_with_audit_logging_adds_enable_audit_logging_key(self):
        """When include_audit_logging=True the schema contains enable_audit_logging."""
        schema = get_system_config_schema(
            defaults={},
            values={},
            include_rate_limits=False,
            include_audit_logging=True,
        )
        assert SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY in _schema_key_strings(schema)

    def test_without_audit_logging_omits_enable_audit_logging_key(self):
        """When include_audit_logging=False (default) enable_audit_logging is absent."""
        schema = get_system_config_schema(
            defaults={},
            values={},
            include_rate_limits=False,
            include_audit_logging=False,
        )
        assert SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY not in _schema_key_strings(schema)

    def test_defaults_none_treated_as_empty_dict(self):
        """Passing defaults=None does not raise and yields the same schema as defaults={}."""
        schema_none = get_system_config_schema(
            defaults=None, values=None, include_rate_limits=False
        )
        schema_empty = get_system_config_schema(
            defaults={}, values={}, include_rate_limits=False
        )
        assert _schema_key_strings(schema_none) == _schema_key_strings(schema_empty)

    def test_tts_service_selector_contains_disabled_option(self):
        """When TTS services are present the '(Disabled)' sentinel option is included."""
        tts_service = [{"value": "tts.piper", "label": "Piper"}]
        schema = get_system_config_schema(
            defaults={},
            values={"tts_services": tts_service},
            include_rate_limits=False,
        )
        for key, selector_obj in schema.schema.items():
            if getattr(key, "schema", None) == SYS_CONFIG_TTS_SERVICE_KEY:
                config = getattr(selector_obj, "config", {})
                options = (
                    config.get("options", [])
                    if isinstance(config, dict)
                    else getattr(config, "options", [])
                )
                option_values = [
                    (o["value"] if isinstance(o, dict) else o.value) for o in options
                ]
                assert "None" in option_values
                assert "tts.piper" in option_values
                return
        raise AssertionError(f"Key '{SYS_CONFIG_TTS_SERVICE_KEY}' not found in schema")
