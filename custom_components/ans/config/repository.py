"""Config repository for ANS integration."""

from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime
from uuid import uuid4

from homeassistant.core import HomeAssistant

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

_LOGGER = logging.getLogger(__name__)


class ConfigRepository:
    """Central repository abstraction for ANS configuration."""

    def __init__(
        self,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the ConfigRepository with the HomeAssistant instance."""
        self.hass = hass

        # Cached values
        self.system_config: SystemConfig | None = None
        self.recipients: dict[str, RecipientData] = {}
        self.recipient_configs: dict[str, RecipientConfig] = {}

        # Injected by create_system() after ChannelManager construction.
        from ..channels.channel_manager import ChannelManager  # noqa: PLC0415

        self.channel_manager: ChannelManager | None = None

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

    async def refresh_and_sync(self) -> None:
        """Refresh channel detection and synchronize adapter state in one step.

        Delegates to ChannelManager.sync() which is the single source of
        truth for both channel metadata and live adapters.
        """
        if self.channel_manager is None:
            _LOGGER.warning(
                "refresh_and_sync called before ChannelManager was injected; skipping"
            )
            return
        if not self.system_config:
            _LOGGER.warning(
                "refresh_and_sync called with no system_config available; skipping"
            )
            return
        await self.channel_manager.sync(list(self.system_config.enabled_channels))

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
        if self.channel_manager is None:
            return []
        if recipient_type is None:
            return self.channel_manager.get_all_infos()
        return self.channel_manager.get_infos_for_recipient_type(recipient_type)

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
        )
