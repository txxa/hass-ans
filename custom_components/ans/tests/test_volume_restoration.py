"""Comprehensive unit tests for VolumeRestorationRegistry."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

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
    """Return the current UTC datetime via dt_util.utcnow()."""
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
    """Return a VolumeRestorationRegistry backed by a mock hass with one media-player state."""
    hass = _make_hass(entity_volume)
    return VolumeRestorationRegistry(hass)


def _future_iso(delta_seconds: int = DEFAULT_TIMEOUT) -> str:
    """Return an ISO 8601 string delta_seconds into the future."""
    return (_now_utc() + timedelta(seconds=delta_seconds)).isoformat()


def _past_iso(delta_seconds: int = DEFAULT_TIMEOUT) -> str:
    """Return an ISO 8601 string delta_seconds in the past."""
    return (_now_utc() - timedelta(seconds=delta_seconds)).isoformat()


def _make_intent(
    entity_id: str = "media_player.test",
    original_volume: float = 0.4,
    override_volume: float = 0.6,
    expired: bool = False,
) -> VolumeIntent:
    """Return a VolumeIntent; timeout is set in the past when expired=True, otherwise in the future."""
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
    """Verify _parse_dt() handles UTC-aware timestamps, naive timestamps, and invalid strings."""

    def test_parses_utc_aware_timestamp(self):
        """_parse_dt() returns a timezone-aware datetime for a UTC offset string."""
        ts = "2024-01-01T12:00:00+00:00"
        dt = _parse_dt(ts)
        assert dt.tzinfo is not None

    def test_makes_naive_timestamp_utc_aware(self):
        """_parse_dt() attaches UTC timezone to a naive datetime string."""
        ts = "2024-01-01T12:00:00"
        dt = _parse_dt(ts)
        assert dt.tzinfo is not None
        assert dt.tzinfo == UTC

    def test_raises_value_error_on_invalid_string(self):
        """_parse_dt() raises ValueError for non-datetime input strings."""
        with pytest.raises(ValueError):
            _parse_dt("not-a-date")


# ===========================================================================
# VolumeIntent
# ===========================================================================


class TestVolumeIntent:
    """Verify VolumeIntent to_dict/from_dict round-trip produces an equal object with the expected keys."""

    def test_to_dict_and_from_dict_roundtrip(self):
        """VolumeIntent.to_dict() / from_dict() produces an equal VolumeIntent."""
        intent = _make_intent()
        as_dict = intent.to_dict()
        restored = VolumeIntent.from_dict(as_dict)
        assert restored == intent

    def test_to_dict_returns_typed_dict(self):
        """VolumeIntent.to_dict() returns a plain dict containing all required keys."""
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
    """Verify async_load() handles empty storage, stored intents, corrupt data, missing keys, and listener setup."""

    async def test_load_empty_storage(self):
        """async_load() with a None store result initialises _intents to an empty dict."""
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_load = AsyncMock(return_value=None)

        await registry.async_load()

        assert registry._intents == {}

    async def test_load_with_stored_intents(self):
        """async_load() restores persisted intents, keyed by entity_id."""
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
        """async_load() resets _intents to {} when the store raises OSError."""
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_load = AsyncMock(side_effect=OSError("disk full"))

        await registry.async_load()

        assert registry._intents == {}

    async def test_load_missing_intents_key(self):
        """async_load() treats a stored dict without an 'intents' key as empty."""
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_load = AsyncMock(return_value={"version": 1})

        await registry.async_load()

        assert registry._intents == {}

    async def test_load_registers_state_listener(self):
        """async_load() registers a bus event listener and sets _state_unsub to the unsubscribe callable."""
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
    """Verify async_unload() cancels background/fallback tasks, unsubscribes listeners, and persists state."""

    async def test_unload_cancels_background_tasks(self):
        """async_unload() cancels all running tasks in _background_tasks."""
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_save = AsyncMock()
        registry._store.async_delay_save = MagicMock()

        async def _long_task():
            """Block indefinitely to simulate a long-running background task."""
            await asyncio.sleep(999)

        task = asyncio.create_task(_long_task())
        registry._background_tasks.add(task)
        registry._cleanup_unsub = MagicMock()
        registry._state_unsub = MagicMock()

        await registry.async_unload()

        assert task.cancelled()

    async def test_unload_cancels_fallback_tasks(self):
        """async_unload() cancels all running tasks in _fallback_tasks and clears the dict."""
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_save = AsyncMock()
        registry._store.async_delay_save = MagicMock()

        async def _fallback():
            """Block indefinitely to simulate a long-running fallback restore task."""
            await asyncio.sleep(999)

        task = asyncio.create_task(_fallback())
        registry._fallback_tasks["media_player.test"] = task
        registry._cleanup_unsub = MagicMock()
        registry._state_unsub = MagicMock()

        await registry.async_unload()

        assert task.cancelled()
        assert registry._fallback_tasks == {}

    async def test_unload_unsubscribes_listeners(self):
        """async_unload() calls both _cleanup_unsub() and _state_unsub() exactly once."""
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
        """async_unload() awaits async_save() to persist the final intent state."""
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
    """Verify capture_volume_intent(): stores intent, validates inputs, carries forward original volume, and cancels pending restore tasks."""

    async def test_captures_current_volume(self):
        """capture_volume_intent() stores an intent with the entity's current volume_level as original_volume."""
        registry = _make_registry(entity_volume=0.5)
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        await registry.capture_volume_intent("media_player.test")

        assert "media_player.test" in registry._intents
        assert registry._intents["media_player.test"].original_volume == 0.5

    async def test_raises_if_entity_not_found(self):
        """capture_volume_intent() raises HomeAssistantError matching 'not found' when the entity is absent."""
        registry = _make_registry()
        registry._hass.states.get = MagicMock(return_value=None)

        with pytest.raises(HomeAssistantError, match="not found"):
            await registry.capture_volume_intent("media_player.missing")

    async def test_raises_if_volume_level_not_reported(self):
        """capture_volume_intent() raises HomeAssistantError matching 'volume_level' when the attribute is missing."""
        hass = _make_hass(entity_volume=None)
        registry = VolumeRestorationRegistry(hass)
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        with pytest.raises(HomeAssistantError, match="volume_level"):
            await registry.capture_volume_intent("media_player.test")

    async def test_raises_for_non_positive_timeout(self):
        """capture_volume_intent() raises ValueError('timeout_seconds must be positive') for zero or negative values."""
        registry = _make_registry()

        with pytest.raises(ValueError, match="timeout_seconds must be positive"):
            await registry.capture_volume_intent("media_player.test", timeout_seconds=0)

        with pytest.raises(ValueError, match="timeout_seconds must be positive"):
            await registry.capture_volume_intent(
                "media_player.test", timeout_seconds=-10
            )

    async def test_raises_for_out_of_range_override_volume(self):
        """capture_volume_intent() raises ValueError('override_volume') for values outside [0.0, 1.0]."""
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
        """capture_volume_intent() cancels any existing _restore_tasks entry for the entity."""
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        async def _dummy():
            """Block indefinitely to simulate a pending restore task."""
            await asyncio.sleep(999)

        pending = asyncio.create_task(_dummy())
        registry._restore_tasks["media_player.test"] = pending

        await registry.capture_volume_intent("media_player.test")

        # Yield to let the cancellation take effect
        await asyncio.sleep(0)
        assert pending.cancelled()
        assert "media_player.test" not in registry._restore_tasks

    async def test_sets_override_volume_when_provided(self):
        """capture_volume_intent() stores override_volume=0.7 when the argument is explicitly provided."""
        registry = _make_registry(entity_volume=0.4)
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        await registry.capture_volume_intent("media_player.test", override_volume=0.7)

        assert registry._intents["media_player.test"].override_volume == 0.7


# ===========================================================================
# restore_volume / _do_restore
# ===========================================================================


class TestRestoreVolume:
    """Verify restore_volume(): no-op with no intent, calls _set_volume with original volume, and clears intent on failure."""

    async def test_restore_no_intent_is_noop(self):
        """restore_volume() returns without calling _set_volume when no intent is stored."""
        registry = _make_registry()

        # Must not raise; _set_volume should never be called
        with patch.object(registry, "_set_volume", AsyncMock()) as mock_set:
            await registry.restore_volume("media_player.test")
        mock_set.assert_not_awaited()

    async def test_restore_calls_set_volume_with_original(self):
        """restore_volume() calls _set_volume(entity_id, original_volume)."""
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        intent = _make_intent(original_volume=0.3, override_volume=0.7)
        registry._intents["media_player.test"] = intent

        with patch.object(registry, "_set_volume", AsyncMock()) as mock_set:
            await registry.restore_volume("media_player.test")

        mock_set.assert_awaited_once_with("media_player.test", 0.3)

    async def test_restore_clears_intent_even_on_failure(self):
        """restore_volume() removes the intent from _intents even when _set_volume raises TTSVolumeControlError."""
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
    """Verify _set_volume(): calls the media_player service, clamps values, and raises TTSVolumeControlError on failure."""

    async def test_set_volume_calls_service(self):
        """_set_volume() calls the media_player service with the correct entity_id and volume_level."""
        registry = _make_registry()

        await registry._set_volume("media_player.test", 0.5)

        registry._hass.services.async_call.assert_awaited_once()
        call_kwargs = registry._hass.services.async_call.call_args
        assert call_kwargs[0][0] == "media_player"
        assert call_kwargs[0][2]["volume_level"] == 0.5

    async def test_set_volume_clamps_above_one(self):
        """_set_volume() clamps the volume_level to 1.0 when the argument exceeds 1."""
        registry = _make_registry()

        await registry._set_volume("media_player.test", 1.5)

        call_kwargs = registry._hass.services.async_call.call_args
        assert call_kwargs[0][2]["volume_level"] == 1.0

    async def test_set_volume_clamps_below_zero(self):
        """_set_volume() clamps the volume_level to 0.0 when the argument is negative."""
        registry = _make_registry()

        await registry._set_volume("media_player.test", -0.1)

        call_kwargs = registry._hass.services.async_call.call_args
        assert call_kwargs[0][2]["volume_level"] == 0.0

    async def test_set_volume_raises_on_timeout(self):
        """_set_volume() raises TTSVolumeControlError matching 'timed out' on TimeoutError."""
        registry = _make_registry()
        registry._hass.services.async_call = AsyncMock(side_effect=TimeoutError)

        with pytest.raises(TTSVolumeControlError, match="timed out"):
            await registry._set_volume("media_player.test", 0.5)

    async def test_set_volume_raises_on_ha_error(self):
        """_set_volume() raises TTSVolumeControlError matching 'Failed to set volume' on HomeAssistantError."""
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
    """Verify complete_intent(): removes intent, is a no-op when absent, and cancels any pending fallback task."""

    async def test_complete_intent_removes_entry(self):
        """complete_intent() removes the entity's intent from _intents."""
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        registry._intents["media_player.test"] = _make_intent()
        await registry.complete_intent("media_player.test")

        assert "media_player.test" not in registry._intents

    async def test_complete_intent_noop_if_no_intent(self):
        """complete_intent() does not raise when no intent is stored for the entity."""
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        # Must not raise
        await registry.complete_intent("media_player.does_not_exist")

    async def test_complete_intent_cancels_fallback_task(self):
        """complete_intent() cancels the entity's pending fallback task and removes it from _fallback_tasks."""
        registry = _make_registry()
        registry._store = MagicMock()
        registry._store.async_delay_save = MagicMock()

        async def _fallback():
            """Block indefinitely to simulate a long-running fallback restore task."""
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
    """Verify _handle_state_change(): user-change detection, echo guard, and idle-to-restore scheduling."""

    def _make_event(
        self,
        entity_id: str,
        new_state_val: str,
        old_state_val: str | None,
        volume: float = 0.5,
    ) -> MagicMock:
        """Build a mock HA state-change event with the given entity_id, states, and volume attribute."""
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
        """_handle_state_change() ignores events for entities without an active intent."""
        registry = _make_registry()
        event = self._make_event("media_player.other", "idle", "playing")

        # No exception, no side effect
        registry._handle_state_change(event)

    def test_ignores_missing_entity_id(self):
        """_handle_state_change() ignores events with entity_id=None without raising."""
        registry = _make_registry()
        event = MagicMock()
        event.data = {"entity_id": None, "new_state": MagicMock(), "old_state": None}
        registry._handle_state_change(event)  # must not raise

    def test_ignores_missing_new_state(self):
        """_handle_state_change() ignores events with new_state=None without raising."""
        registry = _make_registry()
        registry._intents["media_player.test"] = _make_intent()
        event = MagicMock()
        event.data = {"entity_id": "media_player.test", "new_state": None}
        registry._handle_state_change(event)  # must not raise

    async def test_user_volume_change_starts_complete_intent_task(self):
        """A volume delta above VOLUME_CHANGE_THRESHOLD from the override level spawns a complete_intent background task."""
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
        """A state event within the echo guard window does not spawn a complete_intent task even if the volume delta looks large."""
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
        """A playing→idle state transition with unchanged volume schedules a delayed-restore task."""
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
        """An idle→idle transition does not schedule a restore task."""
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
    """Verify _delayed_restore() calls _do_restore under normal conditions and skips when intent is absent, expired, or delivery is active."""

    async def test_delayed_restore_calls_do_restore(self):
        """_delayed_restore() awaits _do_restore() when a valid, unexpired intent is present."""
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
        """_delayed_restore() does nothing when the intent has been removed before the delay elapsed."""
        registry = _make_registry()

        with (
            patch("asyncio.sleep", AsyncMock()),
            patch.object(registry, "_do_restore", AsyncMock()) as mock_restore,
        ):
            await registry._delayed_restore("media_player.test")

        mock_restore.assert_not_awaited()

    async def test_delayed_restore_skips_if_active_delivery(self):
        """_delayed_restore() skips restore when a delivery is still active for the entity."""
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
        """_delayed_restore() removes an expired intent from _intents and skips _do_restore."""
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
    """Verify has_active_intent(), mark_delivery_active(), and mark_delivery_inactive() behaviour."""

    def test_has_active_intent_false_when_no_intent(self):
        """has_active_intent() returns False when no intent is stored for the entity."""
        registry = _make_registry()
        assert registry.has_active_intent("media_player.test") is False

    def test_has_active_intent_true_when_intent_exists(self):
        """has_active_intent() returns True when an intent is stored for the entity."""
        registry = _make_registry()
        registry._intents["media_player.test"] = _make_intent()
        assert registry.has_active_intent("media_player.test") is True

    def test_mark_delivery_active_and_inactive(self):
        """mark_delivery_active() adds the entity to _active_delivery; mark_delivery_inactive() removes it."""
        registry = _make_registry()
        registry.mark_delivery_active("media_player.test")
        assert "media_player.test" in registry._active_delivery

        registry.mark_delivery_inactive("media_player.test")
        assert "media_player.test" not in registry._active_delivery

    def test_mark_delivery_inactive_noop_if_not_active(self):
        """mark_delivery_inactive() does not raise when the entity is not in _active_delivery."""
        registry = _make_registry()
        # Must not raise
        registry.mark_delivery_inactive("media_player.missing")


# ===========================================================================
# record_volume_set_time
# ===========================================================================


class TestRecordVolumeSetTime:
    """Verify record_volume_set_time() records a timestamp in _last_volume_set_time."""

    def test_records_current_time(self):
        """record_volume_set_time() stores a UTC timestamp between the before and after datetimes of the call."""
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
    """Verify set_fallback_task() replaces and cancels existing tasks, and cancel_fallback_task() is a safe no-op."""

    async def test_set_fallback_task_replaces_existing(self):
        """set_fallback_task() cancels the existing task and stores the new task in _fallback_tasks."""
        registry = _make_registry()

        async def _dummy():
            """Block indefinitely to simulate a fallback task awaiting cancellation."""
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
        """cancel_fallback_task() does not raise when no fallback task is registered for the entity."""
        registry = _make_registry()
        registry.cancel_fallback_task("media_player.missing")  # must not raise

    async def test_cancel_fallback_task_cancels_running_task(self):
        """cancel_fallback_task() cancels the running task and removes it from _fallback_tasks."""
        registry = _make_registry()

        async def _dummy():
            """Block indefinitely to simulate a fallback task that should be cancelled."""
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
    """Verify schedule_idle_restore(): no-op without an intent or pending task, and creates a restore task when needed."""

    def test_schedule_idle_restore_noop_if_no_intent(self):
        """schedule_idle_restore() does nothing when no intent is stored for the entity."""
        registry = _make_registry()
        registry.schedule_idle_restore("media_player.test")
        assert "media_player.test" not in registry._restore_tasks

    async def test_schedule_idle_restore_noop_if_task_already_pending(self):
        """schedule_idle_restore() does nothing when a restore task is already scheduled for the entity."""
        registry = _make_registry()
        registry._intents["media_player.test"] = _make_intent()

        async def _pending():
            """Block indefinitely to simulate an already-scheduled restore task."""
            await asyncio.sleep(999)

        existing = asyncio.create_task(_pending())
        registry._restore_tasks["media_player.test"] = existing

        registry.schedule_idle_restore("media_player.test")

        # Should keep the existing task, not create a new one
        assert registry._restore_tasks["media_player.test"] is existing
        existing.cancel()  # clean up

    async def test_schedule_idle_restore_creates_task_when_none_pending(self):
        """schedule_idle_restore() creates a _delayed_restore task in _restore_tasks when none is pending."""
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
    """Verify apply_volume() calls capture, set, and record; clears intent on failure."""

    async def test_apply_volume_captures_then_sets(self):
        """apply_volume() calls capture_volume_intent() with override_volume before calling _set_volume."""
        registry = _make_registry(entity_volume=0.4)

        with (
            patch.object(registry, "capture_volume_intent", AsyncMock()) as mock_cap,
            patch.object(registry, "_set_volume", AsyncMock()),
            patch.object(registry, "record_volume_set_time", MagicMock()),
        ):
            await registry.apply_volume("media_player.test", 0.7)

        mock_cap.assert_awaited_once_with("media_player.test", override_volume=0.7)

    async def test_apply_volume_clears_intent_when_set_fails(self):
        """apply_volume() awaits complete_intent() when _set_volume raises TTSVolumeControlError."""
        registry = _make_registry(entity_volume=0.4)

        with (
            patch.object(registry, "capture_volume_intent", AsyncMock()),
            patch.object(
                registry,
                "_set_volume",
                AsyncMock(side_effect=TTSVolumeControlError("failure")),
            ),
            patch.object(registry, "complete_intent", AsyncMock()) as mock_complete,
            pytest.raises(TTSVolumeControlError),
        ):
            await registry.apply_volume("media_player.test", 0.7)

        mock_complete.assert_awaited_once_with("media_player.test")

    async def test_apply_volume_records_set_time_on_success(self):
        """apply_volume() calls record_volume_set_time() after a successful _set_volume."""
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
    """Verify _on_background_task_done() logs an ERROR when the background task raised an exception."""

    async def test_logs_exception_from_failed_background_task(self, caplog):
        """_on_background_task_done() logs an ERROR containing the exception message when the task raised."""

        registry = _make_registry()

        async def _fail():
            """Raise RuntimeError to simulate a failed background task."""
            raise RuntimeError("explosion")

        task = asyncio.create_task(_fail())
        registry._background_tasks.add(task)

        with contextlib.suppress(RuntimeError):
            await task

        with caplog.at_level(
            logging.ERROR,
            logger="custom_components.ans.persistence.volume_restoration",
        ):
            registry._on_background_task_done(task)

        assert any("explosion" in r.message for r in caplog.records)
