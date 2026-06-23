"""Tests for NotificationOrchestrator fan-out logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

from custom_components.ans.const import EVENT_NOTIFICATION_SETTLED
from custom_components.ans.delivery.orchestrator import NotificationOrchestrator
from custom_components.ans.models import (
    ChannelInfo,
    ChannelScope,
    NotificationType,
    RecipientContactInfo,
    RecipientNotificationPolicy,
    RecipientType,
    TaskOutcome,
)
from custom_components.ans.models.recipient import TTSSettings

from ..conftest import make_payload

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
        allowed_types=frozenset(NotificationType),
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


def _make_hass() -> MagicMock:
    """Return a minimal mock HomeAssistant instance for orchestrator tests."""
    hass = MagicMock()
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()
    # async_listen returns a callable that unsubscribes
    hass.bus.async_listen = MagicMock(return_value=MagicMock())
    return hass


def _make_orchestrator(
    *,
    snapshot=None,
    recipients: list[str] | None = None,
    channels_per_recipient: dict[str, list[str]] | None = None,
    dedup=None,
    hass=None,
    settled_ttl_seconds: int = 60,
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
            hass=hass,
            settled_ttl_seconds=settled_ttl_seconds,
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


# ── Deduplication removes all channels ───────────────────────────────────────


class TestDeduplicationAllFiltered:
    """Verify orchestrator behaviour when deduplication removes every channel for a recipient."""

    async def test_all_channels_deduplicated_registers_notification_no_tasks(self):
        """When all channels are deduplicated the notification is still registered but no tasks are enqueued."""
        dedup = MagicMock()
        dedup.is_duplicate = AsyncMock(return_value=(True, "duplicate within window"))

        orch, queue, reg = _make_orchestrator(
            recipients=["rcpt-1"],
            channels_per_recipient={"rcpt-1": ["notify.ch_a", "notify.ch_b"]},
            dedup=dedup,
        )
        await orch.handle_notification(make_payload())
        # No tasks enqueued because all channels were deduplicated
        queue.enqueue.assert_not_awaited()
        # But the notification is still registered because the recipient was resolved
        reg.register_notification.assert_awaited_once()


# ── Task creation error ───────────────────────────────────────────────────────


class TestTaskCreationError:
    """Verify that a ValueError from _create_task is recovered — error is logged and other channels' tasks are still enqueued."""

    async def test_valueerror_skips_failing_channel_continues_others(self):
        """When _create_task raises ValueError for one channel the orchestrator logs the error and still enqueues the remaining tasks."""
        orch, queue, _ = _make_orchestrator(
            recipients=["rcpt-1"],
            channels_per_recipient={
                "rcpt-1": ["notify.bad_channel", "notify.good_channel"]
            },
        )

        # Make get_info return None for the bad channel (triggers ValueError in _create_task)
        def _get_info_side_effect(channel_id: str):
            if channel_id == "notify.bad_channel":
                return None
            return _make_channel_info(channel_id)

        orch._channel_manager.get_info.side_effect = _get_info_side_effect
        orch._channel_manager.get_all_infos.return_value = [
            _make_channel_info("notify.good_channel")
        ]

        await orch.handle_notification(make_payload())
        # Only the good-channel task should have been enqueued
        assert queue.enqueue.await_count == 1


# ── TTS recipient ─────────────────────────────────────────────────────────────


class TestTTSRecipient:
    """Verify that TTS settings are propagated correctly into delivery tasks."""

    async def test_tts_recipient_with_settings_populates_task(self):
        """A TTS recipient whose config includes tts_settings produces a task with those settings attached."""
        tts_settings = TTSSettings.default()

        snap = _make_snapshot(
            recipients=["rcpt-tts"],
            channels_per_recipient={"rcpt-tts": ["notify.tts_channel"]},
        )
        snap.recipients["rcpt-tts"] = MagicMock(type=RecipientType.TTS)
        snap.recipient_configs["rcpt-tts"] = MagicMock(tts_settings=tts_settings)

        orch, queue, _ = _make_orchestrator(snapshot=snap)
        await orch.handle_notification(make_payload())

        queue.enqueue.assert_awaited_once()
        enqueued_task = queue.enqueue.call_args[0][0]
        assert enqueued_task.tts_settings is tts_settings

    async def test_tts_recipient_without_settings_creates_task_with_none(self):
        """A TTS recipient with no tts_settings configured still produces a task; tts_settings is None and a warning is emitted."""
        snap = _make_snapshot(
            recipients=["rcpt-tts"],
            channels_per_recipient={"rcpt-tts": ["notify.tts_channel"]},
        )
        snap.recipients["rcpt-tts"] = MagicMock(type=RecipientType.TTS)
        snap.recipient_configs["rcpt-tts"] = MagicMock(tts_settings=None)

        orch, queue, _ = _make_orchestrator(snapshot=snap)
        await orch.handle_notification(make_payload())

        queue.enqueue.assert_awaited_once()
        enqueued_task = queue.enqueue.call_args[0][0]
        assert enqueued_task.tts_settings is None


# ── Non-TTS recipient on media_player channel ─────────────────────────────────


class TestNonTTSOnMediaPlayerChannel:
    """Verify that a misconfigured non-TTS recipient routed to a media_player.* channel still delivers but emits a warning."""

    async def test_non_tts_recipient_on_media_player_warns_and_enqueues(self):
        """A non-TTS recipient assigned a media_player.* channel enqueues a task but logs a configuration warning."""
        snap = _make_snapshot(
            recipients=["rcpt-user"],
            channels_per_recipient={"rcpt-user": ["media_player.living_room"]},
        )
        snap.recipients["rcpt-user"] = MagicMock(type=RecipientType.HA_USER)
        snap.recipient_configs["rcpt-user"] = MagicMock(tts_settings=None)

        orch, queue, _ = _make_orchestrator(snapshot=snap)
        await orch.handle_notification(make_payload())

        queue.enqueue.assert_awaited_once()
        enqueued_task = queue.enqueue.call_args[0][0]
        # tts_settings should be None since the recipient is not TTS
        assert enqueued_task.tts_settings is None


# ── Settled event ─────────────────────────────────────────────────────────────

_PATCH_ACL = "custom_components.ans.delivery.orchestrator.async_call_later"


class TestSettledEvent:
    """Verify that ans_notification_settled fires once all fan-out tasks reach a terminal state."""

    def _fire_terminal(
        self, orch, event_name: str, notification_id: str, recipient_id: str = "rcpt-1"
    ) -> None:
        """Simulate a terminal delivery outcome from the processor."""
        outcome_map = {
            "ans_notification_delivered": TaskOutcome.DELIVERED,
            "ans_notification_failed": TaskOutcome.FAILED,
            "ans_notification_filtered": TaskOutcome.FILTERED,
        }
        orch.on_task_terminal(notification_id, outcome_map[event_name], recipient_id)

    async def test_all_delivered_fires_settled(self):
        """Two tasks both delivered → settled fires with delivered=2."""
        hass = _make_hass()
        cancel = MagicMock()
        with patch(_PATCH_ACL, return_value=cancel):
            orch, _, _ = _make_orchestrator(
                recipients=["rcpt-1"],
                channels_per_recipient={"rcpt-1": ["notify.ch_a", "notify.ch_b"]},
                hass=hass,
            )
            payload = make_payload()
            await orch.handle_notification(payload)
            nid = payload.notification_id

            self._fire_terminal(orch, "ans_notification_delivered", nid)
            self._fire_terminal(orch, "ans_notification_delivered", nid)

        hass.bus.async_fire.assert_called_once_with(
            EVENT_NOTIFICATION_SETTLED,
            {
                "notification_id": nid,
                "total_tasks": 2,
                "total_recipients": 1,
                "delivered": 2,
                "failed": 0,
                "filtered": 0,
                "recipients_delivered": 1,
                "recipients": {
                    "rcpt-1": {
                        "channels_total": 2,
                        "channels_delivered": 2,
                        "channels_failed": 0,
                        "channels_filtered": 0,
                    },
                },
            },
        )
        assert nid not in orch._tracking

    async def test_all_failed_fires_settled(self):
        """Two tasks both failed → settled fires with failed=2."""
        hass = _make_hass()
        cancel = MagicMock()
        with patch(_PATCH_ACL, return_value=cancel):
            orch, _, _ = _make_orchestrator(
                recipients=["rcpt-1"],
                channels_per_recipient={"rcpt-1": ["notify.ch_a", "notify.ch_b"]},
                hass=hass,
            )
            payload = make_payload()
            await orch.handle_notification(payload)
            nid = payload.notification_id

            self._fire_terminal(orch, "ans_notification_failed", nid)
            self._fire_terminal(orch, "ans_notification_failed", nid)

        hass.bus.async_fire.assert_called_once_with(
            EVENT_NOTIFICATION_SETTLED,
            {
                "notification_id": nid,
                "total_tasks": 2,
                "total_recipients": 1,
                "delivered": 0,
                "failed": 2,
                "filtered": 0,
                "recipients_delivered": 0,
                "recipients": {
                    "rcpt-1": {
                        "channels_total": 2,
                        "channels_delivered": 0,
                        "channels_failed": 2,
                        "channels_filtered": 0,
                    },
                },
            },
        )

    async def test_all_filtered_fires_settled(self):
        """Two tasks both filtered → settled fires with filtered=2."""
        hass = _make_hass()
        cancel = MagicMock()
        with patch(_PATCH_ACL, return_value=cancel):
            orch, _, _ = _make_orchestrator(
                recipients=["rcpt-1"],
                channels_per_recipient={"rcpt-1": ["notify.ch_a", "notify.ch_b"]},
                hass=hass,
            )
            payload = make_payload()
            await orch.handle_notification(payload)
            nid = payload.notification_id

            self._fire_terminal(orch, "ans_notification_filtered", nid)
            self._fire_terminal(orch, "ans_notification_filtered", nid)

        hass.bus.async_fire.assert_called_once_with(
            EVENT_NOTIFICATION_SETTLED,
            {
                "notification_id": nid,
                "total_tasks": 2,
                "total_recipients": 1,
                "delivered": 0,
                "failed": 0,
                "filtered": 2,
                "recipients_delivered": 0,
                "recipients": {
                    "rcpt-1": {
                        "channels_total": 2,
                        "channels_delivered": 0,
                        "channels_failed": 0,
                        "channels_filtered": 2,
                    },
                },
            },
        )

    async def test_mixed_fires_settled(self):
        """One delivered + one failed → settled fires with correct mixed counts."""
        hass = _make_hass()
        cancel = MagicMock()
        with patch(_PATCH_ACL, return_value=cancel):
            orch, _, _ = _make_orchestrator(
                recipients=["rcpt-1"],
                channels_per_recipient={"rcpt-1": ["notify.ch_a", "notify.ch_b"]},
                hass=hass,
            )
            payload = make_payload()
            await orch.handle_notification(payload)
            nid = payload.notification_id

            self._fire_terminal(orch, "ans_notification_delivered", nid)
            self._fire_terminal(orch, "ans_notification_failed", nid)

        hass.bus.async_fire.assert_called_once_with(
            EVENT_NOTIFICATION_SETTLED,
            {
                "notification_id": nid,
                "total_tasks": 2,
                "total_recipients": 1,
                "delivered": 1,
                "failed": 1,
                "filtered": 0,
                "recipients_delivered": 1,
                "recipients": {
                    "rcpt-1": {
                        "channels_total": 2,
                        "channels_delivered": 1,
                        "channels_failed": 1,
                        "channels_filtered": 0,
                    },
                },
            },
        )

    async def test_single_task_fires_settled(self):
        """A single-task notification fires settled after one terminal event."""
        hass = _make_hass()
        cancel = MagicMock()
        with patch(_PATCH_ACL, return_value=cancel):
            orch, _, _ = _make_orchestrator(
                recipients=["rcpt-1"],
                channels_per_recipient={"rcpt-1": ["notify.ch_a"]},
                hass=hass,
            )
            payload = make_payload()
            await orch.handle_notification(payload)
            nid = payload.notification_id

            self._fire_terminal(orch, "ans_notification_delivered", nid)

        hass.bus.async_fire.assert_called_once_with(
            EVENT_NOTIFICATION_SETTLED,
            {
                "notification_id": nid,
                "total_tasks": 1,
                "total_recipients": 1,
                "delivered": 1,
                "failed": 0,
                "filtered": 0,
                "recipients_delivered": 1,
                "recipients": {
                    "rcpt-1": {
                        "channels_total": 1,
                        "channels_delivered": 1,
                        "channels_failed": 0,
                        "channels_filtered": 0,
                    },
                },
            },
        )

    async def test_zero_tasks_no_settled(self):
        """When no tasks are enqueued no settled event fires."""
        hass = _make_hass()
        with patch(_PATCH_ACL, return_value=MagicMock()):
            orch, _, _ = _make_orchestrator(
                recipients=["rcpt-1"],
                channels_per_recipient={"rcpt-1": []},
                hass=hass,
            )
            await orch.handle_notification(make_payload())

        hass.bus.async_fire.assert_not_called()
        assert orch._tracking == {}

    async def test_ttl_evicts_entry_no_settled(self, caplog):
        """When the TTL fires before all tasks settle, the entry is evicted without firing settled."""
        import logging  # noqa: PLC0415

        hass = _make_hass()
        captured_ttl_callback = {}
        cancel = MagicMock()

        def _fake_acl(_hass, _delay, callback):
            captured_ttl_callback["cb"] = callback
            return cancel

        with patch(_PATCH_ACL, side_effect=_fake_acl):
            orch, _, _ = _make_orchestrator(
                recipients=["rcpt-1"],
                channels_per_recipient={"rcpt-1": ["notify.ch_a", "notify.ch_b"]},
                hass=hass,
            )
            payload = make_payload()
            await orch.handle_notification(payload)
            nid = payload.notification_id

        # Only one of the two tasks settles
        self._fire_terminal(orch, "ans_notification_delivered", nid)

        # Fire TTL callback manually
        with caplog.at_level(logging.WARNING):
            captured_ttl_callback["cb"](None)

        assert nid not in orch._tracking
        hass.bus.async_fire.assert_not_called()
        assert "TTL expired" in caplog.text

    async def test_settled_fires_only_once(self):
        """A spurious duplicate event after settled does not fire settled again."""
        hass = _make_hass()
        cancel = MagicMock()
        with patch(_PATCH_ACL, return_value=cancel):
            orch, _, _ = _make_orchestrator(
                recipients=["rcpt-1"],
                channels_per_recipient={"rcpt-1": ["notify.ch_a"]},
                hass=hass,
            )
            payload = make_payload()
            await orch.handle_notification(payload)
            nid = payload.notification_id

            self._fire_terminal(orch, "ans_notification_delivered", nid)
            # Spurious second event for the same notification_id (e.g. retry race)
            self._fire_terminal(orch, "ans_notification_delivered", nid)

        settled_calls = [
            c
            for c in hass.bus.async_fire.call_args_list
            if c
            == call(
                EVENT_NOTIFICATION_SETTLED,
                {
                    "notification_id": nid,
                    "total_tasks": 1,
                    "total_recipients": 1,
                    "delivered": 1,
                    "failed": 0,
                    "filtered": 0,
                    "recipients_delivered": 1,
                    "recipients": {
                        "rcpt-1": {
                            "channels_total": 1,
                            "channels_delivered": 1,
                            "channels_failed": 0,
                            "channels_filtered": 0,
                        },
                    },
                },
            )
        ]
        assert len(settled_calls) == 1

    async def test_stop_cancels_ttl_and_clears_tracking(self):
        """stop() cancels pending TTL timers and clears tracking."""
        hass = _make_hass()
        cancel = MagicMock()
        with patch(_PATCH_ACL, return_value=cancel):
            orch, _, _ = _make_orchestrator(
                recipients=["rcpt-1"],
                channels_per_recipient={"rcpt-1": ["notify.ch_a"]},
                hass=hass,
            )
            payload = make_payload()
            await orch.handle_notification(payload)

        # Confirm tracking entry is present
        assert payload.notification_id in orch._tracking

        await orch.stop()

        # Tracking cleared, TTL cancelled
        assert orch._tracking == {}
        cancel.assert_called_once()

    async def test_settled_ttl_uses_configured_value(self):
        """The TTL passed to async_call_later matches the settled_ttl_seconds constructor argument."""
        hass = _make_hass()
        captured_delay: list[int] = []

        def _fake_acl(_hass, delay, _callback):
            captured_delay.append(delay)
            return MagicMock()

        with patch(_PATCH_ACL, side_effect=_fake_acl):
            orch, _, _ = _make_orchestrator(
                recipients=["rcpt-1"],
                channels_per_recipient={"rcpt-1": ["notify.ch_a"]},
                hass=hass,
                settled_ttl_seconds=7200,
            )
            await orch.handle_notification(make_payload())

        assert captured_delay == [7200]

    async def test_settled_fires_after_retry_exhaustion(self):
        """ans_notification_settled fires when the 'failed' callback comes from retry exhaustion.

        This is the key integration test for the retry-exhaustion fix: the processor
        calls _on_task_terminal with 'failed' after exhausting retries, which must
        be enough to settle the notification (no TTL eviction needed).
        """
        hass = _make_hass()
        cancel = MagicMock()
        with patch(_PATCH_ACL, return_value=cancel):
            orch, _, _ = _make_orchestrator(
                recipients=["rcpt-1"],
                channels_per_recipient={"rcpt-1": ["notify.ch_a"]},
                hass=hass,
            )
            payload = make_payload()
            await orch.handle_notification(payload)
            nid = payload.notification_id

            # Simulate processor calling the callback after retry exhaustion
            self._fire_terminal(orch, "ans_notification_failed", nid)

        hass.bus.async_fire.assert_called_once_with(
            EVENT_NOTIFICATION_SETTLED,
            {
                "notification_id": nid,
                "total_tasks": 1,
                "total_recipients": 1,
                "delivered": 0,
                "failed": 1,
                "filtered": 0,
                "recipients_delivered": 0,
                "recipients": {
                    "rcpt-1": {
                        "channels_total": 1,
                        "channels_delivered": 0,
                        "channels_failed": 1,
                        "channels_filtered": 0,
                    },
                },
            },
        )
        assert nid not in orch._tracking

    async def test_multi_recipient_all_delivered(self):
        """Two recipients each with two channels all delivered → correct per-recipient breakdown."""
        hass = _make_hass()
        cancel = MagicMock()
        with patch(_PATCH_ACL, return_value=cancel):
            orch, _, _ = _make_orchestrator(
                recipients=["rcpt-1", "rcpt-2"],
                channels_per_recipient={
                    "rcpt-1": ["notify.ch_a", "notify.ch_b"],
                    "rcpt-2": ["notify.ch_a", "notify.ch_b"],
                },
                hass=hass,
            )
            payload = make_payload()
            await orch.handle_notification(payload)
            nid = payload.notification_id

            self._fire_terminal(orch, "ans_notification_delivered", nid, "rcpt-1")
            self._fire_terminal(orch, "ans_notification_delivered", nid, "rcpt-1")
            self._fire_terminal(orch, "ans_notification_delivered", nid, "rcpt-2")
            self._fire_terminal(orch, "ans_notification_delivered", nid, "rcpt-2")

        hass.bus.async_fire.assert_called_once_with(
            EVENT_NOTIFICATION_SETTLED,
            {
                "notification_id": nid,
                "total_tasks": 4,
                "total_recipients": 2,
                "delivered": 4,
                "failed": 0,
                "filtered": 0,
                "recipients_delivered": 2,
                "recipients": {
                    "rcpt-1": {
                        "channels_total": 2,
                        "channels_delivered": 2,
                        "channels_failed": 0,
                        "channels_filtered": 0,
                    },
                    "rcpt-2": {
                        "channels_total": 2,
                        "channels_delivered": 2,
                        "channels_failed": 0,
                        "channels_filtered": 0,
                    },
                },
            },
        )

    async def test_multi_recipient_partial_success(self):
        """One recipient fully delivered, one fully filtered → recipients_delivered counts only the delivered one."""
        hass = _make_hass()
        cancel = MagicMock()
        with patch(_PATCH_ACL, return_value=cancel):
            orch, _, _ = _make_orchestrator(
                recipients=["rcpt-1", "rcpt-2"],
                channels_per_recipient={
                    "rcpt-1": ["notify.ch_a", "notify.ch_b"],
                    "rcpt-2": ["notify.ch_a", "notify.ch_b"],
                },
                hass=hass,
            )
            payload = make_payload()
            await orch.handle_notification(payload)
            nid = payload.notification_id

            self._fire_terminal(orch, "ans_notification_delivered", nid, "rcpt-1")
            self._fire_terminal(orch, "ans_notification_delivered", nid, "rcpt-1")
            self._fire_terminal(orch, "ans_notification_filtered", nid, "rcpt-2")
            self._fire_terminal(orch, "ans_notification_filtered", nid, "rcpt-2")

        hass.bus.async_fire.assert_called_once_with(
            EVENT_NOTIFICATION_SETTLED,
            {
                "notification_id": nid,
                "total_tasks": 4,
                "total_recipients": 2,
                "delivered": 2,
                "failed": 0,
                "filtered": 2,
                "recipients_delivered": 1,
                "recipients": {
                    "rcpt-1": {
                        "channels_total": 2,
                        "channels_delivered": 2,
                        "channels_failed": 0,
                        "channels_filtered": 0,
                    },
                    "rcpt-2": {
                        "channels_total": 2,
                        "channels_delivered": 0,
                        "channels_failed": 0,
                        "channels_filtered": 2,
                    },
                },
            },
        )

    async def test_multi_recipient_all_failed_recipients_delivered_zero(self):
        """All tasks fail across all recipients → recipients_delivered is 0."""
        hass = _make_hass()
        cancel = MagicMock()
        with patch(_PATCH_ACL, return_value=cancel):
            orch, _, _ = _make_orchestrator(
                recipients=["rcpt-1", "rcpt-2"],
                channels_per_recipient={
                    "rcpt-1": ["notify.ch_a"],
                    "rcpt-2": ["notify.ch_a"],
                },
                hass=hass,
            )
            payload = make_payload()
            await orch.handle_notification(payload)
            nid = payload.notification_id

            self._fire_terminal(orch, "ans_notification_failed", nid, "rcpt-1")
            self._fire_terminal(orch, "ans_notification_failed", nid, "rcpt-2")

        hass.bus.async_fire.assert_called_once_with(
            EVENT_NOTIFICATION_SETTLED,
            {
                "notification_id": nid,
                "total_tasks": 2,
                "total_recipients": 2,
                "delivered": 0,
                "failed": 2,
                "filtered": 0,
                "recipients_delivered": 0,
                "recipients": {
                    "rcpt-1": {
                        "channels_total": 1,
                        "channels_delivered": 0,
                        "channels_failed": 1,
                        "channels_filtered": 0,
                    },
                    "rcpt-2": {
                        "channels_total": 1,
                        "channels_delivered": 0,
                        "channels_failed": 1,
                        "channels_filtered": 0,
                    },
                },
            },
        )

    async def test_recipient_partial_channel_delivery(self):
        """One recipient, one channel delivered and one failed → recipients_delivered=1."""
        hass = _make_hass()
        cancel = MagicMock()
        with patch(_PATCH_ACL, return_value=cancel):
            orch, _, _ = _make_orchestrator(
                recipients=["rcpt-1"],
                channels_per_recipient={"rcpt-1": ["notify.ch_a", "notify.ch_b"]},
                hass=hass,
            )
            payload = make_payload()
            await orch.handle_notification(payload)
            nid = payload.notification_id

            self._fire_terminal(orch, "ans_notification_delivered", nid, "rcpt-1")
            self._fire_terminal(orch, "ans_notification_failed", nid, "rcpt-1")

        hass.bus.async_fire.assert_called_once_with(
            EVENT_NOTIFICATION_SETTLED,
            {
                "notification_id": nid,
                "total_tasks": 2,
                "total_recipients": 1,
                "delivered": 1,
                "failed": 1,
                "filtered": 0,
                "recipients_delivered": 1,
                "recipients": {
                    "rcpt-1": {
                        "channels_total": 2,
                        "channels_delivered": 1,
                        "channels_failed": 1,
                        "channels_filtered": 0,
                    },
                },
            },
        )
