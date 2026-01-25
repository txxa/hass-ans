"""Config repository for ANS integration."""

from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime
from uuid import uuid4

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant

from .channel_registry import ChannelRegistry
from .const import (
    # CONFIG_IDENTITY_DEFAULT_SETTINGS_KEY,
    ID_CONFIG_ID_KEY,
    SYS_RECIPIENT_CONFIG_KEY,
    SYS_RECIPIENT_ID,
    SYS_RECIPIENT_NAME,
)
from .helper import async_detect_notification_channels, get_main_entry, get_subentries
from .models import (
    ConfigSnapshot,
    RecipientConfig,
    RecipientData,
    RecipientType,
    SystemConfig,
)

_LOGGER = logging.getLogger(__name__)


class ConfigRepository:
    """Central repository abstraction for ANS configuration."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the ConfigRepository with the HomeAssistant instance."""
        self.hass = hass

        # Cached values
        self.system_config: SystemConfig | None = None
        self.recipients: dict[str, RecipientData] = {}
        self.recipient_configs: dict[str, RecipientConfig] = {}
        self.channel_registry: ChannelRegistry = ChannelRegistry()

    # ---------------------------
    # Main entry helpers
    # ---------------------------

    # def _update_validation_context(self) -> None:
    #     """Update the validation context with current system limits."""
    #     if self._system_config:
    #         system_limits = {
    #             SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY: self._system_config.retry_attempts_max,
    #             SYS_CONFIG_RATE_LIMIT_MAX_KEY: self._system_config.rate_limit_max,
    #         }
    #         available_channels = list(self._system_config.enabled_channels)
    #         self._validation_context = ValidationContext(
    #             system_limits=system_limits, available_channels=available_channels
    #         )

    # def _get_main_entry(self) -> ConfigEntry | None:
    #     """Return the main ANS config entry (should be unique)."""
    #     # TODO: harden this code incl. error handling (try/except) and logging
    #     entries = self.hass.config_entries.async_entries(DOMAIN)
    #     return entries[0] if entries else None

    def _load_main_entry(self) -> bool:
        """Load the main config entry into the repository.

        Loads configuration exactly as stored without any transformations.
        Business logic for defaults and channel configuration should be
        handled in config flows, not in the repository.
        """
        main_entry = get_main_entry(self.hass)
        if not main_entry:
            _LOGGER.error("No main ANS config entry found")
            return False

        # Load system config from entry data
        sys_dict = dict(main_entry.data or {})
        self.system_config = SystemConfig.from_dict(sys_dict)

        # Load system recipient config from entry options (or use defaults)
        sys_recipient_dict = (main_entry.options or {}).get(
            SYS_RECIPIENT_CONFIG_KEY, {}
        )

        if sys_recipient_dict:
            sys_recipient_config = RecipientConfig.from_dict(sys_recipient_dict)
            _LOGGER.debug(
                "System recipient loaded from options: low=%s, medium=%s, high=%s, critical=%s",
                sys_recipient_config.channels_low,
                sys_recipient_config.channels_medium,
                sys_recipient_config.channels_high,
                sys_recipient_config.channels_critical,
            )
        else:
            # No config stored - use model defaults
            sys_recipient_config = RecipientConfig.system_default()
            _LOGGER.info(
                "System recipient not configured in options, using model defaults"
            )

        # Create system recipient data
        sys_recipient_data = RecipientData(
            id=SYS_RECIPIENT_ID,
            name=SYS_RECIPIENT_NAME,
            type=RecipientType.SYSTEM,
            email=None,
            phone=None,
        )

        # Store in repository cache
        self.recipients[SYS_RECIPIENT_ID] = sys_recipient_data
        self.recipient_configs[SYS_RECIPIENT_ID] = sys_recipient_config

        _LOGGER.info("System recipient '%s' loaded successfully", SYS_RECIPIENT_ID)
        return True

    def _unload_main_entry(self) -> bool:
        """Unload the main config entry from the repository."""
        # TODO: harden this code incl. error handling (try/except) and logging
        self.system_config = None
        # Remove system recipient if present
        self.recipients.pop(SYS_RECIPIENT_ID, None)
        self.recipient_configs.pop(SYS_RECIPIENT_ID, None)
        return True

    # ---------------------------
    # Sub-entries helpers
    # ---------------------------

    # def _get_sub_entries(self) -> list[ConfigSubentry]:
    #     """Return all sub-entries for identities."""
    #     # TODO: harden this code incl. error handling (try/except) and logging
    #     main_entry = self._get_main_entry()
    #     if not main_entry:
    #         _LOGGER.error("No main ANS config entry found for sub-entries")
    #         return []
    #     return list(dict(main_entry.subentries).values())

    def _load_subentries(self) -> bool:
        """Load all sub-entries into the repository."""
        # TODO: harden this code incl. error handling (try/except) and logging
        loaded = True
        # for entry in self._get_sub_entries():
        for entry in get_subentries(self.hass):
            recipient_id = entry.data.get(ID_CONFIG_ID_KEY)
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
        return loaded

    def _get_subentry(self, recipient_id: str) -> ConfigSubentry | None:
        """Return a sub-entry by its identity ID."""
        # TODO: harden this code incl. error handling (try/except) and logging
        # for entry in self._get_sub_entries():
        for entry in get_subentries(self.hass):
            if entry.data.get(ID_CONFIG_ID_KEY) == recipient_id:
                return entry
        return None

    def _unload_subentries(self) -> bool:
        """Unload all sub-entries from the repository."""
        # TODO: harden this code incl. error handling (try/except) and logging
        self.recipients.clear()
        self.recipient_configs.clear()
        return True

    def _load_subentry(self, recipient_id: str) -> bool:
        """Load a specific sub-entry by identity ID."""
        # TODO: harden this code incl. error handling (try/except) and logging
        sub_entry = self._get_subentry(recipient_id)
        if not sub_entry:
            _LOGGER.error("No sub-entry found for receiver ID %s", recipient_id)
            return False
        # Load data and options
        data = dict(sub_entry.data)
        # Create IdentityConfig from data
        receiver = RecipientData.from_dict(data.get("data", {}))
        # Create IdentityConfig from options
        receiver_config = RecipientConfig.from_dict(data.get("options", {}))
        # Store in cache
        self.recipients[recipient_id] = receiver
        self.recipient_configs[recipient_id] = receiver_config
        return True

    def _unload_subentry(self, recipient_id: str) -> bool:
        """Unload a specific sub-entry by identity ID."""
        # TODO: harden this code incl. error handling (try/except) and logging
        if recipient_id not in self.recipients:
            _LOGGER.error("No sub-entry found for receiver ID %s", recipient_id)
            return False
        # Remove from cache
        self.recipients.pop(recipient_id, None)
        self.recipient_configs.pop(recipient_id, None)
        return True

    # ---------------------------
    # Loading & persistence
    # ---------------------------

    async def load(self) -> bool:
        """Reload configs from all entries into memory."""
        # TODO: harden this code incl. error handling (try/except) and logging
        # Load main entry data (system config, default receiver config)
        main_entry = self._load_main_entry()
        # Load sub-config entries (receivers, receiver configs)
        sub_entries = self._load_subentries()
        # Load available notification channels
        channels = await self.load_channels()
        return all([main_entry, sub_entries, channels])

    def unload(self) -> bool:
        """Clear all cached configs."""
        # TODO: harden this code incl. error handling (try/except) and logging
        # Unload sub-entries (receivers, receiver configs)
        main_entry = self._unload_subentries()
        # Unload main entry data (system config, default receiver config)
        sub_entries = self._unload_main_entry()
        return all([main_entry, sub_entries])

    # ---------------------------
    # Snapshot
    # ---------------------------

    async def load_channels(self) -> bool:
        """Load available notification channels into the registry.

        Returns
        -------
        bool
            True if channels were loaded successfully.

        """
        try:
            channels = await async_detect_notification_channels(self.hass)
            self.channel_registry.register_multiple(channels)
            _LOGGER.info(
                "Loaded %d notification channels into registry",
                self.channel_registry.count(),
            )
            return True
        except Exception as e:
            _LOGGER.error("Failed to load notification channels: %s", e)
            return False

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
            channel_registry=self.channel_registry,
        )
