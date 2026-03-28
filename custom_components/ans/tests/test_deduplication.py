"""Tests for the ANS DeduplicationService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from custom_components.ans.delivery.deduplication import DeduplicationService

DEDUP_DATETIME_PATH = "custom_components.ans.delivery.deduplication.datetime"


# ── helpers ───────────────────────────────────────────────────────────────────


def _service(window: int = 60, max_size: int = 1000) -> DeduplicationService:
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
    async def test_first_delivery_is_not_duplicate(self):
        svc = _service()
        is_dup, reason = await svc.is_duplicate("nid-1", "notify.foo")
        assert is_dup is False
        assert reason  # non-empty string

    async def test_same_delivery_within_window_is_duplicate(self):
        svc = _service(window=60)
        await svc.is_duplicate("nid-1", "notify.foo")  # seed
        is_dup, reason = await svc.is_duplicate("nid-1", "notify.foo")
        assert is_dup is True
        assert reason  # non-empty string

    async def test_different_notification_id_not_duplicate(self):
        svc = _service()
        await svc.is_duplicate("nid-1", "notify.foo")
        is_dup, _ = await svc.is_duplicate("nid-2", "notify.foo")
        assert is_dup is False

    async def test_different_channel_not_duplicate(self):
        svc = _service()
        await svc.is_duplicate("nid-1", "notify.channel_a")
        is_dup, _ = await svc.is_duplicate("nid-1", "notify.channel_b")
        assert is_dup is False

    async def test_both_different_not_duplicate(self):
        svc = _service()
        await svc.is_duplicate("nid-1", "notify.channel_a")
        is_dup, _ = await svc.is_duplicate("nid-2", "notify.channel_b")
        assert is_dup is False


# ── TTL expiration ────────────────────────────────────────────────────────────


class TestTTLExpiration:
    async def test_entry_expired_after_window(self):
        svc = _service(window=60)

        with patch(DEDUP_DATETIME_PATH) as mock_dt:
            mock_dt.now.return_value = _dt(0)
            await svc.is_duplicate("nid-1", "notify.foo")  # seed at t=0

            # Advance past window
            mock_dt.now.return_value = _dt(61)
            is_dup, _ = await svc.is_duplicate("nid-1", "notify.foo")
            assert is_dup is False

    async def test_entry_valid_just_before_window_end(self):
        svc = _service(window=60)

        with patch(DEDUP_DATETIME_PATH) as mock_dt:
            mock_dt.now.return_value = _dt(0)
            await svc.is_duplicate("nid-1", "notify.foo")

            # 59 seconds later — still within window
            mock_dt.now.return_value = _dt(59)
            is_dup, _ = await svc.is_duplicate("nid-1", "notify.foo")
            assert is_dup is True


# ── LRU cache size limit ──────────────────────────────────────────────────────


class TestLRUCacheSizeLimit:
    async def test_excess_entries_are_evicted(self):
        svc = _service(max_size=3)
        for i in range(4):
            await svc.is_duplicate(f"nid-{i}", "notify.foo")
        # Only 3 most recent entries should remain
        assert len(svc._cache) == 3

    async def test_oldest_entry_is_evicted(self):
        svc = _service(max_size=2)
        await svc.is_duplicate("nid-0", "notify.foo")  # oldest
        await svc.is_duplicate("nid-1", "notify.foo")
        # Adding nid-2 evicts nid-0 (LRU)
        await svc.is_duplicate("nid-2", "notify.foo")

        from custom_components.ans.delivery.deduplication import DeduplicationKey

        assert DeduplicationKey("nid-0", "notify.foo") not in svc._cache

    async def test_lru_promotes_accessed_entry(self):
        """Re-accessing an entry promotes it so it's evicted last."""
        svc = _service(max_size=2)
        # Seed two entries
        await svc.is_duplicate("nid-0", "notify.foo")
        await svc.is_duplicate("nid-1", "notify.foo")
        # Re-access nid-0 — promotes it to most-recent position
        await svc.is_duplicate("nid-0", "notify.foo")
        # Adding nid-2 should evict nid-1 (LRU), not nid-0
        await svc.is_duplicate("nid-2", "notify.foo")

        from custom_components.ans.delivery.deduplication import DeduplicationKey

        assert DeduplicationKey("nid-1", "notify.foo") not in svc._cache
        assert DeduplicationKey("nid-0", "notify.foo") in svc._cache


# ── cleanup ───────────────────────────────────────────────────────────────────


class TestCleanup:
    async def test_cleanup_removes_expired_entries(self):
        svc = _service(window=60)

        with patch(DEDUP_DATETIME_PATH) as mock_dt:
            mock_dt.now.return_value = _dt(0)
            await svc.is_duplicate("nid-old", "notify.foo")

            mock_dt.now.return_value = _dt(61)
            await svc._cleanup_expired()
            assert len(svc._cache) == 0

    async def test_cleanup_keeps_valid_entries(self):
        svc = _service(window=120)

        with patch(DEDUP_DATETIME_PATH) as mock_dt:
            mock_dt.now.return_value = _dt(0)
            await svc.is_duplicate("nid-1", "notify.foo")

            mock_dt.now.return_value = _dt(60)
            await svc._cleanup_expired()
            assert len(svc._cache) == 1


# ── Statistics ────────────────────────────────────────────────────────────────


class TestStatistics:
    async def test_hit_incremented_on_duplicate(self):
        svc = _service()
        await svc.is_duplicate("nid-1", "notify.foo")
        await svc.is_duplicate("nid-1", "notify.foo")
        stats = svc.get_stats()
        assert stats["hits"] == 1

    async def test_miss_incremented_on_first_delivery(self):
        svc = _service()
        await svc.is_duplicate("nid-1", "notify.foo")
        stats = svc.get_stats()
        assert stats["misses"] == 1

    async def test_eviction_incremented_when_cache_full(self):
        svc = _service(max_size=1)
        await svc.is_duplicate("nid-1", "notify.foo")
        await svc.is_duplicate("nid-2", "notify.foo")
        stats = svc.get_stats()
        assert stats["evictions"] >= 1


# ── Lifecycle ─────────────────────────────────────────────────────────────────


class TestLifecycle:
    async def test_start_creates_cleanup_task(self):
        svc = _service()
        await svc.start()
        assert svc._cleanup_task is not None
        await svc.stop()

    async def test_stop_cancels_cleanup_task(self):
        svc = _service()
        await svc.start()
        await svc.stop()
        assert svc._cleanup_task is None

    async def test_double_start_does_not_create_extra_task(self):
        svc = _service()
        await svc.start()
        task1 = svc._cleanup_task
        await svc.start()  # should be no-op
        assert svc._cleanup_task is task1
        await svc.stop()
