"""Config repository for ANS integration."""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from homeassistant.core import HomeAssistant

from ..channels.channel_registry import (
    ChannelRegistry,
    detect_media_players,
    detect_notification_channels,
)
from ..const import (
    # CONFIG_IDENTITY_DEFAULT_SETTINGS_KEY,
    RCPT_CONFIG_ID_KEY,
)
from ..helper import get_main_entry, get_subentries
from ..models import (
    ChannelInfo,
    ConfigSnapshot,
    RecipientConfig,
    RecipientData,
    RecipientType,
    SystemConfig,
)

if TYPE_CHECKING:
    from ..channels.adapter_lifecycle import AdapterLifecycleManager

_LOGGER = logging.getLogger(__name__)


class ConfigRepository:
    """Central repository abstraction for ANS configuration."""

    def __init__(
        self,
        hass: HomeAssistant,
        adapter_classes: dict | None = None,
    ) -> None:
        """Initialize the ConfigRepository with the HomeAssistant instance."""
        self.hass = hass

        # Cached values
        self.system_config: SystemConfig | None = None
        self.recipients: dict[str, RecipientData] = {}
        self.recipient_configs: dict[str, RecipientConfig] = {}
        self.channel_registry: ChannelRegistry = ChannelRegistry(
            adapter_classes=adapter_classes
        )
        self._refresh_lock = asyncio.Lock()

    # ---------------------------
    # Main entry helpers
    # ---------------------------

    def _load_main_entry(self) -> bool:
        """Load the main config entry into the repository.

        Loads configuration exactly as stored without any transformations.
        Business logic for defaults and channel configuration should be
        handled in config flows, not in the repository.

        Merges data (structural settings) with options (tunable parameters).
        Options override data when both are present.
        """
        main_entry = get_main_entry(self.hass)
        if not main_entry:
            _LOGGER.error("No main ANS config entry found")
            return False

        # Start with structural settings from data
        sys_dict = dict(main_entry.data or {})

        # Override with tunable parameters from options
        if main_entry.options:
            sys_dict.update(main_entry.options)
            _LOGGER.debug(
                "Loaded system config: data (structural) + options (tunable parameters)"
            )
        else:
            _LOGGER.debug(
                "Loaded system config from data only (no options configured yet)"
            )

        self.system_config = SystemConfig.from_dict(sys_dict)

        _LOGGER.info("System config loaded successfully")
        return True

    def _unload_main_entry(self) -> bool:
        """Unload the main config entry from the repository."""
        try:
            self.system_config = None
        except Exception:
            _LOGGER.exception("Failed to unload main entry")
            return False
        else:
            return True

    # ---------------------------
    # Sub-entries helpers
    # ---------------------------

    def _load_subentries(self) -> bool:
        """Load all sub-entries into the repository."""
        loaded = True
        for entry in get_subentries(self.hass):
            try:
                recipient_id = entry.data.get(RCPT_CONFIG_ID_KEY)
                if recipient_id:
                    # Load receivers
                    self.recipients[recipient_id] = RecipientData.from_dict(
                        dict(entry.data)  # .get("data", {}))
                    )
                    # Load receiver configs
                    self.recipient_configs[recipient_id] = RecipientConfig.from_dict(
                        dict(entry.data)  # .get("options", {}))
                    )
                else:
                    _LOGGER.warning(
                        "Sub-entry %s has no identity ID, skipping", entry.subentry_id
                    )
                    loaded = False
            except Exception:
                _LOGGER.exception(
                    "Failed to load sub-entry %s; skipping", entry.subentry_id
                )
                loaded = False
        return loaded

    def _unload_subentries(self) -> bool:
        """Unload all sub-entries from the repository."""
        try:
            self.recipients.clear()
            self.recipient_configs.clear()
        except Exception:
            _LOGGER.exception("Error while unloading sub-entries")
            return False
        return True

    # ---------------------------
    # Loading & persistence
    # ---------------------------

    async def load(self) -> bool:
        """Reload configs from all entries into memory."""
        # Main entry (system config) is critical — failure aborts setup.
        if not self._load_main_entry():
            return False

        # Sub-entries (recipients) are non-fatal: failed entries are logged and
        # skipped, so a single bad recipient does not prevent the integration
        # from starting.
        if not self._load_subentries():
            _LOGGER.warning(
                "One or more recipient sub-entries could not be loaded; "
                "affected recipients will be unavailable until the issue is resolved"
            )

        # Channels always succeed (zero channels is a warning, not an error).
        await self.load_channels()
        return True

    def unload(self) -> bool:
        """Clear all cached configs."""
        # Unload sub-entries (receivers, receiver configs)
        sub_entries = self._unload_subentries()
        # Unload main entry data (system config, default receiver config)
        main_entry = self._unload_main_entry()
        return all([main_entry, sub_entries])

    # ---------------------------
    # Channel Management
    # ---------------------------

    async def refresh_channels(self) -> int:
        """Refresh channel registry from current HA services.

        Discovers both notify.* services and compatible media_player entities.

        Returns
        -------
        int
            Number of channels registered.

        """
        _LOGGER.debug("Refreshing channel registry...")

        async with self._refresh_lock:
            # Collect the new channel list BEFORE clearing the registry.
            # This keeps the registry populated while detection runs so that
            # concurrent snapshot() calls and parallel refresh_channels() calls
            # (e.g. from _on_notify_service_registered and update_listener)
            # never observe an empty registry.
            channels = detect_notification_channels(
                self.hass, adapter_classes=self.channel_registry.adapter_classes
            )
            media_players = detect_media_players(self.hass)
            channels.extend(media_players)

            # Atomic swap: replace contents only after the full list is ready.
            self.channel_registry.clear()

            if not channels:
                _LOGGER.warning("No notification channels detected")
                return 0

            self.channel_registry.register_multiple(channels)

            _LOGGER.info(
                "Refreshed channel registry: %d channels registered (%d notify, %d media_player)",
                len(channels),
                len(channels) - len(media_players),
                len(media_players),
            )

            return len(channels)

    async def load_channels(self) -> bool:
        """Load available notification channels into the registry.

        Returns
        -------
        bool
            Always True.  Zero detected channels is treated as a warning so
            that ANS can start even when notify integrations are not yet
            loaded (they may appear later; call refresh_channels() or reload
            the integration after all integrations are up).

        """
        count = await self.refresh_channels()

        if count == 0:
            _LOGGER.warning(
                "No notification channels detected during setup. "
                "Ensure notify integrations are configured, then reload ANS "
                "or call the 'ans.refresh_channels' service."
            )

        return True

    async def sync_channels_to_state(
        self, lifecycle_manager: AdapterLifecycleManager
    ) -> None:
        """Synchronize adapter state with the current channel registry and system config.

        Uses the current channel_registry contents as the authoritative set of
        detected channels.  Call :meth:`refresh_channels` or :meth:`load` first
        to ensure the registry reflects the current HA service landscape.

        Parameters
        ----------
        lifecycle_manager : AdapterLifecycleManager
            The adapter lifecycle manager whose adapter set will be synchronized.

        """
        detected_ids = set(self.channel_registry.get_all_ids())
        if self.system_config:
            await lifecycle_manager.sync_with_config(
                list(self.system_config.enabled_channels),
                detected_channel_ids=detected_ids,
            )
            _LOGGER.debug(
                "Adapter state synchronized with channel registry (%d detected channels)",
                len(detected_ids),
            )
        else:
            _LOGGER.warning(
                "No system config available during channel sync; adapter state unchanged"
            )

    async def refresh_and_sync(
        self, lifecycle_manager: AdapterLifecycleManager
    ) -> None:
        """Refresh channel registry and synchronize adapter state in one step.

        Convenience helper that calls :meth:`refresh_channels` followed by
        :meth:`sync_channels_to_state`.  Use this instead of the two-step
        call to ensure the channel list is always refreshed before adapters
        are synced.

        Parameters
        ----------
        lifecycle_manager : AdapterLifecycleManager
            The adapter lifecycle manager whose adapter set will be synchronized.

        """
        await self.refresh_channels()
        await self.sync_channels_to_state(lifecycle_manager)

    def get_channels_for_ui(
        self, recipient_type: RecipientType | None = None
    ) -> list[ChannelInfo]:
        """Get channels for UI display, optionally filtered by recipient type.

        Parameters
        ----------
        recipient_type : RecipientType, optional
            Filter by recipient type (None = all channels).

        Returns
        -------
        list[ChannelInfo]
            List of channel info objects.

        """
        if recipient_type is None:
            return self.channel_registry.get_all()

        return self.channel_registry.get_channels_for_recipient_type(recipient_type)

    # ---------------------------
    # Snapshot
    # ---------------------------

    def snapshot(self) -> ConfigSnapshot:
        """Return an immutable ConfigSnapshot representing the current ANS configuration."""
        if not self.system_config:
            raise RuntimeError(
                "ConfigRepository: system_config not available; cannot build ConfigSnapshot"
            )

        return ConfigSnapshot(
            snapshot_id=str(uuid4()),
            created_at=datetime.now(UTC),
            recipients=copy.deepcopy(self.recipients),
            recipient_configs=copy.deepcopy(self.recipient_configs),
            system_config=copy.deepcopy(self.system_config),
            channel_registry=copy.deepcopy(self.channel_registry),
        )
