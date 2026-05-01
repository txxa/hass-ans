"""Unit tests for the Signal delivery adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)

from ..channels.signal import SignalDeliveryAdapter
from ..models.delivery import DeliveryStatus
from ..models.notification import (
    NotificationCriticality,
    NotificationPayload,
    NotificationType,
)
from ..models.recipient import RecipientContactInfo


def _make_payload(
    title: str = "",
    message: str = "Hello world",
    metadata: dict | None = None,
) -> NotificationPayload:
    """Build a minimal NotificationPayload for use in signal adapter tests."""
    return NotificationPayload(
        notification_id=str(uuid4()),
        source="test",
        title=title,
        message=message,
        type=NotificationType.INFO,
        criticality=NotificationCriticality.LOW,
        created_at=datetime.now(UTC),
        metadata=metadata or {},
    )


def _make_contact(phone: str | None = "+49123456789") -> RecipientContactInfo:
    """Build a RecipientContactInfo with an optional phone number."""
    return RecipientContactInfo(email_address=None, phone_number=phone)


def _make_adapter() -> tuple[SignalDeliveryAdapter, MagicMock]:
    """Create a SignalDeliveryAdapter with a fully mocked hass instance."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    adapter = SignalDeliveryAdapter(hass=hass)
    return adapter, hass


# ---------------------------------------------------------------------------
# Title formatting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_title_auto_styled_bold():
    """Title present, no explicit text_mode → styled mode with bold title."""
    adapter, hass = _make_adapter()
    payload = _make_payload(title="Motion Alert", message="Detected in living room")

    result = await adapter.deliver(
        payload=payload,
        contact_info=_make_contact(),
        idempotency_key="k1",
        job_id="job-1",
    )

    assert result.status == DeliveryStatus.SUCCESS
    call_kwargs = hass.services.async_call.call_args.kwargs
    service_data = call_kwargs["service_data"]

    assert service_data["message"] == "**Motion Alert**\n\nDetected in living room"
    assert service_data["data"]["text_mode"] == "styled"


@pytest.mark.asyncio
async def test_title_explicit_styled_bold():
    """Title present, explicit text_mode='styled' → bold title."""
    adapter, hass = _make_adapter()
    payload = _make_payload(
        title="Alert",
        message="Body text",
        metadata={"text_mode": "styled"},
    )

    result = await adapter.deliver(
        payload=payload,
        contact_info=_make_contact(),
        idempotency_key="k2",
        job_id="job-1",
    )

    assert result.status == DeliveryStatus.SUCCESS
    service_data = hass.services.async_call.call_args.kwargs["service_data"]
    assert service_data["message"] == "**Alert**\n\nBody text"
    assert service_data["data"]["text_mode"] == "styled"


@pytest.mark.asyncio
async def test_title_explicit_normal_no_bold():
    """Title present but text_mode='normal' overrides auto-styled → plain title."""
    adapter, hass = _make_adapter()
    payload = _make_payload(
        title="Alert",
        message="Body text",
        metadata={"text_mode": "normal"},
    )

    result = await adapter.deliver(
        payload=payload,
        contact_info=_make_contact(),
        idempotency_key="k3",
        job_id="job-1",
    )

    assert result.status == DeliveryStatus.SUCCESS
    service_data = hass.services.async_call.call_args.kwargs["service_data"]
    assert service_data["message"] == "Alert\n\nBody text"
    assert service_data["data"]["text_mode"] == "normal"


@pytest.mark.asyncio
async def test_no_title_no_metadata_normal_mode():
    """No title, no metadata → normal mode, message unchanged."""
    adapter, hass = _make_adapter()
    payload = _make_payload(title="", message="Plain message")

    result = await adapter.deliver(
        payload=payload,
        contact_info=_make_contact(),
        idempotency_key="k4",
        job_id="job-1",
    )

    assert result.status == DeliveryStatus.SUCCESS
    service_data = hass.services.async_call.call_args.kwargs["service_data"]
    assert service_data["message"] == "Plain message"
    assert service_data["data"]["text_mode"] == "normal"


@pytest.mark.asyncio
async def test_no_title_with_metadata_normal_mode():
    """No title, explicit text_mode='normal' → plain message."""
    adapter, hass = _make_adapter()
    payload = _make_payload(
        title="",
        message="Body only",
        metadata={"text_mode": "normal"},
    )

    result = await adapter.deliver(
        payload=payload,
        contact_info=_make_contact(),
        idempotency_key="k5",
        job_id="job-1",
    )

    assert result.status == DeliveryStatus.SUCCESS
    service_data = hass.services.async_call.call_args.kwargs["service_data"]
    assert service_data["message"] == "Body only"
    assert service_data["data"]["text_mode"] == "normal"


# ---------------------------------------------------------------------------
# text_mode validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_text_mode_falls_back_to_normal(caplog):
    """Invalid text_mode value logs a warning and falls back to 'normal'."""
    adapter, hass = _make_adapter()
    payload = _make_payload(
        title="",
        message="Test",
        metadata={"text_mode": "fancy"},
    )

    result = await adapter.deliver(
        payload=payload,
        contact_info=_make_contact(),
        idempotency_key="k6",
        job_id="job-1",
    )

    assert result.status == DeliveryStatus.SUCCESS
    service_data = hass.services.async_call.call_args.kwargs["service_data"]
    assert service_data["data"]["text_mode"] == "normal"
    assert "Invalid text_mode" in caplog.text


# ---------------------------------------------------------------------------
# Missing phone number
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_phone_permanent_failure():
    """No phone number → permanent failure without calling HA service."""
    adapter, hass = _make_adapter()
    payload = _make_payload(title="Hi", message="There")

    result = await adapter.deliver(
        payload=payload,
        contact_info=_make_contact(phone=None),
        idempotency_key="k7",
        job_id="job-1",
    )

    assert result.status == DeliveryStatus.PERMANENT_FAIL
    hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# Metadata passthrough (attachments, urls, verify_ssl)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attachments_and_urls_passed_through():
    """Attachments and urls in metadata are forwarded to the service call."""
    adapter, hass = _make_adapter()
    payload = _make_payload(
        title="",
        message="See attached",
        metadata={
            "text_mode": "normal",
            "attachments": ["/config/www/image.jpg"],
            "urls": ["http://example.com/pic.jpg"],
            "verify_ssl": False,
        },
    )

    result = await adapter.deliver(
        payload=payload,
        contact_info=_make_contact(),
        idempotency_key="k8",
        job_id="job-1",
    )

    assert result.status == DeliveryStatus.SUCCESS
    data = hass.services.async_call.call_args.kwargs["service_data"]["data"]
    assert data["attachments"] == ["/config/www/image.jpg"]
    assert data["urls"] == ["http://example.com/pic.jpg"]
    assert data["verify_ssl"] is False


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_not_found_returns_permanent_failure():
    """ServiceNotFound from the signal service must yield a permanent failure."""
    adapter, hass = _make_adapter()
    hass.services.async_call.side_effect = ServiceNotFound("notify", "signal")

    result = await adapter.deliver(
        payload=_make_payload(title="Hi", message="Body"),
        contact_info=_make_contact(),
        idempotency_key="k-snf",
        job_id="job-err",
    )
    assert result.status == DeliveryStatus.PERMANENT_FAIL


@pytest.mark.asyncio
async def test_service_validation_error_returns_permanent_failure():
    """ServiceValidationError from the signal service must yield a permanent failure."""
    adapter, hass = _make_adapter()
    hass.services.async_call.side_effect = ServiceValidationError("bad payload")

    result = await adapter.deliver(
        payload=_make_payload(title="Hi", message="Body"),
        contact_info=_make_contact(),
        idempotency_key="k-sve",
        job_id="job-err",
    )
    assert result.status == DeliveryStatus.PERMANENT_FAIL


@pytest.mark.asyncio
async def test_ha_error_returns_transient_failure():
    """Generic HomeAssistantError from the signal service must yield a transient failure."""
    adapter, hass = _make_adapter()
    hass.services.async_call.side_effect = HomeAssistantError("temporary outage")

    result = await adapter.deliver(
        payload=_make_payload(title="Hi", message="Body"),
        contact_info=_make_contact(),
        idempotency_key="k-hae",
        job_id="job-err",
    )
    assert result.status == DeliveryStatus.TRANSIENT_FAIL


@pytest.mark.asyncio
async def test_unexpected_exception_returns_transient_failure():
    """An unexpected runtime exception from the signal service yields a transient failure."""
    adapter, hass = _make_adapter()
    hass.services.async_call.side_effect = RuntimeError("unexpected crash")

    result = await adapter.deliver(
        payload=_make_payload(title="Hi", message="Body"),
        contact_info=_make_contact(),
        idempotency_key="k-ux",
        job_id="job-err",
    )
    assert result.status == DeliveryStatus.TRANSIENT_FAIL


# ---------------------------------------------------------------------------
# Class API
# ---------------------------------------------------------------------------


class TestSignalClassAPI:
    """Verify class-level API: channel matching, properties, labels, requirements."""

    def test_matches_channel_returns_true(self):
        """matches_channel returns True for the exact 'notify.signal' channel ID."""
        assert SignalDeliveryAdapter.matches_channel("notify.signal") is True

    def test_matches_channel_returns_false_for_others(self):
        """matches_channel returns False for any other channel ID."""
        assert SignalDeliveryAdapter.matches_channel("notify.mobile_app_x") is False
        assert (
            SignalDeliveryAdapter.matches_channel("notify.persistent_notification")
            is False
        )

    def test_extract_variant_always_none(self):
        """extract_variant always returns None — Signal has no channel variants."""
        assert SignalDeliveryAdapter.extract_variant("notify.signal") is None
        assert SignalDeliveryAdapter.extract_variant("notify.other") is None

    def test_channel_property(self):
        """The channel property returns the canonical 'notify.signal' identifier."""
        adapter, _ = _make_adapter()
        assert adapter.channel == "notify.signal"

    def test_service_name_property(self):
        """service_name strips the 'notify.' prefix from the channel ID."""
        adapter, _ = _make_adapter()
        assert adapter.service_name == "signal"

    def test_get_channel_label(self):
        """get_channel_label returns the human-friendly 'Signal Messenger' string."""
        label = SignalDeliveryAdapter.get_channel_label("notify.signal")
        assert label == "Signal Messenger"

    def test_requires_phone(self):
        """Signal delivery requires a phone number."""
        req = SignalDeliveryAdapter.get_requirements()
        assert req["requires_phone"] is True
        assert req.get("requires_email", False) is False
        assert req.get("requires_ha_user", False) is False


# ---------------------------------------------------------------------------
# _mask_phone helper
# ---------------------------------------------------------------------------


class TestMaskPhone:
    """Unit tests for the _mask_phone module-level helper."""

    def test_mask_phone_shows_last_four(self):
        """Numbers with 4+ digits show the last 4 digits after '****'."""
        from ..channels.signal import _mask_phone  # noqa: PLC0415

        assert _mask_phone("+49123456789") == "****6789"
        assert _mask_phone("1234") == "****1234"

    def test_mask_phone_short_number(self):
        """Numbers shorter than 4 digits return '****' with no digits shown."""
        from ..channels.signal import _mask_phone  # noqa: PLC0415

        assert _mask_phone("123") == "****"
        assert _mask_phone("") == "****"


# ---------------------------------------------------------------------------
# Metadata validation warnings
# ---------------------------------------------------------------------------


class TestSignalMetadataValidation:
    """Verify that invalid metadata field types log warnings and are silently dropped."""

    @pytest.mark.asyncio
    async def test_attachments_non_list_logs_warning(self, caplog):
        """Non-list 'attachments' value logs a warning and is not forwarded."""
        adapter, hass = _make_adapter()
        payload = _make_payload(
            title="",
            message="Test",
            metadata={"attachments": "/single/path.jpg"},
        )

        result = await adapter.deliver(
            payload=payload,
            contact_info=_make_contact(),
            idempotency_key="k-av",
            job_id="job-val",
        )

        assert result.status == DeliveryStatus.SUCCESS
        assert "attachments must be a list" in caplog.text
        data = hass.services.async_call.call_args.kwargs["service_data"].get("data", {})
        assert "attachments" not in data

    @pytest.mark.asyncio
    async def test_urls_non_list_logs_warning(self, caplog):
        """Non-list 'urls' value logs a warning and is not forwarded."""
        adapter, hass = _make_adapter()
        payload = _make_payload(
            title="",
            message="Test",
            metadata={"urls": "http://example.com/img.jpg"},
        )

        result = await adapter.deliver(
            payload=payload,
            contact_info=_make_contact(),
            idempotency_key="k-uv",
            job_id="job-val",
        )

        assert result.status == DeliveryStatus.SUCCESS
        assert "urls must be a list" in caplog.text
        data = hass.services.async_call.call_args.kwargs["service_data"].get("data", {})
        assert "urls" not in data

    @pytest.mark.asyncio
    async def test_verify_ssl_non_bool_logs_warning(self, caplog):
        """Non-boolean 'verify_ssl' value logs a warning and is not forwarded."""
        adapter, hass = _make_adapter()
        payload = _make_payload(
            title="",
            message="Test",
            metadata={"verify_ssl": "yes"},
        )

        result = await adapter.deliver(
            payload=payload,
            contact_info=_make_contact(),
            idempotency_key="k-sv",
            job_id="job-val",
        )

        assert result.status == DeliveryStatus.SUCCESS
        assert "verify_ssl must be boolean" in caplog.text
        data = hass.services.async_call.call_args.kwargs["service_data"].get("data", {})
        assert "verify_ssl" not in data
