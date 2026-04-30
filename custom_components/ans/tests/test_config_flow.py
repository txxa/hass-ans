"""Unit tests for ANSConfigFlow and ANSOptionsFlow.

Coverage targets
----------------
ANSConfigFlow
  async_step_user               — sets unique_id, aborts on duplicate, delegates
  async_step_system_settings    — shows form (GET), creates entry (POST),
                                  TTS validation error, FieldValidationError,
                                  vol.Invalid (empty path), vol.Invalid (path),
                                  unknown exception, audit-logging checkbox logic,
                                  reconfigure mode: update + reload + abort
  async_step_reconfigure        — missing context, entry not found, happy path
  async_get_options_flow        — returns ANSOptionsFlow (not ConfigFlow)
  async_get_supported_subentry_types — returns recipient mapping

ANSOptionsFlow
  async_step_init               — shows form (GET), updates options (POST),
                                  ValueError path, unknown exception path
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import OptionsFlow
from homeassistant.data_entry_flow import FlowResultType

from ..config.recipient_flow import RecipientConfigFlow
from ..config.validator import FieldValidationError
from ..config_flow import ANSConfigFlow, ANSOptionsFlow
from ..const import (
    CONFIG_FLOW_ERROR_INVALID_SYSTEM_SETTINGS_KEY,
    CONFIG_FLOW_STEP_SYS_SETTINGS_KEY,
    DOMAIN,
    NAME,
    SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY,
    SYS_CONFIG_ENABLED_CHANNELS_KEY,
    SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY,
    SYS_CONFIG_QUEUE_CONCURRENCY_KEY,
    SYS_CONFIG_RATE_LIMIT_WINDOW_KEY,
    SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY,
    SYS_CONFIG_RETRY_BASE_DELAY_KEY,
    SYS_CONFIG_RETRY_MAX_DELAY_KEY,
    SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY,
    SYS_CONFIG_TTS_SERVICE_KEY,
    SYS_DEFAULT_ENABLE_AUDIT_LOGGING,
    SYS_DEFAULT_GLOBAL_RATE_LIMIT,
    SYS_DEFAULT_QUEUE_CONCURRENCY,
    SYS_DEFAULT_RATE_LIMIT_WINDOW,
    SYS_DEFAULT_RETRY_BACKOFF_FACTOR,
    SYS_DEFAULT_RETRY_BASE_DELAY_SECONDS,
    SYS_DEFAULT_RETRY_MAX_DELAY_SECONDS,
    SYS_STORAGE_DEFAULT_FILE_RETENTION_DAYS,
)

# ---------------------------------------------------------------------------
# Patch targets
# ---------------------------------------------------------------------------
# The test runner loads the integration package as 'ans', not
# 'custom_components.ans'. Patch targets must use this prefix.
_MOD = "ans.config_flow"

_PATCH_VALIDATE_TTS = f"{_MOD}.validate_tts_service"
_PATCH_CONFIG_VALIDATOR = f"{_MOD}.ConfigValidator"
_PATCH_SYSTEM_CONFIG = f"{_MOD}.SystemConfig"
_PATCH_DETECT_CHANNELS = f"{_MOD}.detect_notification_channels"
_PATCH_DETECT_TTS = f"{_MOD}.detect_tts_integrations"
_PATCH_DETECT_MEDIA = f"{_MOD}.detect_media_players"
_PATCH_CHANNEL_OPTIONS = f"{_MOD}.channel_info_to_select_options"
_PATCH_GET_SCHEMA = f"{_MOD}.get_system_config_schema"
_PATCH_GET_OPTIONS_SCHEMA = f"{_MOD}.get_system_options_schema"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_MINIMAL_VALIDATED = {
    SYS_CONFIG_ENABLED_CHANNELS_KEY: ["notify.persistent_notification"],
    SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY: True,
    SYS_CONFIG_TTS_SERVICE_KEY: None,
    SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY: SYS_DEFAULT_GLOBAL_RATE_LIMIT,
    SYS_CONFIG_RATE_LIMIT_WINDOW_KEY: SYS_DEFAULT_RATE_LIMIT_WINDOW,
    SYS_CONFIG_RETRY_BASE_DELAY_KEY: SYS_DEFAULT_RETRY_BASE_DELAY_SECONDS,
    SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY: SYS_DEFAULT_RETRY_BACKOFF_FACTOR,
    SYS_CONFIG_RETRY_MAX_DELAY_KEY: SYS_DEFAULT_RETRY_MAX_DELAY_SECONDS,
    SYS_CONFIG_QUEUE_CONCURRENCY_KEY: SYS_DEFAULT_QUEUE_CONCURRENCY,
    SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY: SYS_STORAGE_DEFAULT_FILE_RETENTION_DAYS,
    "version": 1,
}


def _minimal_user_input() -> dict:
    """Return the minimum valid user_input dict for the system_settings step."""
    return {
        SYS_CONFIG_ENABLED_CHANNELS_KEY: ["notify.persistent_notification"],
        SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY: True,
    }


def _make_flow() -> ANSConfigFlow:
    """Build a minimal ANSConfigFlow wired to a mock hass."""
    flow = ANSConfigFlow()
    hass = MagicMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_entries = MagicMock(return_value=[])
    hass.states.async_entity_ids = MagicMock(return_value=[])
    hass.states.get = MagicMock(return_value=None)
    flow.hass = hass
    flow.flow_id = "test-flow-id"
    flow.handler = DOMAIN
    flow.context = {"source": "user"}
    return flow


def _make_system_config_mock(config_dict: dict | None = None) -> MagicMock:
    """Return a SystemConfig-like mock whose to_dict() returns config_dict."""
    cfg = MagicMock()
    cfg.to_dict.return_value = config_dict or dict(_MINIMAL_VALIDATED)
    return cfg


# ---------------------------------------------------------------------------
# ANSConfigFlow — async_step_user
# ---------------------------------------------------------------------------


class TestAsyncStepUser:
    """Tests for ANSConfigFlow.async_step_user (entry point for new installations)."""

    async def test_delegates_to_system_settings(self):
        """async_step_user sets the unique ID and advances to the system_settings form."""
        flow = _make_flow()
        # async_set_unique_id and _abort_if_unique_id_configured are HA internals;
        # patch them so the test stays unit-level.
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()

        with (
            patch(_PATCH_DETECT_CHANNELS, return_value=[]),
            patch(_PATCH_DETECT_TTS, return_value=[]),
            patch(_PATCH_DETECT_MEDIA, return_value=[]),
            patch(_PATCH_CHANNEL_OPTIONS, return_value=[]),
            patch(_PATCH_GET_SCHEMA, return_value=vol.Schema({})),
        ):
            result = await flow.async_step_user()

        flow.async_set_unique_id.assert_awaited_once_with(DOMAIN)
        flow._abort_if_unique_id_configured.assert_called_once()
        # Must have progressed to the form step
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == CONFIG_FLOW_STEP_SYS_SETTINGS_KEY

    async def test_aborts_if_unique_id_already_configured(self):
        """async_step_user aborts when the integration is already configured."""
        flow = _make_flow()
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock(
            side_effect=lambda: (_ for _ in ()).throw(Exception("already_configured"))
        )

        with pytest.raises(Exception, match="already_configured"):
            await flow.async_step_user()


# ---------------------------------------------------------------------------
# ANSConfigFlow — async_step_system_settings (GET / form display)
# ---------------------------------------------------------------------------


class TestAsyncStepSystemSettingsGet:
    """Tests for the GET (form display) phase of async_step_system_settings."""

    async def test_shows_form_with_no_user_input(self):
        """With no user_input the step returns a FORM result for the settings step."""
        flow = _make_flow()

        with (
            patch(_PATCH_DETECT_CHANNELS, return_value=[]) as m_channels,
            patch(_PATCH_DETECT_TTS, return_value=[]),
            patch(_PATCH_DETECT_MEDIA, return_value=[]),
            patch(_PATCH_CHANNEL_OPTIONS, return_value=[]),
            patch(_PATCH_GET_SCHEMA, return_value=vol.Schema({})),
        ):
            result = await flow.async_step_system_settings(user_input=None)

        m_channels.assert_called_once()
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == CONFIG_FLOW_STEP_SYS_SETTINGS_KEY
        assert result.get("errors", {}) == {}

    async def test_shows_form_with_media_player_when_tts_configured(self):
        """Media player options are added when tts_service is set in defaults."""
        flow = _make_flow()
        # Simulate an existing entry so defaults carry a TTS service
        existing_entry = MagicMock()
        existing_entry.data = {SYS_CONFIG_TTS_SERVICE_KEY: "tts.piper"}
        existing_entry.options = {}
        flow._reconfigure_entry = existing_entry

        mp_info = MagicMock()
        mp_options = [{"value": "media_player.bedroom", "label": "Bedroom"}]

        with (
            patch(_PATCH_DETECT_CHANNELS, return_value=[]),
            patch(_PATCH_DETECT_TTS, return_value=["tts.piper"]),
            patch(_PATCH_DETECT_MEDIA, return_value=[mp_info]),
            patch(
                _PATCH_CHANNEL_OPTIONS, side_effect=lambda x: mp_options if x else []
            ),
            patch(_PATCH_GET_SCHEMA, return_value=vol.Schema({})) as m_schema,
        ):
            await flow.async_step_system_settings(user_input=None)

        # The schema factory must be called (we just verify no error is raised)
        m_schema.assert_called_once()


# ---------------------------------------------------------------------------
# ANSConfigFlow — async_step_system_settings (POST / successful entry creation)
# ---------------------------------------------------------------------------


class TestAsyncStepSystemSettingsCreateEntry:
    """Tests for the POST (entry creation) phase of async_step_system_settings."""

    async def test_creates_entry_on_valid_input(self):
        """Valid user_input creates a config entry with the correct data keys."""
        flow = _make_flow()
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()

        sys_cfg_mock = _make_system_config_mock()

        with (
            patch(_PATCH_VALIDATE_TTS, new=AsyncMock(return_value=None)),
            patch(
                _PATCH_CONFIG_VALIDATOR + ".validate_system_settings_schema",
                return_value=dict(_MINIMAL_VALIDATED),
            ),
            patch(_PATCH_SYSTEM_CONFIG + ".from_dict", return_value=sys_cfg_mock),
        ):
            result = await flow.async_step_system_settings(
                user_input=_minimal_user_input()
            )

        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["title"] == NAME
        assert SYS_CONFIG_ENABLED_CHANNELS_KEY in result["data"]

    async def test_options_split_from_data_on_create(self):
        """Rate limits go into options, not data."""
        flow = _make_flow()
        sys_cfg_mock = _make_system_config_mock()

        with (
            patch(_PATCH_VALIDATE_TTS, new=AsyncMock(return_value=None)),
            patch(
                _PATCH_CONFIG_VALIDATOR + ".validate_system_settings_schema",
                return_value=dict(_MINIMAL_VALIDATED),
            ),
            patch(_PATCH_SYSTEM_CONFIG + ".from_dict", return_value=sys_cfg_mock),
        ):
            result = await flow.async_step_system_settings(
                user_input=_minimal_user_input()
            )

        assert SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY in result["options"]
        assert SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY not in result["data"]

    async def test_audit_logging_defaults_true_on_initial_setup_when_absent(self):
        """Missing audit checkbox → SYS_DEFAULT_ENABLE_AUDIT_LOGGING during initial setup."""
        flow = _make_flow()
        # Omit the audit key, simulating an unchecked checkbox
        user_input = {
            SYS_CONFIG_ENABLED_CHANNELS_KEY: ["notify.persistent_notification"]
        }
        sys_cfg_mock = _make_system_config_mock()

        with (
            patch(_PATCH_VALIDATE_TTS, new=AsyncMock(return_value=None)),
            patch(
                _PATCH_CONFIG_VALIDATOR + ".validate_system_settings_schema",
                return_value=dict(_MINIMAL_VALIDATED),
            ),
            patch(_PATCH_SYSTEM_CONFIG + ".from_dict", return_value=sys_cfg_mock),
        ):
            result = await flow.async_step_system_settings(user_input=user_input)

        assert (
            result["data"][SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY]
            == SYS_DEFAULT_ENABLE_AUDIT_LOGGING
        )


# ---------------------------------------------------------------------------
# ANSConfigFlow — async_step_system_settings (POST / error paths)
# ---------------------------------------------------------------------------


class TestAsyncStepSystemSettingsErrors:
    """Tests for the error-handling paths in async_step_system_settings."""

    async def test_tts_validation_error_shows_form_with_error(self):
        """A TTS validation failure re-renders the form with the field error populated."""
        flow = _make_flow()
        invalid_exc = vol.Invalid("TTS entity not found", path=["tts_service"])

        with (
            patch(_PATCH_VALIDATE_TTS, new=AsyncMock(side_effect=invalid_exc)),
            patch(_PATCH_DETECT_CHANNELS, return_value=[]),
            patch(_PATCH_DETECT_TTS, return_value=[]),
            patch(_PATCH_DETECT_MEDIA, return_value=[]),
            patch(_PATCH_CHANNEL_OPTIONS, return_value=[]),
            patch(_PATCH_GET_SCHEMA, return_value=vol.Schema({})),
        ):
            result = await flow.async_step_system_settings(
                user_input={
                    **_minimal_user_input(),
                    SYS_CONFIG_TTS_SERVICE_KEY: "tts.does_not_exist",
                }
            )

        assert result["type"] == FlowResultType.FORM
        assert SYS_CONFIG_TTS_SERVICE_KEY in result.get("errors", {})

    async def test_field_validation_error_populates_errors(self):
        """FieldValidationError maps field name and key to the errors dict."""
        flow = _make_flow()
        err = FieldValidationError("enabled_channels", "no_channels_selected")

        with (
            patch(_PATCH_VALIDATE_TTS, new=AsyncMock(return_value=None)),
            patch(
                _PATCH_CONFIG_VALIDATOR + ".validate_system_settings_schema",
                side_effect=err,
            ),
            patch(_PATCH_DETECT_CHANNELS, return_value=[]),
            patch(_PATCH_DETECT_TTS, return_value=[]),
            patch(_PATCH_DETECT_MEDIA, return_value=[]),
            patch(_PATCH_CHANNEL_OPTIONS, return_value=[]),
            patch(_PATCH_GET_SCHEMA, return_value=vol.Schema({})),
        ):
            result = await flow.async_step_system_settings(
                user_input=_minimal_user_input()
            )

        assert result["type"] == FlowResultType.FORM
        assert result["errors"].get("enabled_channels") == "no_channels_selected"

    async def test_vol_invalid_with_empty_path_uses_base_key(self):
        """vol.Invalid with empty path must not raise IndexError."""
        flow = _make_flow()
        err = vol.Invalid("general error")  # e.path == []

        with (
            patch(_PATCH_VALIDATE_TTS, new=AsyncMock(return_value=None)),
            patch(
                _PATCH_CONFIG_VALIDATOR + ".validate_system_settings_schema",
                side_effect=err,
            ),
            patch(_PATCH_DETECT_CHANNELS, return_value=[]),
            patch(_PATCH_DETECT_TTS, return_value=[]),
            patch(_PATCH_DETECT_MEDIA, return_value=[]),
            patch(_PATCH_CHANNEL_OPTIONS, return_value=[]),
            patch(_PATCH_GET_SCHEMA, return_value=vol.Schema({})),
        ):
            result = await flow.async_step_system_settings(
                user_input=_minimal_user_input()
            )

        assert result["type"] == FlowResultType.FORM
        assert "base" in result.get("errors", {})

    async def test_vol_invalid_with_path_extracts_field_and_message(self):
        """vol.Invalid with non-empty path maps first element to field key."""
        flow = _make_flow()
        err = vol.Invalid("value out of range", path=["global_rate_limit", "too_large"])

        with (
            patch(_PATCH_VALIDATE_TTS, new=AsyncMock(return_value=None)),
            patch(
                _PATCH_CONFIG_VALIDATOR + ".validate_system_settings_schema",
                side_effect=err,
            ),
            patch(_PATCH_DETECT_CHANNELS, return_value=[]),
            patch(_PATCH_DETECT_TTS, return_value=[]),
            patch(_PATCH_DETECT_MEDIA, return_value=[]),
            patch(_PATCH_CHANNEL_OPTIONS, return_value=[]),
            patch(_PATCH_GET_SCHEMA, return_value=vol.Schema({})),
        ):
            result = await flow.async_step_system_settings(
                user_input=_minimal_user_input()
            )

        errors = result.get("errors", {})
        assert "global_rate_limit" in errors
        assert errors["global_rate_limit"] == "too_large"

    async def test_unknown_exception_returns_base_error(self):
        """An unexpected exception maps to the generic 'base' error key."""
        flow = _make_flow()

        with (
            patch(_PATCH_VALIDATE_TTS, new=AsyncMock(return_value=None)),
            patch(
                _PATCH_CONFIG_VALIDATOR + ".validate_system_settings_schema",
                side_effect=RuntimeError("unexpected"),
            ),
            patch(_PATCH_DETECT_CHANNELS, return_value=[]),
            patch(_PATCH_DETECT_TTS, return_value=[]),
            patch(_PATCH_DETECT_MEDIA, return_value=[]),
            patch(_PATCH_CHANNEL_OPTIONS, return_value=[]),
            patch(_PATCH_GET_SCHEMA, return_value=vol.Schema({})),
        ):
            result = await flow.async_step_system_settings(
                user_input=_minimal_user_input()
            )

        assert result["type"] == FlowResultType.FORM
        assert (
            result["errors"].get("base")
            == CONFIG_FLOW_ERROR_INVALID_SYSTEM_SETTINGS_KEY
        )


# ---------------------------------------------------------------------------
# ANSConfigFlow — async_step_reconfigure
# ---------------------------------------------------------------------------


class TestAsyncStepReconfigure:
    """Tests for ANSConfigFlow.async_step_reconfigure (editing an existing entry)."""

    async def test_aborts_when_no_entry_id_in_context(self):
        """Missing entry_id in context causes an abort with the appropriate reason."""
        flow = _make_flow()
        flow.context = {}  # No entry_id

        result = await flow.async_step_reconfigure(user_input=None)

        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "reconfigure_entry_not_found"

    async def test_aborts_when_entry_not_found(self):
        """An unresolvable entry_id causes an abort with the appropriate reason."""
        flow = _make_flow()
        flow.context = {"entry_id": "missing-id"}
        flow.hass.config_entries.async_get_entry = MagicMock(return_value=None)

        result = await flow.async_step_reconfigure(user_input=None)

        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "reconfigure_entry_not_found"

    async def test_happy_path_shows_form_with_entry_loaded(self):
        """A valid entry_id loads the existing entry and renders the reconfigure form."""
        flow = _make_flow()
        entry_mock = MagicMock()
        entry_mock.entry_id = "existing-id"
        entry_mock.data = {
            SYS_CONFIG_ENABLED_CHANNELS_KEY: ["notify.persistent_notification"],
            SYS_CONFIG_TTS_SERVICE_KEY: None,
        }
        entry_mock.options = {}
        flow.context = {"entry_id": "existing-id"}
        flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry_mock)

        with (
            patch(_PATCH_DETECT_CHANNELS, return_value=[]),
            patch(_PATCH_DETECT_TTS, return_value=[]),
            patch(_PATCH_DETECT_MEDIA, return_value=[]),
            patch(_PATCH_CHANNEL_OPTIONS, return_value=[]),
            patch(_PATCH_GET_SCHEMA, return_value=vol.Schema({})),
        ):
            result = await flow.async_step_reconfigure(user_input=None)

        assert result["type"] == FlowResultType.FORM
        assert flow._reconfigure_entry is entry_mock

    async def test_reconfigure_success_updates_entry_and_aborts(self):
        """Valid reconfigure input updates the entry and aborts with 'reconfigure_successful'."""
        flow = _make_flow()
        entry_mock = MagicMock()
        entry_mock.entry_id = "existing-id"
        entry_mock.data = {
            SYS_CONFIG_ENABLED_CHANNELS_KEY: ["notify.persistent_notification"],
            SYS_CONFIG_TTS_SERVICE_KEY: None,
            SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY: True,
        }
        entry_mock.options = {
            SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY: 100,
            SYS_CONFIG_RATE_LIMIT_WINDOW_KEY: 60,
        }
        flow.context = {"entry_id": "existing-id"}
        flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry_mock)
        flow.hass.config_entries.async_update_entry = MagicMock()
        flow.hass.config_entries.async_reload = AsyncMock()

        sys_cfg_mock = _make_system_config_mock()

        with (
            patch(_PATCH_VALIDATE_TTS, new=AsyncMock(return_value=None)),
            patch(
                _PATCH_CONFIG_VALIDATOR + ".validate_system_settings_schema",
                return_value=dict(_MINIMAL_VALIDATED),
            ),
            patch(_PATCH_SYSTEM_CONFIG + ".from_dict", return_value=sys_cfg_mock),
        ):
            result = await flow.async_step_reconfigure(user_input=_minimal_user_input())

        flow.hass.config_entries.async_update_entry.assert_called_once()
        flow.hass.config_entries.async_reload.assert_awaited_once_with("existing-id")
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"

    async def test_reconfigure_audit_defaults_to_false_when_checkbox_absent(self):
        """In reconfigure mode, absent audit key → False."""
        flow = _make_flow()
        entry_mock = MagicMock()
        entry_mock.entry_id = "existing-id"
        entry_mock.data = {SYS_CONFIG_ENABLED_CHANNELS_KEY: []}
        entry_mock.options = {}
        flow.context = {"entry_id": "existing-id"}
        flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry_mock)
        flow.hass.config_entries.async_update_entry = MagicMock()
        flow.hass.config_entries.async_reload = AsyncMock()

        # Omit audit key from user_input
        user_input = {
            SYS_CONFIG_ENABLED_CHANNELS_KEY: ["notify.persistent_notification"]
        }
        sys_cfg_mock = _make_system_config_mock()

        with (
            patch(_PATCH_VALIDATE_TTS, new=AsyncMock(return_value=None)),
            patch(
                _PATCH_CONFIG_VALIDATOR + ".validate_system_settings_schema",
                return_value=dict(_MINIMAL_VALIDATED),
            ),
            patch(_PATCH_SYSTEM_CONFIG + ".from_dict", return_value=sys_cfg_mock),
        ):
            await flow.async_step_reconfigure(user_input=user_input)

        call_kwargs = flow.hass.config_entries.async_update_entry.call_args.kwargs
        assert call_kwargs["data"][SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY] is False


# ---------------------------------------------------------------------------
# ANSConfigFlow — metadata / class-level behaviour
# ---------------------------------------------------------------------------


class TestConfigFlowMetadata:
    """Tests for class-level metadata and factory methods on ANSConfigFlow."""

    def test_get_options_flow_returns_options_flow_subclass(self):
        """async_get_options_flow must return an OptionsFlow, not a ConfigFlow."""
        entry = MagicMock()
        result = ANSConfigFlow.async_get_options_flow(entry)
        assert isinstance(result, OptionsFlow)
        assert isinstance(result, ANSOptionsFlow)

    def test_get_options_flow_requires_no_constructor_arg(self):
        """ANSOptionsFlow() must be instantiable without arguments."""
        instance = ANSOptionsFlow()
        assert instance is not None

    def test_get_supported_subentry_types_includes_recipient(self):
        """async_get_supported_subentry_types must include 'recipient' mapped to RecipientConfigFlow."""
        entry = MagicMock()

        types = ANSConfigFlow.async_get_supported_subentry_types(entry)
        assert "recipient" in types
        assert types["recipient"] is RecipientConfigFlow


# ---------------------------------------------------------------------------
# ANSOptionsFlow — async_step_init
# ---------------------------------------------------------------------------


def _make_options_flow() -> ANSOptionsFlow:
    """Return an ANSOptionsFlow with a mock config entry attached."""
    flow = ANSOptionsFlow()
    flow.hass = MagicMock()
    flow.flow_id = "opt-flow-id"
    flow.handler = "existing-entry-id"

    entry = MagicMock()
    entry.entry_id = "existing-entry-id"
    entry.data = {
        SYS_CONFIG_ENABLED_CHANNELS_KEY: ["notify.persistent_notification"],
        SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY: True,
    }
    entry.options = {
        SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY: SYS_DEFAULT_GLOBAL_RATE_LIMIT,
        SYS_CONFIG_RATE_LIMIT_WINDOW_KEY: SYS_DEFAULT_RATE_LIMIT_WINDOW,
    }
    flow.hass.config_entries.async_get_known_entry = MagicMock(return_value=entry)
    return flow


class TestANSOptionsFlowInit:
    """Tests for ANSOptionsFlow.async_step_init (tunable runtime settings)."""

    async def test_shows_form_on_get(self):
        """With no user_input the step returns a FORM result for the init step."""
        flow = _make_options_flow()

        with patch(_PATCH_GET_OPTIONS_SCHEMA, return_value=vol.Schema({})):
            result = await flow.async_step_init(user_input=None)

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "init"
        assert result.get("errors", {}) == {}

    async def test_creates_entry_on_valid_input(self):
        """Valid options input creates an entry containing the tunable settings."""
        flow = _make_options_flow()
        valid_options = {
            SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY: 50,
            SYS_CONFIG_RATE_LIMIT_WINDOW_KEY: 60,
            SYS_CONFIG_RETRY_BASE_DELAY_KEY: 30,
            SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY: 2.0,
            SYS_CONFIG_RETRY_MAX_DELAY_KEY: 3600,
            SYS_CONFIG_QUEUE_CONCURRENCY_KEY: 5,
        }

        with (
            patch(
                _PATCH_CONFIG_VALIDATOR + ".validate_system_settings_schema",
                return_value=dict(_MINIMAL_VALIDATED),
            ),
            patch(_PATCH_SYSTEM_CONFIG + ".from_dict", return_value=MagicMock()),
        ):
            result = await flow.async_step_init(user_input=valid_options)

        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY in result["data"]

    async def test_all_tunable_keys_present_in_options_output(self):
        """Every tunable parameter should end up in the saved options."""
        flow = _make_options_flow()

        with (
            patch(
                _PATCH_CONFIG_VALIDATOR + ".validate_system_settings_schema",
                return_value=dict(_MINIMAL_VALIDATED),
            ),
            patch(_PATCH_SYSTEM_CONFIG + ".from_dict", return_value=MagicMock()),
        ):
            result = await flow.async_step_init(
                user_input={SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY: 50}
            )

        options = result["data"]
        expected_keys = {
            SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY,
            SYS_CONFIG_RATE_LIMIT_WINDOW_KEY,
            SYS_CONFIG_RETRY_BASE_DELAY_KEY,
            SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY,
            SYS_CONFIG_RETRY_MAX_DELAY_KEY,
            SYS_CONFIG_QUEUE_CONCURRENCY_KEY,
            SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY,
        }
        assert expected_keys.issubset(options.keys())

    async def test_value_error_shows_form_with_base_error(self):
        """A ValueError from validation re-renders the form with 'invalid_system_settings'."""
        flow = _make_options_flow()

        with (
            patch(
                _PATCH_CONFIG_VALIDATOR + ".validate_system_settings_schema",
                side_effect=ValueError("bad value"),
            ),
            patch(_PATCH_GET_OPTIONS_SCHEMA, return_value=vol.Schema({})),
        ):
            result = await flow.async_step_init(
                user_input={SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY: -1}
            )

        assert result["type"] == FlowResultType.FORM
        assert result["errors"].get("base") == "invalid_system_settings"

    async def test_unknown_exception_shows_form_with_unknown_error(self):
        """An unexpected exception re-renders the form with the 'unknown' base error."""
        flow = _make_options_flow()

        with (
            patch(
                _PATCH_CONFIG_VALIDATOR + ".validate_system_settings_schema",
                side_effect=RuntimeError("oops"),
            ),
            patch(_PATCH_GET_OPTIONS_SCHEMA, return_value=vol.Schema({})),
        ):
            result = await flow.async_step_init(
                user_input={SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY: 50}
            )

        assert result["type"] == FlowResultType.FORM
        assert result["errors"].get("base") == "unknown"

    async def test_retention_field_shown_when_audit_enabled(self):
        """get_system_options_schema is called with include_audit_retention=True."""
        flow = _make_options_flow()

        with patch(_PATCH_GET_OPTIONS_SCHEMA, return_value=vol.Schema({})) as m_schema:
            await flow.async_step_init(user_input=None)

        call_kwargs = m_schema.call_args.kwargs
        assert call_kwargs.get("include_audit_retention") is True

    async def test_retention_field_hidden_when_audit_disabled(self):
        """get_system_options_schema is called with include_audit_retention=False."""
        flow = _make_options_flow()
        # Override audit flag in entry data
        flow.config_entry.data = {
            SYS_CONFIG_ENABLED_CHANNELS_KEY: [],
            SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY: False,
        }

        with patch(_PATCH_GET_OPTIONS_SCHEMA, return_value=vol.Schema({})) as m_schema:
            await flow.async_step_init(user_input=None)

        call_kwargs = m_schema.call_args.kwargs
        assert call_kwargs.get("include_audit_retention") is False
