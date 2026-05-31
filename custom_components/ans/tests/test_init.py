"""Unit tests for the ANS integration bootstrap (__init__.py).

Coverage targets
----------------
- _setup_config          — happy path + ConfigEntryNotReady on load failure
- _setup_system          — channel sync with / without system_config
- _setup_persistence     — delegates to async_initialize_persistence
- _setup_tasks           — worker startup, retry recovery, orphan removal,
                           exception logging from gather results
- _setup_services        — delegates to async_setup_services
- _setup_listeners       — all four event handlers + exception paths
- async_setup_entry      — happy path, ConfigEntryNotReady passthrough,
                           generic exception wrapping, cleanup on failure
- async_unload_entry     — with / without runtime_data
- _teardown_entry_components — full / partial teardown, exception logging
- _cleanup_entry_data    — with / without runtime_data
- _get_entry_data        — main entry present / absent / no runtime_data
- get_rate_limiter       — system present / absent / no entry_data
- get_task_queue         — system present / absent / no entry_data
- get_channel_manager    — system present / absent / no entry_data
- get_config_repository  — present / absent
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from homeassistant.components.persistent_notification import UpdateType as PNUpdateType
from homeassistant.const import EVENT_SERVICE_REGISTERED
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.entity_registry import (
    EVENT_ENTITY_REGISTRY_UPDATED,
)

from custom_components.ans import (
    _cleanup_entry_data,
    _get_entry_data,
    _setup_config,
    _setup_listeners,
    _setup_persistence,
    _setup_repairs,
    _setup_services,
    _setup_system,
    _setup_tasks,
    _teardown_entry_components,
    async_migrate_entry,
    async_setup_entry,
    async_unload_entry,
    get_channel_manager,
    get_config_repository,
    get_rate_limiter,
    get_task_queue,
)
from custom_components.ans.channels.base import ChannelRecord, ChannelStatus
from custom_components.ans.channels.channel_manager import ChannelManager
from custom_components.ans.const import (
    DOMAIN,
    EVENT_NOTIFICATION_ACKNOWLEDGED,
    EVENT_NOTIFICATION_DELIVERED,
    PERSISTENT_NOTIFICATION_CHANNEL,
    REPAIR_ISSUE_STALE_CHANNEL,
    REQUIRED_MP_FEATURES,
)

from .conftest import make_task

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_hass() -> MagicMock:
    """Minimal HomeAssistant mock suitable for bootstrap tests."""
    hass = MagicMock()
    hass.config_entries = MagicMock()
    hass.bus = MagicMock()
    hass.bus.async_listen = MagicMock(
        return_value=MagicMock()
    )  # returns unsubscribe fn
    hass.services = MagicMock()
    return hass


def _make_entry(entry_id: str = "test-entry-id") -> MagicMock:
    """Return a mock ConfigEntry with entry_id, runtime_data={}, and async_on_unload/add_update_listener pre-configured."""
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.runtime_data = {}
    entry.async_on_unload = MagicMock()
    entry.add_update_listener = MagicMock(return_value=MagicMock())
    return entry


def _make_config_repo(*, has_recipients: bool = True) -> MagicMock:
    """Return a mock ConfigRepository with system_config and optionally a recipient; load() succeeds by default."""
    config_repo = MagicMock()
    config_repo.system_config = MagicMock()
    config_repo.system_config.enabled_channels = ["notify.persistent_notification"]
    config_repo.recipients = {"rcpt-1": MagicMock()} if has_recipients else {}
    config_repo.load = AsyncMock(return_value=True)
    return config_repo


def _make_channel_manager(*, setup_in_progress: bool = False) -> MagicMock:
    """Return a mock ChannelManager with all async methods pre-configured as AsyncMocks."""
    mgr = MagicMock()
    mgr.count_detected = MagicMock(return_value=1)
    mgr.count_active = MagicMock(return_value=1)
    mgr.sync = AsyncMock()
    mgr.resync = AsyncMock()
    mgr.request_resync = AsyncMock()
    mgr.finalize_setup = AsyncMock()
    mgr.cleanup_all = AsyncMock()
    mgr._setup_in_progress = setup_in_progress
    mgr._pending_resync = False
    return mgr


def _make_system(channel_manager: MagicMock | None = None) -> MagicMock:
    """Return a mock ANSSystem with all sub-components (task_queue, housekeeping, etc.) wired as AsyncMocks."""
    system = MagicMock()
    system.channel_manager = channel_manager or _make_channel_manager()
    system.task_queue = MagicMock()
    system.task_queue.start = AsyncMock()
    system.task_queue.stop = AsyncMock()
    system.task_queue.add_task = AsyncMock()
    system.housekeeping_scheduler = MagicMock()
    system.housekeeping_scheduler.start = AsyncMock()
    system.housekeeping_scheduler.stop = AsyncMock()
    system.deduplication_service = MagicMock()
    system.deduplication_service.start = AsyncMock()
    system.deduplication_service.stop = AsyncMock()
    system.notification_registry = MagicMock()
    system.attempt_log = MagicMock()
    system.retry_queue = MagicMock()
    system.retry_queue.remove_retry = AsyncMock()
    system.orchestrator = MagicMock()
    system.orchestrator.stop = AsyncMock()
    system.rate_limiter = MagicMock()
    system.acknowledgement_registry = MagicMock()
    system.acknowledgement_registry.record_acknowledgement = AsyncMock(
        return_value=True
    )
    return system


# ===========================================================================
# _setup_config
# ===========================================================================


class TestSetupConfig:
    """Verify _setup_config() returns a loaded ConfigRepository or raises ConfigEntryNotReady on load failure."""

    async def test_happy_path_returns_repo(self):
        """_setup_config() returns the ConfigRepository after calling load() successfully."""
        hass = _make_hass()
        entry = _make_entry()
        config_repo = _make_config_repo()

        with patch(
            "custom_components.ans.ConfigRepository",
            return_value=config_repo,
        ):
            result = await _setup_config(hass, entry)

        assert result is config_repo
        config_repo.load.assert_awaited_once()

    async def test_load_failure_raises_config_entry_not_ready(self):
        """_setup_config() raises ConfigEntryNotReady when load() returns False."""
        hass = _make_hass()
        entry = _make_entry()
        config_repo = _make_config_repo()
        config_repo.load = AsyncMock(return_value=False)

        with (
            patch("custom_components.ans.ConfigRepository", return_value=config_repo),
            pytest.raises(ConfigEntryNotReady),
        ):
            await _setup_config(hass, entry)


# ===========================================================================
# _setup_system
# ===========================================================================


class TestSetupSystem:
    """Verify _setup_system() creates the ANSSystem and conditionally syncs channels based on system_config."""

    async def test_calls_channel_sync_when_system_config_present(self):
        """_setup_system() calls channel_manager.sync() with enabled_channels when system_config is set."""
        hass = _make_hass()
        channel_manager = _make_channel_manager()
        system = _make_system(channel_manager)
        config_repo = _make_config_repo()
        volume_registry = MagicMock()

        with patch("custom_components.ans.create_system", return_value=system):
            result = await _setup_system(hass, config_repo, volume_registry)

        assert result is system
        channel_manager.sync.assert_awaited_once_with(
            list(config_repo.system_config.enabled_channels)
        )

    async def test_skips_channel_sync_when_no_system_config(self):
        """_setup_system() does not call channel_manager.sync() when system_config is None."""
        hass = _make_hass()
        channel_manager = _make_channel_manager()
        system = _make_system(channel_manager)
        config_repo = _make_config_repo()
        config_repo.system_config = None
        volume_registry = MagicMock()

        with patch("custom_components.ans.create_system", return_value=system):
            await _setup_system(hass, config_repo, volume_registry)

        channel_manager.sync.assert_not_awaited()


# ===========================================================================
# _setup_persistence
# ===========================================================================


class TestSetupPersistence:
    """Verify _setup_persistence() delegates to async_initialize_persistence and forwards its return values."""

    async def test_returns_pending_and_orphaned(self):
        """_setup_persistence() returns the (pending, orphaned) tuple from async_initialize_persistence."""
        hass = _make_hass()
        system = _make_system()
        task = make_task()
        scheduled_time = datetime.now(UTC)

        with patch(
            "custom_components.ans.async_initialize_persistence",
            return_value=([(task, scheduled_time)], ["orphan-1"]),
        ) as mock_init:
            pending, orphaned = await _setup_persistence(hass, system)

        mock_init.assert_awaited_once()
        assert pending == [(task, scheduled_time)]
        assert orphaned == ["orphan-1"]

    async def test_empty_result(self):
        """_setup_persistence() returns ([], []) when async_initialize_persistence finds no pending or orphaned tasks."""
        hass = _make_hass()
        system = _make_system()

        with patch(
            "custom_components.ans.async_initialize_persistence",
            return_value=([], []),
        ):
            pending, orphaned = await _setup_persistence(hass, system)

        assert pending == []
        assert orphaned == []


# ===========================================================================
# _setup_tasks
# ===========================================================================


class TestSetupTasks:
    """Verify _setup_tasks() starts all workers, re-enqueues pending retries, and removes orphaned retry entries."""

    async def test_starts_all_background_workers(self):
        """_setup_tasks() starts task_queue, housekeeping_scheduler, and deduplication_service."""
        system = _make_system()

        await _setup_tasks(system, [], [])

        system.task_queue.start.assert_awaited_once()
        system.housekeeping_scheduler.start.assert_awaited_once()
        system.deduplication_service.start.assert_awaited_once()

    async def test_enqueues_non_overdue_task_with_delay(self):
        """_setup_tasks() re-enqueues a pending retry with a positive delay when scheduled_at is in the future."""
        system = _make_system()
        task = make_task()
        future_time = datetime.now(UTC) + timedelta(seconds=30)

        await _setup_tasks(system, [(task, future_time)], [])

        system.task_queue.add_task.assert_awaited_once()
        _, kwargs = system.task_queue.add_task.call_args
        assert kwargs["delay"].total_seconds() > 0

    async def test_enqueues_overdue_task_with_zero_delay(self):
        """_setup_tasks() re-enqueues an overdue retry with delay=timedelta(0) when scheduled_at is in the past."""
        system = _make_system()
        task = make_task()
        past_time = datetime.now(UTC) - timedelta(seconds=60)

        await _setup_tasks(system, [(task, past_time)], [])

        system.task_queue.add_task.assert_awaited_once()
        _, kwargs = system.task_queue.add_task.call_args
        assert kwargs["delay"].total_seconds() == 0

    async def test_removes_orphaned_retries(self):
        """_setup_tasks() calls retry_queue.remove_retry() for each orphaned job ID."""
        job_id = str(uuid4())
        system = _make_system()

        await _setup_tasks(system, [], [job_id])

        system.retry_queue.remove_retry.assert_awaited_once_with(UUID(job_id))

    async def test_logs_error_for_invalid_uuid_orphan(self, caplog):
        """_setup_tasks() logs an 'Invalid UUID' error and skips remove_retry() when the orphan ID is malformed."""
        system = _make_system()

        with caplog.at_level("ERROR"):
            await _setup_tasks(system, [], ["not-a-uuid"])

        assert "Invalid UUID" in caplog.text
        system.retry_queue.remove_retry.assert_not_awaited()

    async def test_logs_error_when_enqueue_raises(self, caplog):
        """_setup_tasks() logs a 'Failed to re-enqueue' error when add_task() raises."""
        system = _make_system()
        system.task_queue.add_task = AsyncMock(side_effect=RuntimeError("queue full"))
        task = make_task()
        future_time = datetime.now(UTC) + timedelta(seconds=5)

        with caplog.at_level("ERROR"):
            await _setup_tasks(system, [(task, future_time)], [])

        assert "Failed to re-enqueue" in caplog.text

    async def test_logs_error_when_remove_retry_raises(self, caplog):
        """_setup_tasks() logs a 'Failed to remove orphaned retry' error when remove_retry() raises."""
        job_id = str(uuid4())
        system = _make_system()
        system.retry_queue.remove_retry = AsyncMock(
            side_effect=RuntimeError("db error")
        )

        with caplog.at_level("ERROR"):
            await _setup_tasks(system, [], [job_id])

        assert "Failed to remove orphaned retry" in caplog.text


# ===========================================================================
# _setup_services
# ===========================================================================


class TestSetupServices:
    """Verify _setup_services() delegates directly to async_setup_services."""

    async def test_delegates_to_async_setup_services(self):
        """_setup_services() calls async_setup_services(hass, orchestrator)."""
        hass = _make_hass()
        system = _make_system()

        with patch("custom_components.ans.async_setup_services") as mock_setup:
            mock_setup.return_value = None
            await _setup_services(hass, system)

        mock_setup.assert_called_once_with(hass, system.orchestrator)


# ===========================================================================
# _setup_listeners
# ===========================================================================


class TestSetupListeners:
    """Tests for each event listener registered in _setup_listeners."""

    def _capture_listeners(
        self, hass: MagicMock, entry: MagicMock, channel_manager: MagicMock
    ) -> dict[str, Any]:
        """Call _setup_listeners and harvest the registered callbacks.

        Returns a dict with keys:
        - ``update_listener``        — the options-change callback
        - ``notify_service``         — EVENT_SERVICE_REGISTERED callback
        - ``media_player_added``     — state-added callback
        - ``entity_registry_updated``— EVENT_ENTITY_REGISTRY_UPDATED callback
        """
        captured: dict[str, Any] = {}

        def _on_unload(fn):
            # fn is either a callable (the remove/unsubscribe fn) or the result
            # of hass.bus.async_listen / async_track_state_added_domain.
            """Accept and ignore the unsubscribe callable (side-effect capture only)."""

        entry.async_on_unload.side_effect = _on_unload

        # Capture add_update_listener callback
        def _add_listener(cb):
            """Capture the update listener callback for assertion."""
            captured["update_listener"] = cb
            return MagicMock()  # unsubscribe

        entry.add_update_listener.side_effect = _add_listener

        # Capture EVENT_SERVICE_REGISTERED listener
        bus_listens: list = []

        def _bus_listen(event_type, cb):
            """Capture bus event callbacks, keyed by event_type."""
            bus_listens.append((event_type, cb))
            return MagicMock()

        hass.bus.async_listen.side_effect = _bus_listen

        # Capture state-added listener
        state_added_cbs: list = []

        def _state_added(hass_, domain, cb):
            """Capture state-change domain callbacks for assertion."""
            state_added_cbs.append(cb)
            return MagicMock()

        with patch(
            "custom_components.ans.async_track_state_added_domain",
            side_effect=_state_added,
        ):
            _setup_listeners(hass, entry, channel_manager)

        for event_type, cb in bus_listens:
            if event_type == EVENT_SERVICE_REGISTERED:
                captured["notify_service"] = cb
            elif event_type == EVENT_ENTITY_REGISTRY_UPDATED:
                captured["entity_registry_updated"] = cb

        if state_added_cbs:
            captured["media_player_added"] = state_added_cbs[0]

        return captured

    # ── update_listener ──────────────────────────────────────────────────────

    async def test_update_listener_reloads_entry(self):
        """The update listener calls config_entries.async_reload() with the entry_id."""
        hass = _make_hass()
        entry = _make_entry()
        hass.config_entries.async_reload = AsyncMock()
        channel_manager = _make_channel_manager()

        captured = self._capture_listeners(hass, entry, channel_manager)
        await captured["update_listener"](hass, entry)

        hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)

    # ── notify service registered ─────────────────────────────────────────────

    async def test_notify_service_ignores_non_notify_domain(self):
        """The EVENT_SERVICE_REGISTERED callback ignores events from non-notify domains."""
        hass = _make_hass()
        entry = _make_entry()
        channel_manager = _make_channel_manager()

        captured = self._capture_listeners(hass, entry, channel_manager)
        event = MagicMock()
        event.data = {"domain": "mqtt", "service": "foo"}
        await captured["notify_service"](event)

        channel_manager.request_resync.assert_not_awaited()

    async def test_notify_service_ignores_builtin_names(self):
        """The callback ignores the built-in service names 'notify' and 'send_message'."""
        hass = _make_hass()
        entry = _make_entry()
        channel_manager = _make_channel_manager()

        captured = self._capture_listeners(hass, entry, channel_manager)
        for svc in ("notify", "send_message"):
            event = MagicMock()
            event.data = {"domain": "notify", "service": svc}
            await captured["notify_service"](event)

        channel_manager.request_resync.assert_not_awaited()

    async def test_notify_service_triggers_resync(self):
        """A new notify domain service triggers channel_manager.request_resync()."""
        hass = _make_hass()
        entry = _make_entry()
        channel_manager = _make_channel_manager()

        captured = self._capture_listeners(hass, entry, channel_manager)
        event = MagicMock()
        event.data = {"domain": "notify", "service": "mobile_app_phone"}
        await captured["notify_service"](event)

        channel_manager.request_resync.assert_awaited_once()

    async def test_notify_service_logs_exception(self, caplog):
        """An exception from request_resync() is caught and logged with the service name."""
        hass = _make_hass()
        entry = _make_entry()
        channel_manager = _make_channel_manager()
        channel_manager.request_resync = AsyncMock(side_effect=RuntimeError("boom"))

        captured = self._capture_listeners(hass, entry, channel_manager)
        event = MagicMock()
        event.data = {"domain": "notify", "service": "my_service"}

        with caplog.at_level("ERROR"):
            await captured["notify_service"](event)

        assert "my_service" in caplog.text

    # ── media_player added ────────────────────────────────────────────────────

    async def test_media_player_added_ignores_insufficient_features(self):
        """A new media_player state with insufficient supported_features does not trigger a resync."""
        hass = _make_hass()
        entry = _make_entry()
        channel_manager = _make_channel_manager()

        captured = self._capture_listeners(hass, entry, channel_manager)
        state = MagicMock()
        state.attributes = {"supported_features": 0x0001}  # missing required bits
        event = MagicMock()
        event.data = {"entity_id": "media_player.tv", "new_state": state}
        await captured["media_player_added"](event)

        channel_manager.request_resync.assert_not_awaited()

    async def test_media_player_added_with_no_new_state_ignored(self):
        """A state-change event with new_state=None is silently ignored."""
        hass = _make_hass()
        entry = _make_entry()
        channel_manager = _make_channel_manager()

        captured = self._capture_listeners(hass, entry, channel_manager)
        event = MagicMock()
        event.data = {"entity_id": "media_player.tv", "new_state": None}
        await captured["media_player_added"](event)

        channel_manager.request_resync.assert_not_awaited()

    async def test_media_player_added_triggers_resync(self):
        """A new media_player with the required supported_features triggers channel_manager.request_resync()."""
        hass = _make_hass()
        entry = _make_entry()
        channel_manager = _make_channel_manager()

        captured = self._capture_listeners(hass, entry, channel_manager)
        state = MagicMock()
        state.attributes = {"supported_features": REQUIRED_MP_FEATURES}
        event = MagicMock()
        event.data = {"entity_id": "media_player.speaker", "new_state": state}
        await captured["media_player_added"](event)

        channel_manager.request_resync.assert_awaited_once()

    async def test_media_player_added_logs_exception(self, caplog):
        """An exception from request_resync() is caught and logged with the entity_id."""
        hass = _make_hass()
        entry = _make_entry()
        channel_manager = _make_channel_manager()
        channel_manager.request_resync = AsyncMock(side_effect=RuntimeError("fail"))

        captured = self._capture_listeners(hass, entry, channel_manager)
        state = MagicMock()
        state.attributes = {"supported_features": REQUIRED_MP_FEATURES}
        event = MagicMock()
        event.data = {"entity_id": "media_player.speaker", "new_state": state}

        with caplog.at_level("ERROR"):
            await captured["media_player_added"](event)

        assert "media_player.speaker" in caplog.text

    # ── entity registry updated ───────────────────────────────────────────────

    async def test_entity_registry_ignores_non_remove_actions(self):
        """The EVENT_ENTITY_REGISTRY_UPDATED callback ignores non-remove actions such as 'update'."""
        hass = _make_hass()
        entry = _make_entry()
        channel_manager = _make_channel_manager()

        captured = self._capture_listeners(hass, entry, channel_manager)
        event = MagicMock()
        event.data = {"action": "update", "entity_id": "media_player.tv"}
        await captured["entity_registry_updated"](event)

        channel_manager.request_resync.assert_not_awaited()

    async def test_entity_registry_ignores_non_media_player(self):
        """The callback ignores remove events for non-media_player entities."""
        hass = _make_hass()
        entry = _make_entry()
        channel_manager = _make_channel_manager()

        captured = self._capture_listeners(hass, entry, channel_manager)
        event = MagicMock()
        event.data = {"action": "remove", "entity_id": "light.bedroom"}
        await captured["entity_registry_updated"](event)

        channel_manager.request_resync.assert_not_awaited()

    async def test_entity_registry_triggers_resync_on_media_player_remove(self):
        """Removing a media_player entity triggers channel_manager.request_resync()."""
        hass = _make_hass()
        entry = _make_entry()
        channel_manager = _make_channel_manager()

        captured = self._capture_listeners(hass, entry, channel_manager)
        event = MagicMock()
        event.data = {"action": "remove", "entity_id": "media_player.living_room"}
        await captured["entity_registry_updated"](event)

        channel_manager.request_resync.assert_awaited_once()

    async def test_entity_registry_logs_exception(self, caplog):
        """An exception from request_resync() is caught and logged with the entity_id."""
        hass = _make_hass()
        entry = _make_entry()
        channel_manager = _make_channel_manager()
        channel_manager.request_resync = AsyncMock(side_effect=RuntimeError("err"))

        captured = self._capture_listeners(hass, entry, channel_manager)
        event = MagicMock()
        event.data = {"action": "remove", "entity_id": "media_player.kitchen"}

        with caplog.at_level("ERROR"):
            await captured["entity_registry_updated"](event)

        assert "media_player.kitchen" in caplog.text


# ===========================================================================
# async_setup_entry
# ===========================================================================


class TestAsyncSetupEntry:
    """Verify async_setup_entry() happy path, error propagation, cleanup on failure, and warning when no recipients."""

    def _patch_all(
        self,
        config_repo: MagicMock,
        system: MagicMock,
        pending: list,
        orphaned: list,
    ):
        """Return a context-manager stack that patches all setup phases."""
        return [
            patch("custom_components.ans.VolumeRestorationRegistry"),
            patch("custom_components.ans._setup_config", return_value=config_repo),
            patch("custom_components.ans._setup_system", return_value=system),
            patch(
                "custom_components.ans._setup_persistence",
                return_value=(pending, orphaned),
            ),
            patch("custom_components.ans._setup_tasks"),
            patch("custom_components.ans._setup_services"),
            patch("custom_components.ans._setup_listeners"),
        ]

    async def test_happy_path_returns_true(self):
        """async_setup_entry() returns True when all setup phases succeed."""
        hass = _make_hass()
        entry = _make_entry()
        config_repo = _make_config_repo(has_recipients=True)
        system = _make_system()

        patches = self._patch_all(config_repo, system, [], [])
        with (
            patches[0] as mock_vol,
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            vol_instance = mock_vol.return_value
            vol_instance.async_load = AsyncMock()
            result = await async_setup_entry(hass, entry)

        assert result is True

    async def test_runtime_data_populated(self):
        """After setup, entry.runtime_data contains the 'config_repository' and 'system' keys."""
        hass = _make_hass()
        entry = _make_entry()
        config_repo = _make_config_repo()
        system = _make_system()

        patches = self._patch_all(config_repo, system, [], [])
        with (
            patches[0] as mock_vol,
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            vol_instance = mock_vol.return_value
            vol_instance.async_load = AsyncMock()
            await async_setup_entry(hass, entry)

        assert entry.runtime_data.get("config_repository") is config_repo
        assert entry.runtime_data.get("system") is system

    async def test_config_entry_not_ready_is_propagated(self):
        """ConfigEntryNotReady raised in a setup phase propagates out of async_setup_entry()."""
        hass = _make_hass()
        entry = _make_entry()

        with (
            patch(
                "custom_components.ans.VolumeRestorationRegistry",
            ) as mock_vol,
            patch(
                "custom_components.ans._setup_config",
                side_effect=ConfigEntryNotReady("config load failed"),
            ),
            patch("custom_components.ans._cleanup_entry_data", new=AsyncMock()),
        ):
            vol_instance = mock_vol.return_value
            vol_instance.async_load = AsyncMock()
            with pytest.raises(ConfigEntryNotReady, match="config load failed"):
                await async_setup_entry(hass, entry)

    async def test_generic_exception_wrapped_in_config_entry_not_ready(self):
        """A RuntimeError in a setup phase is wrapped in ConfigEntryNotReady('Setup failed...')."""
        hass = _make_hass()
        entry = _make_entry()

        with (
            patch(
                "custom_components.ans.VolumeRestorationRegistry",
            ) as mock_vol,
            patch(
                "custom_components.ans._setup_config",
                side_effect=RuntimeError("unexpected"),
            ),
            patch("custom_components.ans._cleanup_entry_data", new=AsyncMock()),
        ):
            vol_instance = mock_vol.return_value
            vol_instance.async_load = AsyncMock()
            with pytest.raises(ConfigEntryNotReady, match="Setup failed"):
                await async_setup_entry(hass, entry)

    async def test_cleanup_called_on_failure(self):
        """_cleanup_entry_data() is awaited exactly once when any setup phase raises."""
        hass = _make_hass()
        entry = _make_entry()
        cleanup_mock = AsyncMock()

        with (
            patch(
                "custom_components.ans.VolumeRestorationRegistry",
            ) as mock_vol,
            patch(
                "custom_components.ans._setup_config",
                side_effect=RuntimeError("fail"),
            ),
            patch("custom_components.ans._cleanup_entry_data", new=cleanup_mock),
        ):
            vol_instance = mock_vol.return_value
            vol_instance.async_load = AsyncMock()
            with pytest.raises(ConfigEntryNotReady):
                await async_setup_entry(hass, entry)

        cleanup_mock.assert_awaited_once_with(entry)

    async def test_warns_when_no_recipients(self, caplog):
        """async_setup_entry() logs a 'no recipients configured' warning when config_repo has no recipients."""
        hass = _make_hass()
        entry = _make_entry()
        config_repo = _make_config_repo(has_recipients=False)
        system = _make_system()

        patches = self._patch_all(config_repo, system, [], [])
        with (
            patches[0] as mock_vol,
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            caplog.at_level("WARNING"),
        ):
            vol_instance = mock_vol.return_value
            vol_instance.async_load = AsyncMock()
            await async_setup_entry(hass, entry)

        assert "no recipients configured" in caplog.text


# ===========================================================================
# async_unload_entry
# ===========================================================================


class TestAsyncUnloadEntry:
    """Verify async_unload_entry() always returns True and calls _cleanup_entry_data()."""

    async def test_returns_true_with_runtime_data(self):
        """async_unload_entry() returns True when entry.runtime_data is populated."""
        hass = _make_hass()
        entry = _make_entry()
        entry.runtime_data = {"system": _make_system()}

        with patch(
            "custom_components.ans._cleanup_entry_data", new=AsyncMock()
        ) as mock_cleanup:
            result = await async_unload_entry(hass, entry)

        assert result is True
        mock_cleanup.assert_awaited_once_with(entry)

    async def test_returns_true_without_runtime_data(self):
        """async_unload_entry() returns True even when entry.runtime_data is None."""
        hass = _make_hass()
        entry = _make_entry()
        entry.runtime_data = None

        with patch(
            "custom_components.ans._cleanup_entry_data", new=AsyncMock()
        ) as mock_cleanup:
            result = await async_unload_entry(hass, entry)

        assert result is True
        mock_cleanup.assert_awaited_once_with(entry)


# ===========================================================================
# _teardown_entry_components
# ===========================================================================


class TestTeardownEntryComponents:
    """Verify _teardown_entry_components() stops all sub-systems and handles missing components gracefully."""

    async def test_full_teardown_calls_all_coroutines(self):
        """_teardown_entry_components() stops task_queue, housekeeping_scheduler, deduplication_service, channels, and volume_registry."""
        system = _make_system()
        volume_registry = MagicMock()
        volume_registry.async_unload = AsyncMock()
        entry_data = {"system": system, "volume_registry": volume_registry}

        await _teardown_entry_components(entry_data)

        system.task_queue.stop.assert_awaited_once()
        system.housekeeping_scheduler.stop.assert_awaited_once()
        system.deduplication_service.stop.assert_awaited_once()
        system.channel_manager.cleanup_all.assert_awaited_once()
        system.orchestrator.stop.assert_awaited_once()
        volume_registry.async_unload.assert_awaited_once()

    async def test_no_system_only_unloads_volume_registry(self):
        """When system is None, only volume_registry.async_unload() is called."""
        volume_registry = MagicMock()
        volume_registry.async_unload = AsyncMock()
        entry_data = {"system": None, "volume_registry": volume_registry}

        await _teardown_entry_components(entry_data)

        volume_registry.async_unload.assert_awaited_once()

    async def test_no_volume_registry_only_stops_system(self):
        """When volume_registry is None, all system components are still stopped."""
        system = _make_system()
        entry_data = {"system": system, "volume_registry": None}

        await _teardown_entry_components(entry_data)

        system.task_queue.stop.assert_awaited_once()

    async def test_empty_entry_data_is_noop(self):
        """_teardown_entry_components({}) completes without raising."""
        await _teardown_entry_components({})  # should not raise

    async def test_exception_in_teardown_is_logged(self, caplog):
        """Exceptions from teardown coroutines are caught and logged as warnings."""
        system = _make_system()
        system.task_queue.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        entry_data = {"system": system, "volume_registry": None}

        with caplog.at_level("WARNING"):
            await _teardown_entry_components(entry_data)

        assert "stop failed" in caplog.text


# ===========================================================================
# _cleanup_entry_data
# ===========================================================================


class TestCleanupEntryData:
    """Verify _cleanup_entry_data() tears down components and clears entry.runtime_data."""

    async def test_tears_down_and_clears_runtime_data(self):
        """_cleanup_entry_data() calls _teardown_entry_components and then resets entry.runtime_data to {}."""
        system = _make_system()
        entry = _make_entry()
        entry.runtime_data = {"system": system}

        with patch(
            "custom_components.ans._teardown_entry_components", new=AsyncMock()
        ) as mock_teardown:
            await _cleanup_entry_data(entry)

        mock_teardown.assert_awaited_once()
        assert entry.runtime_data == {}

    async def test_noop_when_no_runtime_data(self):
        """_cleanup_entry_data() does nothing when entry.runtime_data is None."""
        entry = _make_entry()
        entry.runtime_data = None

        with patch(
            "custom_components.ans._teardown_entry_components", new=AsyncMock()
        ) as mock_teardown:
            await _cleanup_entry_data(entry)

        mock_teardown.assert_not_awaited()

    async def test_noop_when_empty_runtime_data(self):
        """_cleanup_entry_data() does nothing when entry.runtime_data is an empty dict."""
        entry = _make_entry()
        entry.runtime_data = {}

        with patch(
            "custom_components.ans._teardown_entry_components", new=AsyncMock()
        ) as mock_teardown:
            await _cleanup_entry_data(entry)

        mock_teardown.assert_not_awaited()


# ===========================================================================
# _get_entry_data
# ===========================================================================


class TestGetEntryData:
    """Verify _get_entry_data() returns entry.runtime_data from the main config entry or None."""

    def test_returns_runtime_data_when_entry_exists(self):
        """_get_entry_data() returns entry.runtime_data when the main entry is registered."""
        hass = _make_hass()
        entry = _make_entry()
        entry.runtime_data = {"system": MagicMock()}

        with patch("custom_components.ans.get_main_entry", return_value=entry):
            result = _get_entry_data(hass)

        assert result is entry.runtime_data

    def test_returns_none_when_no_main_entry(self):
        """_get_entry_data() returns None when get_main_entry() returns None."""
        hass = _make_hass()

        with patch("custom_components.ans.get_main_entry", return_value=None):
            result = _get_entry_data(hass)

        assert result is None

    def test_returns_none_when_no_runtime_data_attr(self):
        """_get_entry_data() returns None when the entry object has no runtime_data attribute."""
        hass = _make_hass()
        entry = MagicMock(spec=[])  # no runtime_data attribute

        with patch("custom_components.ans.get_main_entry", return_value=entry):
            result = _get_entry_data(hass)

        assert result is None


# ===========================================================================
# Accessor functions
# ===========================================================================


class TestAccessors:
    """Verify get_rate_limiter(), get_task_queue(), get_channel_manager(), and get_config_repository() with and without a valid system."""

    def _hass_with_entry_data(self, entry_data: dict | None) -> MagicMock:
        """Return a mock hass with get_main_entry patched to return an entry containing the given runtime_data dict."""
        hass = _make_hass()
        with patch("custom_components.ans.get_main_entry") as mock_entry_fn:
            if entry_data is None:
                mock_entry_fn.return_value = None
            else:
                entry = _make_entry()
                entry.runtime_data = entry_data
                mock_entry_fn.return_value = entry
            # Return the patcher so callers can activate it
            hass._patch_get_main_entry = mock_entry_fn
        return hass

    # ── get_rate_limiter ──────────────────────────────────────────────────────

    def test_get_rate_limiter_returns_limiter(self):
        """get_rate_limiter() returns system.rate_limiter when a system is present."""
        hass = _make_hass()
        system = _make_system()
        entry = _make_entry()
        entry.runtime_data = {"system": system}

        with patch("custom_components.ans.get_main_entry", return_value=entry):
            result = get_rate_limiter(hass)

        assert result is system.rate_limiter

    def test_get_rate_limiter_no_system_returns_none(self):
        """get_rate_limiter() returns None when system is None in runtime_data."""
        hass = _make_hass()
        entry = _make_entry()
        entry.runtime_data = {"system": None}

        with patch("custom_components.ans.get_main_entry", return_value=entry):
            result = get_rate_limiter(hass)

        assert result is None

    def test_get_rate_limiter_no_entry_returns_none(self):
        """get_rate_limiter() returns None when no main entry is registered."""
        hass = _make_hass()

        with patch("custom_components.ans.get_main_entry", return_value=None):
            result = get_rate_limiter(hass)

        assert result is None

    # ── get_task_queue ────────────────────────────────────────────────────────

    def test_get_task_queue_returns_queue(self):
        """get_task_queue() returns system.task_queue when a system is present."""
        hass = _make_hass()
        system = _make_system()
        entry = _make_entry()
        entry.runtime_data = {"system": system}

        with patch("custom_components.ans.get_main_entry", return_value=entry):
            result = get_task_queue(hass)

        assert result is system.task_queue

    def test_get_task_queue_no_system_returns_none(self):
        """get_task_queue() returns None when system is None in runtime_data."""
        hass = _make_hass()
        entry = _make_entry()
        entry.runtime_data = {"system": None}

        with patch("custom_components.ans.get_main_entry", return_value=entry):
            result = get_task_queue(hass)

        assert result is None

    def test_get_task_queue_no_entry_returns_none(self):
        """get_task_queue() returns None when no main entry is registered."""
        hass = _make_hass()

        with patch("custom_components.ans.get_main_entry", return_value=None):
            result = get_task_queue(hass)

        assert result is None

    # ── get_channel_manager ───────────────────────────────────────────────────

    def test_get_channel_manager_returns_manager(self):
        """get_channel_manager() returns system.channel_manager when a system is present."""
        hass = _make_hass()
        system = _make_system()
        entry = _make_entry()
        entry.runtime_data = {"system": system}

        with patch("custom_components.ans.get_main_entry", return_value=entry):
            result = get_channel_manager(hass)

        assert result is system.channel_manager

    def test_get_channel_manager_no_system_returns_none(self):
        """get_channel_manager() returns None when system is None in runtime_data."""
        hass = _make_hass()
        entry = _make_entry()
        entry.runtime_data = {"system": None}

        with patch("custom_components.ans.get_main_entry", return_value=entry):
            result = get_channel_manager(hass)

        assert result is None

    def test_get_channel_manager_no_entry_returns_none(self):
        """get_channel_manager() returns None when no main entry is registered."""
        hass = _make_hass()

        with patch("custom_components.ans.get_main_entry", return_value=None):
            result = get_channel_manager(hass)

        assert result is None

    # ── get_config_repository ─────────────────────────────────────────────────

    def test_get_config_repository_returns_repo(self):
        """get_config_repository() returns the ConfigRepository stored in runtime_data."""
        hass = _make_hass()
        repo = _make_config_repo()
        entry = _make_entry()
        entry.runtime_data = {"config_repository": repo}

        with patch("custom_components.ans.get_main_entry", return_value=entry):
            result = get_config_repository(hass)

        assert result is repo

    def test_get_config_repository_no_entry_returns_none(self):
        """get_config_repository() returns None when no main entry is registered."""
        hass = _make_hass()

        with patch("custom_components.ans.get_main_entry", return_value=None):
            result = get_config_repository(hass)

        assert result is None

    def test_get_config_repository_missing_key_returns_none(self):
        """get_config_repository() returns None when 'config_repository' key is absent from runtime_data."""
        hass = _make_hass()
        entry = _make_entry()
        entry.runtime_data = {}

        with patch("custom_components.ans.get_main_entry", return_value=entry):
            result = get_config_repository(hass)

        assert result is None


# ===========================================================================
# ChannelManager.request_resync (new method — unit test in isolation)
# ===========================================================================


class TestChannelManagerRequestResync:
    """Verify the new public request_resync() method behaves correctly."""

    async def test_defers_when_setup_in_progress(self):
        """request_resync() sets _pending_resync=True but does not call resync() while _setup_in_progress is True."""

        hass = MagicMock()
        deps = MagicMock()
        mgr = ChannelManager(hass, deps)
        mgr.resync = AsyncMock()

        # setup_in_progress is True by default after __init__
        assert mgr._setup_in_progress is True

        await mgr.request_resync()

        mgr.resync.assert_not_awaited()
        assert mgr._pending_resync is True

    async def test_calls_resync_when_setup_complete(self):
        """request_resync() calls resync() immediately when _setup_in_progress is False."""

        hass = MagicMock()
        deps = MagicMock()
        mgr = ChannelManager(hass, deps)
        mgr._setup_in_progress = False
        mgr.resync = AsyncMock()

        await mgr.request_resync()

        mgr.resync.assert_awaited_once()
        assert mgr._pending_resync is False

    async def test_finalize_setup_flushes_deferred_request(self):
        """finalize_setup() calls resync() to flush any pending deferred request and resets _setup_in_progress."""

        hass = MagicMock()
        deps = MagicMock()
        mgr = ChannelManager(hass, deps)
        mgr.resync = AsyncMock()

        # Defer a resync
        await mgr.request_resync()
        assert mgr._pending_resync is True

        # Finalize flushes it
        await mgr.finalize_setup()

        mgr.resync.assert_awaited_once()
        assert mgr._setup_in_progress is False
        assert mgr._pending_resync is False


# ===========================================================================
# async_migrate_entry
# ===========================================================================


class TestAsyncMigrateEntry:
    """Verify async_migrate_entry() clamps v1 retry options into the new bounds."""

    def _make_entry_v1(self, options: dict) -> MagicMock:
        """Return a v1 mock ConfigEntry with the given options."""
        entry = MagicMock()
        entry.entry_id = "test-entry-id"
        entry.version = 1
        entry.options = options
        return entry

    async def test_values_within_range_unchanged(self):
        """Options already within the new bounds are preserved unchanged; version becomes 2."""
        hass = _make_hass()
        hass.config_entries.async_update_entry = MagicMock()
        options = {
            "retry_base_delay": 60,
            "retry_backoff_factor": 2.0,
            "retry_max_delay": 3600,
        }
        entry = self._make_entry_v1(options)

        result = await async_migrate_entry(hass, entry)

        assert result is True
        hass.config_entries.async_update_entry.assert_called_once()
        call_kwargs = hass.config_entries.async_update_entry.call_args.kwargs
        assert call_kwargs["version"] == 2
        saved = call_kwargs["options"]
        assert saved["retry_base_delay"] == 60
        assert saved["retry_backoff_factor"] == 2.0
        assert saved["retry_max_delay"] == 3600

    async def test_base_delay_too_high_clamped_to_max(self):
        """retry_base_delay above 300 is clamped to SYS_MAX_RETRY_BASE_DELAY_SECONDS (300)."""
        hass = _make_hass()
        hass.config_entries.async_update_entry = MagicMock()
        entry = self._make_entry_v1({"retry_base_delay": 1800})

        await async_migrate_entry(hass, entry)

        saved = hass.config_entries.async_update_entry.call_args.kwargs["options"]
        assert saved["retry_base_delay"] == 300

    async def test_base_delay_too_low_raised_to_min(self):
        """retry_base_delay below 10 is raised to SYS_MIN_RETRY_BASE_DELAY_SECONDS (10)."""
        hass = _make_hass()
        hass.config_entries.async_update_entry = MagicMock()
        entry = self._make_entry_v1({"retry_base_delay": 5})

        await async_migrate_entry(hass, entry)

        saved = hass.config_entries.async_update_entry.call_args.kwargs["options"]
        assert saved["retry_base_delay"] == 10

    async def test_backoff_factor_too_high_clamped_to_max(self):
        """retry_backoff_factor above 3.0 is clamped to SYS_MAX_RETRY_BACKOFF_FACTOR (3)."""
        hass = _make_hass()
        hass.config_entries.async_update_entry = MagicMock()
        entry = self._make_entry_v1({"retry_backoff_factor": 4.5})

        await async_migrate_entry(hass, entry)

        saved = hass.config_entries.async_update_entry.call_args.kwargs["options"]
        assert saved["retry_backoff_factor"] == 3.0

    async def test_max_delay_too_high_clamped_to_max(self):
        """retry_max_delay above 3600 is clamped to SYS_MAX_RETRY_MAX_DELAY_SECONDS (3600)."""
        hass = _make_hass()
        hass.config_entries.async_update_entry = MagicMock()
        entry = self._make_entry_v1({"retry_max_delay": 86400})

        await async_migrate_entry(hass, entry)

        saved = hass.config_entries.async_update_entry.call_args.kwargs["options"]
        assert saved["retry_max_delay"] == 3600

    async def test_max_delay_raised_when_below_base_delay(self):
        """retry_max_delay is raised to equal retry_base_delay when it would be less after clamping."""
        hass = _make_hass()
        hass.config_entries.async_update_entry = MagicMock()
        # After clamping: base=120, max=60 — cross-field guard must raise max to 120
        entry = self._make_entry_v1({"retry_base_delay": 120, "retry_max_delay": 60})

        await async_migrate_entry(hass, entry)

        saved = hass.config_entries.async_update_entry.call_args.kwargs["options"]
        assert saved["retry_max_delay"] == saved["retry_base_delay"]

    async def test_empty_options_migrates_without_error(self):
        """An entry with no options at all migrates cleanly to version 2."""
        hass = _make_hass()
        hass.config_entries.async_update_entry = MagicMock()
        entry = self._make_entry_v1({})

        result = await async_migrate_entry(hass, entry)

        assert result is True
        call_kwargs = hass.config_entries.async_update_entry.call_args.kwargs
        assert call_kwargs["version"] == 2

    async def test_none_options_migrates_without_error(self):
        """An entry whose options is None migrates cleanly to version 2."""
        hass = _make_hass()
        hass.config_entries.async_update_entry = MagicMock()
        entry = self._make_entry_v1(None)

        result = await async_migrate_entry(hass, entry)

        assert result is True
        call_kwargs = hass.config_entries.async_update_entry.call_args.kwargs
        assert call_kwargs["version"] == 2


# ===========================================================================
# _setup_repairs
# ===========================================================================


class TestSetupRepairs:
    """Verify _setup_repairs() wires the lifecycle callback and reacts correctly."""

    def _make_channel_info(self, channel_id: str) -> MagicMock:
        info = MagicMock()
        info.id = channel_id
        info.label = channel_id.replace("notify.", "").replace("_", " ").title()
        return info

    def _make_record(self, channel_id: str, status: ChannelStatus) -> ChannelRecord:
        info = self._make_channel_info(channel_id)
        return ChannelRecord(info=info, adapter=None, status=status)

    def test_setup_repairs_registers_callback_on_channel_manager(self):
        """_setup_repairs() calls set_channel_lifecycle_callback on the channel manager."""
        hass = _make_hass()
        channel_manager = _make_channel_manager()
        channel_manager.set_channel_lifecycle_callback = MagicMock()

        _setup_repairs(hass, channel_manager)

        channel_manager.set_channel_lifecycle_callback.assert_called_once()
        cb = channel_manager.set_channel_lifecycle_callback.call_args[0][0]
        assert callable(cb)

    def test_callback_creates_repair_issue_for_stale_channel(self):
        """Callback invokes ir.async_create_issue with correct args when a channel goes STALE."""
        hass = _make_hass()
        channel_manager = _make_channel_manager()
        channel_manager.set_channel_lifecycle_callback = MagicMock()
        channel_manager.get_record = MagicMock(
            return_value=self._make_record(
                "notify.mobile_app_phone", ChannelStatus.STALE
            )
        )

        _setup_repairs(hass, channel_manager)
        cb = channel_manager.set_channel_lifecycle_callback.call_args[0][0]

        with patch("custom_components.ans.ir") as mock_ir:
            cb(["notify.mobile_app_phone"], [])

        mock_ir.async_create_issue.assert_called_once_with(
            hass,
            DOMAIN,
            f"{REPAIR_ISSUE_STALE_CHANNEL}_notify_mobile_app_phone",
            is_fixable=False,
            severity=mock_ir.IssueSeverity.WARNING,
            translation_key=REPAIR_ISSUE_STALE_CHANNEL,
            translation_placeholders={
                "channel_label": channel_manager.get_record.return_value.info.label,
                "channel_id": "notify.mobile_app_phone",
            },
        )

    def test_callback_deletes_repair_issue_for_recovered_channel(self):
        """Callback invokes ir.async_delete_issue with the correct issue ID when a channel recovers."""
        hass = _make_hass()
        channel_manager = _make_channel_manager()
        channel_manager.set_channel_lifecycle_callback = MagicMock()

        _setup_repairs(hass, channel_manager)
        cb = channel_manager.set_channel_lifecycle_callback.call_args[0][0]

        with patch("custom_components.ans.ir") as mock_ir:
            cb([], ["notify.mobile_app_phone"])

        mock_ir.async_delete_issue.assert_called_once_with(
            hass,
            DOMAIN,
            f"{REPAIR_ISSUE_STALE_CHANNEL}_notify_mobile_app_phone",
        )

    def test_callback_uses_channel_id_as_label_when_record_is_none(self):
        """When get_record returns None, channel_id is used as the label fallback."""
        hass = _make_hass()
        channel_manager = _make_channel_manager()
        channel_manager.set_channel_lifecycle_callback = MagicMock()
        channel_manager.get_record = MagicMock(return_value=None)

        _setup_repairs(hass, channel_manager)
        cb = channel_manager.set_channel_lifecycle_callback.call_args[0][0]

        with patch("custom_components.ans.ir") as mock_ir:
            cb(["notify.gone_channel"], [])

        call_kwargs = mock_ir.async_create_issue.call_args.kwargs
        assert (
            call_kwargs["translation_placeholders"]["channel_label"]
            == "notify.gone_channel"
        )

    async def test_post_setup_sweep_deletes_issues_for_active_channels(self):
        """After finalize_setup(), ir.async_delete_issue is called for each ACTIVE channel."""
        hass = _make_hass()
        entry = _make_entry()
        config_repo = _make_config_repo()
        system = _make_system()

        active_record = self._make_record(
            "notify.mobile_app_phone", ChannelStatus.ACTIVE
        )
        stale_record = self._make_record("notify.gone", ChannelStatus.STALE)
        system.channel_manager.get_all_records = MagicMock(
            return_value=[active_record, stale_record]
        )

        with (
            patch("custom_components.ans.VolumeRestorationRegistry") as mock_vol,
            patch("custom_components.ans._setup_config", return_value=config_repo),
            patch("custom_components.ans._setup_system", return_value=system),
            patch(
                "custom_components.ans._setup_persistence",
                return_value=([], []),
            ),
            patch("custom_components.ans._setup_tasks"),
            patch("custom_components.ans._setup_services"),
            patch("custom_components.ans._setup_listeners"),
            patch("custom_components.ans._setup_repairs"),
            patch("custom_components.ans.ir") as mock_ir,
        ):
            mock_vol.return_value.async_load = AsyncMock()
            await async_setup_entry(hass, entry)

        # Only the ACTIVE channel should trigger async_delete_issue in the sweep
        delete_calls = [
            call
            for call in mock_ir.async_delete_issue.call_args_list
            if call.args[2] == f"{REPAIR_ISSUE_STALE_CHANNEL}_notify_mobile_app_phone"
        ]
        assert len(delete_calls) == 1
        # STALE channel must NOT be cleaned up in the sweep
        stale_calls = [
            call
            for call in mock_ir.async_delete_issue.call_args_list
            if call.args[2] == f"{REPAIR_ISSUE_STALE_CHANNEL}_notify_gone"
        ]
        assert len(stale_calls) == 0

    async def test_async_setup_entry_calls_setup_repairs(self):
        """async_setup_entry() calls _setup_repairs() with hass and the channel manager."""
        hass = _make_hass()
        entry = _make_entry()
        config_repo = _make_config_repo()
        system = _make_system()

        with (
            patch("custom_components.ans.VolumeRestorationRegistry") as mock_vol,
            patch("custom_components.ans._setup_config", return_value=config_repo),
            patch("custom_components.ans._setup_system", return_value=system),
            patch(
                "custom_components.ans._setup_persistence",
                return_value=([], []),
            ),
            patch("custom_components.ans._setup_tasks"),
            patch("custom_components.ans._setup_services"),
            patch("custom_components.ans._setup_listeners"),
            patch("custom_components.ans._setup_repairs") as mock_setup_repairs,
            patch("custom_components.ans.ir"),
        ):
            mock_vol.return_value.async_load = AsyncMock()
            await async_setup_entry(hass, entry)

        mock_setup_repairs.assert_called_once_with(hass, system.channel_manager)


# ===========================================================================
# _setup_acknowledgement_tracking  (NH-3)
# ===========================================================================


class TestAcknowledgementTracking:
    """Verify _setup_acknowledgement_tracking() listener logic (NH-3)."""

    def _make_event(self, data: dict) -> MagicMock:
        ev = MagicMock()
        ev.data = data
        return ev

    def _setup(self):
        """Return (hass, entry, system, captured_listeners, dispatcher_cbs) ready for testing."""
        from custom_components.ans import (  # noqa: PLC0415
            _setup_acknowledgement_tracking,
        )

        hass = _make_hass()
        entry = _make_entry()
        system = _make_system()

        listeners: dict[str, Any] = {}

        def _capture_listen(event_type, callback, *args, **kwargs):
            listeners[event_type] = callback
            return MagicMock()

        hass.bus.async_listen = MagicMock(side_effect=_capture_listen)

        dispatcher_callbacks: list[Any] = []

        def _capture_dispatcher(h, signal, cb):
            dispatcher_callbacks.append(cb)
            return MagicMock()

        with patch(
            "custom_components.ans.async_dispatcher_connect",
            side_effect=_capture_dispatcher,
        ):
            _setup_acknowledgement_tracking(hass, entry, system)

        return hass, entry, system, listeners, dispatcher_callbacks

    def _collect_tasks(self, hass: MagicMock) -> list:
        """Wire hass.async_create_task to collect coroutines; return the list."""
        tasks: list = []

        def _collect(coro):
            tasks.append(coro)

        hass.async_create_task = _collect
        return tasks

    async def test_delivered_mobile_app_adds_to_pending(self):
        """ans_notification_delivered for mobile_app channel adds notification_id to pending acks."""
        hass, entry, system, listeners, _ = self._setup()

        ev = self._make_event(
            {"channel_id": "notify.mobile_app_phone", "notification_id": "nid-1"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "nid-1"})
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_called_once()
        assert hass.bus.async_fire.call_args.args[0] == EVENT_NOTIFICATION_ACKNOWLEDGED

    async def test_delivered_persistent_notification_adds_to_pending(self):
        """ans_notification_delivered for persistent_notification channel adds to pending acks."""
        hass, entry, system, listeners, dispatcher_cbs = self._setup()

        ev = self._make_event(
            {
                "channel_id": PERSISTENT_NOTIFICATION_CHANNEL,
                "notification_id": "pn-nid-1",
            }
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        await dispatcher_cbs[0](PNUpdateType.REMOVED, {"pn-nid-1": {}})

        hass.bus.async_fire.assert_called_once()
        assert hass.bus.async_fire.call_args.args[0] == EVENT_NOTIFICATION_ACKNOWLEDGED

    async def test_delivered_other_channel_not_added_to_pending(self):
        """ans_notification_delivered for a non-mobile-app, non-pn channel does not add to pending."""
        hass, entry, system, listeners, _ = self._setup()

        ev = self._make_event(
            {"channel_id": "notify.signal", "notification_id": "nid-signal"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "nid-signal"})
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_not_called()

    async def test_mobile_app_action_fires_ack_event(self):
        """mobile_app_notification_action fires ans_notification_acknowledged with correct fields."""
        hass, entry, system, listeners, _ = self._setup()

        ev = self._make_event(
            {"channel_id": "notify.mobile_app_phone", "notification_id": "nid-ack"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "nid-ack"})
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args.args
        assert event_name == EVENT_NOTIFICATION_ACKNOWLEDGED
        assert payload["notification_id"] == "nid-ack"
        assert payload["channel_id"] == "mobile_app"
        assert "acknowledged_at" in payload

    async def test_mobile_app_action_unknown_tag_ignored(self):
        """mobile_app_notification_action with an unknown tag does not fire any event."""
        hass, entry, system, listeners, _ = self._setup()

        action_ev = self._make_event({"tag": "not-in-pending"})
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_not_called()

    async def test_persistent_notification_removed_fires_ack_event(self):
        """Dispatcher REMOVED signal for a pending pn fires ans_notification_acknowledged."""
        hass, entry, system, listeners, dispatcher_cbs = self._setup()

        ev = self._make_event(
            {"channel_id": PERSISTENT_NOTIFICATION_CHANNEL, "notification_id": "pn-1"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        await dispatcher_cbs[0](PNUpdateType.REMOVED, {"pn-1": {}})

        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args.args
        assert event_name == EVENT_NOTIFICATION_ACKNOWLEDGED
        assert payload["notification_id"] == "pn-1"
        assert payload["channel_id"] == PERSISTENT_NOTIFICATION_CHANNEL

    async def test_persistent_notification_removed_unknown_id_ignored(self):
        """Dispatcher REMOVED signal for an unknown notification_id does nothing."""
        hass, entry, system, listeners, dispatcher_cbs = self._setup()

        await dispatcher_cbs[0](PNUpdateType.REMOVED, {"not-pending": {}})

        hass.bus.async_fire.assert_not_called()

    async def test_second_ack_does_not_fire_event(self):
        """When record_acknowledgement returns False (duplicate), no event is fired."""
        hass, entry, system, listeners, _ = self._setup()

        system.acknowledgement_registry.record_acknowledgement = AsyncMock(
            return_value=False
        )

        ev = self._make_event(
            {"channel_id": "notify.mobile_app_phone", "notification_id": "nid-dup"}
        )
        await listeners[EVENT_NOTIFICATION_DELIVERED](ev)

        action_ev = self._make_event({"tag": "nid-dup"})
        await listeners["mobile_app_notification_action"](action_ev)

        hass.bus.async_fire.assert_not_called()

    async def test_async_setup_entry_calls_acknowledgement_tracking(self):
        """async_setup_entry() calls _setup_acknowledgement_tracking with hass, entry, system."""
        hass = _make_hass()
        entry = _make_entry()
        config_repo = _make_config_repo()
        system = _make_system()

        with (
            patch("custom_components.ans.VolumeRestorationRegistry") as mock_vol,
            patch("custom_components.ans._setup_config", return_value=config_repo),
            patch("custom_components.ans._setup_system", return_value=system),
            patch("custom_components.ans._setup_persistence", return_value=([], [])),
            patch("custom_components.ans._setup_tasks"),
            patch("custom_components.ans._setup_services"),
            patch("custom_components.ans._setup_listeners"),
            patch("custom_components.ans._setup_repairs"),
            patch("custom_components.ans._setup_acknowledgement_tracking") as mock_ack,
            patch("custom_components.ans.ir"),
        ):
            mock_vol.return_value.async_load = AsyncMock()
            await async_setup_entry(hass, entry)

        mock_ack.assert_called_once_with(hass, entry, system)
