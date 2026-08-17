"""Unit tests for the ANS integration bootstrap (__init__.py).

Coverage targets
----------------
- _setup_config          — happy path + ConfigEntryNotReady on load failure
- _setup_system          — channel sync with / without system_config
- _setup_persistence     — delegates to async_initialize_persistence
- _setup_tasks           — delegates to ANSSystem.start() (behavioral tests live
                           in tests/delivery/test_factory.py::TestAnsSystemStart)
- _setup_services        — delegates to async_setup_services
- _setup_repairs         — delegates to register_stale_channel_repairs (behavioral
                           tests live in tests/channels/test_channel_manager.py)
- _setup_listeners       — delegates to register_channel_resync_listeners
                           (behavioral tests live in tests/channels/test_channel_manager.py)
- _setup_acknowledgement_tracking — delegates to async_setup_acknowledgement_tracking
                           (behavioral tests live in tests/test_acknowledgement.py)
- async_setup_entry      — happy path, ConfigEntryNotReady passthrough,
                           generic exception wrapping, cleanup on failure,
                           phase-delegate wiring, post-restart Repairs sweep
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

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.ans import (
    _cleanup_entry_data,
    _get_entry_data,
    _setup_acknowledgement_tracking,
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
from custom_components.ans.const import REPAIR_ISSUE_STALE_CHANNEL

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
    hass.async_add_executor_job = AsyncMock()
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
    system.acknowledgement_registry.get_pending_channel_ids = AsyncMock(return_value={})
    system.acknowledgement_registry.get_pending_mobile_tags = AsyncMock(return_value={})
    return system


def _make_channel_info(channel_id: str) -> MagicMock:
    """Return a mock ChannelInfo with id and a human-friendly label derived from it."""
    info = MagicMock()
    info.id = channel_id
    info.label = channel_id.replace("notify.", "").replace("_", " ").title()
    return info


def _make_record(channel_id: str, status: ChannelStatus) -> ChannelRecord:
    """Return a ChannelRecord for *channel_id* with the given status."""
    info = _make_channel_info(channel_id)
    return ChannelRecord(info=info, adapter=None, status=status)


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
    """Verify _setup_tasks() delegates to system.start() with the recovered tasks/retries."""

    async def test_delegates_to_system_start(self):
        """_setup_tasks() awaits system.start(pending_tasks, orphaned_retries)."""
        system = _make_system()
        system.start = AsyncMock()
        task = make_task()
        pending = [(task, datetime.now(UTC))]
        orphaned = ["orphan-1"]

        await _setup_tasks(system, pending, orphaned)

        system.start.assert_awaited_once_with(pending, orphaned)


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
    """Verify _setup_listeners() delegates to register_channel_resync_listeners()."""

    async def test_delegates_to_register_channel_resync_listeners(self):
        """_setup_listeners() calls register_channel_resync_listeners(hass, entry, channel_manager)."""
        hass = _make_hass()
        entry = _make_entry()
        channel_manager = _make_channel_manager()

        with patch(
            "custom_components.ans.register_channel_resync_listeners"
        ) as mock_register:
            _setup_listeners(hass, entry, channel_manager)

        mock_register.assert_called_once_with(hass, entry, channel_manager)


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

    async def test_post_setup_sweep_deletes_issues_for_active_channels(self):
        """After finalize_setup(), ir.async_delete_issue is called for each ACTIVE channel."""
        hass = _make_hass()
        entry = _make_entry()
        config_repo = _make_config_repo()
        system = _make_system()

        active_record = _make_record("notify.mobile_app_phone", ChannelStatus.ACTIVE)
        stale_record = _make_record("notify.gone", ChannelStatus.STALE)
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
            patch(
                "custom_components.ans._setup_acknowledgement_tracking",
                new_callable=AsyncMock,
            ) as mock_ack,
            patch("custom_components.ans.ir"),
        ):
            mock_vol.return_value.async_load = AsyncMock()
            await async_setup_entry(hass, entry)

        mock_ack.assert_called_once_with(hass, entry, system)


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
    """Verify _setup_repairs() delegates to register_stale_channel_repairs()."""

    def test_delegates_to_register_stale_channel_repairs(self):
        """_setup_repairs() calls register_stale_channel_repairs(hass, channel_manager)."""
        hass = _make_hass()
        channel_manager = _make_channel_manager()

        with patch(
            "custom_components.ans.register_stale_channel_repairs"
        ) as mock_register:
            _setup_repairs(hass, channel_manager)

        mock_register.assert_called_once_with(hass, channel_manager)


# ===========================================================================
# _setup_acknowledgement_tracking  (NH-3)
# ===========================================================================


class TestAcknowledgementTracking:
    """Verify _setup_acknowledgement_tracking() delegates to async_setup_acknowledgement_tracking()."""

    async def test_delegates_to_async_setup_acknowledgement_tracking(self):
        """_setup_acknowledgement_tracking() awaits async_setup_acknowledgement_tracking(hass, entry, system)."""
        hass = _make_hass()
        entry = _make_entry()
        system = _make_system()

        with patch(
            "custom_components.ans.async_setup_acknowledgement_tracking",
            new_callable=AsyncMock,
        ) as mock_tracking:
            await _setup_acknowledgement_tracking(hass, entry, system)

        mock_tracking.assert_awaited_once_with(hass, entry, system)
