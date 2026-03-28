"""Tests for the PersistentNotification delivery adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

from custom_components.ans.channels.persistent_notification import (
    PersistentNotificationAdapter,
)
from custom_components.ans.models import DeliveryStatus, RecipientContactInfo

from .conftest import make_payload

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_adapter() -> tuple[PersistentNotificationAdapter, MagicMock]:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    return PersistentNotificationAdapter(hass=hass), hass


def _deliver(adapter, payload, **kwargs):
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
    async def test_success_calls_service(self):
        adapter, hass = _make_adapter()
        result = await _deliver(adapter, make_payload())
        assert result.status == DeliveryStatus.SUCCESS
        hass.services.async_call.assert_awaited_once()

    async def test_service_domain_is_persistent_notification(self):
        adapter, hass = _make_adapter()
        await _deliver(adapter, make_payload())
        call_kwargs = hass.services.async_call.call_args.kwargs
        assert call_kwargs["domain"] == "persistent_notification"

    async def test_service_is_create(self):
        adapter, hass = _make_adapter()
        await _deliver(adapter, make_payload())
        call_kwargs = hass.services.async_call.call_args.kwargs
        assert call_kwargs["service"] == "create"

    async def test_title_in_service_data(self):
        adapter, hass = _make_adapter()
        payload = make_payload(title="Test Title")
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert sd["title"] == "Test Title"

    async def test_message_in_service_data(self):
        adapter, hass = _make_adapter()
        payload = make_payload(message="Test body")
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert "Test body" in sd["message"]

    async def test_idempotency_key_as_notification_id(self):
        adapter, hass = _make_adapter()
        await _deliver(adapter, make_payload(), idempotency_key="unique-idem")
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert sd["notification_id"] == "unique-idem"

    async def test_metadata_appended_to_message(self):
        adapter, hass = _make_adapter()
        payload = make_payload(message="Base body", metadata={"sensor": "door"})
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert "door" in sd["message"]


# ── Error handling ─────────────────────────────────────────────────────────────


class TestPersistentNotificationErrors:
    async def test_service_not_found_returns_permanent_failure(self):
        adapter, hass = _make_adapter()
        hass.services.async_call.side_effect = ServiceNotFound(
            "persistent_notification", "create"
        )
        result = await _deliver(adapter, make_payload())
        assert result.status == DeliveryStatus.PERMANENT_FAIL

    async def test_ha_error_returns_transient_failure(self):
        adapter, hass = _make_adapter()
        hass.services.async_call.side_effect = HomeAssistantError("connection failed")
        result = await _deliver(adapter, make_payload())
        assert result.status == DeliveryStatus.TRANSIENT_FAIL


# ── Class API ─────────────────────────────────────────────────────────────────


class TestPersistentNotificationClassAPI:
    def test_matches_exact_channel_id(self):
        assert PersistentNotificationAdapter.matches_channel(
            "notify.persistent_notification"
        )

    def test_not_matches_other_channels(self):
        assert not PersistentNotificationAdapter.matches_channel("notify.signal")
        assert not PersistentNotificationAdapter.matches_channel("notify.mobile_app_x")

    def test_channel_property(self):
        adapter, _ = _make_adapter()
        assert adapter.channel == "notify.persistent_notification"

    def test_no_contact_requirements(self):
        req = PersistentNotificationAdapter.get_requirements()
        assert req["requires_email"] is False
        assert req["requires_phone"] is False
        assert req["requires_ha_user"] is False

    def test_channel_label(self):
        label = PersistentNotificationAdapter.get_channel_label(
            "notify.persistent_notification"
        )
        assert isinstance(label, str)
        assert len(label) > 0
