"""Constants for the ANS integration."""

# Integration identification
DOMAIN = "ans"
NAME = "Advanced Notification System"
VERSION = "0.1.0"

# Default values for system configuration
DEFAULT_RETRIES_MAX = 5  # Default maximum retries for notifications
DEFAULT_RATE_LIMIT_MAX = 20  # Default maximum rate limit for notifications
DEFAULT_RATE_LIMIT_WINDOW = 60  # Default rate limit window in seconds
# DEFAULT_TTS_INTEGRATION = None  # Default TTS integration, None means no TTS
DEFAULT_ENABLED_CHANNELS = [
    "notify.persistent_notification"
]  # Default enabled channels for notifications

# Default values for identity configurations
DEFAULT_RATE_LIMIT = 10  # Default rate limit for notifications
DEFAULT_RETRY_ATTEMPTS = 3  # Default retry attemts for notifications
DEFAULT_CRITICALITY_LEVELS = []  # Default criticality levels for notifications
DEFAULT_NOTIFICATION_TYPES = []  # Default notification types for users
DEFAULT_CONFIGURED_CHANNELS = []  # Default configured channels for notifications
DEFAULT_DND_ENABLED = False  # Default Do Not Disturb setting
DEFAULT_DND_START = "22:00:00"  # Default DND start time
DEFAULT_DND_END = "06:00:00"  # Default DND end time
DEFAULT_DND_ALLOWED_SOURCES_PATTERN = None  # Default allowed sources pattern
DEFAULT_BLOCKED_SOURCES_PATTERN = None  # Default blocked sources pattern

# Persistent storage keys
# IDENTITIES_KEY = "identities"
# IDENTITY_CONFIGS_KEY = "identity_configs"
# IDENTITY_DEFAULT_CONFIG_KEY = "default_identity_config"
# CONFIG_SYSTEM_SETTINGS_KEY = "system_settings"
# CONFIG_IDENTITY_DEFAULT_SETTINGS_KEY = "default_identity_settings"
CONFIG_VERSION_KEY = "version"

CONFIG_FLOW_STEP_SYS_SETTINGS_KEY = "system_settings"
CONFIG_FLOW_STEP_CONFIG_FLOW_OPTIONS_KEY = "config_flow_options"
CONFIG_FLOW_STEP_ID_DEFAULT_BASIC_SETTINGS_KEY = "default_identity_basic_settings"
CONFIG_FLOW_STEP_ID_DEFAULT_CHANNEL_MAPPING_KEY = "default_identity_channel_mapping"
CONFIG_FLOW_STEP_ID_DEFAULT_DND_SETTINGS_KEY = "default_identity_dnd_settings"
CONFIG_FLOW_STEP_AUTO_HA_USER_CONFIGURATION_KEY = "auto_ha_user_configuration"
CONFIG_FLOW_DEFINE_DEFAULT_IDENTITY_SETTINGS_KEY = "define_identity_default_settings"
CONFIG_FLOW_SELECTED_HA_USERS_KEY = "selected_ha_users"
CONFIG_FLOW_ERROR_INVALID_SYSTEM_SETTINGS_KEY = "invalid_system_settings"
CONFIG_FLOW_ERROR_INVALID_IDENTITY_SETTINGS_KEY = "invalid_identity_settings"
CONFIG_FLOW_ERROR_HA_USER_DETECTION_FAILED_KEY = "ha_user_detection_failed"
CONFIG_FLOW_ERROR_INVALID_HA_USER_SELECTION_KEY = "invalid_ha_user_selection"

SUBENTRY_FLOW_STEP_IDENTITY_DEFINITION_KEY = "identity_definition"
SUBENTRY_FLOW_STEP_IDENTITY_SELECTION_KEY = "identity_selection"
SUBENTRY_FLOW_STEP_BASIC_SETTINGS_KEY = "identity_basic_settings"
SUBENTRY_FLOW_STEP_CHANNEL_MAPPING_KEY = "identity_channel_mapping"
SUBENTRY_FLOW_STEP_DND_SETTINGS_KEY = "identity_dnd_settings"
SUBENTRY_FLOW_STEP_ID_IDENTITY_SELECTION_KEY = "identity_selection"
SUBENTRY_FLOW_IDENTITY_TYPE_SELECTION_KEY = "identity_type_selection"
SUBENTRY_FLOW_SELECTED_HA_USER_KEY = "selected_ha_user"
SUBENTRY_FLOW_DEFINE_IDENTITY_SETTINGS_KEY = "define_identity_settings"
SUBENTRY_FLOW_ERROR_INVALID_IDENTITY_SELECTION_KEY = "invalid_identity_selection"
SUBENTRY_FLOW_ERROR_INVALID_IDENTITY_DEFINITION_KEY = "invalid_identity_definition"
SUBENTRY_FLOW_ERROR_INVALID_CHANNEL_MAPPING_KEY = "invalid_channel_mapping"

# System config keys
SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY = "retry_attempts_max"
SYS_CONFIG_RATE_LIMIT_MAX_KEY = "rate_limit_max"
SYS_CONFIG_RATE_LIMIT_WINDOW_KEY = "rate_limit_window"
# SYS_CONFIG_TTS_INTEGRATION_KEY = "tts_integration"
SYS_CONFIG_ENABLED_CHANNELS_KEY = "enabled_channels"

# Idenity keys
ID_CONFIG_ID_KEY = "id"
ID_CONFIG_TYPE_KEY = "type"
ID_CONFIG_NAME_KEY = "name"
ID_CONFIG_EMAIL_KEY = "email"
ID_CONFIG_PHONE_KEY = "phone"
ID_CONFIG_PARENT_ENTRY_ID_KEY = "parent_entry_id"

# Identity config keys
ID_CONFIG_IDENTITY_ID_KEY = "identity_id"
ID_CONFIG_RATE_LIMIT_KEY = "rate_limit"
ID_CONFIG_RETRY_ATTEMPTS_KEY = "retry_attempts"
ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY = "blocked_sources_pattern"
ID_CONFIG_CONFIGURED_CHANNELS_KEY = "configured_channels"
ID_CONFIG_CRITICALITY_LEVELS_KEY = "criticality_levels"
ID_CONFIG_CHANNELS_KEY = "channels"
ID_CONFIG_NOTIFICATION_TYPES_KEY = "notification_types"
ID_CONFIG_DND_ENABLED_KEY = "dnd_enabled"
ID_CONFIG_DND_TIMES_KEY = "dnd_times"
ID_CONFIG_DND_START_KEY = "dnd_start"
ID_CONFIG_DND_START_MISSING_KEY = "dnd_start_missing"
ID_CONFIG_DND_END_KEY = "dnd_end"
ID_CONFIG_DND_END_MISSING_KEY = "dnd_end_missing"
ID_CONFIG_DND_START_END_EQUALS_KEY = "dnd_start_end_equals"
ID_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY = "dnd_allowed_sources_pattern"
