"""define the delivery adapter contract and shared error semantics."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, ClassVar, TypedDict

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from ..channels.adapter_lifecycle import AdapterFactory, AdapterType

from ..models import (
    DeliveryResult,
    DeliveryStatus,
    NotificationPayload,
    RecipientContactInfo,
)


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


class DeliveryAdapter(ABC):
    """Base class for all channel delivery adapters.

    One adapter = one physical channel implementation.

    Subclasses should define ADAPTER_METADATA as a class variable
    to enable automatic factory registration.
    """

    channel: str  # logical channel name, e.g. "signal", "email"
    is_system_channel: bool = (
        False  # True for system-wide channels like persistent_notification
    )

    # Optional: Subclasses can define this for auto-registration
    ADAPTER_METADATA: ClassVar[AdapterMetadata | None] = None

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
        # Remove domain prefix if present
        label = channel_id.replace("notify.", "").replace("tts.", "")
        # Basic cleanup: underscores to spaces, title case
        return label.replace("_", " ").title()

    @abstractmethod
    async def deliver(
        self,
        *,
        payload: NotificationPayload,
        contact_info: RecipientContactInfo,
        idempotency_key: str,
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
    ) -> AdapterFactory:
        """Create an AdapterFactory for this adapter class.

        Parameters
        ----------
        factory_fn : Callable, optional
            Custom factory function (defaults to standard constructor).
        cleanup_fn : Callable, optional
            Optional cleanup function.

        Returns
        -------
        AdapterFactory
            AdapterFactory configured for this adapter.

        Raises
        ------
        ValueError
            If ADAPTER_METADATA is not defined.

        """
        # Import here to avoid circular dependency between base and adapter_lifecycle
        from ..channels.adapter_lifecycle import AdapterFactory  # noqa: PLC0415

        if cls.ADAPTER_METADATA is None:
            raise ValueError(
                f"{cls.__name__} must define ADAPTER_METADATA for auto-registration"
            )

        # Default factory: call constructor with hass parameter
        if factory_fn is None:

            def _default_factory(hass: HomeAssistant, _device_id: str | None):
                """Create adapter instance with hass parameter."""
                return cls(hass=hass)  # type: ignore[call-arg]

            factory_fn = _default_factory

        return AdapterFactory(
            adapter_type=cls.ADAPTER_METADATA.adapter_type,
            channel_prefix=cls.ADAPTER_METADATA.channel_prefix,
            factory_fn=factory_fn,
            cleanup_fn=cleanup_fn,
        )
