"""Tests for delivery.factory — system construction and adapter registration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ...channels.channel_manager import ChannelManager
from ...channels.tts_mediaplayer import TTSMediaPlayerAdapter
from ...const import EVENT_NOTIFICATION_FAILED
from ...delivery.factory import (
    _ALL_ADAPTER_CLASSES,
    ADAPTER_CLASS_MAP,
    ANSSystem,
    _create_channel_manager,
    _create_processor_factory,
    create_system,
)
from ...delivery.processor import (
    NotificationDeliveryProcessor,
)
from ...models import NotificationCriticality, SystemConfig
from ..conftest import make_payload, make_task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_system_config(**overrides) -> SystemConfig:
    """Return a SystemConfig with safe defaults for tests."""
    defaults: dict = {
        "global_rate_limit": 100,
        "rate_limit_window": 60,
        "enabled_channels": ["notify.persistent_notification"],
        "retry_base_delay": 60,
        "retry_backoff_factor": 2.0,
        "retry_max_delay": 3600,
        "queue_max_concurrency": 3,
        "storage_retention_days": 7,
        "enable_audit_logging": False,
    }
    defaults.update(overrides)
    return SystemConfig(**defaults)


def _make_config_repo(system_config: SystemConfig | None = None) -> MagicMock:
    """Return a mock ConfigRepository that returns *system_config* from snapshot()."""
    repo = MagicMock()
    sc = system_config or _make_system_config()
    repo.system_config = sc
    snapshot = MagicMock()
    snapshot.system_config = sc
    repo.snapshot.return_value = snapshot
    return repo


def _make_volume_registry() -> MagicMock:
    """Return a minimal mock VolumeRestorationRegistry."""
    return MagicMock()


def _make_hass() -> MagicMock:
    """Return a mock HomeAssistant instance with config.path() set to a temp directory."""
    hass = MagicMock()
    hass.config.path.return_value = "/tmp/ans_test/.storage/file.json"  # noqa: S108
    return hass


# ---------------------------------------------------------------------------
# ADAPTER_CLASS_MAP
# ---------------------------------------------------------------------------


class TestAdapterClassMap:
    """Verify that ADAPTER_CLASS_MAP is consistent with _ALL_ADAPTER_CLASSES — no missing and no extra entries."""

    def test_map_contains_all_adapter_classes(self):
        """Every registered adapter class must appear in the channel map."""
        for cls in _ALL_ADAPTER_CLASSES:
            prefix = cls.get_metadata().channel_prefix
            assert prefix in ADAPTER_CLASS_MAP
            assert ADAPTER_CLASS_MAP[prefix] is cls

    def test_map_has_no_extra_entries(self):
        """The map must not contain entries absent from _ALL_ADAPTER_CLASSES."""
        expected_prefixes = {
            cls.get_metadata().channel_prefix for cls in _ALL_ADAPTER_CLASSES
        }
        assert set(ADAPTER_CLASS_MAP.keys()) == expected_prefixes

    def test_map_values_are_classes(self):
        """Every value in ADAPTER_CLASS_MAP is a class and every key is a non-empty string prefix."""
        for prefix, cls in ADAPTER_CLASS_MAP.items():
            assert isinstance(prefix, str) and prefix
            assert isinstance(cls, type)


# ---------------------------------------------------------------------------
# _create_channel_manager
# ---------------------------------------------------------------------------


class TestCreateChannelManager:
    """Verify that _create_channel_manager() returns a ChannelManager with all adapter factories registered."""

    def test_returns_channel_manager_instance(self):
        """_create_channel_manager() returns a ChannelManager instance."""

        hass = _make_hass()
        repo = _make_config_repo()
        vol_reg = _make_volume_registry()

        with (
            patch.object(ChannelManager, "register_factory"),
            patch.object(ChannelManager, "initialize_static_adapters"),
        ):
            mgr = _create_channel_manager(hass, repo, vol_reg)

        assert isinstance(mgr, ChannelManager)

    def test_registers_all_adapter_factories(self):
        """One factory must be registered per adapter class."""

        hass = _make_hass()
        repo = _make_config_repo()
        vol_reg = _make_volume_registry()

        with (
            patch.object(ChannelManager, "register_factory") as mock_register,
            patch.object(ChannelManager, "initialize_static_adapters"),
        ):
            _create_channel_manager(hass, repo, vol_reg)

        assert mock_register.call_count == len(_ALL_ADAPTER_CLASSES)

    def test_tts_adapter_factory_receives_delivery_lock_other_adapters_do_not(self):
        """TTSMediaPlayerAdapter.create_factory must be called with get_delivery_lock; other adapters must not receive it."""
        hass = _make_hass()
        repo = _make_config_repo()
        vol_reg = _make_volume_registry()

        # Collect create_factory call kwargs per adapter class
        tts_call_kwargs: dict = {}
        other_call_kwargs: list[dict] = []

        def _patched_register_factory(factory):
            pass

        # Patch each adapter class's create_factory to capture kwargs
        with (
            patch.object(ChannelManager, "register_factory"),
            patch.object(ChannelManager, "initialize_static_adapters"),
            patch.object(
                TTSMediaPlayerAdapter,
                "create_factory",
                side_effect=lambda **kw: (tts_call_kwargs.update(kw), MagicMock())[1],
            ),
        ):
            # Patch all non-TTS adapters to capture their kwargs
            non_tts_classes = [
                c for c in _ALL_ADAPTER_CLASSES if c is not TTSMediaPlayerAdapter
            ]

            originals = {cls: cls.create_factory for cls in non_tts_classes}
            for cls in non_tts_classes:

                def _make_side_effect(c):
                    def _se(**kw):
                        other_call_kwargs.append({"cls": c, "kwargs": kw})
                        return MagicMock()

                    return _se

                cls.create_factory = _make_side_effect(cls)

            try:
                _create_channel_manager(hass, repo, vol_reg)
            finally:
                for cls in non_tts_classes:
                    cls.create_factory = originals[cls]

        assert "get_delivery_lock" in tts_call_kwargs, (
            "TTSMediaPlayerAdapter.create_factory should receive get_delivery_lock"
        )
        for item in other_call_kwargs:
            assert "get_delivery_lock" not in item["kwargs"], (
                f"{item['cls'].__name__}.create_factory should NOT receive get_delivery_lock"
            )


# ---------------------------------------------------------------------------
# create_system — happy path
# ---------------------------------------------------------------------------


class TestCreateSystemHappyPath:
    """Verify that create_system() builds a fully populated ANSSystem from a valid config repository."""

    def test_returns_ans_system(self):
        """create_system() returns an ANSSystem instance."""
        hass = _make_hass()
        repo = _make_config_repo()
        vol_reg = _make_volume_registry()

        with (
            patch("ans.delivery.factory._create_channel_manager") as mock_cm_factory,
            patch("ans.delivery.factory.NotificationRegistry"),
            patch("ans.delivery.factory.DeliveryAttemptLog"),
            patch("ans.delivery.factory.RetryQueue"),
            patch("ans.delivery.factory.FilterEngine"),
            patch("ans.delivery.factory.RateLimiter"),
            patch("ans.delivery.factory.RetryPolicy"),
            patch("ans.delivery.factory.NotificationDeliveryTaskQueue"),
            patch("ans.delivery.factory.DeduplicationService"),
            patch("ans.delivery.factory.NotificationOrchestrator"),
            patch("ans.delivery.factory.HousekeepingScheduler"),
        ):
            mock_cm = MagicMock()
            mock_cm_factory.return_value = mock_cm

            result = create_system(hass=hass, config_repo=repo, volume_registry=vol_reg)

        assert isinstance(result, ANSSystem)

    def test_ans_system_fields_populated(self):
        """All fields of ANSSystem must be non-None after construction."""
        hass = _make_hass()
        repo = _make_config_repo()
        vol_reg = _make_volume_registry()

        with (
            patch(
                "ans.delivery.factory._create_channel_manager",
                return_value=MagicMock(),
            ),
            patch(
                "ans.delivery.factory.NotificationRegistry",
                return_value=MagicMock(),
            ),
            patch(
                "ans.delivery.factory.DeliveryAttemptLog",
                return_value=MagicMock(),
            ),
            patch(
                "ans.delivery.factory.RetryQueue",
                return_value=MagicMock(),
            ),
            patch(
                "ans.delivery.factory.FilterEngine",
                return_value=MagicMock(),
            ),
            patch(
                "ans.delivery.factory.RateLimiter",
                return_value=MagicMock(),
            ),
            patch(
                "ans.delivery.factory.RetryPolicy",
                return_value=MagicMock(),
            ),
            patch(
                "ans.delivery.factory.NotificationDeliveryTaskQueue",
                return_value=MagicMock(),
            ),
            patch(
                "ans.delivery.factory.DeduplicationService",
                return_value=MagicMock(),
            ),
            patch(
                "ans.delivery.factory.NotificationOrchestrator",
                return_value=MagicMock(),
            ),
            patch(
                "ans.delivery.factory.HousekeepingScheduler",
                return_value=MagicMock(),
            ),
        ):
            system = create_system(hass=hass, config_repo=repo, volume_registry=vol_reg)

        for field_name in ANSSystem.__dataclass_fields__:
            assert getattr(system, field_name) is not None, (
                f"Field {field_name!r} is None"
            )

    def test_queue_concurrency_sourced_from_system_config(self):
        """NotificationDeliveryTaskQueue must be created with system_config.queue_max_concurrency."""
        hass = _make_hass()
        sc = _make_system_config(queue_max_concurrency=7)
        repo = _make_config_repo(sc)
        vol_reg = _make_volume_registry()

        with (
            patch(
                "ans.delivery.factory._create_channel_manager",
                return_value=MagicMock(),
            ),
            patch("ans.delivery.factory.NotificationRegistry"),
            patch("ans.delivery.factory.DeliveryAttemptLog"),
            patch("ans.delivery.factory.RetryQueue"),
            patch("ans.delivery.factory.FilterEngine"),
            patch("ans.delivery.factory.RateLimiter"),
            patch("ans.delivery.factory.RetryPolicy"),
            patch(
                "ans.delivery.factory.NotificationDeliveryTaskQueue"
            ) as mock_queue_cls,
            patch("ans.delivery.factory.DeduplicationService"),
            patch("ans.delivery.factory.NotificationOrchestrator"),
            patch("ans.delivery.factory.HousekeepingScheduler"),
        ):
            mock_queue_cls.return_value = MagicMock()
            create_system(hass=hass, config_repo=repo, volume_registry=vol_reg)

        _, kwargs = mock_queue_cls.call_args
        assert kwargs["max_concurrency"] == 7

    def test_queue_max_depth_sourced_from_system_config(self):
        """NotificationDeliveryTaskQueue must be created with system_config.queue_max_depth."""
        hass = _make_hass()
        sc = _make_system_config(queue_max_depth=250)
        repo = _make_config_repo(sc)
        vol_reg = _make_volume_registry()

        with (
            patch(
                "ans.delivery.factory._create_channel_manager",
                return_value=MagicMock(),
            ),
            patch("ans.delivery.factory.NotificationRegistry"),
            patch("ans.delivery.factory.DeliveryAttemptLog"),
            patch("ans.delivery.factory.RetryQueue"),
            patch("ans.delivery.factory.FilterEngine"),
            patch("ans.delivery.factory.RateLimiter"),
            patch("ans.delivery.factory.RetryPolicy"),
            patch(
                "ans.delivery.factory.NotificationDeliveryTaskQueue"
            ) as mock_queue_cls,
            patch("ans.delivery.factory.DeduplicationService"),
            patch("ans.delivery.factory.NotificationOrchestrator"),
            patch("ans.delivery.factory.HousekeepingScheduler"),
        ):
            mock_queue_cls.return_value = MagicMock()
            create_system(hass=hass, config_repo=repo, volume_registry=vol_reg)

        _, kwargs = mock_queue_cls.call_args
        assert kwargs["queue_max_depth"] == 250

    def test_on_queue_full_fires_failed_event_with_criticality(self):
        """on_queue_full must fire ans_notification_failed with criticality included.

        Regression test for the 2026-07-13 audit finding: the queue-full
        payload used to be hand-built and omitted criticality. It's now
        built via the same shared build_base_event_payload() used by every
        other delivery outcome event.
        """
        hass = _make_hass()
        repo = _make_config_repo()
        vol_reg = _make_volume_registry()

        with (
            patch(
                "ans.delivery.factory._create_channel_manager",
                return_value=MagicMock(),
            ),
            patch("ans.delivery.factory.NotificationRegistry"),
            patch("ans.delivery.factory.DeliveryAttemptLog"),
            patch("ans.delivery.factory.RetryQueue"),
            patch("ans.delivery.factory.FilterEngine"),
            patch("ans.delivery.factory.RateLimiter"),
            patch("ans.delivery.factory.RetryPolicy"),
            patch(
                "ans.delivery.factory.NotificationDeliveryTaskQueue"
            ) as mock_queue_cls,
            patch("ans.delivery.factory.DeduplicationService"),
            patch("ans.delivery.factory.NotificationOrchestrator"),
            patch("ans.delivery.factory.HousekeepingScheduler"),
        ):
            mock_queue_cls.return_value = MagicMock()
            create_system(hass=hass, config_repo=repo, volume_registry=vol_reg)

        _, kwargs = mock_queue_cls.call_args
        on_queue_full = kwargs["on_queue_full"]

        dropped_task = make_task(
            payload=make_payload(criticality=NotificationCriticality.CRITICAL)
        )

        on_queue_full(dropped_task)

        hass.bus.async_fire.assert_called_once()
        fired_event, fired_payload = hass.bus.async_fire.call_args[0]
        assert fired_event == EVENT_NOTIFICATION_FAILED
        assert fired_payload["criticality"] == NotificationCriticality.CRITICAL.value
        assert fired_payload["error"] == "queue_full"
        assert fired_payload["attempt_number"] == 0

    def test_audit_logging_enabled_passed_to_stores(self):
        """NotificationRegistry and DeliveryAttemptLog must receive the audit flag."""
        hass = _make_hass()
        sc = _make_system_config(enable_audit_logging=True)
        repo = _make_config_repo(sc)
        vol_reg = _make_volume_registry()

        with (
            patch(
                "ans.delivery.factory._create_channel_manager",
                return_value=MagicMock(),
            ),
            patch("ans.delivery.factory.NotificationRegistry") as mock_reg,
            patch("ans.delivery.factory.DeliveryAttemptLog") as mock_log,
            patch("ans.delivery.factory.RetryQueue"),
            patch("ans.delivery.factory.FilterEngine"),
            patch("ans.delivery.factory.RateLimiter"),
            patch("ans.delivery.factory.RetryPolicy"),
            patch(
                "ans.delivery.factory.NotificationDeliveryTaskQueue",
                return_value=MagicMock(),
            ),
            patch("ans.delivery.factory.DeduplicationService"),
            patch("ans.delivery.factory.NotificationOrchestrator"),
            patch("ans.delivery.factory.HousekeepingScheduler"),
        ):
            mock_reg.return_value = MagicMock()
            mock_log.return_value = MagicMock()
            create_system(hass=hass, config_repo=repo, volume_registry=vol_reg)

        assert mock_reg.call_args.kwargs["enabled"] is True
        assert mock_log.call_args.kwargs["enabled"] is True

    def test_audit_logging_disabled_passed_to_stores(self):
        """When enable_audit_logging=False, NotificationRegistry and DeliveryAttemptLog are created with enabled=False."""
        hass = _make_hass()
        sc = _make_system_config(enable_audit_logging=False)
        repo = _make_config_repo(sc)
        vol_reg = _make_volume_registry()

        with (
            patch(
                "ans.delivery.factory._create_channel_manager",
                return_value=MagicMock(),
            ),
            patch("ans.delivery.factory.NotificationRegistry") as mock_reg,
            patch("ans.delivery.factory.DeliveryAttemptLog") as mock_log,
            patch("ans.delivery.factory.RetryQueue"),
            patch("ans.delivery.factory.FilterEngine"),
            patch("ans.delivery.factory.RateLimiter"),
            patch("ans.delivery.factory.RetryPolicy"),
            patch(
                "ans.delivery.factory.NotificationDeliveryTaskQueue",
                return_value=MagicMock(),
            ),
            patch("ans.delivery.factory.DeduplicationService"),
            patch("ans.delivery.factory.NotificationOrchestrator"),
            patch("ans.delivery.factory.HousekeepingScheduler"),
        ):
            mock_reg.return_value = MagicMock()
            mock_log.return_value = MagicMock()
            create_system(hass=hass, config_repo=repo, volume_registry=vol_reg)

        assert mock_reg.call_args.kwargs["enabled"] is False
        assert mock_log.call_args.kwargs["enabled"] is False

    def test_static_adapters_initialized(self):
        """initialize_static_adapters() must be called on the channel manager."""
        hass = _make_hass()
        repo = _make_config_repo()
        vol_reg = _make_volume_registry()

        mock_cm = MagicMock()

        with (
            patch(
                "ans.delivery.factory._create_channel_manager",
                return_value=mock_cm,
            ),
            patch("ans.delivery.factory.NotificationRegistry"),
            patch("ans.delivery.factory.DeliveryAttemptLog"),
            patch("ans.delivery.factory.RetryQueue"),
            patch("ans.delivery.factory.FilterEngine"),
            patch("ans.delivery.factory.RateLimiter"),
            patch("ans.delivery.factory.RetryPolicy"),
            patch(
                "ans.delivery.factory.NotificationDeliveryTaskQueue",
                return_value=MagicMock(),
            ),
            patch("ans.delivery.factory.DeduplicationService"),
            patch("ans.delivery.factory.NotificationOrchestrator"),
            patch("ans.delivery.factory.HousekeepingScheduler"),
        ):
            create_system(hass=hass, config_repo=repo, volume_registry=vol_reg)

        mock_cm.initialize_static_adapters.assert_called_once()


# ---------------------------------------------------------------------------
# create_system — error handling
# ---------------------------------------------------------------------------


class TestCreateSystemErrors:
    """Verify that create_system() raises descriptively when the config repository is not properly loaded."""

    def test_raises_when_snapshot_returns_none(self):
        """create_system must raise ValueError when no system config is loaded."""
        hass = _make_hass()
        repo = MagicMock()
        snapshot = MagicMock()
        snapshot.system_config = None
        repo.snapshot.return_value = snapshot

        with pytest.raises(
            ValueError, match="create_system\\(\\) requires a loaded ConfigRepository"
        ):
            create_system(
                hass=hass, config_repo=repo, volume_registry=_make_volume_registry()
            )

    def test_raises_when_snapshot_is_none(self):
        """When config_repo.snapshot() returns None, create_system() raises ValueError or AttributeError."""
        hass = _make_hass()
        repo = MagicMock()
        repo.snapshot.return_value = None

        with pytest.raises((ValueError, AttributeError)):
            create_system(
                hass=hass, config_repo=repo, volume_registry=_make_volume_registry()
            )

    def test_processor_factory_callable_returns_processor(self):
        """The processor factory closure must return a NotificationDeliveryProcessor."""

        filter_engine = MagicMock()
        rate_limiter = MagicMock()
        cm = MagicMock()
        hass = MagicMock()
        retry_policy = MagicMock()
        notification_registry = MagicMock()
        attempt_log = MagicMock()
        retry_queue = MagicMock()

        factory = _create_processor_factory(
            filter_engine=filter_engine,
            rate_limiter=rate_limiter,
            channel_manager=cm,
            hass=hass,
            retry_policy=retry_policy,
            notification_registry=notification_registry,
            attempt_log=attempt_log,
            retry_queue=retry_queue,
        )

        processor = factory()
        assert isinstance(processor, NotificationDeliveryProcessor)

    def test_processor_factory_each_call_returns_new_instance(self):
        """Factory must produce a distinct instance on every call."""

        factory = _create_processor_factory(
            filter_engine=MagicMock(),
            rate_limiter=MagicMock(),
            channel_manager=MagicMock(),
            hass=MagicMock(),
            retry_policy=MagicMock(),
            notification_registry=MagicMock(),
            attempt_log=MagicMock(),
            retry_queue=MagicMock(),
        )

        p1 = factory()
        p2 = factory()
        assert p1 is not p2
