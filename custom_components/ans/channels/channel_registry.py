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

_LOGGER = logging.getLogger(__name__)


class ChannelRegistry:
    """Central registry of available notification channels with metadata.

    Maintains a mapping of channel IDs to ChannelInfo objects, providing
    scope-based filtering and validation capabilities.

    Attributes
    ----------
    _channels : dict[str, ChannelInfo]
        Internal mapping of channel ID to channel information.

    """

    def __init__(self) -> None:
        """Initialize an empty channel registry."""
        self._channels: dict[str, ChannelInfo] = {}

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
        # Import here to avoid circular dependency
        from ..helper import format_channel_label  # noqa: PLC0415

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
            label = format_channel_label(service_id)

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
                "Detected notification channel: %s (scope=%s)", channel_id, scope.value
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
        # Import here to avoid circular dependency
        from ..helper import format_channel_label  # noqa: PLC0415

        services = hass.services.async_services()
        tts_services = services.get("tts", {})

        results: list[IntegrationInfo] = []
        for service_id in sorted(tts_services.keys()):
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

    def clear(self) -> None:
        """Clear all registered channels.

        Useful for testing or reinitialization.
        """
        self._channels.clear()
        _LOGGER.debug("Cleared all channel registrations")
