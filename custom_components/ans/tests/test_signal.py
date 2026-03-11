"""Unit tests for the Signal delivery adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

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
    return RecipientContactInfo(email_address=None, phone_number=phone)


def _make_adapter() -> tuple[SignalDeliveryAdapter, MagicMock]:
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
    )

    assert result.status == DeliveryStatus.SUCCESS
    data = hass.services.async_call.call_args.kwargs["service_data"]["data"]
    assert data["attachments"] == ["/config/www/image.jpg"]
    assert data["urls"] == ["http://example.com/pic.jpg"]
    assert data["verify_ssl"] is False
