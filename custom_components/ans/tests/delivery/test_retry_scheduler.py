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
    """Return a RetryPolicy built from the given parameters; defaults represent a reasonable exponential-backoff configuration."""
    return RetryPolicy(
        max_attempts=max_attempts,
        base_delay=timedelta(seconds=base_delay_s),
        backoff_factor=backoff_factor,
        max_delay=timedelta(seconds=max_delay_s) if max_delay_s else None,
    )


# ── Basic retry logic ─────────────────────────────────────────────────────────


class TestRetryPolicyBasic:
    """Verify the basic retry decision logic: should_retry flag, attempt-limit boundary, and reason propagation."""

    def test_should_retry_before_max_attempts(self):
        """evaluate() returns should_retry=True for any attempt number within max_attempts."""
        policy = _policy(max_attempts=3)
        decision = policy.evaluate(
            attempt_number=1, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        assert decision.should_retry is True
        assert decision.next_run_at is not None
        assert decision.reason == RetryReason.TRANSIENT_FAILURE

    def test_retry_allowed_at_max_attempts(self):
        """The max_attempts boundary is inclusive — the attempt equal to max_attempts is still retried."""
        policy = _policy(max_attempts=3)
        decision = policy.evaluate(
            attempt_number=3, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        assert decision.should_retry is True
        assert decision.next_run_at is not None

    def test_no_retry_after_max_attempts(self):
        """Once attempt_number exceeds max_attempts, should_retry is False and next_run_at is None."""
        policy = _policy(max_attempts=3)
        decision = policy.evaluate(
            attempt_number=4, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        assert decision.should_retry is False
        assert decision.next_run_at is None
        assert decision.reason is None

    def test_no_retry_beyond_max_attempts(self):
        """Attempt numbers well beyond max_attempts also return should_retry=False."""
        policy = _policy(max_attempts=3)
        decision = policy.evaluate(
            attempt_number=5, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        assert decision.should_retry is False

    def test_next_run_at_in_future(self):
        """next_run_at is always a datetime strictly after 'now' when a retry is allowed."""
        policy = _policy(max_attempts=5, base_delay_s=30)
        decision = policy.evaluate(
            attempt_number=1, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        assert decision.next_run_at > NOW

    def test_reason_preserved_in_decision(self):
        """The RetryReason passed to evaluate() is preserved unchanged in the returned RetryDecision."""
        policy = _policy()
        for reason in RetryReason:
            d = policy.evaluate(attempt_number=1, reason=reason, now=NOW)
            assert d.reason == reason


# ── Exponential backoff ───────────────────────────────────────────────────────


class TestExponentialBackoff:
    """Verify that retry delays grow exponentially: delay = base_delay * backoff_factor ^ (attempt_number - 1)."""

    def test_first_attempt_delay_equals_base(self):
        """The first retry delay equals base_delay (exponent is 0, so multiplier is 1)."""
        base = 60
        policy = _policy(base_delay_s=base, backoff_factor=2.0)
        d = policy.evaluate(
            attempt_number=1, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        expected = NOW + timedelta(seconds=base)
        assert d.next_run_at == pytest.approx(expected, abs=timedelta(seconds=1))

    def test_second_attempt_delay_is_doubled(self):
        """The second retry delay is base_delay × backoff_factor^1 (= 120 s for base=60, factor=2)."""
        base = 60
        policy = _policy(base_delay_s=base, backoff_factor=2.0)
        d = policy.evaluate(
            attempt_number=2, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        # delay = base * 2^(2-1) = 60 * 2 = 120
        expected = NOW + timedelta(seconds=120)
        assert d.next_run_at == pytest.approx(expected, abs=timedelta(seconds=1))

    def test_third_attempt_delay_quadrupled(self):
        """The third retry delay is base_delay × backoff_factor^2 (= 240 s for base=60, factor=2)."""
        base = 60
        policy = _policy(base_delay_s=base, backoff_factor=2.0)
        d = policy.evaluate(
            attempt_number=3, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        # delay = base * 2^2 = 240
        expected = NOW + timedelta(seconds=240)
        assert d.next_run_at == pytest.approx(expected, abs=timedelta(seconds=1))

    def test_delays_are_strictly_increasing(self):
        """With backoff_factor > 1, each successive retry is scheduled further in the future than the previous one."""
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
        """With backoff_factor=1.0, every retry is scheduled exactly base_delay after 'now' (no growth)."""
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
    """Verify that the optional max_delay cap prevents exponential delays from growing unbounded."""

    def test_delay_capped_at_max(self):
        """When the computed backoff delay exceeds max_delay, it is clamped to max_delay."""
        policy = _policy(base_delay_s=60, backoff_factor=2.0, max_delay_s=120)
        # attempt 3 without cap: 60 * 4 = 240s → should be capped at 120
        d = policy.evaluate(
            attempt_number=3, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        expected = NOW + timedelta(seconds=120)
        assert d.next_run_at == pytest.approx(expected, abs=timedelta(seconds=1))

    def test_delay_below_cap_not_affected(self):
        """When the computed delay is below max_delay, it is not modified."""
        policy = _policy(base_delay_s=60, backoff_factor=2.0, max_delay_s=999)
        d1 = policy.evaluate(
            attempt_number=1, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        # First attempt = 60s < 999s cap, should not be capped
        expected = NOW + timedelta(seconds=60)
        assert d1.next_run_at == pytest.approx(expected, abs=timedelta(seconds=1))

    def test_no_cap_when_max_delay_none(self):
        """When max_delay is None, delays grow unbounded with the backoff factor."""
        policy = _policy(base_delay_s=60, backoff_factor=2.0, max_delay_s=None)
        # attempt 4: 60 * 2^3 = 480s — no cap applied
        d = policy.evaluate(
            attempt_number=4, reason=RetryReason.TRANSIENT_FAILURE, now=NOW
        )
        expected = NOW + timedelta(seconds=480)
        assert d.next_run_at == pytest.approx(expected, abs=timedelta(seconds=1))


# ── Rate-limited retries ──────────────────────────────────────────────────────


class TestRateLimitedRetry:
    """Verify that RATE_LIMITED retries follow the same attempt-limit logic as transient failures."""

    def test_rate_limited_reason_preserved(self):
        """A RATE_LIMITED retry decision preserves the RATE_LIMITED reason in the returned RetryDecision."""
        policy = _policy()
        d = policy.evaluate(attempt_number=1, reason=RetryReason.RATE_LIMITED, now=NOW)
        assert d.should_retry is True
        assert d.reason == RetryReason.RATE_LIMITED

    def test_rate_limited_also_reaches_max_attempts(self):
        """RATE_LIMITED retries are also exhausted once attempt_number exceeds max_attempts."""
        policy = _policy(max_attempts=2)
        d = policy.evaluate(attempt_number=3, reason=RetryReason.RATE_LIMITED, now=NOW)
        assert d.should_retry is False


# ── RetryDecision frozen ──────────────────────────────────────────────────────


class TestRetryDecisionFrozen:
    """Verify that RetryDecision is immutable (frozen dataclass) and cannot be mutated after creation."""

    def test_retry_decision_is_frozen(self):
        """Attempting to mutate a RetryDecision field raises AttributeError or TypeError."""
        d = RetryDecision(
            should_retry=True, next_run_at=NOW, reason=RetryReason.TRANSIENT_FAILURE
        )
        with pytest.raises((AttributeError, TypeError)):
            d.should_retry = False  # type: ignore[misc]
