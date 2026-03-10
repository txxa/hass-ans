"""Deliver notifications via TTS to Home Assistant media player entities."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_VOLUME_SET,
    STATE_OFF,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

from ..channels.adapter_lifecycle import AdapterType
from ..exceptions import TTSDeliveryError, TTSVolumeControlError
from ..models import (
    DeliveryResult,
    NotificationCriticality,
    NotificationPayload,
    RecipientContactInfo,
)
from ..models.recipient import TTSSettings
from .base import AdapterMetadata, ChannelRequirement, DeliveryAdapter

if TYPE_CHECKING:
    from ..persistence.volume_restoration import VolumeRestorationRegistry

_LOGGER = logging.getLogger(__name__)

# TTS Message Sanitization Constants
MAX_MESSAGE_LENGTH = 1000  # Maximum characters before HTML escaping
CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # Non-printable characters

# Volume Management Constants
VOLUME_SCALE = 100  # HA volume is 0.0-1.0, UI shows 0-100
DELIVERY_LOCK_TIMEOUT = 30  # seconds to wait for device lock


class TTSMediaPlayerAdapter(DeliveryAdapter):
    """Deliver notifications via TTS to media player entities.

    Each instance handles one media player entity. Multiple instances
    are created by the lifecycle manager (DYNAMIC_MULTI pattern).

    Attributes:
        channel: Full channel identifier (e.g., "media_player.living_room")
        entity_id: Media player entity ID
        tts_service: TTS service name (e.g., "tts.google_translate_say")

    """

    is_system_channel = False  # Media players deliver to specific devices

    # Metadata for auto-registration
    ADAPTER_METADATA = AdapterMetadata(
        adapter_type=AdapterType.DYNAMIC_MULTI,
        channel_prefix="media_player.",
        integration="media_player",
    )

    @classmethod
    def get_requirements(cls) -> ChannelRequirement:
        """TTS media players require no specific contact information.

        Returns:
            Empty requirements dict - TTS recipients don't need email/phone/HA user.

        """
        return ChannelRequirement(
            requires_email=False,
            requires_phone=False,
            requires_ha_user=False,
            description="TTS delivery to media player (no contact info required)",
        )

    @classmethod
    def get_channel_label(cls, channel_id: str) -> str:
        """Generate label showing media player name.

        Args:
            channel_id: Full channel identifier (e.g., "media_player.living_room")

        Returns:
            Label with media player name (e.g., "Living Room")

        Examples:
            >>> TTSMediaPlayerAdapter.get_channel_label("media_player.living_room")
            "Living Room"
            >>> TTSMediaPlayerAdapter.get_channel_label("media_player.kitchen_speaker")
            "Kitchen Speaker"

        """
        # Extract entity name from channel_id
        entity_name = channel_id.replace("media_player.", "")
        # Format name nicely: underscores to spaces, title case
        return entity_name.replace("_", " ").title()

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        entity_id: str,
        tts_service: str,
        volume_registry: VolumeRestorationRegistry,
    ) -> None:
        """Initialize TTS media player adapter for a specific entity.

        Args:
            hass: Home Assistant instance for service calls
            entity_id: Media player entity ID (e.g., "living_room" from "media_player.living_room")
            tts_service: TTS service name (e.g., "tts.google_translate_say")
            volume_registry: Volume restoration registry for tracking volume changes

        """
        self._hass = hass
        self.entity_id = entity_id
        self.tts_service = tts_service
        self._volume_registry = volume_registry
        # Set the full channel name for this specific media player
        self.channel = f"media_player.{entity_id}"
        # Per-device lock to prevent concurrent deliveries to same media player
        self._delivery_lock = asyncio.Lock()

        _LOGGER.debug(
            "Initialized TTSMediaPlayerAdapter: channel=%s, tts_service=%s",
            self.channel,
            tts_service,
        )

    async def deliver(
        self,
        *,
        payload: NotificationPayload,
        contact_info: RecipientContactInfo,
        idempotency_key: str,
    ) -> DeliveryResult:
        """Deliver notification via TTS to media player.

        Args:
            payload: The notification content to send
            contact_info: Recipient contact information (unused for TTS)
            idempotency_key: Unique key for idempotent retries

        Returns:
            Result of delivery attempt (success or failure)

        Note:
            TTS settings should be stored in payload.metadata under '__tts_settings__' key.
            If not present, default settings will be used.

        """
        entity_id = self.channel  # Full entity ID: media_player.living_room

        # Extract TTS settings from metadata (if present)
        tts_settings = None
        if payload.metadata and "__tts_settings__" in payload.metadata:
            try:
                tts_settings_dict = payload.metadata["__tts_settings__"]
                tts_settings = TTSSettings.from_dict(tts_settings_dict)
            except (KeyError, ValueError, TypeError) as e:
                _LOGGER.warning(
                    "Failed to parse TTS settings from metadata: %s, using defaults",
                    e,
                )

        _LOGGER.info(
            "Starting TTS delivery: entity=%s, notification_id=%s, idempotency_key=%s",
            entity_id,
            payload.notification_id,
            idempotency_key,
        )

        # Acquire per-device lock to prevent concurrent deliveries
        try:
            async with asyncio.timeout(DELIVERY_LOCK_TIMEOUT):
                async with self._delivery_lock:
                    return await self._deliver_with_volume_management(
                        entity_id=entity_id,
                        payload=payload,
                        tts_settings=tts_settings,
                        idempotency_key=idempotency_key,
                    )
        except TimeoutError:
            error_msg = (
                f"Delivery lock timeout for {entity_id} (another TTS in progress)"
            )
            _LOGGER.warning(error_msg)
            return self.transient_failure(error=error_msg)

    async def _deliver_with_volume_management(
        self,
        *,
        entity_id: str,
        payload: NotificationPayload,
        tts_settings: TTSSettings | None,
        idempotency_key: str,
    ) -> DeliveryResult:
        """Deliver TTS with volume management (capture, set, restore).

        Args:
            entity_id: Media player entity ID
            payload: Notification payload
            tts_settings: TTS settings (if None, use defaults)
            idempotency_key: Unique key for tracking

        Returns:
            Delivery result

        """
        try:
            # Step 1: Validate media player state
            state = self._hass.states.get(entity_id)
            if state is None:
                error = f"Media player {entity_id} not found"
                _LOGGER.error(error)
                return self.permanent_failure(error=error)

            if state.state == STATE_UNAVAILABLE:
                error = f"Media player {entity_id} is unavailable"
                _LOGGER.warning(error)
                return self.transient_failure(error=error)

            if state.state == STATE_OFF:
                # Attempt to power on (some media players support this)
                _LOGGER.info(
                    "Media player %s is off, attempting to power on", entity_id
                )
                try:
                    await self._hass.services.async_call(
                        "media_player",
                        "turn_on",
                        {ATTR_ENTITY_ID: entity_id},
                        blocking=True,
                    )
                    # Wait a moment for device to power on
                    await asyncio.sleep(1.0)
                    # Re-check state
                    new_state = self._hass.states.get(entity_id)
                    if new_state and new_state.state == STATE_OFF:
                        error = f"Media player {entity_id} could not be powered on"
                        _LOGGER.warning(error)
                        return self.transient_failure(error=error)
                except (HomeAssistantError, ServiceNotFound) as e:
                    error = f"Failed to power on {entity_id}: {e}"
                    _LOGGER.warning(error)
                    return self.transient_failure(error=error)

            _LOGGER.debug("Media player %s state: %s", entity_id, state.state)

            # Step 2: Calculate target volume
            target_volume = self._calculate_target_volume(
                payload.criticality, tts_settings
            )
            _LOGGER.debug(
                "Target volume for %s: %.0f%% (criticality=%s)",
                entity_id,
                target_volume * 100,
                payload.criticality.value,
            )

            # Step 3: Capture original volume and set target volume
            try:
                await self._volume_registry.capture_volume_intent(entity_id)
                await self._set_volume(entity_id, target_volume)
                await self._volume_registry.update_override_volume(
                    entity_id, target_volume
                )
            except TTSVolumeControlError as e:
                error_msg = f"Volume control failed for {entity_id}: {e}"
                _LOGGER.warning(error_msg)
                return self.transient_failure(error=error_msg)

            # Step 4: Format and sanitize message
            message_text = self._format_message(payload, tts_settings)
            sanitized_message = self._sanitize_message(message_text)
            _LOGGER.debug(
                "TTS message for %s: length=%d, format=%s",
                entity_id,
                len(sanitized_message),
                tts_settings.message_format if tts_settings else "default",
            )

            # Step 5: Call TTS service
            try:
                await self._speak_message(entity_id, sanitized_message)
                _LOGGER.info(
                    "TTS delivery successful: entity=%s, notification_id=%s",
                    entity_id,
                    payload.notification_id,
                )
                return self.success()

            except (HomeAssistantError, ServiceNotFound) as e:
                error_msg = f"TTS service call failed for {entity_id}: {e}"
                _LOGGER.error(error_msg)
                # TTS service errors are typically transient (service unavailable)
                return self.transient_failure(error=error_msg)

        except TTSDeliveryError as e:
            # Handle known TTS delivery errors
            if e.is_permanent:
                return self.permanent_failure(error=str(e))
            return self.transient_failure(error=str(e))

        except Exception as e:
            # Catch-all for unexpected errors
            error_msg = f"Unexpected error during TTS delivery to {entity_id}: {e}"
            _LOGGER.exception(error_msg)
            return self.transient_failure(error=error_msg)

    def _calculate_target_volume(
        self,
        criticality: NotificationCriticality,
        tts_settings: TTSSettings | None,
    ) -> float:
        """Calculate target volume based on time and criticality.

        Args:
            criticality: Notification criticality level
            tts_settings: TTS settings (if None, use defaults)

        Returns:
            Target volume level (0.0-1.0)

        """
        # Use default settings if none provided
        if tts_settings is None:
            tts_settings = TTSSettings.default()

        # Check for criticality override
        if criticality.value in tts_settings.volume_override_criticalities:
            volume_percent = tts_settings.volume_override_level
            _LOGGER.debug(
                "Using volume override for criticality %s: %d%%",
                criticality.value,
                volume_percent,
            )
            return volume_percent / VOLUME_SCALE

        # Determine time-based volume
        now = datetime.now()
        hour = now.hour

        # Time frames:
        # Morning: 06:00 - 09:00
        # Daytime: 09:00 - 18:00
        # Evening: 18:00 - 22:00
        # Night: 22:00 - 06:00
        if 6 <= hour < 9:
            volume_percent = tts_settings.volume_morning
            time_frame = "morning"
        elif 9 <= hour < 18:
            volume_percent = tts_settings.volume_daytime
            time_frame = "daytime"
        elif 18 <= hour < 22:
            volume_percent = tts_settings.volume_evening
            time_frame = "evening"
        else:  # 22:00 - 06:00
            volume_percent = tts_settings.volume_night
            time_frame = "night"

        _LOGGER.debug("Time-based volume: %s (%d%%)", time_frame, volume_percent)
        return volume_percent / VOLUME_SCALE

    async def _set_volume(self, entity_id: str, volume_level: float) -> None:
        """Set media player volume.

        Args:
            entity_id: Media player entity ID
            volume_level: Volume level (0.0-1.0)

        Raises:
            TTSVolumeControlError: If volume control fails

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

    def _format_message(
        self,
        payload: NotificationPayload,
        tts_settings: TTSSettings | None,
    ) -> str:
        """Format message according to TTS settings.

        Args:
            payload: Notification payload
            tts_settings: TTS settings (if None, use default format)

        Returns:
            Formatted message text

        """
        message_format = (
            tts_settings.message_format
            if tts_settings
            else TTSSettings.default().message_format
        )

        if message_format == "title_and_message":
            return f"{payload.title}. {payload.message}"
        if message_format == "message_only":
            return payload.message
        if message_format == "title_only":
            return payload.title
        # Fallback to default
        _LOGGER.warning("Unknown message format: %s, using default", message_format)
        return f"{payload.title}. {payload.message}"

    def _sanitize_message(self, message: str) -> str:
        """Sanitize message to prevent injection attacks and errors.

        Sanitization steps (in order):
        1. Remove control characters (non-printable)
        2. Enforce maximum length
        3. Escape HTML/XML entities

        Args:
            message: Raw message text

        Returns:
            Sanitized message safe for TTS service

        """
        # Step 1: Remove control characters (non-printable ASCII)
        message = CONTROL_CHAR_PATTERN.sub("", message)

        # Step 2: Enforce maximum length BEFORE escaping
        # (prevents entity expansion bypass attacks)
        if len(message) > MAX_MESSAGE_LENGTH:
            message = message[:MAX_MESSAGE_LENGTH]
            _LOGGER.warning(
                "TTS message truncated to %d characters", MAX_MESSAGE_LENGTH
            )

        # Step 3: Escape all HTML/XML entities to prevent tag injection
        return html.escape(message, quote=False)

    async def _speak_message(self, entity_id: str, message: str) -> None:
        """Call TTS service to speak message.

        Args:
            entity_id: Media player entity ID
            message: Sanitized message text

        Raises:
            HomeAssistantError: If TTS service call fails
            ServiceNotFound: If TTS service doesn't exist

        """
        # Extract service domain and name from tts_service
        # Format: "tts.google_translate_say" -> domain="tts", service="google_translate_say"
        parts = self.tts_service.split(".", 1)
        if len(parts) != 2:
            raise HomeAssistantError(
                f"Invalid TTS service format: {self.tts_service} (expected 'domain.service')"
            )

        domain, service = parts

        service_data: dict[str, Any] = {
            ATTR_ENTITY_ID: entity_id,
            "message": message,
        }

        await self._hass.services.async_call(
            domain=domain,
            service=service,
            service_data=service_data,
            blocking=True,
        )

        _LOGGER.debug("TTS service called: %s for %s", self.tts_service, entity_id)
