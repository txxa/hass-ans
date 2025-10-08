"""Models for the ANS system."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

# from .config_validator import ConfigValidator
from .const import (
    # SYS_CONFIG_TTS_INTEGRATION_KEY,
    CONFIG_VERSION_KEY,
    DEFAULT_BLOCKED_SOURCES_PATTERN,
    DEFAULT_DND_ALLOWED_SOURCES_PATTERN,
    DEFAULT_DND_ENABLED,
    DEFAULT_DND_END,
    DEFAULT_DND_START,
    DEFAULT_RATE_LIMIT,
    DEFAULT_RETRY_ATTEMPTS,
    ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY,
    ID_CONFIG_CHANNELS_KEY,
    ID_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY,
    ID_CONFIG_DND_ENABLED_KEY,
    ID_CONFIG_DND_END_KEY,
    ID_CONFIG_DND_START_KEY,
    ID_CONFIG_EMAIL_KEY,
    ID_CONFIG_ID_KEY,
    ID_CONFIG_IDENTITY_ID_KEY,
    ID_CONFIG_NAME_KEY,
    ID_CONFIG_NOTIFICATION_TYPES_KEY,
    ID_CONFIG_PHONE_KEY,
    ID_CONFIG_RATE_LIMIT_KEY,
    ID_CONFIG_RETRY_ATTEMPTS_KEY,
    ID_CONFIG_TYPE_KEY,
    SYS_CONFIG_ENABLED_CHANNELS_KEY,
    SYS_CONFIG_RATE_LIMIT_MAX_KEY,
    SYS_CONFIG_RATE_LIMIT_WINDOW_KEY,
    SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY,
)

_LOGGER = logging.getLogger(__name__)

# --- Data Classes ---


@dataclass
class Receiver:
    """Combines identity and receiver config preferences in a single object."""

    identity: Identity
    config: IdentityConfig


@dataclass
class Identity:
    """Represents an ANS receiver (native HA user or custom virtual target)."""

    id: str
    type: IdentityType
    name: str
    email: str | None
    phone: str | None
    version: int = field(default=1)

    def __post_init__(self):
        """Validate and convert configuration values."""
        # Import at runtime to avoid circular imports
        from .config_validator import ConfigValidator

        ConfigValidator.validate_identity(self)

    def to_dict(self) -> dict:
        """Convert to JSON-friendly dictionary for storage."""
        data = asdict(self)
        # Ensure ID is always a string
        data[ID_CONFIG_ID_KEY] = str(self.id)
        # Convert Enum to string for Store persistence
        if isinstance(self.type, Enum):
            data[ID_CONFIG_TYPE_KEY] = self.type.value
        else:
            data[ID_CONFIG_TYPE_KEY] = self.type
        return data

    @staticmethod
    def from_dict(data: dict) -> Identity:
        """Create Identity from a dictionary."""
        return Identity(
            id=data[ID_CONFIG_ID_KEY],
            type=IdentityType(data[ID_CONFIG_TYPE_KEY]),
            name=data[ID_CONFIG_NAME_KEY],
            email=data.get(ID_CONFIG_EMAIL_KEY),
            phone=data.get(ID_CONFIG_PHONE_KEY),
            version=data.get(CONFIG_VERSION_KEY, 1),
        )


@dataclass
class IdentityConfig:
    """Configuration for an ANS receiver."""

    identity_id: str | None  # Identity ID, must be set for each config
    retry_attempts: int
    rate_limit: int
    notification_types: list[NotificationType]
    blocked_sources_pattern: str | None
    channels_low: list[str]
    channels_medium: list[str]
    channels_high: list[str]
    channels_critical: list[str]
    dnd_enabled: bool
    dnd_start: str | None
    dnd_end: str | None
    dnd_allowed_sources_pattern: str | None
    version: int = field(default=1)

    def __post_init__(self):
        """Validate and normalize configuration values."""
        # Normalize identity_id to None or string
        if self.identity_id is not None:
            self.identity_id = str(self.identity_id)

        from .config_validator import ConfigValidator

        ConfigValidator.validate_identity_config(self)

    @classmethod
    def default(cls) -> IdentityConfig:
        """Return a default identity config instance."""
        return cls(
            identity_id=None,
            retry_attempts=DEFAULT_RETRY_ATTEMPTS,
            rate_limit=DEFAULT_RATE_LIMIT,
            notification_types=[
                NotificationType.INFO,
                NotificationType.WARNING,
                NotificationType.ALERT,
                NotificationType.REMINDER,
                NotificationType.EVENT,
                NotificationType.SECURITY,
            ],
            channels_low=[],
            channels_medium=[],
            channels_high=[],
            channels_critical=[],
            dnd_enabled=DEFAULT_DND_ENABLED,
            dnd_start=DEFAULT_DND_START,
            dnd_end=DEFAULT_DND_END,
            dnd_allowed_sources_pattern=DEFAULT_DND_ALLOWED_SOURCES_PATTERN,
            blocked_sources_pattern=DEFAULT_BLOCKED_SOURCES_PATTERN,
            version=1,
        )

    def to_dict(self) -> dict:
        """Convert to JSON-friendly dictionary for storage."""
        data = asdict(self)
        # Enums → values
        # data[ID_CONFIG_CRITICALITY_CHANNELS_KEY] = [
        #     c.value for c in self.criticality_levels
        # ]
        data[ID_CONFIG_NOTIFICATION_TYPES_KEY] = [
            t.value for t in self.notification_types
        ]
        data[
            f"{ID_CONFIG_CHANNELS_KEY}_{NotificationCriticality.LOW.value.lower()}"
        ] = list(self.channels_low)
        data[
            f"{ID_CONFIG_CHANNELS_KEY}_{NotificationCriticality.MEDIUM.value.lower()}"
        ] = list(self.channels_medium)
        data[
            f"{ID_CONFIG_CHANNELS_KEY}_{NotificationCriticality.HIGH.value.lower()}"
        ] = list(self.channels_high)
        data[
            f"{ID_CONFIG_CHANNELS_KEY}_{NotificationCriticality.CRITICAL.value.lower()}"
        ] = list(self.channels_critical)
        # Ensure identity_id stays string
        if self.identity_id is None:
            data[ID_CONFIG_IDENTITY_ID_KEY] = None
        else:
            data[ID_CONFIG_IDENTITY_ID_KEY] = str(self.identity_id)
        return data

    @staticmethod
    def from_dict(data: dict) -> IdentityConfig:
        """Create IdentityConfig from a dictionary."""
        # migration hook could be called here if `CONFIG_VERSION_KEY` differs
        version = int(data.get(CONFIG_VERSION_KEY, 1))

        # convert enums back from saved values
        notification_types = [
            NotificationType(t) for t in data.get(ID_CONFIG_NOTIFICATION_TYPES_KEY, [])
        ]
        channels_low = data.get(
            f"{ID_CONFIG_CHANNELS_KEY}_{NotificationCriticality.LOW.value.lower()}",
            [],
        )
        channels_medium = data.get(
            f"{ID_CONFIG_CHANNELS_KEY}_{NotificationCriticality.MEDIUM.value.lower()}",
            [],
        )
        channels_high = data.get(
            f"{ID_CONFIG_CHANNELS_KEY}_{NotificationCriticality.HIGH.value.lower()}",
            [],
        )
        channels_critical = data.get(
            f"{ID_CONFIG_CHANNELS_KEY}_{NotificationCriticality.CRITICAL.value.lower()}",
            [],
        )

        return IdentityConfig(
            identity_id=data.get(ID_CONFIG_IDENTITY_ID_KEY),
            retry_attempts=data.get(
                ID_CONFIG_RETRY_ATTEMPTS_KEY, DEFAULT_RETRY_ATTEMPTS
            ),
            rate_limit=data.get(ID_CONFIG_RATE_LIMIT_KEY, DEFAULT_RATE_LIMIT),
            # criticality_levels=criticality_levels,
            notification_types=notification_types,
            channels_low=channels_low,
            channels_medium=channels_medium,
            channels_high=channels_high,
            channels_critical=channels_critical,
            dnd_enabled=data.get(ID_CONFIG_DND_ENABLED_KEY, False),
            dnd_start=data.get(ID_CONFIG_DND_START_KEY),
            dnd_end=data.get(ID_CONFIG_DND_END_KEY),
            dnd_allowed_sources_pattern=data.get(
                ID_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY
            ),
            blocked_sources_pattern=data.get(ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY),
            version=version,
        )


@dataclass
class SystemConfig:
    """Configuration for the ANS system."""

    retry_attempts_max: int
    rate_limit_max: int
    rate_limit_window: int  # seconds
    enabled_channels: list[str]
    # tts_integration: str | None
    version: int = field(default=1)

    def __post_init__(self):
        """Validate and convert configuration values."""
        from .config_validator import ConfigValidator

        ConfigValidator.validate_system_config(self)

    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        data = asdict(self)
        data[SYS_CONFIG_ENABLED_CHANNELS_KEY] = list(self.enabled_channels)
        return data

    @staticmethod
    def from_dict(data: dict) -> SystemConfig:
        """Create SystemConfig from a dictionary."""
        return SystemConfig(
            retry_attempts_max=data[SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY],
            rate_limit_max=data[SYS_CONFIG_RATE_LIMIT_MAX_KEY],
            rate_limit_window=data[SYS_CONFIG_RATE_LIMIT_WINDOW_KEY],
            enabled_channels=data.get(SYS_CONFIG_ENABLED_CHANNELS_KEY, []),
            # tts_integration=data.get(SYS_CONFIG_TTS_INTEGRATION_KEY),
            version=data.get(CONFIG_VERSION_KEY, 1),
        )


# @dataclass
# class SystemLimits:
#     """System limits for ANS configuration."""

#     enabled_channels: list[ChannelInfo]
#     rate_limit_max: int
#     retries_max: int

#     def __init__(self, system_config: SystemConfig):
#         """Initialize system limits."""
#         self.enabled_channels = system_config.enabled_channels
#         self.rate_limit_max = system_config.rate_limit_max
#         self.retries_max = system_config.retries_max


@dataclass
class IntegrationInfo:
    """Integration information."""

    id: str
    label: str
    service: str


# @dataclass
# class NotificationChannel:
#     """Information about a notification channel."""

#     id: str
#     label: str
#     integration: str
#     service: str


# @dataclass
# class ChannelInfo:
#     """Information about a notification channel."""

#     id: str
#     name: str

#     def to_dict(self) -> dict:
#         """Convert to dictionary for storage."""
#         return asdict(self)

#     @staticmethod
#     def from_dict(data: dict) -> ChannelInfo:
#         """Create ChannelInfo from a dictionary."""
#         return ChannelInfo(
#             id=data["id"],
#             name=data["name"],
#         )


@dataclass
class DeliveryStats:
    """Persistent delivery statistics per user and channel."""

    identity_id: str | None  # Identity ID, must be set for each stats entry
    channel_id: str
    total_sent: int = 0
    total_failed: int = 0
    last_sent_timestamp: str | None = None  # ISO8601
    version: int = field(default=1)

    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> DeliveryStats:
        """Create DeliveryStats from a dictionary."""
        return DeliveryStats(
            identity_id=data.get(ID_CONFIG_IDENTITY_ID_KEY),
            channel_id=data["channel_id"],
            total_sent=data.get("total_sent", 0),
            total_failed=data.get("total_failed", 0),
            last_sent_timestamp=data.get("last_sent_timestamp"),
            version=data.get("version", 1),
        )


@dataclass
class DeliveryJob:
    job_id: str
    identity_id: str
    channel: str
    title: str | None
    message: str
    data: dict[str, Any]
    attempts: int = 0
    max_attempts: int = 3
    next_try_ts: float | None = None  # unix timestamp

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> DeliveryJob:
        return DeliveryJob(
            job_id=d["job_id"],
            identity_id=d["identity_id"],
            channel=d["channel"],
            title=d.get("title"),
            message=d.get("message", ""),
            data=d.get("data", {}),
            attempts=int(d.get("attempts", 0)),
            max_attempts=int(d.get("max_attempts", 3)),
            next_try_ts=d.get("next_try_ts"),
        )


# --- Enums ---


class IdentityType(Enum):
    """Types of users in the ANS system."""

    HA_USER = "HA_USER"
    VIRTUAL = "VIRTUAL"


class NotificationCriticality(Enum):
    """Criticality levels for notifications."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class NotificationType(Enum):
    """Types of notifications."""

    INFO = "INFO"
    WARNING = "WARNING"
    ALERT = "ALERT"
    REMINDER = "REMINDER"
    EVENT = "EVENT"
    SECURITY = "SECURITY"


@dataclass(frozen=True)
class RateResult:
    allowed: bool
    reason: str | None = None
    retry_after_seconds: float | None = None
    tokens_left: int | None = None


@dataclass
class DeliveryResult:
    channel: str
    status: str  # "SUCCESS" | "TRANSIENT_FAIL" | "PERMANENT_FAIL"
    attempt_no: int
    timestamp: str
    error_code: str | None = None
    details: str | None = None


@dataclass
class NotificationEnvelope:
    notification_id: str
    timestamp: float | None = None
    source: str | None = None
    title: str | None = None
    body: str | None = None
    type: Any | None = None
    criticality: Any | None = None
    metadata: dict | None = None
    target_identities: list[str] | None = None


@dataclass
class RetryEntry:
    notification_id: str
    identity_id: str
    channel: str  # for identity-level retry use special token e.g. "__IDENTITY__"
    envelope_meta: dict[str, Any]
    next_attempt_ts: float
    attempts_done: int
    max_attempts: int
    last_error_code: str | None = None
    created_ts: float = 0.0
    version: int = 1


@dataclass
class ProcessingSummary:
    notification_id: str
    accepted_identities: int
    processed_identities: int
    filtered_identities: list[str]
    rate_limited_identities: list[str]
    errors: list[dict[str, Any]]


@dataclass(frozen=True)
class ConfigSnapshot:
    """Immutable snapshot of the ANS configuration used during a single notification processing run.

    Purpose
    - Provide a stable (time-consistent) view of identities, identity configs and system config
      for the duration of processing a notification.
    - Prevent accidental mutation by pipeline components.
    - Provide small convenience helpers for lookup and safe merging (returns a new snapshot).

    Construction
    - Use ConfigSnapshot.from_mappings(...) when you already have Identity and IdentityConfig objects.
    - Use ConfigSnapshot.from_repository(...) if you have a ConfigRepository-like object (optional helper).
    """

    identities: Mapping[str, Identity]
    identity_configs: Mapping[str, IdentityConfig]
    system_config: SystemConfig
    retrieved_at: datetime

    # -----------------------
    # Factory helpers
    # -----------------------
    @classmethod
    def from_mappings(
        cls,
        identities: dict[str, Identity],
        identity_configs: dict[str, IdentityConfig],
        system_config: SystemConfig,
        *,
        retrieved_at: datetime | None = None,
    ) -> ConfigSnapshot:
        """Create a snapshot from provided mappings of Identity and IdentityConfig objects.

        Args:
            identities: dict[str, Identity] (may be empty)
            identity_configs: dict[str, IdentityConfig] (may be empty)
            system_config: SystemConfig instance (required)
            retrieved_at: optional datetime indicating when snapshot was taken (UTC). Defaults to now (UTC).

        Returns:
            ConfigSnapshot (immutable)

        """
        if identities is None:
            identities = {}
        if identity_configs is None:
            identity_configs = {}
        if retrieved_at is None:
            retrieved_at = datetime.now(timezone.utc)

        # Wrap the input dicts in MappingProxyType to make them read-only
        identities_mp = MappingProxyType(dict(identities))
        identity_configs_mp = MappingProxyType(dict(identity_configs))

        return cls(
            identities=identities_mp,
            identity_configs=identity_configs_mp,
            system_config=system_config,
            retrieved_at=retrieved_at,
        )

    @classmethod
    def from_repository(
        cls, repo: object, *, retrieved_at: datetime | None = None
    ) -> ConfigSnapshot:
        """Convenience factory if you have a ConfigRepository-like object.

        The repository is expected to expose either:
          - .get_identities() -> dict[str, Identity] and .get_identity_configs() -> dict[str, IdentityConfig]
          - OR attributes .identities and .identity_configs (mappings)
          - AND .system_config attribute or .get_system_config()

        This method is defensive and will try several access patterns.
        """
        # defensive extraction of identities and configs
        identities = {}
        identity_configs = {}
        system_config = None

        # identities
        if hasattr(repo, "identities"):
            try:
                identities = getattr(repo, "identities") or {}
            except Exception:
                identities = {}

        # identity_configs
        if hasattr(repo, "identity_configs"):
            try:
                identity_configs = getattr(repo, "identity_configs") or {}
            except Exception:
                identity_configs = {}

        # system_config
        if hasattr(repo, "system_config"):
            try:
                system_config = getattr(repo, "system_config")
            except Exception:
                system_config = None

        if system_config is None:
            raise ValueError(
                "ConfigRepository did not expose a system_config; required for snapshot creation"
            )

        return cls.from_mappings(
            identities=identities,
            identity_configs=identity_configs,
            system_config=system_config,
            retrieved_at=retrieved_at,
        )

    # -----------------------
    # Convenience lookups
    # -----------------------
    def get_identity(self, identity_id: str) -> Identity | None:
        """Return Identity object for id or None if not present."""
        return self.identities.get(identity_id)

    def get_identity_config(self, identity_id: str) -> IdentityConfig | None:
        """Return IdentityConfig object for id or None if not present."""
        return self.identity_configs.get(identity_id)

    def identity_ids(self) -> Iterable[str]:
        """Return an iterable of all identity ids present in the snapshot."""
        return self.identities.keys()

    def identity_config_items(self) -> Iterable[tuple[str, IdentityConfig]]:
        """Return iterable of (identity_id, IdentityConfig) pairs."""
        return self.identity_configs.items()

    # -----------------------
    # Immutable "mutation" helpers
    # -----------------------
    def with_updated_identity(
        self, identity: Identity, identity_config: IdentityConfig | None = None
    ) -> ConfigSnapshot:
        """Return a new ConfigSnapshot with the provided identity (and optionally its config) inserted/updated. Original snapshot remains unchanged."""
        # shallow copy underlying dicts then wrap again as MappingProxyType
        new_identities = dict(self.identities)
        new_identities[str(identity.id)] = identity

        new_identity_configs = dict(self.identity_configs)
        if identity_config is not None:
            new_identity_configs[str(identity.id)] = identity_config

        return ConfigSnapshot.from_mappings(
            new_identities,
            new_identity_configs,
            self.system_config,
            retrieved_at=datetime.now(timezone.utc),
        )

    def with_removed_identity(self, identity_id: str) -> "ConfigSnapshot":
        """Return a new ConfigSnapshot with the given identity and its config removed."""
        new_identities = dict(self.identities)
        new_identity_configs = dict(self.identity_configs)
        new_identities.pop(identity_id, None)
        new_identity_configs.pop(identity_id, None)
        return ConfigSnapshot.from_mappings(
            new_identities,
            new_identity_configs,
            self.system_config,
            retrieved_at=datetime.now(timezone.utc),
        )

    def with_system_config(self, system_config: "SystemConfig") -> "ConfigSnapshot":
        """Return a new ConfigSnapshot using the provided system_config."""
        return ConfigSnapshot.from_mappings(
            dict(self.identities),
            dict(self.identity_configs),
            system_config,
            retrieved_at=datetime.now(timezone.utc),
        )

    # -----------------------
    # Utilities
    # -----------------------
    def as_primitives(self) -> dict[str, object]:
        """Convert snapshot to primitive-friendly dict (useful for debugging or events).

        Note: Identity and IdentityConfig objects must provide a .to_dict() method for sensible output.
        """

        def maybe_to_dict(obj):
            try:
                return obj.to_dict()
            except Exception:
                # fallback to __dict__ if data class or simple obj
                try:
                    return dict(obj.__dict__)
                except Exception:
                    return str(obj)

        return {
            "retrieved_at": self.retrieved_at.isoformat(),
            "identities": {k: maybe_to_dict(v) for k, v in self.identities.items()},
            "identity_configs": {
                k: maybe_to_dict(v) for k, v in self.identity_configs.items()
            },
            "system_config": maybe_to_dict(self.system_config),
        }

    def validate(self) -> None:
        """Optional runtime validation hook. Attempts to call ConfigValidator.validate_* helpers if available.
        This method is best-effort and will raise only if validators themselves raise.
        """
        try:
            # import lazily to avoid circular imports
            from .config_validator import ConfigValidator  # type: ignore
        except Exception:
            return

        # Validate system config
        try:
            ConfigValidator.validate_system_config(self.system_config)
        except Exception:
            raise

        # Validate identity configs
        for cfg in self.identity_configs.values():
            ConfigValidator.validate_identity_config(cfg)

        # Validate identities
        for identity in self.identities.values():
            ConfigValidator.validate_identity(identity)
