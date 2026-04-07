"""Config flow for ANS integration - Main entry setup.

This module handles the main integration entry configuration which defines
the SystemConfig (global rate limits, enabled channels, etc.).

All recipient configurations (including system recipient for persistent notifications)
are managed through the recipient_flow.py sub-entry flow.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
)
from homeassistant.core import callback

from .channels.channel_manager import (
    detect_media_players,
    detect_notification_channels,
)
from .config.forms import (
    detect_tts_integrations,
    get_system_config_schema,
    get_system_options_schema,
)

# Import sub-entry flow for Home Assistant to discover
# pyright: reportUnusedImport=false
from .config.recipient_flow import RecipientConfigFlow  # noqa: F401
from .config.validator import (
    ConfigValidator,
    FieldValidationError,
    validate_tts_service,
)
from .const import (
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
from .delivery.factory import ADAPTER_CLASS_MAP
from .helper import channel_info_to_select_options
from .models import SystemConfig

_LOGGER = logging.getLogger(__name__)


# Export for Home Assistant to discover
__all__ = ["ANSConfigFlow", "RecipientConfigFlow"]


class ANSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle config flow for ANS integration main entry.

    This flow creates the main integration entry with SystemConfig only.
    Recipients are configured separately via recipient sub-entry flows.

    Data vs Options split:
    - data: Structural settings (enabled_channels) - set during initial setup
    - options: Tunable parameters (rate limits) - modified via options flow
    """

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._reconfigure_entry: ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Initialize the config flow."""
        # Prevent duplicate main entries; the integration is single-instance
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        # Jump to system settings (start of the real flow)
        return await self.async_step_system_settings()

    async def async_step_system_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step - configure system settings.

        This is the only step for the main entry. It configures:
        - Enabled notification channels (structural)

        Rate limits and other tunable parameters are configured via the options flow.

        This method is reused by both initial setup and reconfiguration flows.
        """
        errors: dict[str, str] = {}
        is_reconfigure = self._reconfigure_entry is not None

        if user_input is not None:
            try:
                # Validate TTS service if provided (PRIORITY 1 - Security)
                if SYS_CONFIG_TTS_SERVICE_KEY in user_input:
                    tts_service = user_input[SYS_CONFIG_TTS_SERVICE_KEY]
                    try:
                        validated_tts = await validate_tts_service(
                            self.hass, tts_service
                        )
                        user_input[SYS_CONFIG_TTS_SERVICE_KEY] = validated_tts
                    except vol.Invalid as e:
                        _LOGGER.warning("TTS service validation failed: %s", e)
                        errors[SYS_CONFIG_TTS_SERVICE_KEY] = str(e)
                        # Don't proceed with config creation if TTS validation fails
                        # Fall through to show form again with error
                        user_input = None

                # Validate system configuration — only when user_input survived
                # TTS validation above (which may have set it to None on failure).
                if user_input is not None:
                    if is_reconfigure:
                        # In reconfigure mode, preserve existing options (rate limits)
                        reconfigure_entry = self._reconfigure_entry
                        if reconfigure_entry is None:
                            return self.async_abort(
                                reason="reconfigure_entry_not_found"
                            )
                        current_options = (
                            dict(reconfigure_entry.options)
                            if reconfigure_entry.options
                            else {}
                        )
                        # Merge user input with current options for validation
                        full_config = {**user_input, **current_options}
                    else:
                        # In initial setup, use default rate limits
                        full_config = {
                            **user_input,
                            SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY: SYS_DEFAULT_GLOBAL_RATE_LIMIT,
                            SYS_CONFIG_RATE_LIMIT_WINDOW_KEY: SYS_DEFAULT_RATE_LIMIT_WINDOW,
                        }

                    validated_data = ConfigValidator.validate_system_settings_schema(
                        full_config
                    )

                    # Create SystemConfig to ensure all validation passes
                    system_config = SystemConfig.from_dict(validated_data)

                    # Split into data (structural) and options (tunable)
                    config_dict = system_config.to_dict()

                    # data: enabled_channels (defines system structure)
                    # Note: Boolean checkbox fields are omitted from user_input when unchecked
                    enable_audit = user_input.get(SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY)
                    if enable_audit is None:
                        # Checkbox was unchecked (omitted from form) - default based on context
                        enable_audit = (
                            SYS_DEFAULT_ENABLE_AUDIT_LOGGING
                            if not is_reconfigure
                            else False
                        )

                    data = {
                        SYS_CONFIG_ENABLED_CHANNELS_KEY: config_dict.get(
                            SYS_CONFIG_ENABLED_CHANNELS_KEY, []
                        ),
                        SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY: enable_audit,
                        SYS_CONFIG_TTS_SERVICE_KEY: config_dict.get(
                            SYS_CONFIG_TTS_SERVICE_KEY
                        ),
                        "version": config_dict.get("version", 1),
                    }

                    # options: rate limits and other tunable parameters
                    options = {
                        SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY: config_dict.get(
                            SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY
                        ),
                        SYS_CONFIG_RATE_LIMIT_WINDOW_KEY: config_dict.get(
                            SYS_CONFIG_RATE_LIMIT_WINDOW_KEY
                        ),
                    }

                    if is_reconfigure:
                        # Update existing entry (type checked by is_reconfigure condition)
                        reconfigure_entry = self._reconfigure_entry
                        if reconfigure_entry is None:
                            return self.async_abort(
                                reason="reconfigure_entry_not_found"
                            )
                        self.hass.config_entries.async_update_entry(
                            reconfigure_entry,
                            data=data,
                            options=options,
                        )

                        # Reload the integration to apply changes
                        await self.hass.config_entries.async_reload(
                            reconfigure_entry.entry_id
                        )

                        return self.async_abort(reason="reconfigure_successful")

                    # Create the main entry with split configuration
                    # Options use defaults until user modifies via options flow
                    return self.async_create_entry(
                        title=NAME,
                        data=data,
                        options=options,
                        description_placeholders={
                            "info": "Configure recipients and tune rate limits in the integration settings"
                        },
                    )

            except FieldValidationError as e:
                _LOGGER.debug("Field validation error: %s - %s", e.field, e.message)
                errors[e.field] = e.message
            except vol.Invalid as e:
                _LOGGER.debug("Voluptuous validation error: %s", e)
                path = e.path
                field_key = str(path[0]) if path else "base"
                error_msg = str(path[-1]) if len(path) > 1 else str(e)
                errors[field_key] = error_msg
            except Exception:
                _LOGGER.exception("System config validation failed")
                errors["base"] = CONFIG_FLOW_ERROR_INVALID_SYSTEM_SETTINGS_KEY

        # Prepare defaults
        if is_reconfigure:
            # For reconfigure, show current data only (not options - they're in options flow)
            reconfigure_entry = self._reconfigure_entry
            defaults = dict(reconfigure_entry.data) if reconfigure_entry else {}
        else:
            defaults = user_input or {}

        # Always detect channels fresh — the channel_registry cache may be
        # stale if notify integrations (e.g. mobile_app) finished loading after
        # ANS started up.  Reading from the cache would hide channels that are
        # now available, and would double-register media_player channels that
        # are already stored in the registry under ChannelScope.TTS.
        available_channels = detect_notification_channels(
            self.hass,
            adapter_classes=ADAPTER_CLASS_MAP,
        )

        channel_options = channel_info_to_select_options(available_channels)

        # Detect TTS integrations
        available_tts_services = detect_tts_integrations(self.hass)

        # Detect media players
        available_media_players = detect_media_players(self.hass)

        # Add media players to channel options if TTS is configured
        current_tts = defaults.get(SYS_CONFIG_TTS_SERVICE_KEY)
        if current_tts and current_tts != "None" and available_media_players:
            # Convert media players to select options and add to channel list
            media_player_options = channel_info_to_select_options(
                available_media_players
            )
            channel_options.extend(media_player_options)

        # Show the system configuration form (structural settings only for initial setup)
        return self.async_show_form(
            step_id=CONFIG_FLOW_STEP_SYS_SETTINGS_KEY,
            data_schema=get_system_config_schema(
                defaults=defaults,
                values={
                    "enabled_channels": channel_options,
                    "tts_services": available_tts_services,
                    "media_players": available_media_players,
                },
                include_rate_limits=False,  # Don't show rate limits in setup/reconfigure
                include_audit_logging=True,  # Show audit logging toggle
                hass=self.hass,  # Pass hass for TTS validation
            ),
            errors=errors,
            description_placeholders={
                "info": (
                    "Select which notification channels to enable and configure audit logging. "
                    "Rate limits can be configured in the options after setup."
                    if not is_reconfigure
                    else "Reconfigure enabled notification channels and audit logging. "
                    "Rate limits are configured via the options menu."
                )
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> ANSOptionsFlow:
        """Get the options flow for this handler."""
        return ANSOptionsFlow()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure the integration (structural + tunable settings).

        Allows changing both:
        - Structural settings (enabled_channels) stored in data
        - Tunable parameters (rate limits) stored in options
        """
        # Get the entry being reconfigured
        entry_id = self.context.get("entry_id")
        if not entry_id:
            return self.async_abort(reason="reconfigure_entry_not_found")

        self._reconfigure_entry = self.hass.config_entries.async_get_entry(entry_id)
        if not self._reconfigure_entry:
            return self.async_abort(reason="reconfigure_entry_not_found")

        # Reuse the system_settings step (it will detect reconfigure mode)
        return await self.async_step_system_settings(user_input)

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentries supported by this integration."""
        return {"recipient": RecipientConfigFlow}


class ANSOptionsFlow(OptionsFlow):
    """Handle options flow for ANS integration main entry.

    Allows reconfiguring the tunable system parameters (rate limits, retry
    settings, queue concurrency, storage retention).  Structural settings
    (enabled channels) are changed via Reconfigure.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options for the main entry - only tunable parameters."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                # Merge with existing data (enabled_channels from data)
                full_config = {
                    **dict(self.config_entry.data),
                    **user_input,
                }

                validated_data = ConfigValidator.validate_system_settings_schema(
                    full_config
                )

                # Create SystemConfig to ensure all validation passes
                SystemConfig.from_dict(validated_data)

                # Extract all tunable parameters for options
                options = {
                    SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY: validated_data.get(
                        SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY, SYS_DEFAULT_GLOBAL_RATE_LIMIT
                    ),
                    SYS_CONFIG_RATE_LIMIT_WINDOW_KEY: validated_data.get(
                        SYS_CONFIG_RATE_LIMIT_WINDOW_KEY, SYS_DEFAULT_RATE_LIMIT_WINDOW
                    ),
                    SYS_CONFIG_RETRY_BASE_DELAY_KEY: validated_data.get(
                        SYS_CONFIG_RETRY_BASE_DELAY_KEY,
                        SYS_DEFAULT_RETRY_BASE_DELAY_SECONDS,
                    ),
                    SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY: validated_data.get(
                        SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY,
                        SYS_DEFAULT_RETRY_BACKOFF_FACTOR,
                    ),
                    SYS_CONFIG_RETRY_MAX_DELAY_KEY: validated_data.get(
                        SYS_CONFIG_RETRY_MAX_DELAY_KEY,
                        SYS_DEFAULT_RETRY_MAX_DELAY_SECONDS,
                    ),
                    SYS_CONFIG_QUEUE_CONCURRENCY_KEY: validated_data.get(
                        SYS_CONFIG_QUEUE_CONCURRENCY_KEY, SYS_DEFAULT_QUEUE_CONCURRENCY
                    ),
                    SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY: validated_data.get(
                        SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY,
                        SYS_STORAGE_DEFAULT_FILE_RETENTION_DAYS,
                    ),
                }

                # Update only options (data remains unchanged)
                return self.async_create_entry(
                    title="",
                    data=options,
                )

            except ValueError as e:
                _LOGGER.debug("System configuration validation failed: %s", e)
                errors["base"] = "invalid_system_settings"
            except Exception:
                _LOGGER.exception("Unexpected error during system configuration update")
                errors["base"] = "unknown"

        # Get current options (or defaults from data if no options exist)
        current_options = (
            dict(self.config_entry.options) if self.config_entry.options else {}
        )
        current_data = dict(self.config_entry.data)

        # Check if audit logging is enabled in config entry data
        audit_enabled = current_data.get(
            SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY, SYS_DEFAULT_ENABLE_AUDIT_LOGGING
        )

        # Merge for defaults (options override data)
        current_config = {**current_data, **current_options}

        # Show only tunable parameters in options form

        return self.async_show_form(
            step_id="init",
            data_schema=get_system_options_schema(
                defaults=user_input or current_config,
                include_audit_retention=audit_enabled,
            ),
            errors=errors,
            description_placeholders={
                "info": "Update rate limits, retry settings, queue concurrency, and storage retention"
            },
        )
