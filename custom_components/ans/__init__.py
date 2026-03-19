"""Advanced Notification System integration bootstrap."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from homeassistant.components.media_player.const import MediaPlayerEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_SERVICE_REGISTERED
from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.entity_registry import (
    EVENT_ENTITY_REGISTRY_UPDATED,
    EventEntityRegistryUpdatedData,
)
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_track_state_added_domain

from .config.repository import ConfigRepository
from .const import (
    SYS_DEFAULT_QUEUE_CONCURRENCY,
)
from .delivery.factory import ANSSystem, create_system
from .helper import get_main_entry
from .persistence.recovery import async_initialize_persistence
from .persistence.volume_restoration import VolumeRestorationRegistry
from .service import async_setup_services

_LOGGER = logging.getLogger(__name__)

_REQUIRED_MP_FEATURES = (
    MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.VOLUME_SET
)


# ---------------------------------------------------------------------------
# Entry-level helpers (A3: decomposed from async_setup_entry)
# ---------------------------------------------------------------------------


async def _setup_config(hass: HomeAssistant, entry: ConfigEntry) -> ConfigRepository:
    """Load and validate configuration.  Raises ConfigEntryNotReady on failure.

    Parameters
    ----------
    hass : HomeAssistant
    entry : ConfigEntry

    Returns
    -------
    ConfigRepository
        Fully loaded repository (system_config + recipients).

    Raises
    ------
    ConfigEntryNotReady
        When the main config entry cannot be loaded.

    """
    _LOGGER.info("[ANS setup] Phase 1/5 — loading configuration")
    config_repo = ConfigRepository(hass)
    if not await config_repo.load():
        raise ConfigEntryNotReady("Failed to load main entry configuration")
    _LOGGER.debug("[ANS setup] Configuration loaded")
    return config_repo


async def _setup_system(
    hass: HomeAssistant,
    config_repo: ConfigRepository,
    volume_registry: VolumeRestorationRegistry,
) -> ANSSystem:
    """Create the ANSSystem and run the initial channel sync.

    Parameters
    ----------
    hass : HomeAssistant
    config_repo : ConfigRepository
    volume_registry : VolumeRestorationRegistry
        Must be already loaded before this call.

    Returns
    -------
    ANSSystem

    """
    _LOGGER.info("[ANS setup] Phase 2/5 — creating system components")
    system = create_system(
        hass=hass,
        config_repo=config_repo,
        volume_registry=volume_registry,
        max_concurrent_deliveries=SYS_DEFAULT_QUEUE_CONCURRENCY,
    )

    # Sync dynamic adapters (ChannelManager.sync is async; create_system is sync)
    if config_repo.system_config:
        await system.channel_manager.sync(
            list(config_repo.system_config.enabled_channels)
        )
        _LOGGER.info(
            "[ANS setup] Channel sync complete: %d detected, %d active",
            system.channel_manager.count_detected(),
            system.channel_manager.count_active(),
        )
    return system


async def _setup_persistence(
    hass: HomeAssistant, system: ANSSystem
) -> tuple[list, list]:
    """Load persistence stores and recover pending retries.

    Parameters
    ----------
    hass : HomeAssistant
    system : ANSSystem

    Returns
    -------
    tuple[list, list]
        ``(pending_tasks, orphaned_retries)``

    """
    _LOGGER.info("[ANS setup] Phase 3/5 — initializing persistence")
    pending_tasks, orphaned_retries = await async_initialize_persistence(
        hass,
        system.notification_registry,
        system.attempt_log,
        system.retry_queue,
    )
    _LOGGER.info(
        "[ANS setup] Persistence initialized: %d tasks to recover, %d orphaned",
        len(pending_tasks),
        len(orphaned_retries),
    )
    return pending_tasks, orphaned_retries


async def _setup_tasks(
    system: ANSSystem,
    pending_tasks: list,
    orphaned_retries: list,
) -> None:
    """Start background workers and schedule recovered retries.

    Parameters
    ----------
    system : ANSSystem
    pending_tasks : list
        ``(task, scheduled_time)`` pairs from persistence recovery.
    orphaned_retries : list
        Job IDs whose task data could not be recovered.

    """
    _LOGGER.info("[ANS setup] Phase 4/5 — starting background tasks")
    await system.task_queue.start()
    _LOGGER.debug("[ANS setup] Task queue started")

    await system.housekeeping_scheduler.start()
    _LOGGER.debug("[ANS setup] Housekeeping scheduler started")

    await system.deduplication_service.start()
    _LOGGER.debug("[ANS setup] Deduplication service started")

    now = datetime.now(UTC)
    for task, scheduled_time in pending_tasks:
        delay_seconds = max((scheduled_time - now).total_seconds(), 0)
        if delay_seconds == 0:
            _LOGGER.info(
                "[ANS setup] Retry for job %s is overdue, executing immediately",
                task.job_id,
            )
        await system.task_queue.add_task(task, delay=timedelta(seconds=delay_seconds))

    for job_id_str in orphaned_retries:
        _LOGGER.warning(
            "[ANS setup] Removing orphaned retry schedule for job %s (no task data)",
            job_id_str,
        )
        try:
            await system.retry_queue.remove_retry(UUID(job_id_str))
        except ValueError:
            _LOGGER.error("[ANS setup] Invalid UUID for orphaned retry: %s", job_id_str)


async def _setup_services(hass: HomeAssistant, system: ANSSystem) -> None:
    """Register ANS HA service handlers."""
    _LOGGER.info("[ANS setup] Phase 5/5 — registering services and listeners")
    await async_setup_services(hass, system.orchestrator)
    _LOGGER.debug("[ANS setup] Services registered")


def _setup_listeners(
    hass: HomeAssistant,
    entry: ConfigEntry,
    config_repo: ConfigRepository,
) -> None:
    """Register all event listeners for the lifetime of this config entry.

    Fixes applied here
    ------------------
    L1  update_listener → simple reload
    L3  _on_media_player_added gates on required feature flags
    L4  _on_entity_registry_updated handles media_player removals

    Parameters
    ----------
    hass : HomeAssistant
    entry : ConfigEntry
    config_repo : ConfigRepository

    """

    # L1 — Options update → clean reload (avoids partial-state in-place updates)
    async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
        await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    # Notify service registration → resync channels
    async def _on_notify_service_registered(event: Event[Any]) -> None:
        if event.data.get("domain") != "notify":
            return
        service = event.data.get("service", "")
        if service in ("notify", "send_message"):
            return
        _LOGGER.debug(
            "New notify service 'notify.%s' registered — refreshing ANS channels",
            service,
        )
        try:
            await config_repo.refresh_and_sync()
        except Exception:
            _LOGGER.exception(
                "Failed to refresh channels after notify service 'notify.%s' was registered",
                service,
            )

    entry.async_on_unload(
        hass.bus.async_listen(EVENT_SERVICE_REGISTERED, _on_notify_service_registered)
    )

    # L3 — New media_player added → resync only if it has required features
    async def _on_media_player_added(event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new_state = event.data.get("new_state")
        supported = (
            new_state.attributes.get("supported_features", 0) if new_state else 0
        )
        if (supported & _REQUIRED_MP_FEATURES) != _REQUIRED_MP_FEATURES:
            _LOGGER.debug(
                "Ignoring media_player '%s': missing required features (0x%x)",
                entity_id,
                supported,
            )
            return
        _LOGGER.debug(
            "Capable media_player '%s' added — refreshing ANS channels", entity_id
        )
        try:
            await config_repo.refresh_and_sync()
        except Exception:
            _LOGGER.exception(
                "Failed to refresh channels after media_player '%s' was added",
                entity_id,
            )

    entry.async_on_unload(
        async_track_state_added_domain(hass, "media_player", _on_media_player_added)
    )

    # L4 — media_player entity removed → resync to mark channel STALE
    async def _on_entity_registry_updated(
        event: Event[EventEntityRegistryUpdatedData],
    ) -> None:
        if event.data["action"] != "remove":
            return
        entity_id = event.data["entity_id"]
        if not entity_id.startswith("media_player."):
            return
        _LOGGER.debug(
            "media_player entity '%s' removed — refreshing ANS channels",
            entity_id,
        )
        try:
            await config_repo.refresh_and_sync()
        except Exception:
            _LOGGER.exception(
                "Failed to refresh channels after media_player '%s' was removed",
                entity_id,
            )

    entry.async_on_unload(
        hass.bus.async_listen(
            EVENT_ENTITY_REGISTRY_UPDATED, _on_entity_registry_updated
        )
    )


# ---------------------------------------------------------------------------
# Config entry lifecycle
# ---------------------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ANS integration for a config entry."""
    _LOGGER.info("Setting up ANS config entry: %s", entry.entry_id)

    try:
        entry_data: dict = {}
        entry.runtime_data = entry_data

        # Phase 1 — configuration
        config_repo = await _setup_config(hass, entry)
        entry_data["config_repository"] = config_repo

        snapshot = config_repo.snapshot()
        if not snapshot.getRecipients():
            _LOGGER.warning(
                "ANS config entry %s has no recipients configured. "
                "Notifications will be dropped.",
                entry.entry_id,
            )

        # Phase 2 — system (ChannelManager injected into config_repo inside create_system)
        volume_registry = VolumeRestorationRegistry(hass)
        await volume_registry.async_load()
        entry_data["volume_registry"] = volume_registry
        _LOGGER.debug("[ANS setup] Volume restoration registry initialized")

        system = await _setup_system(hass, config_repo, volume_registry)
        entry_data["system"] = system

        # Phase 3 — persistence
        pending_tasks, orphaned_retries = await _setup_persistence(hass, system)

        # Phase 4 — background tasks + retry recovery
        await _setup_tasks(system, pending_tasks, orphaned_retries)

        # Phase 5 — HA services
        await _setup_services(hass, system)

        # Register event listeners
        _setup_listeners(hass, entry, config_repo)

        _LOGGER.info("Successfully set up ANS config entry: %s", entry.entry_id)

    except Exception as e:
        _LOGGER.error("Failed to set up ANS config entry %s: %s", entry.entry_id, e)
        await cleanup_entry_data(hass, entry.entry_id)
        if isinstance(e, ConfigEntryNotReady):
            raise
        raise ConfigEntryNotReady(f"Setup failed: {e}") from e

    return True


async def _teardown_entry_components(entry_data: dict) -> None:
    """Stop and clean up all running components for a config entry."""
    system: ANSSystem | None = entry_data.get("system")
    volume_registry = entry_data.get("volume_registry")

    if system:
        await system.task_queue.stop()
        _LOGGER.debug("ANS task queue stopped")

        await system.housekeeping_scheduler.stop()
        _LOGGER.debug("ANS housekeeping scheduler stopped")

        await system.deduplication_service.stop()
        _LOGGER.debug("ANS deduplication service stopped")

        await system.channel_manager.cleanup_all()
        _LOGGER.debug("ANS channel manager cleaned up")

    if volume_registry:
        await volume_registry.async_unload()
        _LOGGER.debug("Volume restoration registry unloaded")


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
    """Unload integration resources for a config entry."""
    _LOGGER.debug("Unloading Advanced Notification System entry: %s", entry.entry_id)
    entry_data = getattr(entry, "runtime_data", None)
    if entry_data:
        await _teardown_entry_components(entry_data)
        entry.runtime_data = {}
    return True


# ---------------------------------------------------------------------------
# Module-level accessors (used by services, diagnostics, etc.)
# ---------------------------------------------------------------------------


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


def get_channel_manager(hass: HomeAssistant):
    """Retrieve the ChannelManager from the main entry data."""
    entry_data = _get_entry_data(hass)
    if entry_data is None:
        return None
    system: ANSSystem | None = entry_data.get("system")
    return system.channel_manager if system else None


def get_config_repository(hass: HomeAssistant) -> ConfigRepository | None:
    """Retrieve the config repository from the main entry data."""
    entry_data = _get_entry_data(hass)
    return entry_data.get("config_repository") if entry_data else None
