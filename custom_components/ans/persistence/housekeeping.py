"""Periodic housekeeping tasks for the notification system.

Handles cleanup of old completed deliveries and attempt history.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from .file import DeliveryAttemptLog, NotificationRegistry, RetryQueue

_LOGGER = logging.getLogger(__name__)


class HousekeepingScheduler:
    """Manages periodic cleanup of old delivery records and attempt history."""

    def __init__(
        self,
        notification_registry: NotificationRegistry,
        attempt_log: DeliveryAttemptLog,
        retry_queue: RetryQueue,
        interval: timedelta = timedelta(hours=1),
        retention_age: timedelta = timedelta(days=30),
    ) -> None:
        """Initialize housekeeping scheduler.

        Args:
            notification_registry: Notification registry to clean.
            attempt_log: Attempt log to clean.
            retry_queue: Retry queue to clean.
            interval: How often to run cleanup (default: hourly).
            retention_age: Keep records younger than this (default: 30 days).

        """
        self._notification_registry = notification_registry
        self._attempt_log = attempt_log
        self._retry_queue = retry_queue
        self._interval = interval
        self._retention_age = retention_age
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the housekeeping scheduler."""
        if self._task is not None:
            _LOGGER.warning("Housekeeping scheduler already running")
            return

        self._task = asyncio.create_task(self._run_loop())
        _LOGGER.info("Housekeeping scheduler started (interval: %s)", self._interval)

    async def stop(self) -> None:
        """Stop the housekeeping scheduler."""
        if self._task is None:
            return

        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            _LOGGER.info("Housekeeping scheduler stopped")

    async def _run_loop(self) -> None:
        """Run the cleanup loop."""
        while True:
            try:
                await asyncio.sleep(self._interval.total_seconds())
                await self._cleanup()
            except asyncio.CancelledError:
                break
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Error during housekeeping")

    async def _cleanup(self) -> None:
        """Run cleanup operation."""
        cutoff = datetime.now(UTC) - self._retention_age

        # Clean up old records from all three stores
        await self._notification_registry.cleanup_old(cutoff)
        await self._attempt_log.cleanup_old(cutoff)
        await self._retry_queue.cleanup_old(cutoff)

        _LOGGER.debug(
            "Housekeeping cleanup completed for records older than %s",
            cutoff.isoformat(),
        )
