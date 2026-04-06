"""Tests for delivery.factory — system construction and adapter registration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.ans.delivery.factory import (
    _ALL_ADAPTER_CLASSES,
    ADAPTER_CLASS_MAP,
    ANSSystem,
    _create_channel_manager,
    create_system,
)
from custom_components.ans.models import SystemConfig

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
    return MagicMock()


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.config.path.return_value = "/tmp/ans_test/.storage/file.json"
    return hass


# ---------------------------------------------------------------------------
# ADAPTER_CLASS_MAP
# ---------------------------------------------------------------------------


class TestAdapterClassMap:
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
        for prefix, cls in ADAPTER_CLASS_MAP.items():
            assert isinstance(prefix, str) and prefix
            assert isinstance(cls, type)


# ---------------------------------------------------------------------------
# _create_channel_manager
# ---------------------------------------------------------------------------


class TestCreateChannelManager:
    def test_returns_channel_manager_instance(self):
        from custom_components.ans.channels.channel_manager import ChannelManager

        hass = _make_hass()
        repo = _make_config_repo()
        vol_reg = _make_volume_registry()

        with (
            patch.object(ChannelManager, "register_factory") as mock_register,
            patch.object(ChannelManager, "initialize_static_adapters"),
        ):
            mgr = _create_channel_manager(hass, repo, vol_reg)

        assert isinstance(mgr, ChannelManager)

    def test_registers_all_adapter_factories(self):
        """One factory must be registered per adapter class."""
        from custom_components.ans.channels.channel_manager import ChannelManager

        hass = _make_hass()
        repo = _make_config_repo()
        vol_reg = _make_volume_registry()

        with (
            patch.object(ChannelManager, "register_factory") as mock_register,
            patch.object(ChannelManager, "initialize_static_adapters"),
        ):
            _create_channel_manager(hass, repo, vol_reg)

        assert mock_register.call_count == len(_ALL_ADAPTER_CLASSES)


# ---------------------------------------------------------------------------
# create_system — happy path
# ---------------------------------------------------------------------------


class TestCreateSystemHappyPath:
    def test_returns_ans_system(self):
        hass = _make_hass()
        repo = _make_config_repo()
        vol_reg = _make_volume_registry()

        with (
            patch(
                "custom_components.ans.delivery.factory._create_channel_manager"
            ) as mock_cm_factory,
            patch("custom_components.ans.delivery.factory.NotificationRegistry"),
            patch("custom_components.ans.delivery.factory.DeliveryAttemptLog"),
            patch("custom_components.ans.delivery.factory.RetryQueue"),
            patch("custom_components.ans.delivery.factory.FilterEngine"),
            patch("custom_components.ans.delivery.factory.RateLimiter"),
            patch("custom_components.ans.delivery.factory.RetryPolicy"),
            patch(
                "custom_components.ans.delivery.factory.NotificationDeliveryTaskQueue"
            ),
            patch("custom_components.ans.delivery.factory.DeduplicationService"),
            patch("custom_components.ans.delivery.factory.NotificationOrchestrator"),
            patch("custom_components.ans.delivery.factory.HousekeepingScheduler"),
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
                "custom_components.ans.delivery.factory._create_channel_manager",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.ans.delivery.factory.NotificationRegistry",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.ans.delivery.factory.DeliveryAttemptLog",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.ans.delivery.factory.RetryQueue",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.ans.delivery.factory.FilterEngine",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.ans.delivery.factory.RateLimiter",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.ans.delivery.factory.RetryPolicy",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.ans.delivery.factory.NotificationDeliveryTaskQueue",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.ans.delivery.factory.DeduplicationService",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.ans.delivery.factory.NotificationOrchestrator",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.ans.delivery.factory.HousekeepingScheduler",
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
                "custom_components.ans.delivery.factory._create_channel_manager",
                return_value=MagicMock(),
            ),
            patch("custom_components.ans.delivery.factory.NotificationRegistry"),
            patch("custom_components.ans.delivery.factory.DeliveryAttemptLog"),
            patch("custom_components.ans.delivery.factory.RetryQueue"),
            patch("custom_components.ans.delivery.factory.FilterEngine"),
            patch("custom_components.ans.delivery.factory.RateLimiter"),
            patch("custom_components.ans.delivery.factory.RetryPolicy"),
            patch(
                "custom_components.ans.delivery.factory.NotificationDeliveryTaskQueue"
            ) as mock_queue_cls,
            patch("custom_components.ans.delivery.factory.DeduplicationService"),
            patch("custom_components.ans.delivery.factory.NotificationOrchestrator"),
            patch("custom_components.ans.delivery.factory.HousekeepingScheduler"),
        ):
            mock_queue_cls.return_value = MagicMock()
            create_system(hass=hass, config_repo=repo, volume_registry=vol_reg)

        _, kwargs = mock_queue_cls.call_args
        assert kwargs["max_concurrency"] == 7

    def test_audit_logging_enabled_passed_to_stores(self):
        """NotificationRegistry and DeliveryAttemptLog must receive the audit flag."""
        hass = _make_hass()
        sc = _make_system_config(enable_audit_logging=True)
        repo = _make_config_repo(sc)
        vol_reg = _make_volume_registry()

        with (
            patch(
                "custom_components.ans.delivery.factory._create_channel_manager",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.ans.delivery.factory.NotificationRegistry"
            ) as mock_reg,
            patch(
                "custom_components.ans.delivery.factory.DeliveryAttemptLog"
            ) as mock_log,
            patch("custom_components.ans.delivery.factory.RetryQueue"),
            patch("custom_components.ans.delivery.factory.FilterEngine"),
            patch("custom_components.ans.delivery.factory.RateLimiter"),
            patch("custom_components.ans.delivery.factory.RetryPolicy"),
            patch(
                "custom_components.ans.delivery.factory.NotificationDeliveryTaskQueue",
                return_value=MagicMock(),
            ),
            patch("custom_components.ans.delivery.factory.DeduplicationService"),
            patch("custom_components.ans.delivery.factory.NotificationOrchestrator"),
            patch("custom_components.ans.delivery.factory.HousekeepingScheduler"),
        ):
            mock_reg.return_value = MagicMock()
            mock_log.return_value = MagicMock()
            create_system(hass=hass, config_repo=repo, volume_registry=vol_reg)

        assert mock_reg.call_args.kwargs["enabled"] is True
        assert mock_log.call_args.kwargs["enabled"] is True

    def test_audit_logging_disabled_passed_to_stores(self):
        hass = _make_hass()
        sc = _make_system_config(enable_audit_logging=False)
        repo = _make_config_repo(sc)
        vol_reg = _make_volume_registry()

        with (
            patch(
                "custom_components.ans.delivery.factory._create_channel_manager",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.ans.delivery.factory.NotificationRegistry"
            ) as mock_reg,
            patch(
                "custom_components.ans.delivery.factory.DeliveryAttemptLog"
            ) as mock_log,
            patch("custom_components.ans.delivery.factory.RetryQueue"),
            patch("custom_components.ans.delivery.factory.FilterEngine"),
            patch("custom_components.ans.delivery.factory.RateLimiter"),
            patch("custom_components.ans.delivery.factory.RetryPolicy"),
            patch(
                "custom_components.ans.delivery.factory.NotificationDeliveryTaskQueue",
                return_value=MagicMock(),
            ),
            patch("custom_components.ans.delivery.factory.DeduplicationService"),
            patch("custom_components.ans.delivery.factory.NotificationOrchestrator"),
            patch("custom_components.ans.delivery.factory.HousekeepingScheduler"),
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
                "custom_components.ans.delivery.factory._create_channel_manager",
                return_value=mock_cm,
            ),
            patch("custom_components.ans.delivery.factory.NotificationRegistry"),
            patch("custom_components.ans.delivery.factory.DeliveryAttemptLog"),
            patch("custom_components.ans.delivery.factory.RetryQueue"),
            patch("custom_components.ans.delivery.factory.FilterEngine"),
            patch("custom_components.ans.delivery.factory.RateLimiter"),
            patch("custom_components.ans.delivery.factory.RetryPolicy"),
            patch(
                "custom_components.ans.delivery.factory.NotificationDeliveryTaskQueue",
                return_value=MagicMock(),
            ),
            patch("custom_components.ans.delivery.factory.DeduplicationService"),
            patch("custom_components.ans.delivery.factory.NotificationOrchestrator"),
            patch("custom_components.ans.delivery.factory.HousekeepingScheduler"),
        ):
            create_system(hass=hass, config_repo=repo, volume_registry=vol_reg)

        mock_cm.initialize_static_adapters.assert_called_once()


# ---------------------------------------------------------------------------
# create_system — error handling
# ---------------------------------------------------------------------------


class TestCreateSystemErrors:
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
        hass = _make_hass()
        repo = MagicMock()
        repo.snapshot.return_value = None

        with pytest.raises((ValueError, AttributeError)):
            create_system(
                hass=hass, config_repo=repo, volume_registry=_make_volume_registry()
            )

    def test_processor_factory_callable_returns_processor(self):
        """The processor factory closure must return a NotificationDeliveryProcessor."""
        from custom_components.ans.delivery.factory import _create_processor_factory
        from custom_components.ans.delivery.processor import (
            NotificationDeliveryProcessor,
        )

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
        from custom_components.ans.delivery.factory import _create_processor_factory

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
