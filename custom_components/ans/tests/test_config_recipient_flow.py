"""Unit tests for RecipientConfigFlow.

Covers every step method and all key private helpers, including success paths,
validation-error paths, and unexpected-exception handlers.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.data_entry_flow import FlowResultType

from ..config.recipient_flow import RecipientConfigFlow
from ..config.validator import ConfigValidator, FieldValidationError
from ..const import (
    PERSISTENT_NOTIFICATION_CHANNEL,
    RCPT_CONFIG_EMAIL_KEY,
    RCPT_CONFIG_ID_KEY,
    RCPT_CONFIG_NAME_KEY,
    RCPT_CONFIG_PHONE_KEY,
    RCPT_CONFIG_RECIPIENT_CHOICE_KEY,
    RCPT_CONFIG_TYPE_KEY,
    RCPT_CONFIG_USER_KEY,
    RECIPIENT_CHOICE_GENERIC,
    RECIPIENT_CHOICE_HA_USER,
    RECIPIENT_CHOICE_SYSTEM_HA,
    RECIPIENT_CHOICE_TTS,
    SUBENTRY_FLOW_ERROR_INVALID_CHANNEL_MAPPING_KEY,
    SUBENTRY_FLOW_ERROR_INVALID_RECIPIENT_DEFINITION_KEY,
    SUBENTRY_FLOW_ERROR_INVALID_RECIPIENT_SELECTION_KEY,
    SUBENTRY_FLOW_ERROR_INVALID_RECIPIENT_SETTINGS_KEY,
    SUBENTRY_FLOW_STEP_RECIPIENT_BASIC_SETTINGS_KEY,
    SUBENTRY_FLOW_STEP_RECIPIENT_CHANNEL_MAPPING_KEY,
    SUBENTRY_FLOW_STEP_RECIPIENT_DEFINITION_KEY,
    SUBENTRY_FLOW_STEP_RECIPIENT_DND_SETTINGS_KEY,
    SUBENTRY_FLOW_STEP_RECIPIENT_SELECTION_KEY,
    SUBENTRY_FLOW_STEP_RECIPIENT_TTS_SETTINGS_KEY,
    SYS_DEFAULT_SYSTEM_RECIPIENT_NAME,
)
from ..models import ChannelInfo, ChannelScope, RecipientType, SystemConfig

# ---------------------------------------------------------------------------
# Patch target constants
# ---------------------------------------------------------------------------

_PATCH_GET_MAIN_ENTRY = "ans.config.recipient_flow.get_main_entry"
_PATCH_CHECK_NAME = "ans.config.recipient_flow.check_recipient_name_availability"
_PATCH_GET_CONFIG_REPO = "ans.get_config_repository"

# ---------------------------------------------------------------------------
# Shared factory helpers
# ---------------------------------------------------------------------------


def _sys_config_dict(tts_service: str | None = None) -> dict:
    return {
        "global_rate_limit": 100,
        "enabled_channels": [PERSISTENT_NOTIFICATION_CHANNEL],
        "persistent_notifications_enabled": True,
        "retry_base_delay": 60,
        "retry_backoff_factor": 2.0,
        "retry_max_delay": 3600,
        "enable_audit_logging": True,
        "tts_service": tts_service,
        "rate_limit_window": 60,
    }


def _make_main_entry(tts_service: str | None = None) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "main_entry_id"
    entry.data = _sys_config_dict(tts_service=tts_service)
    entry.subentries = {}
    return entry


def _make_flow(tts_service: str | None = None) -> tuple[RecipientConfigFlow, MagicMock]:
    """Return a (flow, main_entry) pair ready for testing."""
    flow = RecipientConfigFlow()
    hass = MagicMock()
    hass.auth.async_get_users = AsyncMock(return_value=[])
    flow.hass = hass
    flow.flow_id = "test_flow_id"
    flow.handler = "ans"
    # FlowHandler.context is a class-level mappingproxy; shadow it at instance
    # level so the source property returns "user" when async_create_entry checks it.
    flow.context = {"source": "user"}
    return flow, _make_main_entry(tts_service=tts_service)


def _prime_with_meta(
    flow: RecipientConfigFlow,
    main_entry: MagicMock,
    recipient_type: RecipientType = RecipientType.GENERIC,
) -> None:
    """Simulate having completed the selection step."""
    flow._main_entry = main_entry
    flow.system_config = SystemConfig.from_dict(_sys_config_dict())
    flow._recipient_meta = {
        RCPT_CONFIG_ID_KEY: "rcpt-uuid",
        RCPT_CONFIG_TYPE_KEY: recipient_type.value,
        RCPT_CONFIG_NAME_KEY: "Alice",
        RCPT_CONFIG_EMAIL_KEY: None,
        RCPT_CONFIG_PHONE_KEY: None,
    }


def _prime_for_channel_mapping(
    flow: RecipientConfigFlow,
    main_entry: MagicMock,
    recipient_type: RecipientType = RecipientType.GENERIC,
) -> None:
    """Simulate having completed basic settings."""
    _prime_with_meta(flow, main_entry, recipient_type)
    flow._recipient_settings.update(
        {
            "retry_attempts": 2,
            "rate_limit": 10,
            "notification_types": ["INFO"],
        }
    )


def _prime_for_dnd(
    flow: RecipientConfigFlow,
    main_entry: MagicMock,
) -> None:
    """Simulate having completed channel mapping (VIRTUAL recipient)."""
    _prime_for_channel_mapping(flow, main_entry)
    flow._recipient_settings.update(
        {
            "channels_low": [PERSISTENT_NOTIFICATION_CHANNEL],
            "channels_medium": [],
            "channels_high": [],
            "channels_critical": [],
        }
    )


def _complete_settings() -> dict:
    """Return a fully-populated _recipient_settings dict for _create_recipient_entry."""
    return {
        "recipient_id": "rcpt-uuid",
        "retry_attempts": 2,
        "rate_limit": 10,
        "notification_types": ["INFO"],
        "channels_low": [PERSISTENT_NOTIFICATION_CHANNEL],
        "channels_medium": [],
        "channels_high": [],
        "channels_critical": [],
        "dnd_enabled": False,
        "dnd_start": None,
        "dnd_end": None,
        "dnd_allowed_sources_regex": None,
        "dnd_allowed_criticalities": None,
        "dnd_allowed_types": None,
        "blocked_sources_regex": None,
        "tts_settings": None,
    }


def _get_schema_field(result: dict, field_name: str) -> tuple[Any, Any]:
    """Return the schema marker and selector object for `field_name`."""
    schema_dict = result["data_schema"].schema
    for marker, selector_obj in schema_dict.items():
        if getattr(marker, "schema", None) == field_name:
            return marker, selector_obj
    raise AssertionError(f"Field '{field_name}' not found in schema")


def _selector_option_values(selector_obj: Any) -> list[str]:
    """Extract option values from a SelectSelector-like object."""
    options = getattr(getattr(selector_obj, "config", None), "options", [])
    values: list[str] = []
    for option in options:
        if isinstance(option, dict):
            values.append(option["value"])
        else:
            values.append(option.value)
    return values


# ===========================================================================
# async_step_user
# ===========================================================================


class TestAsyncStepUser:
    async def test_abort_when_no_main_entry(self):
        """No ANS config entry → immediate abort."""
        flow, _ = _make_flow()
        with patch(_PATCH_GET_MAIN_ENTRY, return_value=None):
            result = await flow.async_step_user()

        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "no_main_entry"

    async def test_shows_selection_form_when_entry_found(self):
        """A found main entry redirects to the recipient_selection form."""
        flow, main_entry = _make_flow()
        with patch(_PATCH_GET_MAIN_ENTRY, return_value=main_entry):
            result = await flow.async_step_user()

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_SELECTION_KEY

    async def test_populates_system_config_from_entry(self):
        """system_config is populated with the main entry's global_rate_limit."""
        flow, main_entry = _make_flow()
        with patch(_PATCH_GET_MAIN_ENTRY, return_value=main_entry):
            await flow.async_step_user()

        assert flow.system_config is not None
        assert flow.system_config.global_rate_limit == 100


# ===========================================================================
# async_step_recipient_selection
# ===========================================================================


class TestAsyncStepRecipientSelection:
    async def test_no_input_shows_selection_form_without_errors(self):
        """Calling with None returns the form with an empty error dict."""
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)
        with patch(_PATCH_GET_MAIN_ENTRY, return_value=main_entry):
            result = await flow.async_step_recipient_selection(None)

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_SELECTION_KEY
        assert result["errors"] == {}

    async def test_virtual_choice_routes_to_definition(self):
        """Choosing VIRTUAL sets type in meta and shows the definition form."""
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)
        with patch(_PATCH_GET_MAIN_ENTRY, return_value=main_entry):
            result = await flow.async_step_recipient_selection(
                {RCPT_CONFIG_RECIPIENT_CHOICE_KEY: RECIPIENT_CHOICE_GENERIC}
            )

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_DEFINITION_KEY
        assert flow._recipient_meta[RCPT_CONFIG_TYPE_KEY] == RecipientType.GENERIC.value

    async def test_tts_choice_routes_to_definition(self):
        """Choosing TTS (when tts_service is configured) sets type and shows definition form."""
        flow, main_entry = _make_flow(tts_service="tts.cloud_say")
        flow._main_entry = main_entry
        flow.system_config = SystemConfig.from_dict(
            _sys_config_dict(tts_service="tts.cloud_say")
        )
        with patch(_PATCH_GET_MAIN_ENTRY, return_value=main_entry):
            result = await flow.async_step_recipient_selection(
                {RCPT_CONFIG_RECIPIENT_CHOICE_KEY: RECIPIENT_CHOICE_TTS}
            )

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_DEFINITION_KEY
        assert flow._recipient_meta[RCPT_CONFIG_TYPE_KEY] == RecipientType.TTS.value

    async def test_system_choice_when_not_exists_transitions_to_basic_settings(self):
        """Choosing SYSTEM when none exists calls _setup_system_recipient → basic settings."""
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)
        with patch(_PATCH_GET_MAIN_ENTRY, return_value=main_entry):
            result = await flow.async_step_recipient_selection(
                {RCPT_CONFIG_RECIPIENT_CHOICE_KEY: RECIPIENT_CHOICE_SYSTEM_HA}
            )

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_BASIC_SETTINGS_KEY
        assert flow._recipient_meta[RCPT_CONFIG_TYPE_KEY] == RecipientType.SYSTEM.value
        assert (
            flow._recipient_meta[RCPT_CONFIG_NAME_KEY]
            == SYS_DEFAULT_SYSTEM_RECIPIENT_NAME
        )

    async def test_system_choice_when_already_exists_shows_error(self):
        """Choosing SYSTEM when it already exists shows system_recipient_already_exists."""
        flow, main_entry = _make_flow()
        existing_sub = MagicMock()
        existing_sub.data = {RCPT_CONFIG_TYPE_KEY: RecipientType.SYSTEM.value}
        main_entry.subentries = {"s1": existing_sub}
        _prime_with_meta(flow, main_entry)

        with patch(_PATCH_GET_MAIN_ENTRY, return_value=main_entry):
            result = await flow.async_step_recipient_selection(
                {RCPT_CONFIG_RECIPIENT_CHOICE_KEY: RECIPIENT_CHOICE_SYSTEM_HA}
            )

        assert result["type"] == FlowResultType.FORM
        assert (
            result["errors"].get(RCPT_CONFIG_RECIPIENT_CHOICE_KEY)
            == "system_recipient_already_exists"
        )

    async def test_ha_user_choice_sets_ha_user_type_and_shows_definition(self):
        """Choosing HA_USER sets type in meta and shows the definition form with user dropdown."""
        flow, main_entry = _make_flow()
        ha_user = MagicMock()
        ha_user.id = "user123"
        ha_user.name = "Bob"
        ha_user.credentials = []
        flow.hass.auth.async_get_users = AsyncMock(return_value=[ha_user])
        _prime_with_meta(flow, main_entry)

        with patch(_PATCH_GET_MAIN_ENTRY, return_value=main_entry):
            result = await flow.async_step_recipient_selection(
                {RCPT_CONFIG_RECIPIENT_CHOICE_KEY: RECIPIENT_CHOICE_HA_USER}
            )

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_DEFINITION_KEY
        assert flow._recipient_meta[RCPT_CONFIG_TYPE_KEY] == RecipientType.HA_USER.value
        _, user_selector = _get_schema_field(result, RCPT_CONFIG_USER_KEY)
        assert "user123" in _selector_option_values(user_selector)

    async def test_invalid_choice_shows_error_on_selection_form(self):
        """An unrecognised choice shows invalid_selection on the form field."""
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)

        with patch(_PATCH_GET_MAIN_ENTRY, return_value=main_entry):
            result = await flow.async_step_recipient_selection(
                {RCPT_CONFIG_RECIPIENT_CHOICE_KEY: "completely_unknown_xyz"}
            )

        assert result["type"] == FlowResultType.FORM
        assert (
            result["errors"].get(RCPT_CONFIG_RECIPIENT_CHOICE_KEY)
            == "invalid_selection"
        )

    async def test_unexpected_exception_sets_base_error(self):
        """An unhandled exception during choice processing surfaces as a base error.

        _system_recipient_exists is called twice: once inside the try block (which
        raises) and once outside for form rebuild (which succeeds).
        """
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)

        # First call (inside try) raises; second call (form rebuild) returns False
        mock_exists = AsyncMock(side_effect=[RuntimeError("db down"), False])
        with patch(_PATCH_GET_MAIN_ENTRY, return_value=main_entry):
            with patch.object(flow, "_system_recipient_exists", mock_exists):
                result = await flow.async_step_recipient_selection(
                    {RCPT_CONFIG_RECIPIENT_CHOICE_KEY: RECIPIENT_CHOICE_SYSTEM_HA}
                )

        assert result["type"] == FlowResultType.FORM
        assert (
            result["errors"].get("base")
            == SUBENTRY_FLOW_ERROR_INVALID_RECIPIENT_SELECTION_KEY
        )


# ===========================================================================
# async_step_recipient_definition
# ===========================================================================


class TestAsyncStepRecipientDefinition:
    async def test_no_input_shows_definition_form_without_errors(self):
        """Calling with None returns the definition form with an empty error dict."""
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)
        result = await flow.async_step_recipient_definition(None)

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_DEFINITION_KEY
        assert result["errors"] == {}

    async def test_valid_input_stores_name_and_proceeds_to_basic_settings(self):
        """Valid definition input stores name in meta and shows basic settings form."""
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)

        with patch(_PATCH_CHECK_NAME, return_value=True):
            result = await flow.async_step_recipient_definition(
                {RCPT_CONFIG_NAME_KEY: "Alice"}
            )

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_BASIC_SETTINGS_KEY
        assert flow._recipient_meta[RCPT_CONFIG_NAME_KEY] == "Alice"

    async def test_name_already_taken_re_shows_form_with_name_error(self):
        """When check_availability returns False the form is re-shown with a name error."""
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)

        with patch(_PATCH_CHECK_NAME, return_value=False):
            result = await flow.async_step_recipient_definition(
                {RCPT_CONFIG_NAME_KEY: "TakenName"}
            )

        assert result["type"] == FlowResultType.FORM
        assert RCPT_CONFIG_NAME_KEY in result["errors"]

    async def test_invalid_email_format_re_shows_form_with_error(self):
        """An invalid email triggers vol.Invalid and re-shows the form with errors."""
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)

        with patch(_PATCH_CHECK_NAME, return_value=True):
            result = await flow.async_step_recipient_definition(
                {
                    RCPT_CONFIG_NAME_KEY: "Alice",
                    RCPT_CONFIG_EMAIL_KEY: "not-valid-email",
                }
            )

        assert result["type"] == FlowResultType.FORM
        assert result["errors"] != {}

    async def test_unexpected_exception_sets_base_error(self):
        """An unhandled exception (e.g. auth unavailable) shows the base error."""
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)

        with patch(_PATCH_CHECK_NAME, side_effect=RuntimeError("auth unavailable")):
            result = await flow.async_step_recipient_definition(
                {RCPT_CONFIG_NAME_KEY: "Alice"}
            )

        assert result["type"] == FlowResultType.FORM
        assert (
            result["errors"].get("base")
            == SUBENTRY_FLOW_ERROR_INVALID_RECIPIENT_DEFINITION_KEY
        )


# ===========================================================================
# async_step_recipient_basic_settings
# ===========================================================================


class TestAsyncStepRecipientBasicSettings:
    async def test_no_input_shows_basic_settings_form(self):
        """Calling with None returns the basic settings form."""
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)
        result = await flow.async_step_recipient_basic_settings(None)

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_BASIC_SETTINGS_KEY

    async def test_valid_input_for_virtual_goes_to_channel_mapping(self):
        """Valid settings for a VIRTUAL recipient proceed to channel mapping."""
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)

        with patch(_PATCH_GET_CONFIG_REPO, return_value=None):
            result = await flow.async_step_recipient_basic_settings(
                {"retry_attempts": 2, "rate_limit": 10, "notification_types": ["INFO"]}
            )

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_CHANNEL_MAPPING_KEY
        assert flow._recipient_settings["retry_attempts"] == 2

    async def test_valid_input_for_tts_goes_to_tts_settings(self):
        """Valid settings for a TTS recipient proceed to the TTS-specific step."""
        flow, main_entry = _make_flow(tts_service="tts.cloud_say")
        _prime_with_meta(flow, main_entry, RecipientType.TTS)

        result = await flow.async_step_recipient_basic_settings(
            {"retry_attempts": 1, "rate_limit": 5, "notification_types": ["ALERT"]}
        )

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_TTS_SETTINGS_KEY

    async def test_out_of_range_rate_limit_shows_form_with_error(self):
        """An overly large rate_limit re-shows the form with an error."""
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)

        result = await flow.async_step_recipient_basic_settings(
            {
                "retry_attempts": 2,
                "rate_limit": 999_999,  # exceeds allowed maximum
                "notification_types": ["INFO"],
            }
        )

        assert result["type"] == FlowResultType.FORM
        assert result["errors"] != {}

    async def test_unexpected_exception_sets_base_error(self):
        """An unhandled exception shows the form with the invalid_settings base error."""
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)

        with patch.object(
            ConfigValidator,
            "validate_recipient_basic_settings_schema",
            side_effect=RuntimeError("unexpected"),
        ):
            result = await flow.async_step_recipient_basic_settings(
                {"retry_attempts": 2, "rate_limit": 10, "notification_types": ["INFO"]}
            )

        assert result["type"] == FlowResultType.FORM
        assert (
            result["errors"].get("base")
            == SUBENTRY_FLOW_ERROR_INVALID_RECIPIENT_SETTINGS_KEY
        )


# ===========================================================================
# async_step_recipient_tts_settings
# ===========================================================================

_VALID_TTS_INPUT = {
    "message_format": "message_only",
    "volume_morning": 40,
    "volume_daytime": 50,
    "volume_evening": 35,
    "volume_night": 20,
    "volume_override_level": 80,
}


class TestAsyncStepRecipientTtsSettings:
    async def test_no_input_shows_tts_settings_form(self):
        """Calling with None returns the TTS settings form."""
        flow, main_entry = _make_flow(tts_service="tts.cloud_say")
        _prime_with_meta(flow, main_entry, RecipientType.TTS)

        result = await flow.async_step_recipient_tts_settings(None)

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_TTS_SETTINGS_KEY

    async def test_valid_input_stores_nested_settings_and_goes_to_channel_mapping(self):
        """Valid TTS settings are nested under tts_settings key and proceed to channel mapping."""
        flow, main_entry = _make_flow(tts_service="tts.cloud_say")
        _prime_for_channel_mapping(flow, main_entry, RecipientType.TTS)

        with patch(_PATCH_GET_CONFIG_REPO, return_value=None):
            result = await flow.async_step_recipient_tts_settings(_VALID_TTS_INPUT)

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_CHANNEL_MAPPING_KEY
        assert "tts_settings" in flow._recipient_settings
        assert (
            flow._recipient_settings["tts_settings"]["message_format"] == "message_only"
        )

    async def test_invalid_message_format_shows_form_with_error(self):
        """An invalid message_format value re-shows the form with an error."""
        flow, main_entry = _make_flow(tts_service="tts.cloud_say")
        _prime_with_meta(flow, main_entry, RecipientType.TTS)

        result = await flow.async_step_recipient_tts_settings(
            {**_VALID_TTS_INPUT, "message_format": "invalid_format"}
        )

        assert result["type"] == FlowResultType.FORM
        assert result["errors"] != {}

    async def test_unexpected_exception_sets_base_error(self):
        """An unhandled exception shows the form with the invalid_settings base error."""
        flow, main_entry = _make_flow(tts_service="tts.cloud_say")
        _prime_with_meta(flow, main_entry, RecipientType.TTS)

        with patch.object(
            ConfigValidator,
            "validate_recipient_tts_settings_schema",
            side_effect=RuntimeError("crash"),
        ):
            result = await flow.async_step_recipient_tts_settings(_VALID_TTS_INPUT)

        assert result["type"] == FlowResultType.FORM
        assert (
            result["errors"].get("base")
            == SUBENTRY_FLOW_ERROR_INVALID_RECIPIENT_SETTINGS_KEY
        )


# ===========================================================================
# async_step_recipient_channel_mapping
# ===========================================================================


class TestAsyncStepRecipientChannelMapping:
    async def test_no_input_shows_channel_mapping_form(self):
        """Calling with None returns the channel mapping form with no errors."""
        flow, main_entry = _make_flow()
        _prime_for_channel_mapping(flow, main_entry)

        with patch(_PATCH_GET_CONFIG_REPO, return_value=None):
            result = await flow.async_step_recipient_channel_mapping(None)

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_CHANNEL_MAPPING_KEY
        assert result["errors"] == {}

    async def test_at_least_one_channel_mapped_proceeds_to_dnd(self):
        """At least one channel in any criticality level proceeds to DND settings."""
        flow, main_entry = _make_flow()
        _prime_for_channel_mapping(flow, main_entry)

        with patch(_PATCH_GET_CONFIG_REPO, return_value=None):
            result = await flow.async_step_recipient_channel_mapping(
                {"channels_low": [PERSISTENT_NOTIFICATION_CHANNEL]}
            )

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_DND_SETTINGS_KEY
        assert (
            PERSISTENT_NOTIFICATION_CHANNEL in flow._recipient_settings["channels_low"]
        )

    async def test_all_empty_channels_shows_base_error(self):
        """Sending all empty channel lists triggers the at-least-one-channel error."""
        flow, main_entry = _make_flow()
        _prime_for_channel_mapping(flow, main_entry)

        with patch(_PATCH_GET_CONFIG_REPO, return_value=None):
            result = await flow.async_step_recipient_channel_mapping(
                {
                    "channels_low": [],
                    "channels_medium": [],
                    "channels_high": [],
                    "channels_critical": [],
                }
            )

        assert result["type"] == FlowResultType.FORM
        assert (
            result["errors"].get("base")
            == SUBENTRY_FLOW_ERROR_INVALID_CHANNEL_MAPPING_KEY
        )

    async def test_unexpected_exception_sets_base_error(self):
        """An unhandled exception shows the form with the invalid_channel_mapping base error."""
        flow, main_entry = _make_flow()
        _prime_for_channel_mapping(flow, main_entry)

        with patch.object(
            ConfigValidator,
            "validate_recipient_channel_mapping_schema",
            side_effect=RuntimeError("schema crash"),
        ):
            with patch(_PATCH_GET_CONFIG_REPO, return_value=None):
                result = await flow.async_step_recipient_channel_mapping(
                    {"channels_low": [PERSISTENT_NOTIFICATION_CHANNEL]}
                )

        assert result["type"] == FlowResultType.FORM
        assert (
            result["errors"].get("base")
            == SUBENTRY_FLOW_ERROR_INVALID_CHANNEL_MAPPING_KEY
        )


# ===========================================================================
# async_step_recipient_dnd_settings
# ===========================================================================


class TestAsyncStepRecipientDndSettings:
    async def test_no_input_shows_dnd_form(self):
        """Calling with None returns the DND settings form with no errors."""
        flow, main_entry = _make_flow()
        _prime_for_dnd(flow, main_entry)

        result = await flow.async_step_recipient_dnd_settings(None)

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_DND_SETTINGS_KEY
        assert result["errors"] == {}

    async def test_valid_dnd_disabled_input_creates_entry(self):
        """Disabling DND with valid criticality/type lists produces CREATE_ENTRY."""
        flow, main_entry = _make_flow()
        _prime_for_dnd(flow, main_entry)

        result = await flow.async_step_recipient_dnd_settings(
            {
                "dnd_enabled": False,
                "dnd_allowed_criticalities": [],
                "dnd_allowed_types": [],
            }
        )

        assert result["type"] == FlowResultType.CREATE_ENTRY

    async def test_dnd_enabled_without_times_shows_error(self):
        """Enabling DND without start/end times re-shows the form with an error."""
        flow, main_entry = _make_flow()
        _prime_for_dnd(flow, main_entry)

        result = await flow.async_step_recipient_dnd_settings(
            {
                "dnd_enabled": True,
                "dnd_allowed_criticalities": [],
                "dnd_allowed_types": [],
            }
        )

        assert result["type"] == FlowResultType.FORM
        assert result["errors"] != {}

    async def test_valid_dnd_enabled_with_times_creates_entry(self):
        """DND enabled with valid start/end times produces CREATE_ENTRY."""
        flow, main_entry = _make_flow()
        _prime_for_dnd(flow, main_entry)

        result = await flow.async_step_recipient_dnd_settings(
            {
                "dnd_enabled": True,
                "dnd_start": "22:00",
                "dnd_end": "08:00",
                "dnd_allowed_criticalities": [],
                "dnd_allowed_types": [],
            }
        )

        assert result["type"] == FlowResultType.CREATE_ENTRY


# ===========================================================================
# async_step_reconfigure
# ===========================================================================


class TestAsyncStepReconfigure:
    async def test_abort_when_no_subentry(self):
        """_get_reconfigure_subentry returning None aborts the flow."""
        flow, _ = _make_flow()

        with patch.object(flow, "_get_reconfigure_subentry", return_value=None):
            result = await flow.async_step_reconfigure()

        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "no_subentry"

    async def test_abort_when_no_main_entry(self):
        """A valid subentry but missing main entry aborts with no_main_entry."""
        flow, _ = _make_flow()
        sub = MagicMock()
        sub.data = {
            RCPT_CONFIG_ID_KEY: "rcpt-uuid",
            RCPT_CONFIG_NAME_KEY: "Alice",
            RCPT_CONFIG_TYPE_KEY: RecipientType.GENERIC.value,
            RCPT_CONFIG_EMAIL_KEY: None,
            RCPT_CONFIG_PHONE_KEY: None,
        }

        with patch.object(flow, "_get_reconfigure_subentry", return_value=sub):
            with patch(_PATCH_GET_MAIN_ENTRY, return_value=None):
                result = await flow.async_step_reconfigure()

        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "no_main_entry"

    async def test_valid_subentry_shows_definition_form(self):
        """A valid subentry with a found main entry shows the definition form."""
        flow, main_entry = _make_flow()
        sub = MagicMock()
        sub.data = {
            RCPT_CONFIG_ID_KEY: "rcpt-uuid",
            RCPT_CONFIG_NAME_KEY: "Alice",
            RCPT_CONFIG_TYPE_KEY: RecipientType.GENERIC.value,
            RCPT_CONFIG_EMAIL_KEY: None,
            RCPT_CONFIG_PHONE_KEY: None,
        }

        with patch.object(flow, "_get_reconfigure_subentry", return_value=sub):
            with patch(_PATCH_GET_MAIN_ENTRY, return_value=main_entry):
                result = await flow.async_step_reconfigure()

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_DEFINITION_KEY
        assert flow._reconfigure_entry is sub

    async def test_reconfigure_restores_meta_from_subentry_data(self):
        """After reconfigure is called, _recipient_meta contains subentry values."""
        flow, main_entry = _make_flow()
        sub = MagicMock()
        sub.data = {
            RCPT_CONFIG_ID_KEY: "rcpt-uuid",
            RCPT_CONFIG_NAME_KEY: "Alice",
            RCPT_CONFIG_TYPE_KEY: RecipientType.GENERIC.value,
            RCPT_CONFIG_EMAIL_KEY: "alice@example.com",
            RCPT_CONFIG_PHONE_KEY: None,
        }

        with patch.object(flow, "_get_reconfigure_subentry", return_value=sub):
            with patch(_PATCH_GET_MAIN_ENTRY, return_value=main_entry):
                await flow.async_step_reconfigure()

        assert flow._recipient_meta[RCPT_CONFIG_NAME_KEY] == "Alice"
        assert flow._recipient_meta[RCPT_CONFIG_EMAIL_KEY] == "alice@example.com"

    async def test_reconfigure_ha_user_includes_current_user_and_preselects_it(self):
        """HA_USER reconfigure keeps current user selectable and preselected."""
        flow, main_entry = _make_flow()
        ha_user_current = MagicMock()
        ha_user_current.id = "user123"
        ha_user_current.name = "Bob"
        ha_user_current.credentials = []
        ha_user_other = MagicMock()
        ha_user_other.id = "user999"
        ha_user_other.name = "Alice"
        ha_user_other.credentials = []
        flow.hass.auth.async_get_users = AsyncMock(
            return_value=[ha_user_current, ha_user_other]
        )

        sub = MagicMock()
        sub.data = {
            RCPT_CONFIG_ID_KEY: "user123",
            RCPT_CONFIG_NAME_KEY: "Bob",
            RCPT_CONFIG_TYPE_KEY: RecipientType.HA_USER.value,
            RCPT_CONFIG_EMAIL_KEY: None,
            RCPT_CONFIG_PHONE_KEY: None,
        }
        main_entry.subentries = {"s1": sub}

        with patch.object(flow, "_get_reconfigure_subentry", return_value=sub):
            with patch(_PATCH_GET_MAIN_ENTRY, return_value=main_entry):
                result = await flow.async_step_reconfigure()

        marker, user_selector = _get_schema_field(result, RCPT_CONFIG_USER_KEY)
        assert "user123" in _selector_option_values(user_selector)
        assert marker.description["suggested_value"] == "user123"


# ===========================================================================
# _create_recipient_entry
# ===========================================================================


class TestCreateRecipientEntry:
    async def test_creates_entry_with_valid_state(self):
        """A fully-populated flow state produces a CREATE_ENTRY result."""
        flow, main_entry = _make_flow()
        flow._main_entry = main_entry
        flow._recipient_meta = {
            RCPT_CONFIG_ID_KEY: "rcpt-uuid",
            RCPT_CONFIG_TYPE_KEY: RecipientType.GENERIC.value,
            RCPT_CONFIG_NAME_KEY: "Alice",
            RCPT_CONFIG_EMAIL_KEY: None,
            RCPT_CONFIG_PHONE_KEY: None,
        }
        flow._recipient_settings = _complete_settings()

        result = await flow._create_recipient_entry()

        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert "Alice" in result["title"]

    async def test_create_entry_data_contains_recipient_id_and_name(self):
        """The created entry data includes the recipient id and name."""
        flow, main_entry = _make_flow()
        flow._main_entry = main_entry
        flow._recipient_meta = {
            RCPT_CONFIG_ID_KEY: "rcpt-uuid",
            RCPT_CONFIG_TYPE_KEY: RecipientType.GENERIC.value,
            RCPT_CONFIG_NAME_KEY: "Alice",
            RCPT_CONFIG_EMAIL_KEY: None,
            RCPT_CONFIG_PHONE_KEY: None,
        }
        flow._recipient_settings = _complete_settings()

        result = await flow._create_recipient_entry()

        assert result["data"][RCPT_CONFIG_ID_KEY] == "rcpt-uuid"
        assert result["data"][RCPT_CONFIG_NAME_KEY] == "Alice"

    async def test_missing_name_aborts_with_create_entry_failed(self):
        """A missing required name causes abort with create_entry_failed."""
        flow, main_entry = _make_flow()
        flow._main_entry = main_entry
        flow._recipient_meta = {
            RCPT_CONFIG_ID_KEY: "rcpt-uuid",
            RCPT_CONFIG_TYPE_KEY: RecipientType.GENERIC.value,
            RCPT_CONFIG_NAME_KEY: None,  # missing!
            RCPT_CONFIG_EMAIL_KEY: None,
            RCPT_CONFIG_PHONE_KEY: None,
        }
        flow._recipient_settings = _complete_settings()

        result = await flow._create_recipient_entry()

        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "create_entry_failed"

    async def test_field_validation_error_shows_confirm_form(self):
        """A FieldValidationError is surfaced as a form rather than an abort."""
        flow, main_entry = _make_flow()
        flow._main_entry = main_entry
        flow._recipient_meta = {
            RCPT_CONFIG_ID_KEY: "rcpt-uuid",
            RCPT_CONFIG_TYPE_KEY: RecipientType.GENERIC.value,
            RCPT_CONFIG_NAME_KEY: "Alice",
            RCPT_CONFIG_EMAIL_KEY: None,
            RCPT_CONFIG_PHONE_KEY: None,
        }
        flow._recipient_settings = _complete_settings()

        with patch.object(
            ConfigValidator,
            "validate_recipient_consistency",
            side_effect=FieldValidationError("channels_low", "bad channel"),
        ):
            result = await flow._create_recipient_entry()

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "confirm"
        assert "channels_low" in result["errors"] or "base" in result["errors"]

    async def test_reconfigure_path_returns_abort_with_reconfigure_complete(self):
        """The reconfigure path calls async_update_and_abort and aborts with reconfigure_complete."""
        flow, main_entry = _make_flow()
        flow._main_entry = main_entry
        flow._reconfigure_entry = MagicMock()
        flow._recipient_meta = {
            RCPT_CONFIG_ID_KEY: "rcpt-uuid",
            RCPT_CONFIG_TYPE_KEY: RecipientType.GENERIC.value,
            RCPT_CONFIG_NAME_KEY: "Alice",
            RCPT_CONFIG_EMAIL_KEY: None,
            RCPT_CONFIG_PHONE_KEY: None,
        }
        flow._recipient_settings = _complete_settings()
        flow.async_update_and_abort = MagicMock()

        result = await flow._create_recipient_entry()

        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "reconfigure_complete"
        flow.async_update_and_abort.assert_called_once()


# ===========================================================================
# _system_recipient_exists
# ===========================================================================


class TestSystemRecipientExists:
    async def test_returns_false_when_main_entry_is_none(self):
        """Without a main entry the method returns False."""
        flow, _ = _make_flow()
        # _main_entry is None by default after construction
        assert not await flow._system_recipient_exists()

    async def test_returns_true_when_system_subentry_exists(self):
        """A subentry with SYSTEM type causes the method to return True."""
        flow, main_entry = _make_flow()
        sub = MagicMock()
        sub.data = {RCPT_CONFIG_TYPE_KEY: RecipientType.SYSTEM.value}
        main_entry.subentries = {"s1": sub}
        flow._main_entry = main_entry

        assert await flow._system_recipient_exists()

    async def test_returns_false_when_no_system_subentry(self):
        """Non-SYSTEM subentries do not cause the method to return True."""
        flow, main_entry = _make_flow()
        sub = MagicMock()
        sub.data = {RCPT_CONFIG_TYPE_KEY: RecipientType.GENERIC.value}
        main_entry.subentries = {"s1": sub}
        flow._main_entry = main_entry

        assert not await flow._system_recipient_exists()

    async def test_returns_false_with_empty_subentries(self):
        """An entry with no subentries returns False."""
        flow, main_entry = _make_flow()
        main_entry.subentries = {}
        flow._main_entry = main_entry

        assert not await flow._system_recipient_exists()


# ===========================================================================
# _get_not_configured_ha_users
# ===========================================================================


class TestGetNotConfiguredHaUsers:
    async def test_returns_all_users_when_no_subentries(self):
        """All HA users are returned when no HA_USER subentries exist."""
        flow, main_entry = _make_flow()
        ha_user = MagicMock()
        ha_user.id = "user1"
        ha_user.name = "User One"
        flow.hass.auth.async_get_users = AsyncMock(return_value=[ha_user])
        flow._main_entry = main_entry

        result = await flow._get_not_configured_ha_users()

        assert any(u["value"] == "user1" for u in result)

    async def test_configured_users_are_excluded(self):
        """Users already linked to a HA_USER subentry are omitted from the result."""
        flow, main_entry = _make_flow()
        ha_user = MagicMock()
        ha_user.id = "user1"
        ha_user.name = "User One"
        flow.hass.auth.async_get_users = AsyncMock(return_value=[ha_user])
        existing = MagicMock()
        existing.data = {
            RCPT_CONFIG_TYPE_KEY: RecipientType.HA_USER.value,
            RCPT_CONFIG_ID_KEY: "user1",
        }
        main_entry.subentries = {"s1": existing}
        flow._main_entry = main_entry

        result = await flow._get_not_configured_ha_users()

        assert not any(u["value"] == "user1" for u in result)

    async def test_returns_empty_list_when_no_ha_users(self):
        """When auth returns no users the result is an empty list."""
        flow, main_entry = _make_flow()
        flow.hass.auth.async_get_users = AsyncMock(return_value=[])
        flow._main_entry = main_entry

        result = await flow._get_not_configured_ha_users()

        assert result == []


# ===========================================================================
# _get_ha_user_data
# ===========================================================================


class TestGetHaUserData:
    async def test_returns_name_for_known_user(self):
        """Returns the correct name for a found user."""
        flow, _ = _make_flow()
        ha_user = MagicMock()
        ha_user.id = "user1"
        ha_user.name = "Alice"
        ha_user.credentials = []
        flow.hass.auth.async_get_users = AsyncMock(return_value=[ha_user])

        result = await flow._get_ha_user_data("user1")

        assert result["name"] == "Alice"
        assert result["email"] is None

    async def test_returns_fallback_for_unknown_user(self):
        """When the user_id is not found a generic fallback dict is returned."""
        flow, _ = _make_flow()
        flow.hass.auth.async_get_users = AsyncMock(return_value=[])

        result = await flow._get_ha_user_data("ghost-id")

        assert "name" in result
        assert result["email"] is None

    async def test_extracts_email_from_oauth_credentials(self):
        """An email-like username in credentials is used as the email address."""
        flow, _ = _make_flow()
        cred = MagicMock()
        cred.data = {"username": "alice@example.com"}
        ha_user = MagicMock()
        ha_user.id = "user1"
        ha_user.name = "Alice"
        ha_user.credentials = [cred]
        flow.hass.auth.async_get_users = AsyncMock(return_value=[ha_user])

        result = await flow._get_ha_user_data("user1")

        assert result["email"] == "alice@example.com"

    async def test_non_email_username_not_used_as_email(self):
        """A username without '@' is not treated as an email."""
        flow, _ = _make_flow()
        cred = MagicMock()
        cred.data = {"username": "localuser"}
        ha_user = MagicMock()
        ha_user.id = "user1"
        ha_user.name = "Alice"
        ha_user.credentials = [cred]
        flow.hass.auth.async_get_users = AsyncMock(return_value=[ha_user])

        result = await flow._get_ha_user_data("user1")

        assert result["email"] is None


# ===========================================================================
# _get_available_channels
# ===========================================================================


class TestGetAvailableChannels:
    async def test_returns_empty_without_system_config(self):
        """Without system_config the method immediately returns []."""
        flow, _ = _make_flow()
        # system_config is None by default

        result = await flow._get_available_channels()

        assert result == []

    async def test_returns_empty_when_config_repo_unavailable(self):
        """When get_config_repository returns None the fallback returns []."""
        flow, _ = _make_flow()
        flow.system_config = SystemConfig.from_dict(_sys_config_dict())

        with patch(_PATCH_GET_CONFIG_REPO, return_value=None):
            result = await flow._get_available_channels()

        assert result == []

    async def test_returns_channels_filtered_by_enabled_channels(self):
        """Channels from the repo are filtered to those in system_config.enabled_channels."""
        flow, _ = _make_flow()
        flow.system_config = SystemConfig.from_dict(_sys_config_dict())
        flow._recipient_meta = {RCPT_CONFIG_TYPE_KEY: RecipientType.GENERIC.value}

        ch_enabled = ChannelInfo(
            id=PERSISTENT_NOTIFICATION_CHANNEL,
            label="Persistent",
            scope=ChannelScope.SYSTEM,
        )
        ch_disabled = ChannelInfo(
            id="notify.something_else",
            label="Other",
            scope=ChannelScope.SYSTEM,
        )
        config_repo = MagicMock()
        config_repo.channel_manager.count_detected.return_value = 2
        config_repo.get_channels_for_ui.return_value = [ch_enabled, ch_disabled]

        with patch(_PATCH_GET_CONFIG_REPO, return_value=config_repo):
            result = await flow._get_available_channels()

        assert len(result) == 1
        assert result[0].id == PERSISTENT_NOTIFICATION_CHANNEL

    async def test_channel_manager_absent_returns_empty(self):
        """When the channel_manager has no detected channels the fallback returns []."""
        flow, _ = _make_flow()
        flow.system_config = SystemConfig.from_dict(_sys_config_dict())

        config_repo = MagicMock()
        config_repo.channel_manager.count_detected.return_value = 0

        with patch(_PATCH_GET_CONFIG_REPO, return_value=config_repo):
            result = await flow._get_available_channels()

        assert result == []


# ===========================================================================
# _setup_system_recipient (integration-style helper)
# ===========================================================================


class TestSetupSystemRecipient:
    async def test_populates_system_type_and_default_name(self):
        """_setup_system_recipient fills meta with SYSTEM type and default name."""
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)

        await flow._setup_system_recipient()

        assert flow._recipient_meta[RCPT_CONFIG_TYPE_KEY] == RecipientType.SYSTEM.value
        assert (
            flow._recipient_meta[RCPT_CONFIG_NAME_KEY]
            == SYS_DEFAULT_SYSTEM_RECIPIENT_NAME
        )

    async def test_preconfigures_persistent_notification_for_all_criticality_levels(
        self,
    ):
        """All four channel levels are pre-mapped to persistent_notification."""
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)

        await flow._setup_system_recipient()

        for level in (
            "channels_low",
            "channels_medium",
            "channels_high",
            "channels_critical",
        ):
            assert PERSISTENT_NOTIFICATION_CHANNEL in flow._recipient_settings[level]

    async def test_returns_basic_settings_form(self):
        """_setup_system_recipient transitions directly to the basic settings form."""
        flow, main_entry = _make_flow()
        _prime_with_meta(flow, main_entry)

        result = await flow._setup_system_recipient()

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == SUBENTRY_FLOW_STEP_RECIPIENT_BASIC_SETTINGS_KEY
