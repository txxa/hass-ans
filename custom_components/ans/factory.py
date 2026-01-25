"""Setup and initialization for the ANS notification system.

Provides factory functions and dependency injection for complete system setup.
"""

import logging
from collections.abc import Callable
from datetime import timedelta

from homeassistant.core import HomeAssistant

from .adapter_registry import AdapterRegistry
from .config_repository import ConfigRepository
from .const import (
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_BACKOFF_FACTOR,
    DEFAULT_RETRY_BASE_DELAY_SECONDS,
    DEFAULT_RETRY_MAX_DELAY_SECONDS,
)
from .delivery.persistent_notification import PersistentNotificationAdapter
from .delivery.signal import SignalDeliveryAdapter
from .filter_engine import FilterEngine
from .housekeeping import HousekeepingScheduler
from .orchestrator import NotificationOrchestrator
from .persistence_file import JsonFileAttemptStore, JsonFileDeliveryStateStore
from .processor import NotificationDeliveryProcessor
from .queue import NotificationDeliveryTaskQueue
from .rate_limiter import RateLimiter
from .retry_scheduler import RetryPolicy

_LOGGER = logging.getLogger(__name__)


class NotificationSystemSetup:
    """Factory for creating a complete ANS notification system.

    Handles dependency injection and component wiring.
    """

    @staticmethod
    def create_adapter_registry(hass: HomeAssistant) -> AdapterRegistry:
        """Create and populate the adapter registry with all available adapters.

        Args:
            hass: Home Assistant instance for adapters that need it.

        Returns:
            Populated AdapterRegistry instance.

        """
        registry = AdapterRegistry()

        # Register Signal adapter
        # TODO: Get API URL from configuration
        signal_adapter = SignalDeliveryAdapter(api_url="http://localhost:8080")
        registry.register(signal_adapter)
        _LOGGER.debug("Registered Signal delivery adapter")

        # Register persistent notification adapter
        persistent_adapter = PersistentNotificationAdapter(hass=hass)
        registry.register(persistent_adapter)
        _LOGGER.debug("Registered persistent notification delivery adapter")

        # TODO: Register additional adapters as they become available:
        # registry.register(EmailDeliveryAdapter(...))
        # registry.register(TelegramDeliveryAdapter(...))
        # registry.register(PushbulletDeliveryAdapter(...))

        return registry

    @staticmethod
    def create_processor_factory(
        filter_engine: FilterEngine,
        rate_limiter: RateLimiter,
        adapters: dict,
        retry_policy: RetryPolicy,
        state_store,
        attempt_store,
    ) -> Callable[[], NotificationDeliveryProcessor]:
        """Create a processor factory for queue workers.

        Args:
            filter_engine: Filter evaluation engine.
            rate_limiter: Rate limiting instance.
            adapters: Dict of channel -> DeliveryAdapter.
            retry_policy: Retry policy.
            state_store: Delivery state persistence.
            attempt_store: Attempt tracking persistence.

        Returns:
            Callable that creates new processor instances.

        """

        def _create_processor() -> NotificationDeliveryProcessor:
            return NotificationDeliveryProcessor(
                filter_engine=filter_engine,
                rate_limiter=rate_limiter,
                adapters=adapters,
                retry_policy=retry_policy,
                state_store=state_store,
                attempt_store=attempt_store,
            )

        return _create_processor

    @staticmethod
    def create_system(
        hass: HomeAssistant,
        config_repo: ConfigRepository,
        max_concurrent_deliveries: int = 5,
    ) -> dict:
        """Create a complete ANS notification system with all components.

        Args:
            hass: Home Assistant instance.
            config_repo: Configuration repository.
            max_concurrent_deliveries: Max concurrent delivery tasks (default: 5).

        Returns:
            Dictionary containing all system components:
            - adapter_registry
            - orchestrator
            - task_queue
            - filter_engine
            - rate_limiter
            - retry_policy
            - state_store
            - attempt_store
            - housekeeping_scheduler

        """
        # Create persistence stores (file-based instead of in-memory)
        state_store = JsonFileDeliveryStateStore(hass)
        attempt_store = JsonFileAttemptStore(hass)

        # Load system configuration for rate limiting
        config_snapshot = config_repo.snapshot()
        system_config = config_snapshot.system_config

        # Create stateless/shared components
        filter_engine = FilterEngine()
        rate_limiter = RateLimiter(
            global_rate_limit=system_config.rate_limit_max,
            rate_limit_window=system_config.rate_limit_window,  # Hard-coded to 60s
        )
        retry_policy = RetryPolicy(
            max_attempts=DEFAULT_RETRY_ATTEMPTS,  # Hard-coded: same for all, overridden by recipient config
            base_delay=timedelta(seconds=DEFAULT_RETRY_BASE_DELAY_SECONDS),
            backoff_factor=DEFAULT_RETRY_BACKOFF_FACTOR,
            max_delay=timedelta(seconds=DEFAULT_RETRY_MAX_DELAY_SECONDS),
        )

        # Create and populate adapter registry
        adapter_registry = NotificationSystemSetup.create_adapter_registry(hass)
        adapters_dict = adapter_registry.all()

        _LOGGER.info(
            "ANS: Registered %d delivery adapters: %s",
            len(adapters_dict),
            adapter_registry.channels(),
        )

        # Create processor factory for queue
        processor_factory = NotificationSystemSetup.create_processor_factory(
            filter_engine=filter_engine,
            rate_limiter=rate_limiter,
            adapters=adapters_dict,
            retry_policy=retry_policy,
            state_store=state_store,
            attempt_store=attempt_store,
        )

        # Create task queue with worker pool
        task_queue = NotificationDeliveryTaskQueue(
            max_concurrency=max_concurrent_deliveries,
            processor_factory=processor_factory,
        )

        # Create orchestrator (coordinates notifications → tasks)
        orchestrator = NotificationOrchestrator(
            config_repo=config_repo,
            task_queue=task_queue,
        )

        # Create housekeeping scheduler for cleanup
        housekeeping_scheduler = HousekeepingScheduler(
            state_store=state_store,
            attempt_store=attempt_store,
            interval=timedelta(hours=1),
            retention_age=timedelta(days=30),
        )

        return {
            "adapter_registry": adapter_registry,
            "orchestrator": orchestrator,
            "task_queue": task_queue,
            "filter_engine": filter_engine,
            "rate_limiter": rate_limiter,
            "retry_policy": retry_policy,
            "state_store": state_store,
            "attempt_store": attempt_store,
            "housekeeping_scheduler": housekeeping_scheduler,
        }
