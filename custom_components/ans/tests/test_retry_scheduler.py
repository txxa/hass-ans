"""Tests for the ANS retry scheduler / RetryPolicy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.ans.delivery.retry_scheduler import (
    RetryDecision,
    RetryPolicy,
    RetryReason,
)

# ── helpers ───────────────────────────────────────────────────────────────────

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _policy(
    *,
    max_attempts: int = 5,
    base_delay_s: int = 60,
    backoff_factor: float = 2.0,
    max_delay_s: int | None = None,
) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=max_attempts,
        base_delay=timedelta(seconds=base_delay_s),
        backoff_factor=backoff_factor,
        max_delay=timedelta(seconds=max_delay_s) if max_delay_s else None,
    )


# ── Basic retry logic ─────────────────────────────────────────────────────────


class TestRetryPolicyBasic:
    def test_should_retry_before_max_attempts(self):
        policy = _policy(max_attempts=3)
        decision = policy.evaluate(
            attempt_number=1, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        assert decision.should_retry is True
        assert decision.next_run_at is not None
        assert decision.reason == RetryReason.TRANSIENT_FAILURE

    def test_no_retry_at_max_attempts(self):
        policy = _policy(max_attempts=3)
        decision = policy.evaluate(
            attempt_number=3, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        assert decision.should_retry is False
        assert decision.next_run_at is None
        assert decision.reason is None

    def test_no_retry_beyond_max_attempts(self):
        policy = _policy(max_attempts=3)
        decision = policy.evaluate(
            attempt_number=5, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        assert decision.should_retry is False

    def test_next_run_at_in_future(self):
        policy = _policy(max_attempts=5, base_delay_s=30)
        decision = policy.evaluate(
            attempt_number=1, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        assert decision.next_run_at > NOW

    def test_reason_preserved_in_decision(self):
        policy = _policy()
        for reason in RetryReason:
            d = policy.evaluate(attempt_number=1, reason=reason, now=NOW)
            assert d.reason == reason


# ── Exponential backoff ───────────────────────────────────────────────────────


class TestExponentialBackoff:
    def test_first_attempt_delay_equals_base(self):
        base = 60
        policy = _policy(base_delay_s=base, backoff_factor=2.0)
        d = policy.evaluate(
            attempt_number=1, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        expected = NOW + timedelta(seconds=base)
        assert d.next_run_at == pytest.approx(expected, abs=timedelta(seconds=1))

    def test_second_attempt_delay_is_doubled(self):
        base = 60
        policy = _policy(base_delay_s=base, backoff_factor=2.0)
        d = policy.evaluate(
            attempt_number=2, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        # delay = base * 2^(2-1) = 60 * 2 = 120
        expected = NOW + timedelta(seconds=120)
        assert d.next_run_at == pytest.approx(expected, abs=timedelta(seconds=1))

    def test_third_attempt_delay_quadrupled(self):
        base = 60
        policy = _policy(base_delay_s=base, backoff_factor=2.0)
        d = policy.evaluate(
            attempt_number=3, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        # delay = base * 2^2 = 240
        expected = NOW + timedelta(seconds=240)
        assert d.next_run_at == pytest.approx(expected, abs=timedelta(seconds=1))

    def test_delays_are_strictly_increasing(self):
        policy = _policy(base_delay_s=10, backoff_factor=2.0)
        delays = []
        for attempt in range(1, 5):
            d = policy.evaluate(
                attempt_number=attempt, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
            )
            delays.append(d.next_run_at)
        for i in range(1, len(delays)):
            assert delays[i] > delays[i - 1]

    def test_backoff_factor_1_gives_constant_delay(self):
        base = 30
        policy = _policy(base_delay_s=base, backoff_factor=1.0)
        for attempt in range(1, 4):
            d = policy.evaluate(
                attempt_number=attempt, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
            )
            expected = NOW + timedelta(seconds=base)
            assert d.next_run_at == pytest.approx(expected, abs=timedelta(seconds=1))


# ── Max delay cap ─────────────────────────────────────────────────────────────


class TestMaxDelayCap:
    def test_delay_capped_at_max(self):
        policy = _policy(base_delay_s=60, backoff_factor=2.0, max_delay_s=120)
        # attempt 3 without cap: 60 * 4 = 240s → should be capped at 120
        d = policy.evaluate(
            attempt_number=3, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        expected = NOW + timedelta(seconds=120)
        assert d.next_run_at == pytest.approx(expected, abs=timedelta(seconds=1))

    def test_delay_below_cap_not_affected(self):
        policy = _policy(base_delay_s=60, backoff_factor=2.0, max_delay_s=999)
        d1 = policy.evaluate(
            attempt_number=1, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        # First attempt = 60s < 999s cap, should not be capped
        expected = NOW + timedelta(seconds=60)
        assert d1.next_run_at == pytest.approx(expected, abs=timedelta(seconds=1))

    def test_no_cap_when_max_delay_none(self):
        policy = _policy(base_delay_s=60, backoff_factor=2.0, max_delay_s=None)
        # attempt 4: 60 * 2^3 = 480s — no cap applied
        d = policy.evaluate(
            attempt_number=4, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        expected = NOW + timedelta(seconds=480)
        assert d.next_run_at == pytest.approx(expected, abs=timedelta(seconds=1))


# ── Rate-limited retries ──────────────────────────────────────────────────────


class TestRateLimitedRetry:
    def test_rate_limited_reason_preserved(self):
        policy = _policy()
        d = policy.evaluate(attempt_number=1, reason=RetryReason.RATE_LIMITED, now=NOW)
        assert d.should_retry is True
        assert d.reason == RetryReason.RATE_LIMITED

    def test_rate_limited_also_reaches_max_attempts(self):
        policy = _policy(max_attempts=2)
        d = policy.evaluate(attempt_number=2, reason=RetryReason.RATE_LIMITED, now=NOW)
        assert d.should_retry is False


# ── RetryDecision frozen ──────────────────────────────────────────────────────


class TestRetryDecisionFrozen:
    def test_retry_decision_is_frozen(self):
        d = RetryDecision(
            should_retry=True, next_run_at=NOW, reason=RetryReason.TRANSIENT_FAILURE
        )
        with pytest.raises((AttributeError, TypeError)):
            d.should_retry = False  # type: ignore[misc]
