"""Diagnostics support for ANS integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .models import ChannelScope

_LOGGER = logging.getLogger(__name__)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Parameters
    ----------
    hass : HomeAssistant
        Home Assistant instance.
    entry : ConfigEntry
        Config entry to get diagnostics for.

    Returns
    -------
    dict[str, Any]
        Diagnostics data.

    """
    diagnostics: dict[str, Any] = {
        "entry_id": entry.entry_id,
        "version": entry.version,
    }

    # Get config repository
    # Local import to avoid potential circular dependency
    from . import get_config_repository  # noqa: PLC0415

    config_repo = get_config_repository(hass)
    if not config_repo:
        diagnostics["error"] = "Config repository not initialized"
        return diagnostics

    # Channel registry info
    channel_registry = config_repo.channel_registry
    diagnostics["channels"] = {
        "total": channel_registry.count(),
        "by_scope": {
            "system": len(channel_registry.filter_by_scope(ChannelScope.SYSTEM)),
            "recipient": len(channel_registry.filter_by_scope(ChannelScope.RECIPIENT)),
        },
        "channels": [
            {
                "id": ch.id,
                "label": ch.label,
                "scope": ch.scope.value,
                "integration": ch.integration,
            }
            for ch in channel_registry.get_all()
        ],
    }

    # Adapter registry info
    if DOMAIN in hass.data:
        for entry_data in hass.data[DOMAIN].values():
            if "adapter_registry" in entry_data:
                adapter_registry = entry_data["adapter_registry"]

                diagnostics["adapters"] = {
                    "total": len(adapter_registry.channels()),
                    "channels": adapter_registry.channels(),
                }

                # Validation results
                validation = channel_registry.validate_adapters(adapter_registry)
                diagnostics["validation"] = {
                    "missing_adapters": validation["missing_adapters"],
                    "orphaned_adapters": validation["orphaned_adapters"],
                    "status": "ok" if not validation["missing_adapters"] else "warning",
                }

                break

    # System config
    if config_repo.system_config:
        diagnostics["system_config"] = {
            "enabled_channels": list(config_repo.system_config.enabled_channels),
        }

    # Recipient count
    diagnostics["recipients"] = {
        "total": len(config_repo.recipient_configs),
        "types": {},
    }

    # Count by recipient type
    for recipient_data in config_repo.recipients.values():
        recipient_type = recipient_data.type.value
        diagnostics["recipients"]["types"][recipient_type] = (
            diagnostics["recipients"]["types"].get(recipient_type, 0) + 1
        )

    return diagnostics
