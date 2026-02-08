"""Deliver notifications via Signal."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..channels.adapter_lifecycle import AdapterType
from ..models import DeliveryResult, NotificationPayload, RecipientContactInfo
from .base import AdapterMetadata, ChannelRequirement, DeliveryAdapter

_LOGGER = logging.getLogger(__name__)


class SignalDeliveryAdapter(DeliveryAdapter):
    """Deliver notifications via Home Assistant Signal Messenger notification service.

    Supports all Signal Messenger integration features:
    - Text messages with optional styled formatting (bold, italic, strikethrough)
    - File attachments from local paths
    - Attachments from URLs with SSL verification control
    - Custom recipient targeting per message

    Attributes
    ----------
    channel : str
        The channel identifier "notify.signal".
    service_name : str
        The signal_messenger notify service name configured in Home Assistant.

    """

    channel = "notify.signal"
    is_system_channel = False  # Signal delivers to specific recipients

    # Metadata for auto-registration
    ADAPTER_METADATA = AdapterMetadata(
        adapter_type=AdapterType.DYNAMIC_SINGLE,
        channel_prefix="notify.signal",
        integration="signal_messenger",
    )

    @classmethod
    def get_requirements(cls) -> ChannelRequirement:
        """Signal requires phone number for delivery.

        Returns
        -------
        ChannelRequirement
            Requirements dict specifying phone number is needed.

        """
        return ChannelRequirement(
            requires_email=False,
            requires_phone=True,
            requires_ha_user=False,
            description="Requires phone number for Signal messaging",
        )

    @classmethod
    def get_channel_label(cls, channel_id: str) -> str:
        """Generate label for Signal channel.

        Parameters
        ----------
        channel_id : str
            Channel identifier ("notify.signal").

        Returns
        -------
        str
            Human-friendly label.

        """
        return "Signal Messenger"

    def __init__(self, *, hass: HomeAssistant, service_name: str = "signal") -> None:
        """Initialize Signal adapter with Home Assistant service.

        Parameters
        ----------
        hass : HomeAssistant
            Home Assistant instance for service calls.
        service_name : str, optional
            Signal messenger notify service name, by default "signal".
            This corresponds to the service configured in notify: section.

        """
        self._hass = hass
        self.service_name = service_name

    async def deliver(
        self,
        *,
        payload: NotificationPayload,
        contact_info: RecipientContactInfo,
        idempotency_key: str,
    ) -> DeliveryResult:
        """Deliver notification via Signal messenger notify service.

        Supports all Signal Messenger features through metadata:
        - text_mode: "normal" or "styled" (enables *italic*, **bold**, ~strikethrough~)
        - attachments: list of file paths
        - urls: list of URLs for remote attachments
        - verify_ssl: boolean for SSL verification (default: true)

        Parameters
        ----------
        payload : NotificationPayload
            The notification content to send.
        contact_info : RecipientContactInfo
            Recipient contact information including phone number.
        idempotency_key : str
            Unique key for idempotent retries.

        Returns
        -------
        DeliveryResult
            Result of delivery attempt (success, transient, or permanent failure).

        """
        # Signal adapter uses phone number as the target
        if not contact_info.phone_number:
            return self.permanent_failure(
                error="No phone number configured for Signal delivery"
            )

        try:
            # Build base notification message
            message = payload.message

            # Add title if present (Signal doesn't have separate title field)
            if payload.title:
                message = f"{payload.title}\n\n{payload.message}"

            # Build service data structure
            service_data: dict[str, Any] = {
                "message": message,
                "target": [contact_info.phone_number],
            }

            # Process metadata for Signal-specific features
            data_payload: dict[str, Any] = {}

            if payload.metadata:
                # Text mode: "normal" or "styled" for formatting
                text_mode = payload.metadata.get("text_mode", "normal")
                if text_mode in ("normal", "styled"):
                    data_payload["text_mode"] = text_mode
                else:
                    _LOGGER.warning(
                        "Invalid text_mode '%s', using 'normal'. Valid values: 'normal', 'styled'",
                        text_mode,
                    )
                    data_payload["text_mode"] = "normal"

                # File attachments: list of local file paths
                if "attachments" in payload.metadata:
                    attachments = payload.metadata["attachments"]
                    if isinstance(attachments, list):
                        data_payload["attachments"] = attachments
                    else:
                        _LOGGER.warning(
                            "attachments must be a list of file paths, got: %s",
                            type(attachments).__name__,
                        )

                # URL attachments: list of URLs for remote files
                if "urls" in payload.metadata:
                    urls = payload.metadata["urls"]
                    if isinstance(urls, list):
                        data_payload["urls"] = urls
                    else:
                        _LOGGER.warning(
                            "urls must be a list of URLs, got: %s",
                            type(urls).__name__,
                        )

                # SSL verification for URL attachments
                if "verify_ssl" in payload.metadata:
                    verify_ssl = payload.metadata["verify_ssl"]
                    if isinstance(verify_ssl, bool):
                        data_payload["verify_ssl"] = verify_ssl
                    else:
                        _LOGGER.warning(
                            "verify_ssl must be boolean, got: %s",
                            type(verify_ssl).__name__,
                        )
            else:
                # Default text mode if no metadata
                data_payload["text_mode"] = "normal"

            # Add data payload to service data if not empty
            if data_payload:
                service_data["data"] = data_payload

            # Call signal_messenger notify service
            await self._hass.services.async_call(
                domain="notify",
                service=self.service_name,
                service_data=service_data,
                blocking=True,
            )

            _LOGGER.debug(
                "Sent Signal notification to '%s' via service '%s' (text_mode=%s, attachments=%d, urls=%d) with key '%s'",
                contact_info.phone_number,
                self.service_name,
                data_payload.get("text_mode", "normal"),
                len(data_payload.get("attachments", [])),
                len(data_payload.get("urls", [])),
                idempotency_key,
            )
            return self.success(remote_id=idempotency_key)

        except HomeAssistantError as exc:
            # Service not found or other HA errors
            error_msg = str(exc).lower()
            if "not found" in error_msg or "does not exist" in error_msg:
                return self.permanent_failure(
                    error=f"Signal service 'notify.{self.service_name}' not found: {exc}"
                )
            # Other HA errors could be transient
            return self.transient_failure(error=f"Signal service error: {exc}")

        except Exception as exc:
            _LOGGER.exception("Unexpected Signal adapter failure")
            return self.permanent_failure(error=f"signal unexpected error: {exc}")
