"""Channel registry for managing available notification channels.

The ChannelRegistry maintains a central catalog of all available notification channels
with their metadata (scope, label, integration). This provides type-safe channel
management and scope-based filtering.
"""

from __future__ import annotations

import logging

from .models import ChannelInfo, ChannelScope

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

    def clear(self) -> None:
        """Clear all registered channels.

        Useful for testing or reinitialization.
        """
        self._channels.clear()
        _LOGGER.debug("Cleared all channel registrations")
