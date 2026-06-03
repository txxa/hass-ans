"""Service registration for Home Assistant notification integration.

Provides the `ans.send_notification` service for sending notifications through
the ANS notification delivery system with configurable criticality and delivery
channel selection.

Also provides `ans.refresh_channels` for reloading notification channels.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, SERVICE_SEND
from .delivery.orchestrator import NotificationOrchestrator
from .helper import validate_media_path, validate_url_scheme
from .models import (
    NotificationCriticality,
    NotificationPayload,
    NotificationType,
)

SERVICE_REFRESH_CHANNELS = "refresh_channels"

_LOGGER = logging.getLogger(__name__)

# Schema for a single action button (HA mobile app supports up to 3).
_ACTION_SCHEMA = vol.Schema(
    {
        vol.Required("action"): cv.string,
        vol.Required("title"): cv.string,
        vol.Optional("url"): cv.string,
    }
)

# Full service call schema for ans.send_notification.
SEND_NOTIFICATION_SCHEMA = vol.Schema(
    {
        vol.Required("source"): cv.string,
        vol.Required("title"): cv.string,
        vol.Required("message"): cv.string,
        vol.Required("type"): vol.In([t.value for t in NotificationType]),
        vol.Required("criticality"): vol.In([c.value for c in NotificationCriticality]),
        vol.Optional("context", default={}): vol.Any(None, dict),
        vol.Optional("link"): vol.All(cv.string, validate_url_scheme),
        vol.Optional("image"): vol.All(cv.string, validate_media_path),
        vol.Optional("video"): vol.All(cv.string, validate_media_path),
        vol.Optional("file"): vol.All(cv.string, validate_media_path),
        vol.Optional("channel_data", default={}): vol.Any(None, dict),
        vol.Optional("actions", default=[]): vol.All(
            [_ACTION_SCHEMA], vol.Length(max=3)
        ),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_services(
    hass: HomeAssistant,
    orchestrator: NotificationOrchestrator,
) -> None:
    """Register Home Assistant services for ANS."""

    async def _handle_notify(call: ServiceCall) -> dict[str, str]:
        """Handle notification service call.

        Args:
            call: Service call with notification data.

        Returns:
            A dict containing the generated ``notification_id`` so automations
            can correlate delivery outcome events back to this call.

        """
        notification_id: str | None = None
        try:
            payload = _build_payload(call)
            notification_id = payload.notification_id
            _LOGGER.debug(
                "ANS service called: notification_id=%s source=%s type=%s criticality=%s",
                payload.notification_id,
                payload.source,
                payload.type.value,
                payload.criticality.value,
            )
            await orchestrator.handle_notification(payload)
        except ValueError as exc:
            _LOGGER.error(
                "ANS service error notification_id=%s: %s",
                notification_id or "unknown",
                exc,
            )
            raise
        return {"notification_id": notification_id}

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND,
        _handle_notify,
        schema=SEND_NOTIFICATION_SCHEMA,
    )
    _LOGGER.debug("ANS service '%s.%s' registered", DOMAIN, SERVICE_SEND)

    async def _handle_refresh_channels(call: ServiceCall) -> None:
        """Refresh notification channels and adapters.

        Service: ans.refresh_channels

        Use this if:
        - You added a new notify integration
        - Channels aren't appearing in ANS
        - Diagnostics show channel-adapter mismatches
        """
        # Local import to avoid circular dependency
        from . import get_config_repository  # noqa: PLC0415

        config_repo = get_config_repository(hass)
        if not config_repo:
            _LOGGER.error("ANS not initialized, cannot refresh channels")
            return

        # Refresh channels and re-sync adapters via the new unified manager
        if config_repo.channel_manager:
            await config_repo.channel_manager.resync()
        _LOGGER.info(
            "Channels refreshed: %d detected channels",
            config_repo.channel_manager.count_detected()
            if config_repo.channel_manager
            else 0,
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_CHANNELS,
        _handle_refresh_channels,
    )
    _LOGGER.debug("ANS service '%s.%s' registered", DOMAIN, SERVICE_REFRESH_CHANNELS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_payload(call: ServiceCall) -> NotificationPayload:
    """Translate HA service data into a NotificationPayload.

    This function performs **only syntactic validation**.
    Semantic filtering happens later in the filter engine.
    """

    data: dict[str, Any] = call.data

    # --- backwards-compatibility shim for the deprecated `metadata` field ---
    # Prior to the payload redesign, `metadata` was a single flat dict that
    # adapters read directly (Signal: text_mode/attachments/urls/verify_ssl,
    # Mobile App: entire dict forwarded as push data, Persistent: appended to body).
    # It has been split into `channel_data` (adapter overrides) and `context`
    # (correlation data). When `metadata` is supplied without the new fields,
    # its contents are used as a fallback for both so existing automations
    # continue to work. Explicit `context` / `channel_data` always take priority.
    context = dict(data.get("context") or {})
    channel_data = dict(data.get("channel_data") or {})
    raw_metadata = data.get("metadata")
    if raw_metadata and isinstance(raw_metadata, dict):
        _LOGGER.warning(
            "ANS: 'metadata' is deprecated and will be removed in a future version. "
            "Replace it with 'channel_data' (adapter-specific options such as text_mode, "
            "tag, attachments) and/or 'context' (correlation data such as entity, camera). "
            "See the ANS Advanced Topics documentation for migration details."
        )
        if not context:
            context = dict(raw_metadata)
        if not channel_data:
            channel_data = dict(raw_metadata)

    try:
        payload = NotificationPayload(
            notification_id=str(uuid4()),
            source=str(data["source"]),
            title=str(data["title"]),
            message=str(data["message"]),
            type=NotificationType(data["type"]),
            criticality=NotificationCriticality(data["criticality"]),
            context=context,
            link=data.get("link"),
            image=data.get("image"),
            video=data.get("video"),
            file=data.get("file"),
            channel_data=channel_data,
            actions=list(data.get("actions", [])),
            created_at=datetime.now(UTC),
        )
    except KeyError as exc:
        raise ValueError(f"Missing required field: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"Invalid notification field value: {exc}") from exc

    return payload


# ---------------------
# service call examples
# ---------------------
#
# service: ans.send_notification
# data:
#   source: "home_assistant"
#   title: "Door opened"
#   message: "Front door was opened"
#   type: "security"
#   criticality: "high"
#   metadata:
#     entity_id: binary_sensor.front_door
#
#
# service: ans.send_notification
# data:
#   source: "alarm"
#   title: "Motion detected"
#   message: "Movement in the garage"
#   type: "security"
#   criticality: "high"
#   metadata:
#     camera: garage
