"""Notification delivery processor.

Responsible for executing delivery tasks:
- filter evaluation
- rate limiting
- delivery execution
- retry scheduling
- persistence coordination
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceNotFound
from homeassistant.util import dt

from ..channels.base import TTSDeliveryOptions
from ..channels.channel_manager import ChannelManager
from ..const import (
    EVENT_NOTIFICATION_DELIVERED,
    EVENT_NOTIFICATION_FAILED,
    EVENT_NOTIFICATION_FILTERED,
    EVENT_NOTIFICATION_RATE_LIMITED,
)
from ..models import (
    Attempt,
    ChannelScope,
    DeliveryResult,
    DeliveryStatus,
    FilterDecisionType,
    NotificationDeliveryTask,
    TaskOutcome,
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

    Note: rate_limiter state is held in-memory and resets on HA restart.
    All other durable state (delivery attempts, retry queue) is persisted.
    Safe to re-run after crashes (idempotency via attempt tracking).
    """

    def __init__(
        self,
        *,
        filter_engine: FilterEngine,
        rate_limiter: RateLimiter,
        channel_manager: ChannelManager,
        hass: HomeAssistant,
        retry_policy: RetryPolicy,
        notification_registry: NotificationRegistry,
        attempt_log: DeliveryAttemptLog,
        retry_queue: RetryQueue,
        on_terminal_outcome: Callable[[str, TaskOutcome, str], None] | None = None,
    ) -> None:
        """Initialize the Delivery Processor.

        Args:
            filter_engine: Filter evaluation engine.
            rate_limiter: Rate limiting instance.
            channel_manager: ChannelManager for channel -> DeliveryAdapter lookup.
            hass: HomeAssistant instance (for fire-and-forget resync tasks).
            retry_policy: Retry policy.
            notification_registry: Notification registry for tracking.
            attempt_log: Delivery attempt log for audit trail.
            retry_queue: Retry queue for scheduling retries.
            on_terminal_outcome: Optional callback invoked after each terminal task
                outcome (success, permanent failure, filtered).  Signature:
                ``(notification_id: str, outcome_key: TaskOutcome, recipient_id: str) -> None``.

        """
        self._filter_engine = filter_engine
        self._rate_limiter = rate_limiter
        self._channel_manager = channel_manager
        self._hass = hass
        self._retry_policy = retry_policy
        self._notification_registry = notification_registry
        self._attempt_log = attempt_log
        self._retry_queue = retry_queue
        self._on_terminal_outcome = on_terminal_outcome

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
            "Processing task notification_id=%s job_id=%s recipient=%s channel=%s%s",
            task.payload.notification_id,
            task.job_id,
            task.recipient_id,
            task.channel_info.id,
            " (retry)" if task.is_retry else "",
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
                "Task filtered: notification_id=%s job_id=%s recipient_id=%s channel=%s reason=%s details=%s",
                task.payload.notification_id,
                task.job_id,
                task.recipient_id,
                task.channel_info.id,
                filter_decision.reason,
                filter_decision.details or {},
            )

            self._hass.bus.async_fire(
                EVENT_NOTIFICATION_FILTERED,
                {
                    **self._build_base_event_payload(task),
                    "filter_reason": filter_decision.reason.value,
                },
            )
            if self._on_terminal_outcome is not None:
                self._on_terminal_outcome(
                    str(task.payload.notification_id),
                    TaskOutcome.FILTERED,
                    task.recipient_id,
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

            _LOGGER.warning(
                "Rate limited (%s): notification_id=%s job_id=%s recipient_id=%s channel=%s "
                "— scheduling retry",
                limit_type,
                task.payload.notification_id,
                task.job_id,
                task.recipient_id,
                task.channel_info.id,
            )
            retry_at = await self._schedule_retry(task, reason="rate_limited")

            self._hass.bus.async_fire(
                EVENT_NOTIFICATION_RATE_LIMITED,
                {
                    **self._build_base_event_payload(task),
                    "limit_type": limit_type or "UNKNOWN",
                    "retry_at": retry_at.isoformat() if retry_at else None,
                },
            )
            if retry_at is None:
                # Retries exhausted — the already-logged RATE_LIMITED attempt is the
                # final record; signal failure without creating a phantom attempt.
                self._signal_terminal_failure(
                    task,
                    error="retries_exhausted_after_rate_limit",
                    attempt_number=attempt.attempt_number,
                )
            return

        # CONTACT INFO VALIDATION (terminal decision if missing/invalid)
        # Skip validation for system-wide channels (e.g., persistent_notification)
        # as they deliver to the HA instance, not to a person
        if task.channel_info.scope == ChannelScope.RECIPIENT:
            # Get adapter to check its requirements
            adapter = self._channel_manager.get_adapter(task.channel_info.id)

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
                    await self._handle_permanent_failure(
                        task,
                        attempt,
                        error=f"missing_required_contact_info: {', '.join(missing_requirements)}",
                    )
                    return

        # ATTEMPT CREATION & EXECUTION
        attempt = await self._create_attempt(task)

        try:
            await self._execute_delivery(task, attempt)
        except Exception as exc:  # defensive catch for unexpected errors
            _LOGGER.exception(
                "Unhandled exception during delivery notification_id=%s "
                "job_id=%s recipient_id=%s",
                task.payload.notification_id,
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
        adapter = self._channel_manager.get_adapter(task.channel_info.id)

        if not adapter:
            error_msg = (
                f"No adapter registered for channel '{task.channel_info.id}'. "
                f"This channel may not be properly configured. "
                f"Expected adapter type: {task.channel_info.integration}"
            )
            _LOGGER.warning(
                "No adapter for channel '%s': notification_id=%s job_id=%s recipient=%s "
                "— treating as transient, will retry",
                task.channel_info.id,
                task.payload.notification_id,
                task.job_id,
                task.recipient_id,
            )

            # Transient failure: event listeners in __init__.py handle resync
            # when adapters become available (_on_media_player_added,
            # _on_entity_registry_updated). The adapter should be present by
            # the time the retry fires.
            await self._handle_transient_failure(
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
                job_id=str(task.job_id),
                options=TTSDeliveryOptions(tts_settings=task.tts_settings)
                if task.tts_settings
                else None,
            )
        except ServiceNotFound as exc:
            _LOGGER.warning(
                "ServiceNotFound during delivery: notification_id=%s job_id=%s "
                "channel=%s recipient=%s: %s — treating as transient, will retry",
                task.payload.notification_id,
                task.job_id,
                task.channel_info.id,
                task.recipient_id,
                exc,
            )
            await self._handle_transient_failure(task, attempt, error=str(exc))
            return
        except Exception as exc:
            _LOGGER.exception(
                "Adapter exception: notification_id=%s job_id=%s channel=%s recipient=%s",
                task.payload.notification_id,
                task.job_id,
                task.channel_info.id,
                task.recipient_id,
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

        duration_ms = int(
            (attempt.ended_at - attempt.started_at).total_seconds() * 1000
        )

        # Log attempt with final status (only logged once)
        await self._attempt_log.log_attempt(attempt, task)

        self._hass.bus.async_fire(
            EVENT_NOTIFICATION_DELIVERED,
            {
                **self._build_base_event_payload(task),
                "attempt_number": attempt.attempt_number,
                "remote_id": result.remote_id,
                **({"mobile_tag": result.mobile_tag} if result.mobile_tag else {}),
            },
        )
        if self._on_terminal_outcome is not None:
            self._on_terminal_outcome(
                str(task.payload.notification_id),
                TaskOutcome.DELIVERED,
                task.recipient_id,
            )

        _LOGGER.info(
            "Delivery succeeded: notification_id=%s job_id=%s recipient_id=%s "
            "channel=%s attempt=%s duration_ms=%d%s",
            task.payload.notification_id,
            task.job_id,
            task.recipient_id,
            task.channel_info.id,
            attempt.attempt_number,
            duration_ms,
            " (delivered on retry)" if attempt.attempt_number > 1 else "",
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
            "Transient failure notification_id=%s job_id=%s recipient_id=%s "
            "attempt=%s channel=%s error=%s",
            task.payload.notification_id,
            task.job_id,
            task.recipient_id,
            attempt.attempt_number,
            task.channel_info.id,
            error,
        )

        retry_at = await self._schedule_retry(task, reason="transient_failure")
        if retry_at is None:
            # Retries exhausted — the already-logged TRANSIENT_FAIL attempt is the
            # final record; signal failure without creating a phantom attempt.
            self._signal_terminal_failure(
                task,
                error=f"retries_exhausted: {error}",
                attempt_number=attempt.attempt_number,
            )

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

        self._signal_terminal_failure(task, attempt.error, attempt.attempt_number)

        _LOGGER.error(
            "Permanent delivery failure: notification_id=%s job_id=%s "
            "recipient_id=%s channel=%s attempt=%s error=%s",
            task.payload.notification_id,
            task.job_id,
            task.recipient_id,
            task.channel_info.id,
            attempt.attempt_number,
            error,
        )

    # ------------------------------------------------------------------
    # Retry handling
    # ------------------------------------------------------------------

    def _signal_terminal_failure(
        self,
        task: NotificationDeliveryTask,
        error: str | None,
        attempt_number: int,
    ) -> None:
        """Fire the failed event and invoke the terminal-outcome callback.

        Intentionally decoupled from attempt logging so it can be called both
        from :meth:`_handle_permanent_failure` (after logging) and from
        exhaustion paths where the attempt was already logged as
        TRANSIENT_FAIL / RATE_LIMITED.
        """
        self._hass.bus.async_fire(
            EVENT_NOTIFICATION_FAILED,
            {
                **self._build_base_event_payload(task),
                "error": error,
                "attempt_number": attempt_number,
            },
        )
        if self._on_terminal_outcome is not None:
            self._on_terminal_outcome(
                str(task.payload.notification_id), TaskOutcome.FAILED, task.recipient_id
            )

    def _build_base_event_payload(self, task: NotificationDeliveryTask) -> dict:
        """Build the common event payload fields shared by all delivery outcome events."""
        return {
            "notification_id": str(task.payload.notification_id),
            "recipient_id": task.recipient_id,
            "channel_id": task.channel_info.id,
            "source": task.payload.source,
            "criticality": task.payload.criticality.value,
            "type": task.payload.type.value,
        }

    async def _schedule_retry(
        self,
        task: NotificationDeliveryTask,
        *,
        reason: str,
    ) -> datetime | None:
        """Evaluate retry policy and schedule if appropriate.

        Args:
            task: Delivery task.
            reason: Reason for retry.

        Returns:
            The scheduled retry datetime, or None if no retry was scheduled.

        """
        attempt_count = await self._attempt_log.count_attempts(task.job_id)

        # Check against policy max attempts
        if attempt_count > task.policy.retry_attempts:
            _LOGGER.warning(
                "Max retries exceeded: notification_id=%s job_id=%s "
                "recipient_id=%s channel=%s attempts=%s",
                task.payload.notification_id,
                task.job_id,
                task.recipient_id,
                task.channel_info.id,
                attempt_count,
            )
            return None

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
                "Retry policy exhausted: notification_id=%s job_id=%s recipient_id=%s channel=%s",
                task.payload.notification_id,
                task.job_id,
                task.recipient_id,
                task.channel_info.id,
            )
            return None

        if retry_decision.next_run_at is None:
            _LOGGER.warning(
                "Retry policy returned no next_run_at: notification_id=%s job_id=%s",
                task.payload.notification_id,
                task.job_id,
            )
            return None

        # Schedule retry in queue with full task snapshot
        await self._retry_queue.schedule_retry(
            job_id=task.job_id,
            scheduled_at=retry_decision.next_run_at,
            reason=reason,
            task=task,
        )

        delay_seconds = (retry_decision.next_run_at - datetime.now(UTC)).total_seconds()
        _LOGGER.debug(
            "Retry scheduled: notification_id=%s job_id=%s recipient_id=%s channel=%s "
            "attempt=%s delay_seconds=%.0f next_run=%s reason=%s",
            task.payload.notification_id,
            task.job_id,
            task.recipient_id,
            task.channel_info.id,
            attempt_count + 1,
            delay_seconds,
            retry_decision.next_run_at.isoformat(),
            reason,
        )

        return retry_decision.next_run_at
