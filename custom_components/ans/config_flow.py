"""Config flow and main options flow for the ANS integration."""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryBaseFlow,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import (
    CONFIG_FLOW_DEFINE_DEFAULT_IDENTITY_SETTINGS_KEY,
    CONFIG_FLOW_ERROR_HA_USER_DETECTION_FAILED_KEY,
    CONFIG_FLOW_ERROR_INVALID_HA_USER_SELECTION_KEY,
    CONFIG_FLOW_ERROR_INVALID_IDENTITY_SETTINGS_KEY,
    CONFIG_FLOW_ERROR_INVALID_SYSTEM_SETTINGS_KEY,
    CONFIG_FLOW_SELECTED_HA_USERS_KEY,
    CONFIG_FLOW_STEP_AUTO_HA_USER_CONFIGURATION_KEY,
    CONFIG_FLOW_STEP_ID_DEFAULT_BASIC_SETTINGS_KEY,
    CONFIG_FLOW_STEP_ID_DEFAULT_CHANNEL_MAPPING_KEY,
    CONFIG_FLOW_STEP_ID_DEFAULT_DND_SETTINGS_KEY,
    CONFIG_FLOW_STEP_SYS_SETTINGS_KEY,
    DOMAIN,
    ID_CONFIG_CONFIGURED_CHANNELS_KEY,
    ID_CONFIG_CRITICALITY_LEVELS_KEY,
    ID_CONFIG_NOTIFICATION_TYPES_KEY,
    ID_CONFIG_RATE_LIMIT_KEY,
    ID_CONFIG_RETRY_ATTEMPTS_KEY,
    NAME,
    SYS_CONFIG_ENABLED_CHANNELS_KEY,
    SYS_CONFIG_RATE_LIMIT_MAX_KEY,
    SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY,
)
from .detection import async_detect_notification_integrations
from .flow_base import ANSFlowBase, FlowSettings
from .forms import (
    dict_to_select_options_list,
    get_auto_configure_ha_users_schema,
    get_identity_basic_settings_schema,
    get_identity_criticality_channel_mapping_schema,
    get_identity_dnd_settings_schema,
    get_system_config_schema,
)
from .identity_flow import IdentityConfigFlow
from .models import (
    IdentityConfig,
    NotificationCriticality,
    NotificationType,
    SystemConfig,
)

_LOGGER = logging.getLogger(__name__)


class ANSMainEntryFlowBase(ConfigEntryBaseFlow, ANSFlowBase):
    """Shared implementation of the identity-related steps."""

    def __init__(self, flow_settings: FlowSettings) -> None:
        """Initialize any flow state."""
        super().__init__(flow_settings=flow_settings)
        self._reconfigure_entry: ConfigEntry | None = None
        self._system_settings: dict[str, Any] = {}
        self._identity_defaults: dict[str, Any] = {}

    # def _update_validation_context(self) -> None:
    #     """Update the validation context with current system limits."""
    #     if self._system_settings:
    #         system_limits = {
    #             SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY: self._system_settings.get(
    #                 SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY, DEFAULT_RETRIES_MAX
    #             ),
    #             SYS_CONFIG_RATE_LIMIT_MAX_KEY: self._system_settings.get(
    #                 SYS_CONFIG_RATE_LIMIT_MAX_KEY, DEFAULT_RATE_LIMIT_MAX
    #             ),
    #         }
    #         available_channels = list(
    #             self._system_settings.get(
    #                 SYS_CONFIG_ENABLED_CHANNELS_KEY, DEFAULT_ENABLED_CHANNELS
    #             )
    #         )
    #         self._validation_context = ValidationContext(
    #             system_limits=system_limits, available_channels=available_channels
    #         )

    def _create_data_object(self) -> dict[str, Any]:
        """Return structure to persist to entry.data."""
        system_config = SystemConfig.from_dict(self._system_settings or {})
        return dict(system_config.to_dict() or {})

    def _create_options_object(self) -> dict[str, Any]:
        """Return structure to persist to entry.options."""
        identity_config = IdentityConfig.from_dict(self._identity_defaults or {})
        return dict(identity_config.to_dict() or {})

    async def async_step_default_identity_basic_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect basic default identity settings (template) used when creating identities."""
        step_id = CONFIG_FLOW_STEP_ID_DEFAULT_BASIC_SETTINGS_KEY
        errors: dict[str, str] = {}
        description_placeholders: dict[str, Any] = {
            "step": f"{list(self.flow_settings.flow_steps.keys()).index(step_id) + 1}/{len(self.flow_settings.flow_steps)}"
        }
        last_step = self._is_last_step(step_id) or self._is_reconfigure()

        # Build helper values
        notification_types = {t.name: t.value for t in NotificationType}
        channel_options = {
            ch: ch.split(".", 1)[1].replace("_", " ").title()
            for ch in self._system_settings.get(SYS_CONFIG_ENABLED_CHANNELS_KEY, [])
        }

        # Prepare defaults: if reconfigure use existing entry's options; otherwise use in-flow defaults
        if self._is_reconfigure() and self._reconfigure_entry:
            defaults = dict(self._reconfigure_entry.options or {})
        else:
            defaults = dict(self._identity_defaults or {})

        values = {}
        values[ID_CONFIG_NOTIFICATION_TYPES_KEY] = dict_to_select_options_list(
            notification_types
        )
        values[ID_CONFIG_CONFIGURED_CHANNELS_KEY] = dict_to_select_options_list(
            channel_options
        )

        # Validate and store user input
        if user_input is not None:
            try:
                # Import at runtime to avoid circular imports
                from .config_validator import ConfigValidator

                # Validate identity config template using current system limits
                schema_validated = (
                    ConfigValidator.validate_identity_basic_settings_schema(
                        user_input, self._validation_context
                    )
                )
                # Merge validated defaults into the flow state
                self._identity_defaults.update(schema_validated)
                # Continue to the next step in initial setup
                return await self._execute_next_step(step_id)

            except vol.Invalid as e:
                _LOGGER.debug(str(e))
                errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
            except Exception:
                _LOGGER.exception("Default identity config validation failed")
                errors["base"] = CONFIG_FLOW_ERROR_INVALID_IDENTITY_SETTINGS_KEY

            finally:
                defaults = user_input  # Keep user inputs

        return self.async_show_form(
            step_id=step_id,
            data_schema=get_identity_basic_settings_schema(
                defaults, self._validation_context, values
            ),
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=last_step,
        )

    async def async_step_default_identity_channel_mapping(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect default channel mapping per criticality."""
        step_id = CONFIG_FLOW_STEP_ID_DEFAULT_CHANNEL_MAPPING_KEY
        errors: dict[str, str] = {}
        description_placeholders: dict[str, Any] = {
            "step": f"{list(self.flow_settings.flow_steps.keys()).index(step_id) + 1}/{len(self.flow_settings.flow_steps)}"
        }
        last_step = self._is_last_step(step_id) or self._is_reconfigure()

        # Build helper values for the form
        criticality_levels = {c.name: c.value for c in NotificationCriticality}
        channel_options = {
            ch: ch.split(".", 1)[1].replace("_", " ").title()
            for ch in self._system_settings.get(SYS_CONFIG_ENABLED_CHANNELS_KEY, [])
        }

        # Prepare defaults: if reconfigure use existing entry's options; otherwise use in-flow defaults
        if self._is_reconfigure() and self._reconfigure_entry:
            defaults = dict(self._reconfigure_entry.options or {})
        else:
            defaults = dict(self._identity_defaults or {})

        values = {}
        values[ID_CONFIG_CRITICALITY_LEVELS_KEY] = dict_to_select_options_list(
            criticality_levels
        )
        values[ID_CONFIG_CONFIGURED_CHANNELS_KEY] = dict_to_select_options_list(
            channel_options
        )

        # Validate and store user input
        if user_input is not None:
            try:
                # Import at runtime to avoid circular imports
                from .config_validator import ConfigValidator

                # Validate identity channel mappings
                schema_validated = (
                    ConfigValidator.validate_identity_channel_mapping_schema(
                        user_input, self._validation_context
                    )
                )
                # Merge validated default channels into the flow state
                self._identity_defaults.update(schema_validated)
                # Continue to the next step in initial setup
                return await self._execute_next_step(step_id)

            except vol.Invalid as e:
                _LOGGER.debug(str(e))
                errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
            except Exception:
                _LOGGER.exception("Default identity channel mapping validation failed")
                errors["base"] = CONFIG_FLOW_ERROR_INVALID_IDENTITY_SETTINGS_KEY

            finally:
                defaults = user_input  # Keep user inputs

        return self.async_show_form(
            step_id=step_id,
            data_schema=get_identity_criticality_channel_mapping_schema(
                defaults, values
            ),
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=last_step,
        )

    async def async_step_default_identity_dnd_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect default Do-Not-Disturb settings for identity template."""
        step_id = CONFIG_FLOW_STEP_ID_DEFAULT_DND_SETTINGS_KEY
        errors: dict[str, str] = {}
        description_placeholders: dict[str, Any] = {
            "step": f"{list(self.flow_settings.flow_steps.keys()).index(step_id) + 1}/{len(self.flow_settings.flow_steps)}"
        }
        last_step = self._is_last_step(step_id) or self._is_reconfigure()

        # Prepare defaults: if reconfigure use existing entry's options; otherwise use in-flow defaults
        if self._is_reconfigure() and self._reconfigure_entry:
            defaults = dict(self._reconfigure_entry.options or {})
        else:
            defaults = dict(self._identity_defaults or {})

        # Validate and store user input
        if user_input is not None:
            try:
                # Import at runtime to avoid circular imports
                from .config_validator import ConfigValidator

                # Validate identity config template
                schema_validated = (
                    ConfigValidator.validate_identity_dnd_settings_schema(user_input)
                )
                # Merge validated default DND settings into the flow state
                self._identity_defaults.update(schema_validated)
                # Continue to the next step in initial setup
                return await self._execute_next_step(step_id)

            except vol.Invalid as e:
                _LOGGER.debug(str(e))
                errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
            except Exception:
                _LOGGER.exception("Default identity DND validation failed")
                errors["base"] = CONFIG_FLOW_ERROR_INVALID_IDENTITY_SETTINGS_KEY

            finally:
                defaults = user_input  # Keep user inputs

        return self.async_show_form(
            step_id=step_id,
            data_schema=get_identity_dnd_settings_schema(defaults, {}),
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=last_step,
        )


class ANSConfigFlow(ConfigFlow, ANSMainEntryFlowBase, domain=DOMAIN):
    """Main config flow for the ANS integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize any flow state."""
        flow_settings = FlowSettings(
            flow_steps={
                CONFIG_FLOW_STEP_SYS_SETTINGS_KEY: self.async_step_system_settings,
                CONFIG_FLOW_STEP_ID_DEFAULT_BASIC_SETTINGS_KEY: self.async_step_default_identity_basic_settings,
                CONFIG_FLOW_STEP_ID_DEFAULT_CHANNEL_MAPPING_KEY: self.async_step_default_identity_channel_mapping,
                CONFIG_FLOW_STEP_ID_DEFAULT_DND_SETTINGS_KEY: self.async_step_default_identity_dnd_settings,
                CONFIG_FLOW_STEP_AUTO_HA_USER_CONFIGURATION_KEY: self.async_step_auto_ha_user_configuration,
            },
            skip_steps=[
                CONFIG_FLOW_STEP_ID_DEFAULT_BASIC_SETTINGS_KEY,
                CONFIG_FLOW_STEP_ID_DEFAULT_CHANNEL_MAPPING_KEY,
                CONFIG_FLOW_STEP_ID_DEFAULT_DND_SETTINGS_KEY,
            ],
            skip_condition_key=CONFIG_FLOW_DEFINE_DEFAULT_IDENTITY_SETTINGS_KEY,
        )
        super().__init__(flow_settings)
        self._available_ha_users: dict[str, str] = {}
        self._selected_ha_users: list[str] = []

    async def _async_get_available_notification_integrations(self) -> dict[str, str]:
        """Return a dict of available notification integrations (id -> label)."""
        integrations: dict[str, str] = {}
        try:
            available = await async_detect_notification_integrations(self.hass)
            for channel in available:
                integrations[channel.id] = channel.label
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to detect notification integrations")
        return integrations

    # async def _async_get_available_tts_integrations(self) -> dict[str, str]:
    #     """Detect available TTS integrations."""
    #     integrations: dict[str, str] = {}
    #     available_tts_integrations = await async_detect_tts_integrations(self.hass)
    #     for channel in available_tts_integrations:
    #         integrations[channel.id] = channel.label
    #     return integrations

    def _ensure_identity_defaults_present(self) -> None:
        """Ensure a canonical identity-defaults dict is present for options."""
        if not isinstance(self._identity_defaults, dict) or not self._identity_defaults:
            # store canonical defaults as dict
            self._identity_defaults = IdentityConfig.default().to_dict()

    def _is_reconfigure(self) -> bool:
        """Return True if this flow runs in reconfigure mode."""
        return self._reconfigure_entry is not None

    async def _store_entry(self) -> ConfigFlowResult:
        """Create the main config entry and optionally create identity subentries."""
        try:
            # Ensure identity defaults are present
            self._ensure_identity_defaults_present()
            # Create the main entry
            entry = self.async_create_entry(
                title=NAME,
                data=self._create_data_object(),
                options=self._create_options_object(),
            )
        except Exception:
            _LOGGER.exception("Failed to create main config entry")
            return self.async_abort(reason="create_entry_failed")
        else:
            return entry

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Initilizee the config flow."""
        # Prevent duplicate main entries; the integration is single-instance
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        # Jump to system settings (start of the real flow)
        return await self.async_step_system_settings()

    async def async_step_system_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure system-level settings (rate limits, retries, enabled channels)."""
        step_id = CONFIG_FLOW_STEP_SYS_SETTINGS_KEY
        errors: dict[str, str] = {}
        description_placeholders: dict[str, Any] = {
            "step": f"{list(self.flow_settings.flow_steps.keys()).index(step_id) + 1}/{len(self.flow_settings.flow_steps)}"
            if not self._is_reconfigure()
            else "System Settings"
        }
        last_step = self._is_reconfigure()

        # Get available notification channels (detected integrations)
        channel_options = await self._async_get_available_notification_integrations()

        # Prepare defaults: if reconfigure use existing entry's options; otherwise use in-flow defaults
        if self._is_reconfigure() and self._reconfigure_entry:
            defaults = dict(self._reconfigure_entry.data or {})
            defaults.setdefault(CONFIG_FLOW_DEFINE_DEFAULT_IDENTITY_SETTINGS_KEY, False)
        else:
            defaults = dict(self._system_settings or {})
            defaults.setdefault(CONFIG_FLOW_DEFINE_DEFAULT_IDENTITY_SETTINGS_KEY, True)

        # Transform dynamic choices into the structure expected by form helpers
        values = {
            SYS_CONFIG_ENABLED_CHANNELS_KEY: dict_to_select_options_list(
                channel_options
            )
        }

        # Validate and store user input
        if user_input is not None:
            try:
                # Import at runtime to avoid circular imports
                from .config_validator import ConfigValidator

                # Validate system config and raise informative exceptions on invalid input
                schema_validated = ConfigValidator.validate_system_settings_schema(
                    user_input
                )
                # Merge validated values into system settings
                self._system_settings.update(schema_validated)
                # Set configured system limits (stored internally with underscore)
                self._update_validation_context(self._system_settings)
                # Store whether to define default identity settings
                self._flow_data[CONFIG_FLOW_DEFINE_DEFAULT_IDENTITY_SETTINGS_KEY] = (
                    self._system_settings.pop(
                        CONFIG_FLOW_DEFINE_DEFAULT_IDENTITY_SETTINGS_KEY
                    )
                )
                # If this is a reconfigure flow, apply changes and finish
                if self._is_reconfigure():
                    return await self._apply_reconfigure_changes()
                # Continue to the next step in initial setup
                return await self._execute_next_step(step_id)

            except vol.Invalid as e:
                _LOGGER.debug(str(e))
                errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
            except Exception:
                _LOGGER.exception("System config validation failed")
                errors["base"] = CONFIG_FLOW_ERROR_INVALID_SYSTEM_SETTINGS_KEY

            finally:
                defaults = user_input  # Keep user inputs

        return self.async_show_form(
            step_id=step_id,
            data_schema=get_system_config_schema(defaults, values),
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=last_step,
        )

    async def async_step_auto_ha_user_configuration(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Auto-configure existing HA users as ANS identities."""
        step_id = CONFIG_FLOW_STEP_AUTO_HA_USER_CONFIGURATION_KEY
        errors: dict[str, str] = {}
        description_placeholders: dict[str, Any] = {
            "step": f"{list(self.flow_settings.flow_steps.keys()).index(step_id) + 1}/{len(self.flow_settings.flow_steps)}"
            if not self._is_reconfigure()
            else "Auto configure HA users"
        }
        last_step = self._is_last_step(step_id) or self._is_reconfigure()

        # Detect available HA users once
        if not self._available_ha_users:
            try:
                # Import locally to avoid circular imports
                from .detection import get_not_configured_ha_users

                self._available_ha_users = await get_not_configured_ha_users(self.hass)
            except Exception:
                _LOGGER.exception("Failed to detect HA users")
                errors["base"] = CONFIG_FLOW_ERROR_HA_USER_DETECTION_FAILED_KEY

        # Prepare defaults and dynamic values
        defaults = {CONFIG_FLOW_SELECTED_HA_USERS_KEY: []}
        values = {
            CONFIG_FLOW_SELECTED_HA_USERS_KEY: dict_to_select_options_list(
                self._available_ha_users
            )
        }

        if user_input is not None:
            try:
                # Store selected users (can be empty list) to flow state
                self._selected_ha_users = user_input.get(
                    CONFIG_FLOW_SELECTED_HA_USERS_KEY, []
                )
                # If this is a reconfigure flow, apply changes and finish
                if self._is_reconfigure():
                    return await self._apply_reconfigure_changes()
                # Continue to the next step in initial setup
                return await self._execute_next_step(step_id)

            except vol.Invalid as e:
                _LOGGER.debug(str(e))
                errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
            except Exception:
                _LOGGER.exception("HA user selection failed")
                errors["base"] = CONFIG_FLOW_ERROR_INVALID_HA_USER_SELECTION_KEY

            finally:
                defaults = user_input  # Keep user inputs

        return self.async_show_form(
            step_id=step_id,
            data_schema=get_auto_configure_ha_users_schema(defaults, values),
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=last_step,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure system-level setup stored in entry.data."""
        self._reconfigure_entry = self._get_reconfigure_entry()
        if self._reconfigure_entry is None:
            return self.async_abort(reason="reconfigure_entry_not_found")

        # Menu: system settings and auto user configuration
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=[
                CONFIG_FLOW_STEP_SYS_SETTINGS_KEY,
                CONFIG_FLOW_STEP_AUTO_HA_USER_CONFIGURATION_KEY,
            ],
        )

    async def _apply_reconfigure_changes(self) -> ConfigFlowResult:
        """Apply reconfiguration changes to the existing entry."""
        try:
            # Ensure the reconfigure entry is set
            if self._reconfigure_entry is None:
                return self.async_abort(reason="reconfigure_entry_not_found")

            # Determine old data object
            old_data = dict(self._reconfigure_entry.data)

            # Determine new data object
            new_data = self._create_data_object()

            # Import at runtime to avoid circular imports
            from ._helpers import update_config_entry_and_reload

            # Update the underlying config entry and reload it using the core helper
            await update_config_entry_and_reload(
                self.hass, self._reconfigure_entry, new_data
            )

            # Check and update identity entries (clamp limits if necessary)
            await self._check_and_update_affected_identities(
                self._reconfigure_entry, old_data
            )

            # Indicate the flow finished after applying reconfigure changes
            return self.async_abort(reason="reconfigure_changes_applied")

        except Exception:
            _LOGGER.exception("Failed to apply reconfigure changes")
            return self.async_abort(reason="reconfigure_failed")

    async def _check_and_update_affected_identities(
        self, config_entry: ConfigEntry, old_data: dict
    ) -> None:
        """Check and update identity entries affected by system limit changes."""
        try:
            # Import at runtime to avoid circular imports
            from ._helpers import (
                get_identity_entries,
                update_identity_entries_for_system_limits,
            )

            old_rate_limit_max = old_data.get(SYS_CONFIG_RATE_LIMIT_MAX_KEY)
            old_retries_max = old_data.get(SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY)

            # Get limits from new system settings
            new_rate_limit_max = self._system_settings.get(
                SYS_CONFIG_RATE_LIMIT_MAX_KEY
            )
            new_retries_max = self._system_settings.get(
                SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY
            )

            # Check if system limits were lowered
            limits_lowered = False
            if (
                new_rate_limit_max is not None
                and old_rate_limit_max is not None
                and new_rate_limit_max < old_rate_limit_max
            ):
                limits_lowered = True
                _LOGGER.warning(
                    "Rate limit max lowered from %s to %s; affected identities will be clamped",
                    old_rate_limit_max,
                    new_rate_limit_max,
                )

            if (
                new_retries_max is not None
                and old_retries_max is not None
                and new_retries_max < old_retries_max
            ):
                limits_lowered = True
                _LOGGER.warning(
                    "Retries max lowered from %s to %s; affected identities will be clamped",
                    old_retries_max,
                    new_retries_max,
                )

            # Update identity configs
            if limits_lowered:
                # New system limits
                adapted_values = {
                    ID_CONFIG_RETRY_ATTEMPTS_KEY: new_retries_max,
                    ID_CONFIG_RATE_LIMIT_KEY: new_rate_limit_max,
                }

                # Update default identity config
                updated_options = dict(config_entry.options)
                updated_options.update(adapted_values)
                self.hass.config_entries.async_update_entry(
                    entry=config_entry, options=updated_options
                )

                # Get all identity entries for this integration
                identity_entries = await get_identity_entries(
                    self.hass, config_entry.entry_id
                )
                if not identity_entries:
                    return
                # Update affected identity entries if limits were lowered
                await update_identity_entries_for_system_limits(
                    self.hass, identity_entries, adapted_values
                )

        except Exception:
            _LOGGER.exception("Failed while checking/updating identities")
            # don't re-raise — we don't want to break reconfigure flow

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow bound to the provided config_entry."""
        return ANSOptionsFlowHandler()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentries supported by this integration."""
        return {"identity": IdentityConfigFlow}


class ANSOptionsFlowHandler(OptionsFlow, ANSMainEntryFlowBase):
    """Options flow to edit the default identity template."""

    def __init__(self) -> None:
        """Initialize options flow with the config entry reference."""
        # Configure flow settings
        flow_settings = FlowSettings(
            flow_steps={
                CONFIG_FLOW_STEP_ID_DEFAULT_BASIC_SETTINGS_KEY: self.async_step_default_identity_basic_settings,
                CONFIG_FLOW_STEP_ID_DEFAULT_CHANNEL_MAPPING_KEY: self.async_step_default_identity_channel_mapping,
                CONFIG_FLOW_STEP_ID_DEFAULT_DND_SETTINGS_KEY: self.async_step_default_identity_dnd_settings,
            },
            force_steps=True,  # Always show identity steps in options
        )
        super().__init__(flow_settings)

    def _is_reconfigure(self) -> bool:
        """Return True if this flow runs in reconfigure mode."""
        return False

    async def _store_entry(self) -> ConfigFlowResult:
        """Store the updated identity defaults as the options result."""
        # Return options in the shape expected by callers
        return self.async_create_entry(data=self._create_options_object())

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entry point for options — redirect to first identity template step."""
        # Load system settings (works for dict or MappingProxyType)
        if isinstance(self.config_entry.data, (dict, MappingProxyType)):
            self._system_settings = dict(self.config_entry.data)
        else:
            self._system_settings = {}

        # Load identity defaults from options
        if isinstance(self.config_entry.options, (dict, MappingProxyType)):
            self._identity_defaults = dict(self.config_entry.options)
        else:
            self._identity_defaults = {}

        # Update validation context
        self._update_validation_context(self._system_settings)

        # Start with the first default identity settings config step
        return await self.async_step_default_identity_basic_settings()
