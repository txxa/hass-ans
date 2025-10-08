"""Rate limiting for notification delivery.

Implements per-recipient-per-channel token bucket rate limiting
to prevent overwhelming delivery systems.
"""

import time
from dataclasses import dataclass


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

    Safe for asyncio concurrency within one HA instance.
    """

    def __init__(self) -> None:
        """Initialize the rate limiter."""
        self._buckets: dict[tuple[str, str], _TokenBucket] = {}

    def allow(self, task) -> bool:
        """Check if a notification delivery is allowed under rate limits.

        Args:
            task: NotificationDeliveryTask containing recipient_id, channel,
                  and policy with rate limit configuration.

        Returns:
            True if delivery is allowed, False if rate limited.

        """
        # Extract rate limit from task policy
        rate_limit = task.policy.rate_limit
        rate_limit_window = task.policy.rate_limit_window

        # No rate limit configured → always allow
        if not rate_limit or rate_limit <= 0:
            return True

        key = (task.recipient_id, task.channel)
        now = time.monotonic()

        bucket = self._buckets.get(key)
        if not bucket:
            bucket = _TokenBucket(
                capacity=rate_limit,
                refill_rate=rate_limit / rate_limit_window,
                tokens=float(rate_limit),
                last_refill=now,
            )
            self._buckets[key] = bucket

        bucket.refill(now)
        return bucket.consume(1.0)
