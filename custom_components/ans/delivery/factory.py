"""Setup and initialization for the ANS notification system.

Provides factory functions and dependency injection for complete system setup.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.core import HomeAssistant

from ..channels.adapter_lifecycle import (
    AdapterLifecycleManager,
)
from ..channels.adapter_registry import (
    AdapterRegistry,
)
from ..channels.base import DeliveryAdapter
from ..channels.mobile_app import MobileAppDeliveryAdapter
from ..channels.persistent_notification import PersistentNotificationAdapter
from ..channels.signal import SignalDeliveryAdapter
from ..channels.tts_mediaplayer import TTSMediaPlayerAdapter
from ..config.repository import ConfigRepository
from ..const import (
    RCPT_DEFAULT_RETRY_ATTEMPTS,
    SYS_DEDUP_CLEANUP_INTERVAL,
    SYS_DEDUP_MAX_CACHE_SIZE,
    SYS_DEDUP_WINDOW_SECONDS,
    SYS_STORAGE_HOUSEKEEPING_INTERVAL_HOURS,
)
from ..persistence.file import DeliveryAttemptLog, NotificationRegistry, RetryQueue
from ..persistence.housekeeping import HousekeepingScheduler
from ..persistence.volume_restoration import VolumeRestorationRegistry
from .deduplication import DeduplicationService
from .filter_engine import FilterEngine
from .orchestrator import NotificationOrchestrator
from .processor import NotificationDeliveryProcessor
from .queue import NotificationDeliveryTaskQueue
from .rate_limiter import RateLimiter
from .retry_scheduler import RetryPolicy

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ANSSystem:
    """All runtime components of a single ANS integration instance.

    Fields are populated once by :func:`create_system` and remain stable
    for the lifetime of the config entry.  ``frozen=True`` prevents
    accidental field reassignment after construction.
    """

    adapter_registry: AdapterRegistry
    lifecycle_manager: AdapterLifecycleManager
    orchestrator: NotificationOrchestrator
    task_queue: NotificationDeliveryTaskQueue
    filter_engine: FilterEngine
    rate_limiter: RateLimiter
    retry_policy: RetryPolicy
    notification_registry: NotificationRegistry
    attempt_log: DeliveryAttemptLog
    retry_queue: RetryQueue
    housekeeping_scheduler: HousekeepingScheduler
    deduplication_service: DeduplicationService


@dataclass(frozen=True)
class AdapterDeps:
    """Runtime dependencies injected into every adapter factory.

    Passed uniformly to all :py:meth:`DeliveryAdapter.create_factory` calls
    so that adapters needing extra runtime objects (e.g. TTS) can source them
    from a single well-typed struct without LSP-violating per-adapter kwargs.

    Attributes
    ----------
    config_repo : ConfigRepository
        Configuration repository used by TTS adapter for TTS service lookup.
    volume_registry : VolumeRestorationRegistry
        Volume restoration registry used by TTS adapter.

    """

    config_repo: ConfigRepository
    volume_registry: VolumeRestorationRegistry


#: All adapter classes in registration order.
#: Order determines factory registration order in the lifecycle manager.
#: Adding a new adapter only requires updating this tuple.
_ALL_ADAPTER_CLASSES: tuple[type[DeliveryAdapter], ...] = (
    PersistentNotificationAdapter,
    MobileAppDeliveryAdapter,
    SignalDeliveryAdapter,
    TTSMediaPlayerAdapter,
)

#: Mapping of channel prefixes to adapter classes, derived from metadata.
#: Stays in sync with ``_ALL_ADAPTER_CLASSES`` automatically.
ADAPTER_CLASS_MAP: dict[str, type[DeliveryAdapter]] = {
    cls.get_metadata().channel_prefix: cls for cls in _ALL_ADAPTER_CLASSES
}


def create_adapter_registry(hass: HomeAssistant) -> AdapterRegistry:
    """Create empty adapter registry.

    Adapters are registered via lifecycle manager.

    Args:
        hass: Home Assistant instance.

    Returns:
        Empty registry.

    """
    return AdapterRegistry()


def create_adapter_lifecycle_manager(
    hass: HomeAssistant,
    registry: AdapterRegistry,
    volume_registry,
    config_repo: ConfigRepository,
) -> AdapterLifecycleManager:
    """Create and configure adapter lifecycle manager with all factories.

    Factories for standard adapters are registered via the data-driven
    ``_STANDARD_ADAPTER_CLASSES`` tuple.  ``TTSMediaPlayerAdapter`` is
    registered separately because it requires extra runtime dependencies.
    Adapter instances are only created on-demand for enabled channels.

    Args:
        hass: Home Assistant instance
        registry: Adapter registry to manage
        volume_registry: Pre-initialized VolumeRestorationRegistry instance
        config_repo: Configuration repository for TTS service lookup

    Returns:
        Configured lifecycle manager with all adapter factories

    """
    manager = AdapterLifecycleManager(hass, registry)
    deps = AdapterDeps(config_repo=config_repo, volume_registry=volume_registry)

    for adapter_cls in _ALL_ADAPTER_CLASSES:
        manager.register_factory(adapter_cls.create_factory(deps=deps))

    _LOGGER.info(
        "Adapter lifecycle manager configured with %d factories",
        manager.get_factory_count(),
    )
    return manager


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


def create_system(
    hass: HomeAssistant,
    config_repo: ConfigRepository,
    volume_registry,
    max_concurrent_deliveries: int = 5,
) -> ANSSystem:
    """Create a complete ANS notification system with all components.

    Args:
        hass: Home Assistant instance.
        config_repo: Configuration repository.
        volume_registry: Pre-initialized VolumeRestorationRegistry. Must be
            initialized and loaded before this call.
        max_concurrent_deliveries: Max concurrent delivery tasks (default: 5).

    Returns:
        ANSSystem dataclass containing all system components.

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
    adapter_registry = create_adapter_registry(hass)
    lifecycle_manager = create_adapter_lifecycle_manager(
        hass, adapter_registry, volume_registry, config_repo
    )

    # Initialize static adapters (always available)
    lifecycle_manager.initialize_static_adapters()
    # NOTE: sync_with_config() is async and is called from async_setup_entry
    # after create_system() returns.  Dynamic adapters are not yet registered
    # at this point; validation and adapter-count logging also happen there.

    # Create processor factory for queue
    processor_factory = create_processor_factory(
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
        window_seconds=SYS_DEDUP_WINDOW_SECONDS,
        max_cache_size=SYS_DEDUP_MAX_CACHE_SIZE,
        cleanup_interval=SYS_DEDUP_CLEANUP_INTERVAL,
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

    return ANSSystem(
        adapter_registry=adapter_registry,
        lifecycle_manager=lifecycle_manager,
        orchestrator=orchestrator,
        task_queue=task_queue,
        filter_engine=filter_engine,
        rate_limiter=rate_limiter,
        retry_policy=retry_policy,
        notification_registry=notification_registry,
        attempt_log=attempt_log,
        retry_queue=retry_queue,
        housekeeping_scheduler=housekeeping_scheduler,
        deduplication_service=deduplication_service,
    )
