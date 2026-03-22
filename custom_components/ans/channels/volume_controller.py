"""Volume management controller for TTS media player delivery."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.const import ATTR_ENTITY_ID, SERVICE_VOLUME_SET
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.util import dt as dt_util

from ..exceptions import TTSVolumeControlError
from ..models import NotificationCriticality
from ..models.recipient import TTSSettings

if TYPE_CHECKING:
    from ..persistence.volume_restoration import VolumeRestorationRegistry

_LOGGER = logging.getLogger(__name__)

# HA volume scale: API accepts 0.0–1.0, UI and config expose 0–100.
VOLUME_SCALE = 100


class VolumeController:
    """Manage volume capture, adjustment, and restoration for TTS delivery.

    Encapsulates all :class:`VolumeRestorationRegistry` interactions and the
    time-based volume calculation logic so that ``TTSMediaPlayerAdapter`` can
    delegate every volume concern to a single object.

    Attributes
    ----------
    _hass : HomeAssistant
        Home Assistant instance for service calls.
    _volume_registry : VolumeRestorationRegistry
        Registry tracking per-entity volume intents for restoration.

    """

    def __init__(
        self,
        hass: HomeAssistant,
        volume_registry: VolumeRestorationRegistry,
    ) -> None:
        """Initialise the volume controller.

        Parameters
        ----------
        hass : HomeAssistant
            Home Assistant instance for service calls.
        volume_registry : VolumeRestorationRegistry
            Registry tracking volume intents for restoration.

        """
        self._hass = hass
        self._volume_registry = volume_registry

    def calculate_target_volume(
        self,
        criticality: NotificationCriticality,
        tts_settings: TTSSettings | None,
    ) -> float:
        """Calculate the delivery volume based on time of day and criticality.

        Parameters
        ----------
        criticality : NotificationCriticality
            Notification criticality level.
        tts_settings : TTSSettings | None
            Per-recipient TTS settings; defaults are used when ``None``.

        Returns
        -------
        float
            Target volume level in the range 0.0–1.0.

        """
        if tts_settings is None:
            tts_settings = TTSSettings.default()

        # Criticality override takes priority over time-based selection.
        if criticality.value in tts_settings.volume_override_criticalities:
            volume_percent = tts_settings.volume_override_level
            _LOGGER.debug(
                "Using volume override for criticality %s: %d%%",
                criticality.value,
                volume_percent,
            )
            return volume_percent / VOLUME_SCALE

        # Time-based selection:
        # Morning: 06:00–09:00 | Daytime: 09:00–18:00
        # Evening: 18:00–22:00 | Night:   22:00–06:00
        hour = dt_util.now().hour
        if 6 <= hour < 9:
            volume_percent = tts_settings.volume_morning
            time_frame = "morning"
        elif 9 <= hour < 18:
            volume_percent = tts_settings.volume_daytime
            time_frame = "daytime"
        elif 18 <= hour < 22:
            volume_percent = tts_settings.volume_evening
            time_frame = "evening"
        else:
            volume_percent = tts_settings.volume_night
            time_frame = "night"

        _LOGGER.debug("Time-based volume: %s (%d%%)", time_frame, volume_percent)
        return volume_percent / VOLUME_SCALE

    async def apply_volume(
        self,
        entity_id: str,
        target_volume: float,
    ) -> None:
        """Capture the original volume, set the target volume, and record the override.

        Must be called before TTS playback begins.  Raises on failure so the
        caller can decide whether to abort the delivery.

        Parameters
        ----------
        entity_id : str
            Media player entity ID.
        target_volume : float
            Desired volume level (0.0–1.0).

        Raises
        ------
        TTSVolumeControlError
            If capturing or setting the volume fails.

        """
        await self._volume_registry.capture_volume_intent(
            entity_id, override_volume=target_volume
        )
        try:
            await self._set_volume(entity_id, target_volume)
        except TTSVolumeControlError:
            # _set_volume failed before the volume changed; clear the stale intent
            # so the event-driven restore path doesn't fire later.
            await self._volume_registry.complete_intent(entity_id)
            raise

    async def safe_restore_volume(self, entity_id: str) -> None:
        """Attempt to restore the original volume, logging errors without re-raising.

        Safe to call in error-handling paths where restoration is best-effort.
        A failure here should not mask the original delivery error.

        Parameters
        ----------
        entity_id : str
            Media player entity ID.

        """
        try:
            await self._volume_registry.restore_volume(entity_id)
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to restore volume for %s after delivery error; "
                "manual volume adjustment may be needed",
                entity_id,
                exc_info=True,
            )

    def set_fallback_task(self, entity_id: str, task: asyncio.Task) -> None:
        """Register a fallback restore task in the shared registry for *entity_id*.

        Delegates to :meth:`VolumeRestorationRegistry.set_fallback_task`.
        Stores the task in the long-lived registry so that all adapter generations
        share the same task map and a new adapter can cancel the old task.

        Parameters
        ----------
        entity_id : str
            Media player entity ID.
        task : asyncio.Task
            Task running the fallback restore coroutine.

        """
        self._volume_registry.set_fallback_task(entity_id, task)

    def cancel_fallback_task(self, entity_id: str) -> None:
        """Cancel any pending fallback restore task for *entity_id* in the registry.

        Delegates to :meth:`VolumeRestorationRegistry.cancel_fallback_task`.
        Must be called at the start of each delivery to prevent a stale fallback
        task from a prior adapter instance from running concurrently.

        Parameters
        ----------
        entity_id : str
            Media player entity ID.

        """
        self._volume_registry.cancel_fallback_task(entity_id)

    async def _set_volume(self, entity_id: str, volume_level: float) -> None:
        """Set the media player volume via the HA service API.

        Parameters
        ----------
        entity_id : str
            Media player entity ID.
        volume_level : float
            Volume level (0.0–1.0).

        Raises
        ------
        TTSVolumeControlError
            If the ``media_player.volume_set`` service call fails.

        """
        try:
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
        except (HomeAssistantError, ServiceNotFound) as e:
            raise TTSVolumeControlError(
                f"Failed to set volume for {entity_id}: {e}"
            ) from e
