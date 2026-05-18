"""Tests for the PersistentNotification delivery adapter."""

from __future__ import annotations

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

    async def test_metadata_appended_to_message(self):
        """Payload metadata values must be appended to the message body."""
        adapter, hass = _make_adapter()
        payload = make_payload(message="Base body", metadata={"sensor": "door"})
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert "door" in sd["message"]


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
