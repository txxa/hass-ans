"""Diagnostics support for ANS integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

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

    # Channel manager info
    channel_manager = config_repo.channel_manager
    if channel_manager:
        all_infos = channel_manager.get_all_infos()
        diagnostics["channels"] = {
            "detected": channel_manager.count_detected(),
            "active": channel_manager.count_active(),
            "by_scope": {
                "system": len([i for i in all_infos if i.scope == ChannelScope.SYSTEM]),
                "recipient": len(
                    [i for i in all_infos if i.scope == ChannelScope.RECIPIENT]
                ),
                "tts": len([i for i in all_infos if i.scope == ChannelScope.TTS]),
            },
            "records": [
                {
                    "id": rec.info.id,
                    "label": rec.info.label,
                    "scope": rec.info.scope.value,
                    "integration": rec.info.integration,
                    "status": rec.status.value,
                    "has_adapter": rec.adapter is not None,
                }
                for rec in channel_manager.get_all_records()
            ],
        }
    else:
        diagnostics["channels"] = {"error": "ChannelManager not initialized"}

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
