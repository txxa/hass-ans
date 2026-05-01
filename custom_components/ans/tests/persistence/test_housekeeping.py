"""Unit tests for HousekeepingScheduler."""

from __future__ import annotations

import asyncio
import datetime as dt_module
import logging
from datetime import UTC, timedelta
from unittest.mock import AsyncMock, MagicMock

from ...persistence.housekeeping import HousekeepingScheduler


def _make_scheduler(
    **kwargs,
) -> tuple[HousekeepingScheduler, MagicMock, MagicMock, MagicMock]:
    """Return a (scheduler, notification_registry, attempt_log, retry_queue) tuple with sensible defaults; any kwarg overrides the corresponding HousekeepingScheduler parameter."""
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
    """start() spawns a background asyncio task and stores it in _task."""
    scheduler, *_ = _make_scheduler()
    assert scheduler._task is None
    await scheduler.start()
    assert scheduler._task is not None
    await scheduler.stop()


async def test_start_twice_logs_warning(caplog):
    """Calling start() while the scheduler is already running logs an 'already running' warning."""
    scheduler, *_ = _make_scheduler()
    await scheduler.start()
    await scheduler.start()
    assert "already running" in caplog.text
    await scheduler.stop()


async def test_stop_without_start_is_safe():
    """Calling stop() before start() does not raise."""
    scheduler, *_ = _make_scheduler()
    await scheduler.stop()  # Should not raise


async def test_stop_cancels_running_task():
    """stop() cancels the background task and clears the _task reference."""
    scheduler, *_ = _make_scheduler()
    await scheduler.start()
    assert scheduler._task is not None
    await scheduler.stop()
    assert scheduler._task is None


# ---------------------------------------------------------------------------
# Cleanup logic
# ---------------------------------------------------------------------------


async def test_cleanup_skipped_when_retention_zero():
    """When retention_age is timedelta(0), the cleanup sweep is skipped and no stores are called."""
    scheduler, notification_registry, attempt_log, retry_queue = _make_scheduler(
        retention_age=timedelta(0)
    )
    await scheduler._cleanup()

    notification_registry.cleanup_old.assert_not_called()
    attempt_log.cleanup_old.assert_not_called()
    retry_queue.cleanup_old.assert_not_called()


async def test_cleanup_calls_all_stores():
    """A single cleanup sweep calls cleanup_old() on the notification registry, attempt log, and retry queue."""
    scheduler, notification_registry, attempt_log, retry_queue = _make_scheduler()
    await scheduler._cleanup()

    notification_registry.cleanup_old.assert_called_once()
    attempt_log.cleanup_old.assert_called_once()
    retry_queue.cleanup_old.assert_called_once()


async def test_cleanup_cutoff_uses_retention_age():
    """The cutoff datetime passed to stores is approximately now − retention_age."""

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
    """The background run loop triggers at least one cleanup sweep within its configured interval."""
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


async def test_run_loop_logs_exception_and_continues(caplog):
    """The background loop logs an exception from _cleanup() and keeps running (does not exit)."""

    call_count = 0
    original_cleanup = None

    async def _cleanup_with_first_failure():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("housekeeping boom")
        await original_cleanup()

    scheduler, notification_registry, attempt_log, retry_queue = _make_scheduler(
        interval=timedelta(seconds=0.01)
    )
    original_cleanup = scheduler._cleanup
    scheduler._cleanup = _cleanup_with_first_failure

    with caplog.at_level(
        logging.ERROR, logger="custom_components.ans.persistence.housekeeping"
    ):
        await scheduler.start()
        await asyncio.sleep(0.08)
        await scheduler.stop()

    # The exception should have been logged
    assert any(
        "housekeeping boom" in r.message or "housekeeping boom" in str(r.exc_info)
        for r in caplog.records
    )
    # And the loop continued running — _cleanup was called more than once
    assert call_count >= 2
