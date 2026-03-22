"""Unified channel and adapter lifecycle manager.

Replaces ChannelRegistry + AdapterRegistry + AdapterLifecycleManager with a
single source of truth: ``dict[str, ChannelRecord]``.  Each record carries both
the channel metadata (ChannelInfo) and the live adapter instance, eliminating
the sync gap between the two old registries.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from ..const import PERSISTENT_NOTIFICATION_CHANNEL, REQUIRED_MP_FEATURES
from ..models import ChannelInfo, ChannelScope, RecipientType
from .base import (
    AdapterFactory,
    AdapterType,
    ChannelRecord,
    ChannelStatus,
    DeliveryAdapter,
)

if TYPE_CHECKING:
    from ..delivery.factory import AdapterDeps

_LOGGER = logging.getLogger(__name__)

_TTS_ACTION_ENTITY_SUFFIXES: frozenset[str] = frozenset({"speak", "clear_cache"})
_NOTIFY_SERVICE_EXCLUDE: frozenset[str] = frozenset({"notify", "send_message"})


class ChannelManager:
    """Unified source of truth for channel metadata and live adapters.

    Internal state
    --------------
    _records : dict[str, ChannelRecord]
        Maps channel_id → ChannelRecord (metadata + adapter + status).
    _factories : dict[str, AdapterFactory]
        Maps channel_prefix → AdapterFactory.
    _sync_lock : asyncio.Lock
        Prevents concurrent sync() calls from racing.
    _last_enabled : list[str]
        Last enabled_channels list, used by resync().
    """

    def __init__(self, hass: HomeAssistant, deps: AdapterDeps) -> None:
        """Initialize ChannelManager.

        Parameters
        ----------
        hass : HomeAssistant
            Home Assistant instance.
        deps : AdapterDeps
            Adapter runtime dependencies (config_repo, volume_registry).

        """
        self._hass = hass
        self._deps = deps
        self._records: dict[str, ChannelRecord] = {}
        self._factories: dict[str, AdapterFactory] = {}
        self._sync_lock = asyncio.Lock()
        self._last_enabled: list[str] = []
        self._setup_in_progress: bool = True
        self._pending_resync: bool = False

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_factory(self, factory: AdapterFactory) -> None:
        """Register an adapter factory by channel_prefix."""
        self._factories[factory.channel_prefix] = factory
        _LOGGER.debug(
            "Registered adapter factory for '%s' (type: %s)",
            factory.channel_prefix,
            factory.adapter_type.value,
        )

    def initialize_static_adapters(self) -> None:
        """Create STATIC adapter records at startup.

        STATIC adapters are always ACTIVE regardless of detected channels or
        enabled_channels config.  Called once synchronously during setup.
        """
        for prefix, factory in self._factories.items():
            if factory.adapter_type != AdapterType.STATIC:
                continue
            try:
                adapter = factory.factory_fn(self._hass, None)
                info = self._make_static_channel_info(prefix, factory)
                self._records[prefix] = ChannelRecord(
                    info=info,
                    adapter=adapter,
                    status=ChannelStatus.ACTIVE,
                )
                _LOGGER.info("Initialized static adapter: %s", prefix)
            except Exception:
                _LOGGER.exception("Failed to initialize static adapter '%s'", prefix)

    # ------------------------------------------------------------------
    # Core sync
    # ------------------------------------------------------------------

    async def sync(self, enabled_channels: list[str]) -> None:
        """Detect HA channels, diff against records, create/destroy adapters.

        Status transitions
        ------------------
        - Not seen in HA (and not STATIC): STALE — adapter destroyed.
        - Seen, not in enabled_channels: DETECTED — adapter absent/destroyed.
        - Seen, in enabled_channels, factory exists: ACTIVE — adapter created/kept.
        - In enabled_channels, no factory: INACTIVE.

        This method serialises on an internal lock (L2 fix): concurrent callers
        wait rather than interleaving adapter creation.

        Parameters
        ----------
        enabled_channels : list[str]
            Channel IDs from the current system config.

        """
        async with self._sync_lock:
            self._last_enabled = list(enabled_channels)
            enabled_set = set(enabled_channels)

            # Channels owned by STATIC factories — never go STALE.
            static_ids: set[str] = {
                prefix
                for prefix, f in self._factories.items()
                if f.adapter_type == AdapterType.STATIC
            }

            # Detect all non-STATIC channels currently visible in HA.
            # STATIC channels (e.g. persistent_notification) are excluded from
            # detection — HA guarantees core services are always available, so
            # they never go STALE.
            detected_infos = [
                info
                for info in self._detect_all_channels()
                if info.id not in static_ids
            ]
            detected_ids = {info.id for info in detected_infos}
            detected_map = {info.id: info for info in detected_infos}

            added = removed = 0

            # Start with STATIC records preserved unchanged.
            new_records: dict[str, ChannelRecord] = {
                ch_id: rec
                for ch_id, rec in self._records.items()
                if ch_id in static_ids
            }

            # Process each detected (non-STATIC) channel.
            for channel_id, info in detected_map.items():
                existing = self._records.get(channel_id)

                if channel_id not in enabled_set:
                    # Detected but not enabled → DETECTED, no adapter.
                    if existing and existing.adapter:
                        await self._destroy_adapter(channel_id, existing)
                        removed += 1
                    new_records[channel_id] = ChannelRecord(
                        info=info, adapter=None, status=ChannelStatus.DETECTED
                    )
                    continue

                # Detected and enabled — find factory.
                factory = self._find_factory(channel_id)
                if factory is None:
                    if existing and existing.adapter:
                        await self._destroy_adapter(channel_id, existing)
                        removed += 1
                    new_records[channel_id] = ChannelRecord(
                        info=info, adapter=None, status=ChannelStatus.INACTIVE
                    )
                    _LOGGER.debug(
                        "Channel '%s' is INACTIVE (no factory registered)", channel_id
                    )
                    continue

                # Keep existing live adapter if already ACTIVE.
                if (
                    existing
                    and existing.status == ChannelStatus.ACTIVE
                    and existing.adapter is not None
                ):
                    new_records[channel_id] = ChannelRecord(
                        info=info,
                        adapter=existing.adapter,
                        status=ChannelStatus.ACTIVE,
                    )
                    continue

                # Create new adapter.
                try:
                    variant = factory.adapter_class.extract_variant(channel_id)
                    adapter = factory.factory_fn(self._hass, variant)
                    new_records[channel_id] = ChannelRecord(
                        info=info, adapter=adapter, status=ChannelStatus.ACTIVE
                    )
                    added += 1
                    _LOGGER.info("Channel '%s' → ACTIVE (adapter created)", channel_id)
                except Exception:
                    _LOGGER.exception(
                        "Failed to create adapter for channel '%s'", channel_id
                    )
                    new_records[channel_id] = ChannelRecord(
                        info=info, adapter=None, status=ChannelStatus.INACTIVE
                    )

            # Mark previously-known non-STATIC channels no longer detected as STALE.
            for channel_id, record in self._records.items():
                if channel_id in static_ids or channel_id in detected_ids:
                    continue
                if channel_id in new_records:
                    continue
                if record.adapter:
                    await self._destroy_adapter(channel_id, record)
                    removed += 1
                new_records[channel_id] = ChannelRecord(
                    info=record.info, adapter=None, status=ChannelStatus.STALE
                )
                _LOGGER.info(
                    "Channel '%s' → STALE (no longer detected in HA)", channel_id
                )

            self._records = new_records

            if added or removed:
                _LOGGER.info(
                    "Channel sync completed: +%d activated, -%d deactivated "
                    "(detected=%d, active=%d)",
                    added,
                    removed,
                    self.count_detected(),
                    self.count_active(),
                )

    async def resync(self) -> None:
        """Re-sync using last known enabled_channels.

        Safe to call as a fire-and-forget task; the internal lock serialises
        concurrent sync requests.
        """
        await self.sync(self._last_enabled)

    async def finalize_setup(self) -> None:
        """Mark setup complete and flush any deferred resync."""
        self._setup_in_progress = False
        if self._pending_resync:
            self._pending_resync = False
            await self.resync()

    # ------------------------------------------------------------------
    # Detection helpers (absorbed from channel_registry.py)
    # ------------------------------------------------------------------

    def _detect_all_channels(self) -> list[ChannelInfo]:
        """Detect all HA-available channels (notify.* + compatible media_players)."""
        results = self._detect_notification_channels()
        results.extend(self._detect_media_players())
        return results

    def _detect_notification_channels(self) -> list[ChannelInfo]:
        """Discover available notify.* services."""
        services = self._hass.services.async_services()
        notify_services = services.get("notify", {})

        if not notify_services:
            _LOGGER.debug(
                "No notify services found during channel detection. "
                "This can happen if ANS loads before notify integrations."
            )

        results: list[ChannelInfo] = []
        for service_id in sorted(notify_services.keys()):
            if service_id in _NOTIFY_SERVICE_EXCLUDE:
                continue

            channel_id = f"notify.{service_id}"
            factory = self._find_factory(channel_id)
            label = (
                factory.adapter_class.get_channel_label(channel_id)
                if factory
                else service_id.replace("_", " ").title()
            )
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

    def _detect_media_players(self) -> list[ChannelInfo]:
        """Discover media player entities that support TTS playback."""
        results: list[ChannelInfo] = []

        for entity_id in self._hass.states.async_entity_ids("media_player"):
            state = self._hass.states.get(entity_id)
            if not state:
                continue

            supported = state.attributes.get("supported_features", 0)
            if (supported & REQUIRED_MP_FEATURES) != REQUIRED_MP_FEATURES:
                _LOGGER.debug(
                    "Skipping media player %s: missing required features (supported=0x%x)",
                    entity_id,
                    supported,
                )
                continue

            friendly_name = state.attributes.get("friendly_name", entity_id)
            results.append(
                ChannelInfo(
                    id=entity_id,
                    label=friendly_name,
                    scope=ChannelScope.TTS,
                    integration=state.attributes.get("device_class") or "media_player",
                )
            )

        _LOGGER.info("Discovered %d compatible media players for TTS", len(results))
        return results

    def _make_static_channel_info(
        self, channel_id: str, factory: AdapterFactory
    ) -> ChannelInfo:
        """Build ChannelInfo for a STATIC adapter from its class metadata."""
        label = factory.adapter_class.get_channel_label(channel_id)
        scope = (
            ChannelScope.SYSTEM
            if channel_id == PERSISTENT_NOTIFICATION_CHANNEL
            else ChannelScope.RECIPIENT
        )
        integration = factory.adapter_class.get_metadata().integration or channel_id
        return ChannelInfo(
            id=channel_id,
            label=label,
            scope=scope,
            integration=integration,
        )

    def _find_factory(self, channel_id: str) -> AdapterFactory | None:
        """Return the factory that handles *channel_id*, or None."""
        for factory in self._factories.values():
            if factory.adapter_class.matches_channel(channel_id):
                return factory
        return None

    async def _destroy_adapter(self, channel_id: str, record: ChannelRecord) -> None:
        """Call cleanup_fn on a record's adapter if one is registered."""
        if record.adapter is None:
            return
        factory = self._find_factory(channel_id)
        if factory and factory.cleanup_fn:
            try:
                if inspect.iscoroutinefunction(factory.cleanup_fn):
                    await factory.cleanup_fn(record.adapter)
                else:
                    factory.cleanup_fn(record.adapter)
            except Exception:
                _LOGGER.exception("Error during adapter cleanup for '%s'", channel_id)

    # ------------------------------------------------------------------
    # Read-only lookups
    # ------------------------------------------------------------------

    def get_adapter(self, channel_id: str) -> DeliveryAdapter | None:
        """Return the live adapter for *channel_id*, or None."""
        record = self._records.get(channel_id)
        return record.adapter if record else None

    def get_info(self, channel_id: str) -> ChannelInfo | None:
        """Return ChannelInfo for *channel_id*, or None."""
        record = self._records.get(channel_id)
        return record.info if record else None

    def get_record(self, channel_id: str) -> ChannelRecord | None:
        """Return the full ChannelRecord for *channel_id*, or None."""
        return self._records.get(channel_id)

    def get_all_infos(self) -> list[ChannelInfo]:
        """Return ChannelInfo for all non-STALE channels."""
        return [
            r.info for r in self._records.values() if r.status != ChannelStatus.STALE
        ]

    def get_active_infos(self) -> list[ChannelInfo]:
        """Return ChannelInfo for all ACTIVE channels."""
        return [
            r.info for r in self._records.values() if r.status == ChannelStatus.ACTIVE
        ]

    def get_infos_for_recipient_type(
        self, recipient_type: RecipientType
    ) -> list[ChannelInfo]:
        """Return non-STALE ChannelInfos appropriate for *recipient_type*."""
        all_infos = self.get_all_infos()
        if recipient_type == RecipientType.SYSTEM:
            return [i for i in all_infos if i.scope == ChannelScope.SYSTEM]
        if recipient_type == RecipientType.TTS:
            return [i for i in all_infos if i.scope == ChannelScope.TTS]
        return [i for i in all_infos if i.scope == ChannelScope.RECIPIENT]

    def get_all_records(self) -> list[ChannelRecord]:
        """Return all ChannelRecords including STALE ones."""
        return list(self._records.values())

    def count_detected(self) -> int:
        """Count channels that are not STALE (i.e. currently visible in HA)."""
        return sum(1 for r in self._records.values() if r.status != ChannelStatus.STALE)

    def count_active(self) -> int:
        """Count channels in ACTIVE status (adapter present)."""
        return sum(
            1 for r in self._records.values() if r.status == ChannelStatus.ACTIVE
        )

    def filter_channels_by_contact_info(
        self,
        channel_ids: list[str],
        *,
        has_email: bool = False,
        has_phone: bool = False,
        has_ha_user: bool = False,
    ) -> tuple[list[str], dict[str, str]]:
        """Filter channels based on available recipient contact information.

        Returns
        -------
        tuple[list[str], dict[str, str]]
            (available_channel_ids, {unavailable_channel_id: reason}).

        """
        available: list[str] = []
        unavailable: dict[str, str] = {}

        for channel_id in channel_ids:
            factory = self._find_factory(channel_id)
            if factory is None:
                # Unknown channel — conservative: allow it
                available.append(channel_id)
                continue

            requirements = factory.adapter_class.get_requirements()
            missing: list[str] = []

            if requirements.get("requires_email", False) and not has_email:
                missing.append("email address")
            if requirements.get("requires_phone", False) and not has_phone:
                missing.append("phone number")
            if requirements.get("requires_ha_user", False) and not has_ha_user:
                missing.append("Home Assistant user")

            if missing:
                reason = (
                    f"Missing {missing[0]}"
                    if len(missing) == 1
                    else f"Missing {' and '.join(missing)}"
                )
                unavailable[channel_id] = reason
            else:
                available.append(channel_id)

        return available, unavailable

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def cleanup_all(self) -> None:
        """Destroy all adapters and clear records. Called during integration unload."""
        for channel_id, record in list(self._records.items()):
            await self._destroy_adapter(channel_id, record)
        self._records.clear()
        _LOGGER.info("All channel adapters cleaned up")


# ---------------------------------------------------------------------------
# Module-level detection helpers (no ChannelManager instance required)
# ---------------------------------------------------------------------------
# Used by config_flow.py during initial setup / reconfigure, before the
# integration's ChannelManager is available.


def _match_adapter_class(
    channel_id: str,
    adapter_classes: dict[str, type[DeliveryAdapter]],
) -> type[DeliveryAdapter] | None:
    """Return the adapter class that handles *channel_id*, or None."""
    if channel_id in adapter_classes:
        return adapter_classes[channel_id]
    for cls in adapter_classes.values():
        if cls.matches_channel(channel_id):
            return cls
    return None


def detect_notification_channels(
    hass: HomeAssistant,
    adapter_classes: dict[str, type[DeliveryAdapter]] | None = None,
) -> list[ChannelInfo]:
    """Discover available notify.* services in Home Assistant.

    Lightweight standalone variant of
    :meth:`ChannelManager._detect_notification_channels` for use in config
    flows before the integration's :class:`ChannelManager` is available.
    """
    services = hass.services.async_services()
    notify_services = services.get("notify", {})

    results: list[ChannelInfo] = []
    for service_id in sorted(notify_services.keys()):
        if service_id in _NOTIFY_SERVICE_EXCLUDE:
            continue

        channel_id = f"notify.{service_id}"
        if adapter_classes:
            adapter_cls = _match_adapter_class(channel_id, adapter_classes)
            label = (
                adapter_cls.get_channel_label(channel_id)
                if adapter_cls
                else service_id.replace("_", " ").title()
            )
        else:
            label = service_id.replace("_", " ").title()

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

    return results


def detect_media_players(hass: HomeAssistant) -> list[ChannelInfo]:
    """Discover media player entities suitable for TTS playback.

    Lightweight standalone variant of
    :meth:`ChannelManager._detect_media_players` for use in config flows
    before the integration's :class:`ChannelManager` is available.
    """
    results: list[ChannelInfo] = []

    for entity_id in hass.states.async_entity_ids("media_player"):
        state = hass.states.get(entity_id)
        if not state:
            continue

        supported = state.attributes.get("supported_features", 0)
        if (supported & REQUIRED_MP_FEATURES) != REQUIRED_MP_FEATURES:
            continue

        friendly_name = state.attributes.get("friendly_name", entity_id)
        results.append(
            ChannelInfo(
                id=entity_id,
                label=friendly_name,
                scope=ChannelScope.TTS,
                integration=state.attributes.get("device_class") or "media_player",
            )
        )

    return results


def detect_tts_entities(hass: HomeAssistant) -> list[str]:
    """Return entity IDs of TTS engine entities available in the state machine.

    Lightweight standalone variant of the old channel_registry.detect_tts_entities.
    Excludes internal HA action entities (``tts.speak``, ``tts.clear_cache``,
    ``tts.*_say``) that are not speech engines.
    """
    results: list[str] = []
    for entity_id in hass.states.async_entity_ids("tts"):
        suffix = entity_id.removeprefix("tts.")
        if suffix in _TTS_ACTION_ENTITY_SUFFIXES or suffix.endswith("_say"):
            continue
        results.append(entity_id)
    return sorted(results)
