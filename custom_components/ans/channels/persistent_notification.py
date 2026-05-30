"""Deliver notifications via Home Assistant persistent notifications."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)

from ..helper import entity_url_path, media_label
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
_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")


class PersistentNotificationAdapter(DeliveryAdapter):
    """Deliver notifications via Home Assistant persistent notifications.

    Persistent notifications are displayed in the Home Assistant frontend
    and stored in the state machine, making them persistent across restarts.

    Attributes
    ----------
    channel : str
        The channel identifier "persistent_notification".

    """

    ADAPTER_METADATA: ClassVar[AdapterMetadata] = AdapterMetadata(
        adapter_type=AdapterType.STATIC,
        channel_prefix="notify.persistent_notification",
        integration="persistent_notification",
    )
    # Channel identifier derived from metadata — no separator appended for
    # STATIC adapters since the prefix IS the full channel ID.
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
        """Return None — persistent_notification has no variant."""
        return None

    @property
    def channel(self) -> str:  # type: ignore[override]  # mypy false positive: abstract property
        """Return the channel identifier."""
        return self._CHANNEL_ID

    @classmethod
    def get_requirements(cls) -> ChannelRequirement:
        """Return requirements indicating no contact information is needed.

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
        job_id: str,
        options: DeliveryOptions | None = None,
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
        job_id : str
            Job identifier for cross-layer log correlation.
        options : DeliveryOptions | None
            Per-delivery options (not used by this adapter).

        Returns
        -------
        DeliveryResult
            Result of delivery attempt (success or failure).

        """
        try:
            message = payload.message

            # Append rich content as markdown links/embeds so operators can
            # see and navigate to attached media or linked resources directly
            # from the HA frontend.
            if payload.image:
                if payload.image.startswith(("http://", "https://")):
                    _label = media_label(payload.image)
                    if _label == payload.image:
                        _LOGGER.warning(
                            "Skipping image URL with no filename segment for "
                            "notification_id=%s: %s",
                            payload.notification_id,
                            payload.image,
                        )
                    else:
                        message += f"\n\n[{_label}]({payload.image})"
                else:
                    message += f"\n\n![image]({payload.image})"
            if payload.video:
                _label = media_label(payload.video)
                if _label == payload.video:
                    _LOGGER.warning(
                        "Skipping video URL with no filename segment for "
                        "notification_id=%s: %s",
                        payload.notification_id,
                        payload.video,
                    )
                else:
                    message += f"\n\n[{_label}]({payload.video})"
            if payload.file:
                _label = media_label(payload.file)
                if _label == payload.file:
                    _LOGGER.warning(
                        "Skipping file URL with no filename segment for "
                        "notification_id=%s: %s",
                        payload.notification_id,
                        payload.file,
                    )
                else:
                    message += f"\n\n[{_label}]({payload.file})"
            if payload.link:
                message += f"\n\n[Details]({payload.link})"

            # context is appended verbatim to the notification body by design.
            # Persistent notifications are a system-wide channel; the raw key=value
            # format is intentional for operator visibility and debugging.
            # Context values that match an existing HA entity ID are auto-linked.
            if payload.context:
                message += "\n\nContext:\n"
                for key, value in payload.context.items():
                    str_value = str(value)
                    if _ENTITY_ID_RE.match(str_value) and self._hass.states.get(
                        str_value
                    ):
                        message += (
                            f"- {key}: [{str_value}]({entity_url_path(str_value)})\n"
                        )
                    else:
                        message += f"- {key}: {str_value}\n"

            # Build notification data
            data: dict[str, Any] = {
                "title": payload.title,
                "message": message,
                "notification_id": str(payload.notification_id),
            }

            # Call persistent_notification.create service
            await self._hass.services.async_call(
                domain="persistent_notification",
                service="create",
                service_data=data,
                blocking=True,
            )

            _LOGGER.debug(
                "Persistent notification created: job_id=%s id='%s' notification_id=%s",
                job_id,
                idempotency_key,
                payload.notification_id,
            )
            return self.success(remote_id=idempotency_key)

        except ServiceNotFound as exc:
            _LOGGER.warning(
                "persistent_notification service not found (permanent): "
                "job_id=%s notification_id=%s key=%s: %s",
                job_id,
                payload.notification_id,
                idempotency_key,
                exc,
            )
            return self.permanent_failure(
                error=f"persistent_notification service not found: {exc}"
            )
        except ServiceValidationError as exc:
            _LOGGER.warning(
                "persistent_notification service validation error (permanent): "
                "job_id=%s notification_id=%s key=%s: %s",
                job_id,
                payload.notification_id,
                idempotency_key,
                exc,
            )
            return self.permanent_failure(
                error=f"persistent_notification service validation error: {exc}"
            )
        except HomeAssistantError as exc:
            _LOGGER.warning(
                "persistent_notification service error: job_id=%s notification_id=%s key=%s: %s",
                job_id,
                payload.notification_id,
                idempotency_key,
                exc,
            )
            return self.transient_failure(
                error=f"persistent_notification service error: {exc}"
            )
        except Exception as exc:
            _LOGGER.exception(
                "Unexpected persistent_notification adapter failure: "
                "job_id=%s notification_id=%s key=%s",
                job_id,
                payload.notification_id,
                idempotency_key,
            )
            return self.transient_failure(
                error=f"persistent_notification service error: {exc}"
            )
