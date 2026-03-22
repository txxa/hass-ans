"""define the delivery adapter contract and shared error semantics."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from ..delivery.factory import AdapterDeps

from ..models import (
    ChannelInfo,
    DeliveryResult,
    DeliveryStatus,
    NotificationPayload,
    RecipientContactInfo,
)
from ..models.recipient import TTSSettings


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


class ChannelStatus(str, Enum):
    """Status of a channel in the ChannelManager.

    Values
    ------
    DETECTED : str
        Channel is visible in HA but not in enabled_channels — no adapter.
    ACTIVE : str
        Channel is detected, enabled, and has a live adapter.
    INACTIVE : str
        Channel is in enabled_channels but has no registered factory.
    STALE : str
        Channel was previously detected but is no longer visible in HA — adapter destroyed.

    """

    DETECTED = "detected"
    ACTIVE = "active"
    INACTIVE = "inactive"
    STALE = "stale"


@dataclass(frozen=True)
class DeliveryOptions:
    """Channel-agnostic per-delivery options base class.

    Co-located with the deliver() ABC. Subclass to add adapter-specific fields.
    """


@dataclass(frozen=True)
class TTSDeliveryOptions(DeliveryOptions):
    """TTS-specific delivery options."""

    tts_settings: TTSSettings | None = None


@dataclass(frozen=True)
class ChannelRecord:
    """Unified record holding channel metadata and its live adapter.

    Attributes
    ----------
    info : ChannelInfo
        Frozen channel metadata (id, label, scope, integration).
    adapter : DeliveryAdapter | None
        Live adapter instance, or None when channel is not ACTIVE.
    status : ChannelStatus
        Current lifecycle status of the channel.

    """

    info: ChannelInfo
    adapter: DeliveryAdapter | None
    status: ChannelStatus


class ChannelRequirement(TypedDict, total=False):
    """Channel contact information requirements.

    Attributes
    ----------
    requires_email : bool
        Whether this channel requires an email address
    requires_phone : bool
        Whether this channel requires a phone number
    requires_ha_user : bool
        Whether this channel requires linkage to a Home Assistant user
    description : str
        Human-readable description of the requirement

    """

    requires_email: bool
    requires_phone: bool
    requires_ha_user: bool
    description: str


class AdapterFailureType(str, Enum):
    """Classification of delivery adapter failures.

    Values
    ------
    TRANSIENT : str
        Temporary failure, safe to retry (e.g., network timeout).
    PERMANENT : str
        Permanent failure, do not retry (e.g., invalid recipient).

    """

    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"


@dataclass
class AdapterMetadata:
    """Metadata for adapter registration.

    Attributes
    ----------
    adapter_type : AdapterType
        Lifecycle type (STATIC, DYNAMIC_SINGLE, DYNAMIC_MULTI).
    channel_prefix : str
        Channel identifier or prefix.
    integration : str | None
        Integration name (for logging/diagnostics).

    """

    adapter_type: AdapterType
    channel_prefix: str
    integration: str | None = None


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
    adapter_class : type[DeliveryAdapter]
        The concrete adapter class that owns this factory.  Used by the
        lifecycle manager to call ``matches_channel()`` and
        ``extract_variant()`` without hardcoding prefix strings.
    cleanup_fn : Callable | None
        Optional cleanup function when adapter is unregistered.

    """

    adapter_type: AdapterType
    channel_prefix: str
    factory_fn: Callable[[HomeAssistant, str | None], DeliveryAdapter]
    adapter_class: type[DeliveryAdapter]
    cleanup_fn: Callable[[DeliveryAdapter], None | Awaitable[None]] | None = None


class DeliveryAdapter(ABC):
    """Base class for all channel delivery adapters.

    One adapter = one physical channel implementation.

    Subclasses must define ADAPTER_METADATA as a class variable and implement
    the abstract classmethods ``get_metadata``, ``matches_channel``, and
    ``extract_variant`` for self-describing channel ownership.
    """

    @classmethod
    @abstractmethod
    def get_metadata(cls) -> AdapterMetadata:
        """Return the adapter's metadata (type, prefix, integration, separator).

        Must be implemented on every concrete adapter class.
        """
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def matches_channel(cls, channel_id: str) -> bool:
        """Return True if *channel_id* belongs to this adapter.

        Used by the lifecycle manager to assign channels to adapters without
        relying on external prefix-string knowledge.
        """
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def extract_variant(cls, channel_id: str) -> str | None:
        """Extract the variant portion from *channel_id*.

        Returns ``None`` for single-instance adapters (STATIC, DYNAMIC_SINGLE).
        For multi-instance adapters (DYNAMIC_MULTI), returns the suffix that
        distinguishes one instance from another (e.g. ``"sm_s911b"`` from
        ``"notify.mobile_app_sm_s911b"``).
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def channel(self) -> str:
        """Logical channel identifier (e.g. "notify.signal", "media_player.living_room")."""

    @classmethod
    @abstractmethod
    def get_requirements(cls) -> ChannelRequirement:
        """Return contact information requirements for this channel.

        Returns
        -------
        ChannelRequirement
            Dictionary specifying what contact information is needed for delivery.
            If a channel requires no contact info, return an empty dict or set
            all requirement flags to False.

        """
        raise NotImplementedError

    @classmethod
    def get_channel_label(cls, channel_id: str) -> str:
        """Generate human-friendly label for a channel.

        Parameters
        ----------
        channel_id : str
            Full channel identifier (e.g., "notify.mobile_app_sm_s911b").

        Returns
        -------
        str
            Human-friendly label for display in UI.

        Notes
        -----
        Subclasses can override to provide adapter-specific formatting.
        Default implementation does basic string cleanup.

        """
        # Remove notify. domain prefix if present (no other domain prefix is used)
        label = channel_id.replace("notify.", "")
        # Basic cleanup: underscores to spaces, title case
        return label.replace("_", " ").title()

    @abstractmethod
    async def deliver(
        self,
        *,
        payload: NotificationPayload,
        contact_info: RecipientContactInfo,
        idempotency_key: str,
        options: DeliveryOptions | None = None,
    ) -> DeliveryResult:
        """Perform exactly one delivery attempt.

        Must:
        - be async
        - respect idempotency_key if supported by provider
        - never raise for expected failures
        """
        raise NotImplementedError

    # ------------------------------------------------------------
    # Helper factories (optional, but recommended)
    # ------------------------------------------------------------

    def success(self, *, remote_id: str | None = None) -> DeliveryResult:
        """Create a successful delivery result.

        Parameters
        ----------
        remote_id : str, optional
            Remote service message ID if available.

        Returns
        -------
        DeliveryResult
            Success result object.

        """
        return DeliveryResult(status=DeliveryStatus.SUCCESS, remote_id=remote_id)

    def transient_failure(self, *, error: str) -> DeliveryResult:
        """Create a transient failure result for retrying.

        Parameters
        ----------
        error : str
            Error description for logging.

        Returns
        -------
        DeliveryResult
            Transient failure result object.

        """
        return DeliveryResult(status=DeliveryStatus.TRANSIENT_FAIL, error=error)

    def permanent_failure(self, *, error: str) -> DeliveryResult:
        """Create a permanent failure result.

        Parameters
        ----------
        error : str
            Error description for logging.

        Returns
        -------
        DeliveryResult
            Permanent failure result object.

        """
        return DeliveryResult(status=DeliveryStatus.PERMANENT_FAIL, error=error)

    @classmethod
    def create_factory(
        cls,
        factory_fn: Callable[[HomeAssistant, str | None], DeliveryAdapter]
        | None = None,
        cleanup_fn: Callable[[DeliveryAdapter], None] | None = None,
        deps: AdapterDeps | None = None,  # noqa: ARG003
    ) -> AdapterFactory:
        """Create an AdapterFactory for this adapter class.

        Parameters
        ----------
        factory_fn : Callable, optional
            Custom factory function (defaults to standard constructor).
        cleanup_fn : Callable, optional
            Optional cleanup function.
        deps : AdapterDeps | None, optional
            Runtime dependencies (used by adapters that require extra injection,
            e.g. TTSMediaPlayerAdapter).  Ignored by the base implementation.

        Returns
        -------
        AdapterFactory
            AdapterFactory configured for this adapter.

        """
        meta = cls.get_metadata()

        # Default factory: call constructor with hass parameter
        if factory_fn is None:
            # DYNAMIC_MULTI adapters MUST supply an explicit factory_fn.  The
            # default factory ignores the variant/device_id argument, which
            # would produce a broken instance with no channel variant.  Fail at
            # registration time rather than silently creating bad adapters.
            if meta.adapter_type == AdapterType.DYNAMIC_MULTI:
                raise TypeError(
                    f"{cls.__name__}.create_factory() requires an explicit "
                    "factory_fn for DYNAMIC_MULTI adapters — the default "
                    "factory ignores the variant/device_id argument and would "
                    "produce a broken instance."
                )

            def _default_factory(hass: HomeAssistant, _device_id: str | None):
                """Create adapter instance with hass parameter."""
                return cls(hass=hass)  # type: ignore[call-arg]

            factory_fn = _default_factory

        return AdapterFactory(
            adapter_type=meta.adapter_type,
            channel_prefix=meta.channel_prefix,
            factory_fn=factory_fn,
            adapter_class=cls,
            cleanup_fn=cleanup_fn,
        )
