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

from ..channels.adapter_registry import AdapterRegistry
from ..models import (
    Attempt,
    ChannelScope,
    DeliveryResult,
    DeliveryStatus,
    FilterDecisionType,
    NotificationDeliveryTask,
)
from ..persistence.file import DeliveryAttemptLog, NotificationRegistry, RetryQueue
from .filter_engine import FilterEngine
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
    Safe to re-run after crashes (idempotency via attempt tracking).
    """

    def __init__(
        self,
        *,
        filter_engine: FilterEngine,
        rate_limiter: RateLimiter,
        adapters: AdapterRegistry,
        retry_policy: RetryPolicy,
        notification_registry: NotificationRegistry,
        attempt_log: DeliveryAttemptLog,
        retry_queue: RetryQueue,
    ) -> None:
        """Initialize the Delivery Processor.

        Args:
            filter_engine: Filter evaluation engine.
            rate_limiter: Rate limiting instance.
            adapters: AdapterRegistry for channel -> DeliveryAdapter lookup.
            retry_policy: Retry policy.
            notification_registry: Notification registry for tracking.
            attempt_log: Delivery attempt log for audit trail.
            retry_queue: Retry queue for scheduling retries.

        """
        self._filter_engine = filter_engine
        self._rate_limiter = rate_limiter
        self._adapters = adapters
        self._retry_policy = retry_policy
        self._notification_registry = notification_registry
        self._attempt_log = attempt_log
        self._retry_queue = retry_queue

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

        # NOTE: Notification already registered in orchestrator before fan-out

        # FILTERING (terminal decision)
        filter_decision = self._filter_engine.evaluate(
            task, dt.as_local(datetime.now(UTC))
        )

        if filter_decision.decision == FilterDecisionType.FILTERED:
            # Log attempt with filtered status
            attempt = await self._create_attempt(task)
            attempt.status = DeliveryStatus.FILTERED
            attempt.ended_at = datetime.now(UTC)
            attempt.error = filter_decision.reason

            await self._attempt_log.log_attempt(attempt, task)

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
            # Log attempt with rate-limited status
            attempt = await self._create_attempt(task)
            attempt.status = DeliveryStatus.RATE_LIMITED
            attempt.ended_at = datetime.now(UTC)
            attempt.error = (
                f"{limit_type.lower()}_rate_limit" if limit_type else "rate_limit"
            )

            await self._attempt_log.log_attempt(attempt, task)

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
        if task.channel_info.scope == ChannelScope.RECIPIENT:
            # Get adapter to check its requirements
            adapter = self._adapters.get(task.channel_info.id)

            if adapter:
                requirements = adapter.get_requirements()

                # Build list of missing requirements
                missing_requirements: list[str] = []

                if (
                    requirements.get("requires_email", False)
                    and not task.contact_info.email_address
                ):
                    missing_requirements.append("email address")
                if (
                    requirements.get("requires_phone", False)
                    and not task.contact_info.phone_number
                ):
                    missing_requirements.append("phone number")

                if missing_requirements:
                    # Log attempt with validation failure
                    attempt = await self._create_attempt(task)
                    attempt.status = DeliveryStatus.PERMANENT_FAIL
                    attempt.ended_at = datetime.now(UTC)
                    attempt.error = f"missing_required_contact_info: {', '.join(missing_requirements)}"

                    await self._attempt_log.log_attempt(attempt, task)

                    _LOGGER.warning(
                        "Task job_id=%s skipped: missing required contact information for recipient %s on channel %s: %s",
                        task.job_id,
                        task.recipient_id,
                        task.channel_info.id,
                        ", ".join(missing_requirements),
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
            error_msg = (
                f"No adapter registered for channel '{task.channel_info.id}'. "
                f"This channel may not be properly configured. "
                f"Expected adapter type: {task.channel_info.integration}"
            )
            _LOGGER.error(
                "%s (job_id=%s, recipient=%s)",
                error_msg,
                task.job_id,
                task.recipient_id,
            )

            # Permanent failure - no retry (adapter won't magically appear)
            await self._handle_permanent_failure(
                task,
                attempt,
                error=error_msg,
            )
            return

        try:
            result = await adapter.deliver(
                payload=task.payload,
                contact_info=task.contact_info,
                idempotency_key=attempt.idempotency_key,
                tts_settings=task.tts_settings,
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
        attempt_number = await self._attempt_log.get_next_attempt_number(task.job_id)

        # Don't log - will log when attempt completes with final status
        return Attempt(
            attempt_id=uuid4(),
            job_id=task.job_id,
            attempt_number=attempt_number,
            idempotency_key=f"{task.job_id}:{attempt_number}",
            status=DeliveryStatus.IN_PROGRESS,
            started_at=datetime.now(UTC),
        )

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

        # Log attempt with final status (only logged once)
        await self._attempt_log.log_attempt(attempt, task)

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

        # Log attempt with final status (only logged once)
        await self._attempt_log.log_attempt(attempt, task)

        _LOGGER.warning(
            "Transient failure job_id=%s recipient_id=%s attempt=%s channel=%s error=%s",
            task.job_id,
            task.recipient_id,
            attempt.attempt_number,
            task.channel_info.id,
            error,
        )

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

        # Log attempt with final status (only logged once)
        await self._attempt_log.log_attempt(attempt, task)

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
        attempt_count = await self._attempt_log.count_attempts(task.job_id)

        # Check against policy max attempts
        if attempt_count >= task.policy.retry_attempts:
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
            _LOGGER.warning(
                "Retry policy exceeded for job_id=%s",
                task.job_id,
            )
            return

        if retry_decision.next_run_at is None:
            _LOGGER.warning(
                "Retry policy returned no next_run_at for job_id=%s",
                task.job_id,
            )
            return

        # Schedule retry in queue with full task snapshot
        await self._retry_queue.schedule_retry(
            job_id=task.job_id,
            scheduled_at=retry_decision.next_run_at,
            reason=reason,
            task=task,
        )

        _LOGGER.debug(
            "Retry scheduled job_id=%s recipient_id=%s attempt=%s next_run=%s reason=%s",
            task.job_id,
            task.recipient_id,
            attempt_count + 1,
            retry_decision.next_run_at,
            reason,
        )
