"""Tests for NotificationDeliveryProcessor."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from custom_components.ans.delivery.processor import NotificationDeliveryProcessor
from custom_components.ans.models import (
    ChannelInfo,
    ChannelScope,
    DeliveryResult,
    DeliveryStatus,
    FilterDecision,
    FilterDecisionType,
    FilterReason,
)

from .conftest import make_task

# ── fixture helpers ───────────────────────────────────────────────────────────


def _filter_allowed() -> MagicMock:
    fe = MagicMock()
    fe.evaluate.return_value = FilterDecision(
        decision=FilterDecisionType.ALLOWED,
        reason=FilterReason.NORMAL,
    )
    return fe


def _filter_blocked(reason=FilterReason.TYPE_NOT_ALLOWED) -> MagicMock:
    fe = MagicMock()
    fe.evaluate.return_value = FilterDecision(
        decision=FilterDecisionType.FILTERED,
        reason=reason,
        details={"type": "INFO"},
    )
    return fe


def _rate_limiter_allow() -> MagicMock:
    rl = MagicMock()
    rl.allow.return_value = (True, None)
    return rl


def _rate_limiter_deny(limit_type="GLOBAL") -> MagicMock:
    rl = MagicMock()
    rl.allow.return_value = (False, limit_type)
    return rl


def _adapter(*, status: DeliveryStatus = DeliveryStatus.SUCCESS) -> MagicMock:
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
    async def test_filtered_task_logs_attempt(self):
        proc = _make_processor(filter_engine=_filter_blocked())
        task = make_task()
        await proc.process(task)
        proc._attempt_log.log_attempt.assert_awaited_once()

    async def test_filtered_task_does_not_call_adapter(self):
        adapter = _adapter()
        proc = _make_processor(filter_engine=_filter_blocked(), adapter=adapter)
        task = make_task()
        await proc.process(task)
        adapter.deliver.assert_not_awaited()

    async def test_filtered_task_does_not_schedule_retry(self):
        proc = _make_processor(filter_engine=_filter_blocked())
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_not_awaited()

    async def test_filtered_attempt_has_filtered_status(self):
        proc = _make_processor(filter_engine=_filter_blocked())
        task = make_task()
        await proc.process(task)
        logged_attempt = proc._attempt_log.log_attempt.call_args[0][0]
        assert logged_attempt.status == DeliveryStatus.FILTERED


# ── Rate-limited notifications ────────────────────────────────────────────────


class TestRateLimitedNotifications:
    async def test_rate_limited_logs_attempt(self):
        proc = _make_processor(rate_limiter=_rate_limiter_deny())
        task = make_task()
        await proc.process(task)
        proc._attempt_log.log_attempt.assert_awaited_once()

    async def test_rate_limited_schedules_retry(self):
        proc = _make_processor(rate_limiter=_rate_limiter_deny())
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_awaited_once()

    async def test_rate_limited_attempt_status(self):
        proc = _make_processor(rate_limiter=_rate_limiter_deny("RECIPIENT"))
        task = make_task()
        await proc.process(task)
        logged = proc._attempt_log.log_attempt.call_args[0][0]
        assert logged.status == DeliveryStatus.RATE_LIMITED

    async def test_rate_limited_does_not_call_adapter(self):
        adapter = _adapter()
        proc = _make_processor(rate_limiter=_rate_limiter_deny(), adapter=adapter)
        task = make_task()
        await proc.process(task)
        adapter.deliver.assert_not_awaited()


# ── Successful delivery ───────────────────────────────────────────────────────


class TestSuccessfulDelivery:
    async def test_success_logs_attempt(self):
        proc = _make_processor()
        task = make_task()
        await proc.process(task)
        proc._attempt_log.log_attempt.assert_awaited()

    async def test_success_calls_adapter(self):
        adapter = _adapter(status=DeliveryStatus.SUCCESS)
        proc = _make_processor(adapter=adapter)
        task = make_task()
        await proc.process(task)
        adapter.deliver.assert_awaited_once()

    async def test_success_does_not_schedule_new_retry(self):
        proc = _make_processor()
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_not_awaited()


# ── Transient failure from adapter ───────────────────────────────────────────


class TestTransientFailure:
    async def test_transient_failure_schedules_retry(self):
        proc = _make_processor(adapter=_adapter(status=DeliveryStatus.TRANSIENT_FAIL))
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_awaited()

    async def test_transient_failure_logs_attempt(self):
        proc = _make_processor(adapter=_adapter(status=DeliveryStatus.TRANSIENT_FAIL))
        task = make_task()
        await proc.process(task)
        proc._attempt_log.log_attempt.assert_awaited()


# ── Permanent failure from adapter ───────────────────────────────────────────


class TestPermanentFailure:
    async def test_permanent_failure_does_not_schedule_retry(self):
        proc = _make_processor(adapter=_adapter(status=DeliveryStatus.PERMANENT_FAIL))
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_not_awaited()

    async def test_permanent_failure_logs_attempt(self):
        proc = _make_processor(adapter=_adapter(status=DeliveryStatus.PERMANENT_FAIL))
        task = make_task()
        await proc.process(task)
        proc._attempt_log.log_attempt.assert_awaited()


# ── Missing adapter ───────────────────────────────────────────────────────────


class TestMissingAdapter:
    async def test_missing_adapter_treated_as_transient(self):
        cm = MagicMock()
        cm.get_adapter = MagicMock(return_value=None)
        proc = _make_processor(channel_manager=cm)
        task = make_task()
        await proc.process(task)
        proc._retry_queue.schedule_retry.assert_awaited()

    async def test_missing_adapter_logs_attempt(self):
        cm = MagicMock()
        cm.get_adapter = MagicMock(return_value=None)
        proc = _make_processor(channel_manager=cm)
        task = make_task()
        await proc.process(task)
        proc._attempt_log.log_attempt.assert_awaited()


# ── Contact info validation (RECIPIENT scope) ─────────────────────────────────


class TestContactInfoValidation:
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
        from custom_components.ans.models import RecipientContactInfo

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
