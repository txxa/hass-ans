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
    # DEFAULT_TTS_INTEGRATION,
    RCPT_CONFIG_BLOCKED_SOURCES_PATTERN_KEY,
    RCPT_CONFIG_CHANNELS_KEY,
    RCPT_CONFIG_CONFIGURED_CHANNELS_KEY,
    RCPT_CONFIG_CRITICALITY_LEVELS_KEY,
    RCPT_CONFIG_DND_ALLOWED_CRITICALITIES_KEY,
    RCPT_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY,
    RCPT_CONFIG_DND_ALLOWED_TYPES_KEY,
    RCPT_CONFIG_DND_ENABLED_KEY,
    RCPT_CONFIG_DND_END_KEY,
    RCPT_CONFIG_DND_START_KEY,
    RCPT_CONFIG_EMAIL_KEY,
    RCPT_CONFIG_NAME_KEY,
    RCPT_CONFIG_NOTIFICATION_TYPES_KEY,
    RCPT_CONFIG_PHONE_KEY,
    RCPT_CONFIG_RATE_LIMIT_KEY,
    RCPT_CONFIG_RECIPIENT_CHOICE_KEY,
    RCPT_CONFIG_RETRY_ATTEMPTS_KEY,
    RCPT_DEFAULT_BLOCKED_SOURCES_PATTERN,
    RCPT_DEFAULT_CRITICALITY_LEVELS,
    RCPT_DEFAULT_DND_ALLOWED_CRITICALITIES,
    RCPT_DEFAULT_DND_ALLOWED_SOURCES_PATTERN,
    RCPT_DEFAULT_DND_ALLOWED_TYPES,
    RCPT_DEFAULT_DND_ENABLED_STATE,
    RCPT_DEFAULT_DND_END_TIME,
    RCPT_DEFAULT_DND_START_TIME,
    RCPT_DEFAULT_NOTIFICATION_TYPES,
    RCPT_DEFAULT_RATE_LIMIT,
    # Integration metadata
    RCPT_DEFAULT_RETRY_ATTEMPTS,
    RCPT_MAX_RETRY_ATTEMPTS,
    SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY,
    SYS_CONFIG_ENABLED_CHANNELS_KEY,
    SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY,
    SYS_CONFIG_QUEUE_CONCURRENCY_KEY,
    SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY,
    SYS_CONFIG_RETRY_BASE_DELAY_KEY,
    SYS_CONFIG_RETRY_MAX_DELAY_KEY,
    SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY,
    SYS_DEFAULT_ENABLE_AUDIT_LOGGING,
    SYS_DEFAULT_ENABLED_CHANNELS,
    SYS_DEFAULT_GLOBAL_RATE_LIMIT,
    SYS_DEFAULT_QUEUE_CONCURRENCY,
    SYS_DEFAULT_RETRY_BACKOFF_FACTOR,
    SYS_DEFAULT_RETRY_BASE_DELAY_SECONDS,
    SYS_DEFAULT_RETRY_MAX_DELAY_SECONDS,
    SYS_MAX_GLOBAL_RATE_LIMIT,
    SYS_MAX_QUEUE_CONCURRENCY,
    SYS_MAX_RETRY_BACKOFF_FACTOR,
    SYS_MAX_RETRY_BASE_DELAY_SECONDS,
    SYS_MAX_RETRY_MAX_DELAY_SECONDS,
    SYS_STORAGE_DEFAULT_FILE_RETENTION_DAYS,
    SYS_STORAGE_MAX_FILE_RETENTION_DAYS,
    # SYS_CONFIG_TTS_INTEGRATION_KEY,
)
from .models import (
    NotificationCriticality,
    NotificationType,
)

# ---------------------------
# System Config Schema
# ---------------------------


def get_system_config_schema(
    defaults: dict | None,
    values: dict | None,
    include_rate_limits: bool = True,
    include_audit_logging: bool = False,
) -> vol.Schema:
    """Return schema for system-wide configuration.

    Args:
        defaults: Default values for form fields
        values: Available options for select fields (e.g., enabled_channels)
        include_rate_limits: Whether to include rate limit fields (False for initial setup)
        include_audit_logging: Whether to include audit logging toggle

    """
    defaults = defaults or {}
    values = values or {}

    schema_dict = {}

    # Only include rate limits if requested (for reconfigure flow or old compatibility)
    if include_rate_limits:
        schema_dict[
            vol.Required(
                SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY,
                description={
                    "suggested_value": defaults.get(
                        SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY,
                        SYS_DEFAULT_GLOBAL_RATE_LIMIT,
                    ),
                },
            )
        ] = int

    # Which notification channels are enabled system-wide
    schema_dict[
        vol.Required(
            SYS_CONFIG_ENABLED_CHANNELS_KEY,
            description={
                "suggested_value": defaults.get(
                    SYS_CONFIG_ENABLED_CHANNELS_KEY, SYS_DEFAULT_ENABLED_CHANNELS
                ),
            },
        )
    ] = SelectSelector(
        SelectSelectorConfig(
            options=values.get(
                SYS_CONFIG_ENABLED_CHANNELS_KEY, SYS_DEFAULT_ENABLED_CHANNELS
            ),
            translation_key=SYS_CONFIG_ENABLED_CHANNELS_KEY,
            multiple=True,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )

    # Audit logging toggle (stored in config entry data)
    if include_audit_logging:
        schema_dict[
            vol.Optional(
                SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY,
                default=defaults.get(
                    SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY,
                    SYS_DEFAULT_ENABLE_AUDIT_LOGGING,
                ),
            )
        ] = bool

    # if defaults.get(CONFIG_FLOW_DEFINE_DEFAULT_IDENTITY_SETTINGS_KEY, True):
    #     schema_dict[
    #         vol.Required(
    #             CONFIG_FLOW_DEFINE_DEFAULT_IDENTITY_SETTINGS_KEY,
    #             default=False,
    #         )
    #     ] = bool

    return vol.Schema(schema_dict)


def get_system_options_schema(
    defaults: dict | None, include_audit_retention: bool = True
) -> vol.Schema:
    """Return schema for system options (tunable parameters only).

    Args:
        defaults: Default values for form fields
        include_audit_retention: Whether to include audit retention field

    """
    defaults = defaults or {}

    schema_dict = {
        vol.Required(
            SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY,
            description={
                "suggested_value": defaults.get(
                    SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY,
                    SYS_DEFAULT_GLOBAL_RATE_LIMIT,
                ),
            },
        ): vol.All(
            int,
            vol.Range(min=0, max=SYS_MAX_GLOBAL_RATE_LIMIT),
        ),
        vol.Required(
            SYS_CONFIG_RETRY_BASE_DELAY_KEY,
            description={
                "suggested_value": defaults.get(
                    SYS_CONFIG_RETRY_BASE_DELAY_KEY,
                    SYS_DEFAULT_RETRY_BASE_DELAY_SECONDS,
                )
            },
        ): vol.All(int, vol.Range(min=1, max=SYS_MAX_RETRY_BASE_DELAY_SECONDS)),
        vol.Required(
            SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY,
            description={
                "suggested_value": defaults.get(
                    SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY,
                    SYS_DEFAULT_RETRY_BACKOFF_FACTOR,
                )
            },
        ): vol.All(
            vol.Coerce(float),
            vol.Range(min=1.0, max=SYS_MAX_RETRY_BACKOFF_FACTOR),
        ),
        vol.Required(
            SYS_CONFIG_RETRY_MAX_DELAY_KEY,
            description={
                "suggested_value": defaults.get(
                    SYS_CONFIG_RETRY_MAX_DELAY_KEY,
                    SYS_DEFAULT_RETRY_MAX_DELAY_SECONDS,
                )
            },
        ): vol.All(int, vol.Range(min=60, max=SYS_MAX_RETRY_MAX_DELAY_SECONDS)),
        vol.Required(
            SYS_CONFIG_QUEUE_CONCURRENCY_KEY,
            description={
                "suggested_value": defaults.get(
                    SYS_CONFIG_QUEUE_CONCURRENCY_KEY, SYS_DEFAULT_QUEUE_CONCURRENCY
                )
            },
        ): vol.All(int, vol.Range(min=1, max=SYS_MAX_QUEUE_CONCURRENCY)),
    }

    # Only show audit retention if audit logging is enabled
    if include_audit_retention:
        schema_dict[
            vol.Required(
                SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY,
                description={
                    "suggested_value": defaults.get(
                        SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY,
                        SYS_STORAGE_DEFAULT_FILE_RETENTION_DAYS,
                    )
                },
            )
        ] = vol.All(int, vol.Range(min=0, max=SYS_STORAGE_MAX_FILE_RETENTION_DAYS))

    return vol.Schema(schema_dict)


# ------------------------
# Recipient config schemas
# ------------------------


def get_recipient_selection_schema(defaults: dict | None, values: dict | None):
    """Choose what type of recipient to create with clear, unified options.

    Creates a single dropdown with all available recipient options:
    - System recipients (one per available system channel)
    - HA users (not yet configured as recipients)
    - Virtual recipient (custom/manual entry)

    Args:
        defaults: Default values for the form
        values: Available options including:
            - available_system_channels: List of SelectOptionDict for system channels
            - CONFIG_FLOW_SELECTED_HA_USERS_KEY: List of SelectOptionDict for HA users

    Returns:
        Voluptuous schema with a single unified selection field

    """
    defaults = defaults or {}
    values = values or {}

    # Build unified options list
    options: list[SelectOptionDict] = []

    # Add system recipient option if not already configured
    if values.get("system_recipient_available", True):
        options.append(
            SelectOptionDict(
                label="System: Home Assistant",
                value="system_home_assistant",
            )
        )

    # Add HA user options (users not yet configured as recipients)
    ha_users = values.get(CONFIG_FLOW_SELECTED_HA_USERS_KEY, [])
    options.extend(
        [
            SelectOptionDict(
                label=f"HA User: {user['label']}",
                value=f"ha_user_{user['value']}",
            )
            for user in ha_users
        ]
    )

    # Always add virtual recipient option
    options.append(
        SelectOptionDict(
            label="Virtual: Custom recipient (enter manually)",
            value="virtual_new",
        )
    )

    return vol.Schema(
        {
            vol.Required(
                RCPT_CONFIG_RECIPIENT_CHOICE_KEY,
                description={
                    "suggested_value": defaults.get(RCPT_CONFIG_RECIPIENT_CHOICE_KEY),
                },
            ): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    translation_key=RCPT_CONFIG_RECIPIENT_CHOICE_KEY,
                    multiple=False,
                    mode=SelectSelectorMode.LIST,  # Use LIST mode for radio buttons
                )
            ),
        },
        extra=vol.PREVENT_EXTRA,
    )


def get_recipient_definition_schema(
    defaults: dict | None, values: dict | None = None
) -> vol.Schema:
    """Return schema for defining recipient basics.

    - If linked to an HA user → lock id, pre-fill name.
    - If custom recipient → require name, and derive id from name later.
    """
    defaults = defaults or {}
    values = values or {}

    return vol.Schema(
        {
            # Default name is HA username, still editable for friendliness
            vol.Required(
                RCPT_CONFIG_NAME_KEY,
                # default=values["name"],
                description={
                    "suggested_value": defaults.get(RCPT_CONFIG_NAME_KEY),
                },
            ): str,
            # Contact information optional
            vol.Optional(
                RCPT_CONFIG_EMAIL_KEY,
                # default=defaults.get(ID_CONFIG_EMAIL_KEY),
                description={
                    "suggested_value": defaults.get(RCPT_CONFIG_EMAIL_KEY),
                },
            ): str,
            vol.Optional(
                RCPT_CONFIG_PHONE_KEY,
                # default=defaults.get(ID_CONFIG_PHONE_KEY),
                description={
                    "suggested_value": defaults.get(RCPT_CONFIG_PHONE_KEY),
                },
            ): str,
        },
        extra=vol.PREVENT_EXTRA,
    )


def get_recipient_basic_settings_schema(
    defaults: dict | None, validation_context: ValidationContext, values: dict | None
) -> vol.Schema:
    """Return schema for base recipient-level configuration."""
    defaults = defaults or {}
    values = values or {}

    # Get allowed notification types
    available_types = values.get(
        RCPT_CONFIG_NOTIFICATION_TYPES_KEY, RCPT_DEFAULT_NOTIFICATION_TYPES
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
                RCPT_CONFIG_RATE_LIMIT_KEY,
                # default=defaults.get(ID_CONFIG_RATE_LIMIT_KEY, DEFAULT_RATE_LIMIT_VALUE),
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_RATE_LIMIT_KEY, RCPT_DEFAULT_RATE_LIMIT
                    ),
                },
            ): vol.All(
                int,
                vol.Range(min=0, max=validation_context.get_max_recipient_rate_limit()),
            ),
            vol.Required(
                RCPT_CONFIG_RETRY_ATTEMPTS_KEY,
                # default=defaults.get(
                #     ID_CONFIG_RETRY_ATTEMPTS_KEY, DEFAULT_RETRY_ATTEMPTS
                # ),
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_RETRY_ATTEMPTS_KEY, RCPT_DEFAULT_RETRY_ATTEMPTS
                    ),
                },
            ): vol.All(int, vol.Range(min=0, max=RCPT_MAX_RETRY_ATTEMPTS)),
            # Types of notifications that this identity should receive
            vol.Required(
                RCPT_CONFIG_NOTIFICATION_TYPES_KEY,
                # default=defaults.get(
                #     ID_CONFIG_NOTIFICATION_TYPES_KEY, available_types_list
                # ),
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_NOTIFICATION_TYPES_KEY, available_types_list
                    ),
                },
            ): SelectSelector(
                SelectSelectorConfig(
                    options=available_types,
                    translation_key=RCPT_CONFIG_NOTIFICATION_TYPES_KEY,
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                RCPT_CONFIG_BLOCKED_SOURCES_PATTERN_KEY,
                # default=defaults.get(
                #     ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY,
                #     DEFAULT_BLOCKED_SOURCES_PATTERN,
                # ),
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_BLOCKED_SOURCES_PATTERN_KEY,
                        RCPT_DEFAULT_BLOCKED_SOURCES_PATTERN,
                    ),
                },
            ): vol.Any(None, str),
        },
        extra=vol.PREVENT_EXTRA,
    )


def get_recipient_criticality_channel_mapping_schema(
    defaults: dict | None,
    values: dict | None,
) -> vol.Schema:
    """Return schema for criticality → channel mapping.

    Args:
        defaults: Default values for form fields.
        values: Available options for select fields (channels already filtered).

    Returns:
        Voluptuous schema for channel mapping form.

    """
    defaults = defaults or {}
    values = values or {}

    # Get channels - these are already SelectOptionDict objects from the caller
    available_channels_options = values.get(RCPT_CONFIG_CONFIGURED_CHANNELS_KEY, [])

    # Extract channel IDs from SelectOptionDict objects
    available_channels_list = [ch["value"] for ch in available_channels_options]

    criticality_levels = values.get(
        RCPT_CONFIG_CRITICALITY_LEVELS_KEY, RCPT_DEFAULT_CRITICALITY_LEVELS
    )
    schema_dict = {}

    for crit in criticality_levels:
        key = f"{RCPT_CONFIG_CHANNELS_KEY}_{crit['value'].lower()}"
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


def get_recipient_dnd_settings_schema(
    defaults: dict | None, values: dict | None
) -> vol.Schema:
    """Return schema for recipient Do-Not-Disturb configuration."""
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
                RCPT_CONFIG_DND_ENABLED_KEY,
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_DND_ENABLED_KEY, RCPT_DEFAULT_DND_ENABLED_STATE
                    ),
                },
            ): bool,
            vol.Optional(
                RCPT_CONFIG_DND_START_KEY,
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_DND_START_KEY, RCPT_DEFAULT_DND_START_TIME
                    ),
                    "description": "Start time of Do Not Disturb (in 24-hour format).",
                },
            ): selector({"time": {}}),
            vol.Optional(
                RCPT_CONFIG_DND_END_KEY,
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_DND_END_KEY, RCPT_DEFAULT_DND_END_TIME
                    ),
                    "description": "End time of Do Not Disturb (in 24-hour format).",
                },
            ): selector({"time": {}}),
            vol.Optional(
                RCPT_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY,
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY,
                        RCPT_DEFAULT_DND_ALLOWED_SOURCES_PATTERN,
                    ),
                    "description": "Regular expression pattern for explicitly allowed sources.",
                },
            ): str,
            vol.Optional(
                RCPT_CONFIG_DND_ALLOWED_CRITICALITIES_KEY,
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_DND_ALLOWED_CRITICALITIES_KEY,
                        RCPT_DEFAULT_DND_ALLOWED_CRITICALITIES,
                    ),
                    "description": "Notification criticality levels that bypass DND.",
                },
            ): SelectSelector(
                SelectSelectorConfig(
                    options=criticality_options,
                    translation_key=RCPT_CONFIG_DND_ALLOWED_CRITICALITIES_KEY,
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                ),
            ),
            vol.Optional(
                RCPT_CONFIG_DND_ALLOWED_TYPES_KEY,
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_DND_ALLOWED_TYPES_KEY,
                        RCPT_DEFAULT_DND_ALLOWED_TYPES,
                    ),
                    "description": "Notification types that bypass DND.",
                },
            ): SelectSelector(
                SelectSelectorConfig(
                    options=type_options,
                    translation_key=RCPT_CONFIG_DND_ALLOWED_TYPES_KEY,
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                ),
            ),
        },
        required=True,
        extra=vol.PREVENT_EXTRA,
    )
