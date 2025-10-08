# router.py
from __future__ import annotations

import logging
import re

from .models import (
    ConfigSnapshot,
    IdentityConfig,
    NotificationCriticality,
    SystemConfig,
)

_LOGGER = logging.getLogger(__name__)


class Router:
    """Router maps IdentityConfig + SystemConfig + NotificationCriticality to an ordered list of effective HA notify channels.

    Behavior:
      - Select the channel list from IdentityConfig that matches the criticality
        (channels_low / channels_medium / channels_high / channels_critical).
      - If SystemConfig.enabled_channels is non-empty, intersect identity channels
        with that set (preserving identity-config order).
      - Validate channel strings (format checks). Invalid channels are dropped
        and a warning logged.
      - Return an ordered list without duplicates.
    """

    # Simple validation regex for channel identifiers (allows common HA notify patterns).
    # Accepts letters, digits, underscore, dot, dash, slash and colon.
    _CHANNEL_VALIDATION_RE = re.compile(r"^[\w\.\-:\/]+$")

    # Maximum allowed channel string length
    MAX_CHANNEL_LENGTH = 256

    _CRITICALITY_TO_FIELD = {
        "LOW": "channels_low",
        "MEDIUM": "channels_medium",
        "HIGH": "channels_high",
        "CRITICAL": "channels_critical",
    }

    def __init__(self) -> None:
        pass

    def resolve_channels(
        self,
        identity_config: IdentityConfig,
        system_config: SystemConfig | None,
        criticality: NotificationCriticality,
    ) -> list[str]:
        """Resolve the effective ordered list of channel identifiers.

        Args:
            identity_config: IdentityConfig object (expected to have channels_<level> lists).
            system_config: SystemConfig object (may be None or have enabled_channels list).
            criticality: NotificationCriticality enum or object with .value

        Returns:
            Ordered list of channel strings (may be empty).

        """
        if identity_config is None:
            _LOGGER.debug(
                "Router.resolve_channels called with no identity_config -> returning empty list"
            )
            return []

        # Determine which attribute on identity_config to use
        crit_value = getattr(criticality, "value", criticality)
        field_name = self._CRITICALITY_TO_FIELD.get(str(crit_value).upper())
        if field_name is None:
            _LOGGER.warning(
                "Router.resolve_channels: unknown criticality '%s'; defaulting to 'channels_low'",
                crit_value,
            )
            field_name = "channels_low"

        # Get identity channels (preserve order)
        identity_channels = getattr(identity_config, field_name, None) or []
        # Normalize: ensure list of strings
        identity_channels = [
            c for c in (identity_channels or []) if isinstance(c, str) and c.strip()
        ]

        # System enabled channels: if provided and non-empty, we will intersect.
        system_enabled = None
        if system_config is not None:
            system_enabled = getattr(system_config, "enabled_channels", None)
            if system_enabled:
                # normalize to set of strings
                system_enabled = {s for s in system_enabled if isinstance(s, str)}

        # Build result preserving identity order, removing duplicates
        seen = set()
        result: list[str] = []
        for ch in identity_channels:
            ch_norm = ch.strip()
            if ch_norm in seen:
                continue
            # If system has enabled_channels configured, enforce intersection
            if (
                system_enabled is not None
                and len(system_enabled) > 0
                and ch_norm not in system_enabled
            ):
                _LOGGER.debug(
                    "Router: channel '%s' for identity %s not enabled by system_config -> skipping",
                    ch_norm,
                    getattr(identity_config, "identity_id", "<unknown>"),
                )
                continue
            # Validate format
            if not self.validate_channel(ch_norm):
                _LOGGER.warning(
                    "Router: invalid channel format dropped: '%s' (identity=%s)",
                    ch_norm,
                    getattr(identity_config, "identity_id", "<unknown>"),
                )
                continue
            seen.add(ch_norm)
            result.append(ch_norm)

        return result

    def validate_channel(self, channel: str) -> bool:
        """Quick validation of channel formatting.

        Returns True if channel appears syntactically valid, False otherwise.
        """
        if not isinstance(channel, str) or not channel:
            return False
        if len(channel) > self.MAX_CHANNEL_LENGTH:
            _LOGGER.debug(
                "Router.validate_channel: channel length exceeds max (%d): '%s...'",
                self.MAX_CHANNEL_LENGTH,
                channel[:40],
            )
            return False
        if not self._CHANNEL_VALIDATION_RE.match(channel):
            _LOGGER.debug(
                "Router.validate_channel: channel failed regex validation: '%s'",
                channel,
            )
            return False
        return True

    async def effective_channels_for_identity(
        self,
        identity_id: str,
        config_snapshot: ConfigSnapshot,
        criticality: NotificationCriticality,
    ) -> list[str]:
        """Convenience wrapper to resolve channels for a given identity using a ConfigSnapshot.

        Args:
            identity_id: identity identifier key present in config_snapshot.identity_configs
            config_snapshot: the snapshot containing identity configs and system config
            criticality: NotificationCriticality enum or string-like

        Returns:
            list of resolved channels

        Raises:
            KeyError if the identity_id is not found in the snapshot's identity_configs

        """
        if config_snapshot is None:
            raise ValueError("config_snapshot is required")

        identity_config = config_snapshot.identity_configs.get(identity_id)
        if identity_config is None:
            raise KeyError(f"Unknown identity '{identity_id}'")

        system_config = getattr(config_snapshot, "system_config", None)
        return self.resolve_channels(identity_config, system_config, criticality)
