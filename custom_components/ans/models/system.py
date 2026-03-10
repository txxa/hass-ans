"""System configuration and snapshot models for the ANS system."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..channels.channel_registry import ChannelRegistry
from ..const import (
    CONFIG_VERSION_KEY,
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
    SYS_DEFAULT_GLOBAL_RATE_LIMIT,
    SYS_DEFAULT_QUEUE_CONCURRENCY,
    SYS_DEFAULT_RATE_LIMIT_WINDOW,
    SYS_DEFAULT_RETRY_BACKOFF_FACTOR,
    SYS_DEFAULT_RETRY_BASE_DELAY_SECONDS,
    SYS_DEFAULT_RETRY_MAX_DELAY_SECONDS,
    SYS_STORAGE_DEFAULT_FILE_RETENTION_DAYS,
)
from .notification import NotificationCriticality, NotificationType
from .policy import DoNotDisturbConfig, RecipientNotificationPolicy
from .recipient import RecipientConfig, RecipientContactInfo, RecipientData


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
    tts_service: str | None = (
        None  # NEW: TTS service (e.g., "tts.google_translate_say")
    )
    version: int = field(default=1)

    def __post_init__(self) -> None:
        """Validate system configuration.

        Raises
        ------
        ValueError
            If system configuration is invalid.

        """
        from ..config.validator import ConfigValidator  # noqa: PLC0415

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
        data[SYS_CONFIG_TTS_SERVICE_KEY] = self.tts_service
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
            tts_service=data.get(SYS_CONFIG_TTS_SERVICE_KEY),
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
            case _:
                return set()  # Return empty set for unknown criticality

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
            # from .notification import NotificationCriticality, NotificationType  # noqa: PLC0415

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
