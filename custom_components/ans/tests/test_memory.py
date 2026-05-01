"""Unit tests for InMemoryDeliveryStateStore and InMemoryAttemptStore."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

from ..models import Attempt, DeliveryStatus
from ..persistence.memory import InMemoryAttemptStore, InMemoryDeliveryStateStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(UTC)


def _make_attempt(job_id=None, attempt_number: int = 1, error: str | None = None):
    """Return an Attempt with the given job_id, attempt_number, and optional error string; status is SUCCESS unless error is provided."""
    job_id = job_id or uuid4()
    return Attempt(
        attempt_id=uuid4(),
        job_id=job_id,
        attempt_number=attempt_number,
        idempotency_key=f"key-{attempt_number}",
        status=DeliveryStatus.SUCCESS if not error else DeliveryStatus.TRANSIENT_FAIL,
        started_at=_now(),
        ended_at=_now(),
        error=error,
    )


# ===========================================================================
# InMemoryDeliveryStateStore
# ===========================================================================


class TestInMemoryDeliveryStateStore:
    # --- load ----------------------------------------------------------------
    """Verify InMemoryDeliveryStateStore: load, persist_* methods, schedule_retry, and cleanup_completed."""

    async def test_load_unknown_job_returns_none(self):
        """load() returns None when the job_id has never been registered."""
        store = InMemoryDeliveryStateStore()
        result = await store.load(uuid4())
        assert result is None

    async def test_load_returns_existing_state(self):
        """load() returns the persisted DeliveryState for a known job_id."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        await store.persist_filtered(job_id, reason="Test")
        state = await store.load(job_id)
        assert state is not None
        assert state.job_id == job_id
        assert state.status == DeliveryStatus.FILTERED

    # --- persist_filtered ----------------------------------------------------

    async def test_persist_filtered_sets_status(self):
        """persist_filtered() stores FILTERED status and records the reason string as last_error."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        await store.persist_filtered(job_id, reason="dup")
        state = await store.load(job_id)
        assert state.status == DeliveryStatus.FILTERED
        assert state.last_error == "dup"

    async def test_persist_filtered_removes_pending_retry(self):
        """persist_filtered() removes any pending retry entry for the job."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        await store.schedule_retry(job_id, _now() + timedelta(minutes=5))
        assert job_id in store._retries

        await store.persist_filtered(job_id)
        assert job_id not in store._retries

    async def test_persist_filtered_records_creation_time(self):
        """persist_filtered() records a created_at timestamp no earlier than before the call."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        before = _now()
        await store.persist_filtered(job_id)
        assert store._created_at[job_id] >= before

    # --- persist_rate_limited ------------------------------------------------

    async def test_persist_rate_limited_sets_status(self):
        """persist_rate_limited() stores RATE_LIMITED status with 'rate_limited' as the last_error."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        await store.persist_rate_limited(job_id)
        state = await store.load(job_id)
        assert state.status == DeliveryStatus.RATE_LIMITED
        assert state.last_error == "rate_limited"

    async def test_persist_rate_limited_records_creation_time(self):
        """persist_rate_limited() records a created_at entry for the job."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        await store.persist_rate_limited(job_id)
        assert job_id in store._created_at

    async def test_persist_rate_limited_keeps_pending_retry(self):
        """persist_rate_limited() does NOT remove a pending retry (non-terminal state)."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        run_at = _now() + timedelta(minutes=5)
        await store.schedule_retry(job_id, run_at)
        assert job_id in store._retries

        await store.persist_rate_limited(job_id)

        # Retry must still be present
        assert job_id in store._retries

    # --- persist_success -----------------------------------------------------

    async def test_persist_success_sets_status_and_attempt_count(self):
        """persist_success() stores SUCCESS status and copies attempt_number into attempt_count."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        attempt = _make_attempt(job_id, attempt_number=2)
        await store.persist_success(job_id, attempt)
        state = await store.load(job_id)
        assert state.status == DeliveryStatus.SUCCESS
        assert state.attempt_count == 2

    async def test_persist_success_removes_pending_retry(self):
        """persist_success() removes any pending retry entry for the job."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        await store.schedule_retry(job_id, _now() + timedelta(minutes=5))
        attempt = _make_attempt(job_id)
        await store.persist_success(job_id, attempt)
        assert job_id not in store._retries

    # --- persist_transient_failure -------------------------------------------

    async def test_persist_transient_failure_sets_status(self):
        """persist_transient_failure() stores TRANSIENT_FAIL status and the error message from the attempt."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        attempt = _make_attempt(job_id, error="timeout")
        await store.persist_transient_failure(job_id, attempt)
        state = await store.load(job_id)
        assert state.status == DeliveryStatus.TRANSIENT_FAIL
        assert state.last_error == "timeout"

    async def test_persist_transient_failure_keeps_pending_retry(self):
        """persist_transient_failure() does NOT remove a pending retry (non-terminal state)."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        run_at = _now() + timedelta(minutes=5)
        await store.schedule_retry(job_id, run_at)
        assert job_id in store._retries

        attempt = _make_attempt(job_id, error="timeout")
        await store.persist_transient_failure(job_id, attempt)

        # Retry must still be present
        assert job_id in store._retries

    # --- persist_permanent_failure -------------------------------------------

    async def test_persist_permanent_failure_with_attempt(self):
        """persist_permanent_failure() with an attempt stores PERMANENT_FAIL and copies attempt_number and error."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        attempt = _make_attempt(job_id, attempt_number=3, error="bad config")
        await store.persist_permanent_failure(job_id, attempt=attempt)
        state = await store.load(job_id)
        assert state.status == DeliveryStatus.PERMANENT_FAIL
        assert state.attempt_count == 3
        assert state.last_error == "bad config"

    async def test_persist_permanent_failure_without_attempt(self):
        """persist_permanent_failure() without an attempt stores PERMANENT_FAIL with attempt_count=0 and the given error string."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        await store.persist_permanent_failure(job_id, error="no channels")
        state = await store.load(job_id)
        assert state.status == DeliveryStatus.PERMANENT_FAIL
        assert state.attempt_count == 0
        assert state.last_error == "no channels"

    async def test_persist_permanent_failure_removes_pending_retry(self):
        """persist_permanent_failure() removes any pending retry entry for the job."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        await store.schedule_retry(job_id, _now() + timedelta(minutes=5))
        await store.persist_permanent_failure(job_id, error="permanent")
        assert job_id not in store._retries

    # --- schedule_retry ------------------------------------------------------

    async def test_schedule_retry_stores_entry(self):
        """schedule_retry() stores the run_at datetime and reason in the _retries dict."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        run_at = _now() + timedelta(minutes=10)
        await store.schedule_retry(job_id, run_at, reason="rate")
        assert job_id in store._retries
        stored_run_at, stored_reason = store._retries[job_id]
        assert stored_run_at == run_at
        assert stored_reason == "rate"

    async def test_schedule_retry_accepts_task_kwarg(self):
        """schedule_retry must accept the optional `task` parameter (LSP)."""

        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        mock_task = MagicMock()
        # Must not raise TypeError
        await store.schedule_retry(
            job_id, _now() + timedelta(minutes=1), reason="retry", task=mock_task
        )
        assert job_id in store._retries

    async def test_schedule_retry_overwrites_existing(self):
        """A second call to schedule_retry() for the same job_id overwrites the first entry."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        first_run_at = _now() + timedelta(minutes=5)
        second_run_at = _now() + timedelta(minutes=10)
        await store.schedule_retry(job_id, first_run_at)
        await store.schedule_retry(job_id, second_run_at)
        stored_run_at, _ = store._retries[job_id]
        assert stored_run_at == second_run_at

    # --- get_pending_retries -------------------------------------------------

    async def test_get_pending_retries_empty(self):
        """get_pending_retries() returns an empty list when no retries are scheduled."""
        store = InMemoryDeliveryStateStore()
        assert store.get_pending_retries() == []

    async def test_get_pending_retries_returns_all(self):
        """get_pending_retries() returns one entry per scheduled retry."""
        store = InMemoryDeliveryStateStore()
        job_a = uuid4()
        job_b = uuid4()
        run_a = _now() + timedelta(minutes=1)
        run_b = _now() + timedelta(minutes=2)
        await store.schedule_retry(job_a, run_a)
        await store.schedule_retry(job_b, run_b)
        retries = store.get_pending_retries()
        assert len(retries) == 2
        ids = {r[0] for r in retries}
        assert ids == {job_a, job_b}

    # --- cleanup_completed ---------------------------------------------------

    async def test_cleanup_completed_removes_old_terminal_states(self):
        """cleanup_completed() removes terminal-status states whose created_at is before the cutoff."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        await store.persist_success(job_id, _make_attempt(job_id))

        # Wind creation time back so it's before the cutoff
        store._created_at[job_id] = _now() - timedelta(days=10)

        cutoff = _now() - timedelta(days=5)
        removed = await store.cleanup_completed(cutoff)
        assert removed == 1
        assert await store.load(job_id) is None

    async def test_cleanup_completed_keeps_recent_terminal_states(self):
        """cleanup_completed() retains terminal states whose created_at is at or after the cutoff."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        await store.persist_success(job_id, _make_attempt(job_id))
        # creation time is "now" — should not be removed

        cutoff = _now() - timedelta(days=5)
        removed = await store.cleanup_completed(cutoff)
        assert removed == 0
        assert await store.load(job_id) is not None

    async def test_cleanup_completed_keeps_non_terminal_states(self):
        """cleanup_completed() never removes non-terminal states (e.g. TRANSIENT_FAIL) regardless of age."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        await store.persist_transient_failure(job_id, _make_attempt(job_id, error="x"))
        # Wind back creation time
        store._created_at[job_id] = _now() - timedelta(days=10)

        cutoff = _now() - timedelta(days=5)
        removed = await store.cleanup_completed(cutoff)
        # TRANSIENT_FAIL is not terminal
        assert removed == 0
        assert await store.load(job_id) is not None

    async def test_cleanup_completed_also_removes_pending_retry(self):
        """cleanup_completed() removes the _retries entry alongside the state when cleaning up an old job."""
        store = InMemoryDeliveryStateStore()
        job_id = uuid4()
        await store.persist_filtered(job_id)
        await store.schedule_retry(job_id, _now() + timedelta(minutes=1))
        store._created_at[job_id] = _now() - timedelta(days=10)

        # Force a retry entry to exist (unusual but testing edge case)
        store._retries[job_id] = (_now(), None)

        cutoff = _now() - timedelta(days=5)
        await store.cleanup_completed(cutoff)
        assert job_id not in store._retries


# ===========================================================================
# InMemoryAttemptStore
# ===========================================================================


class TestInMemoryAttemptStore:
    # --- create & get_attempts -----------------------------------------------
    """Verify InMemoryAttemptStore: create, get_attempts, update, next_attempt_number, count, and cleanup_old_attempts."""

    async def test_create_adds_attempt(self):
        """create() stores an Attempt and it is retrievable via get_attempts()."""
        store = InMemoryAttemptStore()
        job_id = uuid4()
        attempt = _make_attempt(job_id)
        await store.create(attempt)
        assert len(store.get_attempts(job_id)) == 1

    async def test_create_multiple_attempts_for_same_job(self):
        """create() can store multiple attempts under the same job_id."""
        store = InMemoryAttemptStore()
        job_id = uuid4()
        for n in range(1, 4):
            await store.create(_make_attempt(job_id, attempt_number=n))
        assert len(store.get_attempts(job_id)) == 3

    async def test_get_attempts_unknown_job_returns_empty_list(self):
        """get_attempts() returns an empty list for an unrecognised job_id."""
        store = InMemoryAttemptStore()
        assert store.get_attempts(uuid4()) == []

    # --- update --------------------------------------------------------------

    async def test_update_replaces_existing_attempt(self):
        """update() replaces the stored Attempt matching attempt_id with the new values."""
        store = InMemoryAttemptStore()
        job_id = uuid4()
        original = _make_attempt(job_id, attempt_number=1)
        await store.create(original)

        updated = Attempt(
            attempt_id=original.attempt_id,
            job_id=job_id,
            attempt_number=1,
            idempotency_key="key-1",
            status=DeliveryStatus.PERMANENT_FAIL,
            started_at=original.started_at,
            ended_at=_now(),
            error="updated error",
        )
        await store.update(updated)

        stored = store.get_attempts(job_id)[0]
        assert stored.status == DeliveryStatus.PERMANENT_FAIL
        assert stored.error == "updated error"

    async def test_update_not_found_logs_warning(self, caplog):
        """update() logs a warning mentioning 'update()' when the attempt_id does not exist."""

        store = InMemoryAttemptStore()
        job_id = uuid4()
        attempt = _make_attempt(job_id, attempt_number=99)

        with caplog.at_level(
            logging.WARNING, logger="custom_components.ans.persistence.memory"
        ):
            await store.update(attempt)

        assert any("update()" in r.message for r in caplog.records)

    # --- next_attempt_number -------------------------------------------------

    async def test_next_attempt_number_starts_at_one(self):
        """next_attempt_number() returns 1 when no attempts have been logged for the job."""
        store = InMemoryAttemptStore()
        assert await store.next_attempt_number(uuid4()) == 1

    async def test_next_attempt_number_increments_with_existing_attempts(self):
        """next_attempt_number() returns max(existing attempt numbers) + 1."""
        store = InMemoryAttemptStore()
        job_id = uuid4()
        await store.create(_make_attempt(job_id, attempt_number=1))
        await store.create(_make_attempt(job_id, attempt_number=2))
        assert await store.next_attempt_number(job_id) == 3

    async def test_next_attempt_number_uses_max_not_count(self):
        """Max is used so gaps in attempt numbers are handled correctly."""
        store = InMemoryAttemptStore()
        job_id = uuid4()
        await store.create(_make_attempt(job_id, attempt_number=5))
        assert await store.next_attempt_number(job_id) == 6

    # --- count ---------------------------------------------------------------

    async def test_count_empty(self):
        """count() returns 0 when no attempts have been logged for the job."""
        store = InMemoryAttemptStore()
        assert await store.count(uuid4()) == 0

    async def test_count_returns_correct_number(self):
        """count() returns the exact number of attempts logged for the job."""
        store = InMemoryAttemptStore()
        job_id = uuid4()
        for n in range(1, 4):
            await store.create(_make_attempt(job_id, attempt_number=n))
        assert await store.count(job_id) == 3

    async def test_count_is_per_job(self):
        """count() tracks attempts independently per job_id."""
        store = InMemoryAttemptStore()
        job_a = uuid4()
        job_b = uuid4()
        await store.create(_make_attempt(job_a))
        await store.create(_make_attempt(job_a, attempt_number=2))
        await store.create(_make_attempt(job_b))
        assert await store.count(job_a) == 2
        assert await store.count(job_b) == 1

    # --- cleanup_old_attempts ------------------------------------------------

    async def test_cleanup_old_attempts_returns_zero(self):
        """In-memory store cleanup is a documented no-op that returns 0."""
        store = InMemoryAttemptStore()
        job_id = uuid4()
        await store.create(_make_attempt(job_id))
        result = await store.cleanup_old_attempts(_now())
        assert result == 0
        # Data is stil present (no cleanup)
        assert await store.count(job_id) == 1
