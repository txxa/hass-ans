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
from .detection import async_check_receiver_name_availability, get_main_entry
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


# def _slugify(name: str) -> str:
#     """Lightweight slugify for identity id generation.

#     Keeps only alphanumeric and dash/underscore. Lowercases and replaces spaces with underscore.
#     """
#     if not isinstance(name, str) or not name:
#         raise ValueError("Name must be a non-empty string")
#     s = name.strip().lower()
#     s = re.sub(r"\s+", "_", s)
#     return re.sub(r"[^a-z0-9_\-]", "", s)


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
        # if not self.hass:
        #     raise ValueError("hass must be set"

    # async def _async_get_main_entry(self) -> ConfigEntry | None:
    #     """Return the main ANS config entry or None if not found."""
    #     entries = list(self.hass.config_entries.async_entries(DOMAIN))
    #     # Prefer the entry that has unique_id == DOMAIN (main entry created in main flow)
    #     for entry in entries:
    #         try:
    #             if getattr(entry, "unique_id", None) == DOMAIN:
    #                 return entry
    #         except Exception:
    #             continue
    #     # Fallback to first if present
    #     return entries[0] if entries else None

    # async def _async_build_validation_context(self) -> ValidationContext:
    #     """Build ValidationContext from main entry/system settings and available channels."""
    #     main_entry = await self._async_get_main_entry()
    #     system_limits = None
    #     available_channels: list[str] = []

    #     try:
    #         if main_entry and isinstance(main_entry.data, (dict, MappingProxyType)):
    #             _data = dict(main_entry.data)
    #             system_limits = {}
    #             # Known keys used by the main flow
    #             if "retry_attempts_max" in _data:
    #                 system_limits["retry_attempts_max"] = _data.get(
    #                     "retry_attempts_max"
    #                 )
    #             if "rate_limit_max" in _data:
    #                 system_limits["rate_limit_max"] = _data.get("rate_limit_max")
    #             # Collect enabled channels from main entry if present
    #             enabled = _data.get("enabled_channels") or []
    #             available_channels = [str(ch) for ch in enabled]

    #         # Augment available channels with current notify services
    #         services = self.hass.services.async_services()
    #         notify_services = services.get("notify", {})
    #         for svc in sorted(notify_services):
    #             full = f"notify.{svc}"
    #             if full not in available_channels:
    #                 available_channels.append(full)
    #     except Exception:
    #         _LOGGER.exception(
    #             "Failed to build validation context; falling back to defaults"
    #         )

    #     vc = ValidationContext(
    #         system_limits=system_limits, available_channels=available_channels
    #     )
    #     self._validation_context = vc
    #     return vc

    # async def _async_get_notify_service_options(self) -> list[dict[str, str]]:
    #     """Return select option dicts for current notify services.

    #     Each option is {'label': friendly, 'value': 'notify.xyz'}
    #     """
    #     try:
    #         services = self.hass.services.async_services()
    #         notify_services = services.get("notify", {})
    #         options: list[dict[str, str]] = []
    #         for svc in sorted(notify_services):
    #             full = f"notify.{svc}"
    #             label = svc.replace("_", " ").title()
    #             options.append({"label": label, "value": full})
    #         return options
    #     except Exception:
    #         _LOGGER.exception("Failed to get notify services")
    #         return []

    # def _map_validation_exception_to_errors(
    #     self, exc: Exception, schema_keys: list[str]
    # ) -> dict[str, str]:
    #     """Map ConfigValidator or voluptuous exceptions to HA form errors dict."""
    #     errors: dict[str, str] = {}
    #     if isinstance(exc, FieldValidationError):
    #         fld = exc.field or "base"
    #         if fld in schema_keys:
    #             errors[fld] = exc.message
    #         else:
    #             errors["base"] = exc.message
    #         return errors

    #     if isinstance(exc, vol.Invalid):
    #         try:
    #             if exc.path:
    #                 p = (
    #                     exc.path[0]
    #                     if isinstance(exc.path, (list, tuple)) and exc.path
    #                     else exc.path
    #                 )
    #                 key = str(p)
    #                 if key in schema_keys:
    #                     errors[key] = str(exc)
    #                     return errors
    #             errors["base"] = str(exc)
    #             return errors
    #         except Exception:
    #             errors["base"] = str(exc)
    #             return errors

    #     errors["base"] = str(exc)
    #     return errors

    # def _identity_schema_keys_for_step(self, step_id: str) -> list[str]:
    #     """Return expected schema keys for a given step (used for error mapping)."""
    #     if step_id == SUBENTRY_FLOW_STEP_BASIC_SETTINGS_KEY:
    #         return [
    #             ID_CONFIG_RETRY_ATTEMPTS_KEY,
    #             ID_CONFIG_RATE_LIMIT_KEY,
    #             ID_CONFIG_NOTIFICATION_TYPES_KEY,
    #             ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY,
    #         ]
    #     if step_id == SUBENTRY_FLOW_STEP_CHANNEL_MAPPING_KEY:
    #         return [
    #             f"{ID_CONFIG_CHANNELS_KEY}_{c.value.lower()}"
    #             for c in NotificationCriticality
    #         ]
    #     if step_id == SUBENTRY_FLOW_STEP_DND_SETTINGS_KEY:
    #         return [
    #             ID_CONFIG_DND_ENABLED_KEY,
    #             ID_CONFIG_DND_START_KEY,
    #             ID_CONFIG_DND_END_KEY,
    #             ID_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY,
    #         ]
    #     return []

    # def _build_identity_data(
    #     self,
    #     identity_id: str,
    #     id_type: str,
    #     name: str,
    #     email: str | None,
    #     phone: str | None,
    # ) -> dict:
    #     """Construct immutable identity metadata for config_entry.data."""
    #     return {
    #         ID_CONFIG_ID_KEY: str(identity_id),
    #         ID_CONFIG_TYPE_KEY: id_type,
    #         ID_CONFIG_NAME_KEY: name,
    #         ID_CONFIG_EMAIL_KEY: email,
    #         ID_CONFIG_PHONE_KEY: phone,
    #         "parent_entry_id": None,  # filled by caller
    #         "created_at": dt_util.utcnow().isoformat(),
    #         "version": 1,
    #     }

    # def _build_identity_options_from_flow_state(self) -> dict:
    #     """Return options dict based on self._identity_options merged with defaults."""
    #     base = IdentityConfig.default().to_dict()
    #     merged = dict(base)
    #     merged.update(self._identity_settings or {})
    #     merged["version"] = merged.get("version", 1)
    #     return merged

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

        # # Ensure validation context is up-to-date
        # await self._async_build_validation_context()

        # defaults = dict(self._identity_settings or self._default_settings or {})
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

                # # Persist immediately if in options flow
                # if getattr(self, "config_entry", None) is not None:
                #     # Update the underlying config entry options
                #     try:
                #         self.hass.config_entries.async_update_entry(
                #             self.config_entry, options=self._identity_settings
                #         )
                #     except Exception:
                #         _LOGGER.exception(
                #             "Failed to update identity options during options flow"
                #         )
                #     # Return to init/menu
                #     # return await self.async_step_init()

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

            # except Exception as exc:  # vol.Invalid or FieldValidationError
            # _LOGGER.debug("Validation exception in basic settings: %s", exc)
            # errors = self._map_validation_exception_to_errors(
            #     exc, self._identity_schema_keys_for_step(step_id)
            # )
            # defaults = user_input

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

        # await self._async_build_validation_context()
        # channel_options = await self._async_get_notify_service_options()

        # defaults = dict(self._identity_settings or {})
        # criticality_levels = [
        #     {"label": c.value.title(), "value": c.value}
        #     for c in NotificationCriticality
        # ]
        # values = {
        #     "criticality_levels": criticality_levels,
        #     ID_CONFIG_CONFIGURED_CHANNELS_KEY: channel_options,
        # }

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

                # if getattr(self, "config_entry", None) is not None:
                #     try:
                #         self.hass.config_entries.async_update_entry(
                #             self.config_entry, options=self._identity_settings
                #         )
                #     except Exception:
                #         _LOGGER.exception(
                #             "Failed to update identity options during channel mapping options flow"
                #         )
                #     # return await self.async_step_init()

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

            # except Exception as exc:
            #     _LOGGER.debug("Validation exception in channel mapping: %s", exc)
            #     errors = self._map_validation_exception_to_errors(
            #         exc, self._identity_schema_keys_for_step(step_id)
            #     )
            #     defaults = user_input

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

                # if getattr(self, "config_entry", None) is not None:
                #     try:
                #         self.hass.config_entries.async_update_entry(
                #             self.config_entry, options=self._identity_settings
                #         )
                #     except Exception:
                #         _LOGGER.exception(
                #             "Failed to update identity options during DND options flow"
                #         )
                #     # return await self.async_step_init()

                # # Final validation completed for config flow -> proceed to store/create
                # return await self._execute_next_step(step_id)

                # except Exception as exc:
                #     _LOGGER.debug("Validation exception in DND settings: %s", exc)
                #     errors = self._map_validation_exception_to_errors(
                #         exc, self._identity_schema_keys_for_step(step_id)
                #     )
                #     defaults = user_input

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
    # config_entry: ConfigEntry | None = None

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
            skip_steps=[
                SUBENTRY_FLOW_STEP_BASIC_SETTINGS_KEY,
                SUBENTRY_FLOW_STEP_CHANNEL_MAPPING_KEY,
                SUBENTRY_FLOW_STEP_DND_SETTINGS_KEY,
            ],
            skip_condition_key=SUBENTRY_FLOW_DEFINE_IDENTITY_SETTINGS_KEY,
        )
        super().__init__(flow_settings=flow_settings)
        # self._selected_ha_user: dict[str, Any] | None = None
        self._identity: dict[str, Any] = {}
        self._identity_settings: dict[str, Any] = {}

    # @staticmethod
    # @callback
    # def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
    #     """Return options flow for an identity entry."""
    #     return IdentityOptionsFlow()

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
                # identity_id = _slugify(name)

            # data = self._build_identity_data(identity_id, id_type, name, email, phone)
            # data["parent_entry_id"] = main_entry.entry_id

            # options = self._build_identity_options_from_flow_state()

            # Final validation via dataclass construction
            identity_obj = Identity(
                id=identity_id,
                type=IdentityType(id_type),
                name=name,
                email=email,
                phone=phone,
            )
            data = identity_obj.to_dict()
            data["parent_entry_id"] = main_entry.entry_id
            # identity_config_obj = IdentityConfig.from_dict(options)

            # Identity config options
            identity_config_obj = IdentityConfig.from_dict(
                # self._build_identity_options_from_flow_state()
                self._identity_settings
            )
            data.update(identity_config_obj.to_dict())

            # await self.async_set_unique_id(str(identity_obj.id))
            # self._abort_if_unique_id_configured()
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

    # async def _init_main_entry_context(self) -> SubentryFlowResult | None:
    #     # Get main config entry
    #     self._main_entry = await self._async_get_main_entry()
    #     if not self._main_entry:
    #         return self.async_abort(reason="main_entry_not_found")

    #     # Get validation context (system limits)
    #     system_settings = dict(self._main_entry.data)
    #     self._update_validation_context(system_settings)
    #     # Get default identity settings
    #     self._default_settings = dict(self._main_entry.options)

    # async def _init_subentry_context(self) -> SubentryFlowResult | None:
    #     # Get subentry
    #     self._reconfigure_entry = self._get_reconfigure_subentry()
    #     if self._reconfigure_entry is None:
    #         return self.async_abort(reason="reconfigure_entry_not_found")

    #     # Get identity definition and settings
    #     self._identity = dict(self._reconfigure_entry.data or {})
    #     self._identity_settings = dict(self._reconfigure_entry.data or {})

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Start the identity config flow (redirect to selection)."""

        # # Get main config entry
        # self._main_entry = await self._async_get_main_entry()
        # if not self._main_entry:
        #     return self.async_abort(reason="main_entry_not_found")

        # # Get validation context (system limits)
        # system_settings = dict(self._main_entry.data)
        # self._update_validation_context(system_settings)
        # # Get default identity settings
        # self._default_settings = dict(self._main_entry.options)

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
            from .detection import get_not_configured_ha_users

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

                self._flow_data[SUBENTRY_FLOW_DEFINE_IDENTITY_SETTINGS_KEY] = (
                    user_input.pop(SUBENTRY_FLOW_DEFINE_IDENTITY_SETTINGS_KEY)
                )

                # self._selected_ha_user = {"id": identity_id}

                # mode = user_input.get("identity_mode")
                # if mode == "ha_user":
                #     ha_user_id = user_input.get("ha_user_id")
                #     if ha_user_id:
                #         self._selected_ha_user = {"id": ha_user_id}
                #     return await self.async_step_identity_definition()
                # else:
                #     self._selected_ha_user = None
                #     return await self.async_step_identity_definition()

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
        # defaults = {}

        # for key, value in self._identity.items():
        #     if key == ID_CONFIG_ID_KEY:
        #         defaults[key] = value
        #         values[key] = value
        #     if key == ID_CONFIG_NAME_KEY:
        #         defaults[key] = value

        # defaults = {
        #     key: value
        #     for key, value in self._identity.items()
        #     if key == ID_CONFIG_NAME_KEY
        # }

        # Prepare defaults: if reconfigure use existing entry's options; otherwise use in-flow defaults
        if self._is_reconfigure() and self._reconfigure_entry:
            defaults = dict(self._reconfigure_entry.data or {})
            # defaults.setdefault(SUBENTRY_FLOW_DEFINE_IDENTITY_SETTINGS_KEY, False)
            # self.flow_settings.force_steps = True
        else:
            defaults = {
                key: value
                for key, value in self._identity.items()
                if key == ID_CONFIG_NAME_KEY
            }
            # defaults = dict(self._system_settings or {})
        # defaults.setdefault(SUBENTRY_FLOW_DEFINE_IDENTITY_SETTINGS_KEY, True)

        # if self._selected_ha_user:
        #     try:
        #         user_id = self._selected_ha_user.get("id")
        #         if not user_id or not isinstance(user_id, str):
        #             raise
        #         ha_user = {"id": user_id, "name": None}
        #         u = await self.hass.auth.async_get_user(user_id)
        #         if u:
        #             ha_user["name"] = u.name
        #             defaults = {
        #                 ID_CONFIG_ID_KEY: u.id,
        #                 ID_CONFIG_NAME_KEY: ha_user["name"],
        #             }
        #     except Exception:
        #         _LOGGER.debug("Could not resolve HA user details for prefill")

        # if self._identity_meta:
        #     defaults.update(self._identity_meta)

        if user_input is not None:
            try:
                # user_input[ID_CONFIG_TYPE_KEY] = self._identity.get(
                #     SUBENTRY_FLOW_IDENTITY_TYPE_SELECTION_KEY
                # )
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

                # if self._selected_ha_user:
                #     identity_id = user_input.get(
                #         ID_CONFIG_ID_KEY
                #     ) or self._selected_ha_user.get("id")
                #     id_type = IdentityType.HA_USER.value
                # else:
                #     name = user_input.get(ID_CONFIG_NAME_KEY)
                #     if not name:
                #         raise ValueError("Name is required")
                #     identity_id = _slugify(name)
                #     id_type = IdentityType.VIRTUAL.value

                # identity_id = self._identity.get(ID_CONFIG_ID_KEY)
                # identity_type = self._identity.get(ID_CONFIG_TYPE_KEY)

                # self._identity_meta.update(
                #     {
                #         ID_CONFIG_ID_KEY: identity_id,
                #         ID_CONFIG_TYPE_KEY: identity_type,
                #         ID_CONFIG_NAME_KEY: user_input.get(ID_CONFIG_NAME_KEY),
                #         ID_CONFIG_EMAIL_KEY: user_input.get(ID_CONFIG_EMAIL_KEY),
                #         ID_CONFIG_PHONE_KEY: user_input.get(ID_CONFIG_PHONE_KEY),
                #     }
                # )

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
        # # Retrieve the parent config entry for reference.
        # config_entry = self._get_entry()
        # # Retrieve the specific subentry targeted for update.
        # config_subentry = self._get_reconfigure_subentry()

        # # Get subentry
        # self._reconfigure_entry = self._get_reconfigure_subentry()
        # if self._reconfigure_entry is None:
        #     return self.async_abort(reason="reconfigure_entry_not_found")

        # self._identity = dict(self._reconfigure_entry.data or {})
        # self._identity_settings = dict(self._reconfigure_entry.data or {})

        # # Get main config entry
        # self._main_entry = await self._async_get_main_entry()
        # if not self._main_entry:
        #     return self.async_abort(reason="main_entry_not_found")

        # system_settings = dict(self._main_entry.data)
        # self._update_validation_context(system_settings)

        # Init main config entry context
        await self._init_main_entry_context()
        # Force showing identity settings steps
        self.flow_settings.force_steps = True

        # Init subentry context
        await self._init_subentry_context()

        return await self.async_step_identity_definition(user_input)


# class IdentityOptionsFlow(OptionsFlow, ANSSubEntryFlowBase):
#     """Options flow for editing an identity's options."""

#     def __init__(self) -> None:
#         """Initialize the identity options flow."""
#         flow_settings = FlowSettings(
#             flow_steps={
#                 SUBENTRY_FLOW_STEP_BASIC_SETTINGS_KEY: self.async_step_identity_basic_settings,
#                 SUBENTRY_FLOW_STEP_CHANNEL_MAPPING_KEY: self.async_step_identity_channel_mapping,
#                 SUBENTRY_FLOW_STEP_DND_SETTINGS_KEY: self.async_step_identity_dnd_settings,
#             },
#             force_steps=True,
#         )
#         super().__init__(flow_settings=flow_settings)

#     async def _store_entry(self) -> SubentryFlowResult:
#         """Return create_entry with updated options (legacy pattern used by callers)."""
#         return self.async_create_entry(data=self._identity_options)

#     async def async_step_init(
#         self, user_input: dict[str, Any] | None = None
#     ) -> SubentryFlowResult:
#         """Entry point for options editing. Load current options and metadata then show menu."""
#         if isinstance(self.config_entry.options, (dict, MappingProxyType)):
#             self._identity_options = dict(self.config_entry.options)
#         else:
#             self._identity_options = {}

#         if isinstance(self.config_entry.data, (dict, MappingProxyType)):
#             self._identity_meta = dict(self.config_entry.data)
#         else:
#             self._identity_meta = {}

#         await self.async_build_validation_context()

#         return self.async_show_menu(
#             step_id="init",
#             menu_options=[
#                 SUBENTRY_FLOW_STEP_BASIC_SETTINGS_KEY,
#                 SUBENTRY_FLOW_STEP_CHANNEL_MAPPING_KEY,
#                 SUBENTRY_FLOW_STEP_DND_SETTINGS_KEY,
#             ],
#         )
