"""Models for the ANS system."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from enum import Enum

# from .config_validator import ConfigValidator
from .const import (
    # SYS_CONFIG_TTS_INTEGRATION_KEY,
    CONFIG_VERSION_KEY,
    DEFAULT_BLOCKED_SOURCES_PATTERN,
    DEFAULT_DND_ALLOWED_SOURCES_PATTERN,
    DEFAULT_DND_ENABLED,
    DEFAULT_DND_END,
    DEFAULT_DND_START,
    DEFAULT_RATE_LIMIT,
    DEFAULT_RETRY_ATTEMPTS,
    ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY,
    ID_CONFIG_CHANNELS_KEY,
    ID_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY,
    ID_CONFIG_DND_ENABLED_KEY,
    ID_CONFIG_DND_END_KEY,
    ID_CONFIG_DND_START_KEY,
    ID_CONFIG_EMAIL_KEY,
    ID_CONFIG_ID_KEY,
    ID_CONFIG_IDENTITY_ID_KEY,
    ID_CONFIG_NAME_KEY,
    ID_CONFIG_NOTIFICATION_TYPES_KEY,
    ID_CONFIG_PHONE_KEY,
    ID_CONFIG_RATE_LIMIT_KEY,
    ID_CONFIG_RETRY_ATTEMPTS_KEY,
    ID_CONFIG_TYPE_KEY,
    SYS_CONFIG_ENABLED_CHANNELS_KEY,
    SYS_CONFIG_RATE_LIMIT_MAX_KEY,
    SYS_CONFIG_RATE_LIMIT_WINDOW_KEY,
    SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY,
)

_LOGGER = logging.getLogger(__name__)

# --- Data Classes ---


@dataclass
class Receiver:
    """Combines identity and receiver config preferences in a single object."""

    identity: Identity
    config: IdentityConfig


@dataclass
class Identity:
    """Represents an ANS receiver (native HA user or custom virtual target)."""

    id: str
    type: IdentityType
    name: str
    email: str | None
    phone: str | None
    version: int = field(default=1)

    def __post_init__(self):
        """Validate and convert configuration values."""
        # Import at runtime to avoid circular imports
        from .config_validator import ConfigValidator

        ConfigValidator.validate_identity(self)

    def to_dict(self) -> dict:
        """Convert to JSON-friendly dictionary for storage."""
        data = asdict(self)
        # Ensure ID is always a string
        data[ID_CONFIG_ID_KEY] = str(self.id)
        # Convert Enum to string for Store persistence
        if isinstance(self.type, Enum):
            data[ID_CONFIG_TYPE_KEY] = self.type.value
        else:
            data[ID_CONFIG_TYPE_KEY] = self.type
        return data

    @staticmethod
    def from_dict(data: dict) -> Identity:
        """Create Identity from a dictionary."""
        return Identity(
            id=data[ID_CONFIG_ID_KEY],
            type=IdentityType(data[ID_CONFIG_TYPE_KEY]),
            name=data[ID_CONFIG_NAME_KEY],
            email=data.get(ID_CONFIG_EMAIL_KEY),
            phone=data.get(ID_CONFIG_PHONE_KEY),
            version=data.get(CONFIG_VERSION_KEY, 1),
        )


@dataclass
class IdentityConfig:
    """Configuration for an ANS receiver."""

    identity_id: str | None  # Identity ID, must be set for each config
    retry_attempts: int
    rate_limit: int
    notification_types: list[NotificationType]
    blocked_sources_pattern: str | None
    channels_low: list[str]
    channels_medium: list[str]
    channels_high: list[str]
    channels_critical: list[str]
    dnd_enabled: bool
    dnd_start: str | None
    dnd_end: str | None
    dnd_allowed_sources_pattern: str | None
    version: int = field(default=1)

    def __post_init__(self):
        """Validate and normalize configuration values."""
        # Normalize identity_id to None or string
        if self.identity_id is not None:
            self.identity_id = str(self.identity_id)

        from .config_validator import ConfigValidator

        ConfigValidator.validate_identity_config(self)

    @classmethod
    def default(cls) -> IdentityConfig:
        """Return a default identity config instance."""
        return cls(
            identity_id=None,
            retry_attempts=DEFAULT_RETRY_ATTEMPTS,
            rate_limit=DEFAULT_RATE_LIMIT,
            notification_types=[
                NotificationType.INFO,
                NotificationType.WARNING,
                NotificationType.ALERT,
                NotificationType.REMINDER,
                NotificationType.EVENT,
                NotificationType.SECURITY,
            ],
            channels_low=[],
            channels_medium=[],
            channels_high=[],
            channels_critical=[],
            dnd_enabled=DEFAULT_DND_ENABLED,
            dnd_start=DEFAULT_DND_START,
            dnd_end=DEFAULT_DND_END,
            dnd_allowed_sources_pattern=DEFAULT_DND_ALLOWED_SOURCES_PATTERN,
            blocked_sources_pattern=DEFAULT_BLOCKED_SOURCES_PATTERN,
            version=1,
        )

    def to_dict(self) -> dict:
        """Convert to JSON-friendly dictionary for storage."""
        data = asdict(self)
        # Enums → values
        # data[ID_CONFIG_CRITICALITY_CHANNELS_KEY] = [
        #     c.value for c in self.criticality_levels
        # ]
        data[ID_CONFIG_NOTIFICATION_TYPES_KEY] = [
            t.value for t in self.notification_types
        ]
        data[
            f"{ID_CONFIG_CHANNELS_KEY}_{NotificationCriticality.LOW.value.lower()}"
        ] = list(self.channels_low)
        data[
            f"{ID_CONFIG_CHANNELS_KEY}_{NotificationCriticality.MEDIUM.value.lower()}"
        ] = list(self.channels_medium)
        data[
            f"{ID_CONFIG_CHANNELS_KEY}_{NotificationCriticality.HIGH.value.lower()}"
        ] = list(self.channels_high)
        data[
            f"{ID_CONFIG_CHANNELS_KEY}_{NotificationCriticality.CRITICAL.value.lower()}"
        ] = list(self.channels_critical)
        # Ensure identity_id stays string
        if self.identity_id is None:
            data[ID_CONFIG_IDENTITY_ID_KEY] = None
        else:
            data[ID_CONFIG_IDENTITY_ID_KEY] = str(self.identity_id)
        return data

    @staticmethod
    def from_dict(data: dict) -> IdentityConfig:
        """Create IdentityConfig from a dictionary."""
        # migration hook could be called here if `CONFIG_VERSION_KEY` differs
        version = int(data.get(CONFIG_VERSION_KEY, 1))

        # convert enums back from saved values
        notification_types = [
            NotificationType(t) for t in data.get(ID_CONFIG_NOTIFICATION_TYPES_KEY, [])
        ]
        channels_low = data.get(
            f"{ID_CONFIG_CHANNELS_KEY}_{NotificationCriticality.LOW.value.lower()}",
            [],
        )
        channels_medium = data.get(
            f"{ID_CONFIG_CHANNELS_KEY}_{NotificationCriticality.MEDIUM.value.lower()}",
            [],
        )
        channels_high = data.get(
            f"{ID_CONFIG_CHANNELS_KEY}_{NotificationCriticality.HIGH.value.lower()}",
            [],
        )
        channels_critical = data.get(
            f"{ID_CONFIG_CHANNELS_KEY}_{NotificationCriticality.CRITICAL.value.lower()}",
            [],
        )

        return IdentityConfig(
            identity_id=data.get(ID_CONFIG_IDENTITY_ID_KEY),
            retry_attempts=data.get(
                ID_CONFIG_RETRY_ATTEMPTS_KEY, DEFAULT_RETRY_ATTEMPTS
            ),
            rate_limit=data.get(ID_CONFIG_RATE_LIMIT_KEY, DEFAULT_RATE_LIMIT),
            # criticality_levels=criticality_levels,
            notification_types=notification_types,
            channels_low=channels_low,
            channels_medium=channels_medium,
            channels_high=channels_high,
            channels_critical=channels_critical,
            dnd_enabled=data.get(ID_CONFIG_DND_ENABLED_KEY, False),
            dnd_start=data.get(ID_CONFIG_DND_START_KEY),
            dnd_end=data.get(ID_CONFIG_DND_END_KEY),
            dnd_allowed_sources_pattern=data.get(
                ID_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY
            ),
            blocked_sources_pattern=data.get(ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY),
            version=version,
        )


@dataclass
class SystemConfig:
    """Configuration for the ANS system."""

    retry_attempts_max: int
    rate_limit_max: int
    rate_limit_window: int  # seconds
    enabled_channels: list[str]
    # tts_integration: str | None
    version: int = field(default=1)

    def __post_init__(self):
        """Validate and convert configuration values."""
        from .config_validator import ConfigValidator

        ConfigValidator.validate_system_config(self)

    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        data = asdict(self)
        data[SYS_CONFIG_ENABLED_CHANNELS_KEY] = list(self.enabled_channels)
        return data

    @staticmethod
    def from_dict(data: dict) -> SystemConfig:
        """Create SystemConfig from a dictionary."""
        return SystemConfig(
            retry_attempts_max=data[SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY],
            rate_limit_max=data[SYS_CONFIG_RATE_LIMIT_MAX_KEY],
            rate_limit_window=data[SYS_CONFIG_RATE_LIMIT_WINDOW_KEY],
            enabled_channels=data.get(SYS_CONFIG_ENABLED_CHANNELS_KEY, []),
            # tts_integration=data.get(SYS_CONFIG_TTS_INTEGRATION_KEY),
            version=data.get(CONFIG_VERSION_KEY, 1),
        )


# @dataclass
# class SystemLimits:
#     """System limits for ANS configuration."""

#     enabled_channels: list[ChannelInfo]
#     rate_limit_max: int
#     retries_max: int

#     def __init__(self, system_config: SystemConfig):
#         """Initialize system limits."""
#         self.enabled_channels = system_config.enabled_channels
#         self.rate_limit_max = system_config.rate_limit_max
#         self.retries_max = system_config.retries_max


@dataclass
class IntegrationInfo:
    """Integration information."""

    id: str
    label: str
    service: str


# @dataclass
# class NotificationChannel:
#     """Information about a notification channel."""

#     id: str
#     label: str
#     integration: str
#     service: str


# @dataclass
# class ChannelInfo:
#     """Information about a notification channel."""

#     id: str
#     name: str

#     def to_dict(self) -> dict:
#         """Convert to dictionary for storage."""
#         return asdict(self)

#     @staticmethod
#     def from_dict(data: dict) -> ChannelInfo:
#         """Create ChannelInfo from a dictionary."""
#         return ChannelInfo(
#             id=data["id"],
#             name=data["name"],
#         )


@dataclass
class DeliveryStats:
    """Persistent delivery statistics per user and channel."""

    identity_id: str | None  # Identity ID, must be set for each stats entry
    channel_id: str
    total_sent: int = 0
    total_failed: int = 0
    last_sent_timestamp: str | None = None  # ISO8601
    version: int = field(default=1)

    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> DeliveryStats:
        """Create DeliveryStats from a dictionary."""
        return DeliveryStats(
            identity_id=data.get(ID_CONFIG_IDENTITY_ID_KEY),
            channel_id=data["channel_id"],
            total_sent=data.get("total_sent", 0),
            total_failed=data.get("total_failed", 0),
            last_sent_timestamp=data.get("last_sent_timestamp"),
            version=data.get("version", 1),
        )


# --- Enums ---


class IdentityType(Enum):
    """Types of users in the ANS system."""

    HA_USER = "HA_USER"
    VIRTUAL = "VIRTUAL"


class NotificationCriticality(Enum):
    """Criticality levels for notifications."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class NotificationType(Enum):
    """Types of notifications."""

    INFO = "INFO"
    WARNING = "WARNING"
    ALERT = "ALERT"
    REMINDER = "REMINDER"
    EVENT = "EVENT"
    SECURITY = "SECURITY"
