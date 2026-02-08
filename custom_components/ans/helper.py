"""Helper utilities for Advanced Notification System.

This module contains general-purpose helper functions for:
- UI formatting and label generation
- Config entry management
- Validation utilities
- Form data conversion

Note: Channel detection has been moved to ChannelRegistry for better
architectural consistency.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    SelectOptionDict,
)

from .const import DOMAIN, RCPT_MAX_RATE_LIMIT
from .exceptions import ConfigEntryNotFoundError
from .models import ChannelInfo

_LOGGER = logging.getLogger(__name__)


# Note: async_detect_notification_channels() and async_detect_tts_integrations()
# have been moved to ChannelRegistry.detect_notification_channels() and
# ChannelRegistry.detect_tts_integrations() for better architectural consistency.


def format_channel_label(service_id: str) -> str:
    """Return a readable label for a channel.

    .. deprecated::
        Channel label formatting has been moved to DeliveryAdapter.get_channel_label()
        for better architectural consistency. Each adapter now provides its own
        channel-specific formatting. This function is kept for backward compatibility
        and as a fallback for unknown channels.

    The label should be human-friendly, e.g., "Mobile App (John)" or "Email (Work)".
    Falls back to service_id if no better info is available.
    """
    # Split on first underscore to separate integration from specific identifier
    # if "_" in service_id:
    #     parts = service_id.split("_", 1)
    #     integration = parts[0].replace("_", " ").title()
    #     specific = parts[1].replace("_", " ").title()
    #     return f"{integration} ({specific})"
    return service_id.replace("_", " ").title()


# Commented-out helper functions kept for reference
# These may be needed in future implementations

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
        if getattr(entry, "unique_id", None) == DOMAIN:
            return entry
    # Fallback to first if present
    return entries[0] if entries else None


def get_subentries(hass: HomeAssistant) -> list[ConfigSubentry]:
    """Return all ANS subentries.

    Raises:
        ConfigEntryNotFoundError: If the main ANS config entry is not found.

    """
    main_entry = get_main_entry(hass)
    if main_entry:
        return list(main_entry.subentries.values())
    raise ConfigEntryNotFoundError("ANS main config entry not found")


async def async_check_recipient_name_availability(
    hass: HomeAssistant, name: str
) -> bool:
    """Check if the receiver name is already used.

    Raises:
        ConfigEntryNotFoundError: If the main ANS config entry is not found.

    """
    # Get all config entries for ANS domain
    main_entry = get_main_entry(hass)
    # Check if name is already used
    if main_entry:
        for subentry in main_entry.subentries.values():
            if subentry.data["name"] == name:
                return False
    else:
        raise ConfigEntryNotFoundError("ANS main config entry not found")
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


def calculate_suggested_rate_limit(global_limit: int) -> int:
    """Calculate a suggested per-recipient rate limit based on global limit.

    Uses 20% of the global limit as the suggested per-recipient rate limit,
    ensuring that the system can accommodate ~5 concurrent users at full capacity
    before hitting the global limit. Capped at DEFAULT_RATE_LIMIT_MAX.

    Args:
        global_limit: The system-wide global rate limit (notifications/minute).

    Returns:
        Suggested per-recipient rate limit (notifications/minute).
        - Minimum of 1 to ensure at least one notification per minute is possible
        - Maximum of DEFAULT_RATE_LIMIT_MAX (system constraint)

    Examples:
        calculate_suggested_rate_limit(100) → 20
        calculate_suggested_rate_limit(1000) → 200 (capped at 1000 max)
        calculate_suggested_rate_limit(10) → 2 (20% of 10, minimum enforced)

    """
    # Use 20% as the suggested factor (allows ~5 concurrent users)
    # Capped at system max to prevent excessive per-recipient limits
    return max(1, min(int(global_limit * 0.2), RCPT_MAX_RATE_LIMIT))


def dict_to_select_options_list(data: dict[str, str]) -> list[SelectOptionDict]:
    """Convert a dictionary to SelectOptionDict list for form selectors.

    Args:
        data: Dictionary with value->label mapping

    Returns:
        List of SelectOptionDict for use in form selectors

    """

    return [SelectOptionDict(label=value, value=key) for key, value in data.items()]


def channel_info_to_select_options(
    channels: list[ChannelInfo],
) -> list[SelectOptionDict]:
    """Convert ChannelInfo objects to select options for forms.

    Args:
        channels: List of ChannelInfo objects.

    Returns:
        List of SelectOptionDict for use in form selectors.

    """

    return [SelectOptionDict(label=ch.label, value=ch.id) for ch in channels]


# Note: filter_channels_by_recipient_type has been moved to
# ChannelRegistry.filter_channels_by_recipient_type() for better
# architectural consistency with other channel filtering methods.
