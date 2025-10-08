"""Advanced Notification System integration bootstrap."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .config_repository import ConfigRepository
from .const import DOMAIN

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
        if DOMAIN not in hass.data:
            hass.data[DOMAIN] = {}

        entry_data = hass.data[DOMAIN].setdefault(entry.entry_id, {})

        # # Set up a subentry
        # if _is_subentry(entry):
        #     return await _setup_subentry(hass, entry, entry_data)

        # # Set up the main entry
        # return await _setup_main_entry(hass, entry, entry_data)

        # Initialize the config repository for this main entry
        config_repo = ConfigRepository(hass)

        # Load the main entry configuration into the repository
        if not config_repo.load():
            _LOGGER.error("Failed to load main entry configuration")
            raise ConfigEntryNotReady("Failed to load main entry configuration")

        # Store the config repository in the entry data
        entry_data["config_repository"] = config_repo
        # entry_data["entry_type"] = "main"

        # TODO: Initialize any services or platforms here
        # For example:
        # - Register notification services
        # - Set up background tasks for rate limiting
        # - Initialize TTS integration if configured

        # Add config entry update listener to manually handle config entry changes
        entry.async_on_unload(entry.add_update_listener(update_listener))

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

    - Unregister services, stop notify driver, cleanup hass.data
    - Return True if fully unloaded, False otherwise.
    """
    _LOGGER.debug("Unloading Advanced Notification System entry: %s", entry.entry_id)

    # Here you would stop any background tasks or services if started in async_setup_entry
    # e.g., await hass.services.async_remove(...)

    hass.data[DOMAIN].pop(entry.entry_id, None)

    # Return True because no platform unloading is needed yet
    return True


async def update_listener(hass: HomeAssistant, config_entry: ConfigEntry):
    """Handle options update."""
    config_repository = get_config_repository(hass)
    if config_repository:
        if config_repository.unload() and config_repository.load():
            _LOGGER.debug(
                "Config repository successfully applied the latest config entry changes"
            )
        else:
            _LOGGER.error(
                "Config repository was unable to apply the latest config entry changes"
            )
    else:
        _LOGGER.error("Config repository not found")


def get_config_repository(hass: HomeAssistant) -> ConfigRepository | None:
    """Retrieve the config repository from the main entry data."""
    if DOMAIN not in hass.data:
        return None

    # Find main entry data
    for entry_data in hass.data[DOMAIN].values():
        if isinstance(entry_data, dict) and "config_repository" in entry_data:
            return entry_data["config_repository"]

    return None
