"""Setup and initialization for the ANS notification system.

Provides factory functions and dependency injection for complete system setup.
"""

import logging
from collections.abc import Callable
from datetime import timedelta

from homeassistant.core import HomeAssistant

from ..channels.adapter_lifecycle import (
    AdapterLifecycleManager,
)
from ..channels.adapter_registry import AdapterRegistry
from ..channels.mobile_app import MobileAppDeliveryAdapter
from ..channels.persistent_notification import PersistentNotificationAdapter
from ..channels.signal import SignalDeliveryAdapter
from ..channels.tts_mediaplayer import TTSMediaPlayerAdapter
from ..config.repository import ConfigRepository
from ..const import (
    DOMAIN,
    RCPT_DEFAULT_RETRY_ATTEMPTS,
    SYS_STORAGE_HOUSEKEEPING_INTERVAL_HOURS,
)
from ..persistence.file import DeliveryAttemptLog, NotificationRegistry, RetryQueue
from ..persistence.housekeeping import HousekeepingScheduler
from .deduplication import DeduplicationService
from .filter_engine import FilterEngine
from .orchestrator import NotificationOrchestrator
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
        """Create empty adapter registry.

        Adapters are registered via lifecycle manager.

        Args:
            hass: Home Assistant instance.

        Returns:
            Empty registry.

        """
        return AdapterRegistry()

    @staticmethod
    def create_adapter_lifecycle_manager(
        hass: HomeAssistant, registry: AdapterRegistry
    ) -> AdapterLifecycleManager:
        """Create and configure adapter lifecycle manager with all factories.

        MEMORY-EFFICIENT POLICY:
        All adapter factories are registered here, but adapter instances
        are only created for enabled channels.

        - persistent_notification: STATIC (HA built-in, always available)
        - mobile_app_*: DYNAMIC_MULTI (only enabled devices)
        - signal: DYNAMIC_SINGLE (only if enabled and configured)

        Args:
            hass: Home Assistant instance
            registry: Adapter registry to manage

        Returns:
            Configured lifecycle manager with all adapter factories

        """
        manager = AdapterLifecycleManager(hass, registry)

        # ----------------------------------------------------------------
        # STATIC ADAPTERS - Always available, no config needed
        # ----------------------------------------------------------------
        manager.register_factory(PersistentNotificationAdapter.create_factory())

        # ----------------------------------------------------------------
        # DYNAMIC_MULTI ADAPTERS - Created per enabled device
        # ----------------------------------------------------------------
        # Mobile App: Device-specific instances, created only for enabled devices
        def _create_mobile_app_adapter(
            h: HomeAssistant, device_id: str | None
        ) -> MobileAppDeliveryAdapter:
            if device_id is None:
                raise ValueError("device_id is required for mobile_app adapter")
            return MobileAppDeliveryAdapter(hass=h, device_id=device_id)

        manager.register_factory(
            MobileAppDeliveryAdapter.create_factory(
                factory_fn=_create_mobile_app_adapter
            )
        )

        # TTS Media Player: Entity-specific instances, created only for enabled media players
        def _create_tts_mediaplayer_adapter(
            h: HomeAssistant, entity_id: str | None
        ) -> TTSMediaPlayerAdapter:
            if entity_id is None:
                raise ValueError("entity_id is required for tts_mediaplayer adapter")

            # Retrieve TTS service and volume registry from hass.data
            # These must be set during integration setup
            tts_service = None
            volume_registry = None

            # Find main entry data
            if DOMAIN in h.data:
                for entry_data in h.data[DOMAIN].values():
                    if isinstance(entry_data, dict):
                        if "config_repository" in entry_data:
                            config_repo = entry_data["config_repository"]
                            snapshot = config_repo.snapshot()
                            tts_service = snapshot.system_config.tts_service
                        if "volume_registry" in entry_data:
                            volume_registry = entry_data["volume_registry"]
                        if tts_service and volume_registry:
                            break

            if not tts_service:
                raise ValueError(
                    "TTS service not configured in system settings. "
                    "Cannot create TTS media player adapter."
                )

            if not volume_registry:
                raise ValueError(
                    "Volume restoration registry not initialized. "
                    "Cannot create TTS media player adapter."
                )

            return TTSMediaPlayerAdapter(
                hass=h,
                entity_id=entity_id,
                tts_service=tts_service,
                volume_registry=volume_registry,
            )

        manager.register_factory(
            TTSMediaPlayerAdapter.create_factory(
                factory_fn=_create_tts_mediaplayer_adapter
            )
        )

        # ----------------------------------------------------------------
        # DYNAMIC_SINGLE ADAPTERS - Created only when enabled
        # ----------------------------------------------------------------
        manager.register_factory(SignalDeliveryAdapter.create_factory())

        _LOGGER.info(
            "Adapter lifecycle manager configured with %d factories (memory-efficient mode)",
            manager.get_factory_count(),
        )
        return manager

    @staticmethod
    def create_processor_factory(
        filter_engine: FilterEngine,
        rate_limiter: RateLimiter,
        adapters: AdapterRegistry,
        retry_policy: RetryPolicy,
        notification_registry,
        attempt_log,
        retry_queue,
    ) -> Callable[[], NotificationDeliveryProcessor]:
        """Create a processor factory for queue workers.

        Args:
            filter_engine: Filter evaluation engine.
            rate_limiter: Rate limiting instance.
            adapters: AdapterRegistry for channel lookup.
            retry_policy: Retry policy.
            notification_registry: Notification tracking registry.
            attempt_log: Attempt tracking log.
            retry_queue: Retry queue for scheduling.

        Returns:
            Callable that creates new processor instances.

        """

        def _create_processor() -> NotificationDeliveryProcessor:
            return NotificationDeliveryProcessor(
                filter_engine=filter_engine,
                rate_limiter=rate_limiter,
                adapters=adapters,
                retry_policy=retry_policy,
                notification_registry=notification_registry,
                attempt_log=attempt_log,
                retry_queue=retry_queue,
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
            - notification_registry
            - attempt_log
            - retry_queue
            - housekeeping_scheduler

        """
        # Load system configuration first to get audit logging setting
        config_snapshot = config_repo.snapshot()
        system_config = config_snapshot.system_config

        # Create persistence stores (file-based event storage)
        # Use enable_audit_logging from SystemConfig (stored in entry.data)
        enable_audit_logging = system_config.enable_audit_logging
        notification_registry = NotificationRegistry(hass, enabled=enable_audit_logging)
        attempt_log = DeliveryAttemptLog(hass, enabled=enable_audit_logging)
        retry_queue = RetryQueue(hass)

        _LOGGER.info(
            "ANS: Audit logging is %s",
            "enabled" if enable_audit_logging else "disabled",
        )

        # Create stateless/shared components
        filter_engine = FilterEngine()
        rate_limiter = RateLimiter(
            global_rate_limit=system_config.global_rate_limit,
            rate_limit_window=system_config.rate_limit_window,  # Hard-coded to 60s
        )
        retry_policy = RetryPolicy(
            max_attempts=RCPT_DEFAULT_RETRY_ATTEMPTS,  # Hard-coded: same for all, overridden by recipient config
            base_delay=timedelta(seconds=system_config.retry_base_delay),
            backoff_factor=system_config.retry_backoff_factor,
            max_delay=timedelta(seconds=system_config.retry_max_delay),
        )

        # Create adapter registry and lifecycle manager
        adapter_registry = NotificationSystemSetup.create_adapter_registry(hass)
        lifecycle_manager = NotificationSystemSetup.create_adapter_lifecycle_manager(
            hass, adapter_registry
        )

        # Initialize static adapters (always available)
        lifecycle_manager.initialize_static_adapters()

        # Sync dynamic adapters with enabled channels
        enabled_channels = system_config.enabled_channels
        lifecycle_manager.sync_with_config(enabled_channels)

        _LOGGER.info(
            "ANS: Registered %d delivery adapters: %s",
            adapter_registry.count(),
            adapter_registry.channels(),
        )

        # Create processor factory for queue
        processor_factory = NotificationSystemSetup.create_processor_factory(
            filter_engine=filter_engine,
            rate_limiter=rate_limiter,
            adapters=adapter_registry,
            retry_policy=retry_policy,
            notification_registry=notification_registry,
            attempt_log=attempt_log,
            retry_queue=retry_queue,
        )

        # Create task queue with worker pool
        task_queue = NotificationDeliveryTaskQueue(
            max_concurrency=system_config.queue_max_concurrency,
            processor_factory=processor_factory,
            retry_queue=retry_queue,
        )

        # Create deduplication service (prevents duplicate deliveries)
        deduplication_service = DeduplicationService(
            window_seconds=60,  # 60 second deduplication window
            max_cache_size=1000,  # Maximum 1000 entries in cache
            cleanup_interval=60,  # Cleanup every 60 seconds
        )

        # Create orchestrator (coordinates notifications → tasks)
        orchestrator = NotificationOrchestrator(
            config_repo=config_repo,
            task_queue=task_queue,
            notification_registry=notification_registry,
            deduplication_service=deduplication_service,
        )

        # Create housekeeping scheduler for cleanup
        housekeeping_scheduler = HousekeepingScheduler(
            notification_registry=notification_registry,
            attempt_log=attempt_log,
            retry_queue=retry_queue,
            interval=timedelta(hours=SYS_STORAGE_HOUSEKEEPING_INTERVAL_HOURS),
            retention_age=timedelta(days=system_config.storage_retention_days),
        )

        return {
            "adapter_registry": adapter_registry,
            "lifecycle_manager": lifecycle_manager,
            "orchestrator": orchestrator,
            "task_queue": task_queue,
            "filter_engine": filter_engine,
            "rate_limiter": rate_limiter,
            "retry_policy": retry_policy,
            "notification_registry": notification_registry,
            "attempt_log": attempt_log,
            "retry_queue": retry_queue,
            "housekeeping_scheduler": housekeeping_scheduler,
            "deduplication_service": deduplication_service,
        }
