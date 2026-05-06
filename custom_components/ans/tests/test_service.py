"""Unit tests for ANS service registration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from ..const import DOMAIN, SERVICE_SEND
from ..models.notification import (
    NotificationCriticality,
    NotificationType,
)
from ..service import SERVICE_REFRESH_CHANNELS, _build_payload, async_setup_services

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service_call(data: dict) -> MagicMock:
    """Return a mock ServiceCall with call.data set to the given dict."""
    call = MagicMock()
    call.data = data
    return call


def _make_hass() -> MagicMock:
    """Return a mock HomeAssistant instance with services.async_register pre-configured."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_register = MagicMock()
    return hass


def _make_orchestrator() -> MagicMock:
    """Return a mock NotificationOrchestrator whose handle_notification is an AsyncMock."""
    orch = MagicMock()
    orch.handle_notification = AsyncMock()
    return orch


# ---------------------------------------------------------------------------
# Service registration
# ---------------------------------------------------------------------------


async def test_setup_registers_send_service():
    """async_setup_services() registers the send_notification service under the ANS domain."""
    hass = _make_hass()
    orchestrator = _make_orchestrator()

    await async_setup_services(hass, orchestrator)

    # Check that async_register was called for send_notification
    calls = [call.args for call in hass.services.async_register.call_args_list]
    service_names = [(c[0], c[1]) for c in calls]

    assert (DOMAIN, SERVICE_SEND) in service_names


async def test_setup_registers_refresh_channels_service():
    """async_setup_services() registers the refresh_channels service under the ANS domain."""
    hass = _make_hass()
    orchestrator = _make_orchestrator()

    await async_setup_services(hass, orchestrator)

    calls = [call.args for call in hass.services.async_register.call_args_list]
    service_names = [(c[0], c[1]) for c in calls]

    assert (DOMAIN, SERVICE_REFRESH_CHANNELS) in service_names


# ---------------------------------------------------------------------------
# _build_payload
# ---------------------------------------------------------------------------


def test_build_payload_valid_data():
    """_build_payload() correctly maps a valid service-call data dict to a NotificationPayload."""

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
    """_build_payload() raises ValueError when a required field (title, message, type, or criticality) is absent."""

    call = _make_service_call(
        {
            "source": "test",
            # missing title, message, type, criticality
        }
    )
    with pytest.raises(ValueError, match="Missing required field"):
        _build_payload(call)


def test_build_payload_invalid_type_raises():
    """_build_payload() raises ValueError when the 'type' field is not a valid NotificationType enum value."""

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
    """_build_payload() preserves an optional 'metadata' dict as-is in the returned NotificationPayload."""

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
    """When 'metadata' is absent from the service call, NotificationPayload.metadata defaults to an empty dict."""

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
    """The registered send handler calls orchestrator.handle_notification() with the parsed NotificationPayload."""
    hass = _make_hass()
    orchestrator = _make_orchestrator()

    await async_setup_services(hass, orchestrator)

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


async def test_handle_notify_returns_notification_id():
    """The registered send handler returns a dict with a 'notification_id' key so automations can correlate events."""
    hass = _make_hass()
    orchestrator = _make_orchestrator()

    await async_setup_services(hass, orchestrator)

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
    result = await handler(service_call)

    assert isinstance(result, dict)
    assert "notification_id" in result
    # Must be a valid UUID string
    UUID(result["notification_id"])


async def test_handle_notify_invalid_data_raises_value_error():
    """The registered send handler re-raises ValueError when the service call data is incomplete or invalid."""
    hass = _make_hass()
    orchestrator = _make_orchestrator()

    await async_setup_services(hass, orchestrator)

    registered_calls = {
        (c.args[0], c.args[1]): c.args[2]
        for c in hass.services.async_register.call_args_list
    }
    handler = registered_calls[(DOMAIN, SERVICE_SEND)]

    bad_call = _make_service_call({"source": "test"})  # Missing required fields
    with pytest.raises(ValueError):
        await handler(bad_call)


# ---------------------------------------------------------------------------
# Handle notify — orchestrator error path
# ---------------------------------------------------------------------------


class TestHandleNotifyValueError:
    """Tests for the _handle_notify error path when the orchestrator raises."""

    async def test_handle_notify_orchestrator_raises_reraises_with_notification_id(
        self,
    ):
        """Re-raises ValueError from orchestrator when a valid payload was already built."""
        hass = _make_hass()
        orchestrator = _make_orchestrator()
        orchestrator.handle_notification.side_effect = ValueError("orchestrator boom")

        await async_setup_services(hass, orchestrator)

        registered_calls = {
            (c.args[0], c.args[1]): c.args[2]
            for c in hass.services.async_register.call_args_list
        }
        handler = registered_calls[(DOMAIN, SERVICE_SEND)]

        good_call = _make_service_call(
            {
                "source": "test",
                "title": "T",
                "message": "M",
                "type": "INFO",
                "criticality": "LOW",
            }
        )
        with pytest.raises(ValueError, match="orchestrator boom"):
            await handler(good_call)

        # Orchestrator must have been called (payload was valid)
        orchestrator.handle_notification.assert_called_once()


# ---------------------------------------------------------------------------
# Handle refresh_channels — handler body branches
# ---------------------------------------------------------------------------


def _get_handler(hass: MagicMock, domain: str, service: str):
    """Extract a registered service handler from hass.services.async_register call_args_list."""
    return {
        (c.args[0], c.args[1]): c.args[2]
        for c in hass.services.async_register.call_args_list
    }[(domain, service)]


class TestHandleRefreshChannels:
    """Tests for the _handle_refresh_channels service handler body."""

    async def test_refresh_channels_config_repo_none_returns_early(self):
        """Logs error and returns early when no ANS config entry is loaded."""
        hass = _make_hass()
        # No config entries → get_main_entry returns None → get_config_repository returns None
        hass.config_entries.async_entries.return_value = []
        orchestrator = _make_orchestrator()

        await async_setup_services(hass, orchestrator)
        handler = _get_handler(hass, DOMAIN, SERVICE_REFRESH_CHANNELS)

        # Must not raise
        await handler(_make_service_call({}))

    async def test_refresh_channels_with_channel_manager_calls_resync(self):
        """When config_repo.channel_manager is set the handler awaits resync() exactly once."""
        hass = _make_hass()
        orchestrator = _make_orchestrator()

        await async_setup_services(hass, orchestrator)
        handler = _get_handler(hass, DOMAIN, SERVICE_REFRESH_CHANNELS)

        channel_manager = MagicMock()
        channel_manager.resync = AsyncMock()
        channel_manager.count_detected = MagicMock(return_value=3)

        config_repo = MagicMock()
        config_repo.channel_manager = channel_manager

        # Wire hass so get_config_repository(hass) returns config_repo
        mock_entry = MagicMock()
        mock_entry.unique_id = DOMAIN
        mock_entry.runtime_data = {"config_repository": config_repo}
        hass.config_entries.async_entries.return_value = [mock_entry]

        await handler(_make_service_call({}))

        channel_manager.resync.assert_awaited_once()

    async def test_refresh_channels_channel_manager_none_skips_resync(self):
        """Skips resync and logs '0 detected channels' when channel_manager is None."""
        hass = _make_hass()
        orchestrator = _make_orchestrator()

        await async_setup_services(hass, orchestrator)
        handler = _get_handler(hass, DOMAIN, SERVICE_REFRESH_CHANNELS)

        config_repo = MagicMock()
        config_repo.channel_manager = None

        mock_entry = MagicMock()
        mock_entry.unique_id = DOMAIN
        mock_entry.runtime_data = {"config_repository": config_repo}
        hass.config_entries.async_entries.return_value = [mock_entry]

        # Must not raise; resync is never called because channel_manager is None
        await handler(_make_service_call({}))


# ---------------------------------------------------------------------------
# _build_payload — granular field validation
# ---------------------------------------------------------------------------


class TestBuildPayloadFieldValidation:
    """Granular tests for _build_payload required-field and enum validation."""

    def test_build_payload_missing_source_raises(self):
        """_build_payload() raises ValueError('Missing required field') when 'source' is absent."""
        call = _make_service_call(
            {"title": "T", "message": "M", "type": "INFO", "criticality": "LOW"}
        )
        with pytest.raises(ValueError, match="Missing required field"):
            _build_payload(call)

    def test_build_payload_missing_title_raises(self):
        """_build_payload() raises ValueError('Missing required field') when 'title' is absent."""
        call = _make_service_call(
            {"source": "s", "message": "M", "type": "INFO", "criticality": "LOW"}
        )
        with pytest.raises(ValueError, match="Missing required field"):
            _build_payload(call)

    def test_build_payload_missing_message_raises(self):
        """_build_payload() raises ValueError('Missing required field') when 'message' is absent."""
        call = _make_service_call(
            {"source": "s", "title": "T", "type": "INFO", "criticality": "LOW"}
        )
        with pytest.raises(ValueError, match="Missing required field"):
            _build_payload(call)

    def test_build_payload_missing_criticality_raises(self):
        """_build_payload() raises ValueError('Missing required field') when 'criticality' is absent."""
        call = _make_service_call(
            {"source": "s", "title": "T", "message": "M", "type": "INFO"}
        )
        with pytest.raises(ValueError, match="Missing required field"):
            _build_payload(call)

    def test_build_payload_invalid_criticality_raises(self):
        """Raises ValueError for an unrecognised 'criticality' enum value."""
        call = _make_service_call(
            {
                "source": "s",
                "title": "T",
                "message": "M",
                "type": "INFO",
                "criticality": "not_a_valid_criticality",
            }
        )
        with pytest.raises(ValueError, match="Invalid notification field value"):
            _build_payload(call)

    def test_build_payload_notification_id_is_uuid_string(self):
        """_build_payload() assigns a valid UUID string as the notification_id."""
        call = _make_service_call(
            {
                "source": "s",
                "title": "T",
                "message": "M",
                "type": "INFO",
                "criticality": "LOW",
            }
        )
        payload = _build_payload(call)
        # Must parse as a valid UUID without raising
        UUID(payload.notification_id)

    def test_build_payload_created_at_has_utc_timezone(self):
        """_build_payload() sets created_at to a UTC-aware datetime."""

        call = _make_service_call(
            {
                "source": "s",
                "title": "T",
                "message": "M",
                "type": "INFO",
                "criticality": "LOW",
            }
        )
        payload = _build_payload(call)
        assert payload.created_at.tzinfo is not None
        assert payload.created_at.utcoffset().total_seconds() == 0
