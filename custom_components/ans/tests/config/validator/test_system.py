"""Unit tests for system configuration validation (config/validator/system.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import voluptuous as vol

from ....config.validator import ConfigValidator
from ....config.validator.common import FieldValidationError

# ---------------------------------------------------------------------------
# validate_system_settings_schema
# ---------------------------------------------------------------------------


def test_system_settings_schema_valid():
    """validate_system_settings_schema() accepts a dict with a positive rate limit and at least one channel."""
    data = {
        "global_rate_limit": 100,
        "enabled_channels": ["notify.persistent_notification"],
    }
    result = ConfigValidator.validate_system_settings_schema(data)
    assert result["global_rate_limit"] == 100


def test_system_settings_schema_empty_channels_raises():
    """validate_system_settings_schema() raises vol.Invalid when enabled_channels is an empty list."""

    data = {
        "global_rate_limit": 100,
        "enabled_channels": [],
    }
    with pytest.raises(vol.Invalid):
        ConfigValidator.validate_system_settings_schema(data)


# ---------------------------------------------------------------------------
# validate_system_config (object-level)
# ---------------------------------------------------------------------------


def _raw_sys_config(**overrides) -> SimpleNamespace:
    """Build a SimpleNamespace that mimics a valid SystemConfig."""
    defaults = {
        "global_rate_limit": 100,
        "retry_base_delay": 60,
        "retry_backoff_factor": 2.0,
        "retry_max_delay": 3600,
        "queue_max_concurrency": 5,
        "queue_max_depth": 500,
        "storage_retention_days": 7,
        "enabled_channels": ["notify.persistent_notification"],
        "persistent_notifications_enabled": True,
        "version": 1,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_validate_system_config_valid_passes():
    """validate_system_config() accepts a well-formed system config namespace without raising."""
    ConfigValidator.validate_system_config(_raw_sys_config())  # must not raise


def test_validate_system_config_negative_rate_limit_raises():
    """A negative global_rate_limit raises FieldValidationError on field 'global_rate_limit'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(global_rate_limit=-1))
    assert exc_info.value.field == "global_rate_limit"


def test_validate_system_config_retry_base_delay_below_one_raises():
    """retry_base_delay of 0 raises FieldValidationError on field 'retry_base_delay'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(retry_base_delay=0))
    assert exc_info.value.field == "retry_base_delay"


def test_validate_system_config_retry_backoff_below_one_raises():
    """retry_backoff_factor below 1.0 raises FieldValidationError on field 'retry_backoff_factor'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(
            _raw_sys_config(retry_backoff_factor=0.5)
        )
    assert exc_info.value.field == "retry_backoff_factor"


def test_validate_system_config_retry_max_delay_below_60_raises():
    """retry_max_delay below 60 raises FieldValidationError on field 'retry_max_delay'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(retry_max_delay=59))
    assert exc_info.value.field == "retry_max_delay"


def test_validate_system_config_queue_concurrency_below_one_raises():
    """queue_max_concurrency of 0 raises FieldValidationError on field 'queue_max_concurrency'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(queue_max_concurrency=0))
    assert exc_info.value.field == "queue_max_concurrency"


def test_validate_system_config_storage_retention_negative_raises():
    """A negative storage_retention_days raises FieldValidationError on field 'storage_retention_days'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(
            _raw_sys_config(storage_retention_days=-1)
        )
    assert exc_info.value.field == "storage_retention_days"


def test_validate_system_config_no_channels_no_persistent_raises():
    """Empty enabled_channels with persistent_notifications_enabled=False raises FieldValidationError('enabled_channels')."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(
            _raw_sys_config(enabled_channels=[], persistent_notifications_enabled=False)
        )
    assert exc_info.value.field == "enabled_channels"


def test_validate_system_config_malformed_channel_raises():
    """A channel name without a dot separator raises FieldValidationError on field 'enabled_channels'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(
            _raw_sys_config(
                enabled_channels=["bad_channel_without_dot"],
                persistent_notifications_enabled=False,
            )
        )
    assert exc_info.value.field == "enabled_channels"


def test_validate_system_config_version_below_one_raises():
    """A version below 1 raises FieldValidationError on field 'version'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(version=0))
    assert exc_info.value.field == "version"


def test_validate_system_config_valid_queue_max_depth_passes():
    """validate_system_config() accepts a valid queue_max_depth value (e.g. 500)."""
    ConfigValidator.validate_system_config(
        _raw_sys_config(queue_max_depth=500)
    )  # must not raise


def test_validate_system_config_queue_max_depth_below_min_raises():
    """A queue_max_depth below the minimum (10) raises FieldValidationError on field 'queue_max_depth'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(queue_max_depth=9))
    assert exc_info.value.field == "queue_max_depth"


def test_validate_system_config_queue_max_depth_non_integer_raises():
    """A non-integer queue_max_depth raises FieldValidationError on field 'queue_max_depth'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(_raw_sys_config(queue_max_depth="500"))
    assert exc_info.value.field == "queue_max_depth"


# ---------------------------------------------------------------------------
# validate_system_config — TypeError paths
# ---------------------------------------------------------------------------


def test_validate_system_config_non_int_rate_limit_raises():
    """A non-integer global_rate_limit raises FieldValidationError on field 'global_rate_limit'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(
            _raw_sys_config(global_rate_limit="high")  # type: ignore[arg-type]
        )
    assert exc_info.value.field == "global_rate_limit"


def test_validate_system_config_non_string_channel_raises():
    """A non-string item in enabled_channels raises FieldValidationError on field 'enabled_channels'."""
    with pytest.raises(FieldValidationError) as exc_info:
        ConfigValidator.validate_system_config(
            _raw_sys_config(
                enabled_channels=[42],  # type: ignore[list-item]
                persistent_notifications_enabled=False,
            )
        )
    assert exc_info.value.field == "enabled_channels"
