"""Tests for the MobileApp delivery adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)

from custom_components.ans.channels.mobile_app import MobileAppDeliveryAdapter
from custom_components.ans.models import DeliveryStatus, RecipientContactInfo

from .conftest import make_payload

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_adapter(
    device_id: str = "my_phone",
) -> tuple[MobileAppDeliveryAdapter, MagicMock]:
    """Create a MobileAppDeliveryAdapter with a mocked hass instance."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    return MobileAppDeliveryAdapter(hass=hass, device_id=device_id), hass


def _deliver(adapter, payload, **kwargs):
    """Invoke adapter.deliver with sensible defaults, allowing keyword overrides."""
    contact = RecipientContactInfo(email_address=None, phone_number=None)
    defaults = {
        "payload": payload,
        "contact_info": contact,
        "idempotency_key": "key-1",
        "job_id": "job-1",
    }
    defaults.update(kwargs)
    return adapter.deliver(**defaults)


# ── Basic delivery ─────────────────────────────────────────────────────────────


class TestMobileAppDelivery:
    """Verify that the adapter calls the correct HA notify service on success."""

    async def test_success_calls_service(self):
        """Successful delivery returns SUCCESS status and invokes async_call once."""
        adapter, hass = _make_adapter()
        result = await _deliver(adapter, make_payload())
        assert result.status == DeliveryStatus.SUCCESS
        hass.services.async_call.assert_awaited_once()

    async def test_service_domain_is_notify(self):
        """The HA service call must target the 'notify' domain."""
        adapter, hass = _make_adapter()
        await _deliver(adapter, make_payload())
        call_kwargs = hass.services.async_call.call_args.kwargs
        assert call_kwargs["domain"] == "notify"

    async def test_service_name_matches_device_id(self):
        """Service name must be the 'mobile_app_' prefix followed by the device ID."""
        adapter, hass = _make_adapter(device_id="sm_s911b")
        await _deliver(adapter, make_payload())
        call_kwargs = hass.services.async_call.call_args.kwargs
        assert call_kwargs["service"] == "mobile_app_sm_s911b"

    async def test_title_and_message_in_service_data(self):
        """Both title and message from the payload must be present in service_data."""
        adapter, hass = _make_adapter()
        payload = make_payload(title="My Alert", message="Body text")
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert sd["title"] == "My Alert"
        assert sd["message"] == "Body text"

    async def test_idempotency_key_in_data(self):
        """The idempotency key must be forwarded inside the nested 'data' dict."""
        adapter, hass = _make_adapter()
        await _deliver(adapter, make_payload(), idempotency_key="idem-xyz")
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert sd["data"]["idempotency_key"] == "idem-xyz"

    async def test_metadata_merged_into_data(self):
        """Payload metadata key-value pairs must be merged into the 'data' dict."""
        adapter, hass = _make_adapter()
        payload = make_payload(metadata={"tag": "motion"})
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert sd["data"]["tag"] == "motion"


# ── Error handling ─────────────────────────────────────────────────────────────


class TestMobileAppErrorHandling:
    """Verify that HA service errors are mapped to the correct delivery status."""

    async def test_service_not_found_returns_permanent_failure(self):
        """ServiceNotFound → permanent_failure (not transient — unique behavior)."""
        adapter, hass = _make_adapter()
        hass.services.async_call.side_effect = ServiceNotFound("notify", "mobile_app_x")
        result = await _deliver(adapter, make_payload())
        assert result.status == DeliveryStatus.PERMANENT_FAIL

    async def test_service_validation_error_returns_permanent_failure(self):
        """ServiceValidationError (malformed data) must yield a permanent failure."""
        adapter, hass = _make_adapter()
        hass.services.async_call.side_effect = ServiceValidationError(
            "bad data", translation_domain="notify", translation_key="invalid"
        )
        result = await _deliver(adapter, make_payload())
        assert result.status == DeliveryStatus.PERMANENT_FAIL

    async def test_ha_error_returns_transient_failure(self):
        """Generic HomeAssistantError (e.g. network issue) must yield a transient failure."""
        adapter, hass = _make_adapter()
        hass.services.async_call.side_effect = HomeAssistantError("network error")
        result = await _deliver(adapter, make_payload())
        assert result.status == DeliveryStatus.TRANSIENT_FAIL

    async def test_unexpected_exception_returns_transient_failure(self):
        """Unexpected exceptions (non-HA errors) must also yield a transient failure."""
        adapter, hass = _make_adapter()
        hass.services.async_call.side_effect = RuntimeError("OOM")
        result = await _deliver(adapter, make_payload())
        assert result.status == DeliveryStatus.TRANSIENT_FAIL


# ── Class API ─────────────────────────────────────────────────────────────────


class TestMobileAppClassAPI:
    """Verify class-level API: channel matching, labelling, and requirements."""

    def test_matches_channel_mobile_app_prefix(self):
        """matches_channel returns True for any channel using the mobile_app_ prefix."""
        assert MobileAppDeliveryAdapter.matches_channel("notify.mobile_app_phone")
        assert MobileAppDeliveryAdapter.matches_channel("notify.mobile_app_iphone_15")

    def test_not_matches_other_channels(self):
        """matches_channel returns False for non-mobile-app channel IDs."""
        assert not MobileAppDeliveryAdapter.matches_channel("notify.signal")
        assert not MobileAppDeliveryAdapter.matches_channel("notify.mobile_app")

    def test_get_channel_label_formats_device_name(self):
        """get_channel_label returns a human-readable label derived from the device name."""
        label = MobileAppDeliveryAdapter.get_channel_label("notify.mobile_app_sm_s911b")
        assert "SM S911B" in label or "sm_s911b".replace("_", " ").title() in label

    def test_channel_property_includes_device_id(self):
        """The channel property must embed the device_id in the channel identifier."""
        adapter, _ = _make_adapter(device_id="my_device")
        assert "my_device" in adapter.channel

    def test_requires_ha_user(self):
        """Mobile app delivery requires an HA user but no email or phone contact."""
        req = MobileAppDeliveryAdapter.get_requirements()
        assert req["requires_ha_user"] is True
        assert req["requires_email"] is False
        assert req["requires_phone"] is False

    def test_missing_device_id_raises(self):
        """Constructing the adapter with an empty device_id must raise ValueError."""
        hass = MagicMock()
        with pytest.raises(ValueError):
            MobileAppDeliveryAdapter(hass=hass, device_id="")
