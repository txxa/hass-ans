"""Recipient config flow for ANS integration - Sub-entry management.

This module handles recipient (sub-entry) configuration for all recipient types:
- System recipient: For persistent notifications (singleton)
- HA User recipient: Linked to Home Assistant user accounts
- Virtual recipient: Custom recipients with email/phone

All recipients go through the same configuration steps for settings, channel mapping, and DND.
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
from homeassistant.helpers.selector import SelectOptionDict

from ..channels.channel_registry import ChannelRegistry
from ..const import (
    PERSISTENT_NOTIFICATION_CHANNEL,
    RCPT_CONFIG_CONFIGURED_CHANNELS_KEY,
    RCPT_CONFIG_CRITICALITY_LEVELS_KEY,
    RCPT_CONFIG_EMAIL_KEY,
    RCPT_CONFIG_ID_KEY,
    RCPT_CONFIG_NAME_KEY,
    RCPT_CONFIG_NOTIFICATION_TYPES_KEY,
    RCPT_CONFIG_PARENT_ENTRY_ID_KEY,
    RCPT_CONFIG_PHONE_KEY,
    RCPT_CONFIG_RECIPIENT_CHOICE_KEY,
    RCPT_CONFIG_RECIPIENT_ID_KEY,
    RCPT_CONFIG_TTS_SETTINGS_KEY,
    RCPT_CONFIG_TYPE_KEY,
    RECIPIENT_CHOICE_HA_USER_PREFIX,
    RECIPIENT_CHOICE_SYSTEM_HA,
    RECIPIENT_CHOICE_TTS,
    RECIPIENT_CHOICE_VIRTUAL,
    SUBENTRY_FLOW_ERROR_INVALID_CHANNEL_MAPPING_KEY,
    SUBENTRY_FLOW_ERROR_INVALID_RECIPIENT_DEFINITION_KEY,
    SUBENTRY_FLOW_ERROR_INVALID_RECIPIENT_SELECTION_KEY,
    SUBENTRY_FLOW_SELECTED_HA_USER_KEY,
    SUBENTRY_FLOW_STEP_RECIPIENT_BASIC_SETTINGS_KEY,
    SUBENTRY_FLOW_STEP_RECIPIENT_CHANNEL_MAPPING_KEY,
    SUBENTRY_FLOW_STEP_RECIPIENT_DEFINITION_KEY,
    SUBENTRY_FLOW_STEP_RECIPIENT_DND_SETTINGS_KEY,
    SUBENTRY_FLOW_STEP_RECIPIENT_SELECTION_KEY,
    SYS_DEFAULT_SYSTEM_RECIPIENT_NAME,
)
from ..helper import (
    async_check_recipient_name_availability,
    channel_info_to_select_options,
    dict_to_select_options_list,
    get_main_entry,
)
from ..models import (
    ChannelInfo,
    NotificationCriticality,
    NotificationType,
    RecipientConfig,
    RecipientData,
    RecipientType,
    SystemConfig,
)
from .forms import (
    get_recipient_basic_settings_schema,
    get_recipient_criticality_channel_mapping_schema,
    get_recipient_definition_schema,
    get_recipient_dnd_settings_schema,
    get_recipient_selection_schema,
    get_recipient_tts_settings_schema,
)
from .validator import ConfigValidator, FieldValidationError, ValidationContext

_LOGGER = logging.getLogger(__name__)


class RecipientConfigFlow(ConfigSubentryFlow):
    """Handle config flow to create a recipient (sub-entry).

    Flow sequence depends on recipient type:
    - System recipient: Skip selection/definition (predefined)
    - Regular recipients: Full flow with selection and definition
    """

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the recipient flow."""
        self._main_entry: ConfigEntry | None = None
        self.system_config: SystemConfig | None = None
        self._recipient_meta: dict[str, Any] = {}
        self._recipient_settings: dict[str, Any] = {}
        self._reconfigure_entry: ConfigSubentry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Entry point for recipient creation - redirect to selection."""
        # Get main entry and system config
        self._main_entry = get_main_entry(self.hass)
        if not self._main_entry:
            return self.async_abort(reason="no_main_entry")

        self.system_config = SystemConfig.from_dict(dict(self._main_entry.data))

        # Start with recipient type selection
        return await self.async_step_recipient_selection(user_input)

    async def async_step_recipient_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Select which recipient to create from unified list."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                # Parse the user's choice
                choice = user_input.get(RCPT_CONFIG_RECIPIENT_CHOICE_KEY, "")

                if choice == RECIPIENT_CHOICE_SYSTEM_HA:
                    # System recipient selected
                    if await self._system_recipient_exists():
                        errors[RCPT_CONFIG_RECIPIENT_CHOICE_KEY] = (
                            "system_recipient_already_exists"
                        )
                    else:
                        # Set up the Home Assistant system recipient
                        return await self._setup_system_recipient()

                elif choice.startswith(RECIPIENT_CHOICE_HA_USER_PREFIX):
                    # HA user selected
                    user_id = choice[len(RECIPIENT_CHOICE_HA_USER_PREFIX) :]

                    # Pre-fill HA user data
                    user_data = await self._get_ha_user_data(user_id)
                    self._recipient_meta.update(
                        {
                            RCPT_CONFIG_TYPE_KEY: RecipientType.HA_USER.value,
                            RCPT_CONFIG_ID_KEY: user_id,
                            RCPT_CONFIG_NAME_KEY: user_data.get("name", user_id),
                            RCPT_CONFIG_EMAIL_KEY: user_data.get("email"),
                        }
                    )

                    # Skip to definition step (to allow editing/adding phone)
                    return await self.async_step_recipient_definition(None)

                elif choice == RECIPIENT_CHOICE_VIRTUAL:
                    # Virtual recipient - user will enter all details
                    self._recipient_meta.update(
                        {
                            RCPT_CONFIG_TYPE_KEY: RecipientType.VIRTUAL.value,
                        }
                    )
                    return await self.async_step_recipient_definition(None)

                elif choice == RECIPIENT_CHOICE_TTS:
                    # TTS recipient - user will enter name only (no contact info needed)
                    self._recipient_meta.update(
                        {
                            RCPT_CONFIG_TYPE_KEY: RecipientType.TTS.value,
                        }
                    )
                    return await self.async_step_recipient_definition(None)

                else:
                    errors[RCPT_CONFIG_RECIPIENT_CHOICE_KEY] = "invalid_selection"

            except vol.Invalid as e:
                _LOGGER.debug("Recipient selection validation failed: %s", e)
                errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
            except Exception:
                _LOGGER.exception("Unexpected error during recipient selection")
                errors["base"] = SUBENTRY_FLOW_ERROR_INVALID_RECIPIENT_SELECTION_KEY

        # Refresh main_entry to get latest subentries before checking for duplicates
        self._main_entry = get_main_entry(self.hass)

        # Always rebuild available options for the form (even on errors)
        # to ensure the list is up-to-date
        system_recipient_available = not await self._system_recipient_exists()
        not_configured_ha_users = await self._get_not_configured_ha_users()

        _LOGGER.debug(
            "System recipient available: %s, Available HA users: %s",
            system_recipient_available,
            [u["value"] for u in not_configured_ha_users],
        )

        # Build the options list here so the schema function stays pure
        options: list[SelectOptionDict] = []
        if system_recipient_available:
            options.append(
                SelectOptionDict(
                    value=RECIPIENT_CHOICE_SYSTEM_HA,
                    label=RECIPIENT_CHOICE_SYSTEM_HA,
                )
            )
        # HA user labels are runtime names — cannot come from translations
        options.extend(
            [
                SelectOptionDict(
                    label=f"HA User: {user['label']}",
                    value=f"{RECIPIENT_CHOICE_HA_USER_PREFIX}{user['value']}",
                )
                for user in not_configured_ha_users
            ]
        )
        options.append(
            SelectOptionDict(value=RECIPIENT_CHOICE_TTS, label=RECIPIENT_CHOICE_TTS)
        )
        options.append(
            SelectOptionDict(
                value=RECIPIENT_CHOICE_VIRTUAL, label=RECIPIENT_CHOICE_VIRTUAL
            )
        )

        return self.async_show_form(
            step_id=SUBENTRY_FLOW_STEP_RECIPIENT_SELECTION_KEY,
            data_schema=get_recipient_selection_schema(
                defaults=user_input or {},
                options=options,
            ),
            errors=errors,
            last_step=False,
            description_placeholders={
                "info": "Select the recipient you want to create"
            },
        )

    async def async_step_recipient_definition(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Define recipient data (name, email, phone, HA user link)."""
        errors: dict[str, str] = {}
        recipient_type = RecipientType(self._recipient_meta[RCPT_CONFIG_TYPE_KEY])

        if user_input is not None:
            try:
                # Validate definition
                validated = ConfigValidator.validate_recipient_definition_schema(
                    user_input
                )

                # Check name availability
                name = validated[RCPT_CONFIG_NAME_KEY]

                # During reconfiguration, allow keeping the same name
                current_name = (
                    self._recipient_meta.get(RCPT_CONFIG_NAME_KEY)
                    if self._reconfigure_entry
                    else None
                )

                # Only check availability if the name has changed (or for new recipients)
                if (
                    name != current_name
                    and not await async_check_recipient_name_availability(
                        self.hass, name
                    )
                ):
                    # errors[ID_CONFIG_NAME_KEY] = "name_already_exists"
                    raise vol.Invalid(
                        message=f"Name '{name}' is already used.",
                        path=[RCPT_CONFIG_NAME_KEY],
                    )

                # Store recipient definition
                self._recipient_meta.update(validated)

                # Proceed to settings
                return await self.async_step_recipient_basic_settings(None)

            except vol.Invalid as e:
                _LOGGER.debug("Recipient definition validation failed: %s", e)
                errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
                # if e.path and len(e.path) > 0:
                #     if e.path[0] == "email_or_phone_required":
                #         errors["base"] = "email_or_phone_required"
                #     else:
                # else:
                #     errors["base"] = (
                #         SUBENTRY_FLOW_ERROR_INVALID_RECIPIENT_DEFINITION_KEY
                #     )
            except Exception:
                _LOGGER.exception("Unexpected error during recipient definition")
                errors["base"] = SUBENTRY_FLOW_ERROR_INVALID_RECIPIENT_DEFINITION_KEY

        # Get available HA users for selection (empty dict if not HA_USER type)
        ha_users = {}
        if recipient_type == RecipientType.HA_USER:
            # HA users need to be fetched from Home Assistant
            # For now, use empty dict - this would need to be implemented
            ha_users = {}

        # Prepare defaults from pre-filled data or user input
        defaults = user_input or self._recipient_meta.copy()

        # Show definition form
        return self.async_show_form(
            step_id=SUBENTRY_FLOW_STEP_RECIPIENT_DEFINITION_KEY,
            data_schema=get_recipient_definition_schema(
                defaults=defaults,
                values={SUBENTRY_FLOW_SELECTED_HA_USER_KEY: ha_users},
                recipient_type=recipient_type,
            ),
            errors=errors,
            last_step=False,
            description_placeholders={
                "info": f"Define the {recipient_type.value} recipient data"
            },
        )

    async def async_step_recipient_basic_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Configure basic notification settings (rate limit, retry, notification types)."""
        errors: dict[str, str] = {}

        # Get defaults for the form
        defaults = dict(self._recipient_settings or {})

        # Build validation context with system limits
        if self.system_config:
            validation_context = ValidationContext(
                system_limits={
                    "rate_limit_max": self.system_config.global_rate_limit,
                },
                available_channels=list(self.system_config.enabled_channels),
            )
        else:
            validation_context = ValidationContext()

        if user_input is not None:
            try:
                # Validate settings
                validated = ConfigValidator.validate_recipient_basic_settings_schema(
                    user_input, validation_context
                )

                # Store settings
                self._recipient_settings.update(validated)

                # Conditional branching based on recipient type
                recipient_type = RecipientType(
                    self._recipient_meta[RCPT_CONFIG_TYPE_KEY]
                )
                if recipient_type == RecipientType.TTS:
                    # TTS recipients go to TTS settings step
                    return await self.async_step_recipient_tts_settings(None)
                # Other recipients go directly to channel mapping
                return await self.async_step_recipient_channel_mapping(None)

            except vol.Invalid as e:
                _LOGGER.debug("Recipient basic settings validation failed: %s", e)
                errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
            except Exception:
                _LOGGER.exception(
                    "Unexpected error during recipient basic settings definition"
                )
                errors["base"] = SUBENTRY_FLOW_STEP_RECIPIENT_BASIC_SETTINGS_KEY

        # Build notification types options
        notification_types = [
            {"label": t.value.title(), "value": t.value} for t in NotificationType
        ]

        # Show settings form
        return self.async_show_form(
            step_id=SUBENTRY_FLOW_STEP_RECIPIENT_BASIC_SETTINGS_KEY,
            data_schema=get_recipient_basic_settings_schema(
                defaults=user_input or defaults,
                validation_context=validation_context,
                values={RCPT_CONFIG_NOTIFICATION_TYPES_KEY: notification_types},
            ),
            errors=errors,
            last_step=False,
            description_placeholders={
                "info": "Configure notification behavior for this recipient"
            },
        )

    async def async_step_recipient_tts_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Configure TTS-specific settings (volume levels, message format)."""
        errors: dict[str, str] = {}

        # Get defaults for the form
        defaults = dict(self._recipient_settings or {})

        if user_input is not None:
            try:
                # Validate TTS settings
                validated = ConfigValidator.validate_recipient_tts_settings_schema(
                    user_input
                )

                # Store TTS settings nested under tts_settings key
                # This matches the expected structure in RecipientConfig.from_dict()
                self._recipient_settings[RCPT_CONFIG_TTS_SETTINGS_KEY] = validated

                # Proceed to channel mapping
                return await self.async_step_recipient_channel_mapping(None)

            except vol.Invalid as e:
                _LOGGER.debug("Recipient TTS settings validation failed: %s", e)
                errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
            except Exception:
                _LOGGER.exception(
                    "Unexpected error during recipient TTS settings configuration"
                )
                errors["base"] = "tts_settings_error"

        # Show TTS settings form
        return self.async_show_form(
            step_id="recipient_tts_settings",
            data_schema=get_recipient_tts_settings_schema(
                defaults=user_input or defaults,
                values={},
            ),
            errors=errors,
            last_step=False,
            description_placeholders={
                "info": "Configure TTS volume levels and message format"
            },
        )

    async def async_step_recipient_channel_mapping(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Map notification criticality levels to delivery channels."""
        errors: dict[str, str] = {}
        recipient_type = RecipientType(self._recipient_meta[RCPT_CONFIG_TYPE_KEY])

        # Get defaults
        defaults = dict(self._recipient_settings or {})

        # Build validation context with system limits
        if self.system_config:
            validation_context = ValidationContext(
                system_limits={
                    "rate_limit_max": self.system_config.global_rate_limit,
                },
                available_channels=list(self.system_config.enabled_channels),
            )
        else:
            validation_context = ValidationContext()

        if user_input is not None:
            try:
                # Validate channel mapping
                validated = ConfigValidator.validate_recipient_channel_mapping_schema(
                    user_input, validation_context
                )

                # Additional validation: At least one channel must be mapped
                all_channels = []
                for key in validated:
                    if key.startswith("channels_") and isinstance(validated[key], list):
                        all_channels.extend(validated[key])

                if not all_channels:
                    # errors["base"] = "at_least_one_channel_required"
                    # raise vol.Invalid(
                    #     "At least one channel must be mapped across all criticality levels"
                    # )
                    raise ValueError("At least one channel needs to be configured")

                # Store mapping
                self._recipient_settings.update(validated)

                # Proceed to DND settings
                return await self.async_step_recipient_dnd_settings(None)

            except vol.Invalid as e:
                _LOGGER.debug("Recipient channel mapping validation failed: %s", e)
                errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
            except ValueError as e:
                _LOGGER.debug("Recipient channel mapping validation failed: %s", e)
                errors["base"] = SUBENTRY_FLOW_ERROR_INVALID_CHANNEL_MAPPING_KEY
            except Exception:
                _LOGGER.exception("Unexpected error during recipient channel mapping")
                errors["base"] = SUBENTRY_FLOW_STEP_RECIPIENT_CHANNEL_MAPPING_KEY

        # Get available channels filtered by recipient type
        available_channels = await self._get_available_channels()
        filtered_channels = ChannelRegistry.filter_channels_by_recipient_type(
            available_channels, recipient_type
        )

        # Further filter channels based on available contact information
        # Extract channel IDs for filtering
        all_channel_ids = [ch.id for ch in filtered_channels]

        # Determine what contact info is available
        has_email = bool(self._recipient_meta.get(RCPT_CONFIG_EMAIL_KEY))
        has_phone = bool(self._recipient_meta.get(RCPT_CONFIG_PHONE_KEY))
        has_ha_user = recipient_type == RecipientType.HA_USER

        # Filter channels by contact info requirements using ChannelRegistry
        available_channel_ids, unavailable_channels = (
            ChannelRegistry.filter_channels_by_contact_info(
                all_channel_ids,
                has_email=has_email,
                has_phone=has_phone,
                has_ha_user=has_ha_user,
            )
        )

        # Build list of available channel info objects
        available_channel_info = [
            ch for ch in filtered_channels if ch.id in available_channel_ids
        ]

        # Build form values
        criticality_levels = {c.name: c.value for c in NotificationCriticality}
        channel_options = channel_info_to_select_options(available_channel_info)

        values = {
            RCPT_CONFIG_CRITICALITY_LEVELS_KEY: dict_to_select_options_list(
                criticality_levels
            ),
            RCPT_CONFIG_CONFIGURED_CHANNELS_KEY: channel_options,
            "unavailable_channels": unavailable_channels,  # Pass unavailable channels with reasons
        }

        # Show channel mapping form
        return self.async_show_form(
            step_id=SUBENTRY_FLOW_STEP_RECIPIENT_CHANNEL_MAPPING_KEY,
            data_schema=get_recipient_criticality_channel_mapping_schema(
                defaults=user_input or defaults,
                values=values,
            ),
            errors=errors,
            last_step=False,
            description_placeholders={
                "info": "Assign channels for each notification priority level"
            },
        )

    async def async_step_recipient_dnd_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Configure Do Not Disturb settings."""
        errors: dict[str, str] = {}

        # Get defaults
        defaults = dict(self._recipient_settings or {})

        if user_input is not None:
            try:
                # Validate DND settings
                validated = ConfigValidator.validate_recipient_dnd_settings_schema(
                    user_input
                )

                # Store settings
                self._recipient_settings.update(validated)

                # Create the recipient sub-entry
                return await self._create_recipient_entry()

            except vol.Invalid as e:
                _LOGGER.debug("Recipient DND settings validation failed: %s", e)
                errors[str(e.path[0])] = str(e.path[len(e.path) - 1])
            except Exception:
                _LOGGER.exception(
                    "Unexpected error during recipient DND settings definition"
                )
                errors["base"] = SUBENTRY_FLOW_STEP_RECIPIENT_DND_SETTINGS_KEY

        # Show DND settings form
        return self.async_show_form(
            step_id=SUBENTRY_FLOW_STEP_RECIPIENT_DND_SETTINGS_KEY,
            data_schema=get_recipient_dnd_settings_schema(
                defaults=user_input or defaults,
                values={},
            ),
            errors=errors,
            last_step=True,
            description_placeholders={
                "info": "Configure quiet hours and allowed notifications during DND"
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfiguration of an existing recipient."""
        # Get the subentry being reconfigured
        self._reconfigure_entry = self._get_reconfigure_subentry()
        if self._reconfigure_entry is None:
            return self.async_abort(reason="no_subentry")

        # Load existing configuration (ConfigSubentry stores all in data)
        entry_data = dict(self._reconfigure_entry.data)

        # Split meta and settings based on RecipientData/RecipientConfig fields
        self._recipient_meta = {
            RCPT_CONFIG_ID_KEY: entry_data.get(RCPT_CONFIG_ID_KEY),
            RCPT_CONFIG_NAME_KEY: entry_data.get(RCPT_CONFIG_NAME_KEY),
            RCPT_CONFIG_TYPE_KEY: entry_data.get(RCPT_CONFIG_TYPE_KEY),
            RCPT_CONFIG_EMAIL_KEY: entry_data.get(RCPT_CONFIG_EMAIL_KEY),
            RCPT_CONFIG_PHONE_KEY: entry_data.get(RCPT_CONFIG_PHONE_KEY),
        }
        self._recipient_settings = entry_data

        # Get main entry and system config
        self._main_entry = get_main_entry(self.hass)
        if not self._main_entry:
            _LOGGER.error("ANS main entry not found when creating recipient")
            return self.async_abort(reason="no_main_entry")

        self.system_config = SystemConfig.from_dict(dict(self._main_entry.data))

        # # Start reconfiguration from basic settings
        # # (skip selection and definition as those are immutable)
        # return await self.async_step_recipient_basic_settings(user_input)

        # Start reconfiguration from definition step to allow changing contact info
        return await self.async_step_recipient_definition(user_input)

    async def _setup_system_recipient(self) -> SubentryFlowResult:
        """Set up the Home Assistant system recipient.

        Creates a single system recipient for Home Assistant instance-level notifications.
        Pre-configures the persistent notification channel for all criticality levels.

        Returns:
            SubentryFlowResult directing to the next step

        """
        # Generate a unique UUID for the system recipient
        recipient_uuid = str(uuid.uuid4())

        # Pre-fill system recipient metadata
        self._recipient_meta.update(
            {
                RCPT_CONFIG_TYPE_KEY: RecipientType.SYSTEM.value,
                RCPT_CONFIG_ID_KEY: recipient_uuid,
                RCPT_CONFIG_NAME_KEY: SYS_DEFAULT_SYSTEM_RECIPIENT_NAME,
            }
        )

        # Use system default config
        system_config = RecipientConfig.system_default()
        self._recipient_settings = system_config.to_dict()

        # Pre-configure persistent notification for all criticality levels
        self._recipient_settings["channels_low"] = [PERSISTENT_NOTIFICATION_CHANNEL]
        self._recipient_settings["channels_medium"] = [PERSISTENT_NOTIFICATION_CHANNEL]
        self._recipient_settings["channels_high"] = [PERSISTENT_NOTIFICATION_CHANNEL]
        self._recipient_settings["channels_critical"] = [
            PERSISTENT_NOTIFICATION_CHANNEL
        ]

        # Proceed to settings configuration
        return await self.async_step_recipient_basic_settings(None)

    # Helper methods

    async def _create_recipient_entry(self) -> SubentryFlowResult:
        """Create the recipient sub-entry with all collected data."""
        try:
            if self._main_entry is None:
                _LOGGER.error("ANS main entry not found when creating recipient")
                return self.async_abort(reason="no_main_entry")

            # Get values
            recipient_id = self._recipient_meta.get(RCPT_CONFIG_ID_KEY)
            recipient_type = self._recipient_meta.get(RCPT_CONFIG_TYPE_KEY)
            name = self._recipient_meta.get(RCPT_CONFIG_NAME_KEY)
            email = self._recipient_meta.get(RCPT_CONFIG_EMAIL_KEY)
            phone = self._recipient_meta.get(RCPT_CONFIG_PHONE_KEY)

            # Validate values
            if not recipient_id:
                raise ValueError("ID is required for creating a recipient")
            if not recipient_type:
                raise ValueError("Type is required for creating a recipient")
            if not name:
                raise ValueError("Name is required for creating a recipient")

            # # Validate identity_id is set
            # if not self._recipient_meta.get(ID_CONFIG_ID_KEY):
            #     return self.async_abort(reason="missing_identity_id")

            # # Set parent entry ID in metadata
            # if self.main_entry:
            #     self._recipient_meta[ID_CONFIG_PARENT_ENTRY_ID_KEY] = (
            #         self.main_entry.entry_id
            #     )

            # # Create RecipientData from metadata
            # recipient_data = RecipientData(
            #     id=self._recipient_meta[ID_CONFIG_ID_KEY],
            #     type=RecipientType(self._recipient_meta[ID_CONFIG_TYPE_KEY]),
            #     name=self._recipient_meta[ID_CONFIG_NAME_KEY],
            #     email=self._recipient_meta.get("email"),
            #     phone=self._recipient_meta.get("phone"),
            # )

            # Final validation via dataclass construction
            recipient_data = RecipientData(
                id=recipient_id,
                type=RecipientType(recipient_type),
                name=name,
                email=email,
                phone=phone,
            )

            # Create RecipientConfig from settings
            # Set recipient_id in settings to link them
            self._recipient_settings[RCPT_CONFIG_RECIPIENT_ID_KEY] = recipient_data.id
            recipient_config = RecipientConfig.from_dict(self._recipient_settings)

            # Combine both into a single data dict for ConfigSubentry
            data = recipient_data.to_dict()
            data[RCPT_CONFIG_PARENT_ENTRY_ID_KEY] = self._main_entry.entry_id
            data.update(recipient_config.to_dict())

            # Title format: "Name (id=ID, type=TYPE)" for debugging and log clarity
            title = f"{recipient_data.name} (id={recipient_data.id}, type={recipient_data.type.value})"

            # Check if we're reconfiguring an existing entry or creating a new one
            if self._reconfigure_entry and self._main_entry:
                # Update existing subentry and reload the integration
                try:
                    self.async_update_and_abort(
                        entry=self._main_entry,
                        subentry=self._reconfigure_entry,
                        data=data,
                    )
                except Exception:
                    _LOGGER.exception("Failed to update recipient entry")
                    return self.async_abort(reason="update_entry_failed")
                return self.async_abort(reason="reconfigure_complete")
                # return self.async_update_reload_and_abort(
                #     entry=self.main_entry,
                #     subentry=self._reconfigure_entry,
                #     title=title,
                #     data=data,
                # )

            # Create new subentry
            return self.async_create_entry(
                title=title,
                data=data,
            )
        except FieldValidationError as fv:
            errors = {fv.field or "base": fv.message}
            _LOGGER.debug("Field validation error on final create: %s", fv)
            return self.async_show_form(
                step_id="confirm", data_schema=vol.Schema({}), errors=errors
            )
        except Exception:
            _LOGGER.exception("Failed to create recipient entry")
            return self.async_abort(reason="create_entry_failed")

    async def _system_recipient_exists(self) -> bool:
        """Check if the Home Assistant system recipient already exists.

        Returns:
            True if a system recipient already exists (regardless of channels)

        """
        if not self._main_entry:
            return False

        # Check all subentries of the main entry
        for subentry in self._main_entry.subentries.values():
            entry_data = subentry.data or {}

            # Check if this is a system recipient
            if entry_data.get(RCPT_CONFIG_TYPE_KEY) == RecipientType.SYSTEM.value:
                return True

        return False

    async def _get_not_configured_ha_users(self) -> list[dict[str, str]]:
        """Get HA users that don't have recipient configs yet.

        Returns:
            List of SelectOptionDict-compatible dicts with label and value

        """
        # Get all HA users from auth system
        users = await self.hass.auth.async_get_users()

        # Get existing recipient IDs for HA users
        existing_recipients = []
        if self._main_entry:
            for subentry in self._main_entry.subentries.values():
                subentry_data = subentry.data or {}
                if (
                    subentry_data.get(RCPT_CONFIG_TYPE_KEY)
                    == RecipientType.HA_USER.value
                ):
                    existing_recipients.append(subentry_data.get(RCPT_CONFIG_ID_KEY))

        # Build list of not-configured users
        return [
            {
                "label": user.name or user.id,
                "value": user.id,
            }
            for user in users
            if user.id not in existing_recipients
        ]

    async def _get_ha_user_data(self, user_id: str) -> dict[str, Any]:
        """Get data for a specific HA user.

        Args:
            user_id: The Home Assistant user ID

        Returns:
            Dict with user data (name, email, etc.)

        """
        # Get user from auth system
        users = await self.hass.auth.async_get_users()
        user = next((u for u in users if u.id == user_id), None)

        if not user:
            return {
                "name": f"User {user_id}",
                "email": None,
            }

        # Extract email from credentials if available
        email = None
        if user.credentials:
            for cred in user.credentials:
                if hasattr(cred, "data") and isinstance(cred.data, dict):
                    username = cred.data.get("username")
                    # Only use as email if it contains @ and is not the same as name
                    if username and "@" in username:
                        email = username
                        break

        return {
            "name": user.name or user.id,
            "email": email,
        }

    def _is_persistent_notification_enabled(self) -> bool:
        """Check if persistent notifications are enabled in system config."""
        if not self.system_config:
            return False
        return PERSISTENT_NOTIFICATION_CHANNEL in self.system_config.enabled_channels

    async def _get_available_channels(self) -> list[ChannelInfo]:
        """Get available notification channels from system config.

        Uses the main entry's config repository as the source of truth.
        Falls back to detection only if repository is unavailable.

        Returns:
            List of ChannelInfo objects for enabled channels only

        """
        if not self.system_config:
            _LOGGER.debug("No system config available, returning empty channel list")
            return []

        all_channels: list[ChannelInfo] = []

        # Try to get from config repository (preferred)
        # Local import to avoid circular dependency
        from .. import get_config_repository  # noqa: PLC0415

        config_repo = get_config_repository(self.hass)
        if config_repo and config_repo.channel_registry.count() > 0:
            # Get channels filtered by recipient type if available
            recipient_type = self._recipient_meta.get(RCPT_CONFIG_TYPE_KEY)
            if recipient_type:
                all_channels = config_repo.get_channels_for_ui(
                    RecipientType(recipient_type)
                )
            else:
                all_channels = config_repo.get_channels_for_ui()

            _LOGGER.debug(
                "Loaded %d channels from config repository",
                len(all_channels),
            )
        else:
            # Fallback: Direct detection (less preferred, logs warning)
            _LOGGER.warning(
                "Config repository unavailable or empty, falling back to direct channel detection"
            )
            all_channels = await ChannelRegistry.detect_notification_channels(self.hass)

            # Apply recipient type filtering manually if needed
            recipient_type = self._recipient_meta.get(RCPT_CONFIG_TYPE_KEY)
            if recipient_type:
                all_channels = ChannelRegistry.filter_channels_by_recipient_type(
                    all_channels, RecipientType(recipient_type)
                )

            _LOGGER.debug(
                "Detected %d channels directly",
                len(all_channels),
            )

        # Filter to only enabled channels from system config
        enabled_channels = [
            ch for ch in all_channels if ch.id in self.system_config.enabled_channels
        ]

        _LOGGER.debug(
            "Returning %d enabled channels (out of %d total): %s",
            len(enabled_channels),
            len(all_channels),
            [ch.id for ch in enabled_channels],
        )

        return enabled_channels
