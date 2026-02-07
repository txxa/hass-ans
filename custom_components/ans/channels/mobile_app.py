"""Deliver notifications via Home Assistant Mobile App."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..channels.adapter_lifecycle import AdapterType
from ..models import DeliveryResult, NotificationPayload, RecipientContactInfo
from .base import AdapterMetadata, DeliveryAdapter

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

    is_system_channel = False  # Mobile app delivers to specific devices

    # Metadata for auto-registration
    ADAPTER_METADATA = AdapterMetadata(
        adapter_type=AdapterType.DYNAMIC_MULTI,
        channel_prefix="notify.mobile_app",
        integration="mobile_app",
    )

    def __init__(self, *, hass: HomeAssistant, device_id: str) -> None:
        """Initialize mobile app adapter for a specific device.

        Parameters
        ----------
        hass : HomeAssistant
            Home Assistant instance for service calls.
        device_id : str
            Device identifier (e.g., "sm_s911b" from "notify.mobile_app_sm_s911b").

        """
        self._hass = hass
        self.device_id = device_id
        # Set the full channel name for this specific device
        self.channel = f"notify.mobile_app_{device_id}"

    async def deliver(
        self,
        *,
        payload: NotificationPayload,
        contact_info: RecipientContactInfo,
        idempotency_key: str,
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

        Returns
        -------
        DeliveryResult
            Result of delivery attempt (success or failure).

        """
        # Build the service name from device_id
        service_name = f"mobile_app_{self.device_id}"

        try:
            # Build notification data
            service_data: dict[str, Any] = {
                "title": payload.title,
                "message": payload.message,
            }

            # Add metadata as data payload if present
            if payload.metadata:
                service_data["data"] = payload.metadata.copy()
                # Add idempotency key to data for tracking
                service_data["data"]["idempotency_key"] = idempotency_key
            else:
                service_data["data"] = {"idempotency_key": idempotency_key}

            # Call mobile_app notify service
            await self._hass.services.async_call(
                domain="notify",
                service=service_name,
                service_data=service_data,
                blocking=True,
            )

            _LOGGER.debug(
                "Sent mobile app notification to device '%s' with key '%s'",
                self.device_id,
                idempotency_key,
            )
            return self.success(remote_id=idempotency_key)

        except HomeAssistantError as exc:
            # Service not found or other HA errors
            error_msg = str(exc).lower()
            if "not found" in error_msg or "does not exist" in error_msg:
                return self.permanent_failure(
                    error=f"Mobile app service '{service_name}' not found: {exc}"
                )
            # Other HA errors could be transient
            return self.transient_failure(error=f"Mobile app service error: {exc}")

        except Exception as exc:
            _LOGGER.exception("Unexpected mobile app adapter failure")
            return self.permanent_failure(error=f"mobile_app unexpected error: {exc}")
