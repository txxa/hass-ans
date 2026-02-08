"""Channel registry for managing available notification channels.

The ChannelRegistry maintains a central catalog of all available notification channels
with their metadata (scope, label, integration). This provides type-safe channel
management and scope-based filtering.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from ..const import PERSISTENT_NOTIFICATION_CHANNEL
from ..models import ChannelInfo, ChannelScope, IntegrationInfo, RecipientType

if TYPE_CHECKING:
    from .adapter_registry import AdapterRegistry
    from .base import ChannelRequirement, DeliveryAdapter

_LOGGER = logging.getLogger(__name__)


class ChannelRegistry:
    """Central registry of available notification channels with metadata.

    Maintains a mapping of channel IDs to ChannelInfo objects, providing
    scope-based filtering and validation capabilities.

    Attributes
    ----------
    _channels : dict[str, ChannelInfo]
        Internal mapping of channel ID to channel information.
    _adapter_classes : dict[str, Type[DeliveryAdapter]]
        Class-level registry mapping channel patterns to adapter classes.

    """

    # Class-level adapter registry for requirement lookups
    _adapter_classes: dict[str, type[DeliveryAdapter]] = {}

    def __init__(self) -> None:
        """Initialize an empty channel registry."""
        self._channels: dict[str, ChannelInfo] = {}

    @classmethod
    def register_adapter_class(
        cls, channel_pattern: str, adapter_class: type[DeliveryAdapter]
    ) -> None:
        """Register an adapter class for requirement lookups.

        Parameters
        ----------
        channel_pattern : str
            Channel ID or pattern (e.g., "notify.signal", "notify.mobile_app").
        adapter_class : Type[DeliveryAdapter]
            Adapter class implementing get_requirements().

        """
        cls._adapter_classes[channel_pattern] = adapter_class
        _LOGGER.debug(
            "Registered adapter class for pattern '%s': %s",
            channel_pattern,
            adapter_class.__name__,
        )

    @classmethod
    def get_adapter_class(cls, channel_id: str) -> type[DeliveryAdapter] | None:
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
        if channel_id in cls._adapter_classes:
            return cls._adapter_classes[channel_id]

        # Pattern match for dynamic channels (e.g., notify.mobile_app_*)
        for pattern, adapter_class in cls._adapter_classes.items():
            if cls._matches_channel_pattern(channel_id, pattern):
                return adapter_class

        return None

    @classmethod
    def get_channel_requirements(cls, channel_id: str) -> ChannelRequirement | None:
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
        adapter_class = cls.get_adapter_class(channel_id)
        if adapter_class:
            return adapter_class.get_requirements()
        return None

    @classmethod
    def filter_channels_by_contact_info(
        cls,
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
            requirements = cls.get_channel_requirements(channel_id)

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

        Supports prefix matching for dynamic channels (e.g., "notify.mobile_app"
        matches "notify.mobile_app_device123").

        Parameters
        ----------
        channel_id : str
            The full channel ID (e.g., "notify.mobile_app_sm_s911b").
        pattern : str
            The pattern to match against (e.g., "notify.mobile_app").

        Returns
        -------
        bool
            True if the channel matches the pattern.

        """
        # Exact match
        if channel_id == pattern:
            return True

        # Prefix match for dynamic channels (e.g., notify.mobile_app_*)
        if channel_id.startswith(f"{pattern}_"):
            return True

        return False

    @staticmethod
    def _get_channel_label_with_fallback(channel_id: str, service_id: str) -> str:
        """Get channel label from adapter or fallback to generic formatting.

        Attempts to get a human-friendly label from the channel's adapter class.
        If no adapter is found, falls back to generic string formatting.

        Parameters
        ----------
        channel_id : str
            Full channel identifier (e.g., "notify.mobile_app_sm_s911b").
        service_id : str
            Service identifier without domain prefix (e.g., "mobile_app_sm_s911b").

        Returns
        -------
        str
            Human-friendly label for the channel.

        """
        # Try to get label from adapter class
        adapter_class = ChannelRegistry.get_adapter_class(channel_id)
        if adapter_class:
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

    def validate_adapters(
        self, adapter_registry: AdapterRegistry
    ) -> dict[str, list[str]]:
        """Validate that all channels have corresponding adapters.

        Parameters
        ----------
        adapter_registry : AdapterRegistry
            The adapter registry to validate against.

        Returns
        -------
        dict[str, list[str]]
            Dictionary with:
            - 'missing_adapters': Channels without adapters
            - 'orphaned_adapters': Adapters without channels

        """
        channel_ids = set(self.get_all_ids())
        adapter_channels = set(adapter_registry.channels())

        return {
            "missing_adapters": sorted(channel_ids - adapter_channels),
            "orphaned_adapters": sorted(adapter_channels - channel_ids),
        }

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
        return self.filter_by_scope(ChannelScope.RECIPIENT)

    @staticmethod
    def filter_channels_by_recipient_type(
        channels: list[ChannelInfo], recipient_type: RecipientType
    ) -> list[ChannelInfo]:
        """Filter a list of channels by recipient type.

        This is a static utility method for filtering channels when you have
        a list but not access to the registry instance. Filters based on
        channel scope metadata.

        Parameters
        ----------
        channels : list[ChannelInfo]
            List of channel information objects to filter.
        recipient_type : RecipientType
            The type of recipient (SYSTEM, HA_USER, VIRTUAL).

        Returns
        -------
        list[ChannelInfo]
            Filtered list of channels appropriate for this recipient type.

        """
        if recipient_type == RecipientType.SYSTEM:
            return [ch for ch in channels if ch.scope == ChannelScope.SYSTEM]
        return [ch for ch in channels if ch.scope == ChannelScope.RECIPIENT]

    @staticmethod
    async def detect_notification_channels(
        hass: HomeAssistant,
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
            label = ChannelRegistry._get_channel_label_with_fallback(
                channel_id, service_id
            )

            # Determine scope based on channel nature:
            # - persistent_notification: delivers to HA instance (SYSTEM)
            # - all others: deliver to specific recipients (RECIPIENT)
            # Future: TTS channels will also be SYSTEM
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

    @staticmethod
    async def detect_tts_integrations(hass: HomeAssistant) -> list[IntegrationInfo]:
        """Discover available TTS integrations.

        TODO: This function is prepared for future TTS channel support.
        Currently not used but will be integrated when TTS notifications are implemented.

        Parameters
        ----------
        hass : HomeAssistant
            Home Assistant instance.

        Returns
        -------
        list[IntegrationInfo]
            List of detected TTS integrations with metadata:
            - id: TTS service identifier (e.g., "tts.google_translate")
            - label: Human-friendly label
            - integration: Integration domain

        """
        services = hass.services.async_services()
        tts_services = services.get("tts", {})

        results: list[IntegrationInfo] = []
        for service_id in sorted(tts_services.keys()):
            channel_id = f"tts.{service_id}"
            label = ChannelRegistry._get_channel_label_with_fallback(
                channel_id, service_id
            )
            results.append(
                IntegrationInfo(
                    channel_id,
                    label,
                    service_id,
                )
            )

        _LOGGER.debug("Detected TTS integrations: %s", results)
        return results

    def clear(self) -> None:
        """Clear all registered channels.

        Useful for testing or reinitialization.
        """
        self._channels.clear()
        _LOGGER.debug("Cleared all channel registrations")
