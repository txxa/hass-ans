"""Simple registry for delivery adapters.

Maintains a mapping of channel names to adapter instances.
Adapters can be registered once during setup and retrieved as needed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import DeliveryAdapter

if TYPE_CHECKING:
    from .channel_registry import ChannelRegistry

_LOGGER = logging.getLogger(__name__)


class AdapterRegistry:
    """Container for registered delivery adapters.

    Provides a simple lookup by channel name with graceful degradation
    (missing adapters return None rather than raising errors).
    """

    def __init__(self) -> None:
        """Initialize the adapter registry."""
        self._adapters: dict[str, DeliveryAdapter] = {}

    def register(self, adapter: DeliveryAdapter) -> None:
        """Register an adapter by its channel name.

        Args:
            adapter: Adapter instance with a channel attribute.

        """
        channel = adapter.channel
        if channel in self._adapters:
            _LOGGER.warning(
                "Overwriting existing adapter for channel '%s' (%s -> %s)",
                channel,
                type(self._adapters[channel]).__name__,
                type(adapter).__name__,
            )

        self._adapters[channel] = adapter
        _LOGGER.debug("Registered adapter for channel '%s'", channel)

    def get(self, channel: str) -> DeliveryAdapter | None:
        """Get adapter by exact channel name.

        Args:
            channel: Channel name to look up.

        Returns:
            Adapter instance if found, None otherwise.

        """
        adapter = self._adapters.get(channel)
        if adapter is None:
            _LOGGER.debug("No adapter registered for channel '%s'", channel)
        return adapter

    def unregister(self, channel: str) -> bool:
        """Unregister an adapter by channel name.

        Args:
            channel: Channel name to unregister.

        Returns:
            True if adapter was found and removed, False otherwise.

        """
        if channel in self._adapters:
            del self._adapters[channel]
            _LOGGER.debug("Unregistered adapter for channel '%s'", channel)
            return True
        _LOGGER.debug("No adapter found for channel '%s' to unregister", channel)
        return False

    def count(self) -> int:
        """Get the count of registered adapters.

        Returns:
            Number of registered adapters.

        """
        return len(self._adapters)

    def all_adapters(self) -> dict[str, DeliveryAdapter]:
        """Get all registered adapters.

        Returns:
            Dictionary mapping channel names to adapter instances.

        """
        return dict(self._adapters)

    def channels(self) -> list[str]:
        """Get list of registered channel names.

        Returns:
            List of channel names in registration order.

        """
        return list(self._adapters.keys())


def validate_channel_adapter_consistency(
    channel_registry: ChannelRegistry,
    adapter_registry: AdapterRegistry,
    *,
    excluded_adapter_channels: set[str] | None = None,
) -> dict[str, list[str]]:
    """Validate that all channels have corresponding adapters and vice versa.

    Parameters
    ----------
    channel_registry : ChannelRegistry
        The channel registry to validate channels from.
    adapter_registry : AdapterRegistry
        The adapter registry to validate adapters against.
    excluded_adapter_channels : set[str] | None
        Optional set of adapter channel IDs to exclude from the orphaned-adapter
        check.  Pass the STATIC adapter channel IDs here so that always-registered
        adapters do not generate spurious warnings before channel discovery runs.

    Returns
    -------
    dict[str, list[str]]
        Dictionary with:
        - 'missing_adapters': Channels without adapters
        - 'orphaned_adapters': Adapters without channel registration

    """
    channel_ids = set(channel_registry.get_all_ids())
    adapter_channels = set(adapter_registry.channels())

    # STATIC adapters are registered unconditionally and therefore exist in the
    # adapter registry before channel discovery populates the channel registry.
    # Excluding them here prevents false "orphaned adapter" warnings at cold startup.
    effective_adapter_channels = (
        adapter_channels - excluded_adapter_channels
        if excluded_adapter_channels
        else adapter_channels
    )

    return {
        "missing_adapters": sorted(channel_ids - adapter_channels),
        "orphaned_adapters": sorted(effective_adapter_channels - channel_ids),
    }
