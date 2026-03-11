"""Advanced Notification System integration bootstrap."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_SERVICE_REGISTERED
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_track_state_added_domain

from .channels.adapter_registry import validate_channel_adapter_consistency
from .config.repository import ConfigRepository
from .const import (
    SYS_DEFAULT_QUEUE_CONCURRENCY,
)
from .delivery.factory import ADAPTER_CLASS_MAP, ANSSystem, create_system
from .helper import get_main_entry
from .persistence.recovery import async_initialize_persistence
from .persistence.volume_restoration import VolumeRestorationRegistry
from .service import async_setup_services

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up integration for a config entry."""

    _LOGGER.info("Setting up ANS config entry: %s", entry.entry_id)

    try:
        # Initialize runtime data dict for this entry
        entry_data: dict = {}
        entry.runtime_data = entry_data

        # Initialize the config repository for this main entry
        config_repo = ConfigRepository(hass, adapter_classes=ADAPTER_CLASS_MAP)

        # Load the main entry configuration into the repository
        if not await config_repo.load():
            _LOGGER.error("Failed to load main entry configuration")
            raise ConfigEntryNotReady("Failed to load main entry configuration")

        _LOGGER.debug("Config repository loaded successfully")

        _LOGGER.info(
            "Loaded %d notification channels", config_repo.channel_registry.count()
        )

        # Store the config repository in the entry data
        entry_data["config_repository"] = config_repo

        # Check if we have recipients configured (log warning if not)
        snapshot = config_repo.snapshot()
        recipients = snapshot.getRecipients()
        if not recipients:
            _LOGGER.warning(
                "ANS config entry %s has no recipients configured. "
                "Notifications will be dropped.",
                entry.entry_id,
            )

        # Initialize volume restoration registry BEFORE create_system() so the
        # TTS adapter factory can receive it via explicit injection.
        volume_registry = VolumeRestorationRegistry(hass)
        await volume_registry.async_load()
        entry_data["volume_registry"] = volume_registry
        _LOGGER.debug("Volume restoration registry initialized")

        # Create and initialize the complete notification system
        system = create_system(
            hass=hass,
            config_repo=config_repo,
            volume_registry=volume_registry,
            max_concurrent_deliveries=SYS_DEFAULT_QUEUE_CONCURRENCY,
        )

        # Store system components in entry data
        entry_data["system"] = system

        # Sync dynamic adapters with enabled channels now that we are in async context.
        # (sync_with_config is async; it was moved out of the synchronous create_system())
        _sync_snapshot = config_repo.snapshot()
        await system.lifecycle_manager.sync_with_config(
            list(_sync_snapshot.system_config.enabled_channels),
            detected_channel_ids=set(config_repo.channel_registry.get_all_ids()),
        )
        _validation = validate_channel_adapter_consistency(
            config_repo.channel_registry,
            system.adapter_registry,
            excluded_adapter_channels=system.lifecycle_manager.get_static_channel_ids(),
        )
        if _validation["missing_adapters"]:
            _LOGGER.warning(
                "ANS: %d channel(s) have no adapter: %s",
                len(_validation["missing_adapters"]),
                _validation["missing_adapters"],
            )
        if _validation["orphaned_adapters"]:
            _LOGGER.warning(
                "ANS: %d adapter(s) have no channel registration: %s",
                len(_validation["orphaned_adapters"]),
                _validation["orphaned_adapters"],
            )
        _LOGGER.info(
            "ANS: Registered %d delivery adapters: %s",
            system.adapter_registry.count(),
            system.adapter_registry.channels(),
        )

        # Initialize persistence and recover pending retries.
        # Pass the authoritative store instances from the ANSSystem so the
        # recovery function reads from the same in-memory objects rather than
        # creating a second set of stores wrapping the same HA Store files.
        (
            pending_tasks,
            orphaned_retries,
        ) = await async_initialize_persistence(
            hass,
            system.notification_registry,
            system.attempt_log,
            system.retry_queue,
        )
        retry_queue = system.retry_queue
        _LOGGER.info(
            "ANS persistence initialized: %d tasks to recover, %d orphaned",
            len(pending_tasks),
            len(orphaned_retries),
        )

        # Start background tasks BEFORE scheduling retries
        task_queue = system.task_queue
        housekeeping_scheduler = system.housekeeping_scheduler
        deduplication_service = system.deduplication_service

        await task_queue.start()
        _LOGGER.debug("ANS task queue started")

        await housekeeping_scheduler.start()
        _LOGGER.debug("ANS housekeeping scheduler started")

        await deduplication_service.start()
        _LOGGER.debug("ANS deduplication service started")

        # Schedule recovered tasks for retry
        now = datetime.now(UTC)
        for task, scheduled_time in pending_tasks:
            # Calculate delay (may be in past if HA was down for a while)
            delay_seconds = max((scheduled_time - now).total_seconds(), 0)

            if delay_seconds == 0:
                _LOGGER.info(
                    "Retry for job %s is overdue (scheduled at %s), executing immediately",
                    task.job_id,
                    scheduled_time.isoformat(),
                )
            else:
                _LOGGER.debug(
                    "Scheduling retry for job %s in %.1f seconds",
                    task.job_id,
                    delay_seconds,
                )

            # Add task to queue with delay
            await task_queue.add_task(task, delay=timedelta(seconds=delay_seconds))

        # Clean up orphaned retry schedules (no task snapshot to recover)
        if orphaned_retries:
            for job_id_str in orphaned_retries:
                _LOGGER.warning(
                    "Removing orphaned retry schedule for job %s (no task data)",
                    job_id_str,
                )
                # Remove from retry queue since we can't recover
                try:
                    await retry_queue.remove_retry(UUID(job_id_str))
                except ValueError:
                    _LOGGER.error("Invalid UUID for orphaned retry: %s", job_id_str)

        # Register service handlers
        orchestrator = system.orchestrator
        await async_setup_services(hass, orchestrator)
        _LOGGER.debug("ANS services registered")

        # Re-detect channels when new notify services appear after ANS has
        # started (e.g. mobile_app integrations that load in parallel during
        # HA startup and lose the initial channel scan race).
        async def _on_notify_service_registered(event: Event) -> None:
            if event.data.get("domain") != "notify":
                return
            service = event.data.get("service", "")
            if service in ("notify", "send_message"):
                return
            _LOGGER.debug(
                "New notify service registered: notify.%s — refreshing ANS channels",
                service,
            )
            try:
                sys_data: ANSSystem | None = entry_data.get("system")
                lm = sys_data.lifecycle_manager if sys_data else None
                if lm and config_repo.system_config:
                    await config_repo.refresh_and_sync(lm)
            except Exception:
                _LOGGER.exception(
                    "Failed to refresh channels after notify service 'notify.%s' was registered",
                    service,
                )

        entry.async_on_unload(
            hass.bus.async_listen(
                EVENT_SERVICE_REGISTERED, _on_notify_service_registered
            )
        )

        # Re-detect TTS media players when a new media_player entity appears
        # (e.g. integrations that load after the initial channel scan).
        async def _on_media_player_added(event) -> None:  # noqa: ANN001
            entity_id = event.data.get("entity_id", "")
            _LOGGER.debug(
                "New media_player entity added: %s — refreshing ANS channels",
                entity_id,
            )
            try:
                sys_data: ANSSystem | None = entry_data.get("system")
                lm = sys_data.lifecycle_manager if sys_data else None
                if lm and config_repo.system_config:
                    await config_repo.refresh_and_sync(lm)
            except Exception:
                _LOGGER.exception(
                    "Failed to refresh channels after media_player '%s' was added",
                    entity_id,
                )

        entry.async_on_unload(
            async_track_state_added_domain(hass, "media_player", _on_media_player_added)
        )

        # Add config entry update listener
        entry.async_on_unload(entry.add_update_listener(update_listener))

        _LOGGER.info("Successfully set up ANS config entry: %s", entry.entry_id)

    except Exception as e:
        _LOGGER.error("Failed to set up ANS config entry %s: %s", entry.entry_id, e)

        # Clean up any partially initialized components
        await cleanup_entry_data(hass, entry.entry_id)

        if isinstance(e, ConfigEntryNotReady):
            raise
        raise ConfigEntryNotReady(f"Setup failed: {e}") from e

    else:
        return True


async def _teardown_entry_components(entry_data: dict) -> None:
    """Stop and clean up all running components for a config entry.

    Called from both async_unload_entry (normal path) and cleanup_entry_data
    (error recovery path) to guarantee consistent teardown.
    """
    system: ANSSystem | None = entry_data.get("system")
    task_queue = system.task_queue if system else None
    housekeeping_scheduler = system.housekeeping_scheduler if system else None
    volume_registry = entry_data.get("volume_registry")
    deduplication_service = system.deduplication_service if system else None
    lifecycle_manager = system.lifecycle_manager if system else None

    if task_queue:
        await task_queue.stop()
        _LOGGER.debug("ANS task queue stopped")

    if housekeeping_scheduler:
        await housekeeping_scheduler.stop()
        _LOGGER.debug("ANS housekeeping scheduler stopped")

    if volume_registry:
        await volume_registry.async_unload()
        _LOGGER.debug("Volume restoration registry unloaded")

    if deduplication_service:
        await deduplication_service.stop()
        _LOGGER.debug("ANS deduplication service stopped")

    if lifecycle_manager:
        await lifecycle_manager.cleanup_all()
        _LOGGER.debug("ANS adapter lifecycle manager cleaned up")


async def cleanup_entry_data(hass: HomeAssistant, entry_id: str) -> None:
    """Clean up any partially initialized data for a config entry."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return
    entry_data = getattr(entry, "runtime_data", None)
    if entry_data:
        await _teardown_entry_components(entry_data)
        entry.runtime_data = {}


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload integration resources for a config entry.

    - Stop task queue and housekeeping
    - Cleanup all adapters
    - Cleanup hass.data
    - Return True if fully unloaded, False otherwise.
    """
    _LOGGER.debug("Unloading Advanced Notification System entry: %s", entry.entry_id)

    entry_data = getattr(entry, "runtime_data", None)
    if entry_data:
        await _teardown_entry_components(entry_data)
        entry.runtime_data = {}

    # Return True because no platform unloading is needed
    return True


async def update_listener(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Handle options update."""
    # Access runtime data directly via config_entry to avoid the overhead of
    # scanning all config entries with get_main_entry().
    entry_data: dict | None = getattr(config_entry, "runtime_data", None)
    config_repository: ConfigRepository | None = (
        entry_data.get("config_repository") if entry_data else None
    )
    if not config_repository:
        _LOGGER.error("Config repository not found")
        return

    system: ANSSystem | None = entry_data.get("system") if entry_data else None

    # Snapshot mutable state before unloading so the system can be kept
    # operational if the subsequent reload fails.
    previous_system_config = config_repository.system_config
    previous_recipients = dict(config_repository.recipients)
    previous_recipient_configs = dict(config_repository.recipient_configs)

    if not config_repository.unload():
        _LOGGER.error("Config repository failed to unload; aborting options update")
        return

    if not await config_repository.load():
        _LOGGER.error(
            "Config repository failed to reload; restoring previous state to keep "
            "the system operational"
        )
        config_repository.system_config = previous_system_config
        config_repository.recipients = previous_recipients
        config_repository.recipient_configs = previous_recipient_configs
        return

    _LOGGER.debug("Config repository successfully applied the latest changes")

    # Only update runtime components if system_config is available
    # (it might not be during initial sub-entry creation)
    if config_repository.system_config:
        # Guard against TOCTOU: between the system_config check above and the
        # snapshot() call below, a concurrent async_unload_entry() could tear
        # down runtime_data, leaving the config repository in an inconsistent
        # state and causing snapshot() to raise RuntimeError.
        try:
            snapshot = config_repository.snapshot()
        except RuntimeError:
            _LOGGER.warning(
                "Config repository became unavailable during options update "
                "(concurrent unload?); skipping runtime component update"
            )
            return
        system_config = snapshot.system_config

        # Update rate limiter with new global rate limits
        rate_limiter = system.rate_limiter if system else None
        if rate_limiter:
            rate_limiter.update_limits(
                global_rate_limit=system_config.global_rate_limit,
                rate_limit_window=system_config.rate_limit_window,
            )
            _LOGGER.debug(
                "Rate limiter updated with new limits: max=%s, window=%s",
                system_config.global_rate_limit,
                system_config.rate_limit_window,
            )
        else:
            _LOGGER.warning("Rate limiter not found, unable to update limits")

        # Update task queue concurrency
        task_queue = system.task_queue if system else None
        if task_queue:
            await task_queue.update_concurrency(system_config.queue_max_concurrency)
            _LOGGER.debug(
                "Task queue concurrency updated to %d",
                system_config.queue_max_concurrency,
            )
        else:
            _LOGGER.warning("Task queue not found, unable to update concurrency")

        # Re-sync adapters with updated enabled channels and detected channels
        lifecycle_manager = system.lifecycle_manager if system else None
        if lifecycle_manager:
            await config_repository.refresh_and_sync(lifecycle_manager)
            _LOGGER.debug("Adapter lifecycle manager synced with new enabled channels")
        else:
            _LOGGER.warning("Lifecycle manager not found, unable to sync adapters")
    else:
        _LOGGER.debug("System config not yet loaded, skipping runtime component update")


def _get_entry_data(hass: HomeAssistant) -> dict | None:
    """Return the main entry's runtime data dict, or None if unavailable."""
    entry = get_main_entry(hass)
    if entry is None:
        return None
    return getattr(entry, "runtime_data", None)


def get_rate_limiter(hass: HomeAssistant):
    """Retrieve the rate limiter from the main entry data."""
    entry_data = _get_entry_data(hass)
    if entry_data is None:
        return None
    system: ANSSystem | None = entry_data.get("system")
    return system.rate_limiter if system else None


def get_task_queue(hass: HomeAssistant):
    """Retrieve the task queue from the main entry data."""
    entry_data = _get_entry_data(hass)
    if entry_data is None:
        return None
    system: ANSSystem | None = entry_data.get("system")
    return system.task_queue if system else None


def get_lifecycle_manager(hass: HomeAssistant):
    """Retrieve the adapter lifecycle manager from the main entry data."""
    entry_data = _get_entry_data(hass)
    if entry_data is None:
        return None
    system: ANSSystem | None = entry_data.get("system")
    return system.lifecycle_manager if system else None


def get_config_repository(hass: HomeAssistant) -> ConfigRepository | None:
    """Retrieve the config repository from the main entry data."""
    entry_data = _get_entry_data(hass)
    return entry_data.get("config_repository") if entry_data else None
