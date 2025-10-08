"""Notification filtering engine.

Evaluates notification eligibility based on recipient policies:
- type allow-lists
- source blocking
- do-not-disturb windows with bypass rules
"""

import re
from datetime import UTC, datetime, time

from .models import (
    FilterDecision,
    FilterDecisionType,
    FilterReason,
    NotificationDeliveryTask,
)


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
                return FilterDecision(
                    decision=FilterDecisionType.FILTERED,
                    reason=FilterReason.TYPE_NOT_ALLOWED,
                )

        # ------------------------------------------------------------
        # 2. Blocked source regex
        # ------------------------------------------------------------
        if task.policy.blocked_sources_regex and re.match(
            task.policy.blocked_sources_regex, task.payload.source
        ):
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
                    return FilterDecision(
                        decision=FilterDecisionType.ALLOWED,
                        reason=FilterReason.DND_BYPASS,
                    )

                return FilterDecision(
                    decision=FilterDecisionType.FILTERED,
                    reason=FilterReason.DND_ACTIVE,
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
