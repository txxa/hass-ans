"""Tests for ANS data models."""

from __future__ import annotations

from datetime import UTC, datetime, time
from uuid import uuid4

import pytest

from custom_components.ans.config.validator import FieldValidationError
from custom_components.ans.models import (
    ChannelInfo,
    ChannelScope,
    ConfigSnapshot,
    DoNotDisturbConfig,
    FilterDecision,
    FilterDecisionType,
    FilterReason,
    NotificationCriticality,
    NotificationDeliveryTask,
    NotificationPayload,
    NotificationType,
    RecipientConfig,
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
    """Verify NotificationPayload is frozen, all fields are accessible, and metadata defaults to an empty dict."""

    def test_frozen(self):
        """NotificationPayload is frozen — mutating a field raises AttributeError or TypeError."""
        p = make_payload()
        with pytest.raises((AttributeError, TypeError)):
            p.source = "other"  # type: ignore[misc]

    def test_all_fields_accessible(self):
        """All NotificationPayload fields are stored and readable after construction."""
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
        """When metadata is not supplied, it defaults to an empty dict."""
        p = make_payload()
        assert isinstance(p.metadata, dict)


# ---------------------------------------------------------------------------
# ChannelInfo
# ---------------------------------------------------------------------------


class TestChannelInfo:
    """Verify ChannelInfo is frozen, integration is optional, and ChannelScope values match expected strings."""

    def test_frozen(self):
        """ChannelInfo is frozen — mutating a field raises AttributeError or TypeError."""
        c = make_channel_info()
        with pytest.raises((AttributeError, TypeError)):
            c.id = "other"  # type: ignore[misc]

    def test_integration_optional(self):
        """ChannelInfo.integration defaults to None when not provided."""
        c = ChannelInfo(id="notify.foo", label="Foo", scope=ChannelScope.RECIPIENT)
        assert c.integration is None

    def test_scope_values(self):
        """ChannelScope enum values match the expected string literals ('SYSTEM', 'RECIPIENT', 'TTS')."""
        assert ChannelScope.SYSTEM == "SYSTEM"
        assert ChannelScope.RECIPIENT == "RECIPIENT"
        assert ChannelScope.TTS == "TTS"


# ---------------------------------------------------------------------------
# FilterDecision
# ---------------------------------------------------------------------------


class TestFilterDecision:
    """Verify FilterDecision: allowed result with no details, filtered result with details, and frozen behaviour."""

    def test_allowed_decision(self):
        """A FilterDecision with decision=ALLOWED and reason=NORMAL has details=None."""
        d = FilterDecision(
            decision=FilterDecisionType.ALLOWED, reason=FilterReason.NORMAL
        )
        assert d.decision == FilterDecisionType.ALLOWED
        assert d.reason == FilterReason.NORMAL
        assert d.details is None

    def test_filtered_with_details(self):
        """A FilterDecision with decision=FILTERED stores the provided details dict."""
        d = FilterDecision(
            decision=FilterDecisionType.FILTERED,
            reason=FilterReason.TYPE_NOT_ALLOWED,
            details={"type": "INFO"},
        )
        assert d.details == {"type": "INFO"}

    def test_frozen(self):
        """FilterDecision is frozen — mutating a field raises AttributeError or TypeError."""
        d = FilterDecision(
            decision=FilterDecisionType.ALLOWED, reason=FilterReason.NORMAL
        )
        with pytest.raises((AttributeError, TypeError)):
            d.decision = FilterDecisionType.FILTERED  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DoNotDisturbConfig
# ---------------------------------------------------------------------------


class TestDoNotDisturbConfig:
    """Verify DoNotDisturbConfig stores start/end times and that optional fields default to None."""

    def test_basic(self):
        """DoNotDisturbConfig stores start and end times correctly."""
        dnd = DoNotDisturbConfig(
            start=time(22, 0),
            end=time(7, 0),
            allowed_sources_regex=None,
        )
        assert dnd.start == time(22, 0)
        assert dnd.end == time(7, 0)

    def test_optional_fields_default_none(self):
        """allowed_criticalities and allowed_types default to None."""
        dnd = DoNotDisturbConfig(
            start=time(0, 0), end=time(8, 0), allowed_sources_regex=None
        )
        assert dnd.allowed_criticalities is None
        assert dnd.allowed_types is None


# ---------------------------------------------------------------------------
# RecipientNotificationPolicy
# ---------------------------------------------------------------------------


class TestRecipientNotificationPolicy:
    """Verify RecipientNotificationPolicy defaults and that all NotificationType values are allowed by default."""

    def test_defaults(self):
        """Default policy has retry_attempts=3, rate_limit=100, rate_limit_window=60, and no DND."""
        p = make_policy()
        assert p.retry_attempts == 3
        assert p.rate_limit == 100
        assert p.rate_limit_window == 60
        assert p.blocked_sources_regex is None
        assert p.dnd is None

    def test_all_types_allowed_by_default(self):
        """By default, all NotificationType values are present in policy.allowed_types."""
        p = make_policy()
        for nt in NotificationType:
            assert nt in p.allowed_types


# ---------------------------------------------------------------------------
# NotificationDeliveryTask
# ---------------------------------------------------------------------------


class TestNotificationDeliveryTask:
    """Verify NotificationDeliveryTask: frozen, to_dict/from_snapshot round-trip, TTS settings, and error handling."""

    def test_frozen(self):
        """NotificationDeliveryTask is frozen — mutating a field raises AttributeError or TypeError."""
        t = make_task()
        with pytest.raises((AttributeError, TypeError)):
            t.recipient_id = "other"  # type: ignore[misc]

    def test_to_dict_round_trip(self):
        """to_dict() serialises recipient_id, channel_info, payload, and policy; all values survive round-trip comparison."""
        t = make_task()
        d = t.to_dict()
        assert d["recipient_id"] == t.recipient_id
        assert d["channel_info"]["id"] == t.channel_info.id
        assert d["payload"]["title"] == t.payload.title
        assert d["policy"]["retry_attempts"] == t.policy.retry_attempts

    def test_from_snapshot_valid(self):
        """from_snapshot() rebuilds a task from to_dict() output; the restored task has is_retry=True."""
        t = make_task()
        snapshot = t.to_dict()
        recovered = NotificationDeliveryTask.from_snapshot(uuid4(), snapshot)
        assert recovered.recipient_id == t.recipient_id
        assert recovered.payload.title == t.payload.title
        assert recovered.is_retry is True

    def test_from_snapshot_missing_key_raises(self):
        """from_snapshot() raises ValueError when a required payload field (e.g. 'title') is absent."""
        t = make_task()
        snapshot = t.to_dict()
        del snapshot["payload"]["title"]
        with pytest.raises(ValueError):
            NotificationDeliveryTask.from_snapshot(uuid4(), snapshot)

    def test_from_snapshot_missing_channel_info_raises(self):
        """from_snapshot() raises ValueError when the 'channel_info' key is absent."""
        t = make_task()
        snapshot = t.to_dict()
        del snapshot["channel_info"]
        with pytest.raises(ValueError):
            NotificationDeliveryTask.from_snapshot(uuid4(), snapshot)

    def test_from_snapshot_restores_channel_scope(self):
        """from_snapshot() restores the original ChannelScope from the serialised representation."""
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
        """to_dict() includes a 'tts_settings' key set to None when no TTSSettings are configured."""
        t = make_task()
        d = t.to_dict()
        assert d.get("tts_settings") is None

    def test_to_dict_includes_tts_settings_when_set(self):
        """to_dict() includes a non-None 'tts_settings' value when TTSSettings are provided on the task."""
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
        """from_snapshot() deserialises TTSSettings and restores all volume fields correctly."""
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
    """Verify TTSSettings construction, validation (volume and format), and to_dict/from_dict round-trip."""

    def test_default(self):
        """TTSSettings.default() returns valid volumes in [0, 100] and a recognised message_format string."""
        s = TTSSettings.default()
        assert 0 <= s.volume_morning <= 100
        assert 0 <= s.volume_daytime <= 100
        assert s.message_format in ("title_and_message", "message_only", "title_only")

    def test_invalid_volume_raises(self):
        """A volume_morning above 100 (e.g. 101) raises ValueError."""
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
        """An unrecognised message_format string raises ValueError."""
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
        """TTSSettings.to_dict() / from_dict() preserves volume_morning and message_format."""
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

    def test_ssml_enabled_non_bool_raises(self):
        """A non-bool value for ssml_enabled raises TypeError."""
        with pytest.raises(TypeError):
            TTSSettings(
                volume_morning=40,
                volume_daytime=50,
                volume_evening=40,
                volume_night=20,
                volume_override_criticalities=[],
                volume_override_level=80,
                message_format="message_only",
                ssml_enabled=1,
            )

    def test_volume_management_non_bool_raises(self):
        """A non-bool value for volume_management_enabled raises TypeError."""
        with pytest.raises(TypeError):
            TTSSettings(
                volume_morning=40,
                volume_daytime=50,
                volume_evening=40,
                volume_night=20,
                volume_override_criticalities=[],
                volume_override_level=80,
                message_format="message_only",
                volume_management_enabled=0,
            )


# ---------------------------------------------------------------------------
# RecipientContactInfo
# ---------------------------------------------------------------------------


class TestRecipientContactInfo:
    """Verify RecipientContactInfo construction with all-None fields and with explicit values."""

    def test_all_none(self):
        """RecipientContactInfo with email=None and phone=None has all contact fields set to None."""
        c = RecipientContactInfo(email_address=None, phone_number=None)
        assert c.email_address is None
        assert c.phone_number is None
        assert c.mobile_device_id is None

    def test_with_values(self):
        """RecipientContactInfo stores email, phone, and mobile_device_id correctly."""
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
    """Verify RecipientData construction, to_dict/from_dict round-trip, and validation of id, name, phone, and email."""

    def _valid(self, **overrides) -> RecipientData:
        """Return a valid RecipientData, allowing field overrides."""
        defaults = {
            "id": "rcpt-1",
            "type": RecipientType.HA_USER,
            "name": "Alice",
            "email": None,
            "phone": None,
            "version": 1,
        }
        defaults.update(overrides)
        return RecipientData(**defaults)

    def test_basic_creation(self):
        """RecipientData stores id, type, and name correctly."""
        r = self._valid()
        assert r.id == "rcpt-1"
        assert r.type == RecipientType.HA_USER
        assert r.name == "Alice"

    def test_to_dict_from_dict_round_trip(self):
        """RecipientData.to_dict() / from_dict() preserves id, name, and type."""
        r = self._valid()
        d = r.to_dict()
        restored = RecipientData.from_dict(d)
        assert restored.id == r.id
        assert restored.name == r.name
        assert restored.type == r.type

    def test_empty_id_raises(self):
        """An empty id string raises an exception during construction."""
        with pytest.raises(FieldValidationError):
            self._valid(id="")

    def test_empty_name_raises(self):
        """An empty name string raises an exception during construction."""
        with pytest.raises(FieldValidationError):
            self._valid(name="")

    def test_invalid_email_accepted(self):
        # The email helper uses vol.Email() which wraps without strict validation;
        # invalid email strings don't raise at model construction time.
        # Actual schema validation happens in the config flow forms.
        """A malformed email string is silently stored — model-level validation does not reject it."""
        rd = self._valid(email="not-an-email")
        assert rd.email == "not-an-email"

    def test_invalid_phone_raises(self):
        """A phone number not in E.164 format raises an exception during construction."""
        with pytest.raises(FieldValidationError):
            self._valid(phone="555-1234")  # Not E.164

    def test_to_dict_non_enum_type_stored_as_is(self):
        """to_dict() stores the type field as-is when it is a plain string rather than a RecipientType enum."""
        r = self._valid()
        r.type = "HA_USER"  # plain string, not an Enum instance
        d = r.to_dict()
        assert d["type"] == "HA_USER"


# ---------------------------------------------------------------------------
# SystemConfig
# ---------------------------------------------------------------------------


class TestSystemConfig:
    """Verify SystemConfig construction, validation (rate limit, channels, retry), and to_dict/from_dict."""

    def _valid(self, **overrides) -> SystemConfig:
        """Return a valid SystemConfig with sensible defaults, allowing field overrides."""
        defaults = {
            "global_rate_limit": 100,
            "rate_limit_window": 60,
            "enabled_channels": ["notify.persistent_notification"],
        }
        defaults.update(overrides)
        return SystemConfig(**defaults)

    def test_basic_creation(self):
        """SystemConfig stores global_rate_limit and enabled_channels correctly."""
        s = self._valid()
        assert s.global_rate_limit == 100
        assert "notify.persistent_notification" in s.enabled_channels

    def test_negative_rate_limit_raises(self):
        """A negative global_rate_limit raises an exception during construction."""
        with pytest.raises(FieldValidationError):
            self._valid(global_rate_limit=-1)

    def test_no_channels_and_no_persistent_raises(self):
        """An empty enabled_channels list with persistent_notifications_enabled=False raises an exception."""
        with pytest.raises(FieldValidationError):
            SystemConfig(
                global_rate_limit=5,
                rate_limit_window=60,
                enabled_channels=[],
                persistent_notifications_enabled=False,
            )

    def test_persistent_notification_alone_valid(self):
        """Empty enabled_channels is valid when persistent_notifications_enabled=True."""
        s = SystemConfig(
            global_rate_limit=5,
            rate_limit_window=60,
            enabled_channels=[],
            persistent_notifications_enabled=True,
        )
        assert s.persistent_notifications_enabled is True

    def test_from_dict_round_trip(self):
        """SystemConfig.to_dict() / from_dict() preserves global_rate_limit."""
        s = self._valid()
        d = s.to_dict()
        restored = SystemConfig.from_dict(d)
        assert restored.global_rate_limit == s.global_rate_limit

    def test_retry_base_delay_lt_1_raises(self):
        """A retry_base_delay below 1 raises an exception during construction."""
        with pytest.raises(FieldValidationError):
            self._valid(retry_base_delay=0)

    def test_retry_backoff_factor_lt_1_raises(self):
        """A retry_backoff_factor below 1.0 raises an exception during construction."""
        with pytest.raises(FieldValidationError):
            self._valid(retry_backoff_factor=0.5)

    def test_from_dict_defaults_for_missing_keys(self):
        """SystemConfig.from_dict() uses default values when optional keys are absent."""
        d = {
            "enabled_channels": ["notify.persistent_notification"],
            "global_rate_limit": 50,
        }
        s = SystemConfig.from_dict(d)
        assert s.global_rate_limit == 50
        assert s.tts_service is None
        assert s.retry_base_delay == 60  # SYS_DEFAULT_RETRY_BASE_DELAY_SECONDS

    def test_tts_service_stored_and_retrieved(self):
        """tts_service field is preserved through to_dict() → from_dict() round-trip."""
        s = self._valid(tts_service="tts.google_translate_say")
        d = s.to_dict()
        restored = SystemConfig.from_dict(d)
        assert restored.tts_service == "tts.google_translate_say"


# ---------------------------------------------------------------------------
# RecipientConfig
# ---------------------------------------------------------------------------


class TestRecipientConfig:
    """Verify RecipientConfig factory methods and to_dict/from_dict serialisation."""

    def test_default_creates_valid_config(self):
        """RecipientConfig.default() returns a config with recipient_id=None and dnd_enabled=False."""
        cfg = RecipientConfig.default()
        assert cfg.recipient_id is None
        assert cfg.dnd_enabled is False
        assert cfg.retry_attempts >= 0

    def test_system_default_has_persistent_notification(self):
        """RecipientConfig.system_default() includes persistent_notification in all channel lists."""
        cfg = RecipientConfig.system_default()
        assert "notify.persistent_notification" in cfg.channels_low
        assert "notify.persistent_notification" in cfg.channels_critical

    def test_to_dict_from_dict_round_trip(self):
        """RecipientConfig.to_dict() / from_dict() preserves retry_attempts and dnd_enabled."""
        cfg = RecipientConfig.default()
        d = cfg.to_dict()
        restored = RecipientConfig.from_dict(d)
        assert restored.retry_attempts == cfg.retry_attempts
        assert restored.dnd_enabled == cfg.dnd_enabled

    def test_to_dict_with_non_none_recipient_id(self):
        """to_dict() serialises a non-None recipient_id as a string value."""
        cfg = RecipientConfig.default()
        cfg.recipient_id = "rcpt-42"
        d = cfg.to_dict()
        assert d["recipient_id"] == "rcpt-42"

    def test_to_dict_from_dict_round_trip_with_tts_settings(self):
        """RecipientConfig.to_dict() / from_dict() preserves TTS settings when present."""
        cfg = RecipientConfig.default()
        cfg.tts_settings = TTSSettings.default()
        d = cfg.to_dict()
        restored = RecipientConfig.from_dict(d)
        assert restored.tts_settings is not None
        assert restored.tts_settings.volume_morning == cfg.tts_settings.volume_morning

    def test_non_none_recipient_id_normalised_at_construction(self):
        """__post_init__ normalises a non-None recipient_id to its string representation."""
        cfg = RecipientConfig(
            recipient_id="rcpt-99",
            retry_attempts=3,
            rate_limit=20,
            notification_types=list(NotificationType),
            blocked_sources_regex=None,
            channels_low=[],
            channels_medium=[],
            channels_high=[],
            channels_critical=[],
            dnd_enabled=False,
            dnd_start="22:00:00",
            dnd_end="06:00:00",
            dnd_allowed_sources_regex=None,
        )
        assert cfg.recipient_id == "rcpt-99"
        assert isinstance(cfg.recipient_id, str)


# ---------------------------------------------------------------------------
# ConfigSnapshot
# ---------------------------------------------------------------------------


class TestConfigSnapshot:
    """Verify ConfigSnapshot.getRecipients, getRecipientChannels, getRecipientNotificationPolicy, and getRecipientContactInfo."""

    def _make_system_config(self) -> SystemConfig:
        return SystemConfig(
            global_rate_limit=100,
            rate_limit_window=60,
            enabled_channels=["notify.persistent_notification"],
        )

    def _make_snapshot(
        self, rcpt_id: str = "rcpt-1", **cfg_overrides
    ) -> ConfigSnapshot:
        recipient = RecipientData(
            id=rcpt_id, type=RecipientType.HA_USER, name="Alice", email=None, phone=None
        )
        cfg = RecipientConfig.default()
        for key, val in cfg_overrides.items():
            setattr(cfg, key, val)
        return ConfigSnapshot(
            snapshot_id="snap-1",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            recipients={rcpt_id: recipient},
            recipient_configs={rcpt_id: cfg},
            system_config=self._make_system_config(),
        )

    def test_get_recipients_returns_list(self):
        """getRecipients() returns a list containing the configured recipient ID."""
        snap = self._make_snapshot()
        assert snap.getRecipients() == ["rcpt-1"]

    def test_get_recipient_channels_low(self):
        """getRecipientChannels() for LOW criticality returns the channels_low set."""
        snap = self._make_snapshot(channels_low=["notify.mobile_app"])
        result = snap.getRecipientChannels("rcpt-1", NotificationCriticality.LOW)
        assert result == {"notify.mobile_app"}

    def test_get_recipient_channels_medium(self):
        """getRecipientChannels() for MEDIUM criticality returns the channels_medium set."""
        snap = self._make_snapshot(channels_medium=["notify.signal"])
        result = snap.getRecipientChannels("rcpt-1", NotificationCriticality.MEDIUM)
        assert result == {"notify.signal"}

    def test_get_recipient_channels_high(self):
        """getRecipientChannels() for HIGH criticality returns the channels_high set."""
        snap = self._make_snapshot(channels_high=["notify.email"])
        result = snap.getRecipientChannels("rcpt-1", NotificationCriticality.HIGH)
        assert result == {"notify.email"}

    def test_get_recipient_channels_critical(self):
        """getRecipientChannels() for CRITICAL criticality returns the channels_critical set."""
        snap = self._make_snapshot(
            channels_critical=["notify.sms", "notify.mobile_app"]
        )
        result = snap.getRecipientChannels("rcpt-1", NotificationCriticality.CRITICAL)
        assert result == {"notify.sms", "notify.mobile_app"}

    def test_get_recipient_policy_dnd_disabled(self):
        """getRecipientNotificationPolicy() returns dnd=None when dnd_enabled is False."""
        snap = self._make_snapshot()  # RecipientConfig.default() has dnd_enabled=False
        policy = snap.getRecipientNotificationPolicy("rcpt-1")
        assert policy.dnd is None

    def test_get_recipient_policy_dnd_enabled(self):
        """getRecipientNotificationPolicy() builds a DoNotDisturbConfig when dnd_enabled=True with valid times."""
        # RecipientConfig.default() already has dnd_start="22:00:00" / dnd_end="06:00:00"
        snap = self._make_snapshot(dnd_enabled=True)
        policy = snap.getRecipientNotificationPolicy("rcpt-1")
        assert policy.dnd is not None
        assert policy.dnd.start is not None
        assert policy.dnd.end is not None

    def test_get_recipient_policy_dnd_enabled_no_times_produces_none(self):
        """getRecipientNotificationPolicy() returns dnd=None when dnd_enabled=True but start/end are None."""
        snap = self._make_snapshot(dnd_enabled=True, dnd_start=None, dnd_end=None)
        policy = snap.getRecipientNotificationPolicy("rcpt-1")
        assert policy.dnd is None

    def test_get_recipient_channels_unknown_criticality_returns_empty_set(self):
        """getRecipientChannels() returns an empty set for an unrecognised criticality value."""
        snap = self._make_snapshot()
        result = snap.getRecipientChannels("rcpt-1", "UNKNOWN")  # type: ignore[arg-type]
        assert result == set()

    def test_get_recipient_contact_info(self):
        """getRecipientContactInfo() maps recipient email and phone to a RecipientContactInfo."""
        recipient = RecipientData(
            id="rcpt-1",
            type=RecipientType.HA_USER,
            name="Bob",
            email="bob@example.com",
            phone=None,
        )
        cfg = RecipientConfig.default()
        snap = ConfigSnapshot(
            snapshot_id="snap-1",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            recipients={"rcpt-1": recipient},
            recipient_configs={"rcpt-1": cfg},
            system_config=self._make_system_config(),
        )
        contact = snap.getRecipientContactInfo("rcpt-1")
        assert contact.email_address == "bob@example.com"
        assert contact.phone_number is None
