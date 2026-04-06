"""Setup and initialization for the ANS notification system.

Provides factory functions and dependency injection for complete system setup.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.core import HomeAssistant

from ..channels.base import DeliveryAdapter
from ..channels.channel_manager import ChannelManager
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

    channel_manager: ChannelManager
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
#: Order determines factory registration order in the channel manager.
#: Adding a new adapter only requires updating this tuple.
_ALL_ADAPTER_CLASSES: tuple[type[DeliveryAdapter], ...] = (
    PersistentNotificationAdapter,
    MobileAppDeliveryAdapter,
    SignalDeliveryAdapter,
    TTSMediaPlayerAdapter,
)

#: Mapping of channel prefixes to adapter classes, derived from metadata.
#: Stays in sync with ``_ALL_ADAPTER_CLASSES`` automatically.
#: Used by config flows for contact-info requirement lookups and UI labels.
ADAPTER_CLASS_MAP: dict[str, type[DeliveryAdapter]] = {
    cls.get_metadata().channel_prefix: cls for cls in _ALL_ADAPTER_CLASSES
}


def _create_channel_manager(
    hass: HomeAssistant,
    config_repo: ConfigRepository,
    volume_registry: VolumeRestorationRegistry,
) -> ChannelManager:
    """Create and configure a ChannelManager with all adapter factories.

    Args:
        hass: Home Assistant instance.
        config_repo: Configuration repository (injected into AdapterDeps).
        volume_registry: Pre-initialized VolumeRestorationRegistry.

    Returns:
        Configured ChannelManager ready for initialize_static_adapters() and sync().

    """
    deps = AdapterDeps(config_repo=config_repo, volume_registry=volume_registry)
    manager = ChannelManager(hass, deps)

    for adapter_cls in _ALL_ADAPTER_CLASSES:
        if adapter_cls is TTSMediaPlayerAdapter:
            # Pass the channel manager's delivery-lock factory so all adapter
            # generations for the same entity share a single lock that survives
            # resync().  Other adapters don't need per-entity delivery locks.
            manager.register_factory(
                TTSMediaPlayerAdapter.create_factory(
                    deps=deps,
                    get_delivery_lock=manager.get_delivery_lock,
                )
            )
        else:
            manager.register_factory(adapter_cls.create_factory(deps=deps))

    _LOGGER.info(
        "ChannelManager configured with %d factories", len(_ALL_ADAPTER_CLASSES)
    )
    return manager


def _create_processor_factory(
    filter_engine: FilterEngine,
    rate_limiter: RateLimiter,
    channel_manager: ChannelManager,
    hass: HomeAssistant,
    retry_policy: RetryPolicy,
    notification_registry: NotificationRegistry,
    attempt_log: DeliveryAttemptLog,
    retry_queue: RetryQueue,
) -> Callable[[], NotificationDeliveryProcessor]:
    """Return a zero-argument factory that creates :class:`NotificationDeliveryProcessor` instances.

    The task queue calls this factory once per worker task so that each
    processor gets freshly-initialised internal state while sharing the
    stateless/singleton dependencies (filter engine, rate limiter, …).

    Args:
        filter_engine: Shared filter evaluation engine.
        rate_limiter: Shared in-memory rate limiter.
        channel_manager: Shared channel manager.
        hass: Home Assistant instance.
        retry_policy: Shared retry backoff policy.
        notification_registry: Persistent notification registry.
        attempt_log: Persistent delivery attempt log.
        retry_queue: Persistent retry queue.

    Returns:
        A callable ``() -> NotificationDeliveryProcessor``.

    """

    def _create_processor() -> NotificationDeliveryProcessor:
        return NotificationDeliveryProcessor(
            filter_engine=filter_engine,
            rate_limiter=rate_limiter,
            channel_manager=channel_manager,
            hass=hass,
            retry_policy=retry_policy,
            notification_registry=notification_registry,
            attempt_log=attempt_log,
            retry_queue=retry_queue,
        )

    return _create_processor


def create_system(
    hass: HomeAssistant,
    config_repo: ConfigRepository,
    volume_registry: VolumeRestorationRegistry,
) -> ANSSystem:
    """Create a complete ANS notification system with all components.

    This is the single entry-point for system construction.  All concurrency
    and policy parameters are sourced from :attr:`SystemConfig` stored in the
    config entry, so callers do not need to pass them explicitly.

    Args:
        hass: Home Assistant instance.
        config_repo: Configuration repository (already loaded).
        volume_registry: Pre-initialized :class:`VolumeRestorationRegistry`.
            Must be fully loaded before this call so that TTS adapters can
            restore volumes correctly.

    Returns:
        :class:`ANSSystem` dataclass containing all runtime components.

    Raises:
        ValueError: If ``config_repo`` has no system config loaded.

    """
    config_snapshot = config_repo.snapshot()
    if config_snapshot is None or config_snapshot.system_config is None:
        raise ValueError(
            "create_system() requires a loaded ConfigRepository. "
            "Call config_repo.load() before create_system()."
        )
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

    # Create ChannelManager (single source of truth for channels + adapters)
    channel_manager = _create_channel_manager(hass, config_repo, volume_registry)

    # Initialize static adapters (always available, synchronous)
    channel_manager.initialize_static_adapters()
    # NOTE: sync() is async and is called from async_setup_entry after
    # create_system() returns.  Dynamic adapters are not yet registered.

    # Create processor factory for queue
    processor_factory = _create_processor_factory(
        filter_engine=filter_engine,
        rate_limiter=rate_limiter,
        channel_manager=channel_manager,
        hass=hass,
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
        channel_manager=channel_manager,
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
        channel_manager=channel_manager,
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
