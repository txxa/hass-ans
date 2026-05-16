"""Unit tests for TTSMediaPlayerAdapter."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from homeassistant.const import STATE_OFF, STATE_UNAVAILABLE
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)

from ...channels.tts_mediaplayer import (
    MAX_MESSAGE_LENGTH,
    TTSMediaPlayerAdapter,
    _calculate_target_volume,
)
from ...exceptions import TTSVolumeControlError
from ...models.delivery import DeliveryStatus
from ...models.notification import (
    NotificationCriticality,
    NotificationPayload,
    NotificationType,
)
from ...models.recipient import RecipientContactInfo, TTSSettings


def _make_payload(title="Alert", message="Test message") -> NotificationPayload:
    """Build a minimal NotificationPayload for use in TTS adapter tests."""
    return NotificationPayload(
        notification_id=str(uuid4()),
        source="test",
        title=title,
        message=message,
        type=NotificationType.INFO,
        criticality=NotificationCriticality.LOW,
        created_at=datetime.now(UTC),
        metadata={},
    )


def _make_contact() -> RecipientContactInfo:
    """Return a RecipientContactInfo with no email or phone (TTS needs neither)."""
    return RecipientContactInfo(email_address=None, phone_number=None)


def _make_adapter(
    delivery_lock: asyncio.Lock | None = None,
) -> tuple[TTSMediaPlayerAdapter, MagicMock, MagicMock, MagicMock]:
    """Return (adapter, hass, config_repo, volume_registry)."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.states = MagicMock()

    config_repo = MagicMock()
    snapshot = MagicMock()
    snapshot.system_config.tts_service = "tts.piper"
    config_repo.snapshot.return_value = snapshot

    volume_registry = MagicMock()
    volume_registry.apply_volume = AsyncMock()
    volume_registry.restore_volume = AsyncMock()
    volume_registry.cancel_fallback_task = MagicMock()
    volume_registry.set_fallback_task = MagicMock()
    volume_registry.mark_delivery_active = MagicMock()
    volume_registry.mark_delivery_inactive = MagicMock()

    adapter = TTSMediaPlayerAdapter(
        hass=hass,
        entity_name="living_room",
        config_repo=config_repo,
        volume_registry=volume_registry,
        delivery_lock=delivery_lock if delivery_lock is not None else asyncio.Lock(),
    )
    return adapter, hass, config_repo, volume_registry


# ---------------------------------------------------------------------------
# Class API
# ---------------------------------------------------------------------------


def test_matches_channel_true():
    """matches_channel returns True for any media_player entity."""
    assert TTSMediaPlayerAdapter.matches_channel("media_player.living_room") is True


def test_matches_channel_false():
    """matches_channel returns False for non-media-player channel identifiers."""
    assert TTSMediaPlayerAdapter.matches_channel("notify.mobile_app_phone") is False


def test_get_channel_label():
    """get_channel_label converts a snake_case entity name to Title Case."""
    label = TTSMediaPlayerAdapter.get_channel_label("media_player.living_room")
    assert label == "Living Room"


def test_get_channel_label_underscores():
    """get_channel_label correctly converts multi-segment underscored names."""
    label = TTSMediaPlayerAdapter.get_channel_label("media_player.kitchen_speaker")
    assert label == "Kitchen Speaker"


def test_get_requirements_no_contact_info_needed():
    """TTS delivery requires no email, phone, or HA user contact information."""
    req = TTSMediaPlayerAdapter.get_requirements()
    assert req.get("requires_email", False) is False
    assert req.get("requires_phone", False) is False
    assert req.get("requires_ha_user", False) is False


def test_channel_property():
    """The channel property returns the canonical 'media_player.<entity_name>' string."""
    adapter, *_ = _make_adapter()
    assert adapter.channel == "media_player.living_room"


# ---------------------------------------------------------------------------
# Message sanitization
# ---------------------------------------------------------------------------


def test_sanitize_strips_control_chars():
    """_sanitize_message removes C0 control characters and bidi override code points."""
    adapter, *_ = _make_adapter()
    # Embed C0 control character + bidi override
    dirty = "Hello\x00\x1fWorld\u202e"
    result = adapter._sanitize_message(dirty)
    assert result == "HelloWorld"


def test_sanitize_truncates_at_max_length():
    """_sanitize_message truncates messages that exceed MAX_MESSAGE_LENGTH and appends ellipsis."""
    adapter, *_ = _make_adapter()
    long_msg = "A" * (MAX_MESSAGE_LENGTH + 50)
    result = adapter._sanitize_message(long_msg)
    assert len(result) == MAX_MESSAGE_LENGTH
    assert result.endswith("…")


def test_sanitize_short_message_unchanged():
    """_sanitize_message returns short clean messages without modification."""
    adapter, *_ = _make_adapter()
    msg = "Short message"
    assert adapter._sanitize_message(msg) == msg


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def test_format_message_title_and_message():
    """Default format combines title and message with a period separator."""
    adapter, *_ = _make_adapter()
    payload = _make_payload(title="Alert", message="Detected")
    settings = TTSSettings.default()
    result = adapter._format_message(payload, settings)
    assert result == "Alert. Detected"


def test_format_message_message_only():
    """'message_only' format returns just the message body, omitting the title."""
    adapter, *_ = _make_adapter()
    payload = _make_payload(title="Title", message="Body")

    settings = TTSSettings.default()
    settings = replace(settings, message_format="message_only")
    result = adapter._format_message(payload, settings)
    assert result == "Body"


def test_format_message_title_only():
    """'title_only' format returns just the title, omitting the message body."""
    adapter, *_ = _make_adapter()
    payload = _make_payload(title="Title", message="Body")

    settings = TTSSettings.default()
    settings = replace(settings, message_format="title_only")
    result = adapter._format_message(payload, settings)
    assert result == "Title"


def test_format_message_none_settings_uses_default():
    """Passing None for settings falls back to TTSSettings.default() message format."""
    adapter, *_ = _make_adapter()
    payload = _make_payload(title="T", message="M")
    # None settings → uses TTSSettings.default().message_format
    result = adapter._format_message(payload, None)
    # Default is title_and_message
    assert "T" in result and "M" in result


# ---------------------------------------------------------------------------
# Delivery: no TTS service configured
# ---------------------------------------------------------------------------


async def test_no_tts_service_returns_permanent_failure():
    """Absent TTS service in system config must yield a permanent delivery failure."""
    adapter, hass, config_repo, volume_registry = _make_adapter()
    snapshot = MagicMock()
    snapshot.system_config.tts_service = None
    config_repo.snapshot.return_value = snapshot

    # State must be valid (non-None, non-OFF)
    state = MagicMock()
    state.state = "idle"
    hass.states.get.return_value = state

    result = await adapter.deliver(
        payload=_make_payload(),
        contact_info=_make_contact(),
        idempotency_key="key-1",
        job_id="job-1",
    )
    assert result.status == DeliveryStatus.PERMANENT_FAIL


# ---------------------------------------------------------------------------
# Delivery: media player state checks
# ---------------------------------------------------------------------------


async def test_media_player_not_found_returns_permanent_failure():
    """A non-existent media player entity must yield a permanent failure."""
    adapter, hass, config_repo, volume_registry = _make_adapter()
    hass.states.get.return_value = None

    result = await adapter.deliver(
        payload=_make_payload(),
        contact_info=_make_contact(),
        idempotency_key="key-2",
        job_id="job-2",
    )
    assert result.status == DeliveryStatus.PERMANENT_FAIL
    volume_registry.apply_volume.assert_not_called()


async def test_media_player_off_returns_transient_failure():
    """A media player in STATE_OFF must request turn-on and yield a transient failure."""
    adapter, hass, _, volume_registry = _make_adapter()
    state = MagicMock()
    state.state = STATE_OFF
    hass.states.get.return_value = state

    result = await adapter.deliver(
        payload=_make_payload(),
        contact_info=_make_contact(),
        idempotency_key="key-3",
        job_id="job-3",
    )
    assert result.status == DeliveryStatus.TRANSIENT_FAIL
    # Verify that turn_on was requested so the device wakes up before retry
    hass.services.async_call.assert_called_once()
    call_kwargs = hass.services.async_call.call_args.kwargs
    assert call_kwargs["domain"] == "media_player"
    assert call_kwargs["service"] == "turn_on"
    assert call_kwargs["service_data"] == {"entity_id": "media_player.living_room"}


async def test_media_player_off_turn_on_error_still_returns_transient_failure():
    """If turn-on request fails, delivery still returns transient failure without raising."""
    adapter, hass, _, volume_registry = _make_adapter()
    state = MagicMock()
    state.state = STATE_OFF
    hass.states.get.return_value = state
    hass.services.async_call.side_effect = Exception("connection error")

    result = await adapter.deliver(
        payload=_make_payload(),
        contact_info=_make_contact(),
        idempotency_key="key-3b",
        job_id="job-3b",
    )
    assert result.status == DeliveryStatus.TRANSIENT_FAIL


async def test_media_player_unavailable_returns_transient_failure():
    """A media player in STATE_UNAVAILABLE must yield a transient failure."""
    adapter, hass, _, volume_registry = _make_adapter()
    state = MagicMock()
    state.state = STATE_UNAVAILABLE
    hass.states.get.return_value = state

    result = await adapter.deliver(
        payload=_make_payload(),
        contact_info=_make_contact(),
        idempotency_key="key-4",
        job_id="job-4",
    )
    assert result.status == DeliveryStatus.TRANSIENT_FAIL


# ---------------------------------------------------------------------------
# Delivery: TTS speak errors
# ---------------------------------------------------------------------------


async def test_service_not_found_returns_permanent_failure():
    """ServiceNotFound raised by the TTS speak call must yield a permanent failure."""
    adapter, hass, _, volume_registry = _make_adapter()
    state = MagicMock()
    state.state = "idle"
    hass.states.get.return_value = state
    hass.services.async_call.side_effect = ServiceNotFound("tts", "speak")

    result = await adapter.deliver(
        payload=_make_payload(),
        contact_info=_make_contact(),
        idempotency_key="key-5",
        job_id="job-5",
    )
    assert result.status == DeliveryStatus.PERMANENT_FAIL
    volume_registry.restore_volume.assert_called_once()


async def test_service_validation_error_returns_permanent_failure():
    """ServiceValidationError from the TTS service must yield a permanent failure."""
    adapter, hass, _, volume_registry = _make_adapter()
    state = MagicMock()
    state.state = "idle"
    hass.states.get.return_value = state
    hass.services.async_call.side_effect = ServiceValidationError("bad config")

    result = await adapter.deliver(
        payload=_make_payload(),
        contact_info=_make_contact(),
        idempotency_key="key-6",
        job_id="job-6",
    )
    assert result.status == DeliveryStatus.PERMANENT_FAIL
    volume_registry.restore_volume.assert_called_once()


async def test_ha_error_returns_transient_failure():
    """Generic HomeAssistantError from the TTS speak call must yield a transient failure."""
    adapter, hass, _, volume_registry = _make_adapter()
    state = MagicMock()
    state.state = "idle"
    hass.states.get.return_value = state
    hass.services.async_call.side_effect = HomeAssistantError("oops")

    result = await adapter.deliver(
        payload=_make_payload(),
        contact_info=_make_contact(),
        idempotency_key="key-7",
        job_id="job-7",
    )
    assert result.status == DeliveryStatus.TRANSIENT_FAIL
    volume_registry.restore_volume.assert_called_once()


# ---------------------------------------------------------------------------
# Delivery: success
# ---------------------------------------------------------------------------


async def test_successful_delivery():
    """Full deliver() success path returns SUCCESS and applies volume."""
    adapter, hass, _, volume_registry = _make_adapter()
    state = MagicMock()
    state.state = "idle"
    hass.states.get.return_value = state
    hass.services.async_call = AsyncMock()

    result = await adapter.deliver(
        payload=_make_payload(),
        contact_info=_make_contact(),
        idempotency_key="key-8",
        job_id="job-8",
    )
    assert result.status == DeliveryStatus.SUCCESS
    volume_registry.apply_volume.assert_called_once()
    volume_registry.set_fallback_task.assert_called_once()


async def test_successful_delivery_calls_tts_service():
    """On success, the adapter must invoke tts.speak with correct domain and service name."""
    adapter, hass, _, volume_registry = _make_adapter()
    state = MagicMock()
    state.state = "idle"
    hass.states.get.return_value = state

    await adapter.deliver(
        payload=_make_payload(title="T", message="M"),
        contact_info=_make_contact(),
        idempotency_key="key-9",
        job_id="job-9",
    )

    hass.services.async_call.assert_called_once()
    call_kwargs = hass.services.async_call.call_args.kwargs
    assert call_kwargs["domain"] == "tts"
    assert call_kwargs["service"] == "speak"


# ---------------------------------------------------------------------------
# Delivery: lock timeout
# ---------------------------------------------------------------------------


async def test_lock_timeout_returns_transient_failure():
    """Failing to acquire the delivery lock within the timeout yields a transient failure."""
    # Provide a pre-acquired lock and set a tiny timeout so the test runs fast
    blocked_lock = asyncio.Lock()
    await blocked_lock.acquire()  # Lock is held; adapter can't acquire it
    adapter, hass, _, _ = _make_adapter(delivery_lock=blocked_lock)

    state = MagicMock()
    state.state = "idle"
    hass.states.get.return_value = state

    with patch(
        "custom_components.ans.channels.tts_mediaplayer.DELIVERY_LOCK_TIMEOUT",
        0.01,
    ):
        result = await adapter.deliver(
            payload=_make_payload(),
            contact_info=_make_contact(),
            idempotency_key="key-lock",
            job_id="job-lock",
        )
    assert result.status == DeliveryStatus.TRANSIENT_FAIL
    assert "lock" in result.error.lower() or "timeout" in result.error.lower()


# ---------------------------------------------------------------------------
# _speak_message: SSML mode
# ---------------------------------------------------------------------------


async def test_speak_message_ssml_mode_wraps_and_escapes():
    """SSML enabled: message is XML-escaped and wrapped in <speak>.</speak>."""
    adapter, hass, *_ = _make_adapter()

    await adapter._speak_message(
        "media_player.living_room",
        'Hello <world> & "you"',
        "tts.piper",
        ssml_enabled=True,
    )

    hass.services.async_call.assert_called_once()
    sent_message = hass.services.async_call.call_args.kwargs["service_data"]["message"]
    assert sent_message == '<speak>Hello &lt;world&gt; &amp; "you"</speak>'


async def test_speak_message_ssml_disabled_sends_plain_text():
    """SSML disabled: raw sanitized message is sent with no SSML wrapper."""
    adapter, hass, *_ = _make_adapter()

    await adapter._speak_message(
        "media_player.living_room",
        "Hello world",
        "tts.piper",
        ssml_enabled=False,
    )

    hass.services.async_call.assert_called_once()
    sent_message = hass.services.async_call.call_args.kwargs["service_data"]["message"]
    assert sent_message == "Hello world"


async def test_successful_delivery_with_ssml_enabled():
    """Full deliver() path with ssml_enabled=True produces SUCCESS and correct SSML body."""
    adapter, hass, config_repo, volume_registry = _make_adapter()
    state = MagicMock()
    state.state = "idle"
    hass.states.get.return_value = state

    from ...channels.base import TTSDeliveryOptions  # noqa: PLC0415

    settings = replace(
        TTSSettings.default(),
        ssml_enabled=True,
        message_format="message_only",
    )

    result = await adapter.deliver(
        payload=_make_payload(title="T", message="Test <msg> & more"),
        contact_info=_make_contact(),
        idempotency_key="key-ssml",
        job_id="job-ssml",
        options=TTSDeliveryOptions(tts_settings=settings),
    )

    assert result.status == DeliveryStatus.SUCCESS
    hass.services.async_call.assert_called_once()
    sent_message = hass.services.async_call.call_args.kwargs["service_data"]["message"]
    assert sent_message == "<speak>Test &lt;msg&gt; &amp; more</speak>"


# ---------------------------------------------------------------------------
# _calculate_target_volume module-level function
# ---------------------------------------------------------------------------


class TestCalculateTargetVolume:
    """Unit tests for the _calculate_target_volume standalone helper."""

    def test_criticality_override_takes_priority(self):
        """When criticality is in the override list the override level is used."""
        settings = replace(
            TTSSettings.default(),
            volume_override_criticalities=[NotificationCriticality.HIGH.value],
            volume_override_level=90,
        )
        result = _calculate_target_volume(NotificationCriticality.HIGH, settings)
        assert abs(result - 0.90) < 0.001

    def test_time_based_morning_volume(self):
        """Hour 7 (morning) uses volume_morning."""
        settings = replace(TTSSettings.default(), volume_morning=40, volume_daytime=60)
        with patch(
            "custom_components.ans.channels.tts_mediaplayer.dt_util.now"
        ) as mock_now:
            mock_now.return_value.hour = 7
            result = _calculate_target_volume(NotificationCriticality.LOW, settings)
        assert abs(result - 0.40) < 0.001

    def test_time_based_evening_volume(self):
        """Hour 20 (evening) uses volume_evening."""
        settings = replace(TTSSettings.default(), volume_evening=25)
        with patch(
            "custom_components.ans.channels.tts_mediaplayer.dt_util.now"
        ) as mock_now:
            mock_now.return_value.hour = 20
            result = _calculate_target_volume(NotificationCriticality.LOW, settings)
        assert abs(result - 0.25) < 0.001

    def test_time_based_night_volume(self):
        """Hour 23 (night) uses volume_night."""
        settings = replace(TTSSettings.default(), volume_night=15)
        with patch(
            "custom_components.ans.channels.tts_mediaplayer.dt_util.now"
        ) as mock_now:
            mock_now.return_value.hour = 23
            result = _calculate_target_volume(NotificationCriticality.LOW, settings)
        assert abs(result - 0.15) < 0.001

    def test_no_tts_settings_falls_back_to_defaults(self):
        """Passing tts_settings=None uses TTSSettings.default() values."""
        with patch(
            "custom_components.ans.channels.tts_mediaplayer.dt_util.now"
        ) as mock_now:
            mock_now.return_value.hour = 12  # daytime
            result = _calculate_target_volume(NotificationCriticality.LOW, None)
        assert 0.0 <= result <= 1.0


# ---------------------------------------------------------------------------
# Additional class API coverage
# ---------------------------------------------------------------------------


def test_extract_variant_matching():
    """extract_variant strips the 'media_player.' prefix and returns the entity name."""
    assert TTSMediaPlayerAdapter.extract_variant("media_player.kitchen") == "kitchen"


def test_extract_variant_non_matching():
    """extract_variant returns None for non-media_player channel IDs."""
    assert TTSMediaPlayerAdapter.extract_variant("notify.signal") is None
    assert TTSMediaPlayerAdapter.extract_variant("notify.mobile_app_x") is None


def test_create_factory_requires_deps():
    """create_factory raises ValueError when deps is None."""
    import pytest  # noqa: PLC0415

    with pytest.raises(ValueError, match="requires deps"):
        TTSMediaPlayerAdapter.create_factory(deps=None)


# ---------------------------------------------------------------------------
# Additional delivery error paths
# ---------------------------------------------------------------------------


async def test_unknown_message_format_returns_permanent_failure():
    """An invalid message_format in TTSSettings yields a permanent delivery failure."""
    adapter, hass, config_repo, _ = _make_adapter()
    state = MagicMock()
    state.state = "idle"
    state.attributes = {"volume_level": 0.5}
    hass.states.get.return_value = state

    # Bypass TTSSettings.__post_init__ validation by patching _format_message directly
    from ...exceptions import TTSDeliveryError  # noqa: PLC0415

    original_format = adapter._format_message

    def _bad_format(payload, tts_settings):
        raise TTSDeliveryError("Unknown message_format: 'bogus'", is_permanent=True)

    adapter._format_message = _bad_format

    result = await adapter.deliver(
        payload=_make_payload(),
        contact_info=_make_contact(),
        idempotency_key="key-fmt",
        job_id="job-fmt",
    )
    assert result.status == DeliveryStatus.PERMANENT_FAIL
    adapter._format_message = original_format


async def test_tts_volume_control_error_returns_transient_failure():
    """A TTSVolumeControlError from apply_volume yields a transient failure."""
    adapter, hass, config_repo, volume_registry = _make_adapter()
    state = MagicMock()
    state.state = "idle"
    state.attributes = {"volume_level": 0.1}  # far from default target → change needed
    hass.states.get.return_value = state

    volume_registry.apply_volume.side_effect = TTSVolumeControlError(
        "set volume failed"
    )

    result = await adapter.deliver(
        payload=_make_payload(),
        contact_info=_make_contact(),
        idempotency_key="key-vce",
        job_id="job-vce",
    )
    assert result.status == DeliveryStatus.TRANSIENT_FAIL


async def test_tts_speak_timeout_returns_transient_failure():
    """TimeoutError raised by the TTS speak call yields a transient failure."""
    adapter, hass, _, volume_registry = _make_adapter()
    state = MagicMock()
    state.state = "idle"
    state.attributes = {"volume_level": 0.5}
    hass.states.get.return_value = state
    hass.services.async_call.side_effect = TimeoutError()

    result = await adapter.deliver(
        payload=_make_payload(),
        contact_info=_make_contact(),
        idempotency_key="key-to",
        job_id="job-to",
    )
    assert result.status == DeliveryStatus.TRANSIENT_FAIL


async def test_unexpected_tts_exception_returns_transient_failure():
    """An unexpected exception during TTS delivery yields a transient failure."""
    adapter, hass, _, volume_registry = _make_adapter()
    state = MagicMock()
    state.state = "idle"
    state.attributes = {"volume_level": 0.5}
    hass.states.get.return_value = state
    hass.services.async_call.side_effect = RuntimeError("unexpected crash")

    result = await adapter.deliver(
        payload=_make_payload(),
        contact_info=_make_contact(),
        idempotency_key="key-ux",
        job_id="job-ux",
    )
    assert result.status == DeliveryStatus.TRANSIENT_FAIL


# ---------------------------------------------------------------------------
# Volume management disabled / already-at-target paths
# ---------------------------------------------------------------------------


async def test_volume_management_disabled_skips_volume():
    """volume_management_enabled=False means apply_volume is never called."""
    adapter, hass, config_repo, volume_registry = _make_adapter()
    state = MagicMock()
    state.state = "idle"
    state.attributes = {
        "volume_level": 0.1
    }  # far from target — would change if enabled
    hass.states.get.return_value = state

    settings = replace(TTSSettings.default(), volume_management_enabled=False)

    from ...channels.base import TTSDeliveryOptions  # noqa: PLC0415

    result = await adapter.deliver(
        payload=_make_payload(),
        contact_info=_make_contact(),
        idempotency_key="key-vmd",
        job_id="job-vmd",
        options=TTSDeliveryOptions(tts_settings=settings),
    )

    assert result.status == DeliveryStatus.SUCCESS
    volume_registry.apply_volume.assert_not_called()


# ---------------------------------------------------------------------------
# Post-speak IDLE scheduling and fallback task paths
# ---------------------------------------------------------------------------


async def test_post_speak_idle_schedules_idle_restore():
    """When the player is IDLE after tts.speak, schedule_idle_restore is called."""
    adapter, hass, _, volume_registry = _make_adapter()

    # Pre-speak state: idle with volume far from target (so volume change is triggered)
    pre_state = MagicMock()
    pre_state.state = "idle"
    pre_state.attributes = {"volume_level": 0.1}

    # Post-speak state: still idle (tts.speak returned with player already idle)
    post_state = MagicMock()
    post_state.state = "idle"

    volume_registry.has_active_intent.return_value = True
    volume_registry.schedule_idle_restore = MagicMock()

    # Return pre_state on first call, post_state on subsequent calls
    hass.states.get.side_effect = [pre_state, post_state]

    result = await adapter.deliver(
        payload=_make_payload(),
        contact_info=_make_contact(),
        idempotency_key="key-idle",
        job_id="job-idle",
    )

    assert result.status == DeliveryStatus.SUCCESS
    volume_registry.schedule_idle_restore.assert_called_once_with(
        "media_player.living_room"
    )


async def test_no_active_intent_skips_fallback_registration():
    """When has_active_intent returns False, set_fallback_task is not called."""
    adapter, hass, _, volume_registry = _make_adapter()
    state = MagicMock()
    state.state = "idle"
    state.attributes = {"volume_level": 0.5}
    hass.states.get.return_value = state

    volume_registry.has_active_intent.return_value = False

    result = await adapter.deliver(
        payload=_make_payload(),
        contact_info=_make_contact(),
        idempotency_key="key-nai",
        job_id="job-nai",
    )

    assert result.status == DeliveryStatus.SUCCESS
    volume_registry.set_fallback_task.assert_not_called()
