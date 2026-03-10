"""Deduplication service for notification deliveries.

Prevents duplicate deliveries to the same channel within a time window with
cache size limits, TTL-based cleanup, and LRU eviction.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import UTC, datetime
from typing import NamedTuple

_LOGGER = logging.getLogger(__name__)


class DeduplicationKey(NamedTuple):
    """Key for deduplication cache.

    Attributes
    ----------
    notification_id : str
        Unique identifier for the notification.
    channel_id : str
        Channel identifier (e.g., "media_player.living_room").

    """

    notification_id: str
    channel_id: str


class DeduplicationEntry(NamedTuple):
    """Cache entry with timestamp for TTL tracking.

    Attributes
    ----------
    timestamp : datetime
        When this entry was created.
    notification_id : str
        Notification identifier for logging.

    """

    timestamp: datetime
    notification_id: str


class DeduplicationService:
    """Prevents duplicate deliveries within a time window.

    Features:
    - Cache size limit with LRU eviction
    - TTL-based expiration (300 seconds)
    - Periodic cleanup task (every 60 seconds)
    - Thread-safe async operations

    Parameters
    ----------
    window_seconds : int
        Time window in seconds for deduplication. Default: 60 seconds.
    max_cache_size : int
        Maximum number of entries in cache. Default: 1000.
    cleanup_interval : int
        Interval in seconds for cleanup task. Default: 60 seconds.

    """

    def __init__(
        self,
        window_seconds: int = 60,
        max_cache_size: int = 1000,
        cleanup_interval: int = 60,
    ) -> None:
        """Initialize deduplication service with cache limits."""
        self._window_seconds = window_seconds
        self._max_cache_size = max_cache_size
        self._cleanup_interval = cleanup_interval

        # Use OrderedDict for LRU behavior (insertion order preserved)
        self._cache: OrderedDict[DeduplicationKey, DeduplicationEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

        # Statistics for monitoring
        self._stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "expirations": 0,
        }

        _LOGGER.debug(
            "Deduplication service initialized: window=%ds, max_size=%d, cleanup=%ds",
            window_seconds,
            max_cache_size,
            cleanup_interval,
        )

    async def start(self) -> None:
        """Start the periodic cleanup task."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            _LOGGER.debug("Deduplication cleanup task started")

    async def stop(self) -> None:
        """Stop the periodic cleanup task."""
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
            _LOGGER.debug("Deduplication cleanup task stopped")

    async def is_duplicate(
        self, notification_id: str, channel_id: str
    ) -> tuple[bool, str]:
        """Check if notification to channel is a duplicate.

        Parameters
        ----------
        notification_id : str
            Unique notification identifier.
        channel_id : str
            Target channel identifier.

        Returns
        -------
        tuple[bool, str]
            (is_duplicate, reason) where reason explains the decision.

        """
        async with self._lock:
            key = DeduplicationKey(notification_id, channel_id)
            now = datetime.now(UTC)

            # Check if key exists in cache
            if key in self._cache:
                entry = self._cache[key]

                # Check if entry is still valid (within TTL window)
                age = (now - entry.timestamp).total_seconds()
                if age < self._window_seconds:
                    # Move to end for LRU (mark as recently used)
                    self._cache.move_to_end(key)

                    self._stats["hits"] += 1
                    reason = (
                        f"Duplicate delivery blocked: notification={notification_id} "
                        f"to channel={channel_id} within {age:.1f}s "
                        f"(window={self._window_seconds}s)"
                    )
                    _LOGGER.debug(reason)
                    return True, reason

                # Entry expired, remove it
                del self._cache[key]
                self._stats["expirations"] += 1
                _LOGGER.debug(
                    "Deduplication entry expired: notification=%s, channel=%s, age=%.1fs",
                    notification_id,
                    channel_id,
                    age,
                )

            # Not a duplicate, add to cache
            self._stats["misses"] += 1

            # Enforce cache size limit with LRU eviction
            if len(self._cache) >= self._max_cache_size:
                # Remove oldest entry (first item in OrderedDict)
                evicted_key, evicted_entry = self._cache.popitem(last=False)
                self._stats["evictions"] += 1
                _LOGGER.debug(
                    "Deduplication cache full, evicted oldest entry: "
                    "notification=%s, channel=%s",
                    evicted_entry.notification_id,
                    evicted_key.channel_id,
                )

            # Add new entry
            self._cache[key] = DeduplicationEntry(now, notification_id)
            _LOGGER.debug(
                "Deduplication entry added: notification=%s, channel=%s, cache_size=%d",
                notification_id,
                channel_id,
                len(self._cache),
            )

            return False, "Not a duplicate"

    async def _cleanup_loop(self) -> None:
        """Periodic cleanup task to remove expired entries.

        Runs every cleanup_interval seconds to remove entries older than
        window_seconds. This prevents gradual memory accumulation.
        """
        _LOGGER.debug("Deduplication cleanup loop started")
        try:
            while True:
                await asyncio.sleep(self._cleanup_interval)
                await self._cleanup_expired()
        except asyncio.CancelledError:
            _LOGGER.debug("Deduplication cleanup loop cancelled")
            raise

    async def _cleanup_expired(self) -> None:
        """Remove all expired entries from cache."""
        async with self._lock:
            now = datetime.now(UTC)
            expired_keys = []

            # Find all expired entries
            for key, entry in self._cache.items():
                age = (now - entry.timestamp).total_seconds()
                if age >= self._window_seconds:
                    expired_keys.append(key)

            # Remove expired entries
            for key in expired_keys:
                del self._cache[key]
                self._stats["expirations"] += 1

            if expired_keys:
                _LOGGER.debug(
                    "Deduplication cleanup removed %d expired entries, cache_size=%d",
                    len(expired_keys),
                    len(self._cache),
                )

    def get_stats(self) -> dict:
        """Get deduplication statistics.

        Returns
        -------
        dict
            Statistics including hits, misses, evictions, expirations,
            cache_size, and hit_rate.

        """
        total_checks = self._stats["hits"] + self._stats["misses"]
        hit_rate = self._stats["hits"] / total_checks if total_checks > 0 else 0.0

        return {
            "hits": self._stats["hits"],
            "misses": self._stats["misses"],
            "evictions": self._stats["evictions"],
            "expirations": self._stats["expirations"],
            "cache_size": len(self._cache),
            "hit_rate": hit_rate,
            "max_cache_size": self._max_cache_size,
            "window_seconds": self._window_seconds,
        }

    async def clear(self) -> None:
        """Clear all cache entries and reset statistics."""
        async with self._lock:
            cache_size = len(self._cache)
            self._cache.clear()
            self._stats = {
                "hits": 0,
                "misses": 0,
                "evictions": 0,
                "expirations": 0,
            }
            _LOGGER.debug("Deduplication cache cleared: %d entries removed", cache_size)
