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

from ..conftest import make_payload

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

    def test_extract_variant_returns_device_id(self):
        """extract_variant strips the prefix and returns the device_id suffix."""
        variant = MobileAppDeliveryAdapter.extract_variant("notify.mobile_app_sm_x")
        assert variant == "sm_x"

    def test_extract_variant_non_matching_returns_none(self):
        """extract_variant returns None for channel IDs that don't start with the prefix."""
        assert MobileAppDeliveryAdapter.extract_variant("notify.signal") is None
        assert MobileAppDeliveryAdapter.extract_variant("notify.mobile_app") is None

    def test_channel_property_returns_full_id(self):
        """The channel property returns the full 'notify.mobile_app_<device_id>' string."""
        adapter, _ = _make_adapter(device_id="my_phone")
        assert adapter.channel == "notify.mobile_app_my_phone"

    def test_service_name_strips_notify_prefix(self):
        """service_name returns the channel ID with the 'notify.' prefix removed."""
        adapter, _ = _make_adapter(device_id="my_phone")
        assert adapter.service_name == "mobile_app_my_phone"


# ── Factory ───────────────────────────────────────────────────────────────────


class TestMobileAppFactory:
    """Verify create_factory and the inner _device_factory function."""

    def test_create_factory_empty_device_id_raises(self):
        """The inner _device_factory raises ValueError when device_id is empty."""
        hass = MagicMock()
        factory = MobileAppDeliveryAdapter.create_factory()
        with pytest.raises(ValueError, match="device_id is required"):
            factory.factory_fn(hass, "")

    def test_create_factory_produces_adapter(self):
        """The inner _device_factory creates a MobileAppDeliveryAdapter for a valid device_id."""
        hass = MagicMock()
        factory = MobileAppDeliveryAdapter.create_factory()
        adapter = factory.factory_fn(hass, "test_device")
        assert isinstance(adapter, MobileAppDeliveryAdapter)
        assert adapter.device_id == "test_device"


# ── Actions ─────────────────────────────────────────────────────────────────────────────


class TestMobileAppActions:
    """Verify that payload.actions is forwarded correctly into service_data."""

    async def test_actions_forwarded_when_present(self):
        """Non-empty actions list is placed under service_data['data']['actions']."""
        adapter, hass = _make_adapter()
        actions = [
            {"action": "open", "title": "Open"},
            {"action": "dismiss", "title": "Dismiss"},
        ]
        payload = make_payload(actions=actions)
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert sd["data"]["actions"] == actions

    async def test_empty_actions_not_forwarded(self):
        """Empty actions list must NOT add an 'actions' key to service_data['data']."""
        adapter, hass = _make_adapter()
        payload = make_payload(actions=[])
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert "actions" not in sd["data"]

    async def test_actions_with_no_metadata(self):
        """Actions are added alongside idempotency_key when payload has no metadata."""
        adapter, hass = _make_adapter()
        actions = [{"action": "ok", "title": "OK"}]
        payload = make_payload(metadata={}, actions=actions)
        await _deliver(adapter, payload, idempotency_key="key-x")
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert sd["data"]["idempotency_key"] == "key-x"
        assert sd["data"]["actions"] == actions

    async def test_actions_with_metadata(self):
        """Actions and metadata both appear in service_data['data'] without conflict."""
        adapter, hass = _make_adapter()
        actions = [{"action": "ok", "title": "OK"}]
        payload = make_payload(metadata={"tag": "motion"}, actions=actions)
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert sd["data"]["tag"] == "motion"
        assert sd["data"]["actions"] == actions

    async def test_actions_list_is_a_copy(self):
        """The actions list written to service_data is a copy, not the same object."""
        adapter, hass = _make_adapter()
        actions = [{"action": "ok", "title": "OK"}]
        payload = make_payload(actions=actions)
        await _deliver(adapter, payload)
        sd = hass.services.async_call.call_args.kwargs["service_data"]
        assert sd["data"]["actions"] is not payload.actions
