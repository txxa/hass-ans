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

from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN, SERVICE_SEND
from .delivery.orchestrator import NotificationOrchestrator
from .models import (
    NotificationCriticality,
    NotificationPayload,
    NotificationType,
)

SERVICE_REFRESH_CHANNELS = "refresh_channels"

_LOGGER = logging.getLogger(__name__)


async def async_setup_services(
    hass: HomeAssistant,
    orchestrator: NotificationOrchestrator,
) -> None:
    """Register Home Assistant services for ANS."""

    async def _handle_notify(call: ServiceCall) -> None:
        """Handle notification service call.

        Args:
            call: Service call with notification data.

        """
        try:
            payload = _build_payload(call)
            _LOGGER.debug(
                "ANS service called: notification_id=%s source=%s type=%s criticality=%s",
                payload.notification_id,
                payload.source,
                payload.type,
                payload.criticality,
            )
            await orchestrator.handle_notification(payload)
        except ValueError as exc:
            _LOGGER.error("ANS service error: %s", exc)
            raise

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND,
        _handle_notify,
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
        await config_repo.refresh_and_sync()
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

    try:
        payload = NotificationPayload(
            notification_id=str(uuid4()),
            source=str(data["source"]),
            title=str(data["title"]),
            message=str(data["message"]),
            type=NotificationType(data["type"]),
            criticality=NotificationCriticality(data["criticality"]),
            metadata=dict(data.get("metadata", {})),
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
