"""Notification delivery processor.

Responsible for executing delivery tasks:
- filter evaluation
- rate limiting
- delivery execution
- retry scheduling
- persistence coordination
"""

import logging
from datetime import UTC, datetime
from uuid import uuid4

from homeassistant.util import dt

from .delivery.base import DeliveryAdapter
from .filter_engine import FilterEngine
from .models import (
    Attempt,
    ChannelScope,
    DeliveryResult,
    DeliveryStatus,
    FilterDecisionType,
    NotificationDeliveryTask,
)
from .persistence import AttemptStore, DeliveryStateStore
from .rate_limiter import RateLimiter
from .retry_scheduler import RetryPolicy, RetryReason

_LOGGER = logging.getLogger(__name__)


class NotificationDeliveryProcessor:
    """Executes exactly one delivery task.

    This class owns:
    - filter evaluation
    - rate limiting
    - delivery
    - retry decision
    - persistence coordination

    Stateless in memory. All durable state is persisted.
    Safe to re-run after crashes (idempotent via idempotency_key).
    """

    def __init__(
        self,
        *,
        filter_engine: FilterEngine,
        rate_limiter: RateLimiter,
        adapters: dict[str, DeliveryAdapter],
        retry_policy: RetryPolicy,
        state_store: DeliveryStateStore,
        attempt_store: AttemptStore,
    ) -> None:
        """Initialize the Delivery Processor.

        Args:
            filter_engine: Filter evaluation engine.
            rate_limiter: Rate limiting instance.
            adapters: Dict of channel -> DeliveryAdapter.
            retry_policy: Retry policy.
            state_store: Delivery state persistence.
            attempt_store: Attempt tracking persistence.

        """
        self._filter_engine = filter_engine
        self._rate_limiter = rate_limiter
        self._adapters = adapters
        self._retry_policy = retry_policy
        self._state_store = state_store
        self._attempt_store = attempt_store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process(self, task: NotificationDeliveryTask) -> None:
        """Process a single delivery task.

        Safe to call multiple times for the same task (idempotent).

        Args:
            task: Self-contained task with all required data.

        """
        _LOGGER.debug(
            "Processing task job_id=%s recipient=%s channel=%s",
            task.job_id,
            task.recipient_id,
            task.channel_info.id,
        )

        # Load current delivery state to check if already terminal
        current_state = await self._state_store.load(task.job_id)
        if current_state and current_state.is_terminal:
            _LOGGER.debug(
                "Task job_id=%s already terminal (%s), skipping",
                task.job_id,
                current_state.status,
            )
            return

        # FILTERING (terminal decision)
        filter_decision = self._filter_engine.evaluate(
            task, dt.as_local(datetime.now(UTC))
        )

        if filter_decision.decision == FilterDecisionType.FILTERED:
            await self._state_store.persist_filtered(
                job_id=task.job_id,
                reason=filter_decision.reason,
            )
            _LOGGER.info(
                "Task job_id=%s recipient_id=%s filtered (%s)",
                task.job_id,
                task.recipient_id,
                filter_decision.reason,
            )
            return

        # RATE LIMITING (retryable decision)
        allowed, limit_type = self._rate_limiter.allow(task)
        if not allowed:
            await self._state_store.persist_rate_limited(task.job_id)
            if limit_type == "GLOBAL":
                _LOGGER.debug(
                    "Task job_id=%s recipient_id=%s hit global rate limit, scheduling retry",
                    task.job_id,
                    task.recipient_id,
                )
                await self._schedule_retry(task, reason="rate_limited")
            else:  # RECIPIENT
                _LOGGER.debug(
                    "Task job_id=%s recipient_id=%s hit recipient rate limit, scheduling retry",
                    task.job_id,
                    task.recipient_id,
                )
                await self._schedule_retry(task, reason="rate_limited")
            return

        # CONTACT INFO VALIDATION (terminal decision if missing/invalid)
        # Skip validation for system-wide channels (e.g., persistent_notification)
        # as they deliver to the HA instance, not to a person
        if task.channel_info.scope == ChannelScope.RECIPIENT_SPECIFIC:
            if (
                not task.contact_info.email_address
                and not task.contact_info.phone_number
            ):
                await self._state_store.persist_permanent_failure(
                    task.job_id,
                    attempt=None,
                    error="No contact information available (email and phone both missing)",
                )
                _LOGGER.warning(
                    "Task job_id=%s skipped: no contact information for recipient %s",
                    task.job_id,
                    task.recipient_id,
                )
                return

        # ATTEMPT CREATION & EXECUTION
        attempt = await self._create_attempt(task)

        try:
            await self._execute_delivery(task, attempt)
        except Exception as exc:  # defensive catch for unexpected errors
            _LOGGER.exception(
                "Unhandled exception during delivery job_id=%s recipient_id=%s",
                task.job_id,
                task.recipient_id,
            )
            await self._handle_transient_failure(
                task,
                attempt,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Core delivery logic
    # ------------------------------------------------------------------

    async def _execute_delivery(
        self,
        task: NotificationDeliveryTask,
        attempt: Attempt,
    ) -> None:
        """Execute the delivery for a single attempt.

        Args:
            task: Delivery task.
            attempt: Attempt record.

        """
        adapter = self._adapters.get(task.channel_info.id)
        if not adapter:
            await self._handle_permanent_failure(
                task,
                attempt,
                error=f"No adapter for channel {task.channel_info.id}",
            )
            return

        try:
            result = await adapter.deliver(
                payload=task.payload,
                contact_info=task.contact_info,
                idempotency_key=attempt.idempotency_key,
            )
        except Exception as exc:
            _LOGGER.exception(
                "Adapter exception job_id=%s channel=%s",
                task.job_id,
                task.channel_info.id,
            )
            result = DeliveryResult(
                status=DeliveryStatus.TRANSIENT_FAIL,
                error=str(exc),
            )

        if result.status == DeliveryStatus.SUCCESS:
            await self._finalize_success(task, attempt, result)
            return

        if result.status == DeliveryStatus.TRANSIENT_FAIL:
            await self._handle_transient_failure(
                task,
                attempt,
                error=result.error,
            )
            return

        # Everything else is treated as permanent failure
        await self._handle_permanent_failure(
            task,
            attempt,
            error=result.error,
        )

    # ------------------------------------------------------------------
    # Attempt lifecycle helpers
    # ------------------------------------------------------------------

    async def _create_attempt(self, task: NotificationDeliveryTask) -> Attempt:
        """Create next attempt record for a task.

        Args:
            task: Delivery task.

        Returns:
            Newly created Attempt.

        """
        attempt_number = await self._attempt_store.next_attempt_number(task.job_id)

        attempt = Attempt(
            attempt_id=uuid4(),
            job_id=task.job_id,
            attempt_number=attempt_number,
            idempotency_key=f"{task.job_id}:{attempt_number}",
            status=DeliveryStatus.IN_PROGRESS,
            started_at=datetime.now(UTC),
        )

        await self._attempt_store.create(attempt)
        return attempt

    async def _finalize_success(
        self,
        task: NotificationDeliveryTask,
        attempt: Attempt,
        result: DeliveryResult,
    ) -> None:
        """Handle successful delivery.

        Args:
            task: Delivery task.
            attempt: Attempt record.
            result: Delivery result.

        """
        attempt.status = DeliveryStatus.SUCCESS
        attempt.ended_at = datetime.now(UTC)
        attempt.remote_id = result.remote_id

        await self._attempt_store.update(attempt)
        await self._state_store.persist_success(task.job_id, attempt)

        _LOGGER.info(
            "Delivery succeeded job_id=%s recipient_id=%s attempt=%s",
            task.job_id,
            task.recipient_id,
            attempt.attempt_number,
        )

    async def _handle_transient_failure(
        self,
        task: NotificationDeliveryTask,
        attempt: Attempt,
        error: str | None,
    ) -> None:
        """Handle transient failure and schedule retry.

        Args:
            task: Delivery task.
            attempt: Attempt record.
            error: Error description.

        """
        attempt.status = DeliveryStatus.TRANSIENT_FAIL
        attempt.ended_at = datetime.now(UTC)
        attempt.error = error

        await self._attempt_store.update(attempt)
        await self._state_store.persist_transient_failure(task.job_id, attempt)

        await self._schedule_retry(task, reason="transient_failure")

    async def _handle_permanent_failure(
        self,
        task: NotificationDeliveryTask,
        attempt: Attempt,
        error: str | None,
    ) -> None:
        """Handle permanent failure and terminate task.

        Args:
            task: Delivery task.
            attempt: Attempt record.
            error: Error description.

        """
        attempt.status = DeliveryStatus.PERMANENT_FAIL
        attempt.ended_at = datetime.now(UTC)
        attempt.error = error

        await self._attempt_store.update(attempt)
        await self._state_store.persist_permanent_failure(
            task.job_id, attempt, error=error
        )

        _LOGGER.warning(
            "Permanent failure job_id=%s recipient_id=%s attempt=%s error=%s",
            task.job_id,
            task.recipient_id,
            attempt.attempt_number,
            error,
        )

    # ------------------------------------------------------------------
    # Retry handling
    # ------------------------------------------------------------------

    async def _schedule_retry(
        self,
        task: NotificationDeliveryTask,
        *,
        reason: str,
    ) -> None:
        """Evaluate retry policy and schedule if appropriate.

        Args:
            task: Delivery task.
            reason: Reason for retry.

        """
        attempt_count = await self._attempt_store.count(task.job_id)

        # Check against policy max attempts
        if attempt_count >= task.policy.retry_attempts:
            await self._state_store.persist_permanent_failure(
                task.job_id,
                attempt=None,
                error="max_retries_exceeded",
            )
            _LOGGER.warning(
                "Max retries exceeded job_id=%s recipient_id=%s attempts=%s",
                task.job_id,
                task.recipient_id,
                attempt_count,
            )
            return

        # Calculate delay using retry policy
        retry_reason = (
            RetryReason.RATE_LIMITED
            if reason == "rate_limited"
            else RetryReason.TRANSIENT_FAILURE
        )
        retry_decision = self._retry_policy.evaluate(
            attempt_number=attempt_count,
            reason=retry_reason,
            now=datetime.now(UTC),
        )

        if not retry_decision.should_retry:
            await self._state_store.persist_permanent_failure(
                task.job_id,
                attempt=None,
                error="retry_policy_exceeded",
            )
            return

        if retry_decision.next_run_at is None:
            await self._state_store.persist_permanent_failure(
                task.job_id,
                attempt=None,
                error="retry_policy_exceeded",
            )
            return

        await self._state_store.schedule_retry(
            task.job_id, retry_decision.next_run_at, reason=reason
        )

        _LOGGER.debug(
            "Retry scheduled job_id=%s recipient_id=%s attempt=%s next_run=%s reason=%s",
            task.job_id,
            task.recipient_id,
            attempt_count + 1,
            retry_decision.next_run_at,
            reason,
        )
