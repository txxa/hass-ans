"""Retry scheduling and backoff policy evaluation.

Provides retry decision logic with exponential backoff, configurable retry
attempts, and rate limit handling for notification delivery tasks.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class RetryReason(str, Enum):
    """Reason for scheduling a retry.

    Values
    ------
    RATE_LIMITED : str
        Retry needed because recipient/channel rate limit exceeded.
    TRANSIENT_FAILURE : str
        Retry needed because of transient delivery failure.

    """

    RATE_LIMITED = "RATE_LIMITED"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"


@dataclass(frozen=True)
class RetryDecision:
    """Decision for whether and when to retry a task.

    Attributes
    ----------
    should_retry : bool
        Whether the task should be retried.
    next_run_at : datetime | None
        When to run the retry (None if should_retry is False).
    reason : RetryReason | None
        Why the retry is scheduled (None if should_retry is False).

    """

    should_retry: bool
    next_run_at: datetime | None
    reason: RetryReason | None


class RetryPolicy:
    """Pure retry/backoff policy.

    No persistence, no sleeping, no async primitives.
    """

    def __init__(
        self,
        *,
        max_attempts: int,
        base_delay: timedelta,
        backoff_factor: float = 2.0,
        max_delay: timedelta | None = None,
    ) -> None:
        """Initialize retry policy with exponential backoff parameters.

        Parameters
        ----------
        max_attempts : int
            Maximum number of delivery attempts.
        base_delay : timedelta
            Initial delay before first retry.
        backoff_factor : float, optional
            Exponential backoff multiplier, by default 2.0.
        max_delay : timedelta | None, optional
            Maximum delay between retries, by default None (no cap).

        """
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._backoff_factor = backoff_factor
        self._max_delay = max_delay

    def evaluate(
        self,
        *,
        attempt_number: int,
        reason: RetryReason,
        now: datetime,
    ) -> RetryDecision:
        """Evaluate whether and when to retry a task.

        Parameters
        ----------
        attempt_number : int
            Current attempt number (1-indexed).
        reason : RetryReason
            Reason for the retry.
        now : datetime
            Current timestamp.

        Returns
        -------
        RetryDecision
            Decision with retry timing and reason.

        """
        if attempt_number >= self._max_attempts:
            return RetryDecision(False, None, None)

        delay = self._base_delay * (self._backoff_factor ** (attempt_number - 1))
        if self._max_delay:
            delay = min(delay, self._max_delay)

        return RetryDecision(
            should_retry=True,
            next_run_at=now + delay,
            reason=reason,
        )
