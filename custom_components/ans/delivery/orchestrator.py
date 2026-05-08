"""Notification orchestration and fan-out.

Responsible for:
- snapshotting configuration
- resolving recipients
- resolving channels per recipient
- fan-out task creation
- task enqueueing

Error Handling Policy:
- Missing channel in registry: HARD FAILURE (orchestrator validates before task creation)
- Missing adapter for channel: PERMANENT FAILURE (processor logs and marks as failed)

This ensures:
- Configuration errors are caught early (orchestrator)
- Runtime adapter issues don't crash processing (processor)
- Users get clear feedback about misconfigured channels
"""

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later

from ..channels.channel_manager import ChannelManager
from ..config.repository import ConfigRepository
from ..const import (
    EVENT_NOTIFICATION_SETTLED,
)
from ..models import (
    ConfigSnapshot,
    NotificationDeliveryTask,
    NotificationPayload,
    RecipientType,
    TaskOutcome,
)
from .deduplication import DeduplicationService
from .queue import NotificationDeliveryTaskQueue

_LOGGER = logging.getLogger(__name__)


class NotificationOrchestrator:
    """Creates delivery tasks from a notification payload. Entry‑point coordinator.

    Responsibilities:
    - snapshot configuration
    - resolve recipients
    - resolve channels per recipient
    - fan‑out one task per (recipient × channel)
    - enqueue tasks

    Non‑responsibilities:
    - filtering
    - delivery
    - retries
    - rate limiting
    """

    def __init__(
        self,
        config_repo: ConfigRepository,
        task_queue: NotificationDeliveryTaskQueue,
        notification_registry,
        channel_manager: ChannelManager,
        deduplication_service: DeduplicationService | None = None,
        hass: HomeAssistant | None = None,
        settled_ttl_seconds: int = 3600,
    ) -> None:
        """Initialize the Orchestrator.

        Args:
            config_repo: Repository for managing configuration data.
            task_queue: Queue for managing asynchronous tasks.
            notification_registry: Notification registry for tracking.
            channel_manager: ChannelManager for live channel and adapter lookups.
            deduplication_service: Optional deduplication service for preventing duplicate deliveries.
            hass: HomeAssistant instance, required for firing settled events.
            settled_ttl_seconds: Seconds before an unsettled tracking entry is
                evicted. Should be ``RCPT_MAX_RETRY_ATTEMPTS * retry_max_delay``
                so it always exceeds the worst-case retry schedule.

        """
        self._config_repo = config_repo
        self._task_queue = task_queue
        self._notification_registry = notification_registry
        self._channel_manager = channel_manager
        self._deduplication_service = deduplication_service
        self._hass = hass
        self._settled_ttl_seconds = settled_ttl_seconds

        # {notification_id: {"expected": int, "delivered": int, "failed": int,
        #                    "filtered": int, "cancel_ttl": Callable,
        #                    "total_recipients": int,
        #                    "recipients": {recipient_id: {"channels_total": int,
        #                                                  "channels_delivered": int,
        #                                                  "channels_failed": int,
        #                                                  "channels_filtered": int}}}}
        self._tracking: dict[str, dict] = {}

    async def handle_notification(self, payload: NotificationPayload) -> None:
        """Handle notification orchestration.

        Called exactly once per semantic notification.
        """
        # Check if system_config is loaded before processing
        # (it might be temporarily unavailable during config reload)
        if not self._config_repo.system_config:
            _LOGGER.error(
                "Cannot process notification %s: System config not loaded. "
                "This indicates a system configuration error.",
                payload.notification_id,
            )
            return

        # Validate channels are populated (critical error if none detected)
        if self._channel_manager is None or self._channel_manager.count_detected() == 0:
            _LOGGER.error(
                "Cannot process notification %s: No channels registered. "
                "This indicates a system configuration error. "
                "Please check that notify integrations are properly configured.",
                payload.notification_id,
            )
            return

        try:
            snapshot = self._snapshot_config()
        except RuntimeError:
            _LOGGER.error(
                "Cannot process notification %s: Config snapshot unavailable.",
                payload.notification_id,
            )
            return
        recipients = list(self._resolve_recipients(snapshot))
        _LOGGER.debug(
            "Notification %s: Resolved %d recipients: %s",
            payload.notification_id,
            len(recipients),
            recipients,
        )

        # Log warning if no recipients configured
        if not recipients:
            _LOGGER.warning(
                "No recipients configured for notification %s. "
                "Notification will be dropped.",
                payload.notification_id,
            )
            return

        # Collect recipients with their channels
        recipients_data = []
        tasks: list[NotificationDeliveryTask] = []

        for recipient_id in recipients:
            channels = list(self._resolve_channels(recipient_id, payload, snapshot))

            if channels:
                _LOGGER.debug(
                    "Recipient '%s': Resolved %d channels for criticality '%s' "
                    "(notification_id=%s): %s",
                    recipient_id,
                    len(channels),
                    payload.criticality,
                    payload.notification_id,
                    channels,
                )

            # Log warning if no channels configured for this criticality level
            if not channels:
                _LOGGER.warning(
                    "No channels configured for recipient '%s' at criticality '%s'. "
                    "Skipping delivery for notification %s.",
                    recipient_id,
                    payload.criticality,
                    payload.notification_id,
                )
                continue

            # Apply deduplication if enabled
            if self._deduplication_service:
                channels = await self._deduplicate_channels(
                    payload.notification_id, channels
                )
            else:
                _LOGGER.debug(
                    "Deduplication disabled for notification_id=%s recipient='%s'",
                    payload.notification_id,
                    recipient_id,
                )

                # Check if all channels were deduplicated
                if not channels:
                    _LOGGER.debug(
                        "All channels for recipient '%s' were deduplicated for notification %s",
                        recipient_id,
                        payload.notification_id,
                    )
                    # Still track recipient even if all channels deduplicated
                    recipients_data.append(
                        {
                            "recipient_id": recipient_id,
                            "channels": [],  # No channels after deduplication
                        }
                    )
                    continue

            # Track recipients and channels for registration
            recipients_data.append(
                {
                    "recipient_id": recipient_id,
                    "channels": channels,  # Already strings
                }
            )

            # Log TTS settings once per recipient (not per channel — avoids duplicate
            # lines when a TTS recipient has more than one channel assigned)
            _r_data = snapshot.recipients.get(recipient_id)
            _r_config = snapshot.recipient_configs.get(recipient_id)
            if _r_data and _r_config and _r_data.type == RecipientType.TTS:
                if _r_config.tts_settings:
                    _tts = _r_config.tts_settings
                    _LOGGER.debug(
                        "Task for recipient '%s' includes TTS settings: "
                        "notification_id=%s format=%s volumes=(%d,%d,%d,%d)",
                        recipient_id,
                        payload.notification_id,
                        _tts.message_format,
                        _tts.volume_morning,
                        _tts.volume_daytime,
                        _tts.volume_evening,
                        _tts.volume_night,
                    )
                else:
                    _LOGGER.warning(
                        "TTS recipient '%s' has no tts_settings configured "
                        "(notification_id=%s) — "
                        "delivery will use system defaults. Reconfigure via the ANS UI.",
                        recipient_id,
                        payload.notification_id,
                    )

            for channel in channels:
                try:
                    task = self._create_task(
                        snapshot_id=snapshot.snapshot_id,
                        recipient_id=recipient_id,
                        payload=payload,
                        channel=channel,
                        snapshot=snapshot,
                    )
                    tasks.append(task)
                except ValueError:
                    _LOGGER.error(
                        "Task creation failed: notification_id=%s recipient='%s' "
                        "channel='%s' — %d task(s) already assembled will still be enqueued",
                        payload.notification_id,
                        recipient_id,
                        channel,
                        len(tasks),
                    )

        # Register notification ONCE before fan-out
        if recipients_data:
            await self._notification_registry.register_notification(
                notification_id=payload.notification_id,
                source=payload.source,
                triggered_at=payload.created_at,
                payload={
                    "title": payload.title,
                    "message": payload.message,
                    "type": payload.type.value,
                    "criticality": payload.criticality.value,
                    "metadata": payload.metadata,
                },
                recipients=recipients_data,
            )

        _LOGGER.debug(
            "ANS fan‑out created %d delivery tasks for notification %s",
            len(tasks),
            payload.notification_id,
        )

        _LOGGER.info(
            "Enqueueing %d delivery task(s) for notification_id=%s",
            len(tasks),
            payload.notification_id,
        )
        for task in tasks:
            await self._task_queue.enqueue(task)
        _LOGGER.debug(
            "Fan-out complete: all %d task(s) enqueued for notification_id=%s",
            len(tasks),
            payload.notification_id,
        )

        if tasks and self._hass is not None:
            self._register_tracking(payload.notification_id, tasks)

    # ------------------------------------------------------------------
    # Settled-event tracking
    # ------------------------------------------------------------------

    def _register_tracking(
        self, notification_id: str, tasks: list[NotificationDeliveryTask]
    ) -> None:
        """Register a notification for settled-event tracking."""

        def _on_ttl(_now: datetime) -> None:
            if notification_id in self._tracking:
                _LOGGER.warning(
                    "Settled-event TTL expired for notification_id=%s — "
                    "not all tasks reached a terminal state within %d seconds. "
                    "Evicting tracking entry without firing settled event.",
                    notification_id,
                    self._settled_ttl_seconds,
                )
                del self._tracking[notification_id]

        recipients_map: dict[str, dict] = {}
        for task in tasks:
            rid = task.recipient_id
            if rid not in recipients_map:
                recipients_map[rid] = {
                    "channels_total": 0,
                    "channels_delivered": 0,
                    "channels_failed": 0,
                    "channels_filtered": 0,
                }
            recipients_map[rid]["channels_total"] += 1

        cancel_ttl = async_call_later(self._hass, self._settled_ttl_seconds, _on_ttl)
        self._tracking[notification_id] = {
            "expected": len(tasks),
            "delivered": 0,
            "failed": 0,
            "filtered": 0,
            "cancel_ttl": cancel_ttl,
            "total_recipients": len(recipients_map),
            "recipients": recipients_map,
        }

    def on_task_terminal(
        self, notification_id: str, outcome_key: TaskOutcome, recipient_id: str
    ) -> None:
        """Receive a terminal task outcome from the processor and check settlement.

        Args:
            notification_id: The notification ID of the settled task.
            outcome_key: One of :attr:`TaskOutcome.DELIVERED`, :attr:`TaskOutcome.FAILED`,
                or :attr:`TaskOutcome.FILTERED`.
            recipient_id: The recipient ID of the settled task.

        """
        if notification_id not in self._tracking:
            return
        self._tracking[notification_id][outcome_key] += 1
        recipient_stats = self._tracking[notification_id]["recipients"].get(
            recipient_id
        )
        if recipient_stats is not None:
            recipient_stats[f"channels_{outcome_key}"] += 1
        self._check_settled(notification_id)

    def _check_settled(self, notification_id: str) -> None:
        """Fire ans_notification_settled if all tasks have reached a terminal state."""
        entry = self._tracking.get(notification_id)
        if entry is None:
            return
        total_terminal = entry["delivered"] + entry["failed"] + entry["filtered"]
        if total_terminal < entry["expected"]:
            return

        entry["cancel_ttl"]()
        del self._tracking[notification_id]

        recipients_delivered = sum(
            1 for r in entry["recipients"].values() if r["channels_delivered"] > 0
        )
        self._hass.bus.async_fire(
            EVENT_NOTIFICATION_SETTLED,
            {
                "notification_id": notification_id,
                "total_tasks": entry["expected"],
                "total_recipients": entry["total_recipients"],
                "delivered": entry["delivered"],
                "failed": entry["failed"],
                "filtered": entry["filtered"],
                "recipients_delivered": recipients_delivered,
                "recipients": {
                    rid: dict(stats) for rid, stats in entry["recipients"].items()
                },
            },
        )
        _LOGGER.debug(
            "Notification settled: notification_id=%s delivered=%d failed=%d filtered=%d",
            notification_id,
            entry["delivered"],
            entry["failed"],
            entry["filtered"],
        )

    async def stop(self) -> None:
        """Cancel pending TTL timers and clear tracking state."""
        for entry in self._tracking.values():
            entry["cancel_ttl"]()
        self._tracking.clear()

    # ------------------------------------------------------------------
    # Snapshotting
    # ------------------------------------------------------------------

    def _snapshot_config(self):
        """Freeze configuration for deterministic behavior.

        Returns an immutable snapshot object.
        """
        return self._config_repo.snapshot()

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------

    def _resolve_recipients(
        self,
        snapshot: ConfigSnapshot,
    ) -> Iterable[str]:
        """Resolve logical recipients for the notification.

        This may expand groups, areas, or dynamic selectors.
        """
        # intentionally simple for v1
        return snapshot.getRecipients()

    def _resolve_channels(
        self,
        recipient_id: str,
        payload: NotificationPayload,
        snapshot: ConfigSnapshot,
    ) -> Iterable[str]:
        """Resolve delivery channels for a recipient.

        This is logical routing only — no endpoints.
        """
        return snapshot.getRecipientChannels(recipient_id, payload.criticality)

    async def _deduplicate_channels(
        self, notification_id: str, channels: list[str]
    ) -> list[str]:
        """Remove duplicate channels using deduplication service.

        Args:
            notification_id: Notification identifier.
            channels: List of channel IDs to check.

        Returns:
            Filtered list of channel IDs (non-duplicates only).

        """
        if not self._deduplication_service:
            return channels

        non_duplicate_channels = []
        for channel_id in channels:
            is_duplicate, reason = await self._deduplication_service.is_duplicate(
                notification_id, channel_id
            )

            if is_duplicate:
                _LOGGER.debug(
                    "Deduplication: Skipping channel '%s' for notification %s: %s",
                    channel_id,
                    notification_id,
                    reason,
                )
            else:
                non_duplicate_channels.append(channel_id)

        if len(non_duplicate_channels) < len(channels):
            _LOGGER.info(
                "Deduplication: Filtered %d/%d channels for notification %s",
                len(channels) - len(non_duplicate_channels),
                len(channels),
                notification_id,
            )

        return non_duplicate_channels

    # ------------------------------------------------------------------
    # Task construction
    # ------------------------------------------------------------------

    def _create_task(
        self,
        snapshot_id: str,
        recipient_id: str,
        payload: NotificationPayload,
        channel: str,
        snapshot: ConfigSnapshot,
    ) -> NotificationDeliveryTask:
        """Create one immutable delivery task.

        Args:
            snapshot_id: ID of the config snapshot for this task
            recipient_id: Recipient identifier
            payload: Notification payload
            channel: Delivery channel ID
            snapshot: Config snapshot containing policy and contact info

        """
        policy = snapshot.getRecipientNotificationPolicy(recipient_id)
        contact_info = snapshot.getRecipientContactInfo(recipient_id)

        # Resolve channel ID to ChannelInfo via live lookup (ChannelInfo is
        # frozen and safe to read without snapshotting).
        channel_manager = self._channel_manager
        if channel_manager is None:
            raise ValueError(
                f"Channel '{channel}' not found: ChannelManager is not initialized."
            )
        channel_info = channel_manager.get_info(channel)
        if not channel_info:
            available_channels = [info.id for info in channel_manager.get_all_infos()]
            _LOGGER.error(
                "Channel '%s' not found in ChannelManager: notification_id=%s "
                "recipient='%s'. Available channels: %s. "
                "This indicates a configuration error or channels were not loaded at startup.",
                channel,
                payload.notification_id,
                recipient_id,
                available_channels,
            )
            raise ValueError(
                f"Channel '{channel}' not found in ChannelManager. "
                f"Cannot create delivery task for recipient '{recipient_id}'."
            )

        # Extract TTS settings for TTS recipients
        # These settings are per-recipient and enable different TTS recipients
        # to have different volume/format settings for the same notification
        tts_settings = None
        recipient_data = snapshot.recipients.get(recipient_id)
        recipient_config = snapshot.recipient_configs.get(recipient_id)

        if recipient_data and recipient_config:
            if recipient_data.type == RecipientType.TTS:
                if recipient_config.tts_settings:
                    tts_settings = recipient_config.tts_settings
            elif (
                channel.startswith("media_player.")
                and recipient_data.type != RecipientType.TTS
            ):
                # A non-TTS recipient routed to a media_player.* channel will
                # deliver with default TTS settings (volume, format), which may
                # not match the user's intent. This indicates a misconfiguration
                # that should be corrected in the recipient's channel assignment.
                _LOGGER.warning(
                    "Recipient '%s' (type=%s) is assigned media_player channel '%s' "
                    "(notification_id=%s) but is not of type TTS — "
                    "TTS settings will use defaults. "
                    "Check the recipient configuration.",
                    recipient_id,
                    recipient_data.type.value,
                    channel,
                    payload.notification_id,
                )

        return NotificationDeliveryTask(
            job_id=uuid4(),
            recipient_id=recipient_id,
            payload=payload,
            channel_info=channel_info,
            policy=policy,
            contact_info=contact_info,
            tts_settings=tts_settings,  # Include TTS settings in task
            snapshot_id=snapshot_id,
            created_at=datetime.now(UTC),
        )
