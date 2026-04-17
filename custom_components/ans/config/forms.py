"""Voluptuous schema definitions for ANS config flows."""

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    selector,
)

from ..const import (
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
    RCPT_CONFIG_TTS_MESSAGE_FORMAT_KEY,
    RCPT_CONFIG_TTS_SSML_ENABLED_KEY,
    RCPT_CONFIG_TTS_VOLUME_DAYTIME_KEY,
    RCPT_CONFIG_TTS_VOLUME_EVENING_KEY,
    RCPT_CONFIG_TTS_VOLUME_MANAGEMENT_ENABLED_KEY,
    RCPT_CONFIG_TTS_VOLUME_MORNING_KEY,
    RCPT_CONFIG_TTS_VOLUME_NIGHT_KEY,
    RCPT_CONFIG_TTS_VOLUME_OVERRIDE_CRITICALITIES_KEY,
    RCPT_CONFIG_TTS_VOLUME_OVERRIDE_LEVEL_KEY,
    RCPT_CONFIG_USER_KEY,
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
    SYS_CONFIG_TTS_SERVICE_KEY,
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
    TTS_DEFAULT_MESSAGE_FORMAT,
    TTS_DEFAULT_SSML_ENABLED,
    TTS_DEFAULT_VOLUME_DAYTIME,
    TTS_DEFAULT_VOLUME_EVENING,
    TTS_DEFAULT_VOLUME_MANAGEMENT_ENABLED,
    TTS_DEFAULT_VOLUME_MORNING,
    TTS_DEFAULT_VOLUME_NIGHT,
    TTS_DEFAULT_VOLUME_OVERRIDE_LEVEL,
)
from ..models import (
    NotificationCriticality,
    NotificationType,
    RecipientType,
)
from .validator import ValidationContext

_LOGGER = logging.getLogger(__name__)

# ---------------------------
# System Config Schema
# ---------------------------


def detect_tts_integrations(hass: HomeAssistant) -> list[SelectOptionDict]:
    """Return available TTS engine entities formatted for a dropdown selector.

    Thin UI-formatting wrapper around
    :func:`~custom_components.ans.channels.channel_manager.detect_tts_entities`.
    Detection logic lives in the channel manager; this function only handles
    the conversion to :class:`SelectOptionDict` for the config-flow forms.

    Parameters
    ----------
    hass : HomeAssistant
        Home Assistant instance.

    Returns
    -------
    list[SelectOptionDict]
        TTS engine entities formatted for a dropdown selector.
        Each item contains ``value`` (entity ID) and ``label`` (friendly name).

    """
    from ..channels.channel_manager import detect_tts_entities  # noqa: PLC0415

    results: list[SelectOptionDict] = [
        SelectOptionDict(
            value=entity_id,
            label=entity_id.removeprefix("tts.").replace("_", " ").title(),
        )
        for entity_id in detect_tts_entities(hass)
    ]

    _LOGGER.debug(
        "Detected %d TTS entities: %s",
        len(results),
        [s["value"] for s in results],
    )
    return results


def get_system_config_schema(
    defaults: dict | None,
    values: dict | None,
    include_rate_limits: bool = True,
    include_audit_logging: bool = False,
    hass: HomeAssistant | None = None,
) -> vol.Schema:
    """Return schema for system-wide configuration.

    Args:
        defaults: Default values for form fields
        values: Available options for select fields (e.g., enabled_channels, tts_services, media_players)
        include_rate_limits: Whether to include rate limit fields (False for initial setup)
        include_audit_logging: Whether to include audit logging toggle
        hass: Home Assistant instance (required for TTS service validation)

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

    # TTS service selection (optional)
    tts_services = values.get("tts_services", [])
    if tts_services:  # Only show if TTS services are available
        # Add "None" option to allow disabling TTS
        tts_options = [SelectOptionDict(value="None", label="(Disabled)")]
        tts_options.extend(tts_services)

        schema_dict[
            vol.Optional(
                SYS_CONFIG_TTS_SERVICE_KEY,
                description={
                    "suggested_value": defaults.get(SYS_CONFIG_TTS_SERVICE_KEY, "None"),
                },
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=tts_options,
                translation_key=SYS_CONFIG_TTS_SERVICE_KEY,
                multiple=False,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )

    # Media player selection (only shown if TTS service is selected)
    # This is handled dynamically in the config flow by conditional form display
    media_players = values.get("media_players", [])
    current_tts = defaults.get(SYS_CONFIG_TTS_SERVICE_KEY)
    if media_players and current_tts and current_tts != "None":
        # Show media player selection when TTS is enabled
        # Note: This will be handled via channel selection in enabled_channels
        # Media players are added to the enabled_channels list automatically
        pass

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
        ] = selector({"boolean": {}})

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


def get_recipient_selection_schema(
    defaults: dict | None, options: list[SelectOptionDict]
):
    """Return schema for the recipient selection step.

    Args:
        defaults: Default values for the form (e.g. previously submitted input)
        options: Pre-built list of SelectOptionDict entries. Static entries use
            the option value as label (translated by the frontend via
            translation_key). Dynamic entries (e.g. HA users) carry their
            runtime label directly.

    Returns:
        Voluptuous schema with a single unified selection field

    """
    defaults = defaults or {}

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
    defaults: dict | None,
    options: list[SelectOptionDict] | None = None,
    recipient_type: RecipientType | None = None,
) -> vol.Schema:
    """Return schema for defining recipient basics.

    Args:
        defaults: Default values for form fields
        options: Available select options. For HA_USER recipients this populates
            the `user` dropdown; otherwise it is ignored.
        recipient_type: Recipient type; TTS recipients omit email and phone fields.

    Note:
        Contact information (email, phone) is optional. Channels that require
        specific contact info will be filtered in the channel mapping step.
        Users can provide minimal info upfront and will see which channels
        are available based on what they've configured.

    """
    defaults = defaults or {}
    options = options or []
    is_tts = recipient_type == RecipientType.TTS
    is_ha_user = recipient_type == RecipientType.HA_USER

    schema_dict: dict = {}

    if is_ha_user:
        schema_dict[
            vol.Required(
                RCPT_CONFIG_USER_KEY,
                description={
                    "suggested_value": defaults.get(RCPT_CONFIG_USER_KEY),
                },
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=options,
                translation_key=RCPT_CONFIG_USER_KEY,
                multiple=False,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
    else:
        schema_dict[
            vol.Required(
                RCPT_CONFIG_NAME_KEY,
                description={
                    "suggested_value": defaults.get(RCPT_CONFIG_NAME_KEY),
                },
            )
        ] = str

    if not is_tts:
        schema_dict[
            vol.Optional(
                RCPT_CONFIG_EMAIL_KEY,
                description={
                    "suggested_value": defaults.get(RCPT_CONFIG_EMAIL_KEY),
                },
            )
        ] = selector(
            {
                "text": {
                    "type": "email",
                    "autocomplete": "email",
                }
            }
        )
        schema_dict[
            vol.Optional(
                RCPT_CONFIG_PHONE_KEY,
                description={
                    "suggested_value": defaults.get(RCPT_CONFIG_PHONE_KEY),
                },
            )
        ] = selector(
            {
                "text": {
                    "type": "tel",
                    "autocomplete": "tel",
                }
            }
        )

    return vol.Schema(schema_dict, extra=vol.PREVENT_EXTRA)


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
        values: Available options for select fields including:
            - RCPT_CONFIG_CONFIGURED_CHANNELS_KEY: Available channels (SelectOptionDict)
                These channels are already filtered based on:
                - System-wide enabled channels
                - Recipient type (system vs recipient-specific)
                - Contact information availability (email, phone, HA user)

    Returns:
        Voluptuous schema for channel mapping form.

    """
    defaults = defaults or {}
    values = values or {}

    # Get available channels (already filtered based on contact info)
    available_channels_options = values.get(RCPT_CONFIG_CONFIGURED_CHANNELS_KEY, [])

    # Extract channel IDs from SelectOptionDict objects
    # available_channels_list = [ch["value"] for ch in available_channels_options]

    criticality_levels = values.get(
        RCPT_CONFIG_CRITICALITY_LEVELS_KEY, RCPT_DEFAULT_CRITICALITY_LEVELS
    )
    schema_dict = {}

    for crit in criticality_levels:
        key = f"{RCPT_CONFIG_CHANNELS_KEY}_{crit['value'].lower()}"

        # Note: Unavailable channels are not currently displayed in the UI
        # They are available in values["unavailable_channels"] if needed for future enhancements

        schema_dict[
            vol.Optional(
                key,
                description={
                    "suggested_value": defaults.get(
                        key,  # available_channels_list
                    ),  # default: all allowed
                },
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=available_channels_options,
                translation_key=key,
                multiple=True,
                mode=SelectSelectorMode.DROPDOWN,
                custom_value=False,
            )
        )

    return vol.Schema(
        schema_dict,
        extra=vol.PREVENT_EXTRA,
    )


def get_recipient_tts_settings_schema(
    defaults: dict | None, values: dict | None
) -> vol.Schema:
    """Return schema for TTS recipient settings.

    Configures volume levels for different time periods, criticality-based overrides,
    and message format preferences for TTS delivery.

    Args:
        defaults: Default values for form fields
        values: Available options for select fields (criticality_options)

    Time Frames (for volume control):
        - Morning: 06:00 - 09:00
        - Daytime: 09:00 - 18:00
        - Evening: 18:00 - 22:00
        - Night: 22:00 - 06:00

    Message Formats:
        - title_and_message: "{title}. {message}"
        - message_only: "{message}"
        - title_only: "{title}"

    """
    defaults = defaults or {}
    values = values or {}

    # Build criticality options for override selection
    criticality_options: list[SelectOptionDict] = [
        SelectOptionDict(label=c.value.title(), value=c.value)
        for c in NotificationCriticality
    ]

    # Build message format options
    message_format_options: list[SelectOptionDict] = [
        SelectOptionDict(
            label="Title and Message",
            value="title_and_message",
        ),
        SelectOptionDict(
            label="Message Only",
            value="message_only",
        ),
        SelectOptionDict(
            label="Title Only",
            value="title_only",
        ),
    ]

    # Volume number selector configuration
    volume_selector = selector(
        {
            "number": {
                "min": 0,
                "max": 100,
                "step": 1,
                "mode": "slider",
                "unit_of_measurement": "%",
            }
        }
    )

    return vol.Schema(
        {
            vol.Required(
                RCPT_CONFIG_TTS_MESSAGE_FORMAT_KEY,
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_TTS_MESSAGE_FORMAT_KEY,
                        TTS_DEFAULT_MESSAGE_FORMAT,
                    ),
                },
            ): SelectSelector(
                SelectSelectorConfig(
                    options=message_format_options,
                    translation_key=RCPT_CONFIG_TTS_MESSAGE_FORMAT_KEY,
                    multiple=False,
                    mode=SelectSelectorMode.DROPDOWN,
                ),
            ),
            vol.Optional(
                RCPT_CONFIG_TTS_SSML_ENABLED_KEY,
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_TTS_SSML_ENABLED_KEY,
                        TTS_DEFAULT_SSML_ENABLED,
                    ),
                },
            ): selector({"boolean": {}}),
            vol.Optional(
                RCPT_CONFIG_TTS_VOLUME_MANAGEMENT_ENABLED_KEY,
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_TTS_VOLUME_MANAGEMENT_ENABLED_KEY,
                        TTS_DEFAULT_VOLUME_MANAGEMENT_ENABLED,
                    ),
                },
            ): selector({"boolean": {}}),
            vol.Required(
                RCPT_CONFIG_TTS_VOLUME_MORNING_KEY,
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_TTS_VOLUME_MORNING_KEY,
                        TTS_DEFAULT_VOLUME_MORNING,
                    ),
                },
            ): volume_selector,
            vol.Required(
                RCPT_CONFIG_TTS_VOLUME_DAYTIME_KEY,
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_TTS_VOLUME_DAYTIME_KEY,
                        TTS_DEFAULT_VOLUME_DAYTIME,
                    ),
                },
            ): volume_selector,
            vol.Required(
                RCPT_CONFIG_TTS_VOLUME_EVENING_KEY,
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_TTS_VOLUME_EVENING_KEY,
                        TTS_DEFAULT_VOLUME_EVENING,
                    ),
                },
            ): volume_selector,
            vol.Required(
                RCPT_CONFIG_TTS_VOLUME_NIGHT_KEY,
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_TTS_VOLUME_NIGHT_KEY,
                        TTS_DEFAULT_VOLUME_NIGHT,
                    ),
                },
            ): volume_selector,
            vol.Required(
                RCPT_CONFIG_TTS_VOLUME_OVERRIDE_LEVEL_KEY,
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_TTS_VOLUME_OVERRIDE_LEVEL_KEY,
                        TTS_DEFAULT_VOLUME_OVERRIDE_LEVEL,
                    ),
                },
            ): volume_selector,
            vol.Optional(
                RCPT_CONFIG_TTS_VOLUME_OVERRIDE_CRITICALITIES_KEY,
                description={
                    "suggested_value": defaults.get(
                        RCPT_CONFIG_TTS_VOLUME_OVERRIDE_CRITICALITIES_KEY,
                        [],
                    ),
                },
            ): SelectSelector(
                SelectSelectorConfig(
                    options=criticality_options,
                    translation_key=RCPT_CONFIG_TTS_VOLUME_OVERRIDE_CRITICALITIES_KEY,
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                ),
            ),
        },
        required=True,
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
            ): selector({"boolean": {}}),
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
