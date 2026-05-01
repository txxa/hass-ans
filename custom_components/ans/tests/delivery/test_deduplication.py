"""Tests for the ANS DeduplicationService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from ...delivery.deduplication import DeduplicationKey, DeduplicationService

DEDUP_DATETIME_PATH = "ans.delivery.deduplication.datetime"


# ── helpers ───────────────────────────────────────────────────────────────────


def _service(window: int = 60, max_size: int = 1000) -> DeduplicationService:
    """Return a DeduplicationService with the given window and max_size; the cleanup interval is set to 600 s so periodic cleanup never fires during tests."""
    return DeduplicationService(
        window_seconds=window,
        max_cache_size=max_size,
        cleanup_interval=600,  # long interval so cleanup never runs during tests
    )


_BASE_DT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _dt(offset_seconds: int) -> datetime:
    """Return a fixed UTC datetime offset by *offset_seconds* from a base time."""
    return _BASE_DT + timedelta(seconds=offset_seconds)


# ── basic deduplication ───────────────────────────────────────────────────────


class TestBasicDeduplication:
    """Verify that (notification_id, channel) pairs are correctly identified as new or duplicate within the same deduplication window."""

    async def test_first_delivery_is_not_duplicate(self):
        """A brand-new (notification_id, channel) pair is never a duplicate."""
        svc = _service()
        is_dup, reason = await svc.is_duplicate("nid-1", "notify.foo")
        assert is_dup is False
        assert reason  # non-empty string

    async def test_same_delivery_within_window_is_duplicate(self):
        """Calling is_duplicate a second time with the same pair inside the window returns True."""
        svc = _service(window=60)
        await svc.is_duplicate("nid-1", "notify.foo")  # seed
        is_dup, reason = await svc.is_duplicate("nid-1", "notify.foo")
        assert is_dup is True
        assert reason  # non-empty string

    async def test_different_notification_id_not_duplicate(self):
        """Different notification IDs on the same channel are independent cache entries."""
        svc = _service()
        await svc.is_duplicate("nid-1", "notify.foo")
        is_dup, _ = await svc.is_duplicate("nid-2", "notify.foo")
        assert is_dup is False

    async def test_different_channel_not_duplicate(self):
        """The same notification ID delivered on different channels are not considered duplicates."""
        svc = _service()
        await svc.is_duplicate("nid-1", "notify.channel_a")
        is_dup, _ = await svc.is_duplicate("nid-1", "notify.channel_b")
        assert is_dup is False

    async def test_both_different_not_duplicate(self):
        """Pairs that differ in both notification ID and channel are always unique."""
        svc = _service()
        await svc.is_duplicate("nid-1", "notify.channel_a")
        is_dup, _ = await svc.is_duplicate("nid-2", "notify.channel_b")
        assert is_dup is False


# ── TTL expiration ────────────────────────────────────────────────────────────


class TestTTLExpiration:
    """Verify that cache entries expire once the deduplication window has elapsed, resetting the duplicate flag."""

    async def test_entry_expired_after_window(self):
        """An entry first seen at t=0 is no longer a duplicate once the 60 s window has elapsed."""
        svc = _service(window=60)

        with patch(DEDUP_DATETIME_PATH) as mock_dt:
            mock_dt.now.return_value = _dt(0)
            await svc.is_duplicate("nid-1", "notify.foo")  # seed at t=0

            # Advance past window
            mock_dt.now.return_value = _dt(61)
            is_dup, _ = await svc.is_duplicate("nid-1", "notify.foo")
            assert is_dup is False

    async def test_entry_valid_just_before_window_end(self):
        """An entry first seen at t=0 is still a duplicate at t=59 (one second before the 60 s window closes)."""
        svc = _service(window=60)

        with patch(DEDUP_DATETIME_PATH) as mock_dt:
            mock_dt.now.return_value = _dt(0)
            await svc.is_duplicate("nid-1", "notify.foo")

            # 59 seconds later — still within window
            mock_dt.now.return_value = _dt(59)
            is_dup, _ = await svc.is_duplicate("nid-1", "notify.foo")
            assert is_dup is True

    async def test_expiration_stat_incremented_on_stale_hit(self):
        """When a previously-cached entry is found but has expired, the expirations counter is incremented and the entry is treated as a miss."""
        svc = _service(window=60)

        with patch(DEDUP_DATETIME_PATH) as mock_dt:
            mock_dt.now.return_value = _dt(0)
            await svc.is_duplicate("nid-1", "notify.foo")  # seed at t=0

            # Advance past window
            mock_dt.now.return_value = _dt(61)
            is_dup, _ = await svc.is_duplicate("nid-1", "notify.foo")

        assert is_dup is False
        assert svc.get_stats()["expirations"] == 1


# ── LRU cache size limit ──────────────────────────────────────────────────────


class TestLRUCacheSizeLimit:
    """Verify that the LRU cache evicts the least-recently-used entry when max_size is reached."""

    async def test_excess_entries_are_evicted(self):
        """When more entries than max_size are added the cache is trimmed back to max_size."""
        svc = _service(max_size=3)
        for i in range(4):
            await svc.is_duplicate(f"nid-{i}", "notify.foo")
        # Only 3 most recent entries should remain
        assert len(svc._cache) == 3

    async def test_oldest_entry_is_evicted(self):
        """The oldest (least-recently-used) entry is removed when the cache overflows."""
        svc = _service(max_size=2)
        await svc.is_duplicate("nid-0", "notify.foo")  # oldest
        await svc.is_duplicate("nid-1", "notify.foo")
        # Adding nid-2 evicts nid-0 (LRU)
        await svc.is_duplicate("nid-2", "notify.foo")

        assert DeduplicationKey("nid-0", "notify.foo") not in svc._cache

    async def test_lru_promotes_accessed_entry(self):
        """Re-reading an existing entry moves it to the MRU position so the least-recently-read entry is evicted instead."""
        svc = _service(max_size=2)
        # Seed two entries
        await svc.is_duplicate("nid-0", "notify.foo")
        await svc.is_duplicate("nid-1", "notify.foo")
        # Re-access nid-0 — promotes it to most-recent position
        await svc.is_duplicate("nid-0", "notify.foo")
        # Adding nid-2 should evict nid-1 (LRU), not nid-0
        await svc.is_duplicate("nid-2", "notify.foo")

        assert DeduplicationKey("nid-1", "notify.foo") not in svc._cache
        assert DeduplicationKey("nid-0", "notify.foo") in svc._cache


# ── cleanup ───────────────────────────────────────────────────────────────────


class TestCleanup:
    """Verify that the periodic cleanup sweep removes expired entries but retains still-valid ones."""

    async def test_cleanup_removes_expired_entries(self):
        """The cleanup sweep purges entries whose TTL has expired."""
        svc = _service(window=60)

        with patch(DEDUP_DATETIME_PATH) as mock_dt:
            mock_dt.now.return_value = _dt(0)
            await svc.is_duplicate("nid-old", "notify.foo")

            mock_dt.now.return_value = _dt(61)
            await svc._cleanup_expired()
            assert len(svc._cache) == 0

    async def test_cleanup_keeps_valid_entries(self):
        """Entries still within their TTL are retained by the cleanup sweep."""
        svc = _service(window=120)

        with patch(DEDUP_DATETIME_PATH) as mock_dt:
            mock_dt.now.return_value = _dt(0)
            await svc.is_duplicate("nid-1", "notify.foo")

            mock_dt.now.return_value = _dt(60)
            await svc._cleanup_expired()
            assert len(svc._cache) == 1


# ── Statistics ────────────────────────────────────────────────────────────────


class TestStatistics:
    """Verify that the hit/miss/eviction counters in get_stats() reflect actual cache behaviour."""

    async def test_hit_incremented_on_duplicate(self):
        """stats['hits'] increments by one each time a duplicate is detected."""
        svc = _service()
        await svc.is_duplicate("nid-1", "notify.foo")
        await svc.is_duplicate("nid-1", "notify.foo")
        stats = svc.get_stats()
        assert stats["hits"] == 1

    async def test_miss_incremented_on_first_delivery(self):
        """stats['misses'] increments by one each time a new pair is seen for the first time."""
        svc = _service()
        await svc.is_duplicate("nid-1", "notify.foo")
        stats = svc.get_stats()
        assert stats["misses"] == 1

    async def test_eviction_incremented_when_cache_full(self):
        """stats['evictions'] increments whenever an LRU entry is removed to make room for a new one."""
        svc = _service(max_size=1)
        await svc.is_duplicate("nid-1", "notify.foo")
        await svc.is_duplicate("nid-2", "notify.foo")
        stats = svc.get_stats()
        assert stats["evictions"] >= 1

    async def test_hit_rate_zero_when_no_checks(self):
        """get_stats() returns hit_rate=0.0 on a freshly created service with no checks yet."""
        svc = _service()
        stats = svc.get_stats()
        assert stats["hit_rate"] == 0.0


# ── Clear ─────────────────────────────────────────────────────────────────────


class TestClear:
    """Verify that clear() empties the cache and resets all statistics."""

    async def test_clear_empties_cache_and_resets_stats(self):
        """After clear() the cache is empty and every statistics counter is zero."""
        svc = _service()
        # Populate cache and statistics
        await svc.is_duplicate("nid-1", "notify.foo")
        await svc.is_duplicate("nid-1", "notify.foo")  # hit
        assert len(svc._cache) > 0

        await svc.clear()

        assert len(svc._cache) == 0
        stats = svc.get_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["evictions"] == 0
        assert stats["expirations"] == 0


# ── Lifecycle ─────────────────────────────────────────────────────────────────


class TestLifecycle:
    """Verify that start()/stop() correctly manage the background cleanup task."""

    async def test_start_creates_cleanup_task(self):
        """start() spawns a background asyncio task for periodic cleanup."""
        svc = _service()
        await svc.start()
        assert svc._cleanup_task is not None
        await svc.stop()

    async def test_stop_cancels_cleanup_task(self):
        """stop() cancels the cleanup task and sets the reference to None."""
        svc = _service()
        await svc.start()
        await svc.stop()
        assert svc._cleanup_task is None

    async def test_double_start_does_not_create_extra_task(self):
        """Calling start() while already running is a no-op; the existing task is not replaced."""
        svc = _service()
        await svc.start()
        task1 = svc._cleanup_task
        await svc.start()  # should be no-op
        assert svc._cleanup_task is task1
        await svc.stop()
