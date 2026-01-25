"""Notification orchestration and fan-out.

Responsible for:
- snapshotting configuration
- resolving recipients
- resolving channels per recipient
- fan-out task creation
- task enqueueing
"""

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import uuid4

from .config_repository import ConfigRepository
from .models import (
    ChannelInfo,
    ChannelScope,
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
    ) -> None:
        """Initialize the Orchestrator.

        Args:
            config_repo: Repository for managing configuration data.
            channel_selector: Service for selecting communication channels.
            task_queue: Queue for managing asynchronous tasks.

        """
        self._config_repo = config_repo
        self._task_queue = task_queue

    async def handle_notification(self, payload: NotificationPayload) -> None:
        """Handle notification orchestration.

        Called exactly once per semantic notification.
        """
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
            _LOGGER.warning(
                "Channel '%s' not found in registry, creating minimal ChannelInfo",
                channel,
            )
            # Fallback: create a minimal ChannelInfo for unknown channels
            channel_info = ChannelInfo(
                id=channel,
                label=channel,
                scope=ChannelScope.RECIPIENT_SPECIFIC,  # Safe default
                integration=None,
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
