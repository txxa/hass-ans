"""Integration tests for the ANS notification system.

Tests end-to-end notification delivery flow through the real
FilterEngine → RateLimiter → Processor → ChannelAdapter pipeline.
All HA-bound dependencies (hass, registries, persistence) are replaced
with lightweight fakes so no Home Assistant instance is needed.
"""

from __future__ import annotations

import asyncio
from datetime import time, timedelta
from unittest.mock import AsyncMock, MagicMock

from ..channels.base import (
    AdapterMetadata,
    AdapterType,
    DeliveryAdapter,
    DeliveryOptions,
)
from ..delivery.filter_engine import FilterEngine
from ..delivery.processor import NotificationDeliveryProcessor
from ..delivery.queue import NotificationDeliveryTaskQueue
from ..delivery.rate_limiter import RateLimiter
from ..delivery.retry_scheduler import RetryPolicy
from ..models import (
    ChannelScope,
    DeliveryResult,
    DoNotDisturbConfig,
    NotificationCriticality,
    NotificationDeliveryTask,
    NotificationPayload,
    NotificationType,
    RecipientContactInfo,
)
from .conftest import make_channel_info, make_payload, make_policy, make_task

# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------


class MockDeliveryAdapter(DeliveryAdapter):
    """Minimal adapter that records delivery attempts without touching HA."""

    _CHANNEL_PREFIX = "mock"

    def __init__(self, *, fail_count: int = 0) -> None:
        """Initialize adapter.

        Args:
            fail_count: Number of times to return TRANSIENT_FAIL before succeeding.

        """
        self.delivered_payloads: list[NotificationPayload] = []
        self.attempt_count = 0
        self.fail_count = fail_count

    # -- DeliveryAdapter abstract interface -----------------------------------

    @classmethod
    def get_metadata(cls) -> AdapterMetadata:
        """Return adapter metadata."""
        return AdapterMetadata(
            adapter_type=AdapterType.STATIC,
            channel_prefix=cls._CHANNEL_PREFIX,
        )

    @classmethod
    def matches_channel(cls, channel_id: str) -> bool:
        """Return True for any channel id starting with 'mock'."""
        return channel_id.startswith(cls._CHANNEL_PREFIX)

    @classmethod
    def extract_variant(cls, channel_id: str) -> str | None:
        """No variant for mock channels."""
        return None

    @classmethod
    def get_requirements(cls) -> dict:
        """No contact-info requirements."""
        return {}

    @property
    def channel(self) -> str:
        """Return the mock channel identifier."""
        return "mock.test"

    async def deliver(
        self,
        *,
        payload: NotificationPayload,
        contact_info: RecipientContactInfo,
        idempotency_key: str,
        job_id: str,
        options: DeliveryOptions | None = None,
    ) -> DeliveryResult:
        """Simulate delivery, honouring fail_count before succeeding."""
        self.attempt_count += 1
        if self.attempt_count <= self.fail_count:
            return self.transient_failure(error="Simulated transient failure")
        self.delivered_payloads.append(payload)
        return self.success(remote_id=f"mock-{idempotency_key}")


class MockEmailAdapter(MockDeliveryAdapter):
    """Adapter variant that declares an email address requirement."""

    @classmethod
    def get_requirements(cls) -> dict:
        """Require an email address in the recipient contact info."""
        return {"requires_email": True}


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_processor(
    adapter: MockDeliveryAdapter,
    *,
    rate_limiter: RateLimiter | None = None,
    attempt_count: int = 0,
) -> tuple[NotificationDeliveryProcessor, AsyncMock]:
    """Build a fully wired processor backed by *adapter*.

    Returns the processor and the retry_queue mock so callers can assert on
    scheduled retries when needed.
    """
    attempt_log = AsyncMock()
    attempt_log.get_next_attempt_number.return_value = 1
    attempt_log.log_attempt.return_value = None
    attempt_log.count_attempts.return_value = attempt_count

    retry_queue = AsyncMock()

    channel_manager = MagicMock()
    channel_manager.get_adapter.return_value = adapter

    processor = NotificationDeliveryProcessor(
        filter_engine=FilterEngine(),
        rate_limiter=rate_limiter or RateLimiter(),
        channel_manager=channel_manager,
        hass=MagicMock(),
        retry_policy=RetryPolicy(max_attempts=3, base_delay=timedelta(seconds=1)),
        notification_registry=AsyncMock(),
        attempt_log=attempt_log,
        retry_queue=retry_queue,
    )
    return processor, retry_queue


def _system_task(**overrides) -> NotificationDeliveryTask:
    """Task whose channel has SYSTEM scope (contact-info validation is skipped)."""
    return make_task(
        channel_info=make_channel_info(
            id="mock.test", label="Mock", scope=ChannelScope.SYSTEM
        ),
        **overrides,
    )


def _recipient_task(**overrides) -> NotificationDeliveryTask:
    """Task whose channel has RECIPIENT scope (contact-info validation is active)."""
    return make_task(
        channel_info=make_channel_info(
            id="mock.test", label="Mock", scope=ChannelScope.RECIPIENT
        ),
        **overrides,
    )


# ---------------------------------------------------------------------------
# Tests: processor delivery pipeline
# ---------------------------------------------------------------------------


class TestProcessorDeliveryFlow:
    """End-to-end tests through FilterEngine → RateLimiter → Processor → Adapter."""

    async def test_successful_delivery(self):
        """Happy path: notification passes all checks and is delivered once."""
        adapter = MockDeliveryAdapter()
        processor, _ = _make_processor(adapter)
        task = _system_task()

        await processor.process(task)

        assert len(adapter.delivered_payloads) == 1
        assert (
            adapter.delivered_payloads[0].notification_id
            == task.payload.notification_id
        )

    async def test_filtered_by_type(self):
        """Notification whose type is not in the policy allowlist is silently dropped."""
        adapter = MockDeliveryAdapter()
        processor, _ = _make_processor(adapter)
        task = _system_task(
            payload=make_payload(type=NotificationType.WARNING),
            policy=make_policy(allowed_types=[NotificationType.INFO]),
        )

        await processor.process(task)

        assert len(adapter.delivered_payloads) == 0
        assert adapter.attempt_count == 0

    async def test_filtered_by_blocked_source(self):
        """Notification whose source matches the blocked-source regex is dropped."""
        adapter = MockDeliveryAdapter()
        processor, _ = _make_processor(adapter)
        task = _system_task(
            payload=make_payload(source="restricted_service"),
            policy=make_policy(blocked_sources_regex=r"restricted_.*"),
        )

        await processor.process(task)

        assert len(adapter.delivered_payloads) == 0
        assert adapter.attempt_count == 0

    async def test_filtered_by_dnd_window(self):
        """Notification blocked by an all-day DND window is not delivered."""
        adapter = MockDeliveryAdapter()
        processor, _ = _make_processor(adapter)
        # 00:00–23:59 is active for every minute of the day
        dnd = DoNotDisturbConfig(
            start=time(0, 0),
            end=time(23, 59),
            allowed_sources_regex=None,
        )
        task = _system_task(policy=make_policy(dnd=dnd))

        await processor.process(task)

        assert len(adapter.delivered_payloads) == 0
        assert adapter.attempt_count == 0

    async def test_dnd_bypassed_for_allowed_criticality(self):
        """CRITICAL notifications pass through an active DND window."""
        adapter = MockDeliveryAdapter()
        processor, _ = _make_processor(adapter)
        dnd = DoNotDisturbConfig(
            start=time(0, 0),
            end=time(23, 59),
            allowed_sources_regex=None,
            allowed_criticalities=[NotificationCriticality.CRITICAL],
        )
        task = _system_task(
            payload=make_payload(criticality=NotificationCriticality.CRITICAL),
            policy=make_policy(dnd=dnd),
        )

        await processor.process(task)

        assert len(adapter.delivered_payloads) == 1

    async def test_rate_limited_on_second_delivery(self):
        """Second delivery to the same recipient exhausts the per-recipient token bucket."""
        rate_limiter = RateLimiter()
        adapter = MockDeliveryAdapter()
        processor, _ = _make_processor(adapter, rate_limiter=rate_limiter)

        # rate_limit=1 creates a bucket with exactly one token per window
        policy = make_policy(rate_limit=1, rate_limit_window=3600)
        task1 = _system_task(recipient_id="user_rl", policy=policy)
        task2 = _system_task(recipient_id="user_rl", policy=policy)

        await processor.process(task1)  # consumes the token → delivered
        await processor.process(task2)  # bucket empty → rate-limited

        assert len(adapter.delivered_payloads) == 1

    async def test_missing_contact_info_causes_permanent_fail(self):
        """RECIPIENT-scope delivery missing required email is permanently failed without calling the adapter."""
        adapter = MockEmailAdapter()
        processor, _ = _make_processor(adapter)
        task = _recipient_task(
            contact_info=RecipientContactInfo(email_address=None, phone_number=None),
        )

        await processor.process(task)

        assert len(adapter.delivered_payloads) == 0
        assert adapter.attempt_count == 0

    async def test_transient_failure_schedules_retry(self):
        """Adapter transient failure causes a retry to be scheduled; no delivery counted."""
        adapter = MockDeliveryAdapter(fail_count=1)
        processor, retry_queue = _make_processor(adapter)
        task = _system_task()

        await processor.process(task)

        assert len(adapter.delivered_payloads) == 0
        retry_queue.schedule_retry.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests: task queue
# ---------------------------------------------------------------------------


class TestQueueDeliveryFlow:
    """End-to-end tests for the task queue worker pool."""

    async def test_queue_delivers_multiple_tasks(self):
        """Queue processes all enqueued tasks and each one reaches the adapter."""
        adapter = MockDeliveryAdapter()
        processor, _ = _make_processor(adapter)

        queue = NotificationDeliveryTaskQueue(
            max_concurrency=2,
            processor_factory=lambda: processor,
            retry_queue=None,
        )

        tasks = [
            _system_task(
                recipient_id=f"user_{i}",
                payload=make_payload(message=f"Notification {i}"),
            )
            for i in range(3)
        ]

        await queue.start()
        try:
            for task in tasks:
                await queue.add_task(task)
            # Yield control so the worker loop can drain the queue
            await asyncio.sleep(0.3)
            assert len(adapter.delivered_payloads) == 3
        finally:
            await queue.stop()
