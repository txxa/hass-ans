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

from ..config.repository import ConfigRepository
from ..models import (
    ConfigSnapshot,
    NotificationDeliveryTask,
    NotificationPayload,
)
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
    ) -> None:
        """Initialize the Orchestrator.

        Args:
            config_repo: Repository for managing configuration data.
            task_queue: Queue for managing asynchronous tasks.
            notification_registry: Notification registry for tracking.

        """
        self._config_repo = config_repo
        self._task_queue = task_queue
        self._notification_registry = notification_registry

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

        # Validate channel registry is populated (critical error if empty)
        if self._config_repo.channel_registry.count() == 0:
            _LOGGER.error(
                "Cannot process notification %s: No channels registered. "
                "This indicates a system configuration error. "
                "Please check that notify integrations are properly configured.",
                payload.notification_id,
            )
            return

        snapshot = self._snapshot_config()
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
        # return self._channel_selector.resolve(
        #     recipient_id=recipient_id,
        #     payload=payload,
        #     snapshot=snapshot,
        # )

        return snapshot.getRecipientChannels(recipient_id, payload.criticality)

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

        # Resolve channel ID to ChannelInfo from registry
        channel_info = snapshot.channel_registry.get(channel)
        if not channel_info:
            available_channels = snapshot.channel_registry.get_all_ids()
            _LOGGER.error(
                "Channel '%s' not found in registry for recipient '%s'. "
                "Available channels: %s. "
                "This indicates a configuration error or that channels weren't loaded during startup.",
                channel,
                recipient_id,
                available_channels,
            )
            raise ValueError(
                f"Channel '{channel}' not found in channel registry. "
                f"Cannot create delivery task for recipient '{recipient_id}'."
            )

        return NotificationDeliveryTask(
            job_id=uuid4(),
            recipient_id=recipient_id,
            payload=payload,
            channel_info=channel_info,
            policy=policy,
            contact_info=contact_info,
            snapshot_id=snapshot_id,
            created_at=datetime.now(UTC),
        )
