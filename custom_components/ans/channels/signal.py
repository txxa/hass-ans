"""Deliver notifications via Signal."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)

from ..models import DeliveryResult, NotificationPayload, RecipientContactInfo
from .base import (
    AdapterMetadata,
    AdapterType,
    ChannelRequirement,
    DeliveryAdapter,
    DeliveryOptions,
)

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)


def _mask_phone(number: str) -> str:
    """Return a masked phone number showing only the last 4 digits.

    Prevents raw PII from appearing in log files and remote log aggregators.
    Example: "+49123456789" → "****6789".
    """
    if len(number) >= 4:
        return f"****{number[-4:]}"
    return "****"


class SignalDeliveryAdapter(DeliveryAdapter):
    """Deliver notifications via Home Assistant Signal Messenger notification service.

    Supports all Signal Messenger integration features:
    - Text messages with optional styled formatting (bold, italic, strikethrough)
    - Automatic bold title highlighting when a title is provided
    - File attachments from local paths
    - Attachments from URLs with SSL verification control
    - Custom recipient targeting per message

    When a notification has a title, the adapter automatically selects
    ``text_mode="styled"`` (unless overridden via metadata) and wraps the
    title in ``**...**`` so it renders as bold in the Signal client.
    Pass ``text_mode: "normal"`` in ``metadata`` to disable this behaviour.

    Attributes
    ----------
    channel : str
        The channel identifier "notify.signal".
    service_name : str
        The signal_messenger notify service name configured in Home Assistant.

    """

    ADAPTER_METADATA: ClassVar[AdapterMetadata] = AdapterMetadata(
        adapter_type=AdapterType.DYNAMIC_SINGLE,
        channel_prefix="notify.signal",
        integration="signal_messenger",
    )
    # Channel identifier derived from metadata — no separator appended for
    # DYNAMIC_SINGLE adapters since the prefix IS the full channel ID.
    _CHANNEL_ID: ClassVar[str] = ADAPTER_METADATA.channel_prefix

    @classmethod
    def get_metadata(cls) -> AdapterMetadata:
        """Return adapter metadata."""
        return cls.ADAPTER_METADATA

    @classmethod
    def matches_channel(cls, channel_id: str) -> bool:
        """Return True if channel_id belongs to this adapter."""
        return channel_id == cls._CHANNEL_ID

    @classmethod
    def extract_variant(cls, channel_id: str) -> str | None:  # noqa: ARG003
        """Return None — Signal has no variant."""
        return None

    @property
    def channel(self) -> str:  # type: ignore[override]  # mypy false positive: abstract property
        """Return the channel identifier."""
        return self._CHANNEL_ID

    @property
    def service_name(self) -> str:
        """Return the notify service name for Signal, derived from channel ID."""
        return self.channel.removeprefix("notify.")

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

    def __init__(self, *, hass: HomeAssistant) -> None:
        """Initialize Signal adapter with Home Assistant service.

        Parameters
        ----------
        hass : HomeAssistant
            Home Assistant instance for service calls.

        """
        self._hass = hass

    @staticmethod
    def _build_service_data(
        payload: NotificationPayload,
        contact_info: RecipientContactInfo,
    ) -> dict[str, Any]:
        """Build Signal service_data from payload and contact information.

        Synchronous. Contains all text-mode detection/validation, metadata
        inspection, and message construction logic so that ``deliver()``
        only performs the async service call.

        Parameters
        ----------
        payload : NotificationPayload
            Notification content (title, message, metadata).
        contact_info : RecipientContactInfo
            Recipient contact info (phone number used as Signal target).

        Returns
        -------
        dict[str, Any]
            Fully assembled service_data for the notify.signal service call.

        """
        data_payload: dict[str, Any] = {}

        if payload.metadata:
            # Text mode: "normal" or "styled" for formatting.
            # When a title is present and no explicit text_mode is set,
            # default to "styled" so the title can be visually highlighted.
            explicit_text_mode = payload.metadata.get("text_mode")
            if explicit_text_mode is None:
                text_mode = "styled" if payload.title else "normal"
            elif explicit_text_mode in ("normal", "styled"):
                text_mode = explicit_text_mode
            else:
                _LOGGER.warning(
                    "Invalid text_mode '%s' for notification_id=%s; using 'normal'. "
                    "Valid values: 'normal', 'styled'",
                    explicit_text_mode,
                    payload.notification_id,
                )
                text_mode = "normal"
            data_payload["text_mode"] = text_mode

            # File attachments: list of local file paths
            if "attachments" in payload.metadata:
                attachments = payload.metadata["attachments"]
                if isinstance(attachments, list):
                    data_payload["attachments"] = attachments
                else:
                    _LOGGER.warning(
                        "attachments must be a list of file paths for notification_id=%s, got: %s",
                        payload.notification_id,
                        type(attachments).__name__,
                    )

            # URL attachments: list of URLs for remote files
            if "urls" in payload.metadata:
                urls = payload.metadata["urls"]
                if isinstance(urls, list):
                    data_payload["urls"] = urls
                else:
                    _LOGGER.warning(
                        "urls must be a list of URLs for notification_id=%s, got: %s",
                        payload.notification_id,
                        type(urls).__name__,
                    )

            # SSL verification for URL attachments
            if "verify_ssl" in payload.metadata:
                verify_ssl = payload.metadata["verify_ssl"]
                if isinstance(verify_ssl, bool):
                    data_payload["verify_ssl"] = verify_ssl
                else:
                    _LOGGER.warning(
                        "verify_ssl must be boolean for notification_id=%s, got: %s",
                        payload.notification_id,
                        type(verify_ssl).__name__,
                    )
        else:
            # No metadata: auto-upgrade to styled when a title is present so
            # it is visually highlighted; fall back to plain for body-only messages.
            text_mode = "styled" if payload.title else "normal"
            data_payload["text_mode"] = text_mode

        # Build message string.  Signal has no native title field, so the
        # title is prepended to the body.  In styled mode the title is
        # wrapped in **...** to render as bold in the Signal client.
        if payload.title:
            if text_mode == "styled":
                message = f"**{payload.title}**\n\n{payload.message}"
            else:
                message = f"{payload.title}\n\n{payload.message}"
        else:
            message = payload.message

        # Build service data structure
        service_data: dict[str, Any] = {
            "message": message,
            "target": [contact_info.phone_number],
        }

        # Add data payload to service data if not empty
        if data_payload:
            service_data["data"] = data_payload

        return service_data

    async def deliver(
        self,
        *,
        payload: NotificationPayload,
        contact_info: RecipientContactInfo,
        idempotency_key: str,
        options: DeliveryOptions | None = None,
    ) -> DeliveryResult:
        """Deliver notification via Signal messenger notify service.

        Supports all Signal Messenger features through metadata:
        - text_mode: "normal" or "styled" (enables *italic*, **bold**, ~strikethrough~).
          Defaults to "styled" when a title is present so the title is highlighted
          in bold automatically.  Set explicitly to "normal" to opt out.
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
        options : DeliveryOptions | None
            Per-delivery options (not used by the Signal adapter).

        Returns
        -------
        DeliveryResult
            Result of delivery attempt (success, transient, or permanent failure).

        """
        # Signal adapter uses phone number as the target
        if not contact_info.phone_number:
            _LOGGER.warning(
                "Signal delivery skipped: no phone number configured "
                "for notification_id=%s key=%s",
                payload.notification_id,
                idempotency_key,
            )
            return self.permanent_failure(
                error="No phone number configured for Signal delivery"
            )

        try:
            service_data = self._build_service_data(payload, contact_info)

            # Call signal_messenger notify service
            await self._hass.services.async_call(
                domain="notify",
                service=self.service_name,
                service_data=service_data,
                blocking=True,
            )

            _LOGGER.debug(
                "Signal notification sent: phone=%s notification_id=%s service=%s "
                "text_mode=%s attachments=%d urls=%d key=%s",
                _mask_phone(contact_info.phone_number),
                payload.notification_id,
                self.service_name,
                service_data.get("data", {}).get("text_mode", "normal"),
                len(service_data.get("data", {}).get("attachments", [])),
                len(service_data.get("data", {}).get("urls", [])),
                idempotency_key,
            )
            return self.success(remote_id=idempotency_key)

        except ServiceNotFound as exc:
            _LOGGER.warning(
                "Signal service 'notify.%s' not found (permanent): "
                "notification_id=%s key=%s: %s",
                self.service_name,
                payload.notification_id,
                idempotency_key,
                exc,
            )
            return self.permanent_failure(
                error=f"Signal service 'notify.{self.service_name}' not found: {exc}"
            )
        except ServiceValidationError as exc:
            _LOGGER.warning(
                "Signal service validation error (permanent): "
                "notification_id=%s key=%s: %s",
                payload.notification_id,
                idempotency_key,
                exc,
            )
            return self.permanent_failure(
                error=f"Signal service validation error: {exc}"
            )
        except HomeAssistantError as exc:
            _LOGGER.warning(
                "Signal service error: notification_id=%s key=%s: %s",
                payload.notification_id,
                idempotency_key,
                exc,
            )
            return self.transient_failure(error=f"Signal service error: {exc}")

        except Exception as exc:
            _LOGGER.exception(
                "Unexpected Signal adapter failure: notification_id=%s key=%s",
                payload.notification_id,
                idempotency_key,
            )
            # Unexpected errors are treated as transient: they are more likely
            # caused by runtime conditions (OOM, event loop issues) than by
            # permanent misconfiguration.
            return self.transient_failure(error=f"signal unexpected error: {exc}")
