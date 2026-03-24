"""Persistent registry for volume restoration tracking."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.const import (
    STATE_IDLE,
    STATE_OFF,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from typing import Any

    from homeassistant.core import Event

_LOGGER = logging.getLogger(__name__)

# Constants for volume restoration
STORAGE_VERSION = 1
STORAGE_KEY = "ans_volume_restoration"
DEFAULT_TIMEOUT = 3600  # 1 hour
VOLUME_CHANGE_THRESHOLD = 0.02  # 2% difference to detect user changes
RESTORATION_DELAY = 2.0  # seconds after idle before restoration
DEBOUNCE_DELAY = 5.0  # seconds to debounce persistence
# Maximum seconds to wait for a media_player.volume_set service call to complete.
# Guards against hung media player integrations stalling the event loop.
VOLUME_SET_TIMEOUT = 10


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

    def to_dict(self) -> dict:
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
        # Delivery locks — one per entity, shared across all adapter instances.
        # Used by TTSMediaPlayerAdapter to serialise concurrent deliveries to the
        # same media player even when the adapter is recreated by resync().
        self._delivery_locks: dict[str, asyncio.Lock] = {}
        self._entity_listeners: dict[str, Callable[[], None]] = {}
        self._cleanup_task = None
        self._persist_task = None
        self._persist_pending = False
        self._background_tasks: set[asyncio.Task] = set()  # Track background tasks
        # Per-entity _delayed_restore tasks — tracked separately so they can be
        # cancelled when a new delivery starts for the same entity.
        self._restore_tasks: dict[str, asyncio.Task] = {}
        # Per-entity fallback restore tasks — registered by the TTS adapter after
        # each successful delivery and shared across adapter instances via the registry.
        self._fallback_tasks: dict[str, asyncio.Task] = {}

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

        # Register state change listeners BEFORE restoring volumes so that no
        # PLAYING→IDLE transitions are missed during _restore_pending_volumes() awaits.
        for entity_id in self._intents:
            self._entity_listeners[entity_id] = async_track_state_change_event(
                self._hass, [entity_id], self._handle_state_change
            )

        # Attempt to restore any pending volumes from previous session.
        # Listeners are already registered above, so state transitions during
        # these service calls will be captured.
        if self._intents:
            await self._restore_pending_volumes()

        # Start periodic cleanup task
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

    async def async_unload(self) -> None:
        """Stop state tracking and persist final state.

        Should be called during integration unload.
        """
        # Cancel periodic tasks
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task

        if self._persist_task:
            self._persist_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._persist_task

        # Cancel all background tasks (e.g. _delayed_restore from state changes)
        for task in list(self._background_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._background_tasks.clear()

        # Cancel per-entity delayed restore tasks
        for task in list(self._restore_tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._restore_tasks.clear()

        # Cancel per-entity fallback tasks (registered by the TTS adapter)
        for task in list(self._fallback_tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._fallback_tasks.clear()

        # Stop state change listeners
        for unsub in self._entity_listeners.values():
            unsub()
        self._entity_listeners.clear()

        # Persist final state
        await self._persist()

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
            override_volume: Volume that will be set for TTS. Initialising the
                intent with the correct override avoids a stale-value window
                between capture and the subsequent volume_set call.

        Raises:
            HomeAssistantError: If media player state cannot be retrieved.

        """
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
            if existing is not None and datetime.fromisoformat(existing.timeout) > now:
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
            await self._schedule_persist()

            # Start listening for state changes on this entity if not already tracking
            if entity_id not in self._entity_listeners:
                self._entity_listeners[entity_id] = async_track_state_change_event(
                    self._hass, [entity_id], self._handle_state_change
                )

            _LOGGER.debug(
                "Captured volume intent for %s: original=%.2f",
                entity_id,
                original_volume,
            )

    async def restore_volume(self, entity_id: str) -> None:
        """Immediately restore volume to the original level and clear the intent.

        Call this when TTS delivery fails before playback starts, so the
        event-driven restoration (PLAYING → IDLE transition) will never trigger.
        Always clears the intent regardless of whether the service call succeeds,
        to prevent stale intents accumulating.

        Args:
            entity_id: Media player entity ID.

        """
        async with self._get_lock(entity_id):
            if entity_id not in self._intents:
                _LOGGER.debug("No active intent for %s, restoration skipped", entity_id)
                return

            intent = self._intents[entity_id]
            try:
                async with asyncio.timeout(VOLUME_SET_TIMEOUT):
                    await self._hass.services.async_call(
                        "media_player",
                        "volume_set",
                        {
                            "entity_id": entity_id,
                            "volume_level": intent.original_volume,
                        },
                        blocking=True,
                    )
                _LOGGER.debug(
                    "Immediately restored volume for %s: %.2f",
                    entity_id,
                    intent.original_volume,
                )
            except (TimeoutError, HomeAssistantError, ValueError) as e:
                _LOGGER.warning(
                    "Failed to immediately restore volume for %s: %s", entity_id, e
                )
            finally:
                # Always remove the intent so the state-change listener does not
                # fire a second restoration attempt later.
                del self._intents[entity_id]
                unsub = self._entity_listeners.pop(entity_id, None)
                if unsub:
                    unsub()
                await self._schedule_persist()

    async def _complete_intent_unlocked(self, entity_id: str) -> None:
        """Remove intent and unsubscribe listener. Caller MUST hold the entity lock."""
        if entity_id in self._intents:
            del self._intents[entity_id]
            self.cancel_fallback_task(entity_id)
            unsub = self._entity_listeners.pop(entity_id, None)
            if unsub:
                unsub()
            await self._schedule_persist()
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

        # Detect user volume change (significant difference from override volume)
        volume_diff = abs(current_volume - intent.override_volume)
        if volume_diff > VOLUME_CHANGE_THRESHOLD:
            # Guard against device echo: HA state feedback can arrive milliseconds
            # after ANS sets the volume. Changes within 500ms of intent creation
            # are treated as device acknowledgement, not a user adjustment.
            elapsed_since_set = (
                dt_util.utcnow() - datetime.fromisoformat(intent.timestamp)
            ).total_seconds()
            if elapsed_since_set < 0.5:
                return  # device echo — ignore
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
            # Not added to _background_tasks — _restore_tasks is the canonical
            # owner for per-entity cancellation (and async_unload cancels it).
            # _on_background_task_done is attached directly for exception surfacing.
            task.add_done_callback(self._on_background_task_done)
            task.add_done_callback(lambda _: self._restore_tasks.pop(entity_id, None))

    async def _delayed_restore(self, entity_id: str) -> None:
        """Restore volume after delay.

        Args:
            entity_id: Media player entity ID.

        """
        await asyncio.sleep(RESTORATION_DELAY)

        async with self._get_lock(entity_id):
            if entity_id not in self._intents:
                return  # Intent already completed/removed

            intent = self._intents[entity_id]

            # Check if intent expired
            timeout = datetime.fromisoformat(intent.timeout)
            if dt_util.utcnow() > timeout:
                _LOGGER.warning(
                    "Volume restoration intent for %s expired, removing", entity_id
                )
                await self._complete_intent_unlocked(entity_id)
                return

            # Restore original volume
            try:
                async with asyncio.timeout(VOLUME_SET_TIMEOUT):
                    await self._hass.services.async_call(
                        "media_player",
                        "volume_set",
                        {
                            "entity_id": entity_id,
                            "volume_level": intent.original_volume,
                        },
                        blocking=True,
                    )
                _LOGGER.info(
                    "Restored volume for %s: %.2f",
                    entity_id,
                    intent.original_volume,
                )
                await self._complete_intent_unlocked(entity_id)

            except (TimeoutError, HomeAssistantError, ValueError, KeyError) as e:
                _LOGGER.error(
                    "Failed to restore volume for %s: %s (will retry)", entity_id, e
                )
                # Keep intent for retry

    async def _restore_pending_volumes(self) -> None:
        """Attempt to restore any pending volumes from previous session.

        Called during startup to recover from HA restart.
        """
        now = dt_util.utcnow()
        expired = []
        restored = []

        for entity_id, intent in list(self._intents.items()):
            # Check expiration
            timeout = datetime.fromisoformat(intent.timeout)
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

            # Attempt restoration
            try:
                async with asyncio.timeout(VOLUME_SET_TIMEOUT):
                    await self._hass.services.async_call(
                        "media_player",
                        "volume_set",
                        {
                            "entity_id": entity_id,
                            "volume_level": intent.original_volume,
                        },
                        blocking=True,
                    )
                _LOGGER.info(
                    "Restored volume for %s after restart: %.2f",
                    entity_id,
                    intent.original_volume,
                )
                restored.append(entity_id)

            except (TimeoutError, HomeAssistantError, ValueError, KeyError) as e:
                _LOGGER.error(
                    "Failed to restore volume for %s after restart: %s (will retry)",
                    entity_id,
                    e,
                )

        # Clean up completed/expired intents
        for entity_id in expired + restored:
            await self.complete_intent(entity_id)

    async def _periodic_cleanup(self) -> None:
        """Periodically clean up expired intents.

        Runs every 5 minutes to remove expired intents.
        """
        while True:
            try:
                await asyncio.sleep(300)  # 5 minutes
                now = dt_util.utcnow()
                expired = []

                # Snapshot to a list: the loop below awaits complete_intent(),
                # which yields and allows other coroutines to modify _intents.
                # Without the snapshot this would raise RuntimeError if a new
                # intent is captured while iterating.
                for entity_id, intent in list(self._intents.items()):
                    timeout = datetime.fromisoformat(intent.timeout)
                    if now > timeout:
                        expired.append(entity_id)

                for entity_id in expired:
                    _LOGGER.warning(
                        "Volume restoration intent for %s expired without restoration "
                        "— volume may be stuck at TTS level. "
                        "Notifying user.",
                        entity_id,
                    )
                    from homeassistant.components.persistent_notification import (  # noqa: PLC0415
                        async_create as pn_async_create,
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

            except asyncio.CancelledError:
                break
            except (OSError, ValueError, KeyError) as e:
                _LOGGER.error(
                    "Error in periodic cleanup (intent_count=%d expired_count=%d): %s",
                    len(self._intents),
                    len(expired),
                    e,
                )

    async def _schedule_persist(self) -> None:
        """Schedule persistence with debouncing to reduce disk writes."""
        self._persist_pending = True

        # Cancel existing persist task if any
        if self._persist_task and not self._persist_task.done():
            return  # Already scheduled

        # Schedule new persist task
        self._persist_task = asyncio.create_task(self._debounced_persist())

    async def _debounced_persist(self) -> None:
        """Debounced persistence (waits for quiet period)."""
        await asyncio.sleep(DEBOUNCE_DELAY)
        if self._persist_pending:
            await self._persist()
            self._persist_pending = False

    async def _persist(self) -> None:
        """Persist current state to storage."""
        try:
            data = {
                "intents": [intent.to_dict() for intent in self._intents.values()],
                "version": STORAGE_VERSION,
                "timestamp": dt_util.utcnow().isoformat(),
            }
            await self._store.async_save(data)
            _LOGGER.debug("Persisted %d volume restoration intents", len(self._intents))

        except (OSError, ValueError) as e:
            _LOGGER.error(
                "Failed to persist volume restoration registry (intent_count=%d): %s",
                len(self._intents),
                e,
            )

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

    def get_delivery_lock(self, entity_id: str) -> asyncio.Lock:
        """Get or create a delivery lock for a media player entity.

        Separate from the volume-intent lock (_get_lock) and used solely to
        serialise concurrent TTS deliveries to the same entity. Stored here
        so the lock survives adapter recreation during ChannelManager.resync()
        — all adapter generations for the same entity share a single lock.

        Args:
            entity_id: Media player entity ID.

        Returns:
            Asyncio lock for this entity's TTS delivery serialisation.

        """
        if entity_id not in self._delivery_locks:
            self._delivery_locks[entity_id] = asyncio.Lock()
        return self._delivery_locks[entity_id]

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
