"""Tests for delivery.queue — NotificationDeliveryTaskQueue."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from custom_components.ans.delivery.queue import (
    _MAX_RETRY_AGE,
    NotificationDeliveryTaskQueue,
)

from ..conftest import make_task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_processor_factory(*, raises: Exception | None = None):
    """Return a factory that creates a mock processor.

    If *raises* is given the processor's process() coroutine will raise it.
    """
    processor = MagicMock()
    if raises:
        processor.process = AsyncMock(side_effect=raises)
    else:
        processor.process = AsyncMock()
    return MagicMock(return_value=processor)


def _make_queue(
    *,
    max_concurrency: int = 5,
    processor_factory=None,
    retry_queue=None,
    queue_max_depth: int = 500,
    on_queue_full=None,
) -> NotificationDeliveryTaskQueue:
    """Return a NotificationDeliveryTaskQueue with a default processor factory and the given parameters."""
    factory = processor_factory or _make_processor_factory()
    return NotificationDeliveryTaskQueue(
        max_concurrency=max_concurrency,
        processor_factory=factory,
        retry_queue=retry_queue,
        queue_max_depth=queue_max_depth,
        on_queue_full=on_queue_full,
    )


def _make_future_snapshot(*, delay_seconds: float = 3600) -> dict:
    """Return a task snapshot whose timestamp is in the future (not yet stale)."""
    task = make_task()
    snap = task.to_dict()
    future_ts = datetime.now(UTC) + timedelta(seconds=delay_seconds)
    snap["payload"]["timestamp"] = future_ts.isoformat()
    return snap


def _make_fresh_snapshot() -> dict:
    """Return a task snapshot whose timestamp is just now (young, eligible)."""
    task = make_task()
    snap = task.to_dict()
    snap["payload"]["timestamp"] = datetime.now(UTC).isoformat()
    return snap


def _make_stale_snapshot() -> dict:
    """Return a task snapshot whose notification is older than _MAX_RETRY_AGE."""
    task = make_task()
    snap = task.to_dict()
    old_ts = datetime.now(UTC) - _MAX_RETRY_AGE - timedelta(seconds=1)
    snap["payload"]["timestamp"] = old_ts.isoformat()
    return snap


# ---------------------------------------------------------------------------
# __init__ — validation
# ---------------------------------------------------------------------------


class TestInit:
    """Verify that __init__() validates max_concurrency and sets correct initial queue state."""

    def test_valid_max_concurrency_does_not_raise(self):
        """NotificationDeliveryTaskQueue() does not raise for valid positive max_concurrency values."""
        _make_queue(max_concurrency=1)
        _make_queue(max_concurrency=10)

    def test_zero_max_concurrency_raises(self):
        """NotificationDeliveryTaskQueue() raises ValueError when max_concurrency is 0."""
        with pytest.raises(ValueError, match="max_concurrency"):
            _make_queue(max_concurrency=0)

    def test_negative_max_concurrency_raises(self):
        """NotificationDeliveryTaskQueue() raises ValueError when max_concurrency is negative."""
        with pytest.raises(ValueError, match="max_concurrency"):
            _make_queue(max_concurrency=-1)

    def test_initial_state(self):
        """A newly created queue reports 0 pending tasks, 0 active tasks, and is not running."""
        q = _make_queue(max_concurrency=3)
        assert q.pending_count == 0
        assert q.active_task_count == 0
        assert not q.is_running


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


class TestDiagnostics:
    """Verify pending_count, is_running, active_task_count, and __repr__ reflect the queue state accurately."""

    async def test_pending_count_increases_on_enqueue(self):
        """pending_count increments by 1 for each enqueue() call when the queue is not started."""
        q = _make_queue()
        # Don't start the queue so tasks accumulate
        task = make_task()
        await q.enqueue(task)
        assert q.pending_count == 1
        await q.enqueue(task)
        assert q.pending_count == 2

    async def test_is_running_true_after_start(self):
        """is_running returns True after start() and before stop()."""
        q = _make_queue()
        await q.start()
        try:
            assert q.is_running
        finally:
            await q.stop()

    async def test_is_running_false_after_stop(self):
        """is_running returns False after stop()."""
        q = _make_queue()
        await q.start()
        await q.stop()
        assert not q.is_running

    def test_repr_contains_key_fields(self):
        """__repr__() output includes max_concurrency, pending, active, and running fields."""
        q = _make_queue(max_concurrency=4)
        text = repr(q)
        assert "max_concurrency=4" in text
        assert "pending=" in text
        assert "active=" in text
        assert "running=" in text


# ---------------------------------------------------------------------------
# start() / stop() lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Verify start/stop lifecycle: worker creation, idempotency, and retry poller management."""

    async def test_start_creates_worker_task(self):
        """start() creates a non-done _worker_task."""
        q = _make_queue()
        await q.start()
        assert q._worker_task is not None
        assert not q._worker_task.done()
        await q.stop()

    async def test_stop_sets_stopped_flag_and_clears_tasks(self):
        """stop() sets the _stopped event and sets _worker_task to None."""
        q = _make_queue()
        await q.start()
        await q.stop()
        assert q._stopped.is_set()
        assert q._worker_task is None

    async def test_double_start_does_not_create_second_worker(self):
        """Calling start() twice keeps the original _worker_task (idempotent)."""
        q = _make_queue()
        await q.start()
        first_task = q._worker_task
        await q.start()
        assert q._worker_task is first_task
        await q.stop()

    async def test_stop_then_start_resumes_processing(self):
        """After stop() → start() the queue must process new tasks."""
        processed: list = []

        async def _process(task):
            """Record the job_id when the processor handles a task."""
            processed.append(task.job_id)

        processor = MagicMock()
        processor.process = _process
        factory = MagicMock(return_value=processor)
        q = _make_queue(processor_factory=factory)

        await q.start()
        await q.stop()

        # restart
        await q.start()
        task = make_task()
        await q.enqueue(task)
        # Allow the worker loop to pick up and process the task
        await asyncio.sleep(0.05)
        await q.stop()

        assert task.job_id in processed

    async def test_start_creates_retry_poller_when_retry_queue_provided(self):
        """start() creates a running _retry_poller_task when a retry_queue is configured."""
        mock_rq = MagicMock()
        mock_rq.get_pending_retries = AsyncMock(return_value=[])
        q = _make_queue(retry_queue=mock_rq)
        await q.start()
        assert q._retry_poller_task is not None
        assert not q._retry_poller_task.done()
        await q.stop()

    async def test_stop_clears_retry_poller_task(self):
        """stop() cancels and clears _retry_poller_task."""
        mock_rq = MagicMock()
        mock_rq.get_pending_retries = AsyncMock(return_value=[])
        q = _make_queue(retry_queue=mock_rq)
        await q.start()
        await q.stop()
        assert q._retry_poller_task is None

    async def test_stop_cancels_delayed_background_tasks(self):
        """stop() cancels all pending delayed-delivery background tasks."""
        q = _make_queue()
        await q.start()
        task = make_task()
        # Schedule a task with a long delay — it should be cancelled on stop()
        await q.add_task(task, delay=timedelta(hours=1))
        assert len(q._background_tasks) >= 1
        await q.stop()
        assert len(q._background_tasks) == 0


# ---------------------------------------------------------------------------
# enqueue / add_task
# ---------------------------------------------------------------------------


class TestEnqueueAndAddTask:
    """Verify enqueue() and add_task() behaviour with immediate and delayed enqueuing."""

    async def test_enqueue_puts_task_in_queue(self):
        """enqueue() increments pending_count by 1."""
        q = _make_queue()
        task = make_task()
        await q.enqueue(task)
        assert q.pending_count == 1

    async def test_add_task_no_delay_enqueues_immediately(self):
        """add_task() with delay=None enqueues the task immediately."""
        q = _make_queue()
        task = make_task()
        await q.add_task(task, delay=None)
        assert q.pending_count == 1

    async def test_add_task_zero_delay_enqueues_immediately(self):
        """add_task() with delay=timedelta(seconds=0) enqueues the task without waiting."""
        q = _make_queue()
        task = make_task()
        await q.add_task(task, delay=timedelta(seconds=0))
        assert q.pending_count == 1

    async def test_add_task_positive_delay_does_not_enqueue_immediately(self):
        """add_task() with a positive delay does not enqueue immediately (task is asleep in a background coroutine)."""
        q = _make_queue()
        task = make_task()
        await q.add_task(task, delay=timedelta(seconds=60))
        # Not yet enqueued — the coroutine is sleeping
        assert q.pending_count == 0
        # Clean up the sleeping background task
        for bg in list(q._background_tasks):
            bg.cancel()

    async def test_add_task_with_delay_enqueues_after_delay(self):
        """add_task() enqueues the task once the delay has elapsed."""
        q = _make_queue()
        task = make_task()
        # Capture real sleep before patching so we can actually yield
        real_sleep = asyncio.sleep

        async def _instant_sleep(*_args):
            """Replace asyncio.sleep with a zero-duration yield so tests don't block."""
            await real_sleep(0)

        with patch.object(asyncio, "sleep", _instant_sleep):
            await q.add_task(task, delay=timedelta(seconds=5))
            # Give the background coroutine time to run past the patched sleep
            await real_sleep(0)
            await real_sleep(0)
        assert q.pending_count == 1

    async def test_delayed_enqueue_skipped_after_stop(self):
        """A delayed task must not be enqueued if the queue stops before the delay expires."""
        q = _make_queue()
        task = make_task()
        await q.start()
        # Schedule a very long delay
        await q.add_task(task, delay=timedelta(hours=10))
        await q.stop()
        # The queue was stopped, so nothing should be in it
        assert q.pending_count == 0


# ---------------------------------------------------------------------------
# Worker task processing
# ---------------------------------------------------------------------------


class TestWorkerProcessing:
    """Verify the worker loop processes tasks, respects max_concurrency, and survives processor exceptions."""

    async def test_worker_processes_enqueued_task(self):
        """The worker loop calls processor.process() for each enqueued task."""
        processed: list = []

        async def _process(task):
            """Record the job_id to confirm the task was processed."""
            processed.append(task.job_id)

        processor = MagicMock()
        processor.process = _process
        factory = MagicMock(return_value=processor)
        q = _make_queue(processor_factory=factory)
        task = make_task()

        await q.start()
        await q.enqueue(task)
        await asyncio.sleep(0.05)
        await q.stop()

        assert task.job_id in processed

    async def test_worker_processes_multiple_tasks(self):
        """The worker loop processes all enqueued tasks, in order."""
        processed: list = []

        async def _process(task):
            """Record each processed job_id."""
            processed.append(task.job_id)

        processor = MagicMock()
        processor.process = _process
        factory = MagicMock(return_value=processor)
        q = _make_queue(processor_factory=factory)

        tasks = [make_task() for _ in range(5)]
        await q.start()
        for t in tasks:
            await q.enqueue(t)
        await asyncio.sleep(0.1)
        await q.stop()

        assert {t.job_id for t in tasks} == set(processed)

    async def test_worker_respects_max_concurrency(self):
        """No more than max_concurrency processors run simultaneously."""
        concurrency_peak = 0
        current_running = 0

        async def _slow_process(task):
            """Track the concurrency peak while simulating a slow delivery operation."""
            nonlocal concurrency_peak, current_running
            current_running += 1
            concurrency_peak = max(concurrency_peak, current_running)
            await asyncio.sleep(0.02)
            current_running -= 1

        processor = MagicMock()
        processor.process = _slow_process
        factory = MagicMock(return_value=processor)
        q = _make_queue(max_concurrency=2, processor_factory=factory)

        tasks = [make_task() for _ in range(6)]
        await q.start()
        for t in tasks:
            await q.enqueue(t)
        await asyncio.sleep(0.5)
        await q.stop()

        assert concurrency_peak <= 2

    async def test_processor_exception_does_not_crash_worker(self):
        """An exception in processor.process() must not kill the worker loop."""
        error_factory = _make_processor_factory(raises=RuntimeError("boom"))
        q = _make_queue(processor_factory=error_factory)
        tasks = [make_task() for _ in range(3)]

        await q.start()
        for t in tasks:
            await q.enqueue(t)
        await asyncio.sleep(0.1)

        # Worker loop must still be alive
        assert q.is_running
        await q.stop()

    async def test_task_done_called_after_processing(self):
        """queue.task_done() is called after each dequeued item, allowing queue.join() to unblock."""
        q = _make_queue()
        task = make_task()
        await q.start()
        await q.enqueue(task)
        await asyncio.sleep(0.05)
        await q.stop()
        # If task_done() was not called, queue.join() would block forever.
        # We verify indirectly: join() should complete without hanging.
        await asyncio.wait_for(q._queue.join(), timeout=0.1)


# ---------------------------------------------------------------------------
# Retry poller
# ---------------------------------------------------------------------------


class TestRetryPoller:
    """Verify the retry poller loop: due retries re-enqueued, stale retries dropped, future retries skipped."""

    async def test_poller_enqueues_due_retry(self):
        """A retry whose scheduled_at is in the past must be re-enqueued."""
        job_id = uuid4()
        snap = _make_fresh_snapshot()
        past = datetime.now(UTC) - timedelta(seconds=1)

        mock_rq = MagicMock()
        mock_rq.get_pending_retries = AsyncMock(return_value=[(job_id, past, snap)])
        mock_rq.remove_retry = AsyncMock()

        enqueued: list = []
        q = _make_queue(retry_queue=mock_rq)

        original_enqueue = q.enqueue

        async def _capture_enqueue(task):
            """Record enqueued tasks for assertion, then forward to the real enqueue."""
            enqueued.append(task)
            await original_enqueue(task)

        q.enqueue = _capture_enqueue  # type: ignore[method-assign]

        real_sleep = asyncio.sleep
        iteration = 0

        async def _one_shot_sleep(*_args):
            """Stop the poller after the first sleep iteration."""
            nonlocal iteration
            iteration += 1
            if iteration >= 1:
                q._stopped.set()
            await real_sleep(0)

        with patch.object(asyncio, "sleep", _one_shot_sleep):
            await q._retry_poller_loop()

        mock_rq.remove_retry.assert_awaited()

    async def test_poller_drops_stale_retry(self):
        """A retry whose notification exceeds _MAX_RETRY_AGE must be dropped."""
        job_id = uuid4()
        snap = _make_stale_snapshot()
        past = datetime.now(UTC) - timedelta(seconds=1)

        mock_rq = MagicMock()
        mock_rq.get_pending_retries = AsyncMock(return_value=[(job_id, past, snap)])
        mock_rq.remove_retry = AsyncMock()

        enqueued: list = []
        q = _make_queue(retry_queue=mock_rq)

        async def _capture_enqueue(task):  # noqa: RUF029
            """Record enqueued tasks for assertion (stale tasks should not appear here)."""
            enqueued.append(task)

        q.enqueue = _capture_enqueue  # type: ignore[method-assign]

        real_sleep = asyncio.sleep
        iteration = 0

        async def _one_shot_sleep(*_args):
            """Stop the poller after the first sleep iteration."""
            nonlocal iteration
            iteration += 1
            if iteration >= 1:
                q._stopped.set()
            await real_sleep(0)

        with patch.object(asyncio, "sleep", _one_shot_sleep):
            await q._retry_poller_loop()

        # Stale task must not reach the queue
        assert enqueued == []
        # But must be removed from the retry store
        mock_rq.remove_retry.assert_awaited()

    async def test_poller_skips_retry_not_yet_due(self):
        """A retry scheduled in the future must not be enqueued yet."""
        job_id = uuid4()
        snap = _make_fresh_snapshot()
        future = datetime.now(UTC) + timedelta(hours=1)

        mock_rq = MagicMock()
        mock_rq.get_pending_retries = AsyncMock(return_value=[(job_id, future, snap)])
        mock_rq.remove_retry = AsyncMock()

        enqueued: list = []
        q = _make_queue(retry_queue=mock_rq)

        async def _capture_enqueue(task):  # noqa: RUF029
            """Record tasks that the poller tried to enqueue."""
            enqueued.append(task)

        q.enqueue = _capture_enqueue  # type: ignore[method-assign]

        real_sleep = asyncio.sleep
        iteration = 0

        async def _one_shot_sleep(*_args):
            """Stop the poller after the first sleep iteration."""
            nonlocal iteration
            iteration += 1
            if iteration >= 1:
                q._stopped.set()
            await real_sleep(0)

        with patch.object(asyncio, "sleep", _one_shot_sleep):
            await q._retry_poller_loop()

        assert enqueued == []
        mock_rq.remove_retry.assert_not_awaited()

    async def test_poller_removes_invalid_snapshot(self):
        """A retry whose snapshot cannot be deserialised must be removed, not crashed."""
        job_id = uuid4()
        bad_snap: dict = {"payload": {}}  # missing required fields
        past = datetime.now(UTC) - timedelta(seconds=1)

        mock_rq = MagicMock()
        mock_rq.get_pending_retries = AsyncMock(return_value=[(job_id, past, bad_snap)])
        mock_rq.remove_retry = AsyncMock()

        q = _make_queue(retry_queue=mock_rq)

        real_sleep = asyncio.sleep
        iteration = 0

        async def _one_shot_sleep(*_args):
            """Stop the poller after the first sleep iteration."""
            nonlocal iteration
            iteration += 1
            if iteration >= 1:
                q._stopped.set()
            await real_sleep(0)

        with patch.object(asyncio, "sleep", _one_shot_sleep):
            await q._retry_poller_loop()

        # Must still remove the broken entry
        mock_rq.remove_retry.assert_awaited_with(job_id)

    async def test_poller_survives_get_pending_retries_exception(self):
        """An exception from get_pending_retries must not kill the poller loop."""
        call_count = 0

        async def _fail_then_succeed():
            """Raise RuntimeError on the first call; return an empty list on subsequent calls."""
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("storage unavailable")
            return []

        mock_rq = MagicMock()
        mock_rq.get_pending_retries = _fail_then_succeed
        mock_rq.remove_retry = AsyncMock()

        q = _make_queue(retry_queue=mock_rq)

        real_sleep = asyncio.sleep
        iteration = 0

        async def _two_shot_sleep(*_args):
            """Stop the poller after the second sleep iteration, allowing it to recover after the first failure."""
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                q._stopped.set()
            await real_sleep(0)

        # Run for 2 iterations so we see the recovery after the first failure
        with patch.object(asyncio, "sleep", _two_shot_sleep):
            await q._retry_poller_loop()

        assert call_count >= 2, "Poller must have recovered and retried"

    async def test_poller_silent_when_no_pending_retries(self):
        """Poller must not crash or log errors when the retry queue is empty."""
        mock_rq = MagicMock()
        mock_rq.get_pending_retries = AsyncMock(return_value=[])
        mock_rq.remove_retry = AsyncMock()

        q = _make_queue(retry_queue=mock_rq)

        real_sleep = asyncio.sleep
        iteration = 0

        async def _one_shot_sleep(*_args):
            """Stop the poller after the first sleep iteration."""
            nonlocal iteration
            iteration += 1
            if iteration >= 1:
                q._stopped.set()
            await real_sleep(0)

        with patch.object(asyncio, "sleep", _one_shot_sleep):
            await q._retry_poller_loop()

        mock_rq.remove_retry.assert_not_awaited()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Verify edge cases: stop before start, idempotent stop, and enqueue after stop."""

    async def test_stop_without_start_is_safe(self):
        """Calling stop() before start() must not raise."""
        q = _make_queue()
        await q.stop()  # must not raise

    async def test_stop_is_idempotent(self):
        """Calling stop() twice does not raise."""
        q = _make_queue()
        await q.start()
        await q.stop()
        await q.stop()  # second stop must not raise

    async def test_enqueue_after_stop_does_not_process(self):
        """Items enqueued after stop() must not be processed by the old worker."""
        processed: list = []

        async def _track(task):
            """Record each processed job_id."""
            processed.append(task.job_id)

        processor = MagicMock()
        processor.process = _track
        factory = MagicMock(return_value=processor)
        q = _make_queue(processor_factory=factory)

        await q.start()
        await q.stop()
        task = make_task()
        await q.enqueue(task)
        await asyncio.sleep(0.05)

        assert task.job_id not in processed


# ---------------------------------------------------------------------------
# Queue depth limit / backpressure
# ---------------------------------------------------------------------------


class TestQueueDepthInit:
    """Verify queue_max_depth parameter validation in __init__."""

    def test_valid_queue_max_depth_does_not_raise(self):
        """NotificationDeliveryTaskQueue() does not raise for valid queue_max_depth values."""
        _make_queue(queue_max_depth=10)
        _make_queue(queue_max_depth=500)
        _make_queue(queue_max_depth=5000)

    def test_zero_queue_max_depth_raises(self):
        """NotificationDeliveryTaskQueue() raises ValueError when queue_max_depth is 0."""
        with pytest.raises(ValueError, match="queue_max_depth"):
            _make_queue(queue_max_depth=0)

    def test_negative_queue_max_depth_raises(self):
        """NotificationDeliveryTaskQueue() raises ValueError when queue_max_depth is negative."""
        with pytest.raises(ValueError, match="queue_max_depth"):
            _make_queue(queue_max_depth=-1)

    def test_repr_includes_max_depth(self):
        """__repr__() output includes the max_depth field."""
        q = _make_queue(queue_max_depth=42)
        assert "max_depth=42" in repr(q)


class TestQueueDepthLimit:
    """Verify backpressure behaviour when the queue reaches its configured max depth."""

    async def test_queue_accepts_tasks_up_to_max_depth(self):
        """pending_count equals max_depth after filling exactly to capacity."""
        q = _make_queue(queue_max_depth=3)
        for _ in range(3):
            await q.enqueue(make_task())
        assert q.pending_count == 3

    async def test_queue_full_drops_task_without_raising(self, caplog):
        """enqueue() on a full queue logs a warning and does not raise."""
        q = _make_queue(queue_max_depth=2)
        await q.enqueue(make_task())
        await q.enqueue(make_task())

        overflow_task = make_task()
        with caplog.at_level(logging.WARNING):
            await q.enqueue(overflow_task)  # must not raise

        assert q.pending_count == 2  # queue did not grow beyond max
        assert any("full" in record.message.lower() for record in caplog.records)

    async def test_queue_full_calls_on_queue_full_callback(self):
        """on_queue_full callback is invoked with the dropped task when queue is full."""
        dropped: list = []

        q = _make_queue(queue_max_depth=1, on_queue_full=dropped.append)
        await q.enqueue(make_task())  # fills the queue

        overflow_task = make_task()
        await q.enqueue(overflow_task)

        assert len(dropped) == 1
        assert dropped[0].job_id == overflow_task.job_id

    async def test_queue_full_no_callback_does_not_raise(self):
        """enqueue() on a full queue without on_queue_full set does not raise."""
        q = _make_queue(queue_max_depth=1)
        await q.enqueue(make_task())
        await q.enqueue(make_task())  # must not raise even with no callback

    async def test_delayed_enqueue_drops_on_full(self):
        """_delayed_enqueue drops the task (via enqueue()) when the queue is full."""
        dropped: list = []
        q = _make_queue(queue_max_depth=1, on_queue_full=dropped.append)
        await q.enqueue(make_task())  # fill the queue

        real_sleep = asyncio.sleep

        async def _instant_sleep(*_args):
            await real_sleep(0)

        overflow_task = make_task()
        with patch.object(asyncio, "sleep", _instant_sleep):
            await q._delayed_enqueue(overflow_task, timedelta(seconds=1))
            await real_sleep(0)

        assert len(dropped) == 1
        assert dropped[0].job_id == overflow_task.job_id


class TestRetryPollerDeferOnFull:
    """Verify that retry tasks are deferred (not dropped) when the queue is full."""

    async def test_poller_defers_retry_when_queue_full(self):
        """remove_retry is NOT called when the queue is full — task stays in retry store."""
        job_id = uuid4()
        snap = _make_fresh_snapshot()
        past = datetime.now(UTC) - timedelta(seconds=1)

        mock_rq = MagicMock()
        mock_rq.get_pending_retries = AsyncMock(return_value=[(job_id, past, snap)])
        mock_rq.remove_retry = AsyncMock()

        # Fill the queue so put_nowait raises QueueFull
        q = _make_queue(retry_queue=mock_rq, queue_max_depth=1)
        await q.enqueue(make_task())  # fill to capacity

        real_sleep = asyncio.sleep
        iteration = 0

        async def _one_shot_sleep(*_args):
            nonlocal iteration
            iteration += 1
            if iteration >= 1:
                q._stopped.set()
            await real_sleep(0)

        with patch.object(asyncio, "sleep", _one_shot_sleep):
            await q._retry_poller_loop()

        # Task must NOT have been removed from persistent store
        mock_rq.remove_retry.assert_not_awaited()

    async def test_poller_removes_retry_after_successful_enqueue(self):
        """remove_retry IS called once the queue has room and the task is handed off."""
        job_id = uuid4()
        snap = _make_fresh_snapshot()
        past = datetime.now(UTC) - timedelta(seconds=1)

        mock_rq = MagicMock()
        mock_rq.get_pending_retries = AsyncMock(return_value=[(job_id, past, snap)])
        mock_rq.remove_retry = AsyncMock()

        # Queue has space (not pre-filled)
        q = _make_queue(retry_queue=mock_rq, queue_max_depth=10)

        real_sleep = asyncio.sleep
        iteration = 0

        async def _one_shot_sleep(*_args):
            nonlocal iteration
            iteration += 1
            if iteration >= 1:
                q._stopped.set()
            await real_sleep(0)

        with patch.object(asyncio, "sleep", _one_shot_sleep):
            await q._retry_poller_loop()

        mock_rq.remove_retry.assert_awaited_once_with(job_id)
