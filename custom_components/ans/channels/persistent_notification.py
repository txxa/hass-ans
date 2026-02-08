"""Deliver notifications via Home Assistant persistent notifications."""

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import TemplateVarsType

from ..channels.adapter_lifecycle import AdapterType
from ..models import DeliveryResult, NotificationPayload, RecipientContactInfo
from .base import AdapterMetadata, ChannelRequirement, DeliveryAdapter

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
    is_system_channel = True  # Persistent notifications are system-wide

    # Metadata for auto-registration
    ADAPTER_METADATA = AdapterMetadata(
        adapter_type=AdapterType.STATIC,
        channel_prefix="notify.persistent_notification",
        integration="persistent_notification",
    )

    @classmethod
    def get_requirements(cls) -> ChannelRequirement:
        """Persistent notification requires no contact information.

        Returns
        -------
        ChannelRequirement
            Requirements dict with all flags set to False.

        """
        return ChannelRequirement(
            requires_email=False,
            requires_phone=False,
            requires_ha_user=False,
            description="No contact information required (system-wide notification)",
        )

    @classmethod
    def get_channel_label(cls, channel_id: str) -> str:
        """Generate label for persistent notification.

        Parameters
        ----------
        channel_id : str
            Channel identifier (always "notify.persistent_notification").

        Returns
        -------
        str
            Human-friendly label.

        """
        return "Persistent Notification"

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
            message = payload.message

            # Add metadata as notification data if present
            if payload.metadata:
                message += "\n\nMetadata:\n"
                for key, value in payload.metadata.items():
                    message += f"- {key}: {value}\n"

            # Build notification data
            data: TemplateVarsType = {
                "title": payload.title,
                "message": message,
                "notification_id": idempotency_key,
            }

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
