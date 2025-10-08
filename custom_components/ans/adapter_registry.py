"""Simple registry for delivery adapters.

Maintains a mapping of channel names to adapter instances.
Adapters can be registered once during setup and retrieved as needed.
"""

import logging

from .delivery.base import DeliveryAdapter

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
        if not hasattr(adapter, "channel"):
            _LOGGER.error(
                "Cannot register adapter without 'channel' attribute: %s",
                type(adapter).__name__,
            )
            return

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
        if adapter:
            _LOGGER.debug("Found adapter for channel '%s'", channel)
        else:
            _LOGGER.debug("No adapter found for channel '%s'", channel)
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

    def all(self) -> dict[str, DeliveryAdapter]:
        """Get all registered adapters.

        Returns:
            Dictionary mapping channel names to adapter instances.

        """
        return dict(self._adapters)

    def channels(self) -> list[str]:
        """Get list of registered channel names.

        Returns:
            Sorted list of channel names.

        """
        return sorted(self._adapters.keys())
