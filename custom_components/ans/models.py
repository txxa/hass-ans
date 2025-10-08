"""Models for the ANS system."""
# pylint: disable=import-outside-toplevel

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from .channel_registry import ChannelRegistry

from .const import (
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
    SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY,
    SYS_CONFIG_ENABLED_CHANNELS_KEY,
    SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY,
    SYS_CONFIG_QUEUE_CONCURRENCY_KEY,
    SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY,
    SYS_CONFIG_RETRY_BASE_DELAY_KEY,
    SYS_CONFIG_RETRY_MAX_DELAY_KEY,
    SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY,
    SYS_DEFAULT_ENABLE_AUDIT_LOGGING,
    SYS_DEFAULT_GLOBAL_RATE_LIMIT,
    SYS_DEFAULT_QUEUE_CONCURRENCY,
    SYS_DEFAULT_RATE_LIMIT_WINDOW,
    SYS_DEFAULT_RETRY_BACKOFF_FACTOR,
    SYS_DEFAULT_RETRY_BASE_DELAY_SECONDS,
    SYS_DEFAULT_RETRY_MAX_DELAY_SECONDS,
    SYS_STORAGE_DEFAULT_FILE_RETENTION_DAYS,
)

# ---------------------------------------------------------------------------
# Channel primitives
# ---------------------------------------------------------------------------


class ChannelScope(str, Enum):
    """Scope of a notification channel.

    Values
    ------
    SYSTEM : str
        Channel delivers to the HA instance itself, not specific recipients.
        Examples: persistent_notification, TTS
    RECIPIENT : str
        Channel delivers to individual recipients.
        Examples: mobile_app, email, SMS
    """

    SYSTEM = "SYSTEM"
    RECIPIENT = "RECIPIENT"


@dataclass(frozen=True)
class ChannelInfo:
    """Immutable channel definition with scope and metadata.

    Attributes
    ----------
    id : str
        Unique channel identifier (e.g., "notify.persistent_notification")
    label : str
        Human-readable display name
    scope : ChannelScope
        Whether channel is system-wide or recipient-specific
    integration : str | None
        Source integration domain (e.g., "mobile_app", "persistent_notification")

    """

    id: str
    label: str
    scope: ChannelScope
    integration: str | None = None


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
    channel_info: ChannelInfo
    payload: NotificationPayload
    policy: RecipientNotificationPolicy
    contact_info: RecipientContactInfo

    created_at: datetime
    snapshot_id: str | None = None

    @classmethod
    def from_snapshot(cls, job_id: UUID, snapshot: dict) -> NotificationDeliveryTask:
        """Reconstruct task from persisted snapshot.

        Used for retry recovery after Home Assistant restart.

        Args:
            job_id: Job identifier.
            snapshot: Persisted task data dictionary.

        Returns:
            Reconstructed NotificationDeliveryTask.

        Raises:
            ValueError: If snapshot data is invalid or incomplete.

        """
        from datetime import UTC  # noqa: PLC0415

        try:
            # Reconstruct ChannelInfo
            channel_data = snapshot["channel_info"]
            channel_info = ChannelInfo(
                id=channel_data["id"],
                label=channel_data.get("name", channel_data["id"]),  # Fallback to id
                scope=ChannelScope.RECIPIENT,  # Assumption for retry tasks
                integration=channel_data.get("adapter_type"),
            )

            # Reconstruct NotificationPayload
            payload_data = snapshot["payload"]
            payload = NotificationPayload(
                notification_id=payload_data.get("notification_id", str(job_id)),
                source=payload_data.get("metadata", {}).get("source", "ans_recovery"),
                title=payload_data["title"],
                message=payload_data["message"],
                type=NotificationType.INFO,  # Default for recovered tasks
                criticality=NotificationCriticality.MEDIUM,  # Default for recovered tasks
                created_at=datetime.fromisoformat(payload_data["timestamp"]),
                metadata=payload_data.get("metadata", {}),
            )

            # Reconstruct RecipientNotificationPolicy
            policy_data = snapshot["policy"]
            policy = RecipientNotificationPolicy(
                rate_limit=policy_data["rate_limit"],
                rate_limit_window=policy_data["rate_limit_window"],
                retry_attempts=policy_data["retry_attempts"],
                allowed_types=list(NotificationType),  # Allow all for recovered tasks
                blocked_sources_regex=None,
                dnd=None,  # DND config not persisted
            )

            # Reconstruct RecipientContactInfo
            contact_data = snapshot["contact_info"]
            contact_info = RecipientContactInfo(
                email_address=contact_data.get("email"),
                phone_number=contact_data.get("phone"),
            )

            return cls(
                job_id=job_id,
                recipient_id=snapshot["recipient_id"],
                channel_info=channel_info,
                payload=payload,
                policy=policy,
                contact_info=contact_info,
                created_at=datetime.now(UTC),  # New timestamp for retry
                snapshot_id=None,
            )

        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"Invalid task snapshot data: {e}") from e

    def to_dict(self) -> dict:
        """Serialize task to dictionary for persistence.

        Returns:
            Dictionary representation of task suitable for JSON storage.

        """
        return {
            "job_id": str(self.job_id),
            "recipient_id": self.recipient_id,
            "channel_info": {
                "id": self.channel_info.id,
                "name": self.channel_info.label,
                "adapter_type": self.channel_info.integration,
            },
            "payload": {
                "notification_id": self.payload.notification_id,
                "title": self.payload.title,
                "message": self.payload.message,
                "timestamp": self.payload.created_at.isoformat(),
                "metadata": self.payload.metadata,
            },
            "policy": {
                "rate_limit": self.policy.rate_limit,
                "rate_limit_window": self.policy.rate_limit_window,
                "retry_attempts": self.policy.retry_attempts,
            },
            "contact_info": {
                "email": self.contact_info.email_address,
                "phone": self.contact_info.phone_number,
            },
            "created_at": self.created_at.isoformat(),
        }


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
    allowed_criticalities: list[NotificationCriticality] | None = None
    allowed_types: list[NotificationType] | None = None


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
    mobile_device_id: str | None = None


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
    SYSTEM = "SYSTEM"


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


# ---------------------------------------------------------------------------
# Config primitives
# ---------------------------------------------------------------------------


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

        from .config_validator import ConfigValidator  # noqa: PLC0415

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
        # data[ID_CONFIG_CRITICALITY_CHANNELS_KEY] = [
        #     c.value for c in self.criticality_levels
        # ]
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


@dataclass
class SystemConfig:
    """Configuration for the ANS system."""

    global_rate_limit: int
    rate_limit_window: int  # seconds (hard-coded to 60)
    enabled_channels: list[str]
    retry_base_delay: int = SYS_DEFAULT_RETRY_BASE_DELAY_SECONDS
    retry_backoff_factor: float = SYS_DEFAULT_RETRY_BACKOFF_FACTOR
    retry_max_delay: int = SYS_DEFAULT_RETRY_MAX_DELAY_SECONDS
    queue_max_concurrency: int = SYS_DEFAULT_QUEUE_CONCURRENCY
    storage_retention_days: int = SYS_STORAGE_DEFAULT_FILE_RETENTION_DAYS
    enable_audit_logging: bool = (
        True  # Enable/disable audit logging (notifications + attempts)
    )
    persistent_notifications_enabled: bool = (
        False  # Enable/disable persistent notifications
    )
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
        data[SYS_CONFIG_RETRY_BASE_DELAY_KEY] = self.retry_base_delay
        data[SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY] = self.retry_backoff_factor
        data[SYS_CONFIG_RETRY_MAX_DELAY_KEY] = self.retry_max_delay
        data[SYS_CONFIG_QUEUE_CONCURRENCY_KEY] = self.queue_max_concurrency
        data[SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY] = self.storage_retention_days
        data[SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY] = self.enable_audit_logging
        return data

    @staticmethod
    def from_dict(data: dict) -> SystemConfig:
        """Create SystemConfig from a dictionary."""
        return SystemConfig(
            global_rate_limit=data.get(
                SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY, SYS_DEFAULT_GLOBAL_RATE_LIMIT
            ),
            rate_limit_window=SYS_DEFAULT_RATE_LIMIT_WINDOW,
            enabled_channels=data.get(SYS_CONFIG_ENABLED_CHANNELS_KEY, []),
            retry_base_delay=data.get(
                SYS_CONFIG_RETRY_BASE_DELAY_KEY, SYS_DEFAULT_RETRY_BASE_DELAY_SECONDS
            ),
            retry_backoff_factor=data.get(
                SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY, SYS_DEFAULT_RETRY_BACKOFF_FACTOR
            ),
            retry_max_delay=data.get(
                SYS_CONFIG_RETRY_MAX_DELAY_KEY, SYS_DEFAULT_RETRY_MAX_DELAY_SECONDS
            ),
            queue_max_concurrency=data.get(
                SYS_CONFIG_QUEUE_CONCURRENCY_KEY, SYS_DEFAULT_QUEUE_CONCURRENCY
            ),
            storage_retention_days=data.get(
                SYS_CONFIG_STORAGE_RETENTION_DAYS_KEY,
                SYS_STORAGE_DEFAULT_FILE_RETENTION_DAYS,
            ),
            enable_audit_logging=data.get(
                SYS_CONFIG_ENABLE_AUDIT_LOGGING_KEY, SYS_DEFAULT_ENABLE_AUDIT_LOGGING
            ),
            # tts_integration=data.get(SYS_CONFIG_TTS_INTEGRATION_KEY),
            version=data.get(CONFIG_VERSION_KEY, 1),
        )


@dataclass(frozen=True)
class ConfigSnapshot:
    """Immutable snapshot of all configuration required for notification delivery decisions.

    Attributes
    ----------
    snapshot_id : str
        Unique identifier for this configuration snapshot.
    created_at : datetime
        Timestamp when snapshot was created.
    recipients : dict[str, RecipientData]
        Recipient identity data indexed by recipient ID.
    recipient_configs : dict[str, RecipientConfig]
        Recipient configuration indexed by recipient ID.
    system_config : SystemConfig
        System-wide configuration settings.
    channel_registry : ChannelRegistry
        Registry of available notification channels with metadata.

    """

    snapshot_id: str
    created_at: datetime
    recipients: dict[str, RecipientData]
    recipient_configs: dict[str, RecipientConfig]
    system_config: SystemConfig
    channel_registry: ChannelRegistry

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
            # Convert allowed_criticalities and allowed_types from strings to enums
            allowed_criticalities = []
            if recipient_config.dnd_allowed_criticalities:
                allowed_criticalities = [
                    NotificationCriticality(c) if isinstance(c, str) else c
                    for c in recipient_config.dnd_allowed_criticalities
                ]

            allowed_types = []
            if recipient_config.dnd_allowed_types:
                allowed_types = [
                    NotificationType(t) if isinstance(t, str) else t
                    for t in recipient_config.dnd_allowed_types
                ]

            dnd = DoNotDisturbConfig(
                start=datetime.strptime(recipient_config.dnd_start, "%H:%M:%S").time()
                if recipient_config.dnd_start
                else None,
                end=datetime.strptime(recipient_config.dnd_end, "%H:%M:%S").time()
                if recipient_config.dnd_end
                else None,
                allowed_sources_regex=recipient_config.dnd_allowed_sources_regex,
                allowed_criticalities=allowed_criticalities
                if allowed_criticalities
                else None,
                allowed_types=allowed_types if allowed_types else None,
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
