"""Config and options flow base implementations for the ANS integration."""

from __future__ import annotations

import logging
from abc import ABCMeta, abstractmethod
from typing import Any

from homeassistant.data_entry_flow import _FlowResultT

from .config_validator import (
    ValidationContext,
)
from .const import (
    DEFAULT_ENABLED_CHANNELS,
    DEFAULT_RATE_LIMIT_MAX,
    DEFAULT_RETRIES_MAX,
    SYS_CONFIG_ENABLED_CHANNELS_KEY,
    SYS_CONFIG_RATE_LIMIT_MAX_KEY,
    SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY,
)

_LOGGER = logging.getLogger(__name__)


class FlowSettings:
    """Settings class for flow configuration."""

    def __init__(
        self,
        flow_steps: dict[str, Any],
        skip_steps: list[str] | None = None,
        skip_condition_key: str | None = None,
        force_steps: bool = False,
    ) -> None:
        """Initialize flow settings.

        Args:
            flow_steps: Ordered dict of flow step methods
            skip_steps: List of step IDs that can be conditionally skipped
            skip_condition_key: Key in system settings that controls step skipping
            force_steps: If True, never skip steps regardless of condition

        """
        self.flow_steps = flow_steps
        self.skip_steps = skip_steps or []
        self.skip_condition_key = skip_condition_key
        self.force_steps = force_steps


class ANSFlowBase(metaclass=ABCMeta):
    """Base class for flow logic shared between ConfigFlow and OptionsFlow."""

    def __init__(self, flow_settings: FlowSettings) -> None:
        """Initialize any flow state."""
        self.flow_settings = flow_settings
        # Flow-wide state
        # self._system_settings: dict[str, Any] = {}
        # self._identity_defaults: dict[str, Any] = {}
        self._flow_data: dict[str, Any] = {}
        self._validation_context: ValidationContext = ValidationContext()

    # The HA flow classes (ConfigFlow / OptionsFlow) already implement these methods.
    # @abstractmethod
    # def async_create_entry(self, **kwargs) -> FlowResult:
    #     """Provided by HA base class."""  # noqa: D401

    # @abstractmethod
    # def async_show_form(self, **kwargs) -> FlowResult:
    #     """Provided by HA base class."""  # noqa: D401

    # @abstractmethod
    # def async_abort(self, **kwargs) -> FlowResult:
    #     """Provided by HA base class."""  # noqa: D401
    @abstractmethod
    def _is_reconfigure(self) -> bool:
        """Return True if this flow runs in reconfigure mode."""

    def _is_last_step(self, current_step_id: str) -> bool:
        """Return True if `current_step_id` is last enabled step in the flow."""
        flow_steps = list(self.flow_settings.flow_steps.keys())
        try:
            current_index = flow_steps.index(current_step_id)
            next_step_id = flow_steps[current_index + 1]
        except (ValueError, IndexError):
            # Unknown step; treat as last to avoid infinite loops
            return True

        if not next_step_id or self._should_skip_step(next_step_id):
            return True

        # next_index = current_index + 1
        # if next_index >= len(flow_steps) or self._should_skip_step(flow_steps[next_index]):
        #     return True

        # for next_step_id in flow_steps[current_index + 1 :]:
        #     if self.flow_settings.flow_steps.get(next_step_id):
        #         return False
        # return True
        return False

    def _should_skip_step(self, step_id: str) -> bool:
        """Check if a step should be skipped based on conditions."""
        # Don't skip if step isn't in skip list
        if step_id not in self.flow_settings.skip_steps:
            return False

        # Don't skip if steps are forced
        if self.flow_settings.force_steps:
            return False

        # Skip if condition key exists and is False
        if self.flow_settings.skip_condition_key:
            return not self._flow_data.get(self.flow_settings.skip_condition_key, False)

        return False

    async def _execute_next_step(self, current_step_id: str) -> _FlowResultT:
        """Find and execute the next enabled step; store entry if none left."""
        flow_steps = list(self.flow_settings.flow_steps.keys())
        current_index = flow_steps.index(current_step_id)
        # try:
        #     current_index = flow_steps.index(current_step_id)
        # except ValueError:
        #     # Unknown step; TODO: remove fallback to creating main entry
        #     return await self._store_entry()

        for next_step_id in flow_steps[current_index + 1 :]:
            # Skip step if conditions are met
            if self._should_skip_step(next_step_id):
                continue

            next_step = self.flow_settings.flow_steps.get(next_step_id)
            if next_step:
                return await next_step()

        return await self._store_entry()

    def _update_validation_context(self, system_settings: dict) -> None:
        """Update the validation context with current system limits."""
        if system_settings:
            system_limits = {
                SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY: system_settings.get(
                    SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY, DEFAULT_RETRIES_MAX
                ),
                SYS_CONFIG_RATE_LIMIT_MAX_KEY: system_settings.get(
                    SYS_CONFIG_RATE_LIMIT_MAX_KEY, DEFAULT_RATE_LIMIT_MAX
                ),
            }
            available_channels = list(
                system_settings.get(
                    SYS_CONFIG_ENABLED_CHANNELS_KEY, DEFAULT_ENABLED_CHANNELS
                )
            )
            self._validation_context = ValidationContext(
                system_limits=system_limits, available_channels=available_channels
            )

    @abstractmethod
    async def _store_entry(self) -> _FlowResultT:
        """Async hook that creates/stores the entry (config or options flow)."""


# class IdentityStepsBase(ANSFlowBase):
#     """Shared implementation of the identity-related steps."""

#     def __init__(self, flow_settings: FlowSettings) -> None:
#         """Initialize any flow state."""
#         super().__init__(flow_settings=flow_settings)

#     async def async_step_default_identity_basic_settings(
#         self, user_input: dict[str, Any] | None = None
#     ) -> ConfigFlowResult:
#         """Collect basic default identity settings (template) used when creating identities."""
#         step_id = CONFIG_FLOW_STEP_ID_DEFAULT_BASIC_SETTINGS_KEY
#         errors: dict[str, str] = {}
#         description_placeholders: dict[str, Any] = {
#             "step": f"{list(self.flow_settings.flow_steps.keys()).index(step_id) + 1}/{len(self.flow_settings.flow_steps)}"
#         }
#         last_step = self._is_last_step(step_id)

#         # Build helper values
#         notification_types = {t.name: t.value for t in NotificationType}
#         channel_options = {
#             ch: ch.split(".", 1)[1].replace("_", " ").title()
#             for ch in self._system_settings.get(SYS_CONFIG_ENABLED_CHANNELS_KEY, [])
#         }

#         # Prepare defaults: if reconfigure use existing entry's options; otherwise use in-flow defaults
#         if self._is_reconfigure() and self._reconfigure_entry:
#             defaults = dict(self._reconfigure_entry.options or {})
#         else:
#             defaults = dict(self._identity_defaults or {})

#         values = {}
#         values[ID_CONFIG_NOTIFICATION_TYPES_KEY] = dict_to_select_options_list(
#             notification_types
#         )
#         values[ID_CONFIG_CONFIGURED_CHANNELS_KEY] = dict_to_select_options_list(
#             channel_options
#         )

#         # Validate and store user input
#         if user_input is not None:
#             try:
#                 # Import at runtime to avoid circular imports
#                 from .config_validator import ConfigValidator

#                 # Validate identity config template using current system limits
#                 schema_validated = (
#                     ConfigValidator.validate_identity_basic_settings_schema(
#                         user_input, self._validation_context
#                     )
#                 )
#                 # Merge validated defaults into the flow state
#                 self._identity_defaults.update(schema_validated)
#                 # Continue to the next step in initial setup
#                 return await self._execute_next_step(step_id)

#             except vol.Invalid as e:
#                 _LOGGER.debug(str(e))
#                 errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
#             except Exception:
#                 _LOGGER.exception("Default identity config validation failed")
#                 errors["base"] = CONFIG_FLOW_ERROR_INVALID_IDENTITY_SETTINGS_KEY

#             finally:
#                 defaults = user_input  # Keep user inputs

#         return self.async_show_form(
#             step_id=step_id,
#             data_schema=get_identity_basic_settings_schema(
#                 defaults, self._validation_context, values
#             ),
#             errors=errors,
#             description_placeholders=description_placeholders,
#             last_step=last_step,
#         )

#     async def async_step_default_identity_channel_mapping(
#         self, user_input: dict[str, Any] | None = None
#     ) -> ConfigFlowResult:
#         """Collect default channel mapping per criticality."""
#         step_id = CONFIG_FLOW_STEP_ID_DEFAULT_CHANNEL_MAPPING_KEY
#         errors: dict[str, str] = {}
#         description_placeholders: dict[str, Any] = {
#             "step": f"{list(self.flow_settings.flow_steps.keys()).index(step_id) + 1}/{len(self.flow_settings.flow_steps)}"
#         }
#         last_step = self._is_last_step(step_id)

#         # Build helper values for the form
#         criticality_levels = {c.name: c.value for c in NotificationCriticality}
#         channel_options = {
#             ch: ch.split(".", 1)[1].replace("_", " ").title()
#             for ch in self._system_settings.get(SYS_CONFIG_ENABLED_CHANNELS_KEY, [])
#         }

#         # Prepare defaults: if reconfigure use existing entry's options; otherwise use in-flow defaults
#         if self._is_reconfigure() and self._reconfigure_entry:
#             defaults = dict(self._reconfigure_entry.options or {})
#         else:
#             defaults = dict(self._identity_defaults or {})

#         values = {}
#         values[ID_CONFIG_CRITICALITY_LEVELS_KEY] = dict_to_select_options_list(
#             criticality_levels
#         )
#         values[ID_CONFIG_CONFIGURED_CHANNELS_KEY] = dict_to_select_options_list(
#             channel_options
#         )

#         # Validate and store user input
#         if user_input is not None:
#             try:
#                 # Import at runtime to avoid circular imports
#                 from .config_validator import ConfigValidator

#                 # Validate identity config template
#                 schema_validated = (
#                     ConfigValidator.validate_identity_channel_mapping_schema(
#                         user_input, self._validation_context
#                     )
#                 )
#                 # Merge validated default channels into the flow state
#                 self._identity_defaults.update(schema_validated)
#                 # Continue to the next step in initial setup
#                 return await self._execute_next_step(step_id)

#             except vol.Invalid as e:
#                 _LOGGER.debug(str(e))
#                 errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
#             except Exception:
#                 _LOGGER.exception("Default identity channel mapping validation failed")
#                 errors["base"] = CONFIG_FLOW_ERROR_INVALID_IDENTITY_SETTINGS_KEY

#             finally:
#                 defaults = user_input  # Keep user inputs

#         return self.async_show_form(
#             step_id=step_id,
#             data_schema=get_identity_criticality_channel_mapping_schema(
#                 defaults, values
#             ),
#             errors=errors,
#             description_placeholders=description_placeholders,
#             last_step=last_step,
#         )

#     async def async_step_default_identity_dnd_settings(
#         self, user_input: dict[str, Any] | None = None
#     ) -> ConfigFlowResult:
#         """Collect default Do-Not-Disturb settings for identity template."""
#         step_id = CONFIG_FLOW_STEP_ID_DEFAULT_DND_SETTINGS_KEY
#         errors: dict[str, str] = {}
#         description_placeholders: dict[str, Any] = {
#             "step": f"{list(self.flow_settings.flow_steps.keys()).index(step_id) + 1}/{len(self.flow_settings.flow_steps)}"
#         }
#         last_step = self._is_last_step(step_id)

#         # Prepare defaults: if reconfigure use existing entry's options; otherwise use in-flow defaults
#         if self._is_reconfigure() and self._reconfigure_entry:
#             defaults = dict(self._reconfigure_entry.options or {})
#         else:
#             defaults = dict(self._identity_defaults or {})

#         # Validate and store user input
#         if user_input is not None:
#             try:
#                 # Import at runtime to avoid circular imports
#                 from .config_validator import ConfigValidator

#                 # Validate identity config template
#                 schema_validated = (
#                     ConfigValidator.validate_identity_dnd_settings_schema(user_input)
#                 )
#                 # Merge validated default DND settings into the flow state
#                 self._identity_defaults.update(schema_validated)
#                 # Continue to the next step in initial setup
#                 return await self._execute_next_step(step_id)

#             except vol.Invalid as e:
#                 _LOGGER.debug(str(e))
#                 errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
#             except Exception:
#                 _LOGGER.exception("Default identity DND validation failed")
#                 errors["base"] = CONFIG_FLOW_ERROR_INVALID_IDENTITY_SETTINGS_KEY

#             finally:
#                 defaults = user_input  # Keep user inputs

#         return self.async_show_form(
#             step_id=step_id,
#             data_schema=get_identity_dnd_settings_schema(defaults, {}),
#             errors=errors,
#             description_placeholders=description_placeholders,
#             last_step=last_step,
#         )
