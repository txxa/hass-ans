"""Adapter lifecycle management for dynamic registration and cleanup."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .adapter_registry import AdapterRegistry
    from .base import DeliveryAdapter

_LOGGER = logging.getLogger(__name__)


class AdapterType(str, Enum):
    """Type of adapter and its lifecycle behavior.

    Values
    ------
    STATIC : str
        Always registered, independent of config (e.g., persistent_notification).
    DYNAMIC_SINGLE : str
        One instance, registered when channel is enabled (e.g., signal).
    DYNAMIC_MULTI : str
        Multiple instances, one per enabled channel variant (e.g., mobile_app_*).

    """

    STATIC = "static"
    DYNAMIC_SINGLE = "dynamic_single"
    DYNAMIC_MULTI = "dynamic_multi"


@dataclass
class AdapterFactory:
    """Factory for creating adapter instances.

    Attributes
    ----------
    adapter_type : AdapterType
        Lifecycle behavior of this adapter.
    channel_prefix : str
        Channel identifier or prefix (e.g., "notify.mobile_app").
    factory_fn : Callable
        Function to create adapter instance(s).
    cleanup_fn : Callable | None
        Optional cleanup function when adapter is unregistered.

    """

    adapter_type: AdapterType
    channel_prefix: str
    factory_fn: Callable[[HomeAssistant, str | None], DeliveryAdapter]
    cleanup_fn: Callable[[DeliveryAdapter], None] | None = None


class AdapterLifecycleManager:
    """Manages dynamic adapter registration and cleanup.

    Handles the full lifecycle of delivery adapters:
    - Registration during initialization
    - Dynamic updates when config changes
    - Cleanup when adapters are removed

    Attributes
    ----------
    _hass : HomeAssistant
        Home Assistant instance.
    _registry : AdapterRegistry
        Adapter registry to manage.
    _factories : dict[str, AdapterFactory]
        Registered adapter factories by channel prefix.

    """

    def __init__(self, hass: HomeAssistant, registry: AdapterRegistry) -> None:
        """Initialize lifecycle manager.

        Parameters
        ----------
        hass : HomeAssistant
            Home Assistant instance.
        registry : AdapterRegistry
            Adapter registry to manage.

        """
        self._hass = hass
        self._registry = registry
        self._factories: dict[str, AdapterFactory] = {}

    def get_factory_count(self) -> int:
        """Get the number of registered factories.

        Returns
        -------
        int
            Number of registered adapter factories.

        """
        return len(self._factories)

    def register_factory(self, factory: AdapterFactory) -> None:
        """Register an adapter factory.

        Parameters
        ----------
        factory : AdapterFactory
            Factory configuration for creating adapters.

        """
        self._factories[factory.channel_prefix] = factory
        _LOGGER.debug(
            "Registered adapter factory for '%s' (type: %s)",
            factory.channel_prefix,
            factory.adapter_type.value,
        )

    def initialize_static_adapters(self) -> None:
        """Initialize all static adapters that are always available.

        Static adapters are registered regardless of configuration.
        """
        for prefix, factory in self._factories.items():
            if factory.adapter_type == AdapterType.STATIC:
                try:
                    adapter = factory.factory_fn(self._hass, None)
                    self._registry.register(adapter)
                    _LOGGER.info("Initialized static adapter: %s", prefix)
                except Exception:
                    _LOGGER.exception(
                        "Failed to initialize static adapter '%s'", prefix
                    )

    def sync_with_config(self, enabled_channels: list[str]) -> None:
        """Synchronize adapters with enabled channels configuration.

        Registers new adapters for enabled channels and unregisters removed ones.

        Parameters
        ----------
        enabled_channels : list[str]
            List of enabled channel IDs from system config.

        """
        # Track changes for logging
        added_count = 0
        removed_count = 0

        # Process each factory type
        for prefix, factory in self._factories.items():
            if factory.adapter_type == AdapterType.STATIC:
                # Static adapters are already registered
                continue

            if factory.adapter_type == AdapterType.DYNAMIC_SINGLE:
                # Single instance, enable/disable based on config
                is_enabled = prefix in enabled_channels
                is_registered = self._registry.get(prefix) is not None

                if is_enabled and not is_registered:
                    # Register the adapter
                    try:
                        adapter = factory.factory_fn(self._hass, None)
                        self._registry.register(adapter)
                        added_count += 1
                        _LOGGER.info("Registered dynamic adapter: %s", prefix)
                    except Exception:
                        _LOGGER.exception("Failed to register adapter '%s'", prefix)

                elif not is_enabled and is_registered:
                    # Unregister the adapter
                    adapter = self._registry.get(prefix)
                    if adapter and factory.cleanup_fn:
                        try:
                            factory.cleanup_fn(adapter)
                        except Exception:
                            _LOGGER.exception(
                                "Error during adapter cleanup '%s'", prefix
                            )
                    self._registry.unregister(prefix)
                    removed_count += 1
                    _LOGGER.info("Unregistered dynamic adapter: %s", prefix)

            elif factory.adapter_type == AdapterType.DYNAMIC_MULTI:
                # Multiple instances based on channel variants
                # Get all currently registered channels for this prefix
                registered_channels = {
                    ch for ch in self._registry.channels() if ch.startswith(prefix)
                }

                # Get all enabled channels for this prefix
                enabled_matching = {
                    ch for ch in enabled_channels if ch.startswith(prefix)
                }

                # Register new channels
                for channel_id in enabled_matching - registered_channels:
                    try:
                        # Extract variant (e.g., "sm_s911b" from "notify.mobile_app_sm_s911b")
                        variant = channel_id[len(prefix) :].lstrip("_")
                        if not variant:
                            _LOGGER.warning(
                                "Cannot extract variant from channel '%s'", channel_id
                            )
                            continue
                        adapter = factory.factory_fn(self._hass, variant)
                        self._registry.register(adapter)
                        added_count += 1
                        _LOGGER.info("Registered adapter variant: %s", channel_id)
                    except Exception:
                        _LOGGER.exception("Failed to register adapter '%s'", channel_id)

                # Unregister removed channels
                for channel_id in registered_channels - enabled_matching:
                    adapter = self._registry.get(channel_id)
                    if adapter and factory.cleanup_fn:
                        try:
                            factory.cleanup_fn(adapter)
                        except Exception:
                            _LOGGER.exception(
                                "Error during adapter cleanup '%s'",
                                channel_id,
                            )
                    self._registry.unregister(channel_id)
                    removed_count += 1
                    _LOGGER.info("Unregistered adapter variant: %s", channel_id)

        if added_count or removed_count:
            _LOGGER.info(
                "Adapter sync completed: +%d, -%d (total: %d)",
                added_count,
                removed_count,
                self._registry.count(),
            )

    def cleanup_all(self) -> None:
        """Cleanup all registered adapters.

        Called during shutdown to properly dispose of all adapters.
        """
        for channel in list(self._registry.channels()):
            adapter = self._registry.get(channel)
            if not adapter:
                continue

            # Find matching factory for cleanup
            for prefix, factory in self._factories.items():
                if channel.startswith(prefix) and factory.cleanup_fn:
                    try:
                        factory.cleanup_fn(adapter)
                    except Exception:
                        _LOGGER.exception("Error during adapter cleanup '%s'", channel)
                    break

            self._registry.unregister(channel)

        _LOGGER.info("All adapters cleaned up")
