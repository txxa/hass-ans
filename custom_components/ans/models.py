"""Models for the ANS system."""
# pylint: disable=import-outside-toplevel

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Any
from uuid import UUID

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

# ---------------------------------------------------------------------------
# Notification primitives
# ---------------------------------------------------------------------------


class NotificationType(str, Enum):
    """Types of notifications."""

    INFO = "INFO"
    WARNING = "WARNING"
    ALERT = "ALERT"
    REMINDER = "REMINDER"
    EVENT = "EVENT"
    SECURITY = "SECURITY"


class NotificationCriticality(str, Enum):
    """Criticality levels for notifications."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class NotificationPayload:
    """Semantic notification content.

    This object is immutable and shared across all fan-out tasks.
    """

    notification_id: str
    source: str
    title: str
    message: str
    type: NotificationType
    criticality: NotificationCriticality
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Delivery task & attempt model
# ---------------------------------------------------------------------------


class DeliveryStatus(str, Enum):
    """Status of a delivery attempt.

    Values
    ------
    FILTERED : str
        Notification filtered by policy (terminal state).
    RATE_LIMITED : str
        Notification rate-limited, scheduled for retry.
    IN_PROGRESS : str
        Delivery attempt in flight.
    TRANSIENT_FAIL : str
        Temporary failure, retryable (e.g., network timeout).
    PERMANENT_FAIL : str
        Permanent failure, no retry (e.g., invalid config).
    SUCCESS : str
        Delivery succeeded (terminal state).

    """

    FILTERED = "FILTERED"
    RATE_LIMITED = "RATE_LIMITED"
    IN_PROGRESS = "IN_PROGRESS"
    TRANSIENT_FAIL = "TRANSIENT_FAIL"
    PERMANENT_FAIL = "PERMANENT_FAIL"
    SUCCESS = "SUCCESS"


@dataclass(frozen=True)
class NotificationDeliveryTask:
    """One task = one (notification × recipient × channel) delivery.

    Immutable description of work. Self-contained with all information needed
    for deterministic processing (crash recovery, replay).
    """

    job_id: UUID
    recipient_id: str
    channel: str
    payload: NotificationPayload
    policy: RecipientNotificationPolicy
    contact_info: RecipientContactInfo

    created_at: datetime
    snapshot_id: str | None = None


@dataclass
class Attempt:
    """One concrete execution attempt for a delivery task.

    Persisted and idempotency-relevant.
    """

    attempt_id: UUID
    job_id: UUID
    attempt_number: int
    idempotency_key: str
    status: DeliveryStatus
    started_at: datetime

    ended_at: datetime | None = None
    endpoint: str | None = None
    remote_id: str | None = None
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryResult:
    """Result returned by a channel adapter."""

    status: DeliveryStatus
    remote_id: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Filtering / policy model
# ---------------------------------------------------------------------------


class FilterDecisionType(str, Enum):
    """Outcome of notification filter evaluation.

    Values
    ------
    ALLOWED : str
        Notification passes all filter policies.
    FILTERED : str
        Notification blocked by filter policy.

    """

    ALLOWED = "ALLOWED"
    FILTERED = "FILTERED"


class FilterReason(str, Enum):
    """Reason for filter decision outcome.

    Values
    ------
    NORMAL : str
        Notification passed all checks normally.
    DND_BYPASS : str
        Notification allowed due to DND bypass rule match.
    TYPE_NOT_ALLOWED : str
        Notification type not in recipient's allowed types.
    SOURCE_BLOCKED : str
        Notification source matches blocked regex pattern.
    DND_ACTIVE : str
        Notification filtered by active Do Not Disturb window.

    """

    NORMAL = "NORMAL"
    DND_BYPASS = "DND_BYPASS"
    TYPE_NOT_ALLOWED = "TYPE_NOT_ALLOWED"
    SOURCE_BLOCKED = "SOURCE_BLOCKED"
    DND_ACTIVE = "DND_ACTIVE"


@dataclass(frozen=True)
class FilterDecision:
    """Canonical output of the filter engine.

    FILTERED decisions are terminal.
    """

    decision: FilterDecisionType
    reason: FilterReason
    details: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# Recipient policy configuration (pure input to filter engine)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DoNotDisturbConfig:
    """Do-not-disturb window definition."""

    start: time | None
    end: time | None
    allowed_sources_regex: str | None  # re.Pattern


@dataclass(frozen=True)
class RecipientNotificationPolicy:
    """Declarative notification policy for a recipient.

    x -> Maps 1:1 to the FilterDecision state machine.
    """

    retry_attempts: int
    rate_limit: int
    rate_limit_window: int
    allowed_types: list[NotificationType]  # TODO: should be set
    blocked_sources_regex: str | None
    dnd: DoNotDisturbConfig | None = None


@dataclass(frozen=True)
class RecipientContactInfo:
    """Contact information for a recipient."""

    email_address: str | None
    phone_number: str | None


# @dataclass(frozen=True)
# class RecipientChannelConfig:
#     low: list[str]
#     medium: list[str]
#     high: list[str]
#     critical: list[str]


# ---------------------------------------------------------------------------
# Retry & scheduling primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrySchedule:
    """Persisted retry intent."""

    job_id: UUID
    attempt_number: int
    run_at: datetime
    reason: str | None = None


# ---------------------------------------------------------------------------
# Aggregate / reporting helpers (optional but useful)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryState:
    """Aggregate state for a delivery task."""

    job_id: UUID
    status: DeliveryStatus
    last_attempt_number: int
    updated_at: datetime
    failure_reason: str | None = None


# ---------------------------------------------------------------------------
# RecipientData primitives
# ---------------------------------------------------------------------------


class RecipientType(str, Enum):
    """Types of users in the ANS system."""

    HA_USER = "HA_USER"
    VIRTUAL = "VIRTUAL"


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
        from .config_validator import ConfigValidator  # noqa: PLC0415

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
    def from_dict(data: dict) -> RecipientData:
        """Create RecipientData from a dictionary."""
        return RecipientData(
            id=data[ID_CONFIG_ID_KEY],
            type=RecipientType(data[ID_CONFIG_TYPE_KEY]),
            name=data[ID_CONFIG_NAME_KEY],
            email=data.get(ID_CONFIG_EMAIL_KEY),
            phone=data.get(ID_CONFIG_PHONE_KEY),
            version=data.get(CONFIG_VERSION_KEY, 1),
        )


# @dataclass
# class Recipient:
#     """Combines identity and receiver config preferences in a single object."""

#     data: RecipientData
#     config: RecipientConfig
#     # type: RecipientType


# ---------------------------------------------------------------------------
# Config primitives
# ---------------------------------------------------------------------------


@dataclass
class RecipientConfig:
    """Configuration for an ANS receiver."""

    identity_id: str | None  # RecipientData ID, must be set for each config
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
    version: int = field(default=1)

    def __post_init__(self):
        """Validate and normalize configuration values."""
        # Normalize identity_id to None or string
        if self.identity_id is not None:
            self.identity_id = str(self.identity_id)

        from .config_validator import ConfigValidator  # noqa: PLC0415

        ConfigValidator.validate_identity_config(self)

    @classmethod
    def default(cls) -> RecipientConfig:
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
            dnd_allowed_sources_regex=DEFAULT_DND_ALLOWED_SOURCES_PATTERN,
            blocked_sources_regex=DEFAULT_BLOCKED_SOURCES_PATTERN,
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
    def from_dict(data: dict) -> RecipientConfig:
        """Create RecipientConfig from a dictionary."""
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

        return RecipientConfig(
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
            dnd_allowed_sources_regex=data.get(
                ID_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY
            ),
            blocked_sources_regex=data.get(ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY),
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

    def __post_init__(self) -> None:
        """Validate system configuration.

        Raises
        ------
        ValueError
            If system configuration is invalid.

        """
        from .config_validator import ConfigValidator  # noqa: PLC0415

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


@dataclass(frozen=True)
class ConfigSnapshot:
    """Immutable snapshot of all configuration required for notification delivery decisions."""

    snapshot_id: str
    created_at: datetime
    recipients: dict[str, RecipientData]
    recipient_configs: dict[str, RecipientConfig]
    system_config: SystemConfig

    def getRecipients(self) -> list[str]:
        """Get list of configured recipient IDs.

        Returns
        -------
        list[str]
            List of all recipient identifiers.

        """
        return list(self.recipients.keys())

    def getRecipientChannels(
        self, recipient_id: str, criticality: NotificationCriticality
    ) -> set[str]:
        """Get delivery channels for recipient at given criticality level.

        Parameters
        ----------
        recipient_id : str
            The recipient identifier.
        criticality : NotificationCriticality
            The notification criticality level.

        Returns
        -------
        set[str]
            Set of channel names for delivery at this criticality.

        """
        recipient_config = self.recipient_configs[recipient_id]

        match criticality:
            case NotificationCriticality.LOW:
                return set(recipient_config.channels_low)
            case NotificationCriticality.MEDIUM:
                return set(recipient_config.channels_medium)
            case NotificationCriticality.HIGH:
                return set(recipient_config.channels_high)
            case NotificationCriticality.CRITICAL:
                return set(recipient_config.channels_critical)

    def getRecipientNotificationPolicy(
        self, recipient_id: str
    ) -> RecipientNotificationPolicy:
        """Get the notification policy for a recipient."""
        recipient_config = self.recipient_configs[recipient_id]

        dnd = None
        if (
            recipient_config.dnd_enabled
            and recipient_config.dnd_start
            and recipient_config.dnd_end
        ):
            dnd = DoNotDisturbConfig(
                start=datetime.strptime(recipient_config.dnd_start, "%H:%M:%S").time()
                if recipient_config.dnd_start
                else None,
                end=datetime.strptime(recipient_config.dnd_end, "%H:%M:%S").time()
                if recipient_config.dnd_end
                else None,
                allowed_sources_regex=recipient_config.dnd_allowed_sources_regex,
            )

        return RecipientNotificationPolicy(
            retry_attempts=recipient_config.retry_attempts,
            rate_limit=recipient_config.rate_limit,
            rate_limit_window=self.system_config.rate_limit_window,
            allowed_types=recipient_config.notification_types,
            blocked_sources_regex=recipient_config.blocked_sources_regex,
            dnd=dnd,
        )

    def getRecipientContactInfo(self, recipient_id: str) -> RecipientContactInfo:
        """Get the contact info for a recipient."""
        recipient_info = self.recipients[recipient_id]
        return RecipientContactInfo(
            email_address=recipient_info.email,
            phone_number=recipient_info.phone,
        )


@dataclass
class IntegrationInfo:
    """Integration information."""

    id: str
    label: str
    service: str
