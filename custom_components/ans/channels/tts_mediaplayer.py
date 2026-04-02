"""Deliver notifications via TTS to Home Assistant media player entities."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import xml.sax.saxutils
from collections.abc import Callable
from contextlib import asynccontextmanager, nullcontext
from typing import TYPE_CHECKING, ClassVar

from homeassistant.const import (
    STATE_IDLE,
    STATE_OFF,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)
from homeassistant.util import dt as dt_util

from custom_components.ans.const import (
    TTS_DEFAULT_SSML_ENABLED,
)

from ..exceptions import TTSDeliveryError, TTSVolumeControlError
from ..models import (
    DeliveryResult,
    NotificationCriticality,
    NotificationPayload,
    RecipientContactInfo,
)
from ..models.recipient import TTSSettings
from ..persistence.volume_restoration import (
    VOLUME_CHANGE_THRESHOLD as _VOLUME_CHANGE_THRESHOLD,
)
from ..persistence.volume_restoration import (
    VolumeRestorationRegistry,
)
from .base import (
    AdapterFactory,
    AdapterMetadata,
    AdapterType,
    ChannelRequirement,
    DeliveryAdapter,
    DeliveryOptions,
    TTSDeliveryOptions,
)

if TYPE_CHECKING:
    from ..config.repository import ConfigRepository
    from ..delivery.factory import AdapterDeps

_LOGGER = logging.getLogger(__name__)

# HA volume scale: API accepts 0.0–1.0, UI and config expose 0–100.
VOLUME_SCALE = 100

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
# Must exceed TTS_SPEAK_TIMEOUT + VOLUME_SET_TIMEOUT (from const.py)
# + headroom so that a concurrent delivery waiting for the lock outlasts
# a hung first delivery completing its full cleanup (TTS timeout + volume restore).
DELIVERY_LOCK_TIMEOUT = 60  # seconds to wait for device lock
TTS_SPEAK_TIMEOUT = 30  # seconds to wait for tts.speak service call to complete

# Safety-net restore: dynamic timeout computed from message length so short
# messages don't wait unnecessarily long.  Constants mirror a 12 char/s average
# speech rate with a 5 s safety buffer, bounded by a hard floor and ceiling.
CHARS_PER_SECOND_ESTIMATE = 12  # average TTS speech rate (chars per second)
FALLBACK_MIN_TIMEOUT = 8  # floor: minimum fallback wait in seconds
FALLBACK_BUFFER_SECONDS = 5  # added headroom after estimated playback end
FALLBACK_MAX_TIMEOUT = 120  # ceiling: cap for very long messages


def _calculate_target_volume(
    criticality: NotificationCriticality,
    tts_settings: TTSSettings | None,
    entity_id: str = "unknown",
) -> float:
    """Calculate delivery volume based on time of day and notification criticality.

    Parameters
    ----------
    criticality : NotificationCriticality
        Notification criticality level.
    tts_settings : TTSSettings | None
        Per-recipient TTS settings; defaults are used when ``None``.
    entity_id : str
        Media player entity ID for logging.

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
            "Using volume override for criticality %s: %d%% (entity=%s)",
            criticality.value,
            volume_percent,
            entity_id,
        )
        return volume_percent / VOLUME_SCALE

    # Time-based selection:
    # Morning: 06:00–09:00 | Daytime: 09:00–19:00
    # Evening: 19:00–22:00 | Night:   22:00–06:00
    hour = dt_util.now().hour
    if 6 <= hour < 9:
        volume_percent = tts_settings.volume_morning
        time_frame = "morning"
    elif 9 <= hour < 19:
        volume_percent = tts_settings.volume_daytime
        time_frame = "daytime"
    elif 19 <= hour < 22:
        volume_percent = tts_settings.volume_evening
        time_frame = "evening"
    else:
        volume_percent = tts_settings.volume_night
        time_frame = "night"

    _LOGGER.debug(
        "Time-based volume: %s (%d%%) (entity=%s)",
        time_frame,
        volume_percent,
        entity_id,
    )
    return volume_percent / VOLUME_SCALE


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
        get_delivery_lock: Callable[[str], asyncio.Lock] | None = None,
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
        get_delivery_lock : Callable[[str], asyncio.Lock] | None
            Callable that returns a shared ``asyncio.Lock`` for a given
            entity ID.  Typically ``ChannelManager.get_delivery_lock``.
            When ``None`` a fresh lock is created per adapter instance
            (suitable for unit tests; not recommended in production).

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
                entity_id = f"{cls._CHANNEL_PREFIX}{entity_name}"
                lock = (
                    get_delivery_lock(entity_id)
                    if get_delivery_lock is not None
                    else asyncio.Lock()
                )
                return cls(
                    hass=hass,
                    entity_name=entity_name,
                    config_repo=config_repo,
                    volume_registry=volume_registry,
                    delivery_lock=lock,
                )

            factory_fn = _factory

        return super().create_factory(factory_fn=factory_fn, cleanup_fn=cleanup_fn)

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        entity_name: str,
        config_repo: ConfigRepository,
        volume_registry: VolumeRestorationRegistry,
        delivery_lock: asyncio.Lock,
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
        volume_registry : VolumeRestorationRegistry
            Registry handling volume capture, adjustment, and restoration.
        delivery_lock : asyncio.Lock
            Shared per-entity lock sourced from :class:`ChannelManager` so it
            survives adapter recreation during :meth:`ChannelManager.resync`
            and all adapter generations for the same entity share a single lock.

        """
        self._hass = hass
        self.entity_name = entity_name
        self._config_repo = config_repo
        self._volume_registry = volume_registry
        self._delivery_lock = delivery_lock
        # Full channel name derived from class-level prefix
        self._channel = f"{self._CHANNEL_PREFIX}{entity_name}"

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
        job_id: str,
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
        job_id : str
            Job identifier for cross-layer log correlation.
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
            "Starting TTS delivery: job_id=%s entity=%s, notification_id=%s, idempotency_key=%s",
            job_id,
            entity_id,
            payload.notification_id,
            idempotency_key,
        )

        # Acquire the shared per-entity delivery lock to serialise concurrent
        # deliveries to the same media player. The lock is sourced from
        # ChannelManager so it survives adapter recreation during resync().
        #
        # IMPORTANT: asyncio.timeout() is scoped to cover ONLY the lock
        # acquisition, not the delivery body. The delivery body has its own
        # independent timeout (TTS_SPEAK_TIMEOUT) inside _speak_message.
        # Wrapping both in one timeout would cause a slow tts.speak call to
        # exhaust the DELIVERY_LOCK_TIMEOUT and produce a misleading
        # "Delivery lock timeout (another TTS in progress)" error even though
        # no other delivery is competing for the lock.
        if not await self._acquire_lock_with_timeout(
            self._delivery_lock, DELIVERY_LOCK_TIMEOUT
        ):
            _LOGGER.warning(
                "Delivery lock timeout for %s job_id=%s notification_id=%s "
                "(another TTS delivery in progress)",
                entity_id,
                job_id,
                payload.notification_id,
            )
            return self.transient_failure(
                error=f"Delivery lock timeout for {entity_id} (another TTS in progress)"
            )
        try:
            self._volume_registry.mark_delivery_active(entity_id)
            return await self._deliver_with_volume_management(
                entity_id=entity_id,
                payload=payload,
                tts_settings=tts_settings,
                idempotency_key=idempotency_key,
                job_id=job_id,
            )
        finally:
            self._volume_registry.mark_delivery_inactive(entity_id)
            self._delivery_lock.release()

    async def _deliver_with_volume_management(
        self,
        *,
        entity_id: str,
        payload: NotificationPayload,
        tts_settings: TTSSettings | None,
        idempotency_key: str,
        job_id: str,
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
        job_id : str
            Job identifier for cross-layer log correlation.

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

        # ── Pre-volume section ──────────────────────────────────────────────
        # Steps 1–2 perform state checks and volume calculation.  No volume
        # change has occurred yet; any failure returns directly without needing
        # volume restoration.

        # Step 1: Validate media player state
        state = self._hass.states.get(entity_id)
        if state is None:
            _LOGGER.error(
                "Media player %s not found: job_id=%s notification_id=%s",
                entity_id,
                job_id,
                payload.notification_id,
            )
            return self.permanent_failure(error=f"Media player {entity_id} not found")

        if state.state in (STATE_UNAVAILABLE, STATE_OFF):
            _LOGGER.warning(
                "Media player %s is %s, delivery skipped: job_id=%s notification_id=%s "
                "— will retry",
                entity_id,
                state.state,
                job_id,
                payload.notification_id,
            )
            return self.transient_failure(
                error=f"Media player {entity_id} is {state.state}, delivery skipped; will retry"
            )

        _LOGGER.debug(
            "Media player %s state: %s job_id=%s notification_id=%s",
            entity_id,
            state.state,
            job_id,
            payload.notification_id,
        )

        # Step 2: Calculate target volume
        target_volume = _calculate_target_volume(
            payload.criticality, tts_settings, entity_id
        )
        _LOGGER.debug(
            "Target volume for %s: %.0f%% (criticality=%s job_id=%s notification_id=%s)",
            entity_id,
            target_volume * 100,
            payload.criticality.value,
            job_id,
            payload.notification_id,
        )

        # Step 2b: Skip volume management entirely when the player is already at
        # the target level.  Issuing a no-op volume_set wastes a round-trip, arms
        # the echo guard, and creates an intent whose override_volume equals
        # original_volume — making every subsequent volume event during playback
        # look like a user change and spuriously clearing the intent.
        current_volume = state.attributes.get("volume_level")
        volume_change_needed = current_volume is None or (
            abs(target_volume - float(current_volume)) >= _VOLUME_CHANGE_THRESHOLD
        )

        # Respect the per-recipient volume management toggle.  When disabled,
        # TTS plays at the current device volume without any capture/set/restore
        # cycle — identical to the "already at target" skip path.
        if (
            volume_change_needed
            and tts_settings is not None
            and not tts_settings.volume_management_enabled
        ):
            _LOGGER.debug(
                "Volume management disabled for %s — skipping "
                "job_id=%s notification_id=%s",
                entity_id,
                job_id,
                payload.notification_id,
            )
            volume_change_needed = False

        # cancel_fallback_task and capture_volume_intent (inside apply_volume)
        # together supersede all prior restore tasks for this entity.  Their
        # order is intentional: cancel_fallback_task removes the previous
        # fallback timer first, then capture_volume_intent (called inside
        # apply_volume) cancels any pending _delayed_restore and creates a
        # fresh intent.  Do not reorder or separate these two calls.
        if volume_change_needed:
            self._volume_registry.cancel_fallback_task(entity_id)
        else:
            _LOGGER.debug(
                "Volume already at target (%.0f%%) for %s — skipping volume management "
                "job_id=%s notification_id=%s",
                target_volume * 100,
                entity_id,
                job_id,
                payload.notification_id,
            )

        # Steps 3–5: apply volume (if needed), format/sanitize message, call TTS service.
        # _apply_and_restore_volume calls apply_volume on entry; any exception
        # raised inside the block triggers _safe_restore_volume before the
        # exception propagates to the handlers below.
        # When no volume change is needed, nullcontext() replaces the context
        # manager so the delivery body runs unchanged without any volume I/O.
        try:
            if volume_change_needed:
                ctx = self._apply_and_restore_volume(entity_id, target_volume)
            else:
                ctx = nullcontext()
            async with ctx:
                # Step 4: Format and sanitize message
                message_text = self._format_message(payload, tts_settings)
                sanitized_message = self._sanitize_message(message_text, entity_id)
                _LOGGER.debug(
                    "TTS message for %s: length=%d format=%s job_id=%s notification_id=%s",
                    entity_id,
                    len(sanitized_message),
                    tts_settings.message_format if tts_settings else "default",
                    job_id,
                    payload.notification_id,
                )

                # Step 5: Call TTS service
                await self._speak_message(
                    entity_id,
                    sanitized_message,
                    tts_service,
                    ssml_enabled=(
                        tts_settings.ssml_enabled
                        if tts_settings is not None
                        else TTS_DEFAULT_SSML_ENABLED
                    ),
                    notification_id=payload.notification_id,
                    job_id=job_id,
                )
                _LOGGER.info(
                    "TTS delivery successful: job_id=%s entity=%s, notification_id=%s",
                    job_id,
                    entity_id,
                    payload.notification_id,
                )
                # For companion-app players (browser, mobile), tts.speak with
                # blocking=True waits for the audio to finish before returning,
                # so the PLAYING→IDLE event fires *during* the service call and
                # was silently dropped by the _active_delivery guard in
                # _handle_state_change.  If the player is already IDLE when we
                # return here, schedule a delayed restore immediately so the
                # fallback timer is kept as a true last-resort safety net rather
                # than the primary restore path.
                if volume_change_needed:
                    post_speak_state = self._hass.states.get(entity_id)
                    if (
                        post_speak_state is not None
                        and post_speak_state.state == STATE_IDLE
                    ):
                        _LOGGER.debug(
                            "Player already IDLE after tts.speak — requesting idle restore "
                            "for %s job_id=%s notification_id=%s",
                            entity_id,
                            job_id,
                            payload.notification_id,
                        )
                        self._volume_registry.schedule_idle_restore(entity_id)
                # Only register a fallback restore task if the intent is still
                # active.  If the intent was cleared mid-delivery (e.g. by a
                # false user-change detection that bypassed the active-delivery
                # guard), there is nothing to restore and spawning the task
                # would result in a misleading WARNING later.
                if self._volume_registry.has_active_intent(entity_id):
                    # Dynamic fallback timeout: estimate audio duration from
                    # message length so short messages restore quickly; the 120 s
                    # ceiling still protects very long announcements.
                    fallback_timeout = min(
                        FALLBACK_MAX_TIMEOUT,
                        max(
                            FALLBACK_MIN_TIMEOUT,
                            len(sanitized_message) / CHARS_PER_SECOND_ESTIMATE
                            + FALLBACK_BUFFER_SECONDS,
                        ),
                    )
                    # Schedule a fallback restore in case the PLAYING→IDLE event
                    # is missed (e.g. Bluetooth disconnect before playback
                    # completes, or schedule_idle_restore's _delayed_restore task
                    # races with a new delivery).
                    # Stored in the shared VolumeRestorationRegistry so all
                    # adapter generations can cancel it on the next delivery.
                    _LOGGER.debug(
                        "Scheduling fallback restore for %s in %.0fs "
                        "job_id=%s notification_id=%s",
                        entity_id,
                        fallback_timeout,
                        job_id,
                        payload.notification_id,
                    )
                    fallback = asyncio.create_task(
                        self._fallback_restore(
                            entity_id,
                            timeout=fallback_timeout,
                            notification_id=payload.notification_id,
                            job_id=job_id,
                        )
                    )
                    self._volume_registry.set_fallback_task(entity_id, fallback)
                else:
                    _LOGGER.debug(
                        "Skipping fallback registration for %s — intent already cleared "
                        "(volume was not changed or user-change detected mid-delivery) "
                        "job_id=%s notification_id=%s",
                        entity_id,
                        job_id,
                        payload.notification_id,
                    )
                return self.success()

        except TTSVolumeControlError as e:
            # apply_volume failed — volume was never changed, no restore needed.
            _LOGGER.warning(
                "Volume control failed for %s: job_id=%s notification_id=%s: %s",
                entity_id,
                job_id,
                payload.notification_id,
                e,
            )
            return self.transient_failure(
                error=f"Volume control failed for {entity_id}: {e}"
            )

        except TTSDeliveryError as e:
            # Raised by _format_message. Volume already restored by context manager.
            if e.is_permanent:
                return self.permanent_failure(error=str(e))
            return self.transient_failure(error=str(e))

        except ServiceNotFound as e:
            _LOGGER.error(
                "TTS service not found for %s: job_id=%s notification_id=%s: %s",
                entity_id,
                job_id,
                payload.notification_id,
                e,
            )
            return self.permanent_failure(
                error=f"TTS service not found for {entity_id}: {e}"
            )

        except ServiceValidationError as e:
            _LOGGER.error(
                "TTS service validation error for %s: job_id=%s notification_id=%s: %s",
                entity_id,
                job_id,
                payload.notification_id,
                e,
            )
            return self.permanent_failure(
                error=f"TTS service validation error for {entity_id}: {e}"
            )

        except TimeoutError:
            _LOGGER.warning(
                "TTS speak timed out for %s after %ds: job_id=%s notification_id=%s "
                "— TTS engine may be overloaded or unresponsive",
                entity_id,
                TTS_SPEAK_TIMEOUT,
                job_id,
                payload.notification_id,
            )
            return self.transient_failure(
                error=(
                    f"TTS speak timed out for {entity_id} after {TTS_SPEAK_TIMEOUT}s"
                    " — TTS engine may be overloaded or unresponsive"
                )
            )

        except HomeAssistantError as e:
            _LOGGER.warning(
                "TTS service call failed for %s: job_id=%s notification_id=%s: %s",
                entity_id,
                job_id,
                payload.notification_id,
                e,
            )
            return self.transient_failure(
                error=f"TTS service call failed for {entity_id}: {e}"
            )

        except Exception as e:
            _LOGGER.exception(
                "Unexpected error during TTS delivery to %s: job_id=%s notification_id=%s",
                entity_id,
                job_id,
                payload.notification_id,
            )
            return self.transient_failure(
                error=f"Unexpected error during TTS delivery to {entity_id}: {e}"
            )

    @staticmethod
    async def _acquire_lock_with_timeout(
        lock: asyncio.Lock,
        timeout: float,
    ) -> bool:
        """Acquire *lock* within *timeout* seconds; return True if acquired.

        Handles the narrow race in ``asyncio.timeout`` where the context
        manager's ``__aexit__`` can raise ``TimeoutError`` *after*
        ``lock.acquire()`` already returned (i.e. the lock IS held but
        the deadline fired in ``__aexit__``). In that case the lock is
        released before returning ``False`` so callers never see a
        stranded, permanently-held lock.

        Parameters
        ----------
        lock : asyncio.Lock
            The lock to acquire.
        timeout : float
            Maximum seconds to wait for acquisition.

        Returns
        -------
        bool
            ``True`` if the lock was acquired (caller must release it),
            ``False`` if the timeout expired before acquisition.

        """
        lock_acquired = False
        try:
            async with asyncio.timeout(timeout):
                await lock.acquire()
                # Set INSIDE the context so the flag is True before
                # asyncio.timeout.__aexit__ can raise TimeoutError.
                lock_acquired = True
        except TimeoutError:
            if lock_acquired:
                # Rare race: TimeoutError fired in __aexit__ even though
                # lock.acquire() already returned.  Release the lock;
                # treat this as a genuine timeout from the caller's view.
                lock.release()
            return False
        return True

    async def _safe_restore_volume(self, entity_id: str) -> None:
        """Restore volume, logging errors without re-raising.

        Safe to call from error-handling paths where restoration is best-effort
        and a failure must not mask the original delivery error.

        Parameters
        ----------
        entity_id : str
            Media player entity ID.

        """
        try:
            await self._volume_registry.restore_volume(entity_id)
        except asyncio.CancelledError:
            # Propagate cancellation so task-shutdown is not swallowed.
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to restore volume for %s after delivery error; "
                "manual volume adjustment may be needed",
                entity_id,
                exc_info=True,
            )

    @asynccontextmanager
    async def _apply_and_restore_volume(self, entity_id: str, target_volume: float):
        """Apply volume on entry and auto-restore on any exception inside the block.

        Calls ``apply_volume`` on entry; if it fails the ``TTSVolumeControlError``
        propagates directly and no restoration is needed (volume was never changed).
        Any exception raised inside the ``async with`` block triggers
        ``_safe_restore_volume`` before the exception is re-raised, ensuring
        volume is always restored regardless of where the exception originates.

        Parameters
        ----------
        entity_id : str
            Media player entity ID.
        target_volume : float
            Desired volume level (0.0–1.0).

        Raises
        ------
        TTSVolumeControlError
            If ``apply_volume`` fails (volume was never changed, no restore needed).

        """
        await self._volume_registry.apply_volume(entity_id, target_volume)
        try:
            yield
        except BaseException:
            await self._safe_restore_volume(entity_id)
            raise

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

    def _sanitize_message(self, message: str, entity_id: str = "unknown") -> str:
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

            When ``TTSSettings.ssml_enabled`` is ``True``, :meth:`_speak_message`
            applies ``xml.sax.saxutils.escape()`` to this sanitized text before
            inserting it into the SSML ``<speak>`` document, so
            ``<``, ``>``, and ``&`` in user content become ``&lt;``, ``&gt;``,
            and ``&amp;`` — literal spoken text, not evaluated markup.

            **Do not enable SSML mode with plain-text-only TTS engines.**
            For such engines user content is sent as-is and any ``<tag>``
            would reach the engine un-escaped.
        """
        # Step 1: Remove control characters (non-printable ASCII)
        message = CONTROL_CHAR_PATTERN.sub("", message)

        # Step 2: Enforce maximum length; append ellipsis so listeners know
        # the message was cut short.
        if len(message) > MAX_MESSAGE_LENGTH:
            message = message[: MAX_MESSAGE_LENGTH - 1] + "…"
            _LOGGER.warning(
                "TTS message truncated to %d characters (entity=%s)",
                MAX_MESSAGE_LENGTH,
                entity_id,
            )

        return message

    async def _speak_message(
        self,
        entity_id: str,
        message: str,
        tts_service: str,
        *,
        ssml_enabled: bool = False,
        notification_id: str = "unknown",
        job_id: str = "unknown",
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
        ssml_enabled : bool
            When ``True``, the sanitized message is XML-escaped with
            ``xml.sax.saxutils.escape()`` and wrapped in a well-formed SSML
            ``<speak>`` document before delivery. When ``False`` (default)
            the message is sent as plain text — safe for all engines.
        notification_id : str
            Unique identifier for the notification (used for logging).
        job_id : str
            Job identifier for cross-layer log correlation.

        Notes
        -----
        ``blocking=True`` on ``tts.speak`` resolves after the TTS platform
        has finished its text-to-audio conversion and the resulting audio URL
        has been passed to the media player's ``play_media`` command — not
        after the audio finishes playing, and not necessarily after the player
        enters ``STATE_PLAYING``.  The media player may still be in
        ``STATE_IDLE`` when this call returns; it enters ``STATE_PLAYING`` only
        once the audio actually starts streaming.

        Consequences of this contract:

        * The event-driven ``PLAYING→IDLE`` listener is needed because this
          call does not wait for playback to finish.
        * The fallback timer is needed because the state-change event may
          never fire (e.g. Bluetooth disconnect or a player that skips
          ``STATE_PLAYING`` entirely).

        The ``TTS_SPEAK_TIMEOUT`` therefore guards against a hung
        text-to-audio conversion or an unresponsive media player connection,
        not against a long spoken message.

        Raises
        ------
        HomeAssistantError
            If the ``tts.speak`` call fails at runtime.
        ServiceNotFound
            If the ``tts.speak`` action is unavailable.

        """
        _t0 = time.monotonic()
        _LOGGER.debug(
            "TTS speak initiating: job_id=%s engine=%s player=%s message_len=%d ssml=%s notification_id=%s",
            job_id,
            tts_service,
            entity_id,
            len(message),
            ssml_enabled,
            notification_id,
        )
        # Build the message body: when SSML mode is on, XML-escape the user
        # content so any <tag> or & in the message becomes literal spoken text,
        # then wrap everything in a well-formed <speak> document. When SSML mode
        # is off the message is sent as plain text — safe for all engines.
        if ssml_enabled:
            escaped = xml.sax.saxutils.escape(message)
            tts_message = f"<speak>{escaped}</speak>"
        else:
            tts_message = message
        # HA transport-level errors (e.g. ClientConnectionResetError) surface in
        # homeassistant.components.tts logs, not here — see HA core for full trace.
        async with asyncio.timeout(TTS_SPEAK_TIMEOUT):
            await self._hass.services.async_call(
                domain="tts",
                service="speak",
                service_data={
                    "media_player_entity_id": entity_id,
                    "message": tts_message,
                    "cache": True,
                },
                target={"entity_id": tts_service},
                blocking=True,
            )

        _LOGGER.debug(
            "TTS speak completed: job_id=%s engine=%s player=%s notification_id=%s elapsed_ms=%d",
            job_id,
            tts_service,
            entity_id,
            notification_id,
            int((time.monotonic() - _t0) * 1000),
        )

    async def _fallback_restore(
        self,
        entity_id: str,
        *,
        timeout: float,
        notification_id: str = "unknown",
        job_id: str = "unknown",
    ) -> None:
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
        notification_id : str
            Unique identifier for the notification (used for logging).
        job_id : str
            Job identifier for cross-layer log correlation.

        """
        try:
            await asyncio.sleep(timeout)
            if self._volume_registry.has_active_intent(entity_id):
                _LOGGER.warning(
                    "Fallback volume restore triggered for %s "
                    "— no PLAYING→IDLE event received within %ds job_id=%s notification_id=%s",
                    entity_id,
                    timeout,
                    job_id,
                    notification_id,
                )
                await self._safe_restore_volume(entity_id)
            else:
                _LOGGER.debug(
                    "Fallback timer fired for %s but intent already resolved "
                    "— no restore needed job_id=%s notification_id=%s",
                    entity_id,
                    job_id,
                    notification_id,
                )
        except asyncio.CancelledError:
            pass
