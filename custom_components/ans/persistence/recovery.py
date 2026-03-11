"""Persistence recovery and startup initialization.

Handles:
- Loading persisted delivery state on startup
- Recovering pending retries from persistent storage
- Scheduling retry tasks based on stored schedule
- Reconstructing tasks from persisted snapshots
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant

from ..models import NotificationDeliveryTask

_LOGGER = logging.getLogger(__name__)


class PersistenceRecovery:
    """Manages persistence layer initialization and recovery on startup."""

    def __init__(
        self,
        hass: HomeAssistant,
        notification_registry=None,
        attempt_log=None,
        retry_queue=None,
    ) -> None:
        """Initialize recovery manager.

        Args:
            hass: Home Assistant instance.
            notification_registry: Existing NotificationRegistry, or None to
                create a new one (used when called standalone).
            attempt_log: Existing DeliveryAttemptLog, or None to create a new one.
            retry_queue: Existing RetryQueue, or None to create a new one.

        """
        self._hass = hass
        if notification_registry is None or attempt_log is None or retry_queue is None:
            # Lazy import to avoid circular dependencies
            from .file import (  # noqa: PLC0415
                DeliveryAttemptLog,
                NotificationRegistry,
                RetryQueue,
            )

            self.notification_registry = notification_registry or NotificationRegistry(
                hass
            )
            self.attempt_log = attempt_log or DeliveryAttemptLog(hass)
            self.retry_queue = retry_queue or RetryQueue(hass)
        else:
            self.notification_registry = notification_registry
            self.attempt_log = attempt_log
            self.retry_queue = retry_queue

    async def recover_on_startup(self) -> dict[str, Any]:
        """Recover pending delivery state on Home Assistant startup.

        Loads persisted delivery states and attempts from file storage,
        and identifies tasks that need retry. Reconstructs full task
        objects from persisted snapshots.

        Returns:
            Recovery summary with keys:
            - 'pending_tasks': List of (NotificationDeliveryTask, run_at) tuples
            - 'orphaned_retries': List of job_ids without task snapshots
            - 'stores_initialized': bool

        """
        _LOGGER.info("Recovering delivery persistence from storage...")

        # Get pending retries with scheduled times
        # (stores load lazily on first access, no explicit load() needed)
        pending_retries = await self.retry_queue.get_pending_retries()
        _LOGGER.debug("Found %d pending retries in queue", len(pending_retries))

        # Reconstruct tasks from snapshots
        pending_tasks = []
        orphaned_retries = []

        for job_id, scheduled_time, snapshot in pending_retries:
            if snapshot:
                try:
                    task = NotificationDeliveryTask.from_snapshot(job_id, snapshot)
                    pending_tasks.append((task, scheduled_time))
                    _LOGGER.debug(
                        "Recovered task %s for retry at %s",
                        job_id,
                        scheduled_time.isoformat(),
                    )
                except (ValueError, KeyError) as e:
                    _LOGGER.error(
                        "Failed to reconstruct task %s from snapshot: %s",
                        job_id,
                        e,
                    )
                    orphaned_retries.append(str(job_id))
            else:
                _LOGGER.warning(
                    "No task snapshot found for scheduled retry %s (orphaned)",
                    job_id,
                )
                orphaned_retries.append(str(job_id))

        _LOGGER.info(
            "Persistence recovery complete: %d tasks recovered, %d orphaned",
            len(pending_tasks),
            len(orphaned_retries),
        )

        return {
            "pending_tasks": pending_tasks,
            "orphaned_retries": orphaned_retries,
            "stores_initialized": True,
        }

    async def cleanup_old_records(self, days: int = 7) -> int:
        """Clean up old completed delivery records.

        Removes terminal state records (SUCCESS, PERMANENT_FAIL, FILTERED)
        that are older than the specified number of days.

        Args:
            days: Remove records older than this many days. Default 7.

        Returns:
            Number of records removed.

        """
        cutoff = datetime.now(UTC) - timedelta(days=days)

        # Clean up all three stores
        await self.notification_registry.cleanup_old(cutoff)
        await self.attempt_log.cleanup_old(cutoff)
        await self.retry_queue.cleanup_old(cutoff)

        _LOGGER.info(
            "Cleaned up delivery records older than %d days (cutoff: %s)",
            days,
            cutoff.isoformat(),
        )

        return 0  # Return count not tracked in new design


async def async_initialize_persistence(
    hass: HomeAssistant,
    notification_registry,
    attempt_log,
    retry_queue,
) -> tuple[list, list]:
    """Initialize persistence and recover pending retries using existing stores.

    Reads the persisted retry schedule from the authoritative store instances
    created by ``create_system()``.  Passing pre-built stores avoids having two
    separate in-memory objects wrapping the same HA Store file.

    Args:
        hass: Home Assistant instance.
        notification_registry: Authoritative NotificationRegistry from ANSSystem.
        attempt_log: Authoritative DeliveryAttemptLog from ANSSystem.
        retry_queue: Authoritative RetryQueue from ANSSystem.

    Returns:
        Tuple of (pending_tasks, orphaned_retries).
        - pending_tasks: List of (NotificationDeliveryTask, run_at) tuples
        - orphaned_retries: List of job_ids that couldn't be recovered

    """
    recovery = PersistenceRecovery(
        hass,
        notification_registry=notification_registry,
        attempt_log=attempt_log,
        retry_queue=retry_queue,
    )
    summary = await recovery.recover_on_startup()
    return (
        summary.get("pending_tasks", []),
        summary.get("orphaned_retries", []),
    )
