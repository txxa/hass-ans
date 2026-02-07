"""Filtering and policy models for the ANS system."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from enum import Enum

from .notification import NotificationCriticality, NotificationType


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

    Maps 1:1 to the FilterDecision state machine.
    """

    retry_attempts: int
    rate_limit: int
    rate_limit_window: int
    allowed_types: list[NotificationType]  # TODO: should be set
    blocked_sources_regex: str | None
    dnd: DoNotDisturbConfig | None = None
