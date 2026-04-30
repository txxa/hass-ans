"""Shared test fixtures and factories for the ANS integration test suite."""

from __future__ import annotations

from datetime import UTC, datetime, time
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.ans.models import (
    ChannelInfo,
    ChannelScope,
    DoNotDisturbConfig,
    NotificationCriticality,
    NotificationDeliveryTask,
    NotificationPayload,
    NotificationType,
    RecipientContactInfo,
    RecipientNotificationPolicy,
)

# ---------------------------------------------------------------------------
# Factory helpers (plain functions — call with overrides as needed)
# ---------------------------------------------------------------------------


def make_payload(**overrides) -> NotificationPayload:
    """Build a default NotificationPayload with optional field overrides."""
    defaults: dict = {
        "notification_id": str(uuid4()),
        "source": "test_source",
        "title": "Test Title",
        "message": "Test message body",
        "type": NotificationType.INFO,
        "criticality": NotificationCriticality.LOW,
        "created_at": datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        "metadata": {},
    }
    defaults.update(overrides)
    return NotificationPayload(**defaults)


def make_dnd(start: str, end: str, **overrides) -> DoNotDisturbConfig:
    """Build a DoNotDisturbConfig from 'HH:MM' strings."""
    defaults: dict = {
        "start": time.fromisoformat(start),
        "end": time.fromisoformat(end),
        "allowed_sources_regex": None,
        "allowed_criticalities": None,
        "allowed_types": None,
    }
    defaults.update(overrides)
    return DoNotDisturbConfig(**defaults)


def make_policy(**overrides) -> RecipientNotificationPolicy:
    """Build a default RecipientNotificationPolicy with optional overrides."""
    defaults: dict = {
        "retry_attempts": 3,
        "rate_limit": 100,
        "rate_limit_window": 60,
        "allowed_types": list(NotificationType),
        "blocked_sources_regex": None,
        "dnd": None,
    }
    defaults.update(overrides)
    return RecipientNotificationPolicy(**defaults)


def make_channel_info(**overrides) -> ChannelInfo:
    """Build a default ChannelInfo."""
    defaults: dict = {
        "id": "notify.persistent_notification",
        "label": "Persistent",
        "scope": ChannelScope.SYSTEM,
        "integration": None,
    }
    defaults.update(overrides)
    return ChannelInfo(**defaults)


def make_task(**overrides) -> NotificationDeliveryTask:
    """Build a default NotificationDeliveryTask with optional overrides."""
    defaults: dict = {
        "job_id": uuid4(),
        "recipient_id": "recipient_1",
        "channel_info": make_channel_info(),
        "payload": make_payload(),
        "policy": make_policy(),
        "contact_info": RecipientContactInfo(
            email_address=None,
            phone_number=None,
        ),
        "tts_settings": None,
        "created_at": datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return NotificationDeliveryTask(**defaults)


def make_task_snapshot(task: NotificationDeliveryTask) -> dict:
    """Serialize a task to its snapshot dict (mirrors to_dict)."""
    return task.to_dict()


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def payload() -> NotificationPayload:
    """Return a default NotificationPayload instance."""
    return make_payload()


@pytest.fixture
def policy() -> RecipientNotificationPolicy:
    """Return a default RecipientNotificationPolicy instance."""
    return make_policy()


@pytest.fixture
def task() -> NotificationDeliveryTask:
    """Return a default NotificationDeliveryTask instance."""
    return make_task()


@pytest.fixture
def mock_hass():
    """Return a minimal MagicMock that behaves like a HomeAssistant instance."""
    hass = MagicMock()
    hass.config.path.return_value = "/tmp/ans_test_storage/file.json"  # noqa: S108
    hass.services.async_call = AsyncMock()
    hass.services.async_services = MagicMock(return_value={})
    hass.states.async_entity_ids = MagicMock(return_value=[])
    hass.states.get = MagicMock(return_value=None)
    hass.bus.async_fire = MagicMock()

    # Executor job runs the callable synchronously (avoids real thread pool)
    async def _fake_executor_job(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = _fake_executor_job
    return hass


@pytest.fixture
def mock_channel_manager():
    """Return a minimal MagicMock ChannelManager."""
    mgr = MagicMock()
    mgr.count_detected = MagicMock(return_value=1)
    mgr.count_active = MagicMock(return_value=1)
    mgr.get_adapter = MagicMock(return_value=None)
    mgr.resync = AsyncMock()
    mgr.get_all_channels = MagicMock(return_value=[])
    return mgr


@pytest.fixture(autouse=True)
def _patch_ha_exception_str(monkeypatch):
    """Prevent HA exceptions from calling async_get_hass() during str() in tests.

    HA exceptions (ServiceNotFound, ServiceValidationError, etc.) try to translate
    their message via async_get_hass() when str() is called. In tests without a
    running HA event loop this raises HomeAssistantError("wrong thread"). This fixture
    silently falls back to repr() instead.
    """

    original_str = HomeAssistantError.__str__

    def _safe_str(self):
        try:
            return original_str(self)
        except Exception:  # noqa: BLE001
            return repr(self)

    monkeypatch.setattr(HomeAssistantError, "__str__", _safe_str)
