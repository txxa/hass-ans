"""Config repository for ANS integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant

from .const import (
    # CONFIG_IDENTITY_DEFAULT_SETTINGS_KEY,
    ID_CONFIG_ID_KEY,
)
from .helper import get_main_entry, get_subentries
from .models import (
    ConfigSnapshot,
    Identity,
    IdentityConfig,
    Receiver,
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
        self.default_receiver_config: IdentityConfig | None = None
        self.receivers: dict[str, Identity] = {}
        self.receiver_configs: dict[str, IdentityConfig] = {}

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
        """Load the main config entry into the repository."""
        # TODO: harden this code incl. error handling (try/except) and logging
        # main_entry = self._get_main_entry()
        main_entry = get_main_entry(self.hass)
        if not main_entry:
            _LOGGER.error("No main ANS config entry found")
            return False
        # Load system config
        # sys_dict = main_entry.data.get(CONFIG_SYSTEM_SETTINGS_KEY, {})
        sys_dict = dict(main_entry.data or {})
        self.system_config = SystemConfig.from_dict(sys_dict)
        # Load default receiver config
        # def_id_dict = main_entry.options.get(CONFIG_IDENTITY_DEFAULT_SETTINGS_KEY, {})
        def_id_dict = dict(main_entry.options or {})
        self.default_receiver_config = IdentityConfig.from_dict(def_id_dict)
        return True

    def _unload_main_entry(self) -> bool:
        """Unload the main config entry from the repository."""
        # TODO: harden this code incl. error handling (try/except) and logging
        self.system_config = None
        self.default_receiver_config = None
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
            receiver_id = entry.data.get(ID_CONFIG_ID_KEY)
            if receiver_id:
                # Load receivers
                self.receivers[receiver_id] = Identity.from_dict(
                    dict(entry.data)  # .get("data", {}))
                )
                # Load receiver configs
                self.receiver_configs[receiver_id] = IdentityConfig.from_dict(
                    dict(entry.data)  # .get("options", {}))
                )
            else:
                _LOGGER.warning(
                    "Sub-entry %s has no identity ID, skipping", entry.subentry_id
                )
                loaded = False
        return loaded

    def _get_subentry(self, receiver_id: str) -> ConfigSubentry | None:
        """Return a sub-entry by its identity ID."""
        # TODO: harden this code incl. error handling (try/except) and logging
        # for entry in self._get_sub_entries():
        for entry in get_subentries(self.hass):
            if entry.data.get(ID_CONFIG_ID_KEY) == receiver_id:
                return entry
        return None

    def _unload_subentries(self) -> bool:
        """Unload all sub-entries from the repository."""
        # TODO: harden this code incl. error handling (try/except) and logging
        self.receivers.clear()
        self.receiver_configs.clear()
        return True

    def _load_subentry(self, receiver_id: str) -> bool:
        """Load a specific sub-entry by identity ID."""
        # TODO: harden this code incl. error handling (try/except) and logging
        sub_entry = self._get_subentry(receiver_id)
        if not sub_entry:
            _LOGGER.error("No sub-entry found for receiver ID %s", receiver_id)
            return False
        # Load data and options
        data = dict(sub_entry.data)
        # Create IdentityConfig from data
        receiver = Identity.from_dict(data.get("data", {}))
        # Create IdentityConfig from options
        receiver_config = IdentityConfig.from_dict(data.get("options", {}))
        # Store in cache
        self.receivers[receiver_id] = receiver
        self.receiver_configs[receiver_id] = receiver_config
        return True

    def _unload_subentry(self, receiver_id: str) -> bool:
        """Unload a specific sub-entry by identity ID."""
        # TODO: harden this code incl. error handling (try/except) and logging
        if receiver_id not in self.receivers:
            _LOGGER.error("No sub-entry found for receiver ID %s", receiver_id)
            return False
        # Remove from cache
        self.receivers.pop(receiver_id, None)
        self.receiver_configs.pop(receiver_id, None)
        return True

    # ---------------------------
    # Loading & persistence
    # ---------------------------

    def load(self) -> bool:
        """Reload configs from all entries into memory."""
        # TODO: harden this code incl. error handling (try/except) and logging
        # Load main entry data (system config, default receiver config)
        main_entry = self._load_main_entry()
        # Load sub-config entries (receivers, receiver configs)
        sub_entries = self._load_subentries()
        return all([main_entry, sub_entries])

    def unload(self) -> bool:
        """Clear all cached configs."""
        # TODO: harden this code incl. error handling (try/except) and logging
        # Unload sub-entries (receivers, receiver configs)
        main_entry = self._unload_subentries()
        # Unload main entry data (system config, default receiver config)
        sub_entries = self._unload_main_entry()
        return all([main_entry, sub_entries])

    # ---------------------------
    # System config
    # ---------------------------

    # def get_system_config(self) -> SystemConfig | None:
    #     """Return the current system config."""
    #     # TODO: harden this code incl. error handling (try/except) and logging
    #     return self.system_config

    # def set_system_config(self, config: SystemConfig) -> None:
    #     """Set or update the system config."""
    #     # TODO: harden this code incl. error handling (try/except) and logging
    #     self.validator.validate_system_config(config)
    #     self._system_config = config
    #     self._update_validation_context()

    # ---------------------------
    # Default identity config
    # ---------------------------

    # def get_default_identity_config(self) -> IdentityConfig | None:
    #     """Return the default identity config."""
    #     # TODO: harden this code incl. error handling (try/except) and logging
    #     return self.default_receiver_config

    # def set_default_identity_config(self, config: IdentityConfig) -> None:
    #     """Set or update the default identity config."""
    #     # TODO: harden this code incl. error handling (try/except) and logging
    #     if self._default_receiver_config:
    #         self.validator.validate_identity_config(config)
    #         self._default_receiver_config = config

    # ---------------------------
    # Receivers
    # ---------------------------

    # def get_receivers(self) -> dict[str, Receiver]:
    #     """Return a dictionary of all receivers, including their configs."""
    #     # TODO: harden this code incl. error handling (try/except) and logging
    #     receivers = {}
    #     for k, v in self.receivers.items():
    #         if k in self.receiver_configs:
    #             receivers[k] = Receiver(v, self.receiver_configs[k])
    #     return receivers

    def get_receiver(self, receiver_id: str) -> Receiver | None:
        """Return a receiver by its identity ID, including its config."""
        # TODO: harden this code incl. error handling (try/except) and logging
        if receiver_id in self.receivers and receiver_id in self.receiver_configs:
            return Receiver(
                self.receivers[receiver_id], self.receiver_configs[receiver_id]
            )
        return None

    # def add_receiver(self, identity: Identity, config: IdentityConfig) -> None:
    #     """Add a new receiver with its identity and config, ensuring uniqueness on ID."""
    #     # TODO: harden this code incl. error handling (try/except) and logging
    #     if self._default_receiver_config and identity:
    #         self.validator.validate_identity_config(config)
    #         self._receivers[identity.id] = identity
    #         self._receiver_configs[identity.id] = config

    # # ---------------------------
    # # Identities
    # # ---------------------------

    # def get_identities(self) -> dict[str, Identity]:
    #     # TODO: harden this code incl. error handling (try/except) and logging
    #     return self._receivers

    # def add_identity(self, identity: Identity) -> None:
    #     # TODO: harden this code incl. error handling (try/except) and logging
    #     # TODO: enforce uniqueness on ID
    #     if identity:
    #         self._receivers[identity.id] = identity

    # def get_identity(self, receiver_id: str) -> Identity | None:
    #     # TODO: harden this code incl. error handling (try/except) and logging
    #     return next(
    #         (v for k, v in self._receivers.items() if k == receiver_id),
    #         None,
    #     )

    # # ---------------------------
    # # Identity configs
    # # ---------------------------

    # def get_identity_configs(self) -> dict[str, IdentityConfig]:
    #     # TODO: harden this code incl. error handling (try/except) and logging
    #     return self._receiver_configs

    # def add_identity_config(self, config: IdentityConfig) -> None:
    #     # TODO: harden this code incl. error handling (try/except) and logging
    #     if (
    #         self._default_receiver_config
    #         and config
    #         and self.validator.validate_identity_config(
    #             config.to_dict(), self._default_receiver_config.to_dict()
    #         )
    #     ):
    #         if config.identity_id:
    #             self._receiver_configs[config.identity_id] = config

    # def find_identity_config(self, receiver_id: str) -> IdentityConfig | None:
    #     # TODO: harden this code incl. error handling (try/except) and logging
    #     return next(
    #         (v for k, v in self._receiver_configs.items() if k == receiver_id),
    #         None,
    #     )

    # ---------------------------
    # HA User Helpers
    # ---------------------------

    # async def get_all_ha_users(self) -> list[dict[str, Any]]:
    #     """Return all HA users (id + name)."""
    #     # TODO: harden this code incl. error handling (try/except) and logging
    #     users = await self.hass.auth.async_get_users()
    #     return [
    #         {
    #             "id": u.id,
    #             "name": u.name or "Unnamed User",
    #         }
    #         for u in users
    #     ]

    # async def get_not_configured_ha_users(self) -> list[dict[str, Any]]:
    #     """Return all HA users that are not yet configured an ANS receiver."""
    #     # TODO: harden this code incl. error handling (try/except) and logging
    #     users = await self.get_all_ha_users()
    #     existing_ids = set(self._receivers.keys())
    #     return [u for u in users if u["id"] not in existing_ids]


# add this to the ConfigRepository class (e.g. near other public helpers)


def snapshot_config(self, refresh: bool = False) -> ConfigSnapshot:
    """Return an immutable ConfigSnapshot representing the current ANS configuration.

    Args:
        refresh: if True force reloading config entries from storage (calls self.load()).

    Returns:
        ConfigSnapshot built from in-memory caches (self.receivers, self.receiver_configs, self.system_config).

    Raises:
        RuntimeError: if system_config is not loaded and cannot be obtained.
    """
    # Optionally refresh caches from config entries
    if refresh:
        try:
            self.load()
        except Exception as exc:
            _LOGGER.warning(
                "ConfigRepository.snapshot_config: refresh/load() failed: %s", exc
            )

    # Ensure we have a system_config (required by ConfigSnapshot)
    if self.system_config is None:
        # Try a best-effort load if it wasn't loaded yet
        try:
            self.load()
        except Exception as exc:
            _LOGGER.debug(
                "ConfigRepository.snapshot_config: load() attempt raised: %s", exc
            )

    if self.system_config is None:
        raise RuntimeError(
            "ConfigRepository: system_config not available; cannot build ConfigSnapshot"
        )

    # Prepare identity configs, filling missing per-identity config from default if needed
    identity_configs = dict(self.receiver_configs)  # shallow copy
    identities = dict(self.receivers)  # shallow copy

    # Fill missing identity configs with default_receiver_config (or IdentityConfig.default())
    for rid in list(identities.keys()):
        if rid not in identity_configs:
            if self.default_receiver_config is not None:
                identity_configs[rid] = self.default_receiver_config
            else:
                # fallback to IdentityConfig.default() to ensure snapshot completeness
                try:
                    identity_configs[rid] = IdentityConfig.default()
                except Exception:
                    _LOGGER.debug(
                        "ConfigRepository.snapshot_config: unable to construct default IdentityConfig for %s",
                        rid,
                    )
                    # leave absent; ConfigSnapshot/from_mappings will still accept an empty mapping entry,
                    # but downstream code should handle missing identity_config robustly.

    # Build and return immutable ConfigSnapshot
    return ConfigSnapshot.from_mappings(
        identities, identity_configs, self.system_config
    )
