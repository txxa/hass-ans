"""Tests for ANS data models."""

from __future__ import annotations

from datetime import UTC, datetime, time
from uuid import uuid4

import pytest

from custom_components.ans.models import (
    ChannelInfo,
    ChannelScope,
    DoNotDisturbConfig,
    FilterDecision,
    FilterDecisionType,
    FilterReason,
    NotificationCriticality,
    NotificationDeliveryTask,
    NotificationPayload,
    NotificationType,
    RecipientContactInfo,
    RecipientData,
    RecipientType,
    SystemConfig,
)
from custom_components.ans.models.recipient import TTSSettings

from .conftest import make_channel_info, make_payload, make_policy, make_task

# ---------------------------------------------------------------------------
# NotificationPayload
# ---------------------------------------------------------------------------


class TestNotificationPayload:
    def test_frozen(self):
        p = make_payload()
        with pytest.raises((AttributeError, TypeError)):
            p.source = "other"  # type: ignore[misc]

    def test_all_fields_accessible(self):
        ts = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
        p = NotificationPayload(
            notification_id="nid-1",
            source="ha.automation",
            title="Hello",
            message="World",
            type=NotificationType.ALERT,
            criticality=NotificationCriticality.HIGH,
            created_at=ts,
            metadata={"key": "val"},
        )
        assert p.notification_id == "nid-1"
        assert p.source == "ha.automation"
        assert p.type == NotificationType.ALERT
        assert p.criticality == NotificationCriticality.HIGH
        assert p.metadata == {"key": "val"}

    def test_metadata_defaults_to_empty_dict(self):
        p = make_payload()
        assert isinstance(p.metadata, dict)


# ---------------------------------------------------------------------------
# ChannelInfo
# ---------------------------------------------------------------------------


class TestChannelInfo:
    def test_frozen(self):
        c = make_channel_info()
        with pytest.raises((AttributeError, TypeError)):
            c.id = "other"  # type: ignore[misc]

    def test_integration_optional(self):
        c = ChannelInfo(id="notify.foo", label="Foo", scope=ChannelScope.RECIPIENT)
        assert c.integration is None

    def test_scope_values(self):
        assert ChannelScope.SYSTEM == "SYSTEM"
        assert ChannelScope.RECIPIENT == "RECIPIENT"
        assert ChannelScope.TTS == "TTS"


# ---------------------------------------------------------------------------
# FilterDecision
# ---------------------------------------------------------------------------


class TestFilterDecision:
    def test_allowed_decision(self):
        d = FilterDecision(
            decision=FilterDecisionType.ALLOWED, reason=FilterReason.NORMAL
        )
        assert d.decision == FilterDecisionType.ALLOWED
        assert d.reason == FilterReason.NORMAL
        assert d.details is None

    def test_filtered_with_details(self):
        d = FilterDecision(
            decision=FilterDecisionType.FILTERED,
            reason=FilterReason.TYPE_NOT_ALLOWED,
            details={"type": "INFO"},
        )
        assert d.details == {"type": "INFO"}

    def test_frozen(self):
        d = FilterDecision(
            decision=FilterDecisionType.ALLOWED, reason=FilterReason.NORMAL
        )
        with pytest.raises((AttributeError, TypeError)):
            d.decision = FilterDecisionType.FILTERED  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DoNotDisturbConfig
# ---------------------------------------------------------------------------


class TestDoNotDisturbConfig:
    def test_basic(self):
        dnd = DoNotDisturbConfig(
            start=time(22, 0),
            end=time(7, 0),
            allowed_sources_regex=None,
        )
        assert dnd.start == time(22, 0)
        assert dnd.end == time(7, 0)

    def test_optional_fields_default_none(self):
        dnd = DoNotDisturbConfig(
            start=time(0, 0), end=time(8, 0), allowed_sources_regex=None
        )
        assert dnd.allowed_criticalities is None
        assert dnd.allowed_types is None


# ---------------------------------------------------------------------------
# RecipientNotificationPolicy
# ---------------------------------------------------------------------------


class TestRecipientNotificationPolicy:
    def test_defaults(self):
        p = make_policy()
        assert p.retry_attempts == 3
        assert p.rate_limit == 100
        assert p.rate_limit_window == 60
        assert p.blocked_sources_regex is None
        assert p.dnd is None

    def test_all_types_allowed_by_default(self):
        p = make_policy()
        for nt in NotificationType:
            assert nt in p.allowed_types


# ---------------------------------------------------------------------------
# NotificationDeliveryTask
# ---------------------------------------------------------------------------


class TestNotificationDeliveryTask:
    def test_frozen(self):
        t = make_task()
        with pytest.raises((AttributeError, TypeError)):
            t.recipient_id = "other"  # type: ignore[misc]

    def test_to_dict_round_trip(self):
        t = make_task()
        d = t.to_dict()
        assert d["recipient_id"] == t.recipient_id
        assert d["channel_info"]["id"] == t.channel_info.id
        assert d["payload"]["title"] == t.payload.title
        assert d["policy"]["retry_attempts"] == t.policy.retry_attempts

    def test_from_snapshot_valid(self):
        t = make_task()
        snapshot = t.to_dict()
        recovered = NotificationDeliveryTask.from_snapshot(uuid4(), snapshot)
        assert recovered.recipient_id == t.recipient_id
        assert recovered.payload.title == t.payload.title
        assert recovered.is_retry is True

    def test_from_snapshot_missing_key_raises(self):
        t = make_task()
        snapshot = t.to_dict()
        del snapshot["payload"]["title"]
        with pytest.raises(ValueError):
            NotificationDeliveryTask.from_snapshot(uuid4(), snapshot)

    def test_from_snapshot_missing_channel_info_raises(self):
        t = make_task()
        snapshot = t.to_dict()
        del snapshot["channel_info"]
        with pytest.raises(ValueError):
            NotificationDeliveryTask.from_snapshot(uuid4(), snapshot)

    def test_from_snapshot_restores_channel_scope(self):
        t = make_task(
            channel_info=ChannelInfo(
                id="notify.mobile_app_phone",
                label="Phone",
                scope=ChannelScope.RECIPIENT,
            )
        )
        snapshot = t.to_dict()
        recovered = NotificationDeliveryTask.from_snapshot(uuid4(), snapshot)
        assert recovered.channel_info.scope == ChannelScope.RECIPIENT

    def test_to_dict_includes_tts_settings_none(self):
        t = make_task()
        d = t.to_dict()
        assert d.get("tts_settings") is None

    def test_to_dict_includes_tts_settings_when_set(self):
        settings = TTSSettings.default()
        t = make_task(
            channel_info=ChannelInfo(
                id="media_player.kitchen",
                label="Kitchen",
                scope=ChannelScope.TTS,
            ),
            tts_settings=settings,
        )
        d = t.to_dict()
        assert d["tts_settings"] is not None

    def test_from_snapshot_restores_tts_settings(self):
        settings = TTSSettings.default()
        t = make_task(
            channel_info=ChannelInfo(
                id="media_player.kitchen",
                label="Kitchen",
                scope=ChannelScope.TTS,
            ),
            tts_settings=settings,
        )
        snapshot = t.to_dict()
        recovered = NotificationDeliveryTask.from_snapshot(uuid4(), snapshot)
        assert recovered.tts_settings is not None
        assert recovered.tts_settings.volume_morning == settings.volume_morning


# ---------------------------------------------------------------------------
# TTSSettings
# ---------------------------------------------------------------------------


class TestTTSSettings:
    def test_default(self):
        s = TTSSettings.default()
        assert 0 <= s.volume_morning <= 100
        assert 0 <= s.volume_daytime <= 100
        assert s.message_format in ("title_and_message", "message_only", "title_only")

    def test_invalid_volume_raises(self):
        with pytest.raises(ValueError):
            TTSSettings(
                volume_morning=101,
                volume_daytime=50,
                volume_evening=40,
                volume_night=20,
                volume_override_criticalities=[],
                volume_override_level=80,
                message_format="message_only",
            )

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError):
            TTSSettings(
                volume_morning=40,
                volume_daytime=50,
                volume_evening=40,
                volume_night=20,
                volume_override_criticalities=[],
                volume_override_level=80,
                message_format="bad_format",
            )

    def test_to_dict_from_dict_round_trip(self):
        s = TTSSettings.default()
        d = s.to_dict()
        restored = TTSSettings.from_dict(d)
        assert restored.volume_morning == s.volume_morning
        assert restored.message_format == s.message_format

    def test_ssml_enabled_true_preserved_in_round_trip(self):
        """ssml_enabled=True must survive to_dict → from_dict unchanged."""
        s = TTSSettings(
            volume_morning=40,
            volume_daytime=50,
            volume_evening=40,
            volume_night=20,
            volume_override_criticalities=[],
            volume_override_level=80,
            message_format="message_only",
            ssml_enabled=True,
        )
        restored = TTSSettings.from_dict(s.to_dict())
        assert restored.ssml_enabled is True

    def test_ssml_enabled_false_preserved_in_round_trip(self):
        """ssml_enabled=False must survive to_dict → from_dict unchanged."""
        s = TTSSettings(
            volume_morning=40,
            volume_daytime=50,
            volume_evening=40,
            volume_night=20,
            volume_override_criticalities=[],
            volume_override_level=80,
            message_format="message_only",
            ssml_enabled=False,
        )
        restored = TTSSettings.from_dict(s.to_dict())
        assert restored.ssml_enabled is False


# ---------------------------------------------------------------------------
# RecipientContactInfo
# ---------------------------------------------------------------------------


class TestRecipientContactInfo:
    def test_all_none(self):
        c = RecipientContactInfo(email_address=None, phone_number=None)
        assert c.email_address is None
        assert c.phone_number is None
        assert c.mobile_device_id is None

    def test_with_values(self):
        c = RecipientContactInfo(
            email_address="user@example.com",
            phone_number="+12025551234",
            mobile_device_id="device_abc",
        )
        assert c.email_address == "user@example.com"
        assert c.phone_number == "+12025551234"
        assert c.mobile_device_id == "device_abc"


# ---------------------------------------------------------------------------
# RecipientData
# ---------------------------------------------------------------------------


class TestRecipientData:
    def _valid(self, **overrides) -> RecipientData:
        """Return a valid RecipientData, allowing field overrides."""
        defaults = dict(
            id="rcpt-1",
            type=RecipientType.HA_USER,
            name="Alice",
            email=None,
            phone=None,
            version=1,
        )
        defaults.update(overrides)
        return RecipientData(**defaults)

    def test_basic_creation(self):
        r = self._valid()
        assert r.id == "rcpt-1"
        assert r.type == RecipientType.HA_USER
        assert r.name == "Alice"

    def test_to_dict_from_dict_round_trip(self):
        r = self._valid()
        d = r.to_dict()
        restored = RecipientData.from_dict(d)
        assert restored.id == r.id
        assert restored.name == r.name
        assert restored.type == r.type

    def test_empty_id_raises(self):
        with pytest.raises(Exception):
            self._valid(id="")

    def test_empty_name_raises(self):
        with pytest.raises(Exception):
            self._valid(name="")

    def test_invalid_email_accepted(self):
        # The email helper uses vol.Email() which wraps without strict validation;
        # invalid email strings don't raise at model construction time.
        # Actual schema validation happens in the config flow forms.
        rd = self._valid(email="not-an-email")
        assert rd.email == "not-an-email"

    def test_invalid_phone_raises(self):
        with pytest.raises(Exception):
            self._valid(phone="555-1234")  # Not E.164


# ---------------------------------------------------------------------------
# SystemConfig
# ---------------------------------------------------------------------------


class TestSystemConfig:
    def _valid(self, **overrides) -> SystemConfig:
        defaults = dict(
            global_rate_limit=100,
            rate_limit_window=60,
            enabled_channels=["notify.persistent_notification"],
        )
        defaults.update(overrides)
        return SystemConfig(**defaults)

    def test_basic_creation(self):
        s = self._valid()
        assert s.global_rate_limit == 100
        assert "notify.persistent_notification" in s.enabled_channels

    def test_negative_rate_limit_raises(self):
        with pytest.raises(Exception):
            self._valid(global_rate_limit=-1)

    def test_no_channels_and_no_persistent_raises(self):
        with pytest.raises(Exception):
            SystemConfig(
                global_rate_limit=5,
                rate_limit_window=60,
                enabled_channels=[],
                persistent_notifications_enabled=False,
            )

    def test_persistent_notification_alone_valid(self):
        s = SystemConfig(
            global_rate_limit=5,
            rate_limit_window=60,
            enabled_channels=[],
            persistent_notifications_enabled=True,
        )
        assert s.persistent_notifications_enabled is True

    def test_from_dict_round_trip(self):
        s = self._valid()
        d = s.to_dict()
        restored = SystemConfig.from_dict(d)
        assert restored.global_rate_limit == s.global_rate_limit

    def test_retry_base_delay_lt_1_raises(self):
        with pytest.raises(Exception):
            self._valid(retry_base_delay=0)

    def test_retry_backoff_factor_lt_1_raises(self):
        with pytest.raises(Exception):
            self._valid(retry_backoff_factor=0.5)
