"""Tests for NotificationDeliveryProcessor."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import ServiceNotFound

from custom_components.ans.delivery.processor import NotificationDeliveryProcessor
from custom_components.ans.models import (
    ChannelInfo,
    ChannelScope,
    DeliveryResult,
    DeliveryStatus,
    FilterDecision,
    FilterDecisionType,
    FilterReason,
    RecipientContactInfo,
)

from ..conftest import make_task

# ── fixture helpers ───────────────────────────────────────────────────────────


def _filter_allowed() -> MagicMock:
    """Return a mock FilterEngine whose evaluate() always returns ALLOWED."""
    fe = MagicMock()
    fe.evaluate.return_value = FilterDecision(
        decision=FilterDecisionType.ALLOWED,
        reason=FilterReason.NORMAL,
    )
    return fe


def _filter_blocked(reason=FilterReason.TYPE_NOT_ALLOWED) -> MagicMock:
    """Return a mock FilterEngine whose evaluate() always returns FILTERED with the given reason."""
    fe = MagicMock()
    fe.evaluate.return_value = FilterDecision(
        decision=FilterDecisionType.FILTERED,
        reason=reason,
        details={"type": "INFO"},
    )
    return fe


def _rate_limiter_allow() -> MagicMock:
    """Return a mock RateLimiter whose allow() always returns (True, None)."""
    rl = MagicMock()
    rl.allow.return_value = (True, None)
    return rl


def _rate_limiter_deny(limit_type="GLOBAL") -> MagicMock:
    """Return a mock RateLimiter whose allow() always returns (False, limit_type)."""
    rl = MagicMock()
    rl.allow.return_value = (False, limit_type)
    return rl


def _adapter(*, status: DeliveryStatus = DeliveryStatus.SUCCESS) -> MagicMock:
    """Return a mock channel adapter whose deliver() returns a result with the given DeliveryStatus."""
    adapter = MagicMock()
    adapter.get_requirements.return_value = {}
    adapter.deliver = AsyncMock(return_value=DeliveryResult(status=status, error=None))
    return adapter


def _make_processor(
    *,
    filter_engine=None,
    rate_limiter=None,
    channel_manager=None,
    hass=None,
    retry_policy=None,
    adapter=None,
) -> NotificationDeliveryProcessor:
    """Build a processor with sensible defaults for all dependencies."""
    adapter = adapter or _adapter()

    if channel_manager is not None:
        cm = channel_manager
    else:
        cm = MagicMock()
        cm.get_adapter = MagicMock(return_value=adapter)

    attempt_log = MagicMock()
    attempt_log.get_next_attempt_number = AsyncMock(return_value=1)
    attempt_log.count_attempts = AsyncMock(return_value=0)
    attempt_log.log_attempt = AsyncMock()

    retry_queue = MagicMock()
    retry_queue.schedule_retry = AsyncMock()
    retry_queue.remove_retry = AsyncMock()

    notification_registry = MagicMock()
    notification_registry.register_notification = AsyncMock()

    retry_pol = retry_policy or MagicMock()
    retry_pol.evaluate = MagicMock(
        return_value=MagicMock(
            should_retry=True,
            next_run_at=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
            reason="TRANSIENT_FAILURE",
        )
    )

    return NotificationDeliveryProcessor(
        filter_engine=filter_engine or _filter_allowed(),
        rate_limiter=rate_limiter or _rate_limiter_allow(),
        channel_manager=cm,
        hass=hass or MagicMock(),
        retry_policy=retry_pol,
        notification_registry=notification_registry,
        attempt_log=attempt_log,
        retry_queue=retry_queue,
    )


# ── Filtered notifications ────────────────────────────────────────────────────


class TestFilteredNotifications:
    """Verify processor behaviour when the filter engine blocks a notification — the attempt is logged but no adapter call or retry is scheduled."""

    async def test_filtered_task_logs_attempt(self):
        """A filtered notification still produces an attempt log entry."""
        proc = _make_processor(filter_engine=_filter_blocked())
        task = make_task()
        await proc.process(task)
        proc._attempt_log.log_attempt.assert_awaited_once()

    async def test_filtered_task_does_not_call_adapter(self):
        """The channel adapter is never called when the notification is filtered."""
        adapter = _adapter()
        proc = _make_processor(filter_engine=_filter_blocked(), adapter=adapter)
        task = make_task()
        await proc.process(task)
        adapter.deliver.assert_not_awaited()

    async def test_filtered_task_does_not_schedule_retry(self):
        """Filtered notifications are not retried; no retry is scheduled."""
        proc = _make_processor(filter_engine=_filter_blocked())
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_not_awaited()

    async def test_filtered_attempt_has_filtered_status(self):
        """The logged attempt for a filtered notification carries FILTERED status."""
        proc = _make_processor(filter_engine=_filter_blocked())
        task = make_task()
        await proc.process(task)
        logged_attempt = proc._attempt_log.log_attempt.call_args[0][0]
        assert logged_attempt.status == DeliveryStatus.FILTERED


# ── Rate-limited notifications ────────────────────────────────────────────────


class TestRateLimitedNotifications:
    """Verify processor behaviour when the rate limiter blocks a notification — the attempt is logged, a retry is scheduled, but the adapter is not called."""

    async def test_rate_limited_logs_attempt(self):
        """A rate-limited notification still produces an attempt log entry."""
        proc = _make_processor(rate_limiter=_rate_limiter_deny())
        task = make_task()
        await proc.process(task)
        proc._attempt_log.log_attempt.assert_awaited_once()

    async def test_rate_limited_schedules_retry(self):
        """A rate-limited notification is rescheduled for a future retry."""
        proc = _make_processor(rate_limiter=_rate_limiter_deny())
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_awaited_once()

    async def test_rate_limited_attempt_status(self):
        """The logged attempt for a rate-limited notification carries RATE_LIMITED status."""
        proc = _make_processor(rate_limiter=_rate_limiter_deny("RECIPIENT"))
        task = make_task()
        await proc.process(task)
        logged = proc._attempt_log.log_attempt.call_args[0][0]
        assert logged.status == DeliveryStatus.RATE_LIMITED

    async def test_rate_limited_does_not_call_adapter(self):
        """The channel adapter is never called when the rate limiter blocks the notification."""
        adapter = _adapter()
        proc = _make_processor(rate_limiter=_rate_limiter_deny(), adapter=adapter)
        task = make_task()
        await proc.process(task)
        adapter.deliver.assert_not_awaited()


# ── Successful delivery ───────────────────────────────────────────────────────


class TestSuccessfulDelivery:
    """Verify processor behaviour on a successful delivery — the adapter is called and no retry is scheduled."""

    async def test_success_logs_attempt(self):
        """A successful delivery produces an attempt log entry."""
        proc = _make_processor()
        task = make_task()
        await proc.process(task)
        proc._attempt_log.log_attempt.assert_awaited()

    async def test_success_calls_adapter(self):
        """The channel adapter's deliver() is called exactly once on a successful attempt."""
        adapter = _adapter(status=DeliveryStatus.SUCCESS)
        proc = _make_processor(adapter=adapter)
        task = make_task()
        await proc.process(task)
        adapter.deliver.assert_awaited_once()

    async def test_success_does_not_schedule_new_retry(self):
        """No retry is scheduled after a successful delivery."""
        proc = _make_processor()
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_not_awaited()


# ── Transient failure from adapter ───────────────────────────────────────────


class TestTransientFailure:
    """Verify that a transient adapter failure causes the processor to schedule a retry."""

    async def test_transient_failure_schedules_retry(self):
        """A TRANSIENT_FAIL adapter result causes the processor to enqueue a retry."""
        proc = _make_processor(adapter=_adapter(status=DeliveryStatus.TRANSIENT_FAIL))
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_awaited()

    async def test_transient_failure_logs_attempt(self):
        """A transient failure still produces an attempt log entry."""
        proc = _make_processor(adapter=_adapter(status=DeliveryStatus.TRANSIENT_FAIL))
        task = make_task()
        await proc.process(task)
        proc._attempt_log.log_attempt.assert_awaited()


# ── Permanent failure from adapter ───────────────────────────────────────────


class TestPermanentFailure:
    """Verify that a permanent adapter failure does not schedule a retry."""

    async def test_permanent_failure_does_not_schedule_retry(self):
        """A PERMANENT_FAIL adapter result does not trigger any retry scheduling."""
        proc = _make_processor(adapter=_adapter(status=DeliveryStatus.PERMANENT_FAIL))
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_not_awaited()

    async def test_permanent_failure_logs_attempt(self):
        """A permanent failure still produces an attempt log entry."""
        proc = _make_processor(adapter=_adapter(status=DeliveryStatus.PERMANENT_FAIL))
        task = make_task()
        await proc.process(task)
        proc._attempt_log.log_attempt.assert_awaited()


# ── Missing adapter ───────────────────────────────────────────────────────────


class TestMissingAdapter:
    """Verify that a missing channel adapter (channel_manager returns None) is treated as a transient failure."""

    async def test_missing_adapter_treated_as_transient(self):
        """When the channel manager returns no adapter, delivery is treated as a transient failure and a retry is scheduled."""
        cm = MagicMock()
        cm.get_adapter = MagicMock(return_value=None)
        proc = _make_processor(channel_manager=cm)
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_awaited()

    async def test_missing_adapter_logs_attempt(self):
        """A missing adapter failure still produces an attempt log entry."""
        cm = MagicMock()
        cm.get_adapter = MagicMock(return_value=None)
        proc = _make_processor(channel_manager=cm)
        task = make_task()
        await proc.process(task)
        proc._attempt_log.log_attempt.assert_awaited()


# ── Contact info validation (RECIPIENT scope) ─────────────────────────────────


class TestContactInfoValidation:
    """Verify that contact-info requirements (e.g. email) are enforced for RECIPIENT-scoped channels but skipped for SYSTEM-scoped channels."""

    async def test_system_channel_skips_contact_validation(self):
        """SYSTEM-scoped channels do not require contact info."""
        adapter = _adapter()
        adapter.get_requirements.return_value = {"requires_email": True}
        proc = _make_processor(adapter=adapter)
        task = make_task(
            channel_info=ChannelInfo(
                id="notify.persistent_notification",
                label="Persistent",
                scope=ChannelScope.SYSTEM,
            )
        )
        # Should not fail even though contact_info has no email
        await proc.process(task)
        adapter.deliver.assert_awaited()

    async def test_recipient_channel_fails_when_missing_email(self):
        """RECIPIENT-scoped channel requiring email blocks delivery permanently."""
        adapter = _adapter()
        adapter.get_requirements.return_value = {"requires_email": True}
        proc = _make_processor(adapter=adapter)
        task = make_task(
            channel_info=ChannelInfo(
                id="notify.mobile_app_phone",
                label="Phone",
                scope=ChannelScope.RECIPIENT,
            ),
            # contact_info has no email
        )
        await proc.process(task)
        # Should NOT have called adapter.deliver
        adapter.deliver.assert_not_awaited()

    async def test_recipient_channel_passes_when_email_present(self):
        """RECIPIENT-scoped channel with required email succeeds when email provided."""
        adapter = _adapter()
        adapter.get_requirements.return_value = {"requires_email": True}
        proc = _make_processor(adapter=adapter)
        task = make_task(
            channel_info=ChannelInfo(
                id="notify.email_channel",
                label="Email",
                scope=ChannelScope.RECIPIENT,
            ),
            contact_info=RecipientContactInfo(
                email_address="user@example.com",
                phone_number=None,
            ),
        )
        await proc.process(task)
        adapter.deliver.assert_awaited()

    async def test_recipient_channel_fails_when_missing_phone(self):
        """RECIPIENT-scoped channel requiring phone blocks delivery permanently when phone is absent."""
        adapter = _adapter()
        adapter.get_requirements.return_value = {"requires_phone": True}
        proc = _make_processor(adapter=adapter)
        task = make_task(
            channel_info=ChannelInfo(
                id="notify.signal_user",
                label="Signal",
                scope=ChannelScope.RECIPIENT,
            ),
            contact_info=RecipientContactInfo(
                email_address=None,
                phone_number=None,  # missing phone
            ),
        )
        await proc.process(task)
        adapter.deliver.assert_not_awaited()


# ── ServiceNotFound during delivery ──────────────────────────────────────────


class TestServiceNotFoundDuringDelivery:
    """Verify that HomeAssistant ServiceNotFound raised by an adapter is treated as a transient failure."""

    async def test_service_not_found_treated_as_transient(self):
        """ServiceNotFound from adapter.deliver() schedules a retry rather than permanently failing."""
        adapter = _adapter()
        adapter.deliver = AsyncMock(
            side_effect=ServiceNotFound("notify", "send_message")
        )
        proc = _make_processor(adapter=adapter)
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_awaited()

    async def test_service_not_found_logs_attempt(self):
        """ServiceNotFound still produces an attempt log entry."""
        adapter = _adapter()
        adapter.deliver = AsyncMock(
            side_effect=ServiceNotFound("notify", "send_message")
        )
        proc = _make_processor(adapter=adapter)
        task = make_task()
        await proc.process(task)
        proc._attempt_log.log_attempt.assert_awaited()


# ── Unexpected exception from adapter ────────────────────────────────────────


class TestAdapterRaisesUnexpectedException:
    """Verify that an unexpected exception from adapter.deliver() is converted to a transient failure."""

    async def test_unexpected_exception_treated_as_transient(self):
        """A generic RuntimeError from adapter.deliver() is caught, result becomes TRANSIENT_FAIL, and a retry is scheduled."""
        adapter = _adapter()
        adapter.deliver = AsyncMock(side_effect=RuntimeError("unexpected boom"))
        proc = _make_processor(adapter=adapter)
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_awaited()

    async def test_unexpected_exception_logs_attempt(self):
        """An unexpected adapter exception still produces an attempt log entry."""
        adapter = _adapter()
        adapter.deliver = AsyncMock(side_effect=RuntimeError("unexpected boom"))
        proc = _make_processor(adapter=adapter)
        task = make_task()
        await proc.process(task)
        proc._attempt_log.log_attempt.assert_awaited()


# ── Outer defensive exception handler ────────────────────────────────────────


class TestProcessUnhandledDefensiveException:
    """Verify that an unexpected exception raised inside _execute_delivery is caught by process()'s outer guard."""

    async def test_outer_exception_triggers_transient_failure(self):
        """When _execute_delivery raises unexpectedly, process() catches it and calls _handle_transient_failure."""
        proc = _make_processor()
        # Patch _execute_delivery to simulate an unhandled error
        proc._execute_delivery = AsyncMock(side_effect=RuntimeError("internal crash"))
        task = make_task()
        await proc.process(task)
        # _handle_transient_failure logs the attempt, so attempt_log should have been called
        proc._attempt_log.log_attempt.assert_awaited()


# ── _schedule_retry edge cases ────────────────────────────────────────────────


class TestScheduleRetry:
    """Verify the retry-scheduling logic inside the processor."""

    async def test_max_retries_exceeded_no_retry(self):
        """When attempt count already exceeds policy.retry_attempts, no retry is scheduled."""
        proc = _make_processor(adapter=_adapter(status=DeliveryStatus.TRANSIENT_FAIL))
        # count_attempts returns 4 which is > default policy retry_attempts (3)
        proc._attempt_log.count_attempts = AsyncMock(return_value=4)
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_not_awaited()

    async def test_policy_returns_no_retry_no_schedule(self):
        """When RetryPolicy.evaluate() returns should_retry=False, no retry is scheduled."""
        proc = _make_processor(adapter=_adapter(status=DeliveryStatus.TRANSIENT_FAIL))
        proc._attempt_log.count_attempts = AsyncMock(return_value=1)
        # Override after construction so _make_processor's default doesn't apply
        proc._retry_policy.evaluate = MagicMock(
            return_value=MagicMock(should_retry=False, next_run_at=None)
        )
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_not_awaited()

    async def test_policy_returns_none_next_run_no_schedule(self):
        """When RetryPolicy.evaluate() returns should_retry=True but next_run_at=None, no retry is scheduled."""
        proc = _make_processor(adapter=_adapter(status=DeliveryStatus.TRANSIENT_FAIL))
        proc._attempt_log.count_attempts = AsyncMock(return_value=1)
        # Override after construction so _make_processor's default doesn't apply
        proc._retry_policy.evaluate = MagicMock(
            return_value=MagicMock(should_retry=True, next_run_at=None)
        )
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_not_awaited()
