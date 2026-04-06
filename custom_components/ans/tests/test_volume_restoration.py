"""Comprehensive unit tests for VolumeRestorationRegistry."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from ..exceptions import TTSVolumeControlError
from ..persistence.volume_restoration import (
    DEFAULT_TIMEOUT,
    VOLUME_CHANGE_THRESHOLD,
    VolumeIntent,
    VolumeRestorationRegistry,
    _parse_dt,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    from homeassistant.util import dt as dt_util

    return dt_util.utcnow()


def _make_hass(entity_volume: float | None = 0.5):
    """Return a minimal MagicMock for hass with one media-player state."""
    hass = MagicMock()
    hass.bus.async_listen = MagicMock(return_value=MagicMock())

    state = MagicMock()
    state.state = "playing"
    state.attributes = (
        {"volume_level": entity_volume} if entity_volume is not None else {}
    )
    hass.states.get = MagicMock(return_value=state)
    hass.services.async_call = AsyncMock()
    return hass


def _make_registry(entity_volume: float | None = 0.5) -> VolumeRestorationRegistry:
    hass = _make_hass(entity_volume)
    registry = VolumeRestorationRegistry(hass)
    return registry


def _future_iso(delta_seconds: int = DEFAULT_TIMEOUT) -> str:
    return (_now_utc() + timedelta(seconds=delta_seconds)).isoformat()


def _past_iso(delta_seconds: int = DEFAULT_TIMEOUT) -> str:
    return (_now_utc() - timedelta(seconds=delta_seconds)).isoformat()


def _make_intent(
    entity_id: str = "media_player.test",
    original_volume: float = 0.4,
    override_volume: float = 0.6,
    expired: bool = False,
) -> VolumeIntent:
    return VolumeIntent(
        entity_id=entity_id,
        original_volume=original_volume,
        override_volume=override_volume,
        timestamp=_now_utc().isoformat(),
        timeout=_past_iso(1) if expired else _future_iso(),
    )


# ===========================================================================
# _parse_dt
# ===========================================================================


class TestParseDt:
    def test_parses_utc_aware_timestamp(self):
        ts = "2024-01-01T12:00:00+00:00"
        dt = _parse_dt(ts)
        assert dt.tzinfo is not None

    def test_makes_naive_timestamp_utc_aware(self):
        ts = "2024-01-01T12:00:00"
        dt = _parse_dt(ts)
        assert dt.tzinfo is not None
        assert dt.tzinfo == UTC

    def test_raises_value_error_on_invalid_string(self):
        with pytest.raises(ValueError):
            _parse_dt("not-a-date")


# ===========================================================================
# VolumeIntent
# ===========================================================================


class TestVolumeIntent:
    def test_to_dict_and_from_dict_roundtrip(self):
        intent = _make_intent()
        as_dict = intent.to_dict()
        restored = VolumeIntent.from_dict(as_dict)
        assert restored == intent

    def test_to_dict_returns_typed_dict(self):
        intent = _make_intent()
        d = intent.to_dict()
        assert isinstance(d, dict)
        assert "entity_id" in d
        assert "original_volume" in d
        assert "override_volume" in d
        assert "timestamp" in d
        assert "timeout" in d


# ===========================================================================
# async_load
# ===========================================================================


class TestAsyncLoad:
    async def test_load_empty_storage(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_load = AsyncMock(return_value=None)

        await registry.async_load()

        assert registry._intents == {}

    async def test_load_with_stored_intents(self):
        registry = _make_registry()
        intent = _make_intent("media_player.living_room")
        registry._store = MagicMock()
        registry._store.async_load = AsyncMock(
            return_value={"intents": [intent.to_dict()]}
        )
        # Prevent _restore_pending_volumes from calling real hass
        with patch.object(registry, "_restore_pending_volumes", AsyncMock()):
            await registry.async_load()

        assert "media_player.living_room" in registry._intents

    async def test_load_corrupted_storage_resets_intents(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_load = AsyncMock(side_effect=OSError("disk full"))

        await registry.async_load()

        assert registry._intents == {}

    async def test_load_missing_intents_key(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_load = AsyncMock(return_value={"version": 1})

        await registry.async_load()

        assert registry._intents == {}

    async def test_load_registers_state_listener(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_load = AsyncMock(return_value=None)

        await registry.async_load()

        registry._hass.bus.async_listen.assert_called_once()
        assert registry._state_unsub is not None


# ===========================================================================
# async_unload
# ===========================================================================


class TestAsyncUnload:
    async def test_unload_cancels_background_tasks(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_save = AsyncMock()
        registry._store.async_delay_save = MagicMock()

        async def _long_task():
            await asyncio.sleep(999)

        task = asyncio.create_task(_long_task())
        registry._background_tasks.add(task)
        registry._cleanup_unsub = MagicMock()
        registry._state_unsub = MagicMock()

        await registry.async_unload()

        assert task.cancelled()

    async def test_unload_cancels_fallback_tasks(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_save = AsyncMock()
        registry._store.async_delay_save = MagicMock()

        async def _fallback():
            await asyncio.sleep(999)

        task = asyncio.create_task(_fallback())
        registry._fallback_tasks["media_player.test"] = task
        registry._cleanup_unsub = MagicMock()
        registry._state_unsub = MagicMock()

        await registry.async_unload()

        assert task.cancelled()
        assert registry._fallback_tasks == {}

    async def test_unload_unsubscribes_listeners(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_save = AsyncMock()
        registry._store.async_delay_save = MagicMock()

        cleanup_unsub = MagicMock()
        state_unsub = MagicMock()
        registry._cleanup_unsub = cleanup_unsub
        registry._state_unsub = state_unsub

        await registry.async_unload()

        cleanup_unsub.assert_called_once()
        state_unsub.assert_called_once()

    async def test_unload_saves_final_state(self):
        registry = _make_registry()
        registry._store = MagicMock()
        save_mock = AsyncMock()
        registry._store.async_save = save_mock
        registry._store.async_delay_save = MagicMock()
        registry._cleanup_unsub = MagicMock()
        registry._state_unsub = MagicMock()

        await registry.async_unload()

        save_mock.assert_awaited_once()


# ===========================================================================
# capture_volume_intent
# ===========================================================================


class TestCaptureVolumeIntent:
    async def test_captures_current_volume(self):
        registry = _make_registry(entity_volume=0.5)
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        await registry.capture_volume_intent("media_player.test")

        assert "media_player.test" in registry._intents
        assert registry._intents["media_player.test"].original_volume == 0.5

    async def test_raises_if_entity_not_found(self):
        registry = _make_registry()
        registry._hass.states.get = MagicMock(return_value=None)

        with pytest.raises(HomeAssistantError, match="not found"):
            await registry.capture_volume_intent("media_player.missing")

    async def test_raises_if_volume_level_not_reported(self):
        hass = _make_hass(entity_volume=None)
        registry = VolumeRestorationRegistry(hass)
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        with pytest.raises(HomeAssistantError, match="volume_level"):
            await registry.capture_volume_intent("media_player.test")

    async def test_raises_for_non_positive_timeout(self):
        registry = _make_registry()

        with pytest.raises(ValueError, match="timeout_seconds must be positive"):
            await registry.capture_volume_intent("media_player.test", timeout_seconds=0)

        with pytest.raises(ValueError, match="timeout_seconds must be positive"):
            await registry.capture_volume_intent(
                "media_player.test", timeout_seconds=-10
            )

    async def test_raises_for_out_of_range_override_volume(self):
        registry = _make_registry()

        with pytest.raises(ValueError, match="override_volume"):
            await registry.capture_volume_intent(
                "media_player.test", override_volume=1.5
            )

        with pytest.raises(ValueError, match="override_volume"):
            await registry.capture_volume_intent(
                "media_player.test", override_volume=-0.1
            )

    async def test_carries_forward_existing_original_volume(self):
        """When a non-expired intent exists, keeps its original_volume."""
        registry = _make_registry(entity_volume=0.8)  # TTS-set volume
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        # First capture at real volume 0.4
        registry._hass.states.get.return_value.attributes = {"volume_level": 0.4}
        await registry.capture_volume_intent("media_player.test")

        # Second capture while volume shows 0.8 (TTS level)
        registry._hass.states.get.return_value.attributes = {"volume_level": 0.8}
        await registry.capture_volume_intent("media_player.test")

        # Should preserve 0.4, not read 0.8
        assert registry._intents["media_player.test"].original_volume == 0.4

    async def test_does_not_carry_forward_expired_original_volume(self):
        """An expired intent should not carry forward its original_volume."""
        registry = _make_registry(entity_volume=0.3)
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        # Inject an expired intent
        expired = _make_intent(original_volume=0.9, expired=True)
        registry._intents["media_player.test"] = expired

        registry._hass.states.get.return_value.attributes = {"volume_level": 0.3}
        await registry.capture_volume_intent("media_player.test")

        # Should use current volume 0.3, not the stale 0.9
        assert registry._intents["media_player.test"].original_volume == 0.3

    async def test_cancels_pending_restore_task(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        async def _dummy():
            await asyncio.sleep(999)

        pending = asyncio.create_task(_dummy())
        registry._restore_tasks["media_player.test"] = pending

        await registry.capture_volume_intent("media_player.test")

        # Yield to let the cancellation take effect
        await asyncio.sleep(0)
        assert pending.cancelled()
        assert "media_player.test" not in registry._restore_tasks

    async def test_sets_override_volume_when_provided(self):
        registry = _make_registry(entity_volume=0.4)
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        await registry.capture_volume_intent("media_player.test", override_volume=0.7)

        assert registry._intents["media_player.test"].override_volume == 0.7


# ===========================================================================
# restore_volume / _do_restore
# ===========================================================================


class TestRestoreVolume:
    async def test_restore_no_intent_is_noop(self):
        registry = _make_registry()

        # Must not raise; _set_volume should never be called
        with patch.object(registry, "_set_volume", AsyncMock()) as mock_set:
            await registry.restore_volume("media_player.test")
        mock_set.assert_not_awaited()

    async def test_restore_calls_set_volume_with_original(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        intent = _make_intent(original_volume=0.3, override_volume=0.7)
        registry._intents["media_player.test"] = intent

        with patch.object(registry, "_set_volume", AsyncMock()) as mock_set:
            await registry.restore_volume("media_player.test")

        mock_set.assert_awaited_once_with("media_player.test", 0.3)

    async def test_restore_clears_intent_even_on_failure(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        intent = _make_intent()
        registry._intents["media_player.test"] = intent

        with patch.object(
            registry,
            "_set_volume",
            side_effect=TTSVolumeControlError("fail"),
        ):
            await registry.restore_volume("media_player.test")

        # Intent must be removed even after failure
        assert "media_player.test" not in registry._intents


# ===========================================================================
# _set_volume
# ===========================================================================


class TestSetVolume:
    async def test_set_volume_calls_service(self):
        registry = _make_registry()

        await registry._set_volume("media_player.test", 0.5)

        registry._hass.services.async_call.assert_awaited_once()
        call_kwargs = registry._hass.services.async_call.call_args
        assert call_kwargs[0][0] == "media_player"
        assert call_kwargs[0][2]["volume_level"] == 0.5

    async def test_set_volume_clamps_above_one(self):
        registry = _make_registry()

        await registry._set_volume("media_player.test", 1.5)

        call_kwargs = registry._hass.services.async_call.call_args
        assert call_kwargs[0][2]["volume_level"] == 1.0

    async def test_set_volume_clamps_below_zero(self):
        registry = _make_registry()

        await registry._set_volume("media_player.test", -0.1)

        call_kwargs = registry._hass.services.async_call.call_args
        assert call_kwargs[0][2]["volume_level"] == 0.0

    async def test_set_volume_raises_on_timeout(self):
        registry = _make_registry()
        registry._hass.services.async_call = AsyncMock(side_effect=TimeoutError)

        with pytest.raises(TTSVolumeControlError, match="timed out"):
            await registry._set_volume("media_player.test", 0.5)

    async def test_set_volume_raises_on_ha_error(self):
        registry = _make_registry()
        registry._hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("service error")
        )

        with pytest.raises(TTSVolumeControlError, match="Failed to set volume"):
            await registry._set_volume("media_player.test", 0.5)


# ===========================================================================
# complete_intent
# ===========================================================================


class TestCompleteIntent:
    async def test_complete_intent_removes_entry(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        registry._intents["media_player.test"] = _make_intent()
        await registry.complete_intent("media_player.test")

        assert "media_player.test" not in registry._intents

    async def test_complete_intent_noop_if_no_intent(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        # Must not raise
        await registry.complete_intent("media_player.does_not_exist")

    async def test_complete_intent_cancels_fallback_task(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        async def _fallback():
            await asyncio.sleep(999)

        task = asyncio.create_task(_fallback())
        registry._fallback_tasks["media_player.test"] = task
        registry._intents["media_player.test"] = _make_intent()

        await registry.complete_intent("media_player.test")

        # Yield to let the cancellation take effect
        await asyncio.sleep(0)
        assert task.cancelled()


# ===========================================================================
# _handle_state_change
# ===========================================================================


class TestHandleStateChange:
    def _make_event(
        self,
        entity_id: str,
        new_state_val: str,
        old_state_val: str | None,
        volume: float = 0.5,
    ) -> MagicMock:
        new_state = MagicMock()
        new_state.state = new_state_val
        new_state.attributes = {"volume_level": volume}

        old_state = MagicMock() if old_state_val is not None else None
        if old_state:
            old_state.state = old_state_val

        event = MagicMock()
        event.data = {
            "entity_id": entity_id,
            "new_state": new_state,
            "old_state": old_state,
        }
        return event

    def test_ignores_entity_without_intent(self):
        registry = _make_registry()
        event = self._make_event("media_player.other", "idle", "playing")

        # No exception, no side effect
        registry._handle_state_change(event)

    def test_ignores_missing_entity_id(self):
        registry = _make_registry()
        event = MagicMock()
        event.data = {"entity_id": None, "new_state": MagicMock(), "old_state": None}
        registry._handle_state_change(event)  # must not raise

    def test_ignores_missing_new_state(self):
        registry = _make_registry()
        registry._intents["media_player.test"] = _make_intent()
        event = MagicMock()
        event.data = {"entity_id": "media_player.test", "new_state": None}
        registry._handle_state_change(event)  # must not raise

    async def test_user_volume_change_starts_complete_intent_task(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        intent = _make_intent(override_volume=0.5)
        registry._intents["media_player.test"] = intent

        # Volume changed by >VOLUME_CHANGE_THRESHOLD beyond override
        event = self._make_event(
            "media_player.test",
            "playing",
            "playing",
            volume=0.5 + VOLUME_CHANGE_THRESHOLD + 0.01,
        )
        registry._handle_state_change(event)

        # A background task should have been scheduled
        assert len(registry._background_tasks) >= 1

    async def test_echo_guard_suppresses_fast_state_feedback(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        intent = _make_intent(override_volume=0.5)
        registry._intents["media_player.test"] = intent

        # Record a very recent volume_set (within echo guard window)
        registry._last_volume_set_time["media_player.test"] = _now_utc()

        # Volume looks user-changed but it's within echo guard
        event = self._make_event(
            "media_player.test",
            "playing",
            "playing",
            volume=0.5 + VOLUME_CHANGE_THRESHOLD + 0.01,
        )
        initial_task_count = len(registry._background_tasks)
        registry._handle_state_change(event)

        # Echo guard should have fired `return` — no new task
        assert len(registry._background_tasks) == initial_task_count

    async def test_idle_transition_schedules_delayed_restore(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        intent = _make_intent(override_volume=0.5)
        registry._intents["media_player.test"] = intent

        # Volume unchanged → no user-change detection
        event = self._make_event(
            "media_player.test",
            "idle",
            "playing",
            volume=0.5,  # exactly equal to override_volume
        )
        registry._handle_state_change(event)

        assert "media_player.test" in registry._restore_tasks
        # Clean up the created task
        registry._restore_tasks["media_player.test"].cancel()

    def test_idle_to_idle_does_not_schedule_restore(self):
        registry = _make_registry()
        registry._intents["media_player.test"] = _make_intent(override_volume=0.5)

        event = self._make_event("media_player.test", "idle", "idle", volume=0.5)
        registry._handle_state_change(event)

        assert "media_player.test" not in registry._restore_tasks

    def test_user_change_skipped_if_active_delivery(self):
        """User-change detection is bypassed while a delivery is active."""
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        intent = _make_intent(override_volume=0.5)
        registry._intents["media_player.test"] = intent
        registry._active_delivery.add("media_player.test")

        # Big volume delta that normally would trigger user-change detection
        event = self._make_event(
            "media_player.test",
            "playing",
            "playing",
            volume=0.5 + VOLUME_CHANGE_THRESHOLD + 0.1,
        )
        initial_task_count = len(registry._background_tasks)
        registry._handle_state_change(event)

        # No complete_intent task should have been spawned
        assert len(registry._background_tasks) == initial_task_count


# ===========================================================================
# _delayed_restore
# ===========================================================================


class TestDelayedRestore:
    async def test_delayed_restore_calls_do_restore(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        intent = _make_intent()
        registry._intents["media_player.test"] = intent

        with (
            patch("asyncio.sleep", AsyncMock()),
            patch.object(registry, "_do_restore", AsyncMock()) as mock_restore,
        ):
            await registry._delayed_restore("media_player.test")

        mock_restore.assert_awaited_once()

    async def test_delayed_restore_skips_if_intent_gone(self):
        registry = _make_registry()

        with (
            patch("asyncio.sleep", AsyncMock()),
            patch.object(registry, "_do_restore", AsyncMock()) as mock_restore,
        ):
            await registry._delayed_restore("media_player.test")

        mock_restore.assert_not_awaited()

    async def test_delayed_restore_skips_if_active_delivery(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        registry._intents["media_player.test"] = _make_intent()
        registry._active_delivery.add("media_player.test")

        with (
            patch("asyncio.sleep", AsyncMock()),
            patch.object(registry, "_do_restore", AsyncMock()) as mock_restore,
        ):
            await registry._delayed_restore("media_player.test")

        mock_restore.assert_not_awaited()

    async def test_delayed_restore_removes_expired_intent(self):
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        expired_intent = _make_intent(expired=True)
        registry._intents["media_player.test"] = expired_intent

        with (
            patch("asyncio.sleep", AsyncMock()),
            patch.object(registry, "_do_restore", AsyncMock()) as mock_restore,
        ):
            await registry._delayed_restore("media_player.test")

        mock_restore.assert_not_awaited()
        # Intent should have been cleaned up
        assert "media_player.test" not in registry._intents


# ===========================================================================
# has_active_intent / mark_delivery_active / mark_delivery_inactive
# ===========================================================================


class TestDeliveryMarkers:
    def test_has_active_intent_false_when_no_intent(self):
        registry = _make_registry()
        assert registry.has_active_intent("media_player.test") is False

    def test_has_active_intent_true_when_intent_exists(self):
        registry = _make_registry()
        registry._intents["media_player.test"] = _make_intent()
        assert registry.has_active_intent("media_player.test") is True

    def test_mark_delivery_active_and_inactive(self):
        registry = _make_registry()
        registry.mark_delivery_active("media_player.test")
        assert "media_player.test" in registry._active_delivery

        registry.mark_delivery_inactive("media_player.test")
        assert "media_player.test" not in registry._active_delivery

    def test_mark_delivery_inactive_noop_if_not_active(self):
        registry = _make_registry()
        # Must not raise
        registry.mark_delivery_inactive("media_player.missing")


# ===========================================================================
# record_volume_set_time
# ===========================================================================


class TestRecordVolumeSetTime:
    def test_records_current_time(self):
        registry = _make_registry()
        before = _now_utc()
        registry.record_volume_set_time("media_player.test")
        after = _now_utc()
        recorded = registry._last_volume_set_time["media_player.test"]
        assert before <= recorded <= after


# ===========================================================================
# set_fallback_task / cancel_fallback_task
# ===========================================================================


class TestFallbackTasks:
    async def test_set_fallback_task_replaces_existing(self):
        registry = _make_registry()

        async def _dummy():
            await asyncio.sleep(999)

        old_task = asyncio.create_task(_dummy())
        registry._fallback_tasks["media_player.test"] = old_task

        new_task = asyncio.create_task(_dummy())
        registry.set_fallback_task("media_player.test", new_task)

        await asyncio.sleep(0)  # yield so cancellation takes effect
        assert old_task.cancelled()
        assert registry._fallback_tasks["media_player.test"] is new_task
        new_task.cancel()

    def test_cancel_fallback_task_noop_if_none(self):
        registry = _make_registry()
        registry.cancel_fallback_task("media_player.missing")  # must not raise

    async def test_cancel_fallback_task_cancels_running_task(self):
        registry = _make_registry()

        async def _dummy():
            await asyncio.sleep(999)

        task = asyncio.create_task(_dummy())
        registry._fallback_tasks["media_player.test"] = task

        registry.cancel_fallback_task("media_player.test")
        await asyncio.sleep(0)  # yield so cancellation takes effect
        assert task.cancelled()


# ===========================================================================
# schedule_idle_restore
# ===========================================================================


class TestScheduleIdleRestore:
    def test_schedule_idle_restore_noop_if_no_intent(self):
        registry = _make_registry()
        registry.schedule_idle_restore("media_player.test")
        assert "media_player.test" not in registry._restore_tasks

    async def test_schedule_idle_restore_noop_if_task_already_pending(self):
        registry = _make_registry()
        registry._intents["media_player.test"] = _make_intent()

        async def _pending():
            await asyncio.sleep(999)

        existing = asyncio.create_task(_pending())
        registry._restore_tasks["media_player.test"] = existing

        registry.schedule_idle_restore("media_player.test")

        # Should keep the existing task, not create a new one
        assert registry._restore_tasks["media_player.test"] is existing
        existing.cancel()  # clean up

    async def test_schedule_idle_restore_creates_task_when_none_pending(self):
        registry = _make_registry()
        registry._intents["media_player.test"] = _make_intent()

        registry.schedule_idle_restore("media_player.test")

        assert "media_player.test" in registry._restore_tasks
        # Clean up
        registry._restore_tasks["media_player.test"].cancel()


# ===========================================================================
# apply_volume
# ===========================================================================


class TestApplyVolume:
    async def test_apply_volume_captures_then_sets(self):
        registry = _make_registry(entity_volume=0.4)

        with (
            patch.object(registry, "capture_volume_intent", AsyncMock()) as mock_cap,
            patch.object(registry, "_set_volume", AsyncMock()),
            patch.object(registry, "record_volume_set_time", MagicMock()),
        ):
            await registry.apply_volume("media_player.test", 0.7)

        mock_cap.assert_awaited_once_with("media_player.test", override_volume=0.7)

    async def test_apply_volume_clears_intent_when_set_fails(self):
        registry = _make_registry(entity_volume=0.4)

        with (
            patch.object(registry, "capture_volume_intent", AsyncMock()),
            patch.object(
                registry,
                "_set_volume",
                AsyncMock(side_effect=TTSVolumeControlError("failure")),
            ),
            patch.object(registry, "complete_intent", AsyncMock()) as mock_complete,
        ):
            with pytest.raises(TTSVolumeControlError):
                await registry.apply_volume("media_player.test", 0.7)

        mock_complete.assert_awaited_once_with("media_player.test")

    async def test_apply_volume_records_set_time_on_success(self):
        registry = _make_registry(entity_volume=0.4)

        with (
            patch.object(registry, "capture_volume_intent", AsyncMock()),
            patch.object(registry, "_set_volume", AsyncMock()),
            patch.object(
                registry, "record_volume_set_time", MagicMock()
            ) as mock_record,
        ):
            await registry.apply_volume("media_player.test", 0.7)

        mock_record.assert_called_once_with("media_player.test")


# ===========================================================================
# _on_background_task_done — exception surfacing
# ===========================================================================


class TestOnBackgroundTaskDone:
    async def test_logs_exception_from_failed_background_task(self, caplog):
        import logging

        registry = _make_registry()

        async def _fail():
            raise RuntimeError("explosion")

        task = asyncio.create_task(_fail())
        registry._background_tasks.add(task)

        try:
            await task
        except RuntimeError:
            pass

        with caplog.at_level(
            logging.ERROR,
            logger="custom_components.ans.persistence.volume_restoration",
        ):
            registry._on_background_task_done(task)

        assert any("explosion" in r.message for r in caplog.records)
