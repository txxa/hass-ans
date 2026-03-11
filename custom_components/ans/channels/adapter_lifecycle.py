"""Adapter lifecycle management for dynamic registration and cleanup."""

from __future__ import annotations

import inspect
import logging
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .adapter_registry import AdapterRegistry

from .base import AdapterFactory

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
        # Maps registered channel_id → factory channel_prefix for O(1) lookup.
        #
        # Key semantics differ by adapter type:
        #   STATIC / DYNAMIC_SINGLE — key IS the factory prefix (there is exactly
        #       one adapter per prefix, registered under the prefix itself as the
        #       channel name).  key == value == factory_prefix, and therefore
        #       the invariant channel == channel_prefix always holds.
        #   DYNAMIC_MULTI — key is the full variant channel_id (e.g.
        #       "notify.mobile_app_sm_s911b"), value is the factory prefix (e.g.
        #       "notify.mobile_app").  Multiple keys can share the same value.
        self._channel_to_factory_prefix: dict[str, str] = {}

    def get_factory_count(self) -> int:
        """Get the number of registered factories.

        Returns
        -------
        int
            Number of registered adapter factories.

        """
        return len(self._factories)

    def get_static_channel_ids(self) -> set[str]:
        """Return the channel IDs of all registered STATIC adapters.

        STATIC adapters are always present regardless of user configuration and
        detected channel state, so they must be excluded from the orphaned-adapter
        check to prevent false warnings on cold startup.

        Returns
        -------
        set[str]
            Set of channel IDs (== factory prefixes) belonging to STATIC adapters.

        """
        return {
            prefix
            for prefix, factory in self._factories.items()
            if factory.adapter_type == AdapterType.STATIC
        }

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
                    self._channel_to_factory_prefix[prefix] = prefix
                    _LOGGER.info("Initialized static adapter: %s", prefix)
                except Exception:
                    _LOGGER.exception(
                        "Failed to initialize static adapter '%s'", prefix
                    )

    async def sync_with_config(
        self,
        enabled_channels: list[str],
        detected_channel_ids: set[str] | None = None,
    ) -> None:
        """Synchronize adapters with enabled channels configuration.

        Registers new adapters for enabled channels and unregisters removed ones.
        When ``detected_channel_ids`` is provided, adapters for channels no longer
        present in Home Assistant are also removed, even if they are still listed
        in ``enabled_channels``.

        Parameters
        ----------
        enabled_channels : list[str]
            List of enabled channel IDs from system config.
        detected_channel_ids : set[str] | None
            Optional set of channel IDs currently detected in HA.  When
            provided, channels absent from this set are treated as removed
            and their adapters are cleaned up.

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
                # Single instance, enable/disable based on config.
                # Set-intersection: channel must appear in enabled_channels AND
                # (if detected_channel_ids is provided) in the currently detected set.
                enabled_matching = {
                    ch
                    for ch in enabled_channels
                    if factory.adapter_class.matches_channel(ch)
                }
                if detected_channel_ids is not None:
                    enabled_matching &= detected_channel_ids
                is_enabled = bool(enabled_matching)
                is_registered = self._registry.get(prefix) is not None

                if is_enabled and not is_registered:
                    # Register the adapter
                    try:
                        adapter = factory.factory_fn(self._hass, None)
                        self._registry.register(adapter)
                        self._channel_to_factory_prefix[prefix] = prefix
                        added_count += 1
                        _LOGGER.info("Registered dynamic adapter: %s", prefix)
                    except Exception:
                        _LOGGER.exception("Failed to register adapter '%s'", prefix)

                elif not is_enabled and is_registered:
                    # Unregister the adapter
                    adapter = self._registry.get(prefix)
                    if adapter and factory.cleanup_fn:
                        try:
                            if inspect.iscoroutinefunction(factory.cleanup_fn):
                                await factory.cleanup_fn(adapter)
                            else:
                                factory.cleanup_fn(adapter)
                        except Exception:
                            _LOGGER.exception(
                                "Error during adapter cleanup '%s'", prefix
                            )
                    self._registry.unregister(prefix)
                    self._channel_to_factory_prefix.pop(prefix, None)
                    removed_count += 1
                    _LOGGER.info("Unregistered dynamic adapter: %s", prefix)

            elif factory.adapter_type == AdapterType.DYNAMIC_MULTI:
                # Multiple instances based on channel variants
                # Get all currently registered channels for this adapter
                registered_channels = {
                    ch
                    for ch in self._registry.channels()
                    if factory.adapter_class.matches_channel(ch)
                }

                # Get all enabled channels for this adapter;
                # intersect with detected channels so stale adapters are pruned
                # when a HA notify service disappears.
                enabled_matching = {
                    ch
                    for ch in enabled_channels
                    if factory.adapter_class.matches_channel(ch)
                }
                if detected_channel_ids is not None:
                    enabled_matching &= detected_channel_ids

                # Register new channels
                for channel_id in enabled_matching - registered_channels:
                    try:
                        variant = factory.adapter_class.extract_variant(channel_id)
                        if not variant:
                            _LOGGER.warning(
                                "Cannot extract variant from channel '%s'", channel_id
                            )
                            continue
                        adapter = factory.factory_fn(self._hass, variant)
                        self._registry.register(adapter)
                        self._channel_to_factory_prefix[channel_id] = prefix
                        added_count += 1
                        _LOGGER.info("Registered adapter variant: %s", channel_id)
                    except Exception:
                        _LOGGER.exception("Failed to register adapter '%s'", channel_id)

                # Unregister removed channels
                for channel_id in registered_channels - enabled_matching:
                    adapter = self._registry.get(channel_id)
                    if adapter and factory.cleanup_fn:
                        try:
                            if inspect.iscoroutinefunction(factory.cleanup_fn):
                                await factory.cleanup_fn(adapter)
                            else:
                                factory.cleanup_fn(adapter)
                        except Exception:
                            _LOGGER.exception(
                                "Error during adapter cleanup '%s'",
                                channel_id,
                            )
                    self._registry.unregister(channel_id)
                    self._channel_to_factory_prefix.pop(channel_id, None)
                    removed_count += 1
                    _LOGGER.info("Unregistered adapter variant: %s", channel_id)

        if added_count or removed_count:
            _LOGGER.info(
                "Adapter sync completed: +%d, -%d (total: %d)",
                added_count,
                removed_count,
                self._registry.count(),
            )

    async def cleanup_all(self) -> None:
        """Cleanup all registered adapters.

        Called during shutdown to properly dispose of all adapters.
        Supports both synchronous and asynchronous cleanup functions.
        """
        for channel in list(self._registry.channels()):
            adapter = self._registry.get(channel)
            if not adapter:
                continue

            # O(1) factory lookup via tracking dict
            factory_prefix = self._channel_to_factory_prefix.get(channel)
            if (
                factory_prefix
                and (factory := self._factories.get(factory_prefix))
                and factory.cleanup_fn
            ):
                try:
                    if inspect.iscoroutinefunction(factory.cleanup_fn):
                        await factory.cleanup_fn(adapter)
                    else:
                        factory.cleanup_fn(adapter)
                except Exception:
                    _LOGGER.exception("Error during adapter cleanup '%s'", channel)

            self._registry.unregister(channel)

        self._channel_to_factory_prefix.clear()
        _LOGGER.info("All adapters cleaned up")
