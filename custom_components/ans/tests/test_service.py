"""Unit tests for ANS service registration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ..models.notification import (
    NotificationCriticality,
    NotificationType,
)
from ..service import SERVICE_REFRESH_CHANNELS, async_setup_services

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service_call(data: dict) -> MagicMock:
    call = MagicMock()
    call.data = data
    return call


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_register = MagicMock()
    return hass


def _make_orchestrator() -> MagicMock:
    orch = MagicMock()
    orch.handle_notification = AsyncMock()
    return orch


# ---------------------------------------------------------------------------
# Service registration
# ---------------------------------------------------------------------------


async def test_setup_registers_send_service():
    hass = _make_hass()
    orchestrator = _make_orchestrator()

    await async_setup_services(hass, orchestrator)

    # Check that async_register was called for send_notification
    calls = [call.args for call in hass.services.async_register.call_args_list]
    service_names = [(c[0], c[1]) for c in calls]
    from ..const import DOMAIN, SERVICE_SEND

    assert (DOMAIN, SERVICE_SEND) in service_names


async def test_setup_registers_refresh_channels_service():
    hass = _make_hass()
    orchestrator = _make_orchestrator()

    await async_setup_services(hass, orchestrator)

    calls = [call.args for call in hass.services.async_register.call_args_list]
    service_names = [(c[0], c[1]) for c in calls]
    from ..const import DOMAIN

    assert (DOMAIN, SERVICE_REFRESH_CHANNELS) in service_names


# ---------------------------------------------------------------------------
# _build_payload
# ---------------------------------------------------------------------------


def test_build_payload_valid_data():
    from ..service import _build_payload

    call = _make_service_call(
        {
            "source": "test",
            "title": "Test",
            "message": "Hello",
            "type": "INFO",
            "criticality": "LOW",
        }
    )
    payload = _build_payload(call)
    assert payload.source == "test"
    assert payload.title == "Test"
    assert payload.message == "Hello"
    assert payload.type == NotificationType.INFO
    assert payload.criticality == NotificationCriticality.LOW


def test_build_payload_missing_field_raises():
    from ..service import _build_payload

    call = _make_service_call(
        {
            "source": "test",
            # missing title, message, type, criticality
        }
    )
    with pytest.raises(ValueError, match="Missing required field"):
        _build_payload(call)


def test_build_payload_invalid_type_raises():
    from ..service import _build_payload

    call = _make_service_call(
        {
            "source": "test",
            "title": "T",
            "message": "M",
            "type": "not_a_valid_type",
            "criticality": "LOW",
        }
    )
    with pytest.raises(ValueError, match="Invalid notification field value"):
        _build_payload(call)


def test_build_payload_with_metadata():
    from ..service import _build_payload

    call = _make_service_call(
        {
            "source": "test",
            "title": "T",
            "message": "M",
            "type": "INFO",
            "criticality": "LOW",
            "metadata": {"key": "value"},
        }
    )
    payload = _build_payload(call)
    assert payload.metadata == {"key": "value"}


def test_build_payload_without_metadata_defaults_empty():
    from ..service import _build_payload

    call = _make_service_call(
        {
            "source": "test",
            "title": "T",
            "message": "M",
            "type": "INFO",
            "criticality": "LOW",
        }
    )
    payload = _build_payload(call)
    assert payload.metadata == {}


# ---------------------------------------------------------------------------
# Handle notify
# ---------------------------------------------------------------------------


async def test_handle_notify_calls_orchestrator():
    hass = _make_hass()
    orchestrator = _make_orchestrator()

    await async_setup_services(hass, orchestrator)

    # Extract the registered handler
    from ..const import DOMAIN, SERVICE_SEND

    registered_calls = {
        (c.args[0], c.args[1]): c.args[2]
        for c in hass.services.async_register.call_args_list
    }
    handler = registered_calls[(DOMAIN, SERVICE_SEND)]

    service_call = _make_service_call(
        {
            "source": "test",
            "title": "T",
            "message": "M",
            "type": "INFO",
            "criticality": "LOW",
        }
    )
    await handler(service_call)

    orchestrator.handle_notification.assert_called_once()


async def test_handle_notify_invalid_data_raises_value_error():
    hass = _make_hass()
    orchestrator = _make_orchestrator()

    await async_setup_services(hass, orchestrator)

    from ..const import DOMAIN, SERVICE_SEND

    registered_calls = {
        (c.args[0], c.args[1]): c.args[2]
        for c in hass.services.async_register.call_args_list
    }
    handler = registered_calls[(DOMAIN, SERVICE_SEND)]

    bad_call = _make_service_call({"source": "test"})  # Missing required fields
    with pytest.raises(ValueError):
        await handler(bad_call)
