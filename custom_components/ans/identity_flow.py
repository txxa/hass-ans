"""Identity config and options flows for ANS integration (refactored).

This module implements:
- ANSSubEntryFlowBase: helper base for identity flows
- IdentityConfigFlow: ConfigFlow to create a new identity (sub-entry)
- IdentityOptionsFlow: OptionsFlow to edit an existing identity

The refactor centralizes the shared step implementations (basic settings, channel mapping, DND)
inside ANSSubEntryFlowBase so both ConfigFlow and OptionsFlow reuse the exact same logic
and validation.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigSubentry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant

from .config_validator import ConfigValidator, FieldValidationError
from .const import (
    CONFIG_FLOW_SELECTED_HA_USERS_KEY,
    ID_CONFIG_CHANNELS_KEY,
    ID_CONFIG_CONFIGURED_CHANNELS_KEY,
    ID_CONFIG_CRITICALITY_LEVELS_KEY,
    ID_CONFIG_EMAIL_KEY,
    ID_CONFIG_ID_KEY,
    ID_CONFIG_NAME_KEY,
    ID_CONFIG_NOTIFICATION_TYPES_KEY,
    ID_CONFIG_PARENT_ENTRY_ID_KEY,
    ID_CONFIG_PHONE_KEY,
    ID_CONFIG_TYPE_KEY,
    SUBENTRY_FLOW_DEFINE_IDENTITY_SETTINGS_KEY,
    SUBENTRY_FLOW_ERROR_INVALID_CHANNEL_MAPPING_KEY,
    SUBENTRY_FLOW_ERROR_INVALID_IDENTITY_DEFINITION_KEY,
    SUBENTRY_FLOW_ERROR_INVALID_IDENTITY_SELECTION_KEY,
    SUBENTRY_FLOW_SELECTED_HA_USER_KEY,
    SUBENTRY_FLOW_STEP_BASIC_SETTINGS_KEY,
    SUBENTRY_FLOW_STEP_CHANNEL_MAPPING_KEY,
    SUBENTRY_FLOW_STEP_DND_SETTINGS_KEY,
    SUBENTRY_FLOW_STEP_IDENTITY_DEFINITION_KEY,
    SUBENTRY_FLOW_STEP_IDENTITY_SELECTION_KEY,
)
from .helper import async_check_receiver_name_availability, get_main_entry
from .flow_base import ANSFlowBase, FlowSettings
from .forms import (
    dict_to_select_options_list,
    get_identity_basic_settings_schema,
    get_identity_criticality_channel_mapping_schema,
    get_identity_definition_schema,
    get_identity_dnd_settings_schema,
    get_identity_selection_schema,
)
from .models import (
    Identity,
    IdentityConfig,
    IdentityType,
    NotificationCriticality,
    NotificationType,
)

_LOGGER = logging.getLogger(__name__)


class ANSSubEntryFlowBase(ConfigSubentryFlow, ANSFlowBase):
    """Base helpers and shared steps for identity (sub-entry) config & options flows.

    This class implements the three shared steps:
    - async_step_identity_basic_settings
    - async_step_identity_channel_mapping
    - async_step_identity_dnd_settings

    The implementations validate using ConfigValidator and merge validated values
    into `self._identity_options`. When running inside an OptionsFlow (i.e. the
    instance has `self.config_entry` bound), the validated options are immediately
    persisted via `hass.config_entries.async_update_entry(...)`.
    """

    hass: HomeAssistant

    def __init__(self, flow_settings: FlowSettings) -> None:
        """Initialize the base class with the provided `FlowSettings` object."""
        super().__init__(flow_settings=flow_settings)
        self._main_entry: ConfigEntry | None = None
        self._reconfigure_entry: ConfigSubentry | None = None
        self._identity_meta: dict[str, Any] = {}
        self._identity_settings: dict[str, Any] = {}

    async def _init_main_entry_context(self) -> SubentryFlowResult | None:
        # Get main config entry
        self._main_entry = get_main_entry(self.hass)
        if not self._main_entry:
            return self.async_abort(reason="main_entry_not_found")

        # Get validation context (system limits)
        system_settings = dict(self._main_entry.data)
        self._update_validation_context(system_settings)
        # Get default identity settings
        self._identity_settings = dict(self._main_entry.options)

    async def _init_subentry_context(self) -> SubentryFlowResult | None:
        # Get subentry
        self._reconfigure_entry = self._get_reconfigure_subentry()
        if self._reconfigure_entry is None:
            return self.async_abort(reason="reconfigure_entry_not_found")

        # Get identity definition and settings
        self._identity = dict(self._reconfigure_entry.data or {})
        self._identity_settings = dict(self._reconfigure_entry.data or {})

    async def async_step_identity_basic_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Collect per-identity basic settings (retry/rate/notification types).

        Reusable by both ConfigFlow and OptionsFlow.
        """
        step_id = SUBENTRY_FLOW_STEP_BASIC_SETTINGS_KEY
        errors: dict[str, str] = {}
        last_step = False if self._is_reconfigure() else self._is_last_step(step_id)

        defaults = dict(self._identity_settings or {})

        # Build available notification types selector
        notification_types = [
            {"label": t.value.title(), "value": t.value} for t in NotificationType
        ]
        values = {ID_CONFIG_NOTIFICATION_TYPES_KEY: notification_types}

        if user_input is not None:
            try:
                # Import at runtime to avoid circular imports
                from .config_validator import ConfigValidator

                # Validate identity basic settings using the configured system limits
                schema_validated = (
                    ConfigValidator.validate_identity_basic_settings_schema(
                        user_input, self._validation_context
                    )
                )
                # Merge validated identity basic settings into the flow state
                self._identity_settings.update(schema_validated)

                # Continue to next step in config flow
                return await self._execute_next_step(step_id)

            except vol.Invalid as e:
                _LOGGER.debug(str(e))
                errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
            except Exception:
                _LOGGER.exception("Identity basic settings validation failed")
                errors["base"] = SUBENTRY_FLOW_STEP_BASIC_SETTINGS_KEY

            finally:
                defaults = user_input  # Keep user inputs

        return self.async_show_form(
            step_id=step_id,
            data_schema=get_identity_basic_settings_schema(
                defaults, self._validation_context, values
            ),
            errors=errors,
            last_step=last_step,
        )

    async def async_step_identity_channel_mapping(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Collect mapping of criticality levels to notify channels.

        Reusable by both ConfigFlow and OptionsFlow.
        """
        step_id = SUBENTRY_FLOW_STEP_CHANNEL_MAPPING_KEY
        errors: dict[str, str] = {}
        last_step = False if self._is_reconfigure() else self._is_last_step(step_id)

        # Build helper values for the form
        criticality_levels = {c.name: c.value for c in NotificationCriticality}
        channel_options = {
            ch: ch.split(".", 1)[1].replace("_", " ").title()
            for ch in self._validation_context.available_channels
        }

        # Prepare defaults and values
        defaults = dict(self._identity_settings or {})
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
                # Ensure that at least one channel is configured
                if not any(
                    schema_validated[f"{ID_CONFIG_CHANNELS_KEY}_{level.value.lower()}"]
                    for level in NotificationCriticality
                ):
                    raise ValueError("At least one channel needs to be configured")
                # Merge validated channels into the flow state
                self._identity_settings.update(schema_validated)

                # Continue to the next step in initial setup
                return await self._execute_next_step(step_id)

            except vol.Invalid as e:
                _LOGGER.debug(str(e))
                errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
            except ValueError as e:
                _LOGGER.debug(str(e))
                errors["base"] = SUBENTRY_FLOW_ERROR_INVALID_CHANNEL_MAPPING_KEY
            except Exception:
                _LOGGER.exception("Identity channel mapping validation failed")
                errors["base"] = SUBENTRY_FLOW_STEP_CHANNEL_MAPPING_KEY

            finally:
                defaults = user_input  # Keep user inputs

        return self.async_show_form(
            step_id=step_id,
            data_schema=get_identity_criticality_channel_mapping_schema(
                defaults, values
            ),
            errors=errors,
            last_step=last_step,
        )

    async def async_step_identity_dnd_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Collect DND settings for the identity.

        Reusable by both ConfigFlow and OptionsFlow.
        On success: in ConfigFlow it continues to next step (or store entry), in OptionsFlow it persists changes immediately.
        """
        step_id = SUBENTRY_FLOW_STEP_DND_SETTINGS_KEY
        errors: dict[str, str] = {}
        last_step = self._is_last_step(step_id)

        defaults = dict(self._identity_settings or {})
        values: dict[str, Any] = {}

        if user_input is not None:
            try:
                # Import at runtime to avoid circular imports
                from .config_validator import ConfigValidator

                # Validate identity config template
                schema_validated = (
                    ConfigValidator.validate_identity_dnd_settings_schema(user_input)
                )
                # Merge validated DND settings into the flow state
                self._identity_settings.update(schema_validated)

                # Continue to the next step in initial setup
                return await self._execute_next_step(step_id)

            except vol.Invalid as e:
                _LOGGER.debug(str(e))
                errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
            except Exception:
                _LOGGER.exception("Identity DND settings validation failed")
                errors["base"] = SUBENTRY_FLOW_STEP_DND_SETTINGS_KEY

            finally:
                defaults = user_input  # Keep user inputs

        return self.async_show_form(
            step_id=step_id,
            data_schema=get_identity_dnd_settings_schema(defaults, values),
            errors=errors,
            last_step=last_step,
        )


class IdentityConfigFlow(ANSSubEntryFlowBase):
    """Config flow to create an identity (ANS sub-entry)."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the identity config flow."""
        flow_settings = FlowSettings(
            flow_steps={
                SUBENTRY_FLOW_STEP_IDENTITY_SELECTION_KEY: self.async_step_identity_selection,
                SUBENTRY_FLOW_STEP_IDENTITY_DEFINITION_KEY: self.async_step_identity_definition,
                SUBENTRY_FLOW_STEP_BASIC_SETTINGS_KEY: self.async_step_identity_basic_settings,
                SUBENTRY_FLOW_STEP_CHANNEL_MAPPING_KEY: self.async_step_identity_channel_mapping,
                SUBENTRY_FLOW_STEP_DND_SETTINGS_KEY: self.async_step_identity_dnd_settings,
            },
            # skip_steps=[
            #     SUBENTRY_FLOW_STEP_BASIC_SETTINGS_KEY,
            #     SUBENTRY_FLOW_STEP_CHANNEL_MAPPING_KEY,
            #     SUBENTRY_FLOW_STEP_DND_SETTINGS_KEY,
            # ],
            # skip_condition_key=SUBENTRY_FLOW_DEFINE_IDENTITY_SETTINGS_KEY,
        )
        super().__init__(flow_settings=flow_settings)
        # self._selected_ha_user: dict[str, Any] | None = None
        self._identity: dict[str, Any] = {}
        self._identity_settings: dict[str, Any] = {}

    def _is_reconfigure(self) -> bool:
        """Return True if this flow runs in reconfigure mode."""
        return self._reconfigure_entry is not None

    async def _store_entry(self) -> SubentryFlowResult:
        """Create the identity config entry using collected metadata & options."""
        try:
            main_entry = get_main_entry(self.hass)
            if main_entry is None:
                _LOGGER.error("ANS main entry not found when creating identity")
                return self.async_abort(reason="main_entry_missing")

            identity_id = self._identity.get(ID_CONFIG_ID_KEY)
            id_type = self._identity.get(ID_CONFIG_TYPE_KEY)
            name = self._identity.get(ID_CONFIG_NAME_KEY)
            email = self._identity.get(ID_CONFIG_EMAIL_KEY)
            phone = self._identity.get(ID_CONFIG_PHONE_KEY)

            if not id_type:
                raise ValueError("ID Type is required for creating an identity")
            if not name:
                raise ValueError("Name is required for creating an identity")
            if not identity_id:
                raise ValueError("ID is required for creating an identity")

            # Final validation via dataclass construction
            identity_obj = Identity(
                id=identity_id,
                type=IdentityType(id_type),
                name=name,
                email=email,
                phone=phone,
            )
            data = identity_obj.to_dict()
            data[ID_CONFIG_PARENT_ENTRY_ID_KEY] = main_entry.entry_id

            # Identity config options
            identity_config_obj = IdentityConfig.from_dict(self._identity_settings)
            data.update(identity_config_obj.to_dict())

            title = f"{name} (id={identity_id}, type={id_type})"

            if self._is_reconfigure() and self._reconfigure_entry and self._main_entry:
                # Update existing entry
                try:
                    self.async_update_and_abort(
                        entry=self._main_entry,
                        subentry=self._reconfigure_entry,
                        data=data,
                    )
                except Exception:
                    _LOGGER.exception("Failed to update identity entry")
                    return self.async_abort(reason="update_entry_failed")
                return self.async_abort(reason="reconfigure_complete")

            return self.async_create_entry(title=title, data=data)
        except FieldValidationError as fv:
            errors = {fv.field or "base": fv.message}
            _LOGGER.debug("Field validation error on final create: %s", fv)
            return self.async_show_form(
                step_id="confirm", data_schema=vol.Schema({}), errors=errors
            )
        except Exception:
            _LOGGER.exception("Failed to create identity entry")
            return self.async_abort(reason="create_entry_failed")

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Start the identity config flow (redirect to selection)."""
        # Init main config entry context
        await self._init_main_entry_context()

        return await self.async_step_identity_selection(user_input)

    async def async_step_identity_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose HA user or custom identity."""
        step_id = SUBENTRY_FLOW_STEP_IDENTITY_SELECTION_KEY
        errors: dict[str, str] = {}
        last_step = self._is_last_step(step_id)

        try:
            from .helper import get_not_configured_ha_users

            available_ha_users = await get_not_configured_ha_users(self.hass)
            ha_users_list = {
                CONFIG_FLOW_SELECTED_HA_USERS_KEY: dict_to_select_options_list(
                    available_ha_users
                )
            }
        except Exception:
            _LOGGER.debug("Could not detect HA users for identity selection")

        defaults = {}

        if user_input is not None:
            try:
                identity_id = uuid.uuid4().hex
                identity_name = None
                identity_type = user_input.get(ID_CONFIG_TYPE_KEY)

                if identity_type and identity_type == IdentityType.HA_USER.value:
                    selected_user = user_input.get(SUBENTRY_FLOW_SELECTED_HA_USER_KEY)
                    for (
                        u_id,
                        u_name,
                    ) in available_ha_users.items():
                        if u_id == selected_user:
                            identity_id = u_id
                            identity_name = u_name
                            break

                user_input[ID_CONFIG_ID_KEY] = identity_id

                if identity_name:
                    user_input[ID_CONFIG_NAME_KEY] = identity_name

                schema_validated = ConfigValidator.validate_identity_selection_schema(
                    user_input
                )

                self._identity.update(schema_validated)

                return await self._execute_next_step(step_id)
            except vol.Invalid as e:
                _LOGGER.debug("vol.Invalid in identity definition: %s", e)
                errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
            except Exception:
                _LOGGER.exception("Identity selection validation failed")
                errors["base"] = SUBENTRY_FLOW_ERROR_INVALID_IDENTITY_SELECTION_KEY

            finally:
                defaults = user_input  # Keep user inputs

        return self.async_show_form(
            step_id=step_id,
            data_schema=get_identity_selection_schema(defaults, ha_users_list),
            errors=errors,
            last_step=last_step,
        )

    async def async_step_identity_definition(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Collect identity basic metadata (name/email/phone)."""
        step_id = SUBENTRY_FLOW_STEP_IDENTITY_DEFINITION_KEY
        errors: dict[str, str] = {}
        last_step = False if self._is_reconfigure() else self._is_last_step(step_id)

        values = {}

        # Prepare defaults: if reconfigure use existing entry's options; otherwise use in-flow defaults
        if self._is_reconfigure() and self._reconfigure_entry:
            defaults = dict(self._reconfigure_entry.data or {})
        else:
            defaults = {
                key: value
                for key, value in self._identity.items()
                if key == ID_CONFIG_NAME_KEY
            }

        if user_input is not None:
            try:
                schema_validated = ConfigValidator.validate_identity_definition_schema(
                    user_input
                )
                if (
                    not await async_check_receiver_name_availability(
                        self.hass, schema_validated[ID_CONFIG_NAME_KEY]
                    )
                    and not self._is_reconfigure()
                ):
                    raise vol.Invalid(
                        message=f"Name '{schema_validated[ID_CONFIG_NAME_KEY]}' is already used.",
                        path=[ID_CONFIG_NAME_KEY],
                    )

                self._identity.update(schema_validated)

                return await self._execute_next_step(step_id)

            except vol.Invalid as e:
                _LOGGER.debug("vol.Invalid in identity definition: %s", e)
                errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
            except Exception:
                _LOGGER.exception("Identity definition validation failed")
                errors["base"] = SUBENTRY_FLOW_ERROR_INVALID_IDENTITY_DEFINITION_KEY

            finally:
                defaults = user_input

        return self.async_show_form(
            step_id=step_id,
            data_schema=get_identity_definition_schema(defaults, values),
            errors=errors,
            last_step=last_step,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """User flow to modify an existing location."""
        # Init main config entry context
        await self._init_main_entry_context()
        # Force showing identity settings steps
        self.flow_settings.force_steps = True

        # Init subentry context
        await self._init_subentry_context()

        return await self.async_step_identity_definition(user_input)
