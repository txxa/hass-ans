"""Voluptuous schema definitions for ANS config flows."""

import voluptuous as vol
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    selector,
)

from .config_validator import ValidationContext
from .const import (
    CONFIG_FLOW_SELECTED_HA_USERS_KEY,
    DEFAULT_BLOCKED_SOURCES_PATTERN,
    DEFAULT_CONFIGURED_CHANNELS,
    DEFAULT_CRITICALITY_LEVELS,
    DEFAULT_DND_ALLOWED_CRITICALITIES,
    DEFAULT_DND_ALLOWED_SOURCES_PATTERN,
    DEFAULT_DND_ALLOWED_TYPES,
    DEFAULT_DND_ENABLED,
    DEFAULT_DND_END,
    DEFAULT_DND_START,
    DEFAULT_ENABLED_CHANNELS,
    DEFAULT_GLOBAL_RATE_LIMIT_VALUE,
    DEFAULT_NOTIFICATION_TYPES,
    DEFAULT_RATE_LIMIT_VALUE,
    # Integration metadata
    DEFAULT_RETRY_ATTEMPTS,
    # DEFAULT_TTS_INTEGRATION,
    ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY,
    ID_CONFIG_CHANNELS_KEY,
    ID_CONFIG_CONFIGURED_CHANNELS_KEY,
    ID_CONFIG_CRITICALITY_LEVELS_KEY,
    ID_CONFIG_DND_ALLOWED_CRITICALITIES_KEY,
    ID_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY,
    ID_CONFIG_DND_ALLOWED_TYPES_KEY,
    ID_CONFIG_DND_ENABLED_KEY,
    ID_CONFIG_DND_END_KEY,
    ID_CONFIG_DND_START_KEY,
    ID_CONFIG_EMAIL_KEY,
    ID_CONFIG_NAME_KEY,
    ID_CONFIG_NOTIFICATION_TYPES_KEY,
    ID_CONFIG_PHONE_KEY,
    ID_CONFIG_RATE_LIMIT_KEY,
    ID_CONFIG_RETRY_ATTEMPTS_KEY,
    ID_CONFIG_TYPE_KEY,
    SUBENTRY_FLOW_SELECTED_HA_USER_KEY,
    SYS_CONFIG_ENABLED_CHANNELS_KEY,
    SYS_CONFIG_RATE_LIMIT_MAX_KEY,
    # SYS_CONFIG_TTS_INTEGRATION_KEY,
    SYSTEM_WIDE_CHANNELS,
)
from .models import (
    ChannelInfo,
    ChannelScope,
    NotificationCriticality,
    NotificationType,
    RecipientType,
)


def dict_to_select_options_list(dict: dict[str, str]) -> list[SelectOptionDict]:
    """Return a dictionary of available backup names."""
    return [
        SelectOptionDict(
            label=value,
            value=key,
        )
        for key, value in dict.items()
    ]


def channel_info_to_select_options(
    channels: list[ChannelInfo],
) -> list[SelectOptionDict]:
    """Convert ChannelInfo objects to select options for forms.

    Args:
        channels: List of ChannelInfo objects.

    Returns:
        List of SelectOptionDict for use in form selectors.

    """
    return [
        SelectOptionDict(
            label=ch.label,
            value=ch.id,
        )
        for ch in channels
    ]


def filter_channels_by_recipient_type(
    channels: list[ChannelInfo], recipient_type: RecipientType
) -> list[ChannelInfo]:
    """Filter channels based on recipient type using channel metadata.

    Args:
        channels: List of ChannelInfo objects.
        recipient_type: Type of recipient (SYSTEM, HA_USER, VIRTUAL).

    Returns:
        Filtered list of ChannelInfo appropriate for the recipient type.

    """
    if recipient_type == RecipientType.SYSTEM:
        # System recipient: only system-wide channels
        return [ch for ch in channels if ch.scope == ChannelScope.SYSTEM_WIDE]

    # Regular recipients: exclude system-wide channels
    return [ch for ch in channels if ch.scope == ChannelScope.RECIPIENT_SPECIFIC]


# ---------------------------
# System Config Schema
# ---------------------------


def get_system_config_schema(defaults: dict | None, values: dict | None) -> vol.Schema:
    """Return schema for system-wide configuration (main config flow & reconfig)."""
    defaults = defaults or {}
    values = values or {}

    schema_dict = {
        # Global rate limit (notifications per time window)
        vol.Required(
            SYS_CONFIG_RATE_LIMIT_MAX_KEY,
            # default=defaults.get(SYS_CONFIG_RATE_LIMIT_MAX_KEY, DEFAULT_GLOBAL_RATE_LIMIT_VALUE),
            description={
                "suggested_value": defaults.get(
                    SYS_CONFIG_RATE_LIMIT_MAX_KEY, DEFAULT_GLOBAL_RATE_LIMIT_VALUE
                ),
            },
        ): int,  # vol.All(int, vol.Range(min=0)),
        # Which notification channels are enabled system-wide
        vol.Required(
            SYS_CONFIG_ENABLED_CHANNELS_KEY,
            # default=defaults.get(
            #     SYS_CONFIG_ENABLED_CHANNELS_KEY, DEFAULT_ENABLED_CHANNELS
            # ),
            description={
                "suggested_value": defaults.get(
                    SYS_CONFIG_ENABLED_CHANNELS_KEY, DEFAULT_ENABLED_CHANNELS
                ),
            },
        ): SelectSelector(
            SelectSelectorConfig(
                options=values.get(
                    SYS_CONFIG_ENABLED_CHANNELS_KEY, DEFAULT_ENABLED_CHANNELS
                ),
                translation_key=SYS_CONFIG_ENABLED_CHANNELS_KEY,
                multiple=True,
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        # # Optional TTS integration (string id, None = no TTS)
        # vol.Optional(
        #     SYS_CONFIG_TTS_INTEGRATION_KEY,
        #     description={
        #         "suggested_value": defaults.get(
        #             SYS_CONFIG_TTS_INTEGRATION_KEY, DEFAULT_TTS_INTEGRATION
        #         )
        #     },
        # ): SelectSelector(
        #     SelectSelectorConfig(
        #         options=values.get(
        #             SYS_CONFIG_TTS_INTEGRATION_KEY, [DEFAULT_TTS_INTEGRATION]
        #         ),
        #         translation_key=SYS_CONFIG_TTS_INTEGRATION_KEY,
        #         multiple=False,
        #         mode=SelectSelectorMode.DROPDOWN,
        #     )
        # ),
    }

    # if defaults.get(CONFIG_FLOW_DEFINE_DEFAULT_IDENTITY_SETTINGS_KEY, True):
    #     schema_dict[
    #         vol.Required(
    #             CONFIG_FLOW_DEFINE_DEFAULT_IDENTITY_SETTINGS_KEY,
    #             default=False,
    #         )
    #     ] = bool

    return vol.Schema(schema_dict)


def get_identity_basic_settings_schema(
    defaults: dict | None, validation_context: ValidationContext, values: dict | None
) -> vol.Schema:
    """Return schema for base identity-level configuration."""
    defaults = defaults or {}
    values = values or {}

    # Get allowed notification types
    available_types = values.get(
        ID_CONFIG_NOTIFICATION_TYPES_KEY, DEFAULT_NOTIFICATION_TYPES
    )
    available_types_list = [t["value"] for t in available_types]
    # # Get allowed notification channels
    # allowed_channels = values.get(
    #     ID_CONFIG_CONFIGURED_CHANNELS_KEY, DEFAULT_CONFIGURED_CHANNELS
    # )
    # allowed_channels_list = [c["value"] for c in allowed_channels]

    # Return the schema
    return vol.Schema(
        {
            vol.Required(
                ID_CONFIG_RATE_LIMIT_KEY,
                # default=defaults.get(ID_CONFIG_RATE_LIMIT_KEY, DEFAULT_RATE_LIMIT_VALUE),
                description={
                    "suggested_value": defaults.get(
                        ID_CONFIG_RATE_LIMIT_KEY, DEFAULT_RATE_LIMIT_VALUE
                    ),
                },
            ): vol.All(
                int,
                vol.Range(min=0, max=validation_context.get_max_recipient_rate_limit()),
            ),
            vol.Required(
                ID_CONFIG_RETRY_ATTEMPTS_KEY,
                # default=defaults.get(
                #     ID_CONFIG_RETRY_ATTEMPTS_KEY, DEFAULT_RETRY_ATTEMPTS
                # ),
                description={
                    "suggested_value": defaults.get(
                        ID_CONFIG_RETRY_ATTEMPTS_KEY, DEFAULT_RETRY_ATTEMPTS
                    ),
                },
            ): vol.All(int, vol.Range(min=0, max=DEFAULT_RETRY_ATTEMPTS)),
            # Types of notifications that this identity should receive
            vol.Required(
                ID_CONFIG_NOTIFICATION_TYPES_KEY,
                # default=defaults.get(
                #     ID_CONFIG_NOTIFICATION_TYPES_KEY, available_types_list
                # ),
                description={
                    "suggested_value": defaults.get(
                        ID_CONFIG_NOTIFICATION_TYPES_KEY, available_types_list
                    ),
                },
            ): SelectSelector(
                SelectSelectorConfig(
                    options=available_types,
                    translation_key=ID_CONFIG_NOTIFICATION_TYPES_KEY,
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY,
                # default=defaults.get(
                #     ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY,
                #     DEFAULT_BLOCKED_SOURCES_PATTERN,
                # ),
                description={
                    "suggested_value": defaults.get(
                        ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY,
                        DEFAULT_BLOCKED_SOURCES_PATTERN,
                    ),
                },
            ): vol.Any(None, str),
        },
        extra=vol.PREVENT_EXTRA,
    )


def get_identity_criticality_channel_mapping_schema(
    defaults: dict | None,
    values: dict | None,
    recipient_type: RecipientType | None = None,
) -> vol.Schema:
    """Return schema for criticality → channel mapping.

    Args:
        defaults: Default values for form fields.
        values: Available options for select fields.
        recipient_type: Type of recipient to filter channels (None = no filtering).

    Returns:
        Voluptuous schema for channel mapping form.

    """
    defaults = defaults or {}
    values = values or {}

    # Get channels - could be list[ChannelInfo] or list[SelectOptionDict]
    available_channels = values.get(
        ID_CONFIG_CONFIGURED_CHANNELS_KEY, DEFAULT_CONFIGURED_CHANNELS
    )

    # If we have ChannelInfo objects, filter and convert them
    if available_channels and isinstance(available_channels[0], ChannelInfo):
        if recipient_type:
            available_channels = filter_channels_by_recipient_type(
                available_channels, recipient_type
            )
        # Convert to SelectOptionDict
        available_channels_options = channel_info_to_select_options(available_channels)
        available_channels_list = [ch.id for ch in available_channels]
    else:
        # Fallback: already SelectOptionDict format (backward compatibility)
        available_channels_options = available_channels
        available_channels_list = [c["value"] for c in available_channels]

        # Filter by recipient type if needed (old string-based approach)
        if recipient_type:
            available_channels_list = filter_channels_by_recipient_type_legacy(
                available_channels_list, recipient_type
            )
            available_channels_options = [
                c for c in available_channels if c["value"] in available_channels_list
            ]

    criticality_levels = values.get(
        ID_CONFIG_CRITICALITY_LEVELS_KEY, DEFAULT_CRITICALITY_LEVELS
    )
    schema_dict = {}

    for crit in criticality_levels:
        key = f"{ID_CONFIG_CHANNELS_KEY}_{crit['value'].lower()}"
        schema_dict[
            vol.Optional(
                key,
                description={
                    "suggested_value": defaults.get(
                        key, available_channels_list
                    ),  # default: all allowed
                },
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=available_channels_options,
                translation_key=key,
                multiple=True,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )

    return vol.Schema(
        schema_dict,
        extra=vol.PREVENT_EXTRA,
    )


def filter_channels_by_recipient_type_legacy(
    channels: list[str], recipient_type: RecipientType
) -> list[str]:
    """Legacy string-based channel filtering (backward compatibility).

    Args:
        channels: List of channel ID strings.
        recipient_type: Type of recipient.

    Returns:
        Filtered list of channel IDs.

    """
    if recipient_type == RecipientType.SYSTEM:
        return [ch for ch in channels if ch in SYSTEM_WIDE_CHANNELS]
    return [ch for ch in channels if ch not in SYSTEM_WIDE_CHANNELS]


def get_identity_dnd_settings_schema(
    defaults: dict | None, values: dict | None
) -> vol.Schema:
    """Return schema for identity Do-Not-Disturb configuration."""
    defaults = defaults or {}
    values = values or {}

    # Build criticality and type options
    criticality_options: list[SelectOptionDict] = [
        SelectOptionDict(label=c.value.title(), value=c.value)
        for c in NotificationCriticality
    ]
    type_options: list[SelectOptionDict] = [
        SelectOptionDict(label=t.value.title(), value=t.value) for t in NotificationType
    ]

    # Return the schema
    return vol.Schema(
        {
            vol.Required(
                ID_CONFIG_DND_ENABLED_KEY,
                description={
                    "suggested_value": defaults.get(
                        ID_CONFIG_DND_ENABLED_KEY, DEFAULT_DND_ENABLED
                    ),
                },
            ): bool,
            vol.Optional(
                ID_CONFIG_DND_START_KEY,
                description={
                    "suggested_value": defaults.get(
                        ID_CONFIG_DND_START_KEY, DEFAULT_DND_START
                    ),
                    "description": "Start time of Do Not Disturb (in 24-hour format).",
                },
            ): selector({"time": {}}),
            vol.Optional(
                ID_CONFIG_DND_END_KEY,
                description={
                    "suggested_value": defaults.get(
                        ID_CONFIG_DND_END_KEY, DEFAULT_DND_END
                    ),
                    "description": "End time of Do Not Disturb (in 24-hour format).",
                },
            ): selector({"time": {}}),
            vol.Optional(
                ID_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY,
                description={
                    "suggested_value": defaults.get(
                        ID_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY,
                        DEFAULT_DND_ALLOWED_SOURCES_PATTERN,
                    ),
                    "description": "Regular expression pattern for explicitly allowed sources.",
                },
            ): str,
            vol.Optional(
                ID_CONFIG_DND_ALLOWED_CRITICALITIES_KEY,
                description={
                    "suggested_value": defaults.get(
                        ID_CONFIG_DND_ALLOWED_CRITICALITIES_KEY,
                        DEFAULT_DND_ALLOWED_CRITICALITIES,
                    ),
                    "description": "Notification criticality levels that bypass DND.",
                },
            ): SelectSelector(
                SelectSelectorConfig(
                    options=criticality_options,
                    translation_key=ID_CONFIG_DND_ALLOWED_CRITICALITIES_KEY,
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                ),
            ),
            vol.Optional(
                ID_CONFIG_DND_ALLOWED_TYPES_KEY,
                description={
                    "suggested_value": defaults.get(
                        ID_CONFIG_DND_ALLOWED_TYPES_KEY,
                        DEFAULT_DND_ALLOWED_TYPES,
                    ),
                    "description": "Notification types that bypass DND.",
                },
            ): SelectSelector(
                SelectSelectorConfig(
                    options=type_options,
                    translation_key=ID_CONFIG_DND_ALLOWED_TYPES_KEY,
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                ),
            ),
        },
        required=True,
        extra=vol.PREVENT_EXTRA,
    )


# ---------------------------
# Auto-Create Identities Schema
# ---------------------------


def get_auto_configure_ha_users_schema(
    defaults: dict | None, values: dict | None
) -> vol.Schema:
    """Return schema for auto-creating identities from existing HA users."""
    defaults = defaults or {}
    values = values or {}

    not_configured_ha_users = values.get(CONFIG_FLOW_SELECTED_HA_USERS_KEY, [])
    not_configured_ha_users_list = [u["value"] for u in not_configured_ha_users]

    # user_choices = {user["id"]: user["name"] for user in not_configured_ha_users}

    return vol.Schema(
        {
            # Select one or more HA users to auto-create identities for
            # vol.Required(
            #     "selected_users", default=defaults.get("selected_users", [])
            # ): [vol.In(list(user_choices.keys()))],
            vol.Required(
                CONFIG_FLOW_SELECTED_HA_USERS_KEY,
                default=defaults.get(
                    CONFIG_FLOW_SELECTED_HA_USERS_KEY,
                    not_configured_ha_users_list,
                ),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=not_configured_ha_users,
                    translation_key=CONFIG_FLOW_SELECTED_HA_USERS_KEY,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            ),
        }
    )


# ---------------------------
# Identity Selector Schema
# ---------------------------


def get_identity_selection_schema(defaults: dict | None, values: dict | None):
    """Choose an existing HA user or create a custom identity."""
    defaults = defaults or {}
    values = values or {}
    schema_dict: dict[vol.Marker, object]

    user_modes = [RecipientType.VIRTUAL.value]
    not_configured_ha_users = values.get(CONFIG_FLOW_SELECTED_HA_USERS_KEY, [])
    # not_configured_ha_users_list = [u["value"] for u in not_configured_ha_users]
    if not_configured_ha_users:
        user_modes.append(RecipientType.HA_USER.value)

    schema_dict = {
        vol.Required(
            ID_CONFIG_TYPE_KEY,
            default=defaults.get(ID_CONFIG_TYPE_KEY, RecipientType.VIRTUAL.value),
        ): vol.In(user_modes),
        # vol.Optional("ha_user_id", default=defaults.get("ha_user_id")): vol.In(
        #     # list(user_choices.keys())
        #     not_configured_ha_users_list
        # )
        # if not_configured_ha_users_list
        # else str,
    }
    if not_configured_ha_users:
        schema_dict[
            vol.Optional(
                SUBENTRY_FLOW_SELECTED_HA_USER_KEY,
                description={
                    "suggested_value": defaults.get(SUBENTRY_FLOW_SELECTED_HA_USER_KEY),
                },
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=not_configured_ha_users,
                translation_key=SUBENTRY_FLOW_SELECTED_HA_USER_KEY,
                multiple=False,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        # ] = vol.Any(
        #     SelectSelector(
        #         SelectSelectorConfig(
        #             options=not_configured_ha_users,
        #             translation_key=SUBENTRY_FLOW_SELECTED_HA_USER_KEY,
        #             multiple=False,
        #             mode=SelectSelectorMode.DROPDOWN,
        #         )
        #     ),
        #     None
        # )

    # if defaults.get(SUBENTRY_FLOW_DEFINE_IDENTITY_SETTINGS_KEY, True):
    #     schema_dict[
    #         vol.Required(
    #             SUBENTRY_FLOW_DEFINE_IDENTITY_SETTINGS_KEY,
    #             default=False,
    #         )
    #     ] = bool
    # user_choices = {u["user_id"]: u.get("name") or u.get("username") for u in values}
    return vol.Schema(
        schema_dict,
        extra=vol.PREVENT_EXTRA,
    )


# ---------------------------
# Identity Definition Schema
# ---------------------------


def get_identity_definition_schema(
    defaults: dict | None, values: dict | None = None
) -> vol.Schema:
    """Return schema for defining identity basics.

    - If linked to an HA user → lock id, pre-fill name.
    - If custom identity → require name, and derive id from name later.
    """
    defaults = defaults or {}
    values = values or {}

    # if values:
    #     # Identity is tied to an HA user: lock id, default name = HA username
    return vol.Schema(
        {
            # # Lock ID to HA user ID (cannot be changed)
            # vol.Required(
            #     ID_CONFIG_ID_KEY, default=defaults.get(ID_CONFIG_ID_KEY)
            # ): vol.In([values["id"]]),
            # Default name is HA username, still editable for friendliness
            vol.Required(
                ID_CONFIG_NAME_KEY,
                # default=values["name"],
                description={
                    "suggested_value": defaults.get(ID_CONFIG_NAME_KEY),
                },
            ): str,
            # Contact information optional
            vol.Optional(
                ID_CONFIG_EMAIL_KEY,
                # default=defaults.get(ID_CONFIG_EMAIL_KEY),
                description={
                    "suggested_value": defaults.get(ID_CONFIG_EMAIL_KEY),
                },
            ): str,
            vol.Optional(
                ID_CONFIG_PHONE_KEY,
                # default=defaults.get(ID_CONFIG_PHONE_KEY),
                description={
                    "suggested_value": defaults.get(ID_CONFIG_PHONE_KEY),
                },
            ): str,
        },
        extra=vol.PREVENT_EXTRA,
    )

    # # Custom identity: user must provide a name
    # # The ID will be auto-derived later from name using slugify
    # return vol.Schema(
    #     {
    #         vol.Required(
    #             ID_CONFIG_NAME_KEY, default=defaults.get(ID_CONFIG_NAME_KEY)
    #         ): str,
    #         vol.Optional(
    #             ID_CONFIG_EMAIL_KEY, default=defaults.get(ID_CONFIG_EMAIL_KEY)
    #         ): str,
    #         vol.Optional(
    #             ID_CONFIG_PHONE_KEY, default=defaults.get(ID_CONFIG_PHONE_KEY)
    #         ): str,
    #     }
    # )
