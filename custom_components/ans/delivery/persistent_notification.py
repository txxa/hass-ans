"""Deliver notifications via Home Assistant persistent notifications."""

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import TemplateVarsType

from ..models import DeliveryResult, NotificationPayload, RecipientContactInfo
from .base import DeliveryAdapter

_LOGGER = logging.getLogger(__name__)


class PersistentNotificationAdapter(DeliveryAdapter):
    """Deliver notifications via Home Assistant persistent notifications.

    Persistent notifications are displayed in the Home Assistant frontend
    and stored in the state machine, making them persistent across restarts.

    Attributes
    ----------
    channel : str
        The channel identifier "persistent_notification".

    """

    channel = "notify.persistent_notification"

    def __init__(self, *, hass: HomeAssistant) -> None:
        """Initialize persistent notification adapter.

        Parameters
        ----------
        hass : HomeAssistant
            Home Assistant instance for service calls.

        """
        self._hass = hass

    async def deliver(
        self,
        *,
        payload: NotificationPayload,
        contact_info: RecipientContactInfo,
        idempotency_key: str,
    ) -> DeliveryResult:
        """Deliver notification via persistent notification service.

        Parameters
        ----------
        payload : NotificationPayload
            The notification content to send.
        contact_info : RecipientContactInfo
            Recipient contact information (not used for persistent notifications).
        idempotency_key : str
            Unique key for idempotent retries.

        Returns
        -------
        DeliveryResult
            Result of delivery attempt (success or failure).

        """
        try:
            # Build notification data
            data: TemplateVarsType = {
                "title": payload.title,
                "message": payload.message,
                "notification_id": idempotency_key,
            }

            # Add metadata as notification data if present
            if payload.metadata:
                data["data"] = payload.metadata

            # Call persistent_notification.create service
            await self._hass.services.async_call(
                domain="persistent_notification",
                service="create",
                service_data=data,
            )

            _LOGGER.debug(
                "Created persistent notification with ID '%s'", idempotency_key
            )
            return self.success(remote_id=idempotency_key)

        except Exception as exc:
            _LOGGER.exception("Failed to create persistent notification")
            return self.permanent_failure(
                error=f"persistent_notification service error: {exc}"
            )
