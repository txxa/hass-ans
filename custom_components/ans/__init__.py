"""Advanced Notification System integration bootstrap."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .config_repository import ConfigRepository
from .const import (
    DOMAIN,
    SYS_DEFAULT_QUEUE_CONCURRENCY,
)
from .factory import NotificationSystemSetup
from .persistence_recovery import async_initialize_persistence
from .service import async_setup_services

_LOGGER = logging.getLogger(__name__)


# async def _setup_main_entry(
#     hass: HomeAssistant, entry: ConfigEntry, entry_data: dict[str, Any]
# ) -> bool:
#     """Set up the main entry for the integration."""
#     _LOGGER.debug("Setting up main ANS entry: %s", entry.entry_id)

#     try:
#         # Initialize the config repository for this main entry
#         config_repo = ConfigRepository(hass, ConfigValidator())

#         # Load the main entry configuration into the repository
#         if not config_repo.load():
#             _LOGGER.error("Failed to load main entry configuration")
#             raise ConfigEntryNotReady("Failed to load main entry configuration")

#         # Store the config repository in the entry data
#         entry_data["config_repository"] = config_repo
#         entry_data["entry_type"] = "main"

#         # TODO: Initialize any services or platforms here
#         # For example:
#         # - Register notification services
#         # - Set up background tasks for rate limiting
#         # - Initialize TTS integration if configured

#         _LOGGER.info("Successfully set up main ANS entry: %s", entry.entry_id)

#     except Exception as e:
#         _LOGGER.error("Failed to set up main ANS entry %s: %s", entry.entry_id, e)
#         raise ConfigEntryNotReady(f"Main entry setup failed: {e}") from e

#     else:
#         return True


# async def _setup_subentry(
#     hass: HomeAssistant, entry: ConfigEntry, entry_data: dict[str, Any]
# ) -> bool:
#     """Set up a subentry for the integration."""
#     # TODO: Implement logic to set up a subentry
#     return True  # Placeholder for subentry setup logic


# def _is_subentry(entry: ConfigEntry) -> bool:
#     """Check if the entry is a subentry."""
#     # TODO: Implement logic to determine if this is a subentry
#     if entry.data.get(ID_CONFIG_PARENT_ENTRY_ID_KEY):
#         return True
#     return False  # Placeholder for subentry check logic


# async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
#     """Bootstrap at HA startup. Return True on success."""
#     hass.data.setdefault(DOMAIN, {})
#     return True  # Only config flow entries are supported


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up integration for a config entry."""

    _LOGGER.info("Setting up ANS config entry: %s", entry.entry_id)

    try:
        # Initialize domain data for this entry
        hass.data.setdefault(DOMAIN, {})
        if DOMAIN not in hass.data:
            hass.data[DOMAIN] = {}

        entry_data = hass.data[DOMAIN].setdefault(entry.entry_id, {})

        # Initialize the config repository for this main entry
        config_repo = ConfigRepository(hass)

        # Load the main entry configuration into the repository
        if not await config_repo.load():
            _LOGGER.error("Failed to load main entry configuration")
            raise ConfigEntryNotReady("Failed to load main entry configuration")

        _LOGGER.debug("Config repository loaded successfully")

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

        # Create and initialize the complete notification system
        # Note: enable_audit_logging is now read from SystemConfig inside factory
        system = NotificationSystemSetup.create_system(
            hass=hass,
            config_repo=config_repo,
            max_concurrent_deliveries=SYS_DEFAULT_QUEUE_CONCURRENCY,
        )

        # Store system components in entry data
        entry_data.update(system)

        # Initialize persistence and recover pending retries
        (
            notification_registry,
            attempt_log,
            retry_queue,
            pending_tasks,
            orphaned_retries,
        ) = await async_initialize_persistence(hass)
        _LOGGER.info(
            "ANS persistence initialized: %d tasks to recover, %d orphaned",
            len(pending_tasks),
            len(orphaned_retries),
        )

        # Start background tasks BEFORE scheduling retries
        task_queue = system["task_queue"]
        housekeeping_scheduler = system["housekeeping_scheduler"]

        await task_queue.start()
        _LOGGER.debug("ANS task queue started")

        await housekeeping_scheduler.start()
        _LOGGER.debug("ANS housekeeping scheduler started")

        # Schedule recovered tasks for retry
        from datetime import UTC, datetime, timedelta  # noqa: PLC0415

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
            from uuid import UUID  # noqa: PLC0415

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
        orchestrator = system["orchestrator"]
        await async_setup_services(hass, orchestrator)
        _LOGGER.debug("ANS services registered")

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


async def cleanup_entry_data(hass: HomeAssistant, entry_id: str) -> None:
    """Clean up any partially initialized data for a config entry."""
    if DOMAIN in hass.data and entry_id in hass.data[DOMAIN]:
        # Implement any necessary cleanup logic here
        hass.data[DOMAIN].pop(entry_id, None)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload integration resources for a config entry.

    - Stop task queue and housekeeping
    - Unregister services
    - Cleanup hass.data
    - Return True if fully unloaded, False otherwise.
    """
    _LOGGER.debug("Unloading Advanced Notification System entry: %s", entry.entry_id)

    if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
        entry_data = hass.data[DOMAIN][entry.entry_id]

        # Stop background tasks
        task_queue = entry_data.get("task_queue")
        housekeeping_scheduler = entry_data.get("housekeeping_scheduler")

        if task_queue:
            await task_queue.stop()
            _LOGGER.debug("ANS task queue stopped")

        if housekeeping_scheduler:
            await housekeeping_scheduler.stop()
            _LOGGER.debug("ANS housekeeping scheduler stopped")

        # Clean up entry data
        hass.data[DOMAIN].pop(entry.entry_id, None)

    # Return True because no platform unloading is needed
    return True


async def update_listener(hass: HomeAssistant, config_entry: ConfigEntry):
    """Handle options update."""
    config_repository = get_config_repository(hass)
    if config_repository:
        if config_repository.unload() and await config_repository.load():
            _LOGGER.debug("Config repository successfully applied the latest changes")

            # Only update rate limiter if system_config is available
            # (it might not be during initial sub-entry creation)
            if config_repository.system_config:
                # Update rate limiter with new global rate limits
                snapshot = config_repository.snapshot()
                system_config = snapshot.system_config
                rate_limiter = get_rate_limiter(hass)
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
            else:
                _LOGGER.debug(
                    "System config not yet loaded, skipping rate limiter update"
                )
        else:
            _LOGGER.error("Config repository was unable to apply the latest changes")
    else:
        _LOGGER.error("Config repository not found")


def get_rate_limiter(hass: HomeAssistant):
    """Retrieve the rate limiter from the main entry data."""
    if DOMAIN not in hass.data:
        return None

    # Find main entry data
    for entry_data in hass.data[DOMAIN].values():
        if isinstance(entry_data, dict) and "rate_limiter" in entry_data:
            return entry_data["rate_limiter"]

    return None


def get_config_repository(hass: HomeAssistant) -> ConfigRepository | None:
    """Retrieve the config repository from the main entry data."""
    if DOMAIN not in hass.data:
        return None

    # Find main entry data
    for entry_data in hass.data[DOMAIN].values():
        if isinstance(entry_data, dict) and "config_repository" in entry_data:
            return entry_data["config_repository"]

    return None
