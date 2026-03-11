"""Deliver notifications via TTS to Home Assistant media player entities."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, ClassVar

from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_OFF,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)

from ..channels.adapter_lifecycle import AdapterType
from ..exceptions import TTSDeliveryError, TTSVolumeControlError
from ..models import (
    DeliveryResult,
    NotificationPayload,
    RecipientContactInfo,
)
from ..models.recipient import TTSSettings
from .base import AdapterFactory, AdapterMetadata, ChannelRequirement, DeliveryAdapter
from .volume_controller import VolumeController

if TYPE_CHECKING:
    from ..config.repository import ConfigRepository
    from ..delivery.factory import AdapterDeps

_LOGGER = logging.getLogger(__name__)

# TTS Message Sanitization Constants
MAX_MESSAGE_LENGTH = 1000  # Maximum characters before truncation
CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # Non-printable characters

# Delivery timing constants
DELIVERY_LOCK_TIMEOUT = 30  # seconds to wait for device lock
POWER_ON_WAIT_SECONDS = 1.0  # seconds to wait after powering on a media player


class TTSMediaPlayerAdapter(DeliveryAdapter):
    """Deliver notifications via TTS to media player entities.

    Each instance handles one media player entity. Multiple instances
    are created by the lifecycle manager (DYNAMIC_MULTI pattern).

    Attributes
    ----------
    channel : str
        Full channel identifier (e.g., "media_player.living_room").
    entity_name : str
        Media player entity name.

    """

    # Metadata for auto-registration
    ADAPTER_METADATA: ClassVar[AdapterMetadata] = AdapterMetadata(
        adapter_type=AdapterType.DYNAMIC_MULTI,
        channel_prefix="media_player",
        integration="media_player",
        channel_separator=".",
    )
    # Full channel prefix including separator, derived from metadata.
    # Eliminates hardcoded "media_player." literals throughout the class.
    _PREFIX: ClassVar[str] = (
        ADAPTER_METADATA.channel_prefix + ADAPTER_METADATA.channel_separator
    )

    @classmethod
    def get_requirements(cls) -> ChannelRequirement:
        """Return contact-info requirements for TTS media player channels.

        Returns
        -------
        ChannelRequirement
            Empty requirements — TTS recipients don't need email, phone, or HA user.

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

        Parameters
        ----------
        channel_id : str
            Full channel identifier (e.g., "media_player.living_room").

        Returns
        -------
        str
            Label with media player name (e.g., "Living Room").

        Examples
        --------
        >>> TTSMediaPlayerAdapter.get_channel_label("media_player.living_room")
        "Living Room"
        >>> TTSMediaPlayerAdapter.get_channel_label("media_player.kitchen_speaker")
        "Kitchen Speaker"

        """
        # Extract entity name from channel_id
        entity_name = channel_id[len(cls._PREFIX) :]
        # Format name nicely: underscores to spaces, title case
        return entity_name.replace("_", " ").title()

    @classmethod
    def get_metadata(cls) -> AdapterMetadata:
        """Return adapter metadata."""
        return cls.ADAPTER_METADATA

    @classmethod
    def matches_channel(cls, channel_id: str) -> bool:
        """Return True if channel_id belongs to this adapter."""
        return channel_id.startswith(cls._PREFIX)

    @classmethod
    def extract_variant(cls, channel_id: str) -> str | None:
        """Return the entity_name portion of a media_player channel_id."""
        if cls.matches_channel(channel_id):
            return channel_id[len(cls._PREFIX) :]
        return None

    @classmethod
    def create_factory(
        cls,
        factory_fn=None,
        cleanup_fn=None,
        deps: AdapterDeps | None = None,
    ) -> AdapterFactory:
        """Create an AdapterFactory sourcing dependencies from *deps*.

        Parameters
        ----------
        factory_fn : Callable, optional
            Custom factory function (defaults to standard TTS constructor).
        cleanup_fn : Callable, optional
            Optional cleanup function.
        deps : AdapterDeps | None
            Runtime dependencies.  Must not be ``None`` — both
            ``deps.config_repo`` and ``deps.volume_registry`` are required.

        Returns
        -------
        AdapterFactory
            Configured for TTS media player adapters.

        Raises
        ------
        ValueError
            If *deps* is ``None``.

        """
        if deps is None:
            raise ValueError(
                "TTSMediaPlayerAdapter.create_factory() requires deps "
                "(config_repo and volume_registry must be provided)."
            )
        config_repo = deps.config_repo
        volume_registry = deps.volume_registry

        if factory_fn is None:

            def _factory(
                hass: HomeAssistant, entity_name: str | None
            ) -> TTSMediaPlayerAdapter:
                if not entity_name:
                    raise ValueError(
                        "entity_name is required for TTSMediaPlayerAdapter"
                    )
                return cls(
                    hass=hass,
                    entity_name=entity_name,
                    config_repo=config_repo,
                    volume_controller=VolumeController(
                        hass=hass, volume_registry=volume_registry
                    ),
                )

            factory_fn = _factory

        return super().create_factory(factory_fn=factory_fn, cleanup_fn=cleanup_fn)

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        entity_name: str,
        config_repo: ConfigRepository,
        volume_controller: VolumeController,
    ) -> None:
        """Initialize TTS media player adapter for a specific entity.

        Parameters
        ----------
        hass : HomeAssistant
            Home Assistant instance for service calls.
        entity_name : str
            Media player entity name (e.g., "living_room" from "media_player.living_room").
        config_repo : ConfigRepository
            Config repository used to resolve the TTS service at delivery time.
            Reading the service name on each delivery ensures config changes are
            honoured without recreating the adapter.
        volume_controller : VolumeController
            Volume controller handling capture, adjustment, and restoration.

        """
        self._hass = hass
        self.entity_name = entity_name
        self._config_repo = config_repo
        self._volume_controller = volume_controller
        # Full channel name derived from class-level prefix
        self._channel = f"{self._PREFIX}{entity_name}"
        # Per-device lock to prevent concurrent deliveries to same media player
        self._delivery_lock = asyncio.Lock()

        _LOGGER.debug(
            "Initialized TTSMediaPlayerAdapter: channel=%s",
            self._channel,
        )

    @property
    def channel(self) -> str:  # type: ignore[override]  # mypy false positive: abstract property
        """Return the channel identifier for this media player."""
        return self._channel

    async def deliver(
        self,
        *,
        payload: NotificationPayload,
        contact_info: RecipientContactInfo,
        idempotency_key: str,
        tts_settings: TTSSettings | None = None,
    ) -> DeliveryResult:
        """Deliver notification via TTS to media player.

        Parameters
        ----------
        payload : NotificationPayload
            The notification content to send.
        contact_info : RecipientContactInfo
            Recipient contact information (unused for TTS).
        idempotency_key : str
            Unique key for idempotent retries.
        tts_settings : TTSSettings | None
            Per-recipient TTS configuration (volume, format, etc.).

        Returns
        -------
        DeliveryResult
            Result of delivery attempt (success or failure).

        """
        entity_id = self.channel  # Full entity ID: media_player.living_room

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

        Parameters
        ----------
        entity_id : str
            Media player entity ID.
        payload : NotificationPayload
            Notification payload.
        tts_settings : TTSSettings | None
            TTS settings (if None, use defaults).
        idempotency_key : str
            Unique key for tracking.

        Returns
        -------
        DeliveryResult
            Delivery result.

        """
        # Resolve TTS service from config at delivery time so that config changes
        # (e.g. switching TTS engine) take effect without recreating the adapter.
        tts_service = self._config_repo.snapshot().system_config.tts_service
        if not tts_service:
            return self.permanent_failure(
                error="TTS service not configured in system settings"
            )

        volume_captured = False
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
                    await asyncio.sleep(POWER_ON_WAIT_SECONDS)
                    # Re-check state
                    new_state = self._hass.states.get(entity_id)
                    if new_state and new_state.state in (STATE_OFF, STATE_UNAVAILABLE):
                        error = f"Media player {entity_id} could not be powered on"
                        _LOGGER.warning(error)
                        return self.transient_failure(error=error)
                except ServiceNotFound as e:
                    error = f"Failed to power on {entity_id}: {e}"
                    _LOGGER.error(error)
                    return self.permanent_failure(error=error)
                except ServiceValidationError as e:
                    error = f"Failed to power on {entity_id}: {e}"
                    _LOGGER.error(error)
                    return self.permanent_failure(error=error)
                except HomeAssistantError as e:
                    error = f"Failed to power on {entity_id}: {e}"
                    _LOGGER.warning(error)
                    return self.transient_failure(error=error)

            _LOGGER.debug("Media player %s state: %s", entity_id, state.state)

            # Step 2: Calculate target volume
            target_volume = self._volume_controller.calculate_target_volume(
                payload.criticality, tts_settings
            )
            _LOGGER.debug(
                "Target volume for %s: %.0f%% (criticality=%s)",
                entity_id,
                target_volume * 100,
                payload.criticality.value,
            )

            # Step 3: Capture original volume, set target volume, track override
            try:
                await self._volume_controller.apply_volume(entity_id, target_volume)
                volume_captured = True  # Volume is now changed; restoration is required
            except TTSVolumeControlError as e:
                # Volume was never changed; no restoration needed.
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
                await self._speak_message(entity_id, sanitized_message, tts_service)
                _LOGGER.info(
                    "TTS delivery successful: entity=%s, notification_id=%s",
                    entity_id,
                    payload.notification_id,
                )
                # Success: the player will enter PLAYING then transition to IDLE;
                # the event-driven volume restoration in VolumeRestorationRegistry
                # handles cleanup automatically.
                return self.success()

            except ServiceNotFound as e:
                error_msg = f"TTS service not found for {entity_id}: {e}"
                _LOGGER.error(error_msg)
                # TTS never started → player won't enter PLAYING → event-driven
                # restoration (PLAYING→IDLE) won't fire → restore volume immediately.
                await self._volume_controller.safe_restore_volume(entity_id)
                volume_captured = False
                return self.permanent_failure(error=error_msg)

            except ServiceValidationError as e:
                error_msg = f"TTS service validation error for {entity_id}: {e}"
                _LOGGER.error(error_msg)
                await self._volume_controller.safe_restore_volume(entity_id)
                volume_captured = False
                return self.permanent_failure(error=error_msg)

            except HomeAssistantError as e:
                error_msg = f"TTS service call failed for {entity_id}: {e}"
                _LOGGER.warning(error_msg)
                await self._volume_controller.safe_restore_volume(entity_id)
                volume_captured = False
                return self.transient_failure(error=error_msg)

        except TTSDeliveryError as e:
            # Handle known TTS delivery errors
            if volume_captured:
                await self._volume_controller.safe_restore_volume(entity_id)
            if e.is_permanent:
                return self.permanent_failure(error=str(e))
            return self.transient_failure(error=str(e))

        except Exception as e:
            # Catch-all for unexpected errors
            if volume_captured:
                await self._volume_controller.safe_restore_volume(entity_id)
            error_msg = f"Unexpected error during TTS delivery to {entity_id}: {e}"
            _LOGGER.exception(error_msg)
            return self.transient_failure(error=error_msg)

    def _format_message(
        self,
        payload: NotificationPayload,
        tts_settings: TTSSettings | None,
    ) -> str:
        """Format message according to TTS settings.

        Parameters
        ----------
        payload : NotificationPayload
            Notification payload.
        tts_settings : TTSSettings | None
            TTS settings (if None, use default format).

        Returns
        -------
        str
            Formatted message text.

        Raises
        ------
        TTSDeliveryError
            If message_format is not a recognised value (permanent failure — no
            retry).  Since message_format is written and validated by the config
            flow this branch is unreachable in normal operation; the exception
            makes the invariant explicit and prevents wasted retry cycles.

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
        raise TTSDeliveryError(
            f"Unknown message_format: {message_format!r}", is_permanent=True
        )

    def _sanitize_message(self, message: str) -> str:
        """Sanitize message to prevent control-character injection and truncate.

        Sanitization steps (in order):
        1. Remove control characters (non-printable)
        2. Enforce maximum length

        Note: HTML/XML escaping is intentionally omitted. Most TTS engines
        accept plain text; escaping would cause engines to speak entity names
        aloud (e.g., "&amp;" instead of "&"). SSML-aware engines should handle
        their own escaping at the service layer.

        Parameters
        ----------
        message : str
            Raw message text.

        Returns
        -------
        str
            Sanitized message safe for TTS service.

        """
        # Step 1: Remove control characters (non-printable ASCII)
        message = CONTROL_CHAR_PATTERN.sub("", message)

        # Step 2: Enforce maximum length; append ellipsis so listeners know
        # the message was cut short.
        if len(message) > MAX_MESSAGE_LENGTH:
            message = message[: MAX_MESSAGE_LENGTH - 1] + "…"
            _LOGGER.warning(
                "TTS message truncated to %d characters (truncation marker appended)",
                MAX_MESSAGE_LENGTH,
            )

        return message

    async def _speak_message(
        self, entity_id: str, message: str, tts_service: str
    ) -> None:
        """Deliver a message via the modern ``tts.speak`` service action.

        Targets the TTS engine entity directly and provides the destination
        media player and message as service data.

        Parameters
        ----------
        entity_id : str
            Media player entity ID (delivery destination).
        message : str
            Sanitized message text.
        tts_service : str
            TTS engine entity ID (e.g. ``"tts.piper"`` or
            ``"tts.google_translate"``), used as the service call target.

        Raises
        ------
        HomeAssistantError
            If the ``tts.speak`` call fails at runtime.
        ServiceNotFound
            If the ``tts.speak`` action is unavailable.

        """
        await self._hass.services.async_call(
            domain="tts",
            service="speak",
            service_data={
                "media_player_entity_id": entity_id,
                "message": message,
            },
            target={"entity_id": tts_service},
            blocking=True,
        )

        _LOGGER.debug("TTS speak called: engine=%s, player=%s", tts_service, entity_id)
