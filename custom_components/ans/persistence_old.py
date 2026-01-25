"""Persistence layer abstractions and in-memory implementations."""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from .models import Attempt, DeliveryStatus, FilterReason, NotificationDeliveryTask


class DeliveryTaskStore(ABC):
    """Abstract store for persistent delivery tasks."""

    @abstractmethod
    async def save_task(self, task: NotificationDeliveryTask) -> None:
        """Save a task for later processing."""
        ...

    @abstractmethod
    async def load_pending_tasks(self) -> list[NotificationDeliveryTask]:
        """Load all pending tasks (e.g., on startup)."""
        ...

    @abstractmethod
    async def delete_task(self, job_id: UUID) -> None:
        """Delete a task after successful processing."""
        ...


class AttemptStore(ABC):
    """Abstract store for delivery attempts."""

    @abstractmethod
    async def save_attempt(self, attempt: Attempt) -> None:
        """Save an attempt record."""
        ...

    @abstractmethod
    async def load_attempts(self, job_id: UUID) -> list[Attempt]:
        """Load all attempts for a job."""
        ...

    @abstractmethod
    async def next_attempt_number(self, job_id: UUID) -> int:
        """Get the next attempt number for a job."""
        ...

    @abstractmethod
    async def save_filtered_attempt(
        self,
        task: NotificationDeliveryTask,
        reason: FilterReason,
    ) -> None:
        """Save a filtered attempt (terminal state)."""
        ...


class InMemoryDeliveryTaskStore(DeliveryTaskStore):
    """Simple in-memory task store for testing/basic use."""

    def __init__(self) -> None:
        """Initialize the in-memory store."""
        self._tasks: dict[UUID, NotificationDeliveryTask] = {}

    async def save_task(self, task: NotificationDeliveryTask) -> None:
        """Save a task for later processing."""
        self._tasks[task.job_id] = task

    async def load_pending_tasks(self) -> list[NotificationDeliveryTask]:
        """Load all pending tasks."""
        return list(self._tasks.values())

    async def delete_task(self, job_id: UUID) -> None:
        """Delete a task after successful processing."""
        self._tasks.pop(job_id, None)


class InMemoryAttemptStore(AttemptStore):
    """Simple in-memory attempt store for testing/basic use."""

    def __init__(self) -> None:
        """Initialize the in-memory store."""
        self._attempts: dict[UUID, list[Attempt]] = {}

    async def save_attempt(self, attempt: Attempt) -> None:
        """Save an attempt record."""
        if attempt.job_id not in self._attempts:
            self._attempts[attempt.job_id] = []

        # Check if this attempt already exists and update it
        existing = next(
            (
                a
                for a in self._attempts[attempt.job_id]
                if a.attempt_id == attempt.attempt_id
            ),
            None,
        )
        if existing:
            idx = self._attempts[attempt.job_id].index(existing)
            self._attempts[attempt.job_id][idx] = attempt
        else:
            self._attempts[attempt.job_id].append(attempt)

    async def load_attempts(self, job_id: UUID) -> list[Attempt]:
        """Load all attempts for a job."""
        return self._attempts.get(job_id, [])

    async def next_attempt_number(self, job_id: UUID) -> int:
        """Get the next attempt number for a job."""
        attempts = self._attempts.get(job_id, [])
        return len(attempts) + 1

    async def save_filtered_attempt(
        self,
        task: NotificationDeliveryTask,
        reason: FilterReason,
    ) -> None:
        """Save a filtered attempt (terminal state)."""
        attempt = Attempt(
            attempt_id=task.job_id,  # Use job_id as attempt_id for filtered
            job_id=task.job_id,
            attempt_number=1,
            idempotency_key=f"{task.job_id}:filtered",
            status=DeliveryStatus.FILTERED,
            started_at=datetime.now(),
            ended_at=datetime.now(),
            error=str(reason),
        )
        await self.save_attempt(attempt)
