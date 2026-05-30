"""Deliver notifications via Home Assistant Mobile App."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)

from ..models import DeliveryResult, NotificationPayload, RecipientContactInfo
from ..helper import media_label
from .base import (
    AdapterFactory,
    AdapterMetadata,
    AdapterType,
    ChannelRequirement,
    DeliveryAdapter,
    DeliveryOptions,
)

if TYPE_CHECKING:
    from ..delivery.factory import AdapterDeps

_LOGGER = logging.getLogger(__name__)


class MobileAppDeliveryAdapter(DeliveryAdapter):
    """Deliver notifications via Home Assistant Mobile App push notifications.

    Each instance is created for a specific device, with the full channel name
    (e.g., "notify.mobile_app_sm_s911b") set during initialization.

    Attributes
    ----------
    channel : str
        The full channel identifier (e.g., "notify.mobile_app_sm_s911b").
    device_id : str
        The device identifier extracted from channel (e.g., "sm_s911b").

    """

    # Metadata for auto-registration
    ADAPTER_METADATA: ClassVar[AdapterMetadata] = AdapterMetadata(
        adapter_type=AdapterType.DYNAMIC_MULTI,
        channel_prefix="notify.mobile_app",
        integration="mobile_app",
    )
    # Full channel prefix including separator, derived from metadata.
    # Eliminates hardcoded "notify.mobile_app_" literals throughout the class.
    _CHANNEL_PREFIX: ClassVar[str] = ADAPTER_METADATA.channel_prefix + "_"

    @classmethod
    def get_metadata(cls) -> AdapterMetadata:
        """Return adapter metadata."""
        return cls.ADAPTER_METADATA

    @classmethod
    def matches_channel(cls, channel_id: str) -> bool:
        """Return True if channel_id belongs to this adapter."""
        return channel_id.startswith(cls._CHANNEL_PREFIX)

    @classmethod
    def extract_variant(cls, channel_id: str) -> str | None:
        """Return the device_id portion of a mobile_app channel_id."""
        if cls.matches_channel(channel_id):
            return channel_id[len(cls._CHANNEL_PREFIX) :]
        return None

    @classmethod
    def get_requirements(cls) -> ChannelRequirement:
        """Mobile app requires Home Assistant user linkage.

        Returns
        -------
        ChannelRequirement
            Requirements dict specifying HA user is needed.

        """
        return ChannelRequirement(
            requires_email=False,
            requires_phone=False,
            requires_ha_user=True,
            description="Requires Home Assistant user for mobile app push notifications",
        )

    @classmethod
    def get_channel_label(cls, channel_id: str) -> str:
        """Generate label showing device name.

        Parameters
        ----------
        channel_id : str
            Full channel identifier (e.g., "notify.mobile_app_sm_s911b").

        Returns
        -------
        str
            Label with device name (e.g., "Mobile App (SM S911B)").

        Examples
        --------
        >>> MobileAppDeliveryAdapter.get_channel_label("notify.mobile_app_sm_s911b")
        "Mobile App (SM S911B)"
        >>> MobileAppDeliveryAdapter.get_channel_label("notify.mobile_app_iphone")
        "Mobile App (iPhone)"

        """
        # Extract device ID from channel_id
        device_id = channel_id[len(cls._CHANNEL_PREFIX) :]
        # Format device name nicely
        device_name = device_id.replace("_", " ").title()
        return f"Mobile App ({device_name})"

    @classmethod
    def create_factory(
        cls,
        factory_fn: Callable[[HomeAssistant, str | None], DeliveryAdapter]
        | None = None,
        cleanup_fn: Callable[[DeliveryAdapter], None] | None = None,
        deps: AdapterDeps | None = None,  # noqa: ARG003
    ) -> AdapterFactory:
        """Create an AdapterFactory that forwards the device_id to the constructor.

        Overrides the base implementation to supply a factory function that
        passes the channel variant (``device_id``) to the constructor so that
        each mobile-app device gets its own correctly-wired adapter instance.
        """
        if factory_fn is None:

            def _device_factory(
                hass: HomeAssistant, device_id: str | None
            ) -> MobileAppDeliveryAdapter:
                if not device_id:
                    raise ValueError(
                        "device_id is required for MobileAppDeliveryAdapter"
                    )
                return cls(hass=hass, device_id=device_id)

            factory_fn = _device_factory
        return super().create_factory(factory_fn=factory_fn, cleanup_fn=cleanup_fn)

    def __init__(self, *, hass: HomeAssistant, device_id: str) -> None:
        """Initialize mobile app adapter for a specific device.

        Parameters
        ----------
        hass : HomeAssistant
            Home Assistant instance for service calls.
        device_id : str
            Device identifier (e.g., "sm_s911b" from "notify.mobile_app_sm_s911b").

        """
        if not device_id:
            raise ValueError("device_id is required for MobileAppDeliveryAdapter")
        self._hass = hass
        self.device_id = device_id
        # Full channel name derived from class-level prefix
        self._channel = f"{self._CHANNEL_PREFIX}{device_id}"

    @property
    def channel(self) -> str:  # type: ignore[override]  # mypy false positive: abstract property
        """Return the channel identifier for this device."""
        return self._channel

    @property
    def service_name(self) -> str:
        """Return the notify service name for this device."""
        return self.channel.removeprefix("notify.")

    async def deliver(
        self,
        *,
        payload: NotificationPayload,
        contact_info: RecipientContactInfo,
        idempotency_key: str,
        job_id: str,
        options: DeliveryOptions | None = None,
    ) -> DeliveryResult:
        """Deliver notification via mobile_app notify service.

        Parameters
        ----------
        payload : NotificationPayload
            The notification content to send.
        contact_info : RecipientContactInfo
            Recipient contact information including mobile_device_id.
        idempotency_key : str
            Unique key for idempotent retries.
        job_id : str
            Job identifier for cross-layer log correlation.
        options : DeliveryOptions | None
            Per-delivery options (not used by the mobile app adapter).

        Returns
        -------
        DeliveryResult
            Result of delivery attempt (success or failure).

        """
        # Build the service name from device_id
        service_name = self.service_name

        try:
            # Build notification data
            service_data: dict[str, Any] = {
                "title": payload.title,
                "message": payload.message,
            }

            # Build the nested data dict starting from empty.
            # Explicit rich-content fields are added first; channel_data keys
            # override them if present; idempotency_key is always forced last.
            data: dict[str, Any] = {}

            # Rich-content fields
            if payload.image and payload.image.startswith(("http://", "https://")):
                if media_label(payload.image) == payload.image:
                    _LOGGER.warning(
                        "Skipping image URL with no filename segment for "
                        "notification_id=%s: %s",
                        payload.notification_id,
                        payload.image,
                    )
                else:
                    data["image"] = payload.image

            # Tap action URL — set on both iOS (url) and Android (clickAction).
            # Both keys accept the same values: full URLs, relative HA paths,
            # and companion-app schemes such as entityId:<entity_id>.
            _tap_url: str | None = None
            if payload.link:
                _tap_url = payload.link
            elif _entity := payload.context.get("entity"):
                _tap_url = f"entityId:{_entity}"
            if _tap_url:
                data["url"] = _tap_url  # iOS / macOS
                data["clickAction"] = _tap_url  # Android

            # channel_data flat merge — overrides image/url set above
            data.update(payload.channel_data)

            # Set tag for acknowledgement tracking (NH-3).
            # channel_data may have supplied 'tag'; fall back to the ANS UUID.
            if "tag" not in data:
                data["tag"] = str(payload.notification_id)

            # Forward action buttons if the notification defines any.
            # Non-mobile-app adapters silently ignore payload.actions.
            if payload.actions:
                data["actions"] = list(payload.actions)

            # idempotency_key is always set last — cannot be overridden
            data["idempotency_key"] = idempotency_key

            service_data["data"] = data

            # Call mobile_app notify service
            await self._hass.services.async_call(
                domain="notify",
                service=service_name,
                service_data=service_data,
                blocking=True,
            )

            _LOGGER.debug(
                "Mobile app notification sent: job_id=%s device=%s notification_id=%s key=%s",
                job_id,
                self.device_id,
                payload.notification_id,
                idempotency_key,
            )
            return self.success(remote_id=idempotency_key)

        except ServiceNotFound as exc:
            _LOGGER.warning(
                "Mobile app service 'notify.%s' not found (permanent): "
                "job_id=%s notification_id=%s key=%s: %s",
                self.device_id,
                job_id,
                payload.notification_id,
                idempotency_key,
                exc,
            )
            return self.permanent_failure(
                error=f"Mobile app service '{service_name}' not found: {exc}"
            )
        except ServiceValidationError as exc:
            _LOGGER.warning(
                "Mobile app service 'notify.%s' validation error (permanent): "
                "job_id=%s notification_id=%s key=%s: %s",
                self.device_id,
                job_id,
                payload.notification_id,
                idempotency_key,
                exc,
            )
            return self.permanent_failure(
                error=f"Mobile app service '{service_name}' validation error: {exc}"
            )
        except HomeAssistantError as exc:
            _LOGGER.warning(
                "Mobile app service error for device '%s': job_id=%s notification_id=%s key=%s: %s",
                self.device_id,
                job_id,
                payload.notification_id,
                idempotency_key,
                exc,
            )
            return self.transient_failure(error=f"Mobile app service error: {exc}")

        except Exception as exc:
            _LOGGER.exception(
                "Unexpected mobile app adapter failure: notification_id=%s key=%s",
                payload.notification_id,
                idempotency_key,
            )
            # Unexpected errors are treated as transient: they are more likely
            # caused by runtime conditions (OOM, event loop issues) than by
            # permanent misconfiguration.
            return self.transient_failure(error=f"mobile_app unexpected error: {exc}")
