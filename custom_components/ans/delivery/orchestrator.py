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

from ..channels.channel_manager import ChannelManager
from ..config.repository import ConfigRepository
from ..models import (
    ConfigSnapshot,
    NotificationDeliveryTask,
    NotificationPayload,
    RecipientType,
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
    ) -> None:
        """Initialize the Orchestrator.

        Args:
            config_repo: Repository for managing configuration data.
            task_queue: Queue for managing asynchronous tasks.
            notification_registry: Notification registry for tracking.
            channel_manager: ChannelManager for live channel and adapter lookups.
            deduplication_service: Optional deduplication service for preventing duplicate deliveries.

        """
        self._config_repo = config_repo
        self._task_queue = task_queue
        self._notification_registry = notification_registry
        self._channel_manager = channel_manager
        self._deduplication_service = deduplication_service

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

            _LOGGER.debug(
                "Recipient '%s': Resolved %d channels for criticality '%s': %s",
                recipient_id,
                len(channels),
                payload.criticality,
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

            tasks.extend(
                self._create_task(
                    snapshot_id=snapshot.snapshot_id,
                    recipient_id=recipient_id,
                    payload=payload,
                    channel=channel,
                    snapshot=snapshot,
                )
                for channel in channels
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

        for task in tasks:
            await self._task_queue.enqueue(task)

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
                "Channel '%s' not found in ChannelManager for recipient '%s'. "
                "Available channels: %s. "
                "This indicates a configuration error or that channels weren't loaded during startup.",
                channel,
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
            if (
                recipient_data.type == RecipientType.TTS
                and recipient_config.tts_settings
            ):
                tts_settings = recipient_config.tts_settings
                _LOGGER.debug(
                    "Task for recipient '%s' includes TTS settings: format=%s, volumes=(%d,%d,%d,%d)",
                    recipient_id,
                    tts_settings.message_format,
                    tts_settings.volume_morning,
                    tts_settings.volume_daytime,
                    tts_settings.volume_evening,
                    tts_settings.volume_night,
                )
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
                    "but is not of type TTS — TTS settings will use defaults. "
                    "Check the recipient configuration.",
                    recipient_id,
                    recipient_data.type.value,
                    channel,
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
