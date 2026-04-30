"""Tests for the ANS rate limiter."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from custom_components.ans.delivery.rate_limiter import RateLimiter

from .conftest import make_policy, make_task

# ── helpers ──────────────────────────────────────────────────────────────────

MOCK_TIME_PATH = "custom_components.ans.delivery.rate_limiter.time.monotonic"


def _limiter(*, global_limit: int = 0, window: int = 60) -> RateLimiter:
    """Return a RateLimiter with optional global rate limit."""
    return RateLimiter(global_rate_limit=global_limit, rate_limit_window=window)


# ── No limits configured ─────────────────────────────────────────────────────


class TestNoLimits:
    """Verify that a RateLimiter with no limits configured always allows every notification."""

    def test_always_allowed_when_no_limits(self):
        """With no global or per-recipient rate limits set, allow() always returns (True, None)."""
        rl = _limiter()
        task = make_task(policy=make_policy(rate_limit=0))
        allowed, reason = rl.allow(task)
        assert allowed is True
        assert reason is None

    def test_no_global_bucket_attribute(self):
        """With no global limit configured, the internal _global_bucket is None."""
        rl = _limiter()
        assert rl._global_bucket is None


# ── Global rate limit ─────────────────────────────────────────────────────────


class TestGlobalRateLimit:
    """Verify token-bucket behaviour of the global rate limit shared across all recipients."""

    def test_global_limit_allows_up_to_capacity(self):
        """Requests within the global token-bucket capacity are all allowed."""
        rl = _limiter(global_limit=3, window=60)
        task = make_task(policy=make_policy(rate_limit=0))
        for _ in range(3):
            allowed, reason = rl.allow(task)
            assert allowed is True

    def test_global_limit_blocks_when_exhausted(self):
        """Once the global token-bucket is exhausted, further requests are denied with reason 'GLOBAL'."""
        rl = _limiter(global_limit=2, window=60)
        task = make_task(policy=make_policy(rate_limit=0))
        rl.allow(task)
        rl.allow(task)
        allowed, reason = rl.allow(task)
        assert allowed is False
        assert reason == "GLOBAL"

    def test_global_token_restored_when_denied(self):
        """If global limit denies, the token must NOT be consumed."""
        rl = _limiter(global_limit=1, window=60)
        task = make_task(policy=make_policy(rate_limit=0))
        # Exhaust
        rl.allow(task)
        # Attempt 2 — should be denied and token NOT further consumed
        rl.allow(task)
        # Token count should be >= 0 (not negative)
        assert rl._global_bucket.tokens >= 0

    def test_global_refills_over_time(self):
        """The global token-bucket refills when simulated time advances past the window boundary."""
        with patch(MOCK_TIME_PATH) as mock_t:
            mock_t.return_value = 0.0
            rl = _limiter(global_limit=1, window=60)
            task = make_task(policy=make_policy(rate_limit=0))

            rl.allow(task)  # consume
            allowed, _ = rl.allow(task)
            assert allowed is False  # exhausted

            # Advance time by 61 seconds — bucket should refill
            mock_t.return_value = 61.0
            allowed, _ = rl.allow(task)
            assert allowed is True


# ── Per-recipient rate limit ──────────────────────────────────────────────────


class TestRecipientRateLimit:
    """Verify token-bucket behaviour of the per-recipient rate limit."""

    def test_recipient_allowed_within_limit(self):
        """Requests within the per-recipient token-bucket capacity are all allowed."""
        rl = _limiter()
        task = make_task(policy=make_policy(rate_limit=5, rate_limit_window=60))
        for _ in range(5):
            allowed, _ = rl.allow(task)
            assert allowed is True

    def test_recipient_blocked_when_exhausted(self):
        """Once the per-recipient bucket is exhausted, further requests are denied with reason 'RECIPIENT'."""
        rl = _limiter()
        task = make_task(policy=make_policy(rate_limit=2, rate_limit_window=60))
        rl.allow(task)
        rl.allow(task)
        allowed, reason = rl.allow(task)
        assert allowed is False
        assert reason == "RECIPIENT"

    def test_recipient_buckets_are_independent(self):
        """Each recipient has its own independent token bucket; exhausting one does not affect others."""
        rl = _limiter()
        task_a = make_task(
            recipient_id="alice",
            policy=make_policy(rate_limit=1, rate_limit_window=60),
        )
        task_b = make_task(
            recipient_id="bob",
            policy=make_policy(rate_limit=1, rate_limit_window=60),
        )
        # Exhaust alice
        rl.allow(task_a)
        a_allowed, _ = rl.allow(task_a)
        assert a_allowed is False

        # Bob should still be allowed
        b_allowed, _ = rl.allow(task_b)
        assert b_allowed is True

    def test_recipient_refills_over_time(self):
        """The per-recipient token-bucket refills when simulated time advances past the window boundary."""
        with patch(MOCK_TIME_PATH) as mock_t:
            mock_t.return_value = 0.0
            rl = _limiter()
            task = make_task(policy=make_policy(rate_limit=1, rate_limit_window=60))

            rl.allow(task)  # consume
            allowed, _ = rl.allow(task)
            assert allowed is False

            mock_t.return_value = 61.0
            allowed, _ = rl.allow(task)
            assert allowed is True


# ── Global takes priority over recipient ──────────────────────────────────────


class TestGlobalPriority:
    """Verify that the global bucket is evaluated before the per-recipient bucket on every request."""

    def test_global_checked_before_recipient(self):
        """When the global bucket is exhausted, the request is denied with 'GLOBAL' reason even if the recipient bucket has capacity."""
        rl = _limiter(global_limit=1, window=60)
        task = make_task(policy=make_policy(rate_limit=100, rate_limit_window=60))
        # Exhaust global
        rl.allow(task)
        allowed, reason = rl.allow(task)
        assert allowed is False
        assert reason == "GLOBAL"

    def test_global_token_restored_on_recipient_block(self):
        """If recipient bucket blocks, global token must be restored."""
        rl = _limiter(global_limit=10, window=60)
        task = make_task(policy=make_policy(rate_limit=1, rate_limit_window=60))
        rl.allow(task)  # consume recipient token
        before_tokens = rl._global_bucket.tokens
        rl.allow(task)  # recipient blocks — global token should be restored
        after_tokens = rl._global_bucket.tokens
        # Global count should be the same (restored)
        assert after_tokens == pytest.approx(before_tokens)


# ── update_limits ─────────────────────────────────────────────────────────────


class TestUpdateLimits:
    """Verify that update_limits() can enable, disable, and reconfigure the global rate-limit bucket at runtime."""

    def test_enable_global_limit_after_init(self):
        """Calling update_limits() with a non-zero global_rate_limit creates the global token bucket."""
        rl = _limiter()
        assert rl._global_bucket is None
        rl.update_limits(global_rate_limit=10, rate_limit_window=60)
        assert rl._global_bucket is not None

    def test_disable_global_limit(self):
        """Calling update_limits() with global_rate_limit=0 removes the global token bucket."""
        rl = _limiter(global_limit=10, window=60)
        rl.update_limits(global_rate_limit=0)
        assert rl._global_bucket is None

    def test_update_preserves_refill_rate(self):
        """Calling update_limits() with a new capacity correctly updates the bucket's capacity."""
        rl = _limiter(global_limit=10, window=60)
        rl.update_limits(global_rate_limit=20, rate_limit_window=60)
        assert rl._global_bucket.capacity == 20
