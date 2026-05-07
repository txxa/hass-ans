"""Data models for the ANS integration.

This module re-exports all models for backward compatibility.
Existing imports like `from .models import NotificationPayload` will continue to work.
"""

from .channel import ChannelInfo, ChannelScope
from .delivery import (
    Attempt,
    DeliveryResult,
    DeliveryState,
    DeliveryStatus,
    NotificationDeliveryTask,
    RetrySchedule,
    TaskOutcome,
)
from .notification import (
    NotificationCriticality,
    NotificationPayload,
    NotificationType,
)
from .policy import (
    DoNotDisturbConfig,
    FilterDecision,
    FilterDecisionType,
    FilterReason,
    RecipientNotificationPolicy,
)
from .recipient import (
    RecipientConfig,
    RecipientContactInfo,
    RecipientData,
    RecipientType,
)
from .system import ConfigSnapshot, IntegrationInfo, SystemConfig

__all__ = [
    # Channel
    "ChannelInfo",
    "ChannelScope",
    # Delivery
    "Attempt",
    "DeliveryResult",
    "DeliveryState",
    "DeliveryStatus",
    "NotificationDeliveryTask",
    "RetrySchedule",
    "TaskOutcome",
    # Notification
    "NotificationCriticality",
    "NotificationPayload",
    "NotificationType",
    # Policy
    "DoNotDisturbConfig",
    "FilterDecision",
    "FilterDecisionType",
    "FilterReason",
    "RecipientNotificationPolicy",
    # Recipient
    "RecipientConfig",
    "RecipientContactInfo",
    "RecipientData",
    "RecipientType",
    # System
    "ConfigSnapshot",
    "IntegrationInfo",
    "SystemConfig",
]
