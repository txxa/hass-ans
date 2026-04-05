"""Constants for the ANS integration."""

from homeassistant.components.media_player.const import MediaPlayerEntityFeature

# Integration identification
DOMAIN = "ans"
NAME = "Advanced Notification System"
VERSION = "0.1.0"

SERVICE_SEND = "send_notification"
PERSISTENT_NOTIFICATION_CHANNEL = "notify.persistent_notification"
SYS_DEFAULT_SYSTEM_RECIPIENT_NAME = "Home Assistant"

# Max values for system configuration
SYS_MAX_GLOBAL_RATE_LIMIT = 10000  # Max value configurable in system settings
SYS_MAX_RETRY_BASE_DELAY_SECONDS = 3600
SYS_MAX_RETRY_BACKOFF_FACTOR = 5
SYS_MAX_RETRY_MAX_DELAY_SECONDS = 86400
SYS_MAX_QUEUE_CONCURRENCY = 20
# Default values for system configuration
SYS_DEFAULT_RATE_LIMIT_WINDOW = 60  # Rate limit window in seconds
SYS_DEFAULT_GLOBAL_RATE_LIMIT = 100  # Pre-filled default value in form
SYS_DEFAULT_RETRY_BASE_DELAY_SECONDS = 60  # Base delay before first retry
SYS_DEFAULT_RETRY_BACKOFF_FACTOR = 2  # Exponential backoff multiplier
SYS_DEFAULT_RETRY_MAX_DELAY_SECONDS = 3600  # Maximum delay (1 hour)
SYS_DEFAULT_ENABLED_CHANNELS = [
    PERSISTENT_NOTIFICATION_CHANNEL
]  # Enabled channels for notifications
SYS_DEFAULT_QUEUE_CONCURRENCY = 5
SYS_DEFAULT_ENABLE_AUDIT_LOGGING = True  # Enable audit logging by default

# Deduplication constants
SYS_DEDUP_WINDOW_SECONDS = 60
SYS_DEDUP_MAX_CACHE_SIZE = 1000
SYS_DEDUP_CLEANUP_INTERVAL = 60

# Storage and persistence keys
SYS_STORAGE_VERSION = 1
SYS_STORAGE_DIR = ".storage"
SYS_STORAGE_NOTIFICATIONS_FILE = "ans_notifications.json"
SYS_STORAGE_ATTEMPTS_FILE = "ans_delivery_attempts.json"
SYS_STORAGE_RETRIES_FILE = "ans_retry_queue.json"
SYS_STORAGE_HOUSEKEEPING_INTERVAL_HOURS = 1
SYS_STORAGE_MAX_FILE_RETENTION_DAYS = 365
SYS_STORAGE_DEFAULT_FILE_RETENTION_DAYS = 7

# Max values for recipient configurations
RCPT_MAX_RATE_LIMIT = 1000  # Max value configurable per recipient
RCPT_MAX_RETRY_ATTEMPTS = 5  # Maximum retries for all notifications
# Default values for recipient configurations
RCPT_DEFAULT_RATE_LIMIT = 20  # Pre-filled default value in form
RCPT_DEFAULT_RETRY_ATTEMPTS = 3  # Default retry attempts for notifications
RCPT_DEFAULT_CRITICALITY_LEVELS = []  # Default criticality levels for notifications
RCPT_DEFAULT_NOTIFICATION_TYPES = []  # Default notification types for users
RCPT_DEFAULT_CONFIGURED_CHANNELS = []  # Default configured channels for notifications
RCPT_DEFAULT_BLOCKED_SOURCES_PATTERN = None  # Default blocked sources pattern
RCPT_DEFAULT_DND_ENABLED_STATE = False  # Default Do Not Disturb setting
RCPT_DEFAULT_DND_START_TIME = "22:00:00"  # Default DND start time
RCPT_DEFAULT_DND_END_TIME = "06:00:00"  # Default DND end time
RCPT_DEFAULT_DND_ALLOWED_SOURCES_PATTERN = None  # Default allowed sources pattern
RCPT_DEFAULT_DND_ALLOWED_TYPES = ["ALERT"]  # Notification types that bypass DND
RCPT_DEFAULT_DND_ALLOWED_CRITICALITIES = [
    "CRITICAL"
]  # Criticality levels that bypass DND

# Persistent storage keys
# IDENTITIES_KEY = "identities"
# IDENTITY_CONFIGS_KEY = "identity_configs"
# IDENTITY_DEFAULT_CONFIG_KEY = "default_identity_config"
# CONFIG_SYSTEM_SETTINGS_KEY = "system_settings"
# CONFIG_IDENTITY_DEFAULT_SETTINGS_KEY = "default_identity_settings"
CONFIG_VERSION_KEY = "version"

# System recipient (virtual recipient for system-wide channels)
# SYS_RECIPIENT_CONFIG_KEY = "system_recipient_config"
# SYS_RECIPIENT_ID = "_ans_system"
# SYS_RECIPIENT_NAME = "System Channels"

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

SUBENTRY_FLOW_STEP_RECIPIENT_SELECTION_KEY = "recipient_selection"
SUBENTRY_FLOW_STEP_RECIPIENT_DEFINITION_KEY = "recipient_definition"
SUBENTRY_FLOW_STEP_RECIPIENT_BASIC_SETTINGS_KEY = "recipient_basic_settings"
SUBENTRY_FLOW_STEP_RECIPIENT_CHANNEL_MAPPING_KEY = "recipient_channel_mapping"
SUBENTRY_FLOW_STEP_RECIPIENT_DND_SETTINGS_KEY = "recipient_dnd_settings"
SUBENTRY_FLOW_STEP_RECIPIENT_TTS_SETTINGS_KEY = (
    "recipient_tts_settings"  # NEW: TTS settings step
)
# SUBENTRY_FLOW_STEP_ID_IDENTITY_SELECTION_KEY = "recipient_selection"
# SUBENTRY_FLOW_IDENTITY_TYPE_SELECTION_KEY = "recipient_type_selection"
SUBENTRY_FLOW_SELECTED_HA_USER_KEY = "selected_ha_user"
# SUBENTRY_FLOW_DEFINE_RECIPIENT_SETTINGS_KEY = "define_recipient_settings"
SUBENTRY_FLOW_ERROR_INVALID_RECIPIENT_SELECTION_KEY = "invalid_selection"
SUBENTRY_FLOW_ERROR_INVALID_RECIPIENT_DEFINITION_KEY = "invalid_definition"
SUBENTRY_FLOW_ERROR_INVALID_RECIPIENT_SETTINGS_KEY = "invalid_settings"
SUBENTRY_FLOW_ERROR_INVALID_CHANNEL_MAPPING_KEY = "invalid_channel_mapping"
SUBENTRY_FLOW_ERROR_INVALID_DND_SETTINGS_KEY = "invalid_dnd_settings"

# System config keys
SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY = "global_rate_limit"
SYS_CONFIG_RATE_LIMIT_WINDOW_KEY = "rate_limit_window"
SYS_CONFIG_ENABLED_CHANNELS_KEY = "enabled_channels"
SYS_CONFIG_RETRY_BASE_DELAY_KEY = "retry_base_delay"
SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY = "retry_backoff_factor"
SYS_CONFIG_RETRY_MAX_DELAY_KEY = "retry_max_delay"
SYS_CONFIG_QUEUE_CONCURRENCY_KEY = "queue_max_concurrency"
SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY = "storage_retention_days"
SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY = "enable_audit_logging"
SYS_CONFIG_TTS_SERVICE_KEY = "tts_service"  # NEW: TTS service configuration

# Recipient keys
RCPT_CONFIG_ID_KEY = "id"
RCPT_CONFIG_TYPE_KEY = "type"
RCPT_CONFIG_NAME_KEY = "name"
RCPT_CONFIG_EMAIL_KEY = "email"
RCPT_CONFIG_PHONE_KEY = "phone"
RCPT_CONFIG_PARENT_ENTRY_ID_KEY = "parent_entry_id"

# Recipient config keys
RCPT_CONFIG_RECIPIENT_ID_KEY = "recipient_id"
RCPT_CONFIG_RATE_LIMIT_KEY = "rate_limit"
RCPT_CONFIG_RETRY_ATTEMPTS_KEY = "retry_attempts"
RCPT_CONFIG_BLOCKED_SOURCES_PATTERN_KEY = "blocked_sources_regex"
RCPT_CONFIG_CONFIGURED_CHANNELS_KEY = "configured_channels"
RCPT_CONFIG_CRITICALITY_LEVELS_KEY = "criticality_levels"
RCPT_CONFIG_CHANNELS_KEY = "channels"
RCPT_CONFIG_NOTIFICATION_TYPES_KEY = "notification_types"
RCPT_CONFIG_DND_ENABLED_KEY = "dnd_enabled"
RCPT_CONFIG_DND_TIMES_KEY = "dnd_times"
RCPT_CONFIG_DND_START_KEY = "dnd_start"
RCPT_CONFIG_DND_START_MISSING_KEY = "dnd_start_missing"
RCPT_CONFIG_DND_END_KEY = "dnd_end"
RCPT_CONFIG_DND_END_MISSING_KEY = "dnd_end_missing"
RCPT_CONFIG_DND_START_END_EQUALS_KEY = "dnd_start_end_equals"
RCPT_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY = "dnd_allowed_sources_regex"
RCPT_CONFIG_DND_ALLOWED_CRITICALITIES_KEY = "dnd_allowed_criticalities"
RCPT_CONFIG_DND_ALLOWED_TYPES_KEY = "dnd_allowed_types"
RCPT_CONFIG_RECIPIENT_CHOICE_KEY = "recipient_choice"

# Recipient choice values
RECIPIENT_CHOICE_SYSTEM_HA = "system_home_assistant"
RECIPIENT_CHOICE_HA_USER_PREFIX = "ha_user_"
RECIPIENT_CHOICE_TTS = "tts_new"
RECIPIENT_CHOICE_VIRTUAL = "virtual_new"

# TTS Recipient Settings Keys
RCPT_CONFIG_TTS_SETTINGS_KEY = "tts_settings"
RCPT_CONFIG_TTS_VOLUME_MORNING_KEY = "volume_morning"
RCPT_CONFIG_TTS_VOLUME_DAYTIME_KEY = "volume_daytime"
RCPT_CONFIG_TTS_VOLUME_EVENING_KEY = "volume_evening"
RCPT_CONFIG_TTS_VOLUME_NIGHT_KEY = "volume_night"
RCPT_CONFIG_TTS_VOLUME_OVERRIDE_CRITICALITIES_KEY = "volume_override_criticalities"
RCPT_CONFIG_TTS_VOLUME_OVERRIDE_LEVEL_KEY = "volume_override_level"
RCPT_CONFIG_TTS_MESSAGE_FORMAT_KEY = "message_format"
RCPT_CONFIG_TTS_SSML_ENABLED_KEY = "ssml_enabled"
RCPT_CONFIG_TTS_VOLUME_MANAGEMENT_ENABLED_KEY = "volume_management_enabled"

# TTS Defaults
TTS_DEFAULT_VOLUME_MORNING = 65
TTS_DEFAULT_VOLUME_DAYTIME = 75
TTS_DEFAULT_VOLUME_EVENING = 60
TTS_DEFAULT_VOLUME_NIGHT = 50
TTS_DEFAULT_VOLUME_OVERRIDE_LEVEL = 80
TTS_DEFAULT_MESSAGE_FORMAT = "title_and_message"
TTS_DEFAULT_SSML_ENABLED = (
    False  # -> SSML mode disabled by default (plain-text engines)
)
TTS_DEFAULT_VOLUME_MANAGEMENT_ENABLED = True
# Maximum seconds to wait for a media_player.volume_set service call to complete.
# Guards against hung media player integrations stalling the event loop.
VOLUME_SET_TIMEOUT = 10

# Channel detection
REQUIRED_MP_FEATURES = (
    MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.VOLUME_SET
)
