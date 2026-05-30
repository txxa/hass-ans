"""Tests for the PersistentNotification delivery adapter."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)

from custom_components.ans.channels.persistent_notification import (
    PersistentNotificationAdapter,
)
from custom_components.ans.models import DeliveryStatus, RecipientContactInfo

from ..conftest import make_payload

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_adapter() -> tuple[PersistentNotificationAdapter, MagicMock]:
    """Create a PersistentNotificationAdapter with a mocked hass instance."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    return PersistentNotificationAdapter(hass=hass), hass


def _deliver(adapter, payload, **kwargs):
    """Invoke adapter.deliver with sensible defaults, allowing keyword overrides."""
    contact = RecipientContactInfo(email_address=None, phone_number=None)
    defaults = {
        "payload": payload,
        "contact_info": contact,
        "idempotency_key": "pn-key-1",
        "job_id": "job-1",
    }
    defaults.update(kwargs)
    return adapter.deliver(**defaults)


# ── Basic delivery ─────────────────────────────────────────────────────────────


class TestPersistentNotificationDelivery:
    """Verify that the adapter calls the persistent_notification HA service on success."""

    async def test_success_calls_service(self):
        """Successful delivery returns SUCCESS status and invokes async_call once."""
        adapter, hass = _make_adapter()
        result = await _deliver(adapter, make_payload())
        assert result.status == DeliveryStatus.SUCCESS
        hass.services.async_call.assert_awaited_once()

    async def test_service_domain_is_persistent_notification(self):
        """The HA service call must target the 'persistent_notification' domain."""
        adapter, hass = _make_adapter()
        await _deliver(adapter, make_payload())
        call_kwargs = hass.services.async_call.call_args.kwargs
        assert call_kwargs["domain"] == "persistent_notification"

    async def test_service_is_create(self):
        """The HA service name must be 'create'."""
        adapter, hass = _make_adapter()
        await _deliver(adapter, make_payload())
        call_kwargs = hass.services.async_call.call_args.kwargs
        assert call_kwargs["service"] == "create"

    async def test_title_in_service_data(self):
        """The payload title must appear in the service_data sent to HA."""
        adapter, hass = _make_adapter()
        payload = make_payload(title="Test Title")
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert sd["title"] == "Test Title"

    async def test_message_in_service_data(self):
        """The payload message must appear in the 'message' field of service_data."""
        adapter, hass = _make_adapter()
        payload = make_payload(message="Test body")
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert "Test body" in sd["message"]

    async def test_payload_notification_id_used(self):
        """The payload notification_id (ANS UUID) must be used as the HA notification_id.

        This ensures that retries update the existing HA notification rather than
        creating duplicates, and enables acknowledgement correlation (NH-3).
        """
        adapter, hass = _make_adapter()
        payload = make_payload()
        await _deliver(adapter, payload, idempotency_key="some-attempt-key")
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert sd["notification_id"] == str(payload.notification_id)

    async def test_context_appended_to_message(self):
        """Payload context values must be appended to the message body."""
        adapter, hass = _make_adapter()
        payload = make_payload(message="Base body", context={"sensor": "door"})
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert "door" in sd["message"]

    async def test_image_rendered_as_markdown(self):
        """payload.image with an http(s) URL is rendered as a [filename] Markdown link."""
        adapter, hass = _make_adapter()
        payload = make_payload(image="https://example.com/img.jpg")
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert "[img.jpg](https://example.com/img.jpg)" in sd["message"]
        assert "![image]" not in sd["message"]
        assert "[View image]" not in sd["message"]

    async def test_video_rendered_as_markdown_link(self):
        """payload.video is rendered as a [filename] Markdown link in the message."""
        adapter, hass = _make_adapter()
        payload = make_payload(video="https://example.com/clip.mp4")
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert "[clip.mp4](https://example.com/clip.mp4)" in sd["message"]
        assert "[Video]" not in sd["message"]

    async def test_file_rendered_as_markdown_link(self):
        """payload.file is rendered as a [filename] Markdown link in the message."""
        adapter, hass = _make_adapter()
        payload = make_payload(file="https://example.com/doc.pdf")
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert "[doc.pdf](https://example.com/doc.pdf)" in sd["message"]
        assert "[File]" not in sd["message"]

    async def test_link_rendered_as_markdown(self):
        """payload.link is rendered as a [Details] Markdown link in the message."""
        adapter, hass = _make_adapter()
        payload = make_payload(link="https://example.com")
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert "[Details](https://example.com)" in sd["message"]

    async def test_entity_rendered_as_deep_link(self):
        """context value matching a known entity ID is auto-linked in the Context section."""
        adapter, hass = _make_adapter()
        payload = make_payload(context={"entity": "binary_sensor.door"})
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert (
            "- entity: [binary_sensor.door](/history?entity_id=binary_sensor.door)"
            in sd["message"]
        )

    async def test_image_local_path_rendered_as_embed(self):
        """A /local/ path image is rendered inline as a Markdown image embed."""
        adapter, hass = _make_adapter()
        payload = make_payload(image="/local/snapshot.jpg")
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert "![image](/local/snapshot.jpg)" in sd["message"]
        assert "[View image]" not in sd["message"]

    async def test_entity_not_in_ha_states_not_linked(self):
        """A context value that looks like an entity ID but doesn't exist in HA renders as plain text."""
        adapter, hass = _make_adapter()
        hass.states.get.return_value = None
        payload = make_payload(context={"entity": "binary_sensor.door"})
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert "- entity: binary_sensor.door" in sd["message"]
        assert "/history?entity_id=" not in sd["message"]

    async def test_camera_entity_auto_linked_in_context(self):
        """Any context value matching a known entity ID is auto-linked, not just 'entity' key."""
        adapter, hass = _make_adapter()
        payload = make_payload(context={"camera": "camera.front"})
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert (
            "- camera: [camera.front](/history?entity_id=camera.front)" in sd["message"]
        )

    async def test_non_entity_context_value_not_linked(self):
        """Context values that don't match the entity ID pattern render as plain text."""
        adapter, hass = _make_adapter()
        payload = make_payload(context={"zone": "home", "count": "5"})
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert "- zone: home" in sd["message"]
        assert "- count: 5" in sd["message"]
        assert "/history?entity_id=" not in sd["message"]


# ── Error handling ─────────────────────────────────────────────────────────────


class TestPersistentNotificationErrors:
    """Verify that HA service errors are mapped to the correct delivery status."""

    async def test_service_not_found_returns_permanent_failure(self):
        """ServiceNotFound must yield a permanent failure (channel is misconfigured)."""
        adapter, hass = _make_adapter()
        hass.services.async_call.side_effect = ServiceNotFound(
            "persistent_notification", "create"
        )
        result = await _deliver(adapter, make_payload())
        assert result.status == DeliveryStatus.PERMANENT_FAIL

    async def test_ha_error_returns_transient_failure(self):
        """Generic HomeAssistantError must yield a transient failure to allow retries."""
        adapter, hass = _make_adapter()
        hass.services.async_call.side_effect = HomeAssistantError("connection failed")
        result = await _deliver(adapter, make_payload())
        assert result.status == DeliveryStatus.TRANSIENT_FAIL

    async def test_service_validation_error_returns_permanent_failure(self):
        """ServiceValidationError must yield a permanent failure (bad service config)."""
        adapter, hass = _make_adapter()
        hass.services.async_call.side_effect = ServiceValidationError("bad field")
        result = await _deliver(adapter, make_payload())
        assert result.status == DeliveryStatus.PERMANENT_FAIL

    async def test_unexpected_exception_returns_transient_failure(self):
        """An unexpected runtime exception must yield a transient failure."""
        adapter, hass = _make_adapter()
        hass.services.async_call.side_effect = RuntimeError("something broke")
        result = await _deliver(adapter, make_payload())
        assert result.status == DeliveryStatus.TRANSIENT_FAIL


# ── Class API ─────────────────────────────────────────────────────────────────


class TestPersistentNotificationClassAPI:
    """Verify class-level API: channel matching, labelling, and requirements."""

    def test_matches_exact_channel_id(self):
        """matches_channel returns True only for the exact persistent_notification channel ID."""
        assert PersistentNotificationAdapter.matches_channel(
            "notify.persistent_notification"
        )

    def test_not_matches_other_channels(self):
        """matches_channel returns False for any non-persistent-notification channel."""
        assert not PersistentNotificationAdapter.matches_channel("notify.signal")
        assert not PersistentNotificationAdapter.matches_channel("notify.mobile_app_x")

    def test_channel_property(self):
        """The channel property must return the canonical persistent_notification identifier."""
        adapter, _ = _make_adapter()
        assert adapter.channel == "notify.persistent_notification"

    def test_no_contact_requirements(self):
        """Persistent notifications require no email, phone, or HA user contact info."""
        req = PersistentNotificationAdapter.get_requirements()
        assert req["requires_email"] is False
        assert req["requires_phone"] is False
        assert req["requires_ha_user"] is False

    def test_channel_label(self):
        """get_channel_label returns a non-empty human-readable string."""
        label = PersistentNotificationAdapter.get_channel_label(
            "notify.persistent_notification"
        )
        assert isinstance(label, str)
        assert len(label) > 0

    def test_extract_variant_always_none(self):
        """extract_variant always returns None — persistent_notification has no variants."""
        assert (
            PersistentNotificationAdapter.extract_variant(
                "notify.persistent_notification"
            )
            is None
        )


# ── Media URL validation ───────────────────────────────────────────────────────


class TestMediaUrlValidation:
    """Verify that image/video/file URLs without a filename segment are skipped with a warning."""

    async def test_bare_domain_image_url_skipped_with_warning(self, caplog):
        """An image URL with no filename path segment is omitted from the message and a warning is logged."""
        adapter, hass = _make_adapter()
        payload = make_payload(image="https://example.com")
        with caplog.at_level(
            logging.WARNING,
            logger="custom_components.ans.channels.persistent_notification",
        ):
            await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert "https://example.com" not in sd["message"]
        assert "example.com" not in sd["message"] or "![" not in sd["message"]
        assert any("no filename" in r.message for r in caplog.records)
