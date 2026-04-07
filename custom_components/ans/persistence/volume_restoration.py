"""Persistent registry for volume restoration tracking."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_STATE_CHANGED,
    SERVICE_VOLUME_SET,
    STATE_IDLE,
    STATE_OFF,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from typing import Any

    from homeassistant.core import Event

from ..const import VOLUME_SET_TIMEOUT
from ..exceptions import TTSVolumeControlError

_LOGGER = logging.getLogger(__name__)


def _parse_dt(value: str) -> datetime:
    """Parse an ISO datetime string, always returning a UTC-aware datetime.

    Handles both timezone-aware timestamps (e.g. ``2024-01-01T12:00:00+00:00``)
    and naive timestamps that may appear in manually edited storage files.
    Naive timestamps are assumed to be UTC so that comparisons against
    ``dt_util.utcnow()`` (which is always aware) never raise ``TypeError``.
    """

    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# Constants for volume restoration
STORAGE_VERSION = 1
STORAGE_KEY = "ans_volume_restoration"
DEFAULT_TIMEOUT = 3600  # 1 hour
VOLUME_CHANGE_THRESHOLD = 0.05  # 5% difference to detect user changes
RESTORATION_DELAY = 2.0  # seconds after idle before restoration
DEBOUNCE_DELAY = 5.0  # seconds to debounce persistence
# Seconds after a successful volume_set during which an inbound state-change
# event is treated as device echo rather than a genuine user adjustment.
# Must be wide enough to cover Bluetooth, Chromecast and browser companion-app
# round-trip latencies (which can reach several seconds), while still being
# shorter than any plausible intentional volume tweak by a user.
# Derived from VOLUME_SET_TIMEOUT so the guard scales with the service budget.
ECHO_GUARD_WINDOW = VOLUME_SET_TIMEOUT * 0.5  # seconds (default: 3.0 s)


@dataclass
class VolumeIntent:
    """Volume restoration intent with user-change detection and retry support.

    Attributes
    ----------
    entity_id : str
        Media player entity ID.
    original_volume : float
        Original volume level (0.0-1.0) to restore.
    override_volume : float
        Volume level set by TTS (0.0-1.0).
    timestamp : str
        ISO timestamp when intent was created.
    timeout : str
        ISO timestamp when intent expires (no longer valid).

    """

    entity_id: str
    original_volume: float
    override_volume: float
    timestamp: str
    timeout: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for storage."""
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> VolumeIntent:
        """Create VolumeIntent from dictionary."""
        return VolumeIntent(
            entity_id=data["entity_id"],
            original_volume=data["original_volume"],
            override_volume=data["override_volume"],
            timestamp=data["timestamp"],
            timeout=data["timeout"],
        )


class VolumeRestorationRegistry:
    """Automatic volume restoration with event-driven timing.

    Features:
    - Persistent storage survives Home Assistant restarts
    - Event-driven restoration (triggers when media player becomes idle)
    - User-change detection (aborts restoration if user manually adjusts volume)
    - Automatic cleanup of expired/completed intents
    - Debounced persistence (reduces disk writes)

    Usage Pattern:
    ```python
    # Step 1: Capture original volume before changing it
    await registry.capture_volume_intent(entity_id)

    # Step 2: Set new volume for TTS
    await hass.services.async_call("media_player", "volume_set", {
        "entity_id": entity_id,
        "volume_level": 0.5
    })

    # Step 3: Play TTS (registry automatically restores volume when media player becomes idle)
    await hass.services.async_call("tts", "speak", {...})
    ```
    """

    def __init__(self, hass: HomeAssistant):
        """Initialize volume restoration registry.

        Args:
            hass: Home Assistant instance.

        """
        self._hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._intents: dict[str, VolumeIntent] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._state_unsub: Callable[[], None] | None = None
        self._cleanup_unsub: Callable[[], None] | None = None
        self._background_tasks: set[asyncio.Task] = set()  # Track background tasks
        # Per-entity _delayed_restore tasks — tracked separately so they can be
        # cancelled when a new delivery starts for the same entity.
        self._restore_tasks: dict[str, asyncio.Task] = {}
        # Per-entity fallback restore tasks — registered by the TTS adapter after
        # each successful delivery and shared across adapter instances via the registry.
        self._fallback_tasks: dict[str, asyncio.Task] = {}
        # Non-persisted: time of last successful volume_set per entity.
        # Used by the echo-guard in _handle_state_change to distinguish
        # integration-level state-feedback (device echo) from genuine user volume
        # changes.  Cleared when the intent is completed.
        self._last_volume_set_time: dict[str, datetime] = {}
        # Entities currently undergoing active TTS delivery (delivery lock held).
        # _delayed_restore skips restoration for active entities so it does not
        # interrupt mid-delivery playback; the fallback timer handles cleanup.
        self._active_delivery: set[str] = set()

    async def async_load(self) -> None:
        """Load restoration intents from storage and start state tracking.

        Should be called during integration setup.
        """
        try:
            data = await self._store.async_load()
            if data and "intents" in data:
                self._intents = {
                    intent_data["entity_id"]: VolumeIntent.from_dict(intent_data)
                    for intent_data in data["intents"]
                }
                _LOGGER.info(
                    "Loaded %d volume restoration intents from storage",
                    len(self._intents),
                )

        except (OSError, ValueError, KeyError) as e:
            _LOGGER.error("Failed to load volume restoration registry: %s", e)
            self._intents = {}

        # Register a single global state-change listener BEFORE restoring volumes
        # so that no PLAYING→IDLE transitions are missed during
        # _restore_pending_volumes() awaits.  The early-exit guard in
        # _handle_state_change (entity_id not in self._intents) provides the
        # per-entity filtering, so there is no need for individual subscriptions.
        self._state_unsub = self._hass.bus.async_listen(
            EVENT_STATE_CHANGED, self._handle_state_change
        )

        # Attempt to restore any pending volumes from previous session.
        # Listener is already registered above, so state transitions during
        # these service calls will be captured.
        if self._intents:
            await self._restore_pending_volumes()

        # Schedule periodic cleanup via HA's time-interval helper.
        self._cleanup_unsub = async_track_time_interval(
            self._hass, self._periodic_cleanup_callback, timedelta(minutes=5)
        )

    async def async_unload(self) -> None:
        """Stop state tracking and persist final state.

        Should be called during integration unload.
        """
        # Stop scheduling new cleanup callbacks.
        if self._cleanup_unsub:
            self._cleanup_unsub()

        # Cancel per-entity fallback tasks (registered by TTSMediaPlayerAdapter
        # after each delivery).  Fallback tasks are spawned externally and are
        # NOT in _background_tasks, so they need their own cancellation loop.
        for task in list(self._fallback_tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._fallback_tasks.clear()

        # Cancel all other background tasks: user-change complete_intent calls
        # and _delayed_restore tasks.  Both are tracked in _background_tasks.
        # _restore_tasks entries are a subset of _background_tasks; clearing
        # both here avoids any dangling references.
        for task in list(self._background_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._background_tasks.clear()
        self._restore_tasks.clear()

        # Stop the global state-change listener.
        if self._state_unsub:
            self._state_unsub()

        # Persist final state (bypass debounce for guaranteed flush on unload).
        await self._store.async_save(self._build_persist_data())

    async def capture_volume_intent(
        self,
        entity_id: str,
        timeout_seconds: int = DEFAULT_TIMEOUT,
        *,
        override_volume: float | None = None,
    ) -> None:
        """Capture current volume for future restoration.

        This should be called BEFORE changing the volume. The registry will
        automatically restore the volume when the media player becomes idle.

        Args:
            entity_id: Media player entity ID.
            timeout_seconds: Intent expires after this many seconds (default: 1 hour).
                Must be a positive integer.
            override_volume: Volume that will be set for TTS (0.0–1.0).
                Initialising the intent with the correct override avoids a
                stale-value window between capture and the subsequent volume_set
                call.  Must be in [0.0, 1.0] when provided.

        Raises:
            HomeAssistantError: If media player state cannot be retrieved.
            ValueError: If ``timeout_seconds`` is not positive or ``override_volume``
                is outside [0.0, 1.0].

        """
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be positive, got {timeout_seconds}")
        if override_volume is not None and not (0.0 <= override_volume <= 1.0):
            raise ValueError(
                f"override_volume must be in [0.0, 1.0], got {override_volume}"
            )

        async with self._get_lock(entity_id):
            state = self._hass.states.get(entity_id)
            if state is None:
                raise HomeAssistantError(f"Media player {entity_id} not found")

            current_volume = state.attributes.get("volume_level")
            if current_volume is None:
                raise HomeAssistantError(
                    f"Media player {entity_id} does not report volume_level"
                )

            now = dt_util.utcnow()
            timeout_time = now + timedelta(seconds=timeout_seconds)

            # If an unexpired intent already exists, carry forward its original_volume
            # rather than re-reading from entity state, which may already be at the
            # TTS-set level from a prior rapid delivery (back-to-back scenario).
            existing = self._intents.get(entity_id)
            if existing is not None and _parse_dt(existing.timeout) > now:
                original_volume = existing.original_volume
                _LOGGER.debug(
                    "Carrying forward original volume for %s: %.2f "
                    "(active intent exists; current level may be TTS-set)",
                    entity_id,
                    original_volume,
                )
            else:
                original_volume = float(current_volume)

            # Cancel any pending _delayed_restore for this entity: it belongs to
            # the prior delivery and would restore prematurely if left running.
            pending_restore = self._restore_tasks.pop(entity_id, None)
            if pending_restore and not pending_restore.done():
                pending_restore.cancel()

            intent = VolumeIntent(
                entity_id=entity_id,
                original_volume=original_volume,
                override_volume=float(override_volume)
                if override_volume is not None
                else original_volume,
                timestamp=now.isoformat(),
                timeout=timeout_time.isoformat(),
            )

            self._intents[entity_id] = intent
            self._store.async_delay_save(self._build_persist_data, DEBOUNCE_DELAY)

            _LOGGER.debug(
                "Captured volume intent for %s: original=%.2f",
                entity_id,
                original_volume,
            )

    async def _do_restore(self, entity_id: str, intent: VolumeIntent) -> None:
        """Restore volume to the original level and complete the intent.

        Single canonical implementation shared by :meth:`restore_volume`,
        :meth:`_delayed_restore`, and :meth:`_restore_pending_volumes`.
        Caller MUST hold ``_get_lock(entity_id)``.

        On failure the intent is still cleared so stale intents do not
        accumulate — the volume has already been changed by this point, and
        keeping the intent would risk a future spurious PLAYING→IDLE restore
        overwriting a user-adjusted volume.

        Args:
            entity_id: Media player entity ID.
            intent: Active volume intent for this entity.

        """
        try:
            # Record the restore time before issuing volume_set so that the
            # resulting state-change echo (which fires _handle_state_change with
            # the original volume) lands inside ECHO_GUARD_WINDOW and is not
            # mis-classified as a user volume adjustment.
            self.record_volume_set_time(entity_id)
            await self._set_volume(entity_id, intent.original_volume)
            _LOGGER.info(
                "Restored volume for %s: %.2f",
                entity_id,
                intent.original_volume,
            )
        except TTSVolumeControlError as e:
            _LOGGER.warning(
                "Failed to restore volume for %s: %s — volume may be stuck at TTS level",
                entity_id,
                e,
            )
        finally:
            # Always complete the intent so the state-change listener does not
            # trigger a second restoration attempt later.
            await self._complete_intent_unlocked(entity_id)

    async def restore_volume(self, entity_id: str) -> None:
        """Immediately restore volume to the original level and clear the intent.

        Call this when TTS delivery fails before playback starts, so the
        event-driven restoration (PLAYING → IDLE transition) will never trigger.
        Delegates to :meth:`_do_restore`, which always clears the intent
        regardless of whether the service call succeeds.

        Args:
            entity_id: Media player entity ID.

        """
        async with self._get_lock(entity_id):
            if entity_id not in self._intents:
                _LOGGER.debug("No active intent for %s, restoration skipped", entity_id)
                return
            await self._do_restore(entity_id, self._intents[entity_id])

    async def _set_volume(self, entity_id: str, volume_level: float) -> None:
        """Set the media player volume via the HA service API.

        ``volume_level`` is clamped to the valid [0.0, 1.0] range so callers
        do not need to sanitize their values beforehand.

        Args:
            entity_id: Media player entity ID.
            volume_level: Desired volume level (0.0–1.0). Values outside this
                range are silently clamped.

        Raises:
            TTSVolumeControlError: If the service call fails or times out.

        """
        # Clamp to valid media_player range before issuing the service call.
        volume_level = max(0.0, min(1.0, volume_level))
        try:
            async with asyncio.timeout(VOLUME_SET_TIMEOUT):
                await self._hass.services.async_call(
                    "media_player",
                    SERVICE_VOLUME_SET,
                    {
                        ATTR_ENTITY_ID: entity_id,
                        "volume_level": volume_level,
                    },
                    blocking=True,
                )
            _LOGGER.debug("Set volume for %s: %.0f%%", entity_id, volume_level * 100)
        except TimeoutError as e:
            raise TTSVolumeControlError(
                f"Volume set timed out for {entity_id} after {VOLUME_SET_TIMEOUT}s"
            ) from e
        except (HomeAssistantError, ServiceNotFound) as e:
            raise TTSVolumeControlError(
                f"Failed to set volume for {entity_id}: {e}"
            ) from e

    async def apply_volume(self, entity_id: str, target_volume: float) -> None:
        """Capture the original volume, set the target volume, and record the override.

        Must be called before TTS playback begins.  Raises on failure so the
        caller can decide whether to abort the delivery.

        Args:
            entity_id: Media player entity ID.
            target_volume: Desired volume level (0.0–1.0).

        Raises:
            TTSVolumeControlError: If capturing or setting the volume fails.

        """
        await self.capture_volume_intent(entity_id, override_volume=target_volume)
        try:
            await self._set_volume(entity_id, target_volume)
        except TTSVolumeControlError:
            # _set_volume failed before the volume changed; clear the stale
            # intent so the event-driven restore path doesn't fire later.
            await self.complete_intent(entity_id)
            raise
        # Record the time volume_set succeeded so the echo-guard in
        # _handle_state_change can distinguish device-echo state feedback from
        # genuine user volume adjustments.
        self.record_volume_set_time(entity_id)

    async def _complete_intent_unlocked(self, entity_id: str) -> None:
        """Remove intent and schedule persistence. Caller MUST hold the entity lock."""
        if entity_id in self._intents:
            del self._intents[entity_id]
            self.cancel_fallback_task(entity_id)
            self._last_volume_set_time.pop(entity_id, None)
            self._store.async_delay_save(self._build_persist_data, DEBOUNCE_DELAY)
            _LOGGER.debug("Completed volume restoration intent for %s", entity_id)

    async def complete_intent(self, entity_id: str) -> None:
        """Mark volume restoration as complete and remove intent.

        Args:
            entity_id: Media player entity ID.

        """
        async with self._get_lock(entity_id):
            await self._complete_intent_unlocked(entity_id)

    @callback
    def _handle_state_change(self, event: Event[Any]) -> None:
        """Handle media player state changes for automatic restoration.

        Triggers volume restoration when media player becomes idle/paused.
        Aborts restoration if user manually changed volume.

        Args:
            event: State change event.

        """
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        if not entity_id or not new_state:
            return

        # Check if we have a restoration intent for this entity
        if entity_id not in self._intents:
            return

        intent = self._intents[entity_id]

        # Get current volume
        current_volume = new_state.attributes.get("volume_level")
        if current_volume is None:
            return

        # User-change detection: only run when no delivery is active for this
        # entity.  While a delivery holds the lock, every volume state event
        # (device echo, OS ducking, companion-app feedback) is indistinguishable
        # from a genuine user adjustment.  Blocking user-change detection here
        # prevents premature intent cancellation; the IDLE detection below is
        # intentionally left outside this guard so that a PLAYING→IDLE event
        # that arrives during the tiny active-delivery window is still processed.
        if entity_id not in self._active_delivery:
            # Detect user volume change (significant difference from override volume)
            volume_diff = abs(current_volume - intent.override_volume)
            if volume_diff > VOLUME_CHANGE_THRESHOLD:
                # Guard against device echo: some integrations feed the volume back
                # as a state update milliseconds after ANS calls volume_set. Compare
                # against _last_volume_set_time (recorded immediately after volume_set
                # succeeds) rather than intent.timestamp (recorded at capture, which
                # may be several seconds earlier if the service call was slow).
                last_set = self._last_volume_set_time.get(entity_id)
                if last_set is not None:
                    elapsed_since_set = (dt_util.utcnow() - last_set).total_seconds()
                    if elapsed_since_set < ECHO_GUARD_WINDOW:
                        return  # device echo — ignore
                else:
                    # Fallback for intents loaded from storage on restart (no
                    # set-time recorded); use intent.timestamp for the log only.
                    elapsed_since_set = (
                        dt_util.utcnow() - _parse_dt(intent.timestamp)
                    ).total_seconds()
                _LOGGER.info(
                    "User changed volume on %s (%.2f -> %.2f), "
                    "aborting restoration (elapsed_ms=%d)",
                    entity_id,
                    intent.override_volume,
                    current_volume,
                    int(elapsed_since_set * 1000),
                )
                # Remove intent without restoration
                task = asyncio.create_task(self.complete_intent(entity_id))
                self._background_tasks.add(task)
                task.add_done_callback(self._on_background_task_done)
                return

        # Trigger restoration when the player reaches idle from any non-idle state.
        # This covers PLAYING→IDLE (normal), PAUSED→IDLE, BUFFERING→IDLE, and the
        # case where a fast player skips STATE_PLAYING entirely (direct IDLE→IDLE
        # is excluded by old_state_val != STATE_IDLE to avoid spurious triggers
        # on HA state-refresh events where both old and new state are IDLE).
        new_state_val = new_state.state
        old_state_val = old_state.state if old_state else None

        if new_state_val == STATE_IDLE and old_state_val != STATE_IDLE:
            _LOGGER.debug(
                "Media player %s transitioned from %s to idle, scheduling volume restoration",
                entity_id,
                old_state_val,
            )
            # Schedule restoration with delay to ensure TTS fully completed.
            # Track per-entity so it can be cancelled if a new delivery starts
            # before this task fires, preventing premature or wrong restoration.
            existing_restore = self._restore_tasks.pop(entity_id, None)
            if existing_restore and not existing_restore.done():
                existing_restore.cancel()
            task = asyncio.create_task(self._delayed_restore(entity_id))
            self._restore_tasks[entity_id] = task
            # Also add to _background_tasks so all background tasks share one
            # strong-reference set and async_unload can cancel them in a single
            # loop.  _restore_tasks keeps the per-entity reference for targeted
            # cancellation when a new delivery starts.
            self._background_tasks.add(task)
            task.add_done_callback(self._on_background_task_done)
            task.add_done_callback(lambda _: self._restore_tasks.pop(entity_id, None))

    async def _delayed_restore(self, entity_id: str) -> None:
        """Restore volume after a short delay following a PLAYING→IDLE transition.

        Args:
            entity_id: Media player entity ID.

        """
        await asyncio.sleep(RESTORATION_DELAY)

        async with self._get_lock(entity_id):
            if entity_id not in self._intents:
                return  # Intent already completed/removed

            # Skip if a delivery is actively holding the delivery lock for this
            # entity. Restoring volume mid-delivery would interrupt ongoing
            # playback. The fallback timer scheduled after delivery completes
            # will handle restoration.
            if entity_id in self._active_delivery:
                _LOGGER.debug(
                    "Skipping delayed restore for %s — delivery is active "
                    "(fallback timer will restore after delivery completes)",
                    entity_id,
                )
                return

            intent = self._intents[entity_id]

            # Check if intent expired
            timeout = _parse_dt(intent.timeout)
            if dt_util.utcnow() > timeout:
                _LOGGER.warning(
                    "Volume restoration intent for %s expired, removing", entity_id
                )
                await self._complete_intent_unlocked(entity_id)
                return

            await self._do_restore(entity_id, intent)

    async def _restore_pending_volumes(self) -> None:
        """Attempt to restore any pending volumes from previous session.

        Called during startup to recover from HA restart.
        """
        now = dt_util.utcnow()
        expired = []

        for entity_id, intent in list(self._intents.items()):
            # Check expiration
            timeout = _parse_dt(intent.timeout)
            if now > timeout:
                _LOGGER.info(
                    "Volume restoration intent for %s expired during restart", entity_id
                )
                expired.append(entity_id)
                continue

            # Check if media player is in restorable state
            state = self._hass.states.get(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_OFF):
                _LOGGER.debug(
                    "Media player %s not available for volume restoration, keeping intent",
                    entity_id,
                )
                continue

            # Attempt restoration under the entity lock. The state-change
            # listeners registered in async_load above may already be tracking
            # this entity; holding the lock prevents a concurrent _delayed_restore
            # from racing with this startup restore. Both paths call _do_restore,
            # so the first caller to acquire the lock wins; the second finds the
            # intent already gone and returns early.
            async with self._get_lock(entity_id):
                if entity_id not in self._intents:
                    # A concurrent _delayed_restore (triggered by a state-change
                    # event during a prior await in this loop) already handled it.
                    continue
                await self._do_restore(entity_id, intent)

        # Clean up expired intents (un-restorable players are kept for retry).
        for entity_id in expired:
            await self.complete_intent(entity_id)

    @callback
    def _periodic_cleanup_callback(self, _now: datetime) -> None:
        """Schedule one async cleanup pass (called by async_track_time_interval)."""
        task = asyncio.create_task(self._do_cleanup())
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)

    async def _do_cleanup(self) -> None:
        """Clean up expired intents (invoked every 5 minutes via the scheduler)."""
        # Import here to avoid loading the component at module import time.
        from homeassistant.components.persistent_notification import (  # noqa: PLC0415
            async_create as pn_async_create,
        )

        now = dt_util.utcnow()
        expired: list[str] = []
        try:
            # Snapshot to a list: the loop below awaits complete_intent(),
            # which yields and allows other coroutines to modify _intents.
            # Without the snapshot this would raise RuntimeError if a new
            # intent is captured while iterating.
            for entity_id, intent in list(self._intents.items()):
                timeout = _parse_dt(intent.timeout)
                if now > timeout:
                    expired.append(entity_id)

            for entity_id in expired:
                _LOGGER.warning(
                    "Volume restoration intent for %s expired without restoration "
                    "— volume may be stuck at TTS level. "
                    "Notifying user.",
                    entity_id,
                )
                pn_async_create(
                    self._hass,
                    f"The volume on **{entity_id}** was not automatically "
                    "restored after TTS playback. It may still be at the "
                    "TTS volume level. Please check and adjust manually.",
                    title="ANS: Volume Not Restored",
                    notification_id=f"ans_volume_expired_{entity_id.replace('.', '_')}",
                )
                await self.complete_intent(entity_id)

        except (OSError, ValueError, KeyError) as e:
            _LOGGER.error(
                "Error in periodic cleanup (intent_count=%d expired_count=%d): %s",
                len(self._intents),
                len(expired),
                e,
            )

    def _build_persist_data(self) -> dict:
        """Build the persistence payload from current intents."""
        return {
            "intents": [intent.to_dict() for intent in self._intents.values()],
            "version": STORAGE_VERSION,
            "timestamp": dt_util.utcnow().isoformat(),
        }

    def set_fallback_task(self, entity_id: str, task: asyncio.Task) -> None:
        """Register a fallback restore task for an entity in the shared registry.

        Called by the TTS adapter after a successful delivery to schedule a
        safety-net restore in case the PLAYING\u2192IDLE event is never received.
        Cancels any previously registered fallback for the same entity so that
        only one fallback task runs at a time regardless of adapter instance.

        Args:
            entity_id: Media player entity ID.
            task: Asyncio Task running the fallback restore coroutine.

        """
        self.cancel_fallback_task(entity_id)
        self._fallback_tasks[entity_id] = task
        task.add_done_callback(lambda _: self._fallback_tasks.pop(entity_id, None))

    def cancel_fallback_task(self, entity_id: str) -> None:
        """Cancel any pending fallback restore task for an entity.

        Called at the start of each delivery to prevent a stale fallback task
        (from a prior or concurrently replaced adapter) from restoring volume
        while the new delivery is in progress.

        Args:
            entity_id: Media player entity ID.

        """
        task = self._fallback_tasks.pop(entity_id, None)
        if task and not task.done():
            task.cancel()

    def _get_lock(self, entity_id: str) -> asyncio.Lock:
        """Get or create lock for entity.

        Args:
            entity_id: Entity ID.

        Returns:
            Asyncio lock for this entity.

        """
        if entity_id not in self._locks:
            self._locks[entity_id] = asyncio.Lock()
        return self._locks[entity_id]

    def schedule_idle_restore(self, entity_id: str) -> None:
        """Schedule a delayed volume restoration as if a PLAYING\u2192IDLE event just fired.

        Called by :class:`TTSMediaPlayerAdapter` immediately after
        ``tts.speak`` returns when the media player is already in STATE_IDLE.
        This handles companion-app players (browser, mobile) whose
        ``tts.speak blocking=True`` call waits for audio playback to finish,
        meaning the PLAYING\u2192IDLE transition occurred *during* the service
        call and was therefore silently dropped by the ``_active_delivery``
        guard in :meth:`_handle_state_change`.

        If :meth:`_handle_state_change` already scheduled a restore task when
        the PLAYING\u2192IDLE event fired (which happens when the event arrives
        *before* ``tts.speak`` returns), that task is preserved so its timer
        runs from the actual IDLE timestamp rather than being reset to now.
        A new task is only created when no pending task exists, i.e. the IDLE
        event was genuinely missed.

        Args:
            entity_id: Media player entity ID.

        """
        if entity_id not in self._intents:
            return
        existing = self._restore_tasks.get(entity_id)  # get, not pop
        if existing and not existing.done():
            # A restore task was already queued by _handle_state_change when the
            # PLAYING→IDLE event fired during the service call.  It will
            # complete normally; no need to reset its timer.
            _LOGGER.debug(
                "Idle restore already pending for %s — deferring to existing task",
                entity_id,
            )
            return
        task = asyncio.create_task(self._delayed_restore(entity_id))
        self._restore_tasks[entity_id] = task
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)
        task.add_done_callback(lambda _: self._restore_tasks.pop(entity_id, None))
        _LOGGER.debug(
            "Scheduled idle restore for %s (IDLE event was missed during active delivery)",
            entity_id,
        )

    def has_active_intent(self, entity_id: str) -> bool:
        """Return True if an unexpired restoration intent exists for *entity_id*.

        Used by TTSMediaPlayerAdapter to guard fallback-task registration:
        if the intent was already cleared (e.g. by a false user-change detection
        that slipped through before delivery completed), there is nothing to
        restore and the fallback task should not be spawned.

        Args:
            entity_id: Media player entity ID.

        Returns:
            True if an active intent is present, False otherwise.

        """
        return entity_id in self._intents

    def record_volume_set_time(self, entity_id: str) -> None:
        """Record the time of the most recent successful volume_set for *entity_id*.

        Called immediately after a ``media_player.volume_set`` service call
        succeeds. The timestamp is used by the echo-guard in
        :meth:`_handle_state_change` to distinguish integration-level state
        feedback from genuine user volume adjustments.

        Args:
            entity_id: Media player entity ID.

        """
        self._last_volume_set_time[entity_id] = dt_util.utcnow()

    def mark_delivery_active(self, entity_id: str) -> None:
        """Mark *entity_id* as currently undergoing active TTS delivery.

        Called by ``TTSMediaPlayerAdapter`` immediately after acquiring the
        delivery lock. :meth:`_delayed_restore` will skip restoration for any
        entity in this set, deferring it to the fallback timer scheduled after
        the delivery completes.

        Args:
            entity_id: Media player entity ID.

        """
        self._active_delivery.add(entity_id)

    def mark_delivery_inactive(self, entity_id: str) -> None:
        """Clear the active-delivery marker for *entity_id*.

        Called in the ``finally`` block of ``TTSMediaPlayerAdapter.deliver``,
        before releasing the delivery lock.

        Args:
            entity_id: Media player entity ID.

        """
        self._active_delivery.discard(entity_id)

    def _on_background_task_done(self, task: asyncio.Task) -> None:
        """Discard a completed background task and log any unexpected exceptions.

        Used as a done-callback on all background tasks spawned in
        _handle_state_change so that unhandled exceptions surface in the HA
        log rather than being silently swallowed by asyncio.

        Args:
            task: The completed asyncio Task.

        """
        self._background_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()):
            _LOGGER.error(
                "Background volume restoration task failed (active_intents=%d): %s",
                len(self._intents),
                exc,
                exc_info=exc,
            )
