"""Advanced Notification System integration bootstrap."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from homeassistant.components.persistent_notification import (
    SIGNAL_PERSISTENT_NOTIFICATIONS_UPDATED,
)
from homeassistant.components.persistent_notification import (
    UpdateType as PNUpdateType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_SERVICE_REGISTERED
from homeassistant.core import Context, Event, EventStateChangedData, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_registry import (
    EVENT_ENTITY_REGISTRY_UPDATED,
    EventEntityRegistryUpdatedData,
)
from homeassistant.helpers.event import async_track_state_added_domain

from custom_components.ans.config_flow import ANSConfigFlow

from .channels.base import ChannelStatus
from .channels.channel_manager import ChannelManager
from .channels.mobile_app import MobileAppDeliveryAdapter
from .config.repository import ConfigRepository
from .const import (
    DOMAIN,
    EVENT_NOTIFICATION_ACKNOWLEDGED,
    EVENT_NOTIFICATION_DELIVERED,
    PERSISTENT_NOTIFICATION_CHANNEL,
    REPAIR_ISSUE_STALE_CHANNEL,
    REQUIRED_MP_FEATURES,
    SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY,
    SYS_CONFIG_RETRY_BASE_DELAY_KEY,
    SYS_CONFIG_RETRY_MAX_DELAY_KEY,
    SYS_MAX_RETRY_BACKOFF_FACTOR,
    SYS_MAX_RETRY_BASE_DELAY_SECONDS,
    SYS_MAX_RETRY_MAX_DELAY_SECONDS,
    SYS_MIN_RETRY_BACKOFF_FACTOR,
    SYS_MIN_RETRY_BASE_DELAY_SECONDS,
    SYS_MIN_RETRY_MAX_DELAY_SECONDS,
    SYS_STORAGE_ACKNOWLEDGEMENTS_FILE,
    SYS_STORAGE_ATTEMPTS_FILE,
    SYS_STORAGE_NOTIFICATIONS_FILE,
    SYS_STORAGE_RETRIES_FILE,
    SYS_STORAGE_VOLUME_RESTORATION_FILE,
)
from .delivery.factory import ANSSystem, create_system
from .helper import get_main_entry
from .models import NotificationDeliveryTask
from .persistence.recovery import async_initialize_persistence
from .persistence.volume_restoration import VolumeRestorationRegistry
from .service import async_setup_services

if TYPE_CHECKING:
    from .delivery.queue import NotificationDeliveryTaskQueue
    from .delivery.rate_limiter import RateLimiter

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Entry-level helpers (A3: decomposed from async_setup_entry)
# ---------------------------------------------------------------------------


async def _setup_config(hass: HomeAssistant, entry: ConfigEntry) -> ConfigRepository:
    """Load and validate configuration.  Raises ConfigEntryNotReady on failure.

    Parameters
    ----------
    hass : HomeAssistant
        Home Assistant instance used to initialize and load integration data.
    entry : ConfigEntry
        Config entry being set up for this integration instance.

    Returns
    -------
    ConfigRepository
        Fully loaded repository (system_config + recipients).

    Raises
    ------
    ConfigEntryNotReady
        When the main config entry cannot be loaded.

    """
    _LOGGER.info("[ANS setup] Phase 1/5 — loading configuration")
    config_repo = ConfigRepository(hass)
    if not await config_repo.load():
        raise ConfigEntryNotReady("Failed to load main entry configuration")
    _LOGGER.debug("[ANS setup] Configuration loaded")
    return config_repo


async def _setup_system(
    hass: HomeAssistant,
    config_repo: ConfigRepository,
    volume_registry: VolumeRestorationRegistry,
) -> ANSSystem:
    """Create the ANSSystem and run the initial channel sync.

    Parameters
    ----------
    hass : HomeAssistant
        Home Assistant instance used to initialize system components.
    config_repo : ConfigRepository
        Loaded configuration repository containing system settings and recipients.
    volume_registry : VolumeRestorationRegistry
        Must be already loaded before this call.

    Returns
    -------
    ANSSystem

    """
    _LOGGER.info("[ANS setup] Phase 2/5 — creating system components")
    system = create_system(
        hass=hass,
        config_repo=config_repo,
        volume_registry=volume_registry,
    )

    # Sync dynamic adapters (ChannelManager.sync is async; create_system is sync)
    if config_repo.system_config:
        await system.channel_manager.sync(
            list(config_repo.system_config.enabled_channels)
        )
        _LOGGER.info(
            "[ANS setup] Channel sync complete: %d detected, %d active",
            system.channel_manager.count_detected(),
            system.channel_manager.count_active(),
        )
    return system


async def _setup_persistence(
    hass: HomeAssistant, system: ANSSystem
) -> tuple[list[tuple[NotificationDeliveryTask, datetime]], list[str]]:
    """Load persistence stores and recover pending retries.

    Parameters
    ----------
    hass : HomeAssistant
        Home Assistant instance used to initialize persistence state.
    system : ANSSystem
        Active ANS system whose registries and queues are restored from storage.

    Returns
    -------
    tuple[list[tuple[NotificationDeliveryTask, datetime]], list[str]]
        ``(pending_tasks, orphaned_retries)`` where *pending_tasks* is a list
        of ``(task, scheduled_time)`` pairs and *orphaned_retries* is a list
        of job-ID strings whose task snapshots could not be recovered.

    """
    _LOGGER.info("[ANS setup] Phase 3/5 — initializing persistence")
    pending_tasks, orphaned_retries = await async_initialize_persistence(
        hass,
        system.notification_registry,
        system.attempt_log,
        system.retry_queue,
    )
    _LOGGER.info(
        "[ANS setup] Persistence initialized: %d tasks to recover, %d orphaned",
        len(pending_tasks),
        len(orphaned_retries),
    )
    return pending_tasks, orphaned_retries


async def _setup_tasks(
    system: ANSSystem,
    pending_tasks: list[tuple[NotificationDeliveryTask, datetime]],
    orphaned_retries: list[str],
) -> None:
    """Start background workers and schedule recovered retries.

    Parameters
    ----------
    system : ANSSystem
        Active ANS system whose queue and schedulers are started.
    pending_tasks : list[tuple[NotificationDeliveryTask, datetime]]
        ``(task, scheduled_time)`` pairs from persistence recovery.
    orphaned_retries : list[str]
        Job-ID strings whose task snapshots could not be recovered.

    """
    _LOGGER.info("[ANS setup] Phase 4/5 — starting background tasks")
    await asyncio.gather(
        system.task_queue.start(),
        system.housekeeping_scheduler.start(),
        system.deduplication_service.start(),
    )
    _LOGGER.debug("[ANS setup] Background workers started")

    now = datetime.now(UTC)
    pending_coros = []
    for task, scheduled_time in pending_tasks:
        delay_seconds = max((scheduled_time - now).total_seconds(), 0)
        if delay_seconds == 0:
            _LOGGER.info(
                "[ANS setup] Retry for job %s is overdue, executing immediately",
                task.job_id,
            )
        pending_coros.append(
            system.task_queue.add_task(task, delay=timedelta(seconds=delay_seconds))
        )
    if pending_coros:
        enqueue_results = await asyncio.gather(*pending_coros, return_exceptions=True)
        for result in enqueue_results:
            if isinstance(result, BaseException):
                _LOGGER.error(
                    "[ANS setup] Failed to re-enqueue a recovered retry task: %s",
                    result,
                )

    remove_coros = []
    for job_id_str in orphaned_retries:
        _LOGGER.warning(
            "[ANS setup] Removing orphaned retry schedule for job %s (no task data)",
            job_id_str,
        )
        try:
            remove_coros.append(system.retry_queue.remove_retry(UUID(job_id_str)))
        except ValueError:
            _LOGGER.error("[ANS setup] Invalid UUID for orphaned retry: %s", job_id_str)
    if remove_coros:
        remove_results = await asyncio.gather(*remove_coros, return_exceptions=True)
        for result in remove_results:
            if isinstance(result, BaseException):
                _LOGGER.error(
                    "[ANS setup] Failed to remove orphaned retry from queue: %s",
                    result,
                )


async def _setup_services(hass: HomeAssistant, system: ANSSystem) -> None:
    """Register ANS HA service handlers."""
    _LOGGER.info("[ANS setup] Phase 5/5 — registering services and listeners")
    await async_setup_services(hass, system.orchestrator)
    _LOGGER.debug("[ANS setup] Services registered")


def _setup_repairs(
    hass: HomeAssistant,
    channel_manager: ChannelManager,
) -> None:
    """Register the channel lifecycle callback that creates and deletes Repairs issues.

    Creates a HA Repairs issue whenever a channel transitions to STALE so the user
    sees an actionable alert in the UI.  Deletes the issue when the channel recovers.

    Parameters
    ----------
    hass : HomeAssistant
        Home Assistant instance used to call the issue registry helpers.
    channel_manager : ChannelManager
        Channel manager on which the lifecycle callback is registered.

    """

    def _on_channel_lifecycle_change(
        newly_staled: list[str], newly_recovered: list[str]
    ) -> None:
        for channel_id in newly_staled:
            record = channel_manager.get_record(channel_id)
            channel_label = record.info.label if record else channel_id
            issue_id = f"{REPAIR_ISSUE_STALE_CHANNEL}_{channel_id.replace('.', '_')}"
            _LOGGER.debug(
                "Creating repair issue '%s' for stale channel '%s'",
                issue_id,
                channel_id,
            )
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=REPAIR_ISSUE_STALE_CHANNEL,
                translation_placeholders={
                    "channel_label": channel_label,
                    "channel_id": channel_id,
                },
            )
        for channel_id in newly_recovered:
            issue_id = f"{REPAIR_ISSUE_STALE_CHANNEL}_{channel_id.replace('.', '_')}"
            _LOGGER.debug(
                "Deleting repair issue '%s' for recovered channel '%s'",
                issue_id,
                channel_id,
            )
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    channel_manager.set_channel_lifecycle_callback(_on_channel_lifecycle_change)


def _setup_listeners(
    hass: HomeAssistant,
    entry: ConfigEntry,
    channel_manager: ChannelManager,
) -> None:
    """Register all event listeners for the lifetime of this config entry.

    Attaches four listeners to the config entry's unload callback so they
    are automatically removed when the entry is unloaded:

    - Options update → triggers a clean config-entry reload.
    - ``EVENT_SERVICE_REGISTERED`` → resyncs channels when a new
      ``notify.*`` service is registered.
    - State-added for ``media_player`` domain → resyncs channels when a
      capable media player appears.
    - ``EVENT_ENTITY_REGISTRY_UPDATED`` → resyncs channels when a
      ``media_player`` entity is removed from the registry.

    Resync requests during setup are deferred via
    :meth:`ChannelManager.request_resync` and flushed once
    :meth:`ChannelManager.finalize_setup` is called.

    Parameters
    ----------
    hass : HomeAssistant
        Home Assistant instance used to register event bus and state listeners.
    entry : ConfigEntry
        Config entry whose unload callbacks own the listener lifecycle.
    channel_manager : ChannelManager
        Channel manager to resync channels when relevant HA events occur.

    """

    # L1 — Options update → clean reload (avoids partial-state in-place updates)
    async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
        await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    # Notify service registration → resync channels
    async def _on_notify_service_registered(event: Event[Any]) -> None:
        if event.data.get("domain") != "notify":
            return
        service = event.data.get("service", "")
        if service in ("notify", "send_message"):
            return
        _LOGGER.debug(
            "New notify service 'notify.%s' registered — refreshing ANS channels",
            service,
        )
        try:
            await channel_manager.request_resync()
        except Exception:
            _LOGGER.exception(
                "Failed to refresh channels after notify service 'notify.%s' was registered",
                service,
            )

    entry.async_on_unload(
        hass.bus.async_listen(EVENT_SERVICE_REGISTERED, _on_notify_service_registered)
    )

    # New media_player added → resync only if it has required features
    async def _on_media_player_added(event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new_state = event.data.get("new_state")
        supported = (
            new_state.attributes.get("supported_features", 0) if new_state else 0
        )
        if (supported & REQUIRED_MP_FEATURES) != REQUIRED_MP_FEATURES:
            _LOGGER.debug(
                "Ignoring media_player '%s': missing required features (0x%x)",
                entity_id,
                supported,
            )
            return
        _LOGGER.debug(
            "Capable media_player '%s' added — refreshing ANS channels", entity_id
        )
        try:
            await channel_manager.request_resync()
        except Exception:
            _LOGGER.exception(
                "Failed to refresh channels after media_player '%s' was added",
                entity_id,
            )

    entry.async_on_unload(
        async_track_state_added_domain(hass, "media_player", _on_media_player_added)
    )

    # media_player entity removed → resync to mark channel STALE
    async def _on_entity_registry_updated(
        event: Event[EventEntityRegistryUpdatedData],
    ) -> None:
        if event.data["action"] != "remove":
            return
        entity_id = event.data["entity_id"]
        if not entity_id.startswith("media_player."):
            return
        _LOGGER.debug(
            "media_player entity '%s' removed — refreshing ANS channels",
            entity_id,
        )
        try:
            await channel_manager.request_resync()
        except Exception:
            _LOGGER.exception(
                "Failed to refresh channels after media_player '%s' was removed",
                entity_id,
            )

    entry.async_on_unload(
        hass.bus.async_listen(
            EVENT_ENTITY_REGISTRY_UPDATED, _on_entity_registry_updated
        )
    )


def _mobile_device_name(channel_id: str) -> str | None:
    """Return the device-name slug for a mobile_app channel_id, or None for other channels."""
    if not channel_id.startswith(MobileAppDeliveryAdapter.CHANNEL_PREFIX):
        return None
    return channel_id.removeprefix(MobileAppDeliveryAdapter.CHANNEL_PREFIX) or None


async def _setup_acknowledgement_tracking(
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


# ---------------------------------------------------------------------------
# Config entry lifecycle
# ---------------------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ANS integration for a config entry."""
    _LOGGER.info("Setting up ANS config entry: %s", entry.entry_id)

    try:
        entry_data: dict[str, Any] = {}
        entry.runtime_data = entry_data

        # Phase 1 — configuration + volume restoration (concurrent: independent I/O)
        volume_registry = VolumeRestorationRegistry(hass)
        config_repo, _ = await asyncio.gather(
            _setup_config(hass, entry),
            volume_registry.async_load(),
        )
        entry_data["config_repository"] = config_repo
        entry_data["volume_registry"] = volume_registry
        _LOGGER.debug("[ANS setup] Configuration and volume registry loaded")

        if not config_repo.recipients:
            _LOGGER.warning(
                "ANS config entry %s has no recipients configured. "
                "Notifications will be dropped.",
                entry.entry_id,
            )

        # Phase 2 — system (ChannelManager injected into config_repo inside create_system)
        system = await _setup_system(hass, config_repo, volume_registry)
        entry_data["system"] = system

        # Wire ChannelManager into config_repo for UI-facing lookups
        # (diagnostics, config flows, service handler).
        config_repo.channel_manager = system.channel_manager

        # Register event listeners early so no channel events are missed
        # during the remaining setup phases. Resync calls are suppressed
        # until finalize_setup() clears the _setup_in_progress flag.
        _setup_listeners(hass, entry, system.channel_manager)

        # Register the Repairs callback so stale-channel issues are surfaced in the UI.
        # Must be registered before finalize_setup() in case a deferred resync fires.
        _setup_repairs(hass, system.channel_manager)

        # Phase 3 — persistence
        pending_tasks, orphaned_retries = await _setup_persistence(hass, system)

        # Phase 4 — background tasks + retry recovery
        await _setup_tasks(system, pending_tasks, orphaned_retries)

        # Phase 5 — HA services
        await _setup_services(hass, system)

        # Acknowledgement tracking (NH-3): listen for mobile_app actions and
        # persistent_notification dismissals to fire ans_notification_acknowledged.
        await _setup_acknowledgement_tracking(hass, entry, system)

        # Finalize setup: clear suppression flag and flush any deferred resync.
        await system.channel_manager.finalize_setup()

        # Post-restart cleanup: delete any stale-channel Repairs issues for channels
        # that are now ACTIVE (they recovered during the restart window). This is a
        # no-op if no issue exists for a given channel.
        for record in system.channel_manager.get_all_records():
            if record.status == ChannelStatus.ACTIVE:
                ir.async_delete_issue(
                    hass,
                    DOMAIN,
                    f"{REPAIR_ISSUE_STALE_CHANNEL}_{record.info.id.replace('.', '_')}",
                )

        _LOGGER.info("Successfully set up ANS config entry: %s", entry.entry_id)

    except Exception as e:
        _LOGGER.exception("Failed to set up ANS config entry %s", entry.entry_id)
        await _cleanup_entry_data(entry)
        if isinstance(e, ConfigEntryNotReady):
            raise
        raise ConfigEntryNotReady(f"Setup failed: {e}") from e

    return True


async def _teardown_entry_components(entry_data: dict) -> None:
    """Stop and clean up all running components for a config entry."""
    system: ANSSystem | None = entry_data.get("system")
    volume_registry = entry_data.get("volume_registry")

    teardown_coros = []
    if system:
        teardown_coros.extend(
            [
                system.task_queue.stop(),
                system.housekeeping_scheduler.stop(),
                system.deduplication_service.stop(),
                system.channel_manager.cleanup_all(),
                system.orchestrator.stop(),
            ]
        )
    if volume_registry:
        teardown_coros.append(volume_registry.async_unload())

    if teardown_coros:
        teardown_results = await asyncio.gather(*teardown_coros, return_exceptions=True)
        for result in teardown_results:
            if isinstance(result, BaseException):
                _LOGGER.warning("ANS component teardown error: %s", result)
    _LOGGER.debug("ANS components torn down")


async def _cleanup_entry_data(entry: ConfigEntry) -> None:
    """Clean up any partially initialized runtime data for a config entry.

    Safe to call during both normal unload and failed-setup error paths.
    No-ops gracefully if *entry* has no ``runtime_data``.

    Parameters
    ----------
    entry : ConfigEntry
        The config entry whose runtime data should be torn down.

    """
    entry_data = getattr(entry, "runtime_data", None)
    if entry_data:
        await _teardown_entry_components(entry_data)
        entry.runtime_data = {}


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload integration resources for a config entry."""
    _LOGGER.debug("Unloading Advanced Notification System entry: %s", entry.entry_id)
    await _cleanup_entry_data(entry)
    return True


_STORAGE_MIGRATION_PAIRS: list[tuple[str, str]] = [
    ("ans_notifications.json", SYS_STORAGE_NOTIFICATIONS_FILE),
    ("ans_delivery_attempts.json", SYS_STORAGE_ATTEMPTS_FILE),
    ("ans_retry_queue.json", SYS_STORAGE_RETRIES_FILE),
    ("ans_acknowledgements.json", SYS_STORAGE_ACKNOWLEDGEMENTS_FILE),
    ("ans_volume_restoration", SYS_STORAGE_VOLUME_RESTORATION_FILE),
]


def _do_migrate_storage_files(storage_dir: Path) -> None:
    """Rename legacy storage files to the current dot-separated naming scheme."""
    for old_name, new_name in _STORAGE_MIGRATION_PAIRS:
        _LOGGER.debug("Checking storage file migration: %s → %s", old_name, new_name)
        old_path = storage_dir / old_name
        new_path = storage_dir / new_name

        if old_path.exists() and not new_path.exists():
            old_path.rename(new_path)
            _LOGGER.info("Migrated storage file %s → %s", old_name, new_name)

        elif old_path.exists() and new_path.exists():
            old_mtime = old_path.stat().st_mtime
            new_mtime = new_path.stat().st_mtime
            _LOGGER.debug(
                "Conflict: both %s and %s exist — old_mtime=%s, new_mtime=%s",
                old_name,
                new_name,
                old_mtime,
                new_mtime,
            )
            if old_mtime > new_mtime:
                old_path.replace(new_path)
                _LOGGER.warning(
                    "Both %s and %s existed; old was newer — replaced new with old",
                    old_name,
                    new_name,
                )
            else:
                old_path.unlink()
                _LOGGER.warning(
                    "Both %s and %s existed; new was newer — deleted old",
                    old_name,
                    new_name,
                )

        else:
            _LOGGER.debug(
                "Skipping %s — not found (fresh install or already migrated)", old_name
            )


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entries to the current schema version."""

    version = config_entry.version
    minor_version = getattr(config_entry, "minor_version", 0)

    _LOGGER.debug(
        "Checking if ANS config entry %s requires migration (current version: %s.%s)",
        config_entry.entry_id,
        version,
        minor_version,
    )

    # If the entry version is newer than the code supports, log a warning and skip migration
    if version > ANSConfigFlow.VERSION:
        _LOGGER.warning(
            "ANS config entry %s uses a newer schema version (%s.%s) than this integration "
            "supports. This usually happens after downgrading the integration, and the older "
            "version cannot safely read the newer entry data.",
            config_entry.entry_id,
            version,
            minor_version,
        )
        return False

    # At the moment no migration needed for version 2
    if version == 2:
        _LOGGER.info(
            "ANS config entry migration skipped - no migration needed for version %s.%s",
            version,
            minor_version,
        )
        return False

    # Migration logic for version 1 → 2: clamp retry configuration values into safe bounds and enforce max_delay >= base_delay.
    if version == 1:
        new_options = dict(config_entry.options or {})
        # Clamp retry_base_delay into [SYS_MIN_RETRY_BASE_DELAY_SECONDS, SYS_MAX_RETRY_BASE_DELAY_SECONDS]
        if SYS_CONFIG_RETRY_BASE_DELAY_KEY in new_options:
            new_options[SYS_CONFIG_RETRY_BASE_DELAY_KEY] = max(
                SYS_MIN_RETRY_BASE_DELAY_SECONDS,
                min(
                    new_options[SYS_CONFIG_RETRY_BASE_DELAY_KEY],
                    SYS_MAX_RETRY_BASE_DELAY_SECONDS,
                ),
            )
        # Clamp retry_backoff_factor into [SYS_MIN_RETRY_BACKOFF_FACTOR, SYS_MAX_RETRY_BACKOFF_FACTOR]
        if SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY in new_options:
            new_options[SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY] = max(
                SYS_MIN_RETRY_BACKOFF_FACTOR,
                min(
                    new_options[SYS_CONFIG_RETRY_BACKOFF_FACTOR_KEY],
                    float(SYS_MAX_RETRY_BACKOFF_FACTOR),
                ),
            )
        # Clamp retry_max_delay into [SYS_MIN_RETRY_MAX_DELAY_SECONDS, SYS_MAX_RETRY_MAX_DELAY_SECONDS]
        if SYS_CONFIG_RETRY_MAX_DELAY_KEY in new_options:
            new_options[SYS_CONFIG_RETRY_MAX_DELAY_KEY] = max(
                SYS_MIN_RETRY_MAX_DELAY_SECONDS,
                min(
                    new_options[SYS_CONFIG_RETRY_MAX_DELAY_KEY],
                    SYS_MAX_RETRY_MAX_DELAY_SECONDS,
                ),
            )
        # Enforce cross-field constraint: retry_max_delay >= retry_base_delay
        base = new_options.get(SYS_CONFIG_RETRY_BASE_DELAY_KEY)
        max_delay = new_options.get(SYS_CONFIG_RETRY_MAX_DELAY_KEY)
        if base is not None and max_delay is not None and max_delay < base:
            new_options[SYS_CONFIG_RETRY_MAX_DELAY_KEY] = base
        # Rename legacy storage files to the current dot-separated naming scheme
        storage_dir = Path(hass.config.path(".storage"))
        await hass.async_add_executor_job(_do_migrate_storage_files, storage_dir)
        # Update the entry with the new options and version numbers
        hass.config_entries.async_update_entry(
            config_entry,
            options=new_options,
            version=ANSConfigFlow.VERSION,
            minor_version=ANSConfigFlow.MINOR_VERSION,
        )
        # Log the successful migration with old and new version numbers
        _LOGGER.info(
            "Migrated ANS config entry %s from version %s.%s to version %s.%s",
            config_entry.entry_id,
            version,
            minor_version,
            ANSConfigFlow.VERSION,
            ANSConfigFlow.MINOR_VERSION,
        )

    return True


# ---------------------------------------------------------------------------
# Module-level accessors (used by services, diagnostics, etc.)
# ---------------------------------------------------------------------------


def _get_entry_data(hass: HomeAssistant) -> dict | None:
    """Return the main entry's runtime data dict, or None if unavailable."""
    entry = get_main_entry(hass)
    if entry is None:
        return None
    return getattr(entry, "runtime_data", None)


def get_rate_limiter(hass: HomeAssistant) -> RateLimiter | None:
    """Retrieve the rate limiter from the main entry data.

    Returns ``None`` if the integration has not finished setting up.
    """
    entry_data = _get_entry_data(hass)
    if entry_data is None:
        return None
    system: ANSSystem | None = entry_data.get("system")
    return system.rate_limiter if system else None


def get_task_queue(hass: HomeAssistant) -> NotificationDeliveryTaskQueue | None:
    """Retrieve the task queue from the main entry data.

    Returns ``None`` if the integration has not finished setting up.
    """
    entry_data = _get_entry_data(hass)
    if entry_data is None:
        return None
    system: ANSSystem | None = entry_data.get("system")
    return system.task_queue if system else None


def get_channel_manager(hass: HomeAssistant) -> ChannelManager | None:
    """Retrieve the ChannelManager from the main entry data.

    Returns ``None`` if the integration has not finished setting up.
    """
    entry_data = _get_entry_data(hass)
    if entry_data is None:
        return None
    system: ANSSystem | None = entry_data.get("system")
    return system.channel_manager if system else None


def get_config_repository(hass: HomeAssistant) -> ConfigRepository | None:
    """Retrieve the config repository from the main entry data."""
    entry_data = _get_entry_data(hass)
    return entry_data.get("config_repository") if entry_data else None
