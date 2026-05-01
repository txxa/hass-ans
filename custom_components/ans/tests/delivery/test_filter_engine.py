"""Tests for the ANS FilterEngine."""

from __future__ import annotations

from datetime import UTC, datetime, time

from custom_components.ans.delivery.filter_engine import FilterEngine
from custom_components.ans.models import (
    DoNotDisturbConfig,
    FilterDecisionType,
    FilterReason,
    NotificationCriticality,
    NotificationType,
)

from ..conftest import make_dnd, make_payload, make_policy, make_task

ENGINE = FilterEngine()

# ── helpers ──────────────────────────────────────────────────────────────────


def _now(hour: int, minute: int = 0) -> datetime:
    """Return a UTC-aware datetime today at the given hour:minute."""
    return datetime(2026, 1, 1, hour, minute, tzinfo=UTC)


# ── 1. Type allow-list ────────────────────────────────────────────────────────


class TestTypeFilter:
    """Verify that the type allow-list filter correctly permits or blocks notifications based on their NotificationType."""

    def test_allowed_when_type_in_list(self):
        """A notification whose type is in allowed_types passes the filter."""
        task = make_task(
            payload=make_payload(type=NotificationType.INFO),
            policy=make_policy(allowed_types=[NotificationType.INFO]),
        )
        decision = ENGINE.evaluate(task, _now(10))
        assert decision.decision == FilterDecisionType.ALLOWED
        assert decision.reason == FilterReason.NORMAL

    def test_filtered_when_type_not_in_list(self):
        """A notification whose type is absent from allowed_types is blocked with TYPE_NOT_ALLOWED reason."""
        task = make_task(
            payload=make_payload(type=NotificationType.ALERT),
            policy=make_policy(allowed_types=[NotificationType.INFO]),
        )
        decision = ENGINE.evaluate(task, _now(10))
        assert decision.decision == FilterDecisionType.FILTERED
        assert decision.reason == FilterReason.TYPE_NOT_ALLOWED

    def test_allowed_when_type_list_empty(self):
        """Empty allowed_types = no type restriction."""
        task = make_task(
            payload=make_payload(type=NotificationType.SECURITY),
            policy=make_policy(allowed_types=[]),
        )
        decision = ENGINE.evaluate(task, _now(10))
        assert decision.decision == FilterDecisionType.ALLOWED

    def test_details_populated_on_filter(self):
        """When a notification is filtered, decision.details contains a 'type' key describing the blocked type."""
        task = make_task(
            payload=make_payload(type=NotificationType.REMINDER),
            policy=make_policy(allowed_types=[NotificationType.ALERT]),
        )
        decision = ENGINE.evaluate(task, _now(10))
        assert decision.details is not None
        assert "type" in decision.details

    def test_all_types_in_list_all_allowed(self):
        """When every NotificationType is in allowed_types, every notification passes the filter."""
        for nt in NotificationType:
            task = make_task(
                payload=make_payload(type=nt),
                policy=make_policy(allowed_types=list(NotificationType)),
            )
            assert (
                ENGINE.evaluate(task, _now(10)).decision == FilterDecisionType.ALLOWED
            )


# ── 2. Blocked source regex ───────────────────────────────────────────────────


class TestSourceBlockFilter:
    """Verify that the source-block regex filter correctly permits or blocks notifications based on their source field."""

    def test_source_blocked_by_pattern(self):
        """A notification whose source matches blocked_sources_regex is blocked with SOURCE_BLOCKED reason."""
        task = make_task(
            payload=make_payload(source="automation.morning_alert"),
            policy=make_policy(blocked_sources_regex="^automation\\..*"),
        )
        decision = ENGINE.evaluate(task, _now(10))
        assert decision.decision == FilterDecisionType.FILTERED
        assert decision.reason == FilterReason.SOURCE_BLOCKED

    def test_source_not_blocked_when_no_match(self):
        """A notification whose source does not match blocked_sources_regex is allowed."""
        task = make_task(
            payload=make_payload(source="script.important"),
            policy=make_policy(blocked_sources_regex="^automation\\..*"),
        )
        decision = ENGINE.evaluate(task, _now(10))
        assert decision.decision == FilterDecisionType.ALLOWED

    def test_source_not_blocked_when_no_pattern(self):
        """When blocked_sources_regex is None, no source is ever blocked."""
        task = make_task(
            payload=make_payload(source="automation.foo"),
            policy=make_policy(blocked_sources_regex=None),
        )
        decision = ENGINE.evaluate(task, _now(10))
        assert decision.decision == FilterDecisionType.ALLOWED

    def test_details_populated_on_block(self):
        """When a source is blocked, decision.details contains a 'pattern' key showing the matched regex."""
        task = make_task(
            payload=make_payload(source="automation.noisy"),
            policy=make_policy(blocked_sources_regex="^automation\\..*"),
        )
        decision = ENGINE.evaluate(task, _now(10))
        assert decision.details is not None
        assert "pattern" in decision.details

    def test_type_filter_takes_priority_over_source_block(self):
        """Type mismatch must be returned BEFORE source block is evaluated."""
        task = make_task(
            payload=make_payload(
                type=NotificationType.ALERT,
                source="automation.noisy",
            ),
            policy=make_policy(
                allowed_types=[NotificationType.INFO],
                blocked_sources_regex="^automation\\..*",
            ),
        )
        decision = ENGINE.evaluate(task, _now(10))
        assert decision.reason == FilterReason.TYPE_NOT_ALLOWED


# ── 3. Do Not Disturb ─────────────────────────────────────────────────────────


class TestDNDFilter:
    """DND evaluation is the third step after type and source checks."""

    def test_dnd_active_filters_notification(self):
        """A notification sent during an active DND window is blocked with DND_ACTIVE reason."""
        dnd = make_dnd("22:00", "07:00")  # midnight-crossing
        task = make_task(
            payload=make_payload(source="other"),
            policy=make_policy(dnd=dnd),
        )
        decision = ENGINE.evaluate(task, _now(23))  # inside DND
        assert decision.decision == FilterDecisionType.FILTERED
        assert decision.reason == FilterReason.DND_ACTIVE

    def test_dnd_inactive_allows_notification(self):
        """A notification sent outside the active DND window is allowed."""
        dnd = make_dnd("22:00", "07:00")  # midnight-crossing
        task = make_task(
            payload=make_payload(source="other"),
            policy=make_policy(dnd=dnd),
        )
        decision = ENGINE.evaluate(task, _now(10))  # outside DND
        assert decision.decision == FilterDecisionType.ALLOWED

    def test_dnd_simple_window_active(self):
        """A notification sent inside a simple (non-midnight-crossing) DND window is blocked."""
        dnd = make_dnd("08:00", "09:00")  # simple window
        task = make_task(policy=make_policy(dnd=dnd))
        decision = ENGINE.evaluate(task, _now(8, 30))
        assert decision.decision == FilterDecisionType.FILTERED

    def test_dnd_simple_window_inactive_before(self):
        """A notification sent one minute before the DND window opens is allowed."""
        dnd = make_dnd("08:00", "09:00")
        task = make_task(policy=make_policy(dnd=dnd))
        decision = ENGINE.evaluate(task, _now(7, 59))
        assert decision.decision == FilterDecisionType.ALLOWED

    def test_dnd_simple_window_inactive_at_end(self):
        """End time is exclusive."""
        dnd = make_dnd("08:00", "09:00")
        task = make_task(policy=make_policy(dnd=dnd))
        decision = ENGINE.evaluate(task, _now(9, 0))
        assert decision.decision == FilterDecisionType.ALLOWED

    def test_no_dnd_allows_all(self):
        """When no DND config is attached to the policy, all notifications pass through."""
        task = make_task(policy=make_policy(dnd=None))
        assert ENGINE.evaluate(task, _now(23)).decision == FilterDecisionType.ALLOWED

    def test_dnd_details_populated(self):
        """When DND blocks a notification, decision.details contains current_time, dnd_start, and dnd_end keys."""
        dnd = make_dnd("22:00", "07:00")
        task = make_task(policy=make_policy(dnd=dnd))
        decision = ENGINE.evaluate(task, _now(23))
        assert decision.details is not None
        assert "current_time" in decision.details
        assert "dnd_start" in decision.details
        assert "dnd_end" in decision.details

    def test_naive_datetime_treated_as_utc(self):
        """A naive datetime (tzinfo=None) is treated as UTC; DND evaluation proceeds correctly."""
        dnd = make_dnd("22:00", "07:00")  # midnight-crossing; 23:00 is inside
        task = make_task(
            payload=make_payload(source="other"),
            policy=make_policy(dnd=dnd),
        )
        naive_now = datetime(2026, 1, 1, 23, 0, 0)  # no tzinfo
        decision = ENGINE.evaluate(task, naive_now)
        # 23:00 UTC is within the 22:00–07:00 DND window — should be filtered
        assert decision.decision == FilterDecisionType.FILTERED
        assert decision.reason == FilterReason.DND_ACTIVE


# ── 4. DND bypass rules ───────────────────────────────────────────────────────


class TestDNDBypass:
    """DND can be bypassed by source regex, criticality, or type allow-lists."""

    def _active_dnd_task(self, **policy_overrides):
        """Return a task inside an active midnight-crossing DND window; override any DoNotDisturbConfig field via policy_overrides."""
        dnd_kwargs = {
            "start": time(22, 0),
            "end": time(7, 0),
            "allowed_sources_regex": None,
            "allowed_criticalities": None,
            "allowed_types": None,
        }
        dnd_kwargs.update(policy_overrides)
        dnd = DoNotDisturbConfig(**dnd_kwargs)
        return make_task(
            payload=make_payload(
                source="trusted.source",
                criticality=NotificationCriticality.CRITICAL,
                type=NotificationType.SECURITY,
            ),
            policy=make_policy(dnd=dnd),
        )

    def test_bypass_by_source_regex(self):
        """A notification from a source matching allowed_sources_regex bypasses DND."""
        task = self._active_dnd_task(allowed_sources_regex=r"^trusted\.")
        decision = ENGINE.evaluate(task, _now(23))
        assert decision.decision == FilterDecisionType.ALLOWED
        assert decision.reason == FilterReason.DND_BYPASS

    def test_bypass_by_criticality(self):
        """A notification with a criticality listed in DND's allowed_criticalities bypasses the DND window."""
        task = self._active_dnd_task(
            allowed_criticalities=[NotificationCriticality.CRITICAL]
        )
        decision = ENGINE.evaluate(task, _now(23))
        assert decision.decision == FilterDecisionType.ALLOWED
        assert decision.reason == FilterReason.DND_BYPASS

    def test_bypass_by_type(self):
        """A notification with a type listed in DND's allowed_types bypasses the DND window."""
        task = self._active_dnd_task(allowed_types=[NotificationType.SECURITY])
        decision = ENGINE.evaluate(task, _now(23))
        assert decision.decision == FilterDecisionType.ALLOWED
        assert decision.reason == FilterReason.DND_BYPASS

    def test_no_bypass_when_source_no_match(self):
        """DND is not bypassed when the notification source does not match allowed_sources_regex."""
        task = self._active_dnd_task(allowed_sources_regex=r"^critical_only\.")
        decision = ENGINE.evaluate(task, _now(23))
        assert decision.decision == FilterDecisionType.FILTERED

    def test_no_bypass_when_criticality_not_in_list(self):
        """DND is not bypassed when the notification criticality is absent from allowed_criticalities."""
        task = make_task(
            payload=make_payload(criticality=NotificationCriticality.LOW),
            policy=make_policy(
                dnd=DoNotDisturbConfig(
                    start=time(22, 0),
                    end=time(7, 0),
                    allowed_sources_regex=None,
                    allowed_criticalities=[NotificationCriticality.CRITICAL],
                )
            ),
        )
        decision = ENGINE.evaluate(task, _now(23))
        assert decision.decision == FilterDecisionType.FILTERED

    def test_type_block_takes_priority_before_dnd_bypass(self):
        """Type filter is evaluated BEFORE DND (step 1 < step 3)."""
        dnd = DoNotDisturbConfig(
            start=time(0, 0),
            end=time(23, 59),
            allowed_sources_regex=r".*",  # would bypass DND
        )
        task = make_task(
            payload=make_payload(type=NotificationType.REMINDER),
            policy=make_policy(
                allowed_types=[NotificationType.INFO],  # blocks
                dnd=dnd,
            ),
        )
        decision = ENGINE.evaluate(task, _now(12))
        assert decision.reason == FilterReason.TYPE_NOT_ALLOWED


# ── 5. _is_dnd_active helper ──────────────────────────────────────────────────


class TestIsDNDActive:
    """Unit-test the static helper for time-window logic."""

    def _check(self, start: str, end: str, current: str) -> bool:
        """Invoke FilterEngine._is_dnd_active with ISO-time strings for convenience."""
        return FilterEngine._is_dnd_active(
            time.fromisoformat(current),
            time.fromisoformat(start),
            time.fromisoformat(end),
        )

    # Simple (non-midnight-crossing) windows
    def test_simple_inside(self):
        """A time strictly inside a non-midnight-crossing window is active."""
        assert self._check("22:00", "23:00", "22:30") is True

    def test_simple_at_start(self):
        """The start boundary of a simple window is inclusive."""
        assert self._check("22:00", "23:00", "22:00") is True

    def test_simple_at_end_exclusive(self):
        """The end boundary of a simple (non-midnight-crossing) window is exclusive."""
        assert self._check("22:00", "23:00", "23:00") is False

    def test_simple_before(self):
        """A time before a simple window opens is not active."""
        assert self._check("22:00", "23:00", "21:59") is False

    def test_simple_after(self):
        """A time after a simple window closes is not active."""
        assert self._check("22:00", "23:00", "23:01") is False

    # Midnight-crossing windows
    def test_midnight_crossing_inside_evening(self):
        """A time in the evening portion (before midnight) of a midnight-crossing window is active."""
        assert self._check("22:00", "07:00", "23:00") is True

    def test_midnight_crossing_inside_morning(self):
        """A time in the morning portion (after midnight) of a midnight-crossing window is active."""
        assert self._check("22:00", "07:00", "06:00") is True

    def test_midnight_crossing_at_start(self):
        """The start boundary of a midnight-crossing window is inclusive."""
        assert self._check("22:00", "07:00", "22:00") is True

    def test_midnight_crossing_at_end_exclusive(self):
        """The end boundary of a midnight-crossing window is exclusive."""
        assert self._check("22:00", "07:00", "07:00") is False

    def test_midnight_crossing_outside_midday(self):
        """A midday time is outside a midnight-crossing window (22:00–07:00)."""
        assert self._check("22:00", "07:00", "12:00") is False
