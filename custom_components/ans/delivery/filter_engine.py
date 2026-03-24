"""Notification filtering engine.

Evaluates notification eligibility based on recipient policies:
- type allow-lists
- source blocking
- do-not-disturb windows with bypass rules
"""

import logging
import re
from datetime import UTC, datetime, time

from ..models import (
    FilterDecision,
    FilterDecisionType,
    FilterReason,
    NotificationDeliveryTask,
)

_LOGGER = logging.getLogger(__name__)


class FilterEngine:
    """Evaluates whether a notification is allowed for a recipient *at this moment*.

    This class is intentionally stateless.
    """

    # -------------------------
    # Public API
    # -------------------------

    def evaluate(
        self,
        task: NotificationDeliveryTask,
        # task.payload: NotificationPayload,
        # task.policy: RecipientNotificationPolicy,
        now: datetime,
    ) -> FilterDecision:
        """Evaluate notification eligibility.

        Order of evaluation is a hard contract and must not change.
        """

        # ------------------------------------------------------------
        # 1. Type allow‑list
        # ------------------------------------------------------------
        if task.policy.allowed_types:
            if task.payload.type not in task.policy.allowed_types:
                _LOGGER.debug(
                    "FilterEngine: TYPE_NOT_ALLOWED notification_id=%s "
                    "channel_id=%s type=%s allowed_types=%s",
                    task.payload.notification_id,
                    task.channel_info.id,
                    task.payload.type,
                    task.policy.allowed_types,
                )
                return FilterDecision(
                    decision=FilterDecisionType.FILTERED,
                    reason=FilterReason.TYPE_NOT_ALLOWED,
                    details={
                        "type": str(task.payload.type),
                        "allowed_types": ",".join(
                            str(t) for t in task.policy.allowed_types
                        ),
                    },
                )

        # ------------------------------------------------------------
        # 2. Blocked source regex
        # ------------------------------------------------------------
        if task.policy.blocked_sources_regex and re.match(
            task.policy.blocked_sources_regex, task.payload.source
        ):
            _LOGGER.debug(
                "FilterEngine: SOURCE_BLOCKED notification_id=%s "
                "channel_id=%s source='%s' pattern='%s'",
                task.payload.notification_id,
                task.channel_info.id,
                task.payload.source,
                task.policy.blocked_sources_regex,
            )
            return FilterDecision(
                decision=FilterDecisionType.FILTERED,
                reason=FilterReason.SOURCE_BLOCKED,
                details={"pattern": task.policy.blocked_sources_regex},
            )

        # ------------------------------------------------------------
        # 3. Do Not Disturb
        # ------------------------------------------------------------
        if task.policy.dnd and task.policy.dnd.start and task.policy.dnd.end:
            # Get local time (assume UTC if no timezone info)
            if now.tzinfo is None:
                local_now = now.replace(tzinfo=UTC)
            else:
                local_now = now
            current = local_now.time()
            start = task.policy.dnd.start
            end = task.policy.dnd.end

            if self._is_dnd_active(current, start, end):
                # DND bypass for allowed sources
                if task.policy.dnd.allowed_sources_regex and re.match(
                    task.policy.dnd.allowed_sources_regex, task.payload.source
                ):
                    _LOGGER.debug(
                        "FilterEngine: DND_BYPASS(source) notification_id=%s "
                        "channel_id=%s source='%s' pattern='%s'",
                        task.payload.notification_id,
                        task.channel_info.id,
                        task.payload.source,
                        task.policy.dnd.allowed_sources_regex,
                    )
                    return FilterDecision(
                        decision=FilterDecisionType.ALLOWED,
                        reason=FilterReason.DND_BYPASS,
                    )

                # DND bypass for allowed criticalities
                if (
                    task.policy.dnd.allowed_criticalities
                    and task.payload.criticality
                    in task.policy.dnd.allowed_criticalities
                ):
                    _LOGGER.debug(
                        "FilterEngine: DND_BYPASS(criticality) notification_id=%s "
                        "channel_id=%s criticality=%s allowed=%s",
                        task.payload.notification_id,
                        task.channel_info.id,
                        task.payload.criticality,
                        task.policy.dnd.allowed_criticalities,
                    )
                    return FilterDecision(
                        decision=FilterDecisionType.ALLOWED,
                        reason=FilterReason.DND_BYPASS,
                    )

                # DND bypass for allowed types
                if (
                    task.policy.dnd.allowed_types
                    and task.payload.type in task.policy.dnd.allowed_types
                ):
                    _LOGGER.debug(
                        "FilterEngine: DND_BYPASS(type) notification_id=%s "
                        "channel_id=%s type=%s allowed=%s",
                        task.payload.notification_id,
                        task.channel_info.id,
                        task.payload.type,
                        task.policy.dnd.allowed_types,
                    )
                    return FilterDecision(
                        decision=FilterDecisionType.ALLOWED,
                        reason=FilterReason.DND_BYPASS,
                    )

                _LOGGER.debug(
                    "FilterEngine: DND_ACTIVE notification_id=%s "
                    "channel_id=%s source='%s' time=%s window=%s–%s "
                    "(no bypass matched)",
                    task.payload.notification_id,
                    task.channel_info.id,
                    task.payload.source,
                    current.isoformat(),
                    start.isoformat(),
                    end.isoformat(),
                )
                return FilterDecision(
                    decision=FilterDecisionType.FILTERED,
                    reason=FilterReason.DND_ACTIVE,
                    details={
                        "current_time": current.isoformat(),
                        "dnd_start": start.isoformat(),
                        "dnd_end": end.isoformat(),
                    },
                )

        # Allowed normally
        return FilterDecision(
            decision=FilterDecisionType.ALLOWED,
            reason=FilterReason.NORMAL,
        )

    # -------------------------
    # Helpers (pure)
    # -------------------------

    @staticmethod
    def _is_dnd_active(now: time, start: time, end: time) -> bool:
        """Determine whether the current local time is within a DND window.

        Handles windows that cross midnight.
        """

        if start <= end:
            # Simple window: e.g. 22:00 - 23:00
            return start <= now < end

        # Midnight-crossing window: e.g. 22:00 - 07:00
        return now >= start or now < end
