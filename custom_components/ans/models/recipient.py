"""Recipient data and configuration models for the ANS system."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum

from ..const import (
    CONFIG_VERSION_KEY,
    PERSISTENT_NOTIFICATION_CHANNEL,
    RCPT_CONFIG_BLOCKED_SOURCES_PATTERN_KEY,
    RCPT_CONFIG_CHANNELS_KEY,
    RCPT_CONFIG_DND_ALLOWED_CRITICALITIES_KEY,
    RCPT_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY,
    RCPT_CONFIG_DND_ALLOWED_TYPES_KEY,
    RCPT_CONFIG_DND_ENABLED_KEY,
    RCPT_CONFIG_DND_END_KEY,
    RCPT_CONFIG_DND_START_KEY,
    RCPT_CONFIG_EMAIL_KEY,
    RCPT_CONFIG_ID_KEY,
    RCPT_CONFIG_NAME_KEY,
    RCPT_CONFIG_NOTIFICATION_TYPES_KEY,
    RCPT_CONFIG_PHONE_KEY,
    RCPT_CONFIG_RATE_LIMIT_KEY,
    RCPT_CONFIG_RECIPIENT_ID_KEY,
    RCPT_CONFIG_RETRY_ATTEMPTS_KEY,
    RCPT_CONFIG_TYPE_KEY,
    RCPT_DEFAULT_BLOCKED_SOURCES_PATTERN,
    RCPT_DEFAULT_DND_ALLOWED_CRITICALITIES,
    RCPT_DEFAULT_DND_ALLOWED_SOURCES_PATTERN,
    RCPT_DEFAULT_DND_ALLOWED_TYPES,
    RCPT_DEFAULT_DND_ENABLED_STATE,
    RCPT_DEFAULT_DND_END_TIME,
    RCPT_DEFAULT_DND_START_TIME,
    RCPT_DEFAULT_RATE_LIMIT,
    RCPT_DEFAULT_RETRY_ATTEMPTS,
)
from .notification import NotificationCriticality, NotificationType


class RecipientType(str, Enum):
    """Types of users in the ANS system."""

    HA_USER = "HA_USER"
    VIRTUAL = "VIRTUAL"
    SYSTEM = "SYSTEM"


@dataclass(frozen=True)
class RecipientContactInfo:
    """Contact information for a recipient."""

    email_address: str | None
    phone_number: str | None
    mobile_device_id: str | None = None


@dataclass
class RecipientData:
    """Represents an ANS recipient (native HA user or custom virtual target)."""

    id: str
    type: RecipientType
    name: str
    email: str | None
    phone: str | None
    version: int = field(default=1)

    def __post_init__(self) -> None:
        """Validate identity configuration.

        Raises
        ------
        ValueError
            If identity configuration is invalid.

        """
        from ..config.validator import ConfigValidator  # noqa: PLC0415

        ConfigValidator.validate_recipient(self)

    def to_dict(self) -> dict:
        """Convert to JSON-friendly dictionary for storage."""
        data = asdict(self)
        # Ensure ID is always a string
        data[RCPT_CONFIG_ID_KEY] = str(self.id)
        # Convert Enum to string for Store persistence
        if isinstance(self.type, Enum):
            data[RCPT_CONFIG_TYPE_KEY] = self.type.value
        else:
            data[RCPT_CONFIG_TYPE_KEY] = self.type
        return data

    @staticmethod
    def from_dict(data: dict) -> RecipientData:
        """Create RecipientData from a dictionary."""
        return RecipientData(
            id=data[RCPT_CONFIG_ID_KEY],
            type=RecipientType(data[RCPT_CONFIG_TYPE_KEY]),
            name=data[RCPT_CONFIG_NAME_KEY],
            email=data.get(RCPT_CONFIG_EMAIL_KEY),
            phone=data.get(RCPT_CONFIG_PHONE_KEY),
            version=data.get(CONFIG_VERSION_KEY, 1),
        )


@dataclass
class RecipientConfig:
    """Configuration for an ANS receiver."""

    recipient_id: str | None  # RecipientData ID, must be set for each config
    retry_attempts: int
    rate_limit: int
    notification_types: list[NotificationType]
    blocked_sources_regex: str | None
    channels_low: list[str]
    channels_medium: list[str]
    channels_high: list[str]
    channels_critical: list[str]
    dnd_enabled: bool
    dnd_start: str | None
    dnd_end: str | None
    dnd_allowed_sources_regex: str | None
    dnd_allowed_criticalities: list[str] | None = None
    dnd_allowed_types: list[str] | None = None
    version: int = field(default=1)

    def __post_init__(self):
        """Validate and normalize configuration values."""
        # Normalize identity_id to None or string
        if self.recipient_id is not None:
            self.recipient_id = str(self.recipient_id)

        from ..config.validator import ConfigValidator  # noqa: PLC0415

        ConfigValidator.validate_recipient_config(self)

    @classmethod
    def default(cls) -> RecipientConfig:
        """Return a default identity config instance."""
        return cls(
            recipient_id=None,
            retry_attempts=RCPT_DEFAULT_RETRY_ATTEMPTS,
            rate_limit=RCPT_DEFAULT_RATE_LIMIT,
            notification_types=list(NotificationType),
            channels_low=[],
            channels_medium=[],
            channels_high=[],
            channels_critical=[],
            dnd_enabled=RCPT_DEFAULT_DND_ENABLED_STATE,
            dnd_start=RCPT_DEFAULT_DND_START_TIME,
            dnd_end=RCPT_DEFAULT_DND_END_TIME,
            dnd_allowed_sources_regex=RCPT_DEFAULT_DND_ALLOWED_SOURCES_PATTERN,
            dnd_allowed_criticalities=RCPT_DEFAULT_DND_ALLOWED_CRITICALITIES,
            dnd_allowed_types=RCPT_DEFAULT_DND_ALLOWED_TYPES,
            blocked_sources_regex=RCPT_DEFAULT_BLOCKED_SOURCES_PATTERN,
            version=1,
        )

    @classmethod
    def system_default(cls) -> RecipientConfig:
        """Return a default config for the system recipient (with persistent_notification enabled)."""
        return cls(
            recipient_id=None,
            retry_attempts=RCPT_DEFAULT_RETRY_ATTEMPTS,
            rate_limit=RCPT_DEFAULT_RATE_LIMIT,
            notification_types=list(NotificationType),
            channels_low=[PERSISTENT_NOTIFICATION_CHANNEL],
            channels_medium=[PERSISTENT_NOTIFICATION_CHANNEL],
            channels_high=[PERSISTENT_NOTIFICATION_CHANNEL],
            channels_critical=[PERSISTENT_NOTIFICATION_CHANNEL],
            dnd_enabled=RCPT_DEFAULT_DND_ENABLED_STATE,
            dnd_start=RCPT_DEFAULT_DND_START_TIME,
            dnd_end=RCPT_DEFAULT_DND_END_TIME,
            dnd_allowed_sources_regex=RCPT_DEFAULT_DND_ALLOWED_SOURCES_PATTERN,
            dnd_allowed_criticalities=RCPT_DEFAULT_DND_ALLOWED_CRITICALITIES,
            dnd_allowed_types=RCPT_DEFAULT_DND_ALLOWED_TYPES,
            blocked_sources_regex=RCPT_DEFAULT_BLOCKED_SOURCES_PATTERN,
            version=1,
        )

    def to_dict(self) -> dict:
        """Convert to JSON-friendly dictionary for storage."""
        data = asdict(self)
        # Enums → values
        data[RCPT_CONFIG_NOTIFICATION_TYPES_KEY] = [
            t.value for t in self.notification_types
        ]
        data[
            f"{RCPT_CONFIG_CHANNELS_KEY}_{NotificationCriticality.LOW.value.lower()}"
        ] = list(self.channels_low)
        data[
            f"{RCPT_CONFIG_CHANNELS_KEY}_{NotificationCriticality.MEDIUM.value.lower()}"
        ] = list(self.channels_medium)
        data[
            f"{RCPT_CONFIG_CHANNELS_KEY}_{NotificationCriticality.HIGH.value.lower()}"
        ] = list(self.channels_high)
        data[
            f"{RCPT_CONFIG_CHANNELS_KEY}_{NotificationCriticality.CRITICAL.value.lower()}"
        ] = list(self.channels_critical)
        # Ensure identity_id stays string
        if self.recipient_id is None:
            data[RCPT_CONFIG_RECIPIENT_ID_KEY] = None
        else:
            data[RCPT_CONFIG_RECIPIENT_ID_KEY] = str(self.recipient_id)
        return data

    @staticmethod
    def from_dict(data: dict) -> RecipientConfig:
        """Create RecipientConfig from a dictionary."""
        # migration hook could be called here if `CONFIG_VERSION_KEY` differs
        version = int(data.get(CONFIG_VERSION_KEY, 1))

        # convert enums back from saved values
        notification_types = [
            NotificationType(t)
            for t in data.get(RCPT_CONFIG_NOTIFICATION_TYPES_KEY, [])
        ]
        channels_low = data.get(
            f"{RCPT_CONFIG_CHANNELS_KEY}_{NotificationCriticality.LOW.value.lower()}",
            [],
        )
        channels_medium = data.get(
            f"{RCPT_CONFIG_CHANNELS_KEY}_{NotificationCriticality.MEDIUM.value.lower()}",
            [],
        )
        channels_high = data.get(
            f"{RCPT_CONFIG_CHANNELS_KEY}_{NotificationCriticality.HIGH.value.lower()}",
            [],
        )
        channels_critical = data.get(
            f"{RCPT_CONFIG_CHANNELS_KEY}_{NotificationCriticality.CRITICAL.value.lower()}",
            [],
        )

        return RecipientConfig(
            recipient_id=data.get(RCPT_CONFIG_RECIPIENT_ID_KEY),
            retry_attempts=data.get(
                RCPT_CONFIG_RETRY_ATTEMPTS_KEY, RCPT_DEFAULT_RETRY_ATTEMPTS
            ),
            rate_limit=data.get(RCPT_CONFIG_RATE_LIMIT_KEY, RCPT_DEFAULT_RATE_LIMIT),
            notification_types=notification_types,
            channels_low=channels_low,
            channels_medium=channels_medium,
            channels_high=channels_high,
            channels_critical=channels_critical,
            dnd_enabled=data.get(RCPT_CONFIG_DND_ENABLED_KEY, False),
            dnd_start=data.get(RCPT_CONFIG_DND_START_KEY),
            dnd_end=data.get(RCPT_CONFIG_DND_END_KEY),
            dnd_allowed_sources_regex=data.get(
                RCPT_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY
            ),
            dnd_allowed_criticalities=data.get(
                RCPT_CONFIG_DND_ALLOWED_CRITICALITIES_KEY,
                RCPT_DEFAULT_DND_ALLOWED_CRITICALITIES,
            ),
            dnd_allowed_types=data.get(
                RCPT_CONFIG_DND_ALLOWED_TYPES_KEY, RCPT_DEFAULT_DND_ALLOWED_TYPES
            ),
            blocked_sources_regex=data.get(RCPT_CONFIG_BLOCKED_SOURCES_PATTERN_KEY),
            version=version,
        )
