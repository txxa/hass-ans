"""Unit tests for HousekeepingScheduler."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from ..persistence.housekeeping import HousekeepingScheduler


def _make_scheduler(
    **kwargs,
) -> tuple[HousekeepingScheduler, MagicMock, MagicMock, MagicMock]:
    notification_registry = MagicMock()
    notification_registry.cleanup_old = AsyncMock(return_value=0)

    attempt_log = MagicMock()
    attempt_log.cleanup_old = AsyncMock(return_value=0)

    retry_queue = MagicMock()
    retry_queue.cleanup_old = AsyncMock(return_value=0)

    defaults = {
        "notification_registry": notification_registry,
        "attempt_log": attempt_log,
        "retry_queue": retry_queue,
        "interval": timedelta(seconds=10),
        "retention_age": timedelta(days=30),
    }
    defaults.update(kwargs)

    scheduler = HousekeepingScheduler(**defaults)
    return scheduler, notification_registry, attempt_log, retry_queue


# ---------------------------------------------------------------------------
# Start / stop lifecycle
# ---------------------------------------------------------------------------


async def test_start_creates_task():
    scheduler, *_ = _make_scheduler()
    assert scheduler._task is None
    await scheduler.start()
    assert scheduler._task is not None
    await scheduler.stop()


async def test_start_twice_logs_warning(caplog):
    scheduler, *_ = _make_scheduler()
    await scheduler.start()
    await scheduler.start()
    assert "already running" in caplog.text
    await scheduler.stop()


async def test_stop_without_start_is_safe():
    scheduler, *_ = _make_scheduler()
    await scheduler.stop()  # Should not raise


async def test_stop_cancels_running_task():
    scheduler, *_ = _make_scheduler()
    await scheduler.start()
    assert scheduler._task is not None
    await scheduler.stop()
    assert scheduler._task is None


# ---------------------------------------------------------------------------
# Cleanup logic
# ---------------------------------------------------------------------------


async def test_cleanup_calls_all_stores():
    scheduler, notification_registry, attempt_log, retry_queue = _make_scheduler()
    await scheduler._cleanup()

    notification_registry.cleanup_old.assert_called_once()
    attempt_log.cleanup_old.assert_called_once()
    retry_queue.cleanup_old.assert_called_once()


async def test_cleanup_cutoff_uses_retention_age():
    import datetime as dt_module
    from datetime import UTC

    scheduler, notification_registry, *_ = _make_scheduler(
        retention_age=timedelta(days=7)
    )
    await scheduler._cleanup()

    called_cutoff = notification_registry.cleanup_old.call_args[0][0]
    # Cutoff should be approximately now - 7 days
    now = dt_module.datetime.now(UTC)
    expected_min = now - timedelta(days=7) - timedelta(seconds=5)
    expected_max = now - timedelta(days=7) + timedelta(seconds=5)
    assert expected_min <= called_cutoff <= expected_max


# ---------------------------------------------------------------------------
# Run loop: test single-shot by patching sleep
# ---------------------------------------------------------------------------


async def test_run_loop_triggers_cleanup():
    scheduler, notification_registry, attempt_log, retry_queue = _make_scheduler(
        interval=timedelta(seconds=0.01)
    )

    # Run the scheduler briefly and stop it
    await scheduler.start()
    await asyncio.sleep(0.05)  # Let the loop tick at least once
    await scheduler.stop()

    # All three stores should have been cleaned
    assert notification_registry.cleanup_old.call_count >= 1
    assert attempt_log.cleanup_old.call_count >= 1
    assert retry_queue.cleanup_old.call_count >= 1
