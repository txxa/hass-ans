"""Concrete implementations of persistence stores for delivery tasks."""

from datetime import datetime
from uuid import UUID

from ..models import Attempt, DeliveryStatus
from .base import AttemptStore, DeliveryState, DeliveryStateStore


class InMemoryDeliveryStateStore(DeliveryStateStore):
    """In-memory implementation of delivery state persistence.

    Suitable for testing and single-instance deployments.
    All data is lost on restart.
    """

    def __init__(self) -> None:
        """Initialize the in-memory state store."""
        # job_id -> DeliveryState
        self._states: dict[UUID, DeliveryState] = {}
        # job_id -> (run_at, reason)
        self._retries: dict[UUID, tuple[datetime, str | None]] = {}

    async def load(self, job_id: UUID) -> DeliveryState | None:
        """Load delivery state for a job."""
        return self._states.get(job_id)

    async def persist_filtered(self, job_id: UUID, reason: str | None = None) -> None:
        """Persist a filtered (terminal) state."""
        state = DeliveryState(
            job_id=job_id,
            status=DeliveryStatus.FILTERED,
            attempt_count=0,
            last_error=reason,
        )
        self._states[job_id] = state
        # Remove any pending retry
        self._retries.pop(job_id, None)

    async def persist_rate_limited(self, job_id: UUID) -> None:
        """Persist rate-limited state (retryable)."""
        state = DeliveryState(
            job_id=job_id,
            status=DeliveryStatus.RATE_LIMITED,
            attempt_count=0,
            last_error="rate_limited",
        )
        self._states[job_id] = state

    async def persist_success(self, job_id: UUID, attempt: Attempt) -> None:
        """Persist successful delivery state."""
        state = DeliveryState(
            job_id=job_id,
            status=DeliveryStatus.SUCCESS,
            attempt_count=attempt.attempt_number,
            last_error=None,
        )
        self._states[job_id] = state
        # Remove any pending retry
        self._retries.pop(job_id, None)

    async def persist_transient_failure(self, job_id: UUID, attempt: Attempt) -> None:
        """Persist transient failure state (retryable)."""
        state = DeliveryState(
            job_id=job_id,
            status=DeliveryStatus.TRANSIENT_FAIL,
            attempt_count=attempt.attempt_number,
            last_error=attempt.error,
        )
        self._states[job_id] = state

    async def persist_permanent_failure(
        self,
        job_id: UUID,
        attempt: Attempt | None = None,
        error: str | None = None,
    ) -> None:
        """Persist permanent failure state (terminal)."""
        attempt_count = attempt.attempt_number if attempt else 0
        state = DeliveryState(
            job_id=job_id,
            status=DeliveryStatus.PERMANENT_FAIL,
            attempt_count=attempt_count,
            last_error=error or (attempt.error if attempt else None),
        )
        self._states[job_id] = state
        # Remove any pending retry
        self._retries.pop(job_id, None)

    async def schedule_retry(
        self,
        job_id: UUID,
        run_at: datetime,
        reason: str | None = None,
    ) -> None:
        """Schedule a retry for a failed delivery task."""
        self._retries[job_id] = (run_at, reason)

    async def cleanup_completed(self, before: datetime) -> int:
        """Clean up terminal states older than the given date.

        Args:
            before: Delete states with status completed before this time.

        Returns:
            Number of states deleted.

        """
        to_delete = [
            job_id
            for job_id, state in self._states.items()
            if state.is_terminal
            # Could track creation_time in DeliveryState if needed
        ]
        for job_id in to_delete:
            self._states.pop(job_id, None)
            self._retries.pop(job_id, None)
        return len(to_delete)

    def get_pending_retries(self) -> list[tuple[UUID, datetime]]:
        """Get all scheduled retries.

        Useful for retrieving retries on startup.

        Returns:
            List of (job_id, run_at) tuples.

        """
        return [(job_id, run_at) for job_id, (run_at, _) in self._retries.items()]


class InMemoryAttemptStore(AttemptStore):
    """In-memory implementation of attempt persistence.

    Tracks all attempts for audit and idempotency.
    """

    def __init__(self) -> None:
        """Initialize the in-memory attempt store."""
        # job_id -> list of attempts (ordered by attempt_number)
        self._attempts: dict[UUID, list[Attempt]] = {}

    async def create(self, attempt: Attempt) -> None:
        """Create a new attempt record."""
        if attempt.job_id not in self._attempts:
            self._attempts[attempt.job_id] = []
        self._attempts[attempt.job_id].append(attempt)

    async def update(self, attempt: Attempt) -> None:
        """Update an existing attempt record."""
        attempts = self._attempts.get(attempt.job_id, [])
        # Find and replace by attempt_number
        for i, existing in enumerate(attempts):
            if existing.attempt_number == attempt.attempt_number:
                attempts[i] = attempt
                return

    async def next_attempt_number(self, job_id: UUID) -> int:
        """Get the next attempt number for a job."""
        attempts = self._attempts.get(job_id, [])
        if not attempts:
            return 1
        return max(a.attempt_number for a in attempts) + 1

    async def count(self, job_id: UUID) -> int:
        """Count total attempts for a job."""
        return len(self._attempts.get(job_id, []))

    async def cleanup_old_attempts(self, before: datetime) -> int:
        """Clean up old attempt records.

        For in-memory store, this is a no-op since memory is freed on restart.
        In practice, the housekeeping scheduler would rely on file-based stores
        for actual cleanup.

        Args:
            before: (unused for in-memory store).

        Returns:
            0 (no cleanup performed).

        """
        # In-memory store doesn't persist, so no cleanup needed
        return 0

    def get_attempts(self, job_id: UUID) -> list[Attempt]:
        """Get all attempts for a job (for debugging/audit).

        Args:
            job_id: Job identifier.

        Returns:
            List of attempts in order.

        """
        return self._attempts.get(job_id, [])
