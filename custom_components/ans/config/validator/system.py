"""System configuration validation."""

from __future__ import annotations

from typing import Any, cast

import voluptuous as vol

from ...const import (
    CONFIG_VERSION_KEY,
    SYS_CONFIG_ENABLED_CHANNELS_KEY,
    SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY,
    SYS_MIN_QUEUE_MAX_DEPTH,
    SYS_MIN_RETRY_BACKOFF_FACTOR,
    SYS_MIN_RETRY_BASE_DELAY_SECONDS,
    SYS_MIN_RETRY_MAX_DELAY_SECONDS,
)
from ...models import SystemConfig
from .common import (
    FieldValidationError,
    _validate_non_negative_integer,
    _validate_string_list,
)


def validate_system_settings_schema(config: dict) -> dict:
    """Validate the system configuration against a schema."""
    schema = vol.Schema(
        {
            vol.Required(SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY): vol.All(
                int, vol.Range(min=0)
            ),
            vol.Required(SYS_CONFIG_ENABLED_CHANNELS_KEY): vol.All(
                [str], vol.Length(min=1)
            ),
        },
        extra=vol.ALLOW_EXTRA,
    )
    return cast(dict[str, Any], schema(config))


def validate_system_config(config: SystemConfig) -> None:
    """Validate the system configuration."""
    # Note: retry_attempts_max is hard-coded and not configurable

    # Validate rate limit
    try:
        _validate_non_negative_integer(config.global_rate_limit)
    except ValueError as e:
        raise FieldValidationError(SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY, str(e)) from e
    except TypeError as e:
        raise FieldValidationError(SYS_CONFIG_GLOBAL_RATE_LIMIT_KEY, str(e)) from e

    # Validate retry settings
    if (
        not isinstance(config.retry_base_delay, int)
        or config.retry_base_delay < SYS_MIN_RETRY_BASE_DELAY_SECONDS
    ):
        raise FieldValidationError(
            "retry_base_delay",
            f"Retry base delay must be an integer >= {SYS_MIN_RETRY_BASE_DELAY_SECONDS}",
        )

    if (
        not isinstance(config.retry_backoff_factor, (int, float))
        or config.retry_backoff_factor < SYS_MIN_RETRY_BACKOFF_FACTOR
    ):
        raise FieldValidationError(
            "retry_backoff_factor",
            f"Retry backoff factor must be a number >= {SYS_MIN_RETRY_BACKOFF_FACTOR}",
        )

    if (
        not isinstance(config.retry_max_delay, int)
        or config.retry_max_delay < SYS_MIN_RETRY_MAX_DELAY_SECONDS
    ):
        raise FieldValidationError(
            "retry_max_delay",
            f"Retry max delay must be an integer >= {SYS_MIN_RETRY_MAX_DELAY_SECONDS}",
        )

    if config.retry_max_delay < config.retry_base_delay:
        raise FieldValidationError(
            "retry_max_delay",
            "Retry max delay must be >= retry base delay",
        )

    # Validate queue concurrency
    if (
        not isinstance(config.queue_max_concurrency, int)
        or config.queue_max_concurrency < 1
    ):
        raise FieldValidationError(
            "queue_max_concurrency",
            "Queue max concurrency must be an integer >= 1",
        )

    # Validate queue max depth
    if (
        not isinstance(config.queue_max_depth, int)
        or config.queue_max_depth < SYS_MIN_QUEUE_MAX_DEPTH
    ):
        raise FieldValidationError(
            "queue_max_depth",
            f"Queue max depth must be an integer >= {SYS_MIN_QUEUE_MAX_DEPTH}",
        )

    # Validate storage retention
    if (
        not isinstance(config.storage_retention_days, int)
        or config.storage_retention_days < 0
    ):
        raise FieldValidationError(
            "storage_retention_days",
            "Storage retention days must be an integer >= 0",
        )

    # Validate enabled channels - must have at least one channel OR persistent notifications enabled
    has_channels = config.enabled_channels and len(config.enabled_channels) > 0
    has_persistent_notifications = getattr(
        config, "persistent_notifications_enabled", False
    )

    if not has_channels and not has_persistent_notifications:
        raise FieldValidationError(
            SYS_CONFIG_ENABLED_CHANNELS_KEY,
            "At least one channel must be enabled or persistent notifications must be enabled.",
        )

    # Validate channel format if any are present
    if has_channels:
        try:
            _validate_string_list(
                config.enabled_channels,
                min_length=1,
                regex_pattern="^(notify|media_player)\\.[a-zA-Z0-9_]+$",
            )
        except ValueError as e:
            raise FieldValidationError(SYS_CONFIG_ENABLED_CHANNELS_KEY, str(e)) from e
        except TypeError as e:
            raise FieldValidationError(SYS_CONFIG_ENABLED_CHANNELS_KEY, str(e)) from e

    # Validate config version
    if not isinstance(config.version, int) or config.version < 1:
        raise FieldValidationError(CONFIG_VERSION_KEY, "Must be a positive integer.")
