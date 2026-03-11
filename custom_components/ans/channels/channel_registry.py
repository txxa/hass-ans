"""Channel registry for managing available notification channels.

The ChannelRegistry maintains a central catalog of all available notification channels
with their metadata (scope, label, integration). This provides type-safe channel
management and scope-based filtering.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.media_player.const import MediaPlayerEntityFeature
from homeassistant.core import HomeAssistant

from ..const import PERSISTENT_NOTIFICATION_CHANNEL
from ..models import ChannelInfo, ChannelScope, RecipientType

if TYPE_CHECKING:
    from .base import ChannelRequirement, DeliveryAdapter

_LOGGER = logging.getLogger(__name__)
_TTS_ACTION_ENTITY_SUFFIXES: frozenset[str] = frozenset({"speak", "clear_cache"})


class ChannelRegistry:
    """Central registry of available notification channels with metadata.

    Maintains a mapping of channel IDs to ChannelInfo objects, providing
    scope-based filtering and validation capabilities.

    Attributes
    ----------
    _channels : dict[str, ChannelInfo]
        Internal mapping of channel ID to channel information.
    _adapter_classes : dict[str, type[DeliveryAdapter]]
        Per-instance mapping of channel patterns to adapter classes used
        for requirement lookups and label generation.

    """

    def __init__(
        self,
        adapter_classes: dict[str, type[DeliveryAdapter]] | None = None,
    ) -> None:
        """Initialize an empty channel registry.

        Parameters
        ----------
        adapter_classes : dict[str, type[DeliveryAdapter]] | None
            Optional mapping of channel patterns to adapter classes, used for
            contact-info requirement lookups and label generation.

        """
        self._channels: dict[str, ChannelInfo] = {}
        self._adapter_classes: dict[str, type[DeliveryAdapter]] = (
            dict(adapter_classes) if adapter_classes else {}
        )

    @property
    def adapter_classes(self) -> dict[str, type[DeliveryAdapter]]:
        """Return the adapter-class map (read-only view)."""
        return self._adapter_classes

    def get_adapter_class(self, channel_id: str) -> type[DeliveryAdapter] | None:
        """Get adapter class for a channel ID.

        Parameters
        ----------
        channel_id : str
            Channel identifier (e.g., "notify.signal", "notify.mobile_app_sm_s911b").

        Returns
        -------
        Type[DeliveryAdapter] | None
            Adapter class if found, None otherwise.

        """
        # Direct match
        if channel_id in self._adapter_classes:
            return self._adapter_classes[channel_id]

        # Pattern match for dynamic channels (e.g., notify.mobile_app_*)
        for pattern, adapter_class in self._adapter_classes.items():
            if self._matches_channel_pattern(channel_id, pattern):
                return adapter_class

        return None

    def get_channel_requirements(self, channel_id: str) -> ChannelRequirement | None:
        """Get contact information requirements for a channel.

        Parameters
        ----------
        channel_id : str
            Channel identifier.

        Returns
        -------
        ChannelRequirement | None
            Requirements dict if adapter found, None otherwise.

        """
        adapter_class = self.get_adapter_class(channel_id)
        if adapter_class:
            return adapter_class.get_requirements()
        return None

    def filter_channels_by_contact_info(
        self,
        channel_ids: list[str],
        *,
        has_email: bool = False,
        has_phone: bool = False,
        has_ha_user: bool = False,
    ) -> tuple[list[str], dict[str, str]]:
        """Filter channels based on available recipient contact information.

        Parameters
        ----------
        channel_ids : list[str]
            List of channel IDs to filter.
        has_email : bool, optional
            Whether recipient has email configured, by default False.
        has_phone : bool, optional
            Whether recipient has phone configured, by default False.
        has_ha_user : bool, optional
            Whether recipient is linked to HA user, by default False.

        Returns
        -------
        tuple[list[str], dict[str, str]]
            Tuple of (available_channels, unavailable_reasons).
            - available_channels: List of channel IDs that can be used
            - unavailable_reasons: Dict mapping unavailable channel IDs to reason strings

        """
        available: list[str] = []
        unavailable: dict[str, str] = {}

        for channel_id in channel_ids:
            requirements = self.get_channel_requirements(channel_id)

            # Unknown channel - allow it (conservative approach for custom channels)
            if requirements is None:
                available.append(channel_id)
                continue

            # Check requirements
            missing_requirements: list[str] = []

            if requirements.get("requires_email", False) and not has_email:
                missing_requirements.append("email address")
            if requirements.get("requires_phone", False) and not has_phone:
                missing_requirements.append("phone number")
            if requirements.get("requires_ha_user", False) and not has_ha_user:
                missing_requirements.append("Home Assistant user")

            if missing_requirements:
                # Build reason message
                if len(missing_requirements) == 1:
                    reason = f"Missing {missing_requirements[0]}"
                else:
                    reason = f"Missing {' and '.join(missing_requirements)}"
                unavailable[channel_id] = reason
            else:
                # All requirements met
                available.append(channel_id)

        return available, unavailable

    @staticmethod
    def _matches_channel_pattern(channel_id: str, pattern: str) -> bool:
        """Check if a channel ID matches a requirement pattern.

        Supports two suffix styles:
        - Underscore-separated: ``notify.mobile_app`` matches ``notify.mobile_app_device123``
        - Period-separated: ``media_player`` matches ``media_player.living_room``

        Parameters
        ----------
        channel_id : str
            The full channel ID (e.g., "notify.mobile_app_sm_s911b").
        pattern : str
            The pattern to match against (e.g., "notify.mobile_app" or "media_player").

        Returns
        -------
        bool
            True if the channel matches the pattern.

        """
        # Exact match
        if channel_id == pattern:
            return True

        # Underscore-separated variant channels (e.g., notify.mobile_app_*)
        if channel_id.startswith(f"{pattern}_"):
            return True

        # Period-separated domain channels (e.g., media_player.*)
        if channel_id.startswith(f"{pattern}."):
            return True

        return False

    @staticmethod
    def get_channel_label_with_fallback(
        channel_id: str,
        service_id: str,
        adapter_classes: dict[str, type[DeliveryAdapter]] | None = None,
    ) -> str:
        """Get channel label from adapter or fallback to generic formatting.

        Attempts to get a human-friendly label from the channel's adapter class.
        If no adapter is found, falls back to generic string formatting.

        Parameters
        ----------
        channel_id : str
            Full channel identifier (e.g., "notify.mobile_app_sm_s911b").
        service_id : str
            Service identifier without domain prefix (e.g., "mobile_app_sm_s911b").
        adapter_classes : dict[str, type[DeliveryAdapter]] | None
            Optional mapping of channel patterns to adapter classes for label lookup.

        Returns
        -------
        str
            Human-friendly label for the channel.

        """
        # Try to get label from adapter class
        if adapter_classes:
            if channel_id in adapter_classes:
                return adapter_classes[channel_id].get_channel_label(channel_id)
            for pattern, adapter_class in adapter_classes.items():
                if ChannelRegistry._matches_channel_pattern(channel_id, pattern):
                    return adapter_class.get_channel_label(channel_id)

        # Fallback to generic formatting for channels without dedicated adapters
        _LOGGER.debug(
            "No adapter found for channel %s, using generic label formatting",
            channel_id,
        )
        return service_id.replace("_", " ").title()

    def register(self, channel_info: ChannelInfo) -> None:
        """Register a channel with its metadata.

        Parameters
        ----------
        channel_info : ChannelInfo
            Channel information to register.

        """
        if channel_info.id in self._channels:
            _LOGGER.warning(
                "Overwriting existing channel registration: %s", channel_info.id
            )

        self._channels[channel_info.id] = channel_info
        _LOGGER.debug(
            "Registered channel '%s' (scope=%s, integration=%s)",
            channel_info.id,
            channel_info.scope.value,
            channel_info.integration,
        )

    def register_multiple(self, channels: list[ChannelInfo]) -> None:
        """Register multiple channels at once.

        Parameters
        ----------
        channels : list[ChannelInfo]
            List of channel information objects to register.

        """
        for channel_info in channels:
            self.register(channel_info)

    def get(self, channel_id: str) -> ChannelInfo | None:
        """Get channel information by ID.

        Parameters
        ----------
        channel_id : str
            Channel identifier to look up.

        Returns
        -------
        ChannelInfo | None
            Channel information if found, None otherwise.

        """
        return self._channels.get(channel_id)

    def is_system_wide(self, channel_id: str) -> bool:
        """Check if a channel is system-wide.

        Parameters
        ----------
        channel_id : str
            Channel identifier to check.

        Returns
        -------
        bool
            True if channel is system-wide, False otherwise.
            Returns False for unknown channels.

        """
        channel_info = self.get(channel_id)
        return channel_info.scope == ChannelScope.SYSTEM if channel_info else False

    def is_recipient_specific(self, channel_id: str) -> bool:
        """Check if a channel is recipient-specific.

        Parameters
        ----------
        channel_id : str
            Channel identifier to check.

        Returns
        -------
        bool
            True if channel is recipient-specific, False otherwise.
            Returns False for unknown channels.

        """
        channel_info = self.get(channel_id)
        return channel_info.scope == ChannelScope.RECIPIENT if channel_info else False

    def get_all(self) -> list[ChannelInfo]:
        """Get all registered channels.

        Returns
        -------
        list[ChannelInfo]
            List of all registered channel information objects.

        """
        return list(self._channels.values())

    def get_all_ids(self) -> list[str]:
        """Get all registered channel IDs.

        Returns
        -------
        list[str]
            List of all registered channel identifiers.

        """
        return list(self._channels.keys())

    def filter_by_scope(self, scope: ChannelScope) -> list[ChannelInfo]:
        """Filter channels by scope.

        Parameters
        ----------
        scope : ChannelScope
            Scope to filter by (SYSTEM or RECIPIENT).

        Returns
        -------
        list[ChannelInfo]
            List of channels matching the specified scope.

        """
        return [info for info in self._channels.values() if info.scope == scope]

    def filter_ids_by_scope(self, scope: ChannelScope) -> list[str]:
        """Filter channel IDs by scope.

        Parameters
        ----------
        scope : ChannelScope
            Scope to filter by (SYSTEM or RECIPIENT).

        Returns
        -------
        list[str]
            List of channel IDs matching the specified scope.

        """
        return [info.id for info in self.filter_by_scope(scope)]

    def has_channel(self, channel_id: str) -> bool:
        """Check if a channel is registered.

        Parameters
        ----------
        channel_id : str
            Channel identifier to check.

        Returns
        -------
        bool
            True if channel is registered, False otherwise.

        """
        return channel_id in self._channels

    def count(self) -> int:
        """Get the number of registered channels.

        Returns
        -------
        int
            Number of registered channels.

        """
        return len(self._channels)

    def get_channels_for_recipient_type(
        self, recipient_type: RecipientType
    ) -> list[ChannelInfo]:
        """Get channels appropriate for a recipient type.

        Parameters
        ----------
        recipient_type : RecipientType
            The type of recipient.

        Returns
        -------
        list[ChannelInfo]
            List of channels appropriate for this recipient type.

        """
        if recipient_type == RecipientType.SYSTEM:
            return self.filter_by_scope(ChannelScope.SYSTEM)
        if recipient_type == RecipientType.TTS:
            return self.filter_by_scope(ChannelScope.TTS)
        return self.filter_by_scope(ChannelScope.RECIPIENT)

    @staticmethod
    def filter_channels_by_recipient_type(
        channels: list[ChannelInfo], recipient_type: RecipientType
    ) -> list[ChannelInfo]:
        """Filter a list of channels by recipient type.

        This is a static utility method for filtering channels when you have
        a list but not access to the registry instance. Filters based on
        channel scope metadata and recipient type.

        Parameters
        ----------
        channels : list[ChannelInfo]
            List of channel information objects to filter.
        recipient_type : RecipientType
            The type of recipient (SYSTEM, HA_USER, VIRTUAL, TTS).

        Returns
        -------
        list[ChannelInfo]
            Filtered list of channels appropriate for this recipient type.

        """
        if recipient_type == RecipientType.SYSTEM:
            return [ch for ch in channels if ch.scope == ChannelScope.SYSTEM]
        if recipient_type == RecipientType.TTS:
            # TTS recipients only use media_player channels (scope=TTS)
            return [ch for ch in channels if ch.scope == ChannelScope.TTS]
        # HA_USER and VIRTUAL recipients use notification channels
        return [ch for ch in channels if ch.scope == ChannelScope.RECIPIENT]

    def clear(self) -> None:
        """Clear all registered channels.

        Useful for testing or reinitialization.
        """
        self._channels.clear()
        _LOGGER.debug("Cleared all channel registrations")


def detect_notification_channels(
    hass: HomeAssistant,
    adapter_classes: dict[str, type[DeliveryAdapter]] | None = None,
) -> list[ChannelInfo]:
    """Discover available notification channels (notify.* services).

    Detects all notify services registered in Home Assistant and creates
    ChannelInfo objects with appropriate scope metadata.

    Note:
        This may run before all notify integrations are loaded during startup.
        Use the 'ans.refresh_channels' service to re-detect channels after
        all integrations have initialized.

    Parameters
    ----------
    hass : HomeAssistant
        Home Assistant instance.
    adapter_classes : dict[str, type[DeliveryAdapter]] | None
        Optional map of channel-id prefix to adapter class, used to
        generate human-readable labels.  When ``None`` the raw service
        name is used as the label.

    Returns
    -------
    list[ChannelInfo]
        List of discovered notification channels with metadata.

    """
    services = hass.services.async_services()
    notify_services = services.get("notify", {})

    if not notify_services:
        _LOGGER.warning(
            "No notify services found during channel detection. "
            "This can happen if ANS loads before notify integrations. "
            "Channels will be detected on next config reload."
        )

    results: list[ChannelInfo] = []
    for service_id in sorted(notify_services.keys()):
        if service_id in ("notify", "send_message"):
            continue  # Skip unsupported services

        channel_id = f"notify.{service_id}"
        label = ChannelRegistry.get_channel_label_with_fallback(
            channel_id, service_id, adapter_classes=adapter_classes
        )

        # Determine scope based on channel nature:
        # - persistent_notification: delivers to HA instance (SYSTEM)
        # - all others: deliver to specific recipients (RECIPIENT)
        scope = (
            ChannelScope.SYSTEM
            if channel_id == PERSISTENT_NOTIFICATION_CHANNEL
            else ChannelScope.RECIPIENT
        )

        results.append(
            ChannelInfo(
                id=channel_id,
                label=label,
                scope=scope,
                integration=service_id,
            )
        )
        _LOGGER.debug(
            "Detected notification channel: %s (label=%s, scope=%s)",
            channel_id,
            label,
            scope.value,
        )

    return results


def detect_media_players(hass: HomeAssistant) -> list[ChannelInfo]:
    """Discover media player entities that support TTS playback.

    Performs runtime validation to ensure media players actually support
    required features (volume control and media playback) based on their
    actual attributes, which is more reliable than feature flags.

    Security: Validates actual capabilities through attribute checks.

    Parameters
    ----------
    hass : HomeAssistant
        Home Assistant instance.

    Returns
    -------
    list[ChannelInfo]
        List of ChannelInfo objects for compatible media players with:
        - id: Entity ID (e.g., "media_player.living_room")
        - label: Friendly name
        - scope: TTS (media players are TTS-scoped)
        - integration: Device class or "media_player"

    """
    media_players: list[ChannelInfo] = []

    _REQUIRED_FEATURES = (
        MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.VOLUME_SET
    )

    for entity_id in hass.states.async_entity_ids("media_player"):
        state = hass.states.get(entity_id)
        if not state:
            continue

        # Require PLAY_MEDIA and VOLUME_SET feature flags so that basic TVs or
        # receivers that expose volume but cannot play arbitrary media are excluded.
        supported = state.attributes.get("supported_features", 0)
        if (supported & _REQUIRED_FEATURES) != _REQUIRED_FEATURES:
            _LOGGER.debug(
                "Skipping media player %s: missing required features (supported=0x%x)",
                entity_id,
                supported,
            )
            continue

        # Get friendly name from attributes
        friendly_name = state.attributes.get("friendly_name", entity_id)

        media_players.append(
            ChannelInfo(
                id=entity_id,
                label=friendly_name,
                scope=ChannelScope.TTS,  # TTS delivery via media player
                integration=state.attributes.get("device_class") or "media_player",
            )
        )

    _LOGGER.info("Discovered %d compatible media players for TTS", len(media_players))

    return media_players


def detect_tts_entities(hass: HomeAssistant) -> list[str]:
    """Return entity IDs of TTS engine entities available in the state machine.

    These entity IDs are suitable for use as the ``target`` of the modern
    ``tts.speak`` service action.

    Excludes HA-internal action entities that are not speech engines:
    - ``tts.speak`` / ``tts.clear_cache`` — service-action entities
    - any ``tts.*_say`` entity — legacy per-integration say actions

    Parameters
    ----------
    hass : HomeAssistant
        Home Assistant instance.

    Returns
    -------
    list[str]
        Sorted list of TTS engine entity IDs (e.g. ``["tts.cloud", "tts.piper"]``).

    """
    results: list[str] = []
    for entity_id in hass.states.async_entity_ids("tts"):
        suffix = entity_id.removeprefix("tts.")
        if suffix in _TTS_ACTION_ENTITY_SUFFIXES or suffix.endswith("_say"):
            _LOGGER.debug("Skipping non-engine TTS entity: %s", entity_id)
            continue
        results.append(entity_id)
    return sorted(results)
