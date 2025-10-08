"""Service detection helpers for Advanced Notification System."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .models import IntegrationInfo

_LOGGER = logging.getLogger(__name__)


async def async_detect_notification_integrations(
    hass: HomeAssistant,
) -> list[IntegrationInfo]:
    """Discover available notification channels (notify.* services).

    Returns:
        List of NotificationChannels:
        {
            "id": str,         # Full service name, e.g. "notify.mobile_app_john"
            "label": str,      # Human-friendly label
            "integration": str,# Integration domain, e.g. "mobile_app"
            "service": str     # Service id, e.g. "mobile_app_john"
        }

    """
    services = hass.services.async_services()
    notify_services = services.get("notify", {})

    results: list[IntegrationInfo] = []
    for service_id in sorted(notify_services.keys()):
        # integration = _guess_integration_from_service(service_id)
        if service_id in ("notify", "send_message"):
            continue  # Skip unsupported services
        label = format_channel_label(service_id)
        results.append(
            IntegrationInfo(
                f"notify.{service_id}",
                label,
                service_id,
            )
        )
        _LOGGER.debug("Notification service detected: %s", service_id)

    # _LOGGER.debug("Detected notify services: %s", results)
    return results


async def async_detect_tts_integrations(hass: HomeAssistant) -> list[IntegrationInfo]:
    """Discover available TTS integrations.

    Returns:
        List of dicts:
        {
            "id": str,         # TTS service id
            "label": str,      # Human-friendly label
            "integration": str # Integration domain
        }

    """
    services = hass.services.async_services()
    tts_services = services.get("tts", {})

    results: list[IntegrationInfo] = []
    for service_id in sorted(tts_services.keys()):
        # integration = _guess_integration_from_service(service_id)
        label = format_channel_label(service_id)
        results.append(
            IntegrationInfo(
                f"tts.{service_id}",
                label,
                service_id,
            )
        )

    _LOGGER.debug("Detected TTS integrations: %s", results)
    return results


def format_channel_label(service_id: str) -> str:
    """Return a readable label for a channel.

    The label should be human-friendly, e.g., "Mobile App (John)" or "Email (Work)".
    Falls back to service_id if no better info is available.
    """
    # friendly_integration = integration.replace("_", " ").title()
    # friendly_service = service_id.replace("_", " ").title()

    # if integration and integration != service_id:
    #     label = f"{friendly_integration} ({friendly_service})"
    # else:
    #     label = friendly_service

    # return label
    return service_id.replace("_", " ").title()


def pretty_channel_name(channel_id: str) -> str:
    """Return a pretty name for a channel based on its service ID.

    Example:
        mobile_app_john -> Mobile App (John)
        email_work -> Email (Work)

    """
    channel_id = channel_id.removeprefix("notify.")
    # if "_" in service_id:
    #     parts = service_id.split("_", 1)
    #     integration = parts[0].replace("_", " ").title()
    #     specific = parts[1].replace("_", " ").title()
    #     return f"{integration} ({specific})"
    return channel_id.replace("_", " ").title()


# def _guess_integration_from_service(service_id: str) -> str:
#     """Attempt to guess the integration domain from a service name.

#     Example:
#         mobile_app_john -> mobile_app
#         email_work -> email

#     """
#     if "_" in service_id:
#         return service_id.split("_", 1)[0]
#     return service_id


# async def get_all_ha_users(hass: HomeAssistant) -> list[dict[str, Any]]:
#     """Return all HA users (id + name)."""
#     # TODO: harden this code incl. error handling (try/except) and logging
#     users = await hass.auth.async_get_users()
#     return [
#         {
#             "id": u.id,
#             "name": u.name or "Unnamed User",
#         }
#         for u in users
#     ]


def get_main_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """Return the main ANS config entry or None if not found."""
    entries = list(hass.config_entries.async_entries(DOMAIN))
    # Prefer the entry that has unique_id == DOMAIN (main entry created in main flow)
    for entry in entries:
        try:
            if getattr(entry, "unique_id", None) == DOMAIN:
                return entry
        except Exception:
            continue
    # Fallback to first if present
    return entries[0] if entries else None


def get_subentries(hass: HomeAssistant) -> list[ConfigSubentry]:
    """Return all ANS subentries or an empty list if none found."""
    main_entry = get_main_entry(hass)
    if main_entry:
        return list(main_entry.subentries.values())
    raise  # TODO: raise a proper exception


async def async_check_receiver_name_availability(
    hass: HomeAssistant, name: str
) -> bool:
    """Check if the receiver name is already used."""
    # Get all config entries for ANS domain
    main_entry = get_main_entry(hass)
    # Check if name is already used
    if main_entry:
        for subentry in main_entry.subentries.values():
            if subentry.data["name"] == name:
                return False
    else:
        # TODO: raise a proper exception
        raise
    return True


async def get_not_configured_ha_users(hass: HomeAssistant) -> dict[str, Any]:
    """Return all HA users that are not yet configured an ANS receiver."""
    # TODO: harden this code incl. error handling (try/except) and logging
    users = await hass.auth.async_get_users()
    configured_users: dict[str, str] = {}

    # Get all config entries for ANS domain
    main_entry = get_main_entry(hass)
    if main_entry:
        for subentry in main_entry.subentries.values():
            configured_users[subentry.data["id"]] = subentry.data["name"]
        return {u.id: u.name for u in users if u.id not in configured_users}

    return {u.id: u.name for u in users}
