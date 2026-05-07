"""Tests for NotificationDeliveryProcessor."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import ServiceNotFound

from custom_components.ans.const import (
    EVENT_NOTIFICATION_DELIVERED,
    EVENT_NOTIFICATION_FAILED,
    EVENT_NOTIFICATION_FILTERED,
    EVENT_NOTIFICATION_RATE_LIMITED,
)
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
    TaskOutcome,
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
    on_terminal_outcome=None,
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
        on_terminal_outcome=on_terminal_outcome,
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
        callback = MagicMock()
        hass = _make_hass_with_bus()
        proc = _make_processor(
            adapter=_adapter(status=DeliveryStatus.TRANSIENT_FAIL),
            hass=hass,
            on_terminal_outcome=callback,
        )
        # count_attempts returns 4 which is > default policy retry_attempts (3)
        proc._attempt_log.count_attempts = AsyncMock(return_value=4)
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_not_awaited()
        fired = [c[0][0] for c in hass.bus.async_fire.call_args_list]
        assert EVENT_NOTIFICATION_FAILED in fired
        callback.assert_called_once_with(
            str(task.payload.notification_id), TaskOutcome.FAILED
        )

    async def test_policy_returns_no_retry_no_schedule(self):
        """When RetryPolicy.evaluate() returns should_retry=False, no retry is scheduled."""
        callback = MagicMock()
        hass = _make_hass_with_bus()
        proc = _make_processor(
            adapter=_adapter(status=DeliveryStatus.TRANSIENT_FAIL),
            hass=hass,
            on_terminal_outcome=callback,
        )
        proc._attempt_log.count_attempts = AsyncMock(return_value=1)
        # Override after construction so _make_processor's default doesn't apply
        proc._retry_policy.evaluate = MagicMock(
            return_value=MagicMock(should_retry=False, next_run_at=None)
        )
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_not_awaited()
        fired = [c[0][0] for c in hass.bus.async_fire.call_args_list]
        assert EVENT_NOTIFICATION_FAILED in fired
        callback.assert_called_once_with(
            str(task.payload.notification_id), TaskOutcome.FAILED
        )

    async def test_policy_returns_none_next_run_no_schedule(self):
        """When RetryPolicy.evaluate() returns should_retry=True but next_run_at=None, no retry is scheduled."""
        callback = MagicMock()
        hass = _make_hass_with_bus()
        proc = _make_processor(
            adapter=_adapter(status=DeliveryStatus.TRANSIENT_FAIL),
            hass=hass,
            on_terminal_outcome=callback,
        )
        proc._attempt_log.count_attempts = AsyncMock(return_value=1)
        # Override after construction so _make_processor's default doesn't apply
        proc._retry_policy.evaluate = MagicMock(
            return_value=MagicMock(should_retry=True, next_run_at=None)
        )
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_not_awaited()
        fired = [c[0][0] for c in hass.bus.async_fire.call_args_list]
        assert EVENT_NOTIFICATION_FAILED in fired
        callback.assert_called_once_with(
            str(task.payload.notification_id), TaskOutcome.FAILED
        )

    async def test_retry_exhaustion_logs_single_attempt(self):
        """When retries are exhausted, only one attempt record (TRANSIENT_FAIL) is logged — no phantom attempt."""
        proc = _make_processor(adapter=_adapter(status=DeliveryStatus.TRANSIENT_FAIL))
        proc._attempt_log.count_attempts = AsyncMock(return_value=4)
        task = make_task()
        await proc.process(task)
        # Only the original TRANSIENT_FAIL attempt — no phantom PERMANENT_FAIL attempt created
        assert proc._attempt_log.log_attempt.await_count == 1


# ── Rate-limit retry exhaustion ───────────────────────────────────────────────


class TestRateLimitExhaustion:
    """Verify that retries exhausted after rate-limiting escalates to a terminal permanent failure."""

    async def test_rate_limited_then_retries_exhausted_fires_failed_event(self):
        """When rate-limited and _schedule_retry returns None, ans_notification_failed fires."""
        callback = MagicMock()
        hass = _make_hass_with_bus()
        proc = _make_processor(
            rate_limiter=_rate_limiter_deny("GLOBAL"),
            hass=hass,
            on_terminal_outcome=callback,
        )
        proc._attempt_log.count_attempts = AsyncMock(return_value=10)
        task = make_task()
        await proc.process(task)
        fired = [c[0][0] for c in hass.bus.async_fire.call_args_list]
        assert EVENT_NOTIFICATION_RATE_LIMITED in fired
        assert EVENT_NOTIFICATION_FAILED in fired
        callback.assert_called_once_with(
            str(task.payload.notification_id), TaskOutcome.FAILED
        )

    async def test_rate_limited_with_retry_available_no_failed_event(self):
        """When rate-limited but a retry is available, ans_notification_failed does NOT fire."""
        callback = MagicMock()
        hass = _make_hass_with_bus()
        proc = _make_processor(
            rate_limiter=_rate_limiter_deny("GLOBAL"),
            hass=hass,
            on_terminal_outcome=callback,
        )
        # count_attempts=0: well within retry budget, retry will be scheduled
        proc._attempt_log.count_attempts = AsyncMock(return_value=0)
        task = make_task()
        await proc.process(task)
        fired = [c[0][0] for c in hass.bus.async_fire.call_args_list]
        assert EVENT_NOTIFICATION_RATE_LIMITED in fired
        assert EVENT_NOTIFICATION_FAILED not in fired
        callback.assert_not_called()


# ── Delivery outcome events ───────────────────────────────────────────────────


def _make_hass_with_bus() -> MagicMock:
    """Return a mock hass whose bus.async_fire is a regular MagicMock."""
    hass = MagicMock()
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()
    return hass


class TestDeliveryOutcomeEvents:
    """Verify that hass bus events are fired at each delivery outcome."""

    async def test_delivered_event_fires_on_success(self):
        """ans_notification_delivered is fired exactly once on a successful delivery."""
        hass = _make_hass_with_bus()
        proc = _make_processor(
            hass=hass, adapter=_adapter(status=DeliveryStatus.SUCCESS)
        )
        task = make_task()
        await proc.process(task)
        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args[0]
        assert event_name == EVENT_NOTIFICATION_DELIVERED
        assert payload["notification_id"] == str(task.payload.notification_id)
        assert payload["recipient_id"] == task.recipient_id
        assert payload["channel_id"] == task.channel_info.id
        assert payload["source"] == task.payload.source
        assert payload["type"] == task.payload.type.value
        assert "attempt_number" in payload
        assert "remote_id" in payload

    async def test_filtered_event_fires_on_filter(self):
        """ans_notification_filtered is fired exactly once when the filter engine blocks a notification."""
        hass = _make_hass_with_bus()
        proc = _make_processor(
            hass=hass, filter_engine=_filter_blocked(FilterReason.DND_ACTIVE)
        )
        task = make_task()
        await proc.process(task)
        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args[0]
        assert event_name == EVENT_NOTIFICATION_FILTERED
        assert payload["filter_reason"] == FilterReason.DND_ACTIVE.value
        assert payload["notification_id"] == str(task.payload.notification_id)

    async def test_failed_event_fires_on_permanent_fail(self):
        """ans_notification_failed is fired exactly once when the adapter returns PERMANENT_FAIL."""
        hass = _make_hass_with_bus()
        proc = _make_processor(
            hass=hass, adapter=_adapter(status=DeliveryStatus.PERMANENT_FAIL)
        )
        task = make_task()
        await proc.process(task)
        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args[0]
        assert event_name == EVENT_NOTIFICATION_FAILED
        assert payload["notification_id"] == str(task.payload.notification_id)
        assert "error" in payload
        assert "attempt_number" in payload

    async def test_failed_event_fires_on_missing_contact_info(self):
        """ans_notification_failed is fired when contact info validation fails (via _handle_permanent_failure)."""
        hass = _make_hass_with_bus()
        adapter = MagicMock()
        adapter.get_requirements.return_value = {"requires_email": True}
        adapter.deliver = AsyncMock(
            return_value=DeliveryResult(status=DeliveryStatus.SUCCESS, error=None)
        )
        cm = MagicMock()
        cm.get_adapter = MagicMock(return_value=adapter)
        # Task with RECIPIENT scope and no email in contact_info
        task = make_task(
            channel_info=ChannelInfo(
                id="notify.signal",
                label="Signal",
                integration="signal",
                scope=ChannelScope.RECIPIENT,
            ),
            contact_info=RecipientContactInfo(
                email_address=None,
                phone_number=None,
                mobile_device_id=None,
            ),
        )
        proc = _make_processor(hass=hass, channel_manager=cm)
        await proc.process(task)
        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args[0]
        assert event_name == EVENT_NOTIFICATION_FAILED
        assert "missing_required_contact_info" in (payload["error"] or "")

    async def test_rate_limited_event_fires_when_rate_limited(self):
        """ans_notification_rate_limited is fired with limit_type and retry_at when rate-limited."""
        hass = _make_hass_with_bus()
        proc = _make_processor(hass=hass, rate_limiter=_rate_limiter_deny("GLOBAL"))
        task = make_task()
        await proc.process(task)
        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args[0]
        assert event_name == EVENT_NOTIFICATION_RATE_LIMITED
        assert payload["limit_type"] == "GLOBAL"
        assert payload["retry_at"] is not None  # default mock returns a valid datetime
        assert payload["notification_id"] == str(task.payload.notification_id)

    async def test_rate_limited_event_fires_with_null_retry_at_when_no_retry(self):
        """ans_notification_rate_limited fires with retry_at=None when max retries are exceeded."""
        hass = _make_hass_with_bus()
        proc = _make_processor(hass=hass, rate_limiter=_rate_limiter_deny("RECIPIENT"))
        # Exceeded max retries: count_attempts > policy.retry_attempts (default 3)
        proc._attempt_log.count_attempts = AsyncMock(return_value=10)
        task = make_task()
        await proc.process(task)
        # Rate-limited fires first; failed fires second (retries exhausted).
        # Find the rate-limited call specifically.
        rate_limited_calls = [
            c
            for c in hass.bus.async_fire.call_args_list
            if c[0][0] == EVENT_NOTIFICATION_RATE_LIMITED
        ]
        assert len(rate_limited_calls) == 1
        payload = rate_limited_calls[0][0][1]
        assert payload["retry_at"] is None

    async def test_no_failed_event_on_transient_fail(self):
        """ans_notification_failed is NOT fired for a transient (retryable) failure."""
        hass = _make_hass_with_bus()
        proc = _make_processor(
            hass=hass, adapter=_adapter(status=DeliveryStatus.TRANSIENT_FAIL)
        )
        task = make_task()
        await proc.process(task)
        fired_names = [call[0][0] for call in hass.bus.async_fire.call_args_list]
        assert EVENT_NOTIFICATION_FAILED not in fired_names

    async def test_events_fire_even_when_audit_logging_disabled(self):
        """Delivery outcome events fire regardless of the audit logging setting (they are independent)."""
        hass = _make_hass_with_bus()
        # Simulate audit log that always no-ops (disabled-like behaviour)
        proc = _make_processor(
            hass=hass, adapter=_adapter(status=DeliveryStatus.SUCCESS)
        )
        proc._attempt_log.log_attempt = AsyncMock()  # still called but irrelevant
        task = make_task()
        await proc.process(task)
        # Event must still fire
        fired_names = [call[0][0] for call in hass.bus.async_fire.call_args_list]
        assert EVENT_NOTIFICATION_DELIVERED in fired_names


class TestTerminalOutcomeCallback:
    """Verify on_terminal_outcome callback is invoked at every terminal state."""

    async def test_callback_called_on_success(self):
        """Callback receives (notification_id, TaskOutcome.DELIVERED) on successful delivery."""
        callback = MagicMock()
        proc = _make_processor(
            adapter=_adapter(status=DeliveryStatus.SUCCESS),
            on_terminal_outcome=callback,
        )
        task = make_task()
        await proc.process(task)
        callback.assert_called_once_with(
            str(task.payload.notification_id), TaskOutcome.DELIVERED
        )

    async def test_callback_called_on_permanent_failure(self):
        """Callback receives (notification_id, TaskOutcome.FAILED) on permanent adapter failure."""
        callback = MagicMock()
        proc = _make_processor(
            adapter=_adapter(status=DeliveryStatus.PERMANENT_FAIL),
            on_terminal_outcome=callback,
        )
        task = make_task()
        await proc.process(task)
        callback.assert_called_once_with(
            str(task.payload.notification_id), TaskOutcome.FAILED
        )

    async def test_callback_called_on_filtered(self):
        """Callback receives (notification_id, TaskOutcome.FILTERED) when filter engine blocks delivery."""
        callback = MagicMock()
        proc = _make_processor(
            filter_engine=_filter_blocked(FilterReason.DND_ACTIVE),
            on_terminal_outcome=callback,
        )
        task = make_task()
        await proc.process(task)
        callback.assert_called_once_with(
            str(task.payload.notification_id), TaskOutcome.FILTERED
        )

    async def test_no_callback_does_not_raise(self):
        """Processor works normally when on_terminal_outcome is None (default)."""
        proc = _make_processor(
            adapter=_adapter(status=DeliveryStatus.SUCCESS),
            on_terminal_outcome=None,
        )
        task = make_task()
        # Must not raise
        await proc.process(task)
