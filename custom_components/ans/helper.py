"""Helper utilities for Advanced Notification System.

This module contains general-purpose helper functions for:
- UI formatting and label generation
- Config entry management
- Validation utilities
- Form data conversion

Note: Channel detection functions live in ``channels.channel_manager`` as
module-level functions (``detect_notification_channels``, ``detect_media_players``).
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import SelectOptionDict

from .const import DOMAIN, RCPT_MAX_RATE_LIMIT
from .exceptions import ConfigEntryNotFoundError
from .models import ChannelInfo

_LOGGER = logging.getLogger(__name__)

# Key names used inside subentry data dictionaries.
_SUBENTRY_DATA_ID_KEY = "id"
_SUBENTRY_DATA_NAME_KEY = "name"


def get_main_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """Return the main ANS config entry, or ``None`` if it has not been set up.

    Preference is given to the entry whose ``unique_id`` equals ``DOMAIN`` (the
    canonical main entry created by the primary config flow).  If no such entry
    exists the first available entry for the domain is returned as a fallback.

    Args:
        hass: The Home Assistant instance.

    Returns:
        The main :class:`ConfigEntry` for the ANS domain, or ``None``.

    """
    entries = hass.config_entries.async_entries(DOMAIN)
    for entry in entries:
        if entry.unique_id == DOMAIN:
            return entry
    return entries[0] if entries else None


def get_subentries(hass: HomeAssistant) -> list[ConfigSubentry]:
    """Return all ANS sub-entries (recipients).

    Args:
        hass: The Home Assistant instance.

    Returns:
        List of :class:`ConfigSubentry` objects registered under the main entry.

    Raises:
        ConfigEntryNotFoundError: If the main ANS config entry is not found.

    """
    main_entry = get_main_entry(hass)
    if main_entry is None:
        raise ConfigEntryNotFoundError("ANS main config entry not found")
    return list(main_entry.subentries.values())


def check_recipient_name_availability(hass: HomeAssistant, name: str) -> bool:
    """Return ``True`` if *name* is not yet used by any configured recipient.

    The check is case-sensitive and compares against the ``name`` field stored
    in each sub-entry's data dictionary.

    Args:
        hass: The Home Assistant instance.
        name: Proposed recipient name to validate.

    Returns:
        ``True`` when the name is available, ``False`` when already taken.

    Raises:
        ConfigEntryNotFoundError: If the main ANS config entry is not found.

    """
    main_entry = get_main_entry(hass)
    if main_entry is None:
        raise ConfigEntryNotFoundError("ANS main config entry not found")

    for subentry in main_entry.subentries.values():
        existing_name = subentry.data.get(_SUBENTRY_DATA_NAME_KEY)
        if existing_name == name:
            _LOGGER.debug(
                "Recipient name '%s' is already taken by subentry %s",
                name,
                subentry.subentry_id,
            )
            return False

    return True


async def get_not_configured_ha_users(hass: HomeAssistant) -> dict[str, str | None]:
    """Return HA users that do not yet have an ANS recipient configured.

    Fetches all Home Assistant user accounts and filters out those that are
    already referenced by an existing ANS sub-entry.  If the main config entry
    is absent every user is considered unconfigured.

    Args:
        hass: The Home Assistant instance.

    Returns:
        Mapping of HA user ID → display name for users without an ANS
        recipient.  The display name may be ``None`` for system accounts
        that have no name set.

    """
    try:
        users = await hass.auth.async_get_users()
    except Exception:
        _LOGGER.exception("Failed to retrieve Home Assistant users")
        return {}

    main_entry = get_main_entry(hass)
    if main_entry is None:
        _LOGGER.debug(
            "ANS main config entry not found; treating all HA users as unconfigured"
        )
        return {u.id: u.name for u in users}

    configured_ids: set[str] = set()
    for subentry in main_entry.subentries.values():
        user_id = subentry.data.get(_SUBENTRY_DATA_ID_KEY)
        if user_id is not None:
            configured_ids.add(user_id)

    unconfigured = {u.id: u.name for u in users if u.id not in configured_ids}
    _LOGGER.debug(
        "Found %d unconfigured HA user(s) out of %d total",
        len(unconfigured),
        len(users),
    )
    return unconfigured


def calculate_suggested_rate_limit(global_limit: int) -> int:
    """Calculate a suggested per-recipient rate limit based on the global limit.

    Uses 20 % of the global limit as the suggested per-recipient value, so the
    system can accommodate roughly five concurrent recipients at full throughput
    before the global cap is reached.  The result is bounded between 1 and
    ``RCPT_MAX_RATE_LIMIT``.

    Args:
        global_limit: The system-wide global rate limit (notifications/minute).
            Must be a positive integer.

    Returns:
        Suggested per-recipient rate limit (notifications/minute).

    Raises:
        ValueError: If *global_limit* is not a positive integer.

    Examples:
        >>> calculate_suggested_rate_limit(100)
        20
        >>> calculate_suggested_rate_limit(10)
        2
        >>> calculate_suggested_rate_limit(1)
        1

    """
    if global_limit <= 0:
        raise ValueError(
            f"global_limit must be a positive integer, got {global_limit!r}"
        )

    # 20 % factor — allows ~5 concurrent recipients at full capacity.
    # Floor at 1 so at least one notification per minute is always possible.
    # Cap at RCPT_MAX_RATE_LIMIT to respect the system constraint.
    return max(1, min(int(global_limit * 0.2), RCPT_MAX_RATE_LIMIT))


def dict_to_select_options(data: dict[str, str | None]) -> list[SelectOptionDict]:
    """Convert a ``{value: label}`` dictionary to a selector options list.

    Args:
        data: Mapping of option *value* → human-readable *label*.  Values
            whose label is ``None`` are included with an empty string label so
            that every key is represented in the UI.

    Returns:
        List of :class:`SelectOptionDict` ready for use in HA form selectors.

    """
    return [
        SelectOptionDict(label=label or "", value=key) for key, label in data.items()
    ]


def channel_info_to_select_options(
    channels: list[ChannelInfo],
) -> list[SelectOptionDict]:
    """Convert :class:`ChannelInfo` objects to a selector options list.

    Args:
        channels: Ordered list of channel definitions to convert.

    Returns:
        List of :class:`SelectOptionDict` where each entry uses the channel
        ``id`` as the option value and ``label`` as the display text.

    """
    return [SelectOptionDict(label=ch.label, value=ch.id) for ch in channels]
