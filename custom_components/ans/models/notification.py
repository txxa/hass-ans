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
    # context:      semantic correlation data (entity_id, camera name, zone, …)
    #               never interpreted by delivery adapters — ANS-internal only
    # link:         URL to open when the notification is tapped (http/https only)
    # image:        URL or local HA path to an image attachment
    # video:        URL or local HA path to a video attachment
    # file:         URL or local HA path to a file attachment
    # Use context["entity"] for the subject entity (e.g. binary_sensor.front_door).
    # actions:      interactive response options — consumed by capable adapters
    #               (e.g. MobileAppDeliveryAdapter); ignored by others
    # channel_data: flat adapter-specific delivery overrides (tag, importance, …)
    #               each adapter reads only its known keys and ignores the rest
    context: dict[str, Any] = field(default_factory=dict)
    link: str | None = None
    image: str | None = None
    video: str | None = None
    file: str | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    channel_data: dict[str, Any] = field(default_factory=dict)
