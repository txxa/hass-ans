"""Persistent registry for volume restoration tracking."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.const import (
    STATE_IDLE,
    STATE_OFF,
    STATE_PAUSED,
    STATE_PLAYING,
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

    # Step 3: Update tracking with new volume (for user-change detection)
    await registry.update_override_volume(entity_id, 0.5)

    # Step 4: Play TTS (registry automatically restores volume when media player becomes idle)
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
        self._state_unsubscribe = None
        self._cleanup_task = None
        self._persist_task = None
        self._persist_pending = False
        self._background_tasks: set[asyncio.Task] = set()  # Track background tasks

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

                # Attempt to restore any pending volumes immediately
                await self._restore_pending_volumes()

        except (OSError, ValueError, KeyError) as e:
            _LOGGER.error("Failed to load volume restoration registry: %s", e)
            self._intents = {}

        # Start state change listener
        self._state_unsubscribe = async_track_state_change_event(
            self._hass, list(self._intents.keys()), self._handle_state_change
        )

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

        # Stop state change listener
        if self._state_unsubscribe:
            self._state_unsubscribe()

        # Persist final state
        await self._persist()

    async def capture_volume_intent(
        self, entity_id: str, timeout_seconds: int = DEFAULT_TIMEOUT
    ) -> None:
        """Capture current volume for future restoration.

        This should be called BEFORE changing the volume. The registry will
        automatically restore the volume when the media player becomes idle.

        Args:
            entity_id: Media player entity ID.
            timeout_seconds: Intent expires after this many seconds (default: 1 hour).

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

            intent = VolumeIntent(
                entity_id=entity_id,
                original_volume=float(current_volume),
                override_volume=float(current_volume),  # Will be updated later
                timestamp=now.isoformat(),
                timeout=timeout_time.isoformat(),
            )

            self._intents[entity_id] = intent
            await self._schedule_persist()

            # Start listening for state changes on this entity if not already
            if self._state_unsubscribe is None:
                self._state_unsubscribe = async_track_state_change_event(
                    self._hass, [entity_id], self._handle_state_change
                )

            _LOGGER.debug(
                "Captured volume intent for %s: original=%.2f",
                entity_id,
                current_volume,
            )

    async def update_override_volume(self, entity_id: str, volume: float) -> None:
        """Update the override volume for user-change detection.

        Should be called AFTER setting the volume for TTS.

        Args:
            entity_id: Media player entity ID.
            volume: Volume level that was set (0.0-1.0).

        """
        async with self._get_lock(entity_id):
            if entity_id not in self._intents:
                _LOGGER.warning(
                    "Cannot update override volume for %s: no intent found", entity_id
                )
                return

            self._intents[entity_id].override_volume = float(volume)
            await self._schedule_persist()

            _LOGGER.debug(
                "Updated override volume for %s: override=%.2f", entity_id, volume
            )

    async def complete_intent(self, entity_id: str) -> None:
        """Mark volume restoration as complete and remove intent.

        Args:
            entity_id: Media player entity ID.

        """
        async with self._get_lock(entity_id):
            if entity_id in self._intents:
                del self._intents[entity_id]
                await self._schedule_persist()
                _LOGGER.debug("Completed volume restoration intent for %s", entity_id)

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
            _LOGGER.info(
                "User changed volume on %s (%.2f -> %.2f), aborting restoration",
                entity_id,
                intent.override_volume,
                current_volume,
            )
            # Remove intent without restoration
            task = asyncio.create_task(self.complete_intent(entity_id))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            return

        # Check if media player transitioned to idle/paused (restoration trigger)
        new_state_val = new_state.state
        old_state_val = old_state.state if old_state else None

        if old_state_val == STATE_PLAYING and new_state_val in (
            STATE_IDLE,
            STATE_PAUSED,
        ):
            _LOGGER.debug(
                "Media player %s transitioned to %s, scheduling volume restoration",
                entity_id,
                new_state_val,
            )
            # Schedule restoration with delay to ensure TTS fully completed
            task = asyncio.create_task(self._delayed_restore(entity_id))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

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
                await self.complete_intent(entity_id)
                return

            # Restore original volume
            try:
                await self._hass.services.async_call(
                    "media_player",
                    "volume_set",
                    {"entity_id": entity_id, "volume_level": intent.original_volume},
                    blocking=True,
                )
                _LOGGER.info(
                    "Restored volume for %s: %.2f",
                    entity_id,
                    intent.original_volume,
                )
                await self.complete_intent(entity_id)

            except (HomeAssistantError, ValueError, KeyError) as e:
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
                await self._hass.services.async_call(
                    "media_player",
                    "volume_set",
                    {"entity_id": entity_id, "volume_level": intent.original_volume},
                    blocking=True,
                )
                _LOGGER.info(
                    "Restored volume for %s after restart: %.2f",
                    entity_id,
                    intent.original_volume,
                )
                restored.append(entity_id)

            except (HomeAssistantError, ValueError, KeyError) as e:
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

                for entity_id, intent in self._intents.items():
                    timeout = datetime.fromisoformat(intent.timeout)
                    if now > timeout:
                        expired.append(entity_id)

                for entity_id in expired:
                    _LOGGER.info(
                        "Removing expired volume restoration intent: %s", entity_id
                    )
                    await self.complete_intent(entity_id)

            except asyncio.CancelledError:
                break
            except (OSError, ValueError, KeyError) as e:
                _LOGGER.error("Error in periodic cleanup: %s", e)

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
            _LOGGER.error("Failed to persist volume restoration registry: %s", e)

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
