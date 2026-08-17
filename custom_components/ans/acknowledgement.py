"""Acknowledgement tracking: listener wiring for the ANS bootstrap."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.persistent_notification import (
    SIGNAL_PERSISTENT_NOTIFICATIONS_UPDATED,
)
from homeassistant.components.persistent_notification import (
    UpdateType as PNUpdateType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Context, Event, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .channels.mobile_app import MobileAppDeliveryAdapter
from .const import (
    EVENT_NOTIFICATION_ACKNOWLEDGED,
    EVENT_NOTIFICATION_DELIVERED,
    PERSISTENT_NOTIFICATION_CHANNEL,
)
from .delivery.factory import ANSSystem

_LOGGER = logging.getLogger(__name__)


def _mobile_device_name(channel_id: str) -> str | None:
    """Return the device-name slug for a mobile_app channel_id, or None for other channels."""
    if not channel_id.startswith(MobileAppDeliveryAdapter.CHANNEL_PREFIX):
        return None
    return channel_id.removeprefix(MobileAppDeliveryAdapter.CHANNEL_PREFIX) or None


async def async_setup_acknowledgement_tracking(
    hass: HomeAssistant,
    entry: ConfigEntry,
    system: ANSSystem,
) -> None:
    """Register event listeners for notification acknowledgement tracking (NH-3).

    Four listeners are registered:

    1. ``ans_notification_delivered`` — when a mobile_app or
       persistent_notification delivery succeeds, persist a ``pending`` record
       via :meth:`AcknowledgementRegistry.mark_pending` so that eligibility
       survives HA restarts.

    2. ``mobile_app_notification_action`` — fired by the HA mobile companion app
       when the user taps an action button.  If the ``tag`` field matches a
       pending notification the acknowledgement is recorded and
       ``ans_notification_acknowledged`` is fired with ``action`` and
       ``device_name`` included when available.

    3. ``mobile_app_notification_tapped`` — fired by the HA mobile companion app
       when the user taps the notification body.  Treated identically to an
       action tap for acknowledgement purposes (no ``action`` field in payload).

    4. ``persistent_notification.removed`` — fired when a persistent notification
       is dismissed in the HA frontend.  If ``notification_id`` matches a pending
       entry the same acknowledgement flow runs.

    All listeners are unloaded automatically when the config entry is unloaded.

    Parameters
    ----------
    hass : HomeAssistant
        Home Assistant instance.
    entry : ConfigEntry
        Config entry whose unload callbacks own the listener lifecycle.
    system : ANSSystem
        Active ANS system; provides ``acknowledgement_registry``.

    """
    acknowledgement_registry = system.acknowledgement_registry

    # Restore eligibility from the durable store so that notifications delivered
    # before an HA restart remain acknowledgeable in the current session.
    # _pending_meta maps notification_id → delivery channel_id; used for
    # membership checks and for deriving device_name on acknowledgement.
    _pending_meta: dict[
        str, str
    ] = await acknowledgement_registry.get_pending_channel_ids()
    # Restore custom-tag → notification_id mapping so notifications sent with
    # channel_data.tag can still be correlated after a restart.
    _tag_to_notif_id: dict[
        str, str
    ] = await acknowledgement_registry.get_pending_mobile_tags()
    # Temporary store for the HA Context captured from call_service events for
    # persistent_notification.dismiss — keyed by ANS notification_id (UUID).
    # Populated just before the service executes; consumed when the dispatcher
    # signal fires (within the same synchronous call chain).
    _pn_dismiss_context: dict[str, Context] = {}
    if _pending_meta:
        _LOGGER.debug(
            "Restored %d pending ack(s) from persistent store on startup",
            len(_pending_meta),
        )

    async def _on_notification_delivered(event: Event[Any]) -> None:
        """Persist a pending-ack record when a mobile_app or persistent_notification delivers."""
        channel_id: str = event.data.get("channel_id", "")
        notification_id: str = event.data.get("notification_id", "")
        if not notification_id:
            return
        if (
            channel_id.startswith(MobileAppDeliveryAdapter.CHANNEL_PREFIX)
            or channel_id == PERSISTENT_NOTIFICATION_CHANNEL
        ):
            # mobile_tag is set by MobileAppDeliveryAdapter only when the
            # effective data.tag is a custom value (differs from the UUID).
            mobile_tag: str | None = event.data.get("mobile_tag")
            recorded = await acknowledgement_registry.mark_pending(
                notification_id=notification_id,
                channel_id=channel_id,
                delivered_at=datetime.now(UTC),
                mobile_tag=mobile_tag,
            )
            if recorded:
                _pending_meta[notification_id] = channel_id
                _LOGGER.debug(
                    "Pending ack registered for notification_id '%s' (channel=%s)",
                    notification_id,
                    channel_id,
                )
            # Always register the mobile_tag regardless of whether a new pending
            # record was created.  When persistent_notification delivers before
            # mobile, mark_pending returns False for the mobile delivery (one
            # record per notification_id), but we still need the
            # tag → notification_id mapping so mobile events can be resolved.
            # The adapter only sets mobile_tag when it differs from notification_id
            # (custom channel_data.tag), so no equality guard is needed here.
            if mobile_tag:
                _tag_to_notif_id[mobile_tag] = notification_id
                if not recorded:
                    _LOGGER.debug(
                        "Mobile tag '%s' registered for already-tracked notification '%s'",
                        mobile_tag,
                        notification_id,
                    )
        else:
            _LOGGER.debug(
                "Delivered notification '%s' on channel '%s' is not tracked for acknowledgement",
                notification_id,
                channel_id,
            )

    entry.async_on_unload(
        hass.bus.async_listen(EVENT_NOTIFICATION_DELIVERED, _on_notification_delivered)
    )

    async def _handle_mobile_ack(
        tag: str | None, action: str | None, event_context: Context
    ) -> None:
        """Shared acknowledgement logic for action-button taps and notification-body taps."""
        if not tag:
            _LOGGER.debug(
                "Mobile notification event ignored: event data contains no 'tag' field"
            )
            return

        # Resolve custom tag → canonical notification_id UUID.  When the
        # notification was sent with channel_data.tag, the companion app echoes
        # that custom tag back; _tag_to_notif_id maps it to the UUID stored in
        # _pending_meta.  Falls back to tag itself for the common case where
        # tag == notification_id (no custom tag).
        notification_id: str = _tag_to_notif_id.get(tag, tag)

        if notification_id not in _pending_meta:
            _LOGGER.debug(
                "Mobile notification event ignored: tag '%s' (resolved to '%s') "
                "is not in pending acks (notification unknown or already acknowledged)",
                tag,
                notification_id,
            )
            return

        acknowledged_at = datetime.now(UTC)

        # notification_id is in _pending_meta (early-returned above if not), so
        # direct key access is safe and avoids an unnecessary fallback expression.
        delivery_channel: str = _pending_meta[notification_id]
        device_name: str | None = _mobile_device_name(delivery_channel)

        recorded = await acknowledgement_registry.record_acknowledgement(
            notification_id=notification_id,
            channel_id=delivery_channel,
            acknowledged_at=acknowledged_at,
        )
        if recorded:
            _pending_meta.pop(notification_id, None)
            _tag_to_notif_id.pop(tag, None)
            payload: dict[str, Any] = {
                "notification_id": notification_id,
                "channel_id": "mobile_app",
                "acknowledged_at": acknowledged_at.isoformat(),
            }
            if action:
                payload["method"] = "action_button"
                payload["action"] = action
            else:
                payload["method"] = "notification_tap"
            if device_name is not None:
                payload["device_name"] = device_name
            hass.bus.async_fire(
                EVENT_NOTIFICATION_ACKNOWLEDGED, payload, context=event_context
            )
            _LOGGER.debug(
                "Notification '%s' acknowledged via mobile_app "
                "(action=%s device_name=%s user_id=%s)",
                notification_id,
                action,
                device_name,
                event_context.user_id,
            )
        else:
            _LOGGER.debug(
                "Mobile notification event for tag '%s' (notification_id='%s'): "
                "acknowledgement not recorded (already acknowledged or registry write failed)",
                tag,
                notification_id,
            )

    async def _on_mobile_app_event(event: Event[Any]) -> None:
        """Handle mobile_app action-button and notification-tap events.

        Both event types are routed through the same handler.  For
        ``mobile_app_notification_action`` the ``action`` field carries the
        identifier of the tapped button; for ``mobile_app_notification_tapped``
        the field is absent so ``event.data.get("action")`` returns ``None``,
        which ``_handle_mobile_ack`` maps to method ``notification_tap``.
        """
        await _handle_mobile_ack(
            tag=event.data.get("tag"),
            action=event.data.get("action"),
            event_context=event.context,
        )

    entry.async_on_unload(
        hass.bus.async_listen("mobile_app_notification_action", _on_mobile_app_event)
    )
    entry.async_on_unload(
        hass.bus.async_listen("mobile_app_notification_tapped", _on_mobile_app_event)
    )

    async def _on_call_service(event: Event[Any]) -> None:
        """Capture the HA context from persistent_notification.dismiss service calls.

        EVENT_CALL_SERVICE fires with the caller's context before the service
        executes, which is the only point at which user identity is available for
        persistent notification dismissals.  The context is stored temporarily
        and consumed when SIGNAL_PERSISTENT_NOTIFICATIONS_UPDATED fires (within
        the same synchronous call chain).
        """
        if (
            event.data.get("domain") == "persistent_notification"
            and event.data.get("service") == "dismiss"
        ):
            # .get(..., "") means an absent key yields "" which is never in _pending_meta.
            nid: str = event.data.get("service_data", {}).get("notification_id", "")
            if nid in _pending_meta:
                _pn_dismiss_context[nid] = event.context

    entry.async_on_unload(hass.bus.async_listen(EVENT_CALL_SERVICE, _on_call_service))

    async def _on_persistent_notification_removed(
        update_type: PNUpdateType, notifications: dict
    ) -> None:
        """Handle persistent notification dismissal — counts as acknowledgement.

        Called by the HA dispatcher with UpdateType.REMOVED when the user dismisses
        a persistent notification.  ``notifications`` is the dict of removed entries
        keyed by notification_id.
        """
        if update_type != PNUpdateType.REMOVED:
            return

        for notification_id in notifications:
            if notification_id not in _pending_meta:
                _LOGGER.debug(
                    "persistent_notification removal ignored for '%s': not in pending acks",
                    notification_id,
                )
                continue

            # Retrieve and discard the context captured by _on_call_service (if any).
            # Present when dismissed via the service (e.g. frontend); absent when
            # dismissed programmatically via async_dismiss directly.
            dismiss_context: Context | None = _pn_dismiss_context.pop(
                notification_id, None
            )

            acknowledged_at = datetime.now(UTC)
            recorded = await acknowledgement_registry.record_acknowledgement(
                notification_id=notification_id,
                channel_id=PERSISTENT_NOTIFICATION_CHANNEL,
                acknowledged_at=acknowledged_at,
            )
            if recorded:
                _pending_meta.pop(notification_id, None)
                # Clean up any custom-tag mappings that pointed to this notification
                # so stale entries don't linger in _tag_to_notif_id.
                kept = {
                    t: v for t, v in _tag_to_notif_id.items() if v != notification_id
                }
                _tag_to_notif_id.clear()
                _tag_to_notif_id.update(kept)
                pn_payload: dict[str, Any] = {
                    "notification_id": notification_id,
                    "channel_id": PERSISTENT_NOTIFICATION_CHANNEL,
                    "acknowledged_at": acknowledged_at.isoformat(),
                    "method": "persistent_notification_dismiss",
                }
                hass.bus.async_fire(
                    EVENT_NOTIFICATION_ACKNOWLEDGED,
                    pn_payload,
                    context=dismiss_context,
                )
                _LOGGER.debug(
                    "Notification '%s' acknowledged via persistent_notification dismissal "
                    "(user_id=%s)",
                    notification_id,
                    getattr(dismiss_context, "user_id", None),
                )
            else:
                _LOGGER.debug(
                    "persistent_notification dismissal for '%s': acknowledgement not recorded "
                    "(already acknowledged or registry write failed)",
                    notification_id,
                )

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_PERSISTENT_NOTIFICATIONS_UPDATED,
            _on_persistent_notification_removed,
        )
    )
