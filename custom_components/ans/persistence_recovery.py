"""Persistence recovery and startup initialization.

Handles:
- Loading persisted delivery state on startup
- Recovering pending retries from persistent storage
- Scheduling retry tasks based on stored schedule
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class PersistenceRecovery:
    """Manages persistence layer initialization and recovery on startup."""

    def __init__(
        self,
        hass: HomeAssistant,
    ) -> None:
        """Initialize recovery manager.

        Args:
            hass: Home Assistant instance.

        """
        self._hass = hass
        # Lazy import to avoid circular dependencies
        from .persistence_file import JsonFileAttemptStore, JsonFileDeliveryStateStore

        self.state_store = JsonFileDeliveryStateStore(hass)
        self.attempt_store = JsonFileAttemptStore(hass)

    async def recover_on_startup(self) -> dict[str, Any]:
        """Recover pending delivery state on Home Assistant startup.

        Loads persisted delivery states and attempts from file storage,
        and identifies tasks that need retry.

        Returns:
            Recovery summary with keys:
            - 'pending_retries': List of (job_id, run_at) tuples
            - 'total_states_loaded': Count of loaded delivery states
            - 'total_attempts_loaded': Count of loaded attempts

        """
        _LOGGER.info("Recovering delivery persistence from storage...")

        # Get pending retries that were scheduled before restart
        pending_retries = self.state_store.get_pending_retries()

        _LOGGER.info(
            "Persistence recovery complete: %d pending retries to process",
            len(pending_retries),
        )

        return {
            "pending_retries": pending_retries,
            "state_store_initialized": True,
            "attempt_store_initialized": True,
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
        removed = await self.state_store.cleanup_completed(cutoff)

        if removed > 0:
            _LOGGER.info(
                "Cleaned up %d old delivery records older than %d days",
                removed,
                days,
            )

        return removed


async def async_initialize_persistence(hass: HomeAssistant) -> tuple:
    """Initialize persistence stores and recover pending retries.

    This function should be called during integration setup to:
    1. Create persistent storage instances
    2. Load any pending retries from storage
    3. Return stores and pending retries for scheduler

    Args:
        hass: Home Assistant instance.

    Returns:
        Tuple of (state_store, attempt_store, pending_retries).

    """
    recovery = PersistenceRecovery(hass)
    summary = await recovery.recover_on_startup()
    return (
        recovery.state_store,
        recovery.attempt_store,
        summary.get("pending_retries", []),
    )
