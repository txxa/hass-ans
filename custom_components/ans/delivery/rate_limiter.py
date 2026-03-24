"""Rate limiting for notification delivery.

Implements per-recipient token bucket rate limiting combined with
a global system-wide limit to prevent overwhelming delivery systems.
"""

import logging
import time
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)


@dataclass
class _TokenBucket:
    capacity: int
    refill_rate: float  # tokens per second
    tokens: float
    last_refill: float

    def refill(self, now: float) -> None:
        elapsed = now - self.last_refill
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def consume(self, amount: float = 1.0) -> bool:
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False


class RateLimiter:
    """Snapshot‑bound, in‑memory token bucket rate limiter.

    Supports both global and per-recipient rate limiting.
    All channels for a recipient share the same rate limit.
    Safe for asyncio concurrency within one HA instance.
    """

    def __init__(
        self,
        *,
        global_rate_limit: int | None = None,
        rate_limit_window: int | None = None,
    ) -> None:
        """Initialize the rate limiter.

        Args:
            global_rate_limit: Global rate limit (notifications/window).
                               If None or <= 0, global limiting is disabled.
            rate_limit_window: Time window in seconds for rate limiting.
                               Used for both global and recipient limits.

        """
        self._buckets: dict[str, _TokenBucket] = {}
        self._global_bucket: _TokenBucket | None = None

        # Initialize global bucket if configured
        if (
            global_rate_limit
            and global_rate_limit > 0
            and rate_limit_window
            and rate_limit_window > 0
        ):
            now = time.monotonic()
            self._global_bucket = _TokenBucket(
                capacity=global_rate_limit,
                refill_rate=global_rate_limit / rate_limit_window,
                tokens=float(global_rate_limit),
                last_refill=now,
            )
        self._rate_limit_window = rate_limit_window or 60

    def update_limits(
        self,
        *,
        global_rate_limit: int | None = None,
        rate_limit_window: int | None = None,
    ) -> None:
        """Update global rate limiting configuration.

        Args:
            global_rate_limit: New global rate limit (notifications/window).
                               If None or <= 0, global limiting is disabled.
            rate_limit_window: New time window in seconds for rate limiting.

        """
        now = time.monotonic()

        # Update rate limit window
        if rate_limit_window and rate_limit_window > 0:
            self._rate_limit_window = rate_limit_window

        # Update or create global bucket
        if global_rate_limit and global_rate_limit > 0 and self._rate_limit_window > 0:
            if self._global_bucket:
                # Update existing bucket's capacity and refill rate
                self._global_bucket.capacity = global_rate_limit
                self._global_bucket.refill_rate = (
                    global_rate_limit / self._rate_limit_window
                )
                # Cap current tokens to new capacity
                self._global_bucket.tokens = min(
                    global_rate_limit, self._global_bucket.tokens
                )
            else:
                # Create new global bucket
                self._global_bucket = _TokenBucket(
                    capacity=global_rate_limit,
                    refill_rate=global_rate_limit / self._rate_limit_window,
                    tokens=float(global_rate_limit),
                    last_refill=now,
                )
        else:
            # Disable global limiting
            self._global_bucket = None

    def allow(self, task) -> tuple[bool, str | None]:
        """Check if a notification delivery is allowed under rate limits.

        Checks both global and recipient-level rate limits.
        Both must allow the delivery for it to be permitted.

        Args:
            task: NotificationDeliveryTask containing recipient_id, channel,
                  and policy with rate limit configuration.

        Returns:
            Tuple of (allowed: bool, limit_type: str | None).
            If allowed is True, limit_type is None.
            If allowed is False, limit_type is "GLOBAL" or "RECIPIENT".

        """
        now = time.monotonic()

        # Check global rate limit first
        if self._global_bucket:
            self._global_bucket.refill(now)
            if not self._global_bucket.consume(1.0):
                # Restore the token since we're not allowing this delivery
                self._global_bucket.tokens += 1.0
                _LOGGER.debug(
                    "Global rate bucket exhausted for recipient '%s': "
                    "tokens=%.2f capacity=%d refill_rate=%.3f/s",
                    task.recipient_id,
                    self._global_bucket.tokens,
                    self._global_bucket.capacity,
                    self._global_bucket.refill_rate,
                )
                return False, "GLOBAL"

        # Check recipient-level rate limit
        rate_limit = task.policy.rate_limit

        # No recipient rate limit configured → allowed (if global passed)
        if not rate_limit or rate_limit <= 0:
            return True, None

        key = task.recipient_id
        bucket = self._buckets.get(key)
        if not bucket:
            bucket = _TokenBucket(
                capacity=rate_limit,
                refill_rate=rate_limit / self._rate_limit_window,
                tokens=float(rate_limit),
                last_refill=now,
            )
            self._buckets[key] = bucket

        bucket.refill(now)
        if not bucket.consume(1.0):
            # Restore global token if we consumed it but recipient limit blocks
            if self._global_bucket:
                self._global_bucket.tokens += 1.0
            _LOGGER.debug(
                "Recipient '%s' rate bucket exhausted: "
                "tokens=%.2f capacity=%d window=%ds",
                task.recipient_id,
                bucket.tokens,
                bucket.capacity,
                self._rate_limit_window,
            )
            return False, "RECIPIENT"

        return True, None
