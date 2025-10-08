"""Integration tests for the ANS notification system.

Tests end-to-end notification delivery flow with mock adapters.
Can be run with standalone asyncio or pytest.
"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from ..delivery.base import DeliveryAdapter, DeliveryResult
from ..filter_engine import FilterEngine
from ..models import (
    NotificationCriticality,
    NotificationDeliveryTask,
    NotificationPayload,
    NotificationType,
    RecipientContactInfo,
    RecipientNotificationPolicy,
)
from ..processor import NotificationDeliveryProcessor
from ..queue import NotificationDeliveryTaskQueue
from ..rate_limiter import RateLimiter


class MockDeliveryAdapter(DeliveryAdapter):
    """Mock adapter for testing that records delivery attempts."""

    channel = "email"

    def __init__(self, fail_count: int = 0):
        """Initialize mock adapter.

        Args:
            fail_count: Number of times to fail before succeeding (0 = always succeed)

        """
        self.delivered_payloads: list[NotificationPayload] = []
        self.failed_payloads: list[NotificationPayload] = []
        self.attempt_count = 0
        self.fail_count = fail_count

    async def deliver(
        self,
        *,
        payload: NotificationPayload,
        contact_info: RecipientContactInfo,
        idempotency_key: str,
    ) -> DeliveryResult:
        """Simulate delivery."""
        self.attempt_count += 1

        if self.attempt_count <= self.fail_count:
            self.failed_payloads.append(payload)
            return self.transient_failure(error="Simulated transient failure")

        self.delivered_payloads.append(payload)
        return self.success(remote_id=f"mock-{idempotency_key}")


class TestNotificationDeliveryFlow:
    """Test suite for notification delivery end-to-end flow."""

    async def test_successful_delivery(self):
        """Test happy path: notification filters, rate limits pass, delivers successfully."""
        adapter = MockDeliveryAdapter()

        payload = NotificationPayload(
            notification_id=str(uuid4()),
            message="Test notification",
            title="Test",
            type=NotificationType.INFO,
            criticality=NotificationCriticality.LOW,
            source="test_service",
            created_at=datetime.now(UTC),
        )

        policy = RecipientNotificationPolicy(
            retry_attempts=3,
            rate_limit=10,
            rate_limit_window=3600,
            allowed_types=[NotificationType.INFO],
            blocked_sources_regex=None,
            dnd=None,
        )

        contact_info = RecipientContactInfo(
            email_address="user@example.com",
            phone_number=None,
            push_token=None,
            matrix_id=None,
        )

        task = NotificationDeliveryTask(
            idempotency_key=str(uuid4()),
            payload=payload,
            policy=policy,
            contact_info=contact_info,
        )

        filter_engine = FilterEngine()
        rate_limiter = RateLimiter()
        from ..retry_scheduler import RetryPolicy
        from datetime import timedelta

        processor = NotificationDeliveryProcessor(
            filter_engine=filter_engine,
            rate_limiter=rate_limiter,
            adapters={"email": adapter},
            retry_policy=RetryPolicy(max_attempts=3, base_delay=timedelta(seconds=1)),
            state_store=None,
            attempt_store=None,
        )

        result = await processor.process(task)

        assert result.success
        assert len(adapter.delivered_payloads) == 1
        assert adapter.delivered_payloads[0].notification_id == payload.notification_id

    async def test_filtered_notification(self):
        """Test notification filtered due to type allowlist."""
        adapter = MockDeliveryAdapter()

        payload = NotificationPayload(
            id=str(uuid4()),
            message="Test notification",
            title="Test",
            notification_type=NotificationType.WARNING,
            criticality=NotificationCriticality.MEDIUM,
            source="test_service",
            timestamp=datetime.now(UTC),
        )

        policy = RecipientNotificationPolicy(
            type_allowlist=[NotificationType.INFO],  # WARNING not in allowlist
            blocked_sources=[],
            do_not_disturb_start=None,
            do_not_disturb_end=None,
            rate_limit_per_hour=10,
            retry_enabled=True,
        )

        contact_info = RecipientContactInfo(recipient_id="user1", channel="email")

        task = NotificationDeliveryTask(
            idempotency_key=str(uuid4()),
            payload=payload,
            policy=policy,
            contact_info=contact_info,
        )

        filter_engine = FilterEngine()
        rate_limiter = RateLimiter()
        processor = NotificationDeliveryProcessor(
            filter_engine=filter_engine,
            rate_limiter=rate_limiter,
            adapters={"email": adapter},
            retry_scheduler=None,
            state_store=None,
            attempt_store=None,
        )

        result = await processor.process(task)

        assert not result.success
        assert len(adapter.delivered_payloads) == 0

    async def test_rate_limiting(self):
        """Test notification rejected due to rate limit."""
        adapter = MockDeliveryAdapter()

        payload = NotificationPayload(
            id=str(uuid4()),
            message="Test notification",
            title="Test",
            notification_type=NotificationType.INFO,
            criticality=NotificationCriticality.LOW,
            source="test_service",
            timestamp=datetime.now(UTC),
        )

        policy = RecipientNotificationPolicy(
            type_allowlist=[NotificationType.INFO],
            blocked_sources=[],
            do_not_disturb_start=None,
            do_not_disturb_end=None,
            rate_limit_per_hour=0,  # No messages allowed per hour
            retry_enabled=True,
        )

        contact_info = RecipientContactInfo(recipient_id="user1", channel="email")

        task = NotificationDeliveryTask(
            idempotency_key=str(uuid4()),
            payload=payload,
            policy=policy,
            contact_info=contact_info,
        )

        filter_engine = FilterEngine()
        rate_limiter = RateLimiter()
        processor = NotificationDeliveryProcessor(
            filter_engine=filter_engine,
            rate_limiter=rate_limiter,
            adapters={"email": adapter},
            retry_scheduler=None,
            state_store=None,
            attempt_store=None,
        )

        result = await processor.process(task)

        assert not result.success
        assert len(adapter.delivered_payloads) == 0

    async def test_task_queue_processing(self):
        """Test queue processes multiple tasks concurrently."""
        adapter = MockDeliveryAdapter()

        filter_engine = FilterEngine()
        rate_limiter = RateLimiter()
        processor_instance = NotificationDeliveryProcessor(
            filter_engine=filter_engine,
            rate_limiter=rate_limiter,
            adapters={"email": adapter},
            retry_scheduler=None,
            state_store=None,
            attempt_store=None,
        )

        # Create queue with processor factory
        def processor_factory():
            return processor_instance

        queue = NotificationDeliveryTaskQueue(
            processor_factory=processor_factory,
            max_concurrent_tasks=2,
        )

        # Create multiple tasks
        tasks = []
        for i in range(3):
            payload = NotificationPayload(
                id=str(uuid4()),
                message=f"Notification {i}",
                title="Test",
                notification_type=NotificationType.INFO,
                criticality=NotificationCriticality.LOW,
                source="test_service",
                timestamp=datetime.now(UTC),
            )

            policy = RecipientNotificationPolicy(
                type_allowlist=[NotificationType.INFO],
                blocked_sources=[],
                do_not_disturb_start=None,
                do_not_disturb_end=None,
                rate_limit_per_hour=10,
                retry_enabled=True,
            )

            contact_info = RecipientContactInfo(
                recipient_id=f"user{i}", channel="email"
            )

            task = NotificationDeliveryTask(
                idempotency_key=str(uuid4()),
                payload=payload,
                policy=policy,
                contact_info=contact_info,
            )
            tasks.append(task)

        # Start queue
        queue.start()

        try:
            # Add tasks
            for task in tasks:
                queue.add_task(task)

            # Wait for processing
            await asyncio.sleep(0.5)

            # Verify all tasks were delivered
            assert len(adapter.delivered_payloads) == 3
        finally:
            await queue.stop()


async def main():
    """Run all tests."""
    test_suite = TestNotificationDeliveryFlow()

    # Run all tests
    try:
        print("Running test_successful_delivery...")
        await test_suite.test_successful_delivery()
        print("✓ Passed")

        print("Running test_filtered_notification...")
        await test_suite.test_filtered_notification()
        print("✓ Passed")

        print("Running test_rate_limiting...")
        await test_suite.test_rate_limiting()
        print("✓ Passed")

        print("Running test_task_queue_processing...")
        await test_suite.test_task_queue_processing()
        print("✓ Passed")

        print("\nAll tests passed!")
    except Exception as e:
        print(f"✗ Test failed: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
