"""Notification primitives for the ANS system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


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
