"""Notification primitives for the ANS system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class NotificationType(StrEnum):
    """Types of notifications."""

    INFO = "INFO"
    WARNING = "WARNING"
    ALERT = "ALERT"
    REMINDER = "REMINDER"
    EVENT = "EVENT"
    SECURITY = "SECURITY"


class NotificationCriticality(StrEnum):
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
    # --- optional payload fields ---
    # metadata:     user-supplied semantic data (entity_id, camera name, …)
    # actions:      interactive response options — notification content consumed by
    #               capable adapters (e.g. MobileAppDeliveryAdapter); ignored by others
    # channel_data: adapter-specific delivery directives keyed by adapter type
    #               (e.g. {"mobile_app": {"tag": "..."}}); reserved for future items
    #               such as NH-3 (acknowledgement tracking) — not yet in service schema
    metadata: dict[str, Any] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    channel_data: dict[str, Any] = field(default_factory=dict)
