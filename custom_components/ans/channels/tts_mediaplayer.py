"""Deliver notifications via TTS to Home Assistant media player entities."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, ClassVar

from homeassistant.const import (
    STATE_OFF,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)

from ..exceptions import TTSDeliveryError, TTSVolumeControlError
from ..models import (
    DeliveryResult,
    NotificationPayload,
    RecipientContactInfo,
)
from ..models.recipient import TTSSettings
from .base import (
    AdapterFactory,
    AdapterMetadata,
    AdapterType,
    ChannelRequirement,
    DeliveryAdapter,
    DeliveryOptions,
    TTSDeliveryOptions,
)
from .volume_controller import VolumeController

if TYPE_CHECKING:
    from ..config.repository import ConfigRepository
    from ..delivery.factory import AdapterDeps

_LOGGER = logging.getLogger(__name__)

# TTS Message Sanitization Constants
MAX_MESSAGE_LENGTH = 1000  # Maximum characters before truncation
# C0/C1 non-printable control chars, Unicode bidi override/embedding controls,
# and bidi isolate controls — prevents log corruption and text rendering attacks.
CONTROL_CHAR_PATTERN = re.compile(
    r"[\x00-\x1f\x7f-\x9f"  # C0 and C1 control characters
    r"\u200b-\u200f"  # Zero-width chars and directional marks
    r"\u202a-\u202e"  # Bidi embedding/override controls
    r"\u2066-\u2069]"  # Bidi isolate controls
)

# Delivery timing constants
# Must exceed TTS_SPEAK_TIMEOUT + VOLUME_SET_TIMEOUT (from volume_controller.py)
# + headroom so that a concurrent delivery waiting for the lock outlasts
# a hung first delivery completing its full cleanup (TTS timeout + volume restore).
DELIVERY_LOCK_TIMEOUT = 60  # seconds to wait for device lock
TTS_SPEAK_TIMEOUT = 30  # seconds to wait for tts.speak service call to complete

# Fallback restore timeout — computed dynamically per delivery from message length.
# Formula: max(MIN, len(message) // CPS + BUFFER)
# This prevents the fallback from firing mid-playback for long messages while
# keeping the safety-net tight for short ones.
CHARS_PER_SECOND_ESTIMATE = 12  # German Thorsten / Piper; ~15 for English engines
FALLBACK_RESTORE_BUFFER = 10  # seconds: player buffering + IDLE event latency
FALLBACK_RESTORE_MIN = 30  # floor: short or empty messages still get a safety net


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
    )
    # Full channel prefix including separator, derived from metadata.
    # Eliminates hardcoded "media_player." literals throughout the class.
    _CHANNEL_PREFIX: ClassVar[str] = "media_player."

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
        entity_name = channel_id[len(cls._CHANNEL_PREFIX) :]
        # Format name nicely: underscores to spaces, title case
        return entity_name.replace("_", " ").title()

    @classmethod
    def get_metadata(cls) -> AdapterMetadata:
        """Return adapter metadata."""
        return cls.ADAPTER_METADATA

    @classmethod
    def matches_channel(cls, channel_id: str) -> bool:
        """Return True if channel_id belongs to this adapter."""
        return channel_id.startswith(cls._CHANNEL_PREFIX)

    @classmethod
    def extract_variant(cls, channel_id: str) -> str | None:
        """Return the entity_name portion of a media_player channel_id."""
        if cls.matches_channel(channel_id):
            return channel_id[len(cls._CHANNEL_PREFIX) :]
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
        self._channel = f"{self._CHANNEL_PREFIX}{entity_name}"
        # Delivery lock is stored in VolumeRestorationRegistry (via
        # VolumeController) so it survives adapter recreation during resync()
        # and all adapter generations for the same entity share a single lock.
        # Fallback restore tasks are tracked in VolumeRestorationRegistry (via
        # VolumeController) so that all adapter generations share the same task
        # map and a new adapter can cancel the old task on the next delivery.

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
        options: DeliveryOptions | None = None,
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
        options : DeliveryOptions | None
            Per-delivery options including TTS settings.

        Returns
        -------
        DeliveryResult
            Result of delivery attempt (success or failure).

        """
        tts_settings = (
            options.tts_settings if isinstance(options, TTSDeliveryOptions) else None
        )
        entity_id = self.channel  # Full entity ID: media_player.living_room

        _LOGGER.info(
            "Starting TTS delivery: entity=%s, notification_id=%s, idempotency_key=%s",
            entity_id,
            payload.notification_id,
            idempotency_key,
        )

        # Acquire the shared per-entity delivery lock to serialise concurrent
        # deliveries to the same media player. The lock is held in the
        # VolumeRestorationRegistry (accessed via VolumeController) so it
        # survives adapter recreation during ChannelManager.resync().
        #
        # IMPORTANT: asyncio.timeout() is scoped to cover ONLY the lock
        # acquisition, not the delivery body. The delivery body has its own
        # independent timeout (TTS_SPEAK_TIMEOUT) inside _speak_message.
        # Wrapping both in one timeout would cause a slow tts.speak call to
        # exhaust the DELIVERY_LOCK_TIMEOUT and produce a misleading
        # "Delivery lock timeout (another TTS in progress)" error even though
        # no other delivery is competing for the lock.
        lock = self._volume_controller.get_delivery_lock(entity_id)
        try:
            async with asyncio.timeout(DELIVERY_LOCK_TIMEOUT):
                await lock.acquire()
        except TimeoutError:
            error_msg = (
                f"Delivery lock timeout for {entity_id} (another TTS in progress)"
            )
            _LOGGER.warning(error_msg)
            return self.transient_failure(error=error_msg)
        try:
            return await self._deliver_with_volume_management(
                entity_id=entity_id,
                payload=payload,
                tts_settings=tts_settings,
                idempotency_key=idempotency_key,
            )
        finally:
            lock.release()

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

            if state.state in (STATE_UNAVAILABLE, STATE_OFF):
                error = f"Media player {entity_id} is {state.state}, delivery skipped; will retry"
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

            # Cancel any pending fallback restore for this entity from a concurrent
            # or prior delivery (possibly from a prior adapter instance) before
            # capturing a fresh volume intent.
            self._volume_controller.cancel_fallback_task(entity_id)

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
                # Schedule a fallback restore in case the PLAYING→IDLE event is
                # missed (e.g. Bluetooth disconnect before playback completes).
                # Stored in the shared VolumeRestorationRegistry so all adapter
                # generations can cancel it on the next delivery.
                # Timeout is computed from message length so it won't fire
                # mid-playback for long messages yet stays tight for short ones.
                fallback_timeout = max(
                    FALLBACK_RESTORE_MIN,
                    len(sanitized_message) // CHARS_PER_SECOND_ESTIMATE
                    + FALLBACK_RESTORE_BUFFER,
                )
                _LOGGER.debug(
                    "Fallback restore timeout for %s: %ds (message=%d chars)",
                    entity_id,
                    fallback_timeout,
                    len(sanitized_message),
                )
                fallback = asyncio.create_task(
                    self._fallback_restore(entity_id, timeout=fallback_timeout)
                )
                self._volume_controller.set_fallback_task(entity_id, fallback)
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

            except TimeoutError:
                error_msg = (
                    f"TTS speak timed out for {entity_id} after {TTS_SPEAK_TIMEOUT}s"
                    " — TTS engine may be overloaded or unresponsive"
                )
                _LOGGER.warning(error_msg)
                await self._volume_controller.safe_restore_volume(entity_id)
                volume_captured = False
                return self.transient_failure(error=error_msg)

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
        1. Remove control characters (non-printable), bidi overrides, and
           zero-width characters (see ``CONTROL_CHAR_PATTERN``).
        2. Enforce maximum length.

        .. warning:: SSML injection risk with SSML-aware TTS engines

            HTML/XML escaping is intentionally omitted here. Most local TTS
            engines (e.g. Piper) treat the message as plain text and would
            speak escaped entities aloud (e.g. ``&amp;`` instead of ``&``).

            However, **SSML-aware engines** (e.g. Google Cloud TTS, Amazon
            Polly, Microsoft Azure TTS) evaluate ``<speak>``, ``<phoneme>``,
            ``<break/>``, and similar tags embedded in the message. If message
            content originates from untrusted sources (e.g. user input,
            external automations, webhook data), an attacker could inject
            arbitrary SSML markup to alter speech synthesis behaviour.

            **Do not use SSML-aware TTS engines with untrusted message
            sources** unless you add SSML escaping (``<`` → ``&lt;``,
            ``>`` → ``&gt;``, ``&`` → ``&amp;``) before this method is
            called. Piper and other plain-text engines are not affected.

        Parameters
        ----------
        message : str
            Raw message text.

        Returns
        -------
        str
            Sanitized message safe for plain-text TTS engines.

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
        async with asyncio.timeout(TTS_SPEAK_TIMEOUT):
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

    async def _fallback_restore(self, entity_id: str, *, timeout: int) -> None:
        """Restore volume after a fallback timeout if PLAYING→IDLE was missed.

        Scheduled after every successful TTS delivery and registered in the
        shared VolumeRestorationRegistry via VolumeController, so that a new
        adapter instance created by resync() can cancel this task on the next
        delivery. Runs as a safety net when the state-change event never fires
        (e.g. Bluetooth disconnect mid-playback).

        Parameters
        ----------
        entity_id : str
            Media player entity ID.
        timeout : int
            Seconds to wait before triggering the restore. Computed dynamically
            from message length by the caller via CHARS_PER_SECOND_ESTIMATE.

        """
        try:
            await asyncio.sleep(timeout)
            _LOGGER.warning(
                "Fallback volume restore triggered for %s "
                "— no PLAYING→IDLE event received within %ds",
                entity_id,
                timeout,
            )
            await self._volume_controller.safe_restore_volume(entity_id)
        except asyncio.CancelledError:
            pass
