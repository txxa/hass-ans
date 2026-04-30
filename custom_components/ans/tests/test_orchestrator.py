"""Tests for NotificationOrchestrator fan-out logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from custom_components.ans.delivery.orchestrator import NotificationOrchestrator
from custom_components.ans.models import (
    ChannelInfo,
    ChannelScope,
    NotificationType,
    RecipientContactInfo,
    RecipientNotificationPolicy,
    RecipientType,
)

from .conftest import make_payload

# ── Snapshot factory ──────────────────────────────────────────────────────────


def _make_snapshot(
    *,
    recipients: list[str] | None = None,
    channels_per_recipient: dict[str, list[str]] | None = None,
) -> MagicMock:
    """Return a mock ConfigSnapshot."""
    recipients = ["rcpt-1"] if recipients is None else recipients
    channels_per_recipient = (
        {"rcpt-1": ["notify.persistent_notification"]}
        if channels_per_recipient is None
        else channels_per_recipient
    )

    snap = MagicMock()
    snap.snapshot_id = str(uuid4())
    snap.getRecipients.return_value = recipients

    def _resolve_channels(rcpt, crit):
        """Return the configured channel list for the given recipient ID."""
        return channels_per_recipient.get(rcpt, [])

    snap.getRecipientChannels.side_effect = _resolve_channels

    policy = RecipientNotificationPolicy(
        retry_attempts=3,
        rate_limit=100,
        rate_limit_window=60,
        allowed_types=list(NotificationType),
        blocked_sources_regex=None,
    )
    snap.getRecipientNotificationPolicy.return_value = policy
    snap.getRecipientContactInfo.return_value = RecipientContactInfo(
        email_address=None, phone_number=None
    )

    mock_rcpt_data = MagicMock()
    mock_rcpt_data.type = RecipientType.HA_USER
    mock_rcpt_data.email = None
    mock_rcpt_data.phone = None
    snap.recipients = dict.fromkeys(recipients, mock_rcpt_data)

    mock_rcpt_cfg = MagicMock()
    mock_rcpt_cfg.tts_settings = None
    snap.recipient_configs = dict.fromkeys(recipients, mock_rcpt_cfg)

    return snap


def _make_channel_info(channel_id: str) -> ChannelInfo:
    """Return a ChannelInfo for the given channel_id, using SYSTEM scope for persistent_notification channels."""
    return ChannelInfo(
        id=channel_id,
        label=channel_id,
        scope=ChannelScope.SYSTEM
        if channel_id.startswith("notify.persistent")
        else ChannelScope.RECIPIENT,
    )


def _make_orchestrator(
    *,
    snapshot=None,
    recipients: list[str] | None = None,
    channels_per_recipient: dict[str, list[str]] | None = None,
    dedup=None,
):
    """Return a (orchestrator, task_queue, notification_registry) tuple wired with sensible mock defaults."""
    snap = snapshot or _make_snapshot(
        recipients=recipients,
        channels_per_recipient=channels_per_recipient,
    )

    config_repo = MagicMock()
    config_repo.system_config = MagicMock()
    config_repo.snapshot.return_value = snap

    task_queue = MagicMock()
    task_queue.enqueue = AsyncMock()

    notification_registry = MagicMock()
    notification_registry.register_notification = AsyncMock()

    channel_manager = MagicMock()
    channel_manager.count_detected.return_value = 1

    # channel_manager.get_info returns a ChannelInfo for any channel_id
    channel_manager.get_info.side_effect = _make_channel_info
    channel_manager.get_all_infos.return_value = []

    return (
        NotificationOrchestrator(
            config_repo=config_repo,
            task_queue=task_queue,
            notification_registry=notification_registry,
            channel_manager=channel_manager,
            deduplication_service=dedup,
        ),
        task_queue,
        notification_registry,
    )


# ── No system config ──────────────────────────────────────────────────────────


class TestMissingSystemConfig:
    """Verify that the orchestrator drops notifications silently when no system config is present."""

    async def test_drops_notification_when_no_system_config(self):
        """When system_config is None, no task is enqueued and no notification is registered."""
        orch, queue, reg = _make_orchestrator()
        orch._config_repo.system_config = None
        await orch.handle_notification(make_payload())
        queue.enqueue.assert_not_awaited()
        reg.register_notification.assert_not_awaited()


# ── No channels detected ──────────────────────────────────────────────────────


class TestNoChannels:
    """Verify that the orchestrator drops notifications when the channel manager reports no detected channels."""

    async def test_drops_notification_when_no_channels_detected(self):
        """When count_detected() returns 0, no delivery task is enqueued."""
        orch, queue, _ = _make_orchestrator()
        orch._channel_manager.count_detected.return_value = 0
        await orch.handle_notification(make_payload())
        queue.enqueue.assert_not_awaited()

    async def test_drops_notification_when_channel_manager_none(self):
        """When the channel manager reference is None, no delivery task is enqueued."""
        orch, queue, _ = _make_orchestrator()
        orch._channel_manager = None
        await orch.handle_notification(make_payload())
        queue.enqueue.assert_not_awaited()


# ── Fan-out ───────────────────────────────────────────────────────────────────


class TestFanOut:
    """Verify that the orchestrator fans out notifications to the correct number of (recipient, channel) delivery tasks."""

    async def test_single_recipient_single_channel_enqueues_one_task(self):
        """A single (recipient, channel) pair results in exactly one enqueued delivery task."""
        orch, queue, _ = _make_orchestrator(
            recipients=["rcpt-1"],
            channels_per_recipient={"rcpt-1": ["notify.channel_a"]},
        )
        await orch.handle_notification(make_payload())
        queue.enqueue.assert_awaited_once()

    async def test_single_recipient_two_channels_enqueues_two_tasks(self):
        """A single recipient with two channels results in two enqueued delivery tasks."""
        orch, queue, _ = _make_orchestrator(
            recipients=["rcpt-1"],
            channels_per_recipient={"rcpt-1": ["notify.ch_a", "notify.ch_b"]},
        )
        await orch.handle_notification(make_payload())
        assert queue.enqueue.await_count == 2

    async def test_two_recipients_one_channel_each_enqueues_two_tasks(self):
        """Two recipients each with one channel results in exactly two enqueued delivery tasks."""
        orch, queue, _ = _make_orchestrator(
            recipients=["rcpt-1", "rcpt-2"],
            channels_per_recipient={
                "rcpt-1": ["notify.channel_a"],
                "rcpt-2": ["notify.channel_b"],
            },
        )
        await orch.handle_notification(make_payload())
        assert queue.enqueue.await_count == 2

    async def test_no_recipients_enqueues_nothing(self):
        """When there are no configured recipients, no delivery task is enqueued."""
        orch, queue, _ = _make_orchestrator(
            recipients=[],
            channels_per_recipient={},
        )
        await orch.handle_notification(make_payload())
        queue.enqueue.assert_not_awaited()

    async def test_no_channels_for_criticality_skips_recipient(self):
        """A recipient with an empty channel list for the notification criticality is silently skipped."""
        orch, queue, _ = _make_orchestrator(
            recipients=["rcpt-1"],
            channels_per_recipient={"rcpt-1": []},  # empty
        )
        await orch.handle_notification(make_payload())
        queue.enqueue.assert_not_awaited()


# ── Notification registration ─────────────────────────────────────────────────


class TestNotificationRegistration:
    """Verify that register_notification() is called exactly once per notification regardless of how many tasks are enqueued."""

    async def test_registers_notification_once(self):
        """Even with multiple recipients and channels, the notification is registered exactly once."""
        orch, _, reg = _make_orchestrator(
            recipients=["rcpt-1", "rcpt-2"],
            channels_per_recipient={
                "rcpt-1": ["notify.ch_a"],
                "rcpt-2": ["notify.ch_b"],
            },
        )
        await orch.handle_notification(make_payload())
        reg.register_notification.assert_awaited_once()

    async def test_not_registered_when_no_deliverable_recipients(self):
        """When no delivery tasks are enqueued (all channels empty), the notification is not registered."""
        orch, _, reg = _make_orchestrator(
            recipients=["rcpt-1"],
            channels_per_recipient={"rcpt-1": []},
        )
        await orch.handle_notification(make_payload())
        reg.register_notification.assert_not_awaited()


# ── ConfigSnapshot unavailable ────────────────────────────────────────────────


class TestSnapshotUnavailable:
    """Verify that the orchestrator handles config snapshot failures gracefully without raising."""

    async def test_drops_notification_when_snapshot_raises(self):
        """When config_repo.snapshot() raises, the orchestrator drops the notification and no tasks are enqueued."""
        orch, queue, _ = _make_orchestrator()
        orch._config_repo.snapshot.side_effect = RuntimeError("No config")
        await orch.handle_notification(make_payload())
        queue.enqueue.assert_not_awaited()


# ── Deduplication ─────────────────────────────────────────────────────────────


class TestDeduplication:
    """Verify that the orchestrator skips duplicate (notification_id, channel) pairs when a DeduplicationService is wired in."""

    async def test_duplicate_channels_are_skipped(self):
        """A (notification_id, channel) pair already seen by the deduplication service is not enqueued again."""
        dedup = MagicMock()
        # First call: not duplicate; second call: duplicate
        dedup.is_duplicate = AsyncMock(side_effect=[(False, ""), (True, "dup")])

        orch, queue, _ = _make_orchestrator(
            recipients=["rcpt-1"],
            channels_per_recipient={"rcpt-1": ["notify.ch_a", "notify.ch_b"]},
            dedup=dedup,
        )
        await orch.handle_notification(make_payload())
        # Only one task (ch_a) should be enqueued
        assert queue.enqueue.await_count == 1

    async def test_all_unique_channels_are_enqueued(self):
        """When no duplicates are detected, all (recipient, channel) pairs are enqueued."""
        dedup = MagicMock()
        dedup.is_duplicate = AsyncMock(return_value=(False, ""))
        orch, queue, _ = _make_orchestrator(
            recipients=["rcpt-1"],
            channels_per_recipient={"rcpt-1": ["notify.ch_a", "notify.ch_b"]},
            dedup=dedup,
        )
        await orch.handle_notification(make_payload())
        assert queue.enqueue.await_count == 2

    async def test_no_dedup_service_enqueues_all(self):
        """Without a deduplication service, all (recipient, channel) pairs are always enqueued."""
        orch, queue, _ = _make_orchestrator(
            recipients=["rcpt-1"],
            channels_per_recipient={"rcpt-1": ["notify.ch_a", "notify.ch_b"]},
            dedup=None,
        )
        await orch.handle_notification(make_payload())
        assert queue.enqueue.await_count == 2
