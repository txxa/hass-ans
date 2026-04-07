"""Diagnostics support for ANS integration."""

from __future__ import annotations

import logging
from collections import Counter
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
        Diagnostics data including channel status, system config summary,
        and recipient counts.  No personally identifiable information
        (contact details, message content) is included.

    """
    diagnostics: dict[str, Any] = {
        "entry_id": entry.entry_id,
        "version": entry.version,
    }

    # Local import to avoid a top-level circular dependency
    # (diagnostics → __init__ → delivery.factory → … → diagnostics).
    from . import get_config_repository  # noqa: PLC0415

    config_repo = get_config_repository(hass)
    if not config_repo:
        _LOGGER.warning(
            "ANS diagnostics: config repository not initialized for entry %s",
            entry.entry_id,
        )
        diagnostics["error"] = "Config repository not initialized"
        return diagnostics

    # Channel manager info
    channel_manager = config_repo.channel_manager
    if channel_manager:
        all_infos = channel_manager.get_all_infos()
        scope_counts: Counter[str] = Counter(i.scope.value for i in all_infos)
        diagnostics["channels"] = {
            "detected": channel_manager.count_detected(),
            "active": channel_manager.count_active(),
            "by_scope": {
                "system": scope_counts.get(ChannelScope.SYSTEM.value, 0),
                "recipient": scope_counts.get(ChannelScope.RECIPIENT.value, 0),
                "tts": scope_counts.get(ChannelScope.TTS.value, 0),
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

    # System config — expose only non-sensitive structural settings
    if config_repo.system_config:
        diagnostics["system_config"] = {
            "enabled_channels": list(config_repo.system_config.enabled_channels),
        }

    # Recipient summary — counts only; no PII
    recipients = config_repo.recipients
    type_counts: Counter[str] = Counter(r.type.value for r in recipients.values())
    diagnostics["recipients"] = {
        "total": len(recipients),
        "types": dict(type_counts),
    }

    return diagnostics
