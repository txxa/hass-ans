"""Persistence layer abstractions for delivery state and attempts."""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from .models import Attempt, DeliveryStatus


class DeliveryStateStore(ABC):
    """Abstracts delivery task state persistence.

    Persists:
    - Terminal states (filtered, success, permanent failure)
    - Retry scheduling
    """

    @abstractmethod
    async def load(self, job_id: UUID) -> "DeliveryState | None":
        """Load delivery state for a job.

        Args:
            job_id: Job identifier.

        Returns:
            DeliveryState if found, None otherwise.

        """

    @abstractmethod
    async def persist_filtered(self, job_id: UUID, reason: str | None = None) -> None:
        """Persist a filtered (terminal) state.

        Args:
            job_id: Job identifier.
            reason: Reason for filtering.

        """

    @abstractmethod
    async def persist_rate_limited(self, job_id: UUID) -> None:
        """Persist rate-limited state (retryable).

        Args:
            job_id: Job identifier.

        """

    @abstractmethod
    async def persist_success(self, job_id: UUID, attempt: Attempt) -> None:
        """Persist successful delivery state.

        Args:
            job_id: Job identifier.
            attempt: Successful attempt record.

        """

    @abstractmethod
    async def persist_transient_failure(self, job_id: UUID, attempt: Attempt) -> None:
        """Persist transient failure state (retryable).

        Args:
            job_id: Job identifier.
            attempt: Failed attempt record.

        """

    @abstractmethod
    async def persist_permanent_failure(
        self,
        job_id: UUID,
        attempt: Attempt | None = None,
        error: str | None = None,
    ) -> None:
        """Persist permanent failure state (terminal).

        Args:
            job_id: Job identifier.
            attempt: Failed attempt record (optional).
            error: Error message.

        """

    @abstractmethod
    async def schedule_retry(
        self,
        job_id: UUID,
        run_at: datetime,
        reason: str | None = None,
    ) -> None:
        """Schedule a retry for a failed delivery task.

        Args:
            job_id: Job identifier.
            run_at: When to retry.
            reason: Reason for retry.

        """

    @abstractmethod
    async def cleanup_completed(self, before: datetime) -> int:
        """Clean up old completed delivery records.

        Args:
            before: Delete records with timestamps before this datetime.

        Returns:
            Number of records deleted.

        """


class AttemptStore(ABC):
    """Abstracts delivery attempt persistence.

    Persists individual delivery attempts for idempotency and audit.
    """

    @abstractmethod
    async def create(self, attempt: Attempt) -> None:
        """Create a new attempt record.

        Args:
            attempt: Attempt to persist.

        """

    @abstractmethod
    async def update(self, attempt: Attempt) -> None:
        """Update an existing attempt record.

        Args:
            attempt: Attempt to update.

        """

    @abstractmethod
    async def next_attempt_number(self, job_id: UUID) -> int:
        """Get the next attempt number for a job.

        Args:
            job_id: Job identifier.

        Returns:
            Next attempt number (1-indexed).

        """

    @abstractmethod
    async def count(self, job_id: UUID) -> int:
        """Count total attempts for a job.

        Args:
            job_id: Job identifier.

        Returns:
            Total number of attempts.

        """

    @abstractmethod
    async def cleanup_old_attempts(self, before: datetime) -> int:
        """Clean up old attempt records.

        Removes attempt history for jobs where all attempts are older than
        the specified datetime. This helps manage disk space without losing
        audit trails for recent deliveries.

        Args:
            before: Remove attempt records older than this datetime.

        Returns:
            Number of attempt records removed.

        """


# Simple aggregate for state queries
class DeliveryState:
    """Aggregate delivery state for a task."""

    def __init__(
        self,
        job_id: UUID,
        status: DeliveryStatus,
        attempt_count: int = 0,
        last_error: str | None = None,
    ):
        """Initialize delivery state.

        Args:
            job_id: Job identifier.
            status: Current delivery status.
            attempt_count: Number of attempts made.
            last_error: Last error message if applicable.

        """
        self.job_id = job_id
        self.status = status
        self.attempt_count = attempt_count
        self.last_error = last_error

    @property
    def is_terminal(self) -> bool:
        """Check if state is terminal (no more retries possible)."""
        return self.status in (
            DeliveryStatus.SUCCESS,
            DeliveryStatus.PERMANENT_FAIL,
            DeliveryStatus.FILTERED,
        )
