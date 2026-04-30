"""Asynchronous task queue for notification delivery.

Manages concurrent delivery processing with configurable concurrency limits.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ..models import NotificationDeliveryTask
from ..persistence.file import RetryQueue
from .processor import NotificationDeliveryProcessor

_LOGGER = logging.getLogger(__name__)

#: Maximum notification age for retry eligibility.  Retries for notifications
#: older than this threshold are silently dropped to avoid waking a user hours
#: after the original event, or delivering a message after a DND window has
#: long since closed.
_MAX_RETRY_AGE = timedelta(hours=2)


class NotificationDeliveryTaskQueue:
    """Task queue for asynchronous notification delivery processing.

    Manages a worker pool that processes delivery tasks with configurable
    concurrency.  Each :meth:`add_task` call enqueues a
    :class:`~..models.NotificationDeliveryTask`; a single background worker
    dequeues tasks and dispatches them to concurrently-running processor
    coroutines capped by *max_concurrency*.

    Lifecycle
    ---------
    1. ``await queue.start()`` — starts the worker loop and (optionally) the
       retry-poller loop.
    2. ``await queue.stop()``  — cancels all background tasks and drains
       in-flight work.
    3. ``await queue.start()`` may be called again after ``stop()`` to resume
       processing (e.g. after a config reload).
    """

    def __init__(
        self,
        *,
        max_concurrency: int,
        processor_factory: Callable[[], NotificationDeliveryProcessor],
        retry_queue: RetryQueue | None = None,
    ) -> None:
        """Initialise the task queue.

        Args:
            max_concurrency: Maximum number of concurrent delivery coroutines.
                Must be a positive integer.
            processor_factory: Zero-argument callable that returns a fresh
                :class:`NotificationDeliveryProcessor` for each worker task.
            retry_queue: Optional :class:`~..persistence.file.RetryQueue` used
                by the retry-poller loop to re-enqueue overdue retries.

        Raises:
            ValueError: If *max_concurrency* is not a positive integer.

        """
        if max_concurrency < 1:
            raise ValueError(
                f"max_concurrency must be a positive integer, got {max_concurrency}"
            )
        self._queue: asyncio.Queue[NotificationDeliveryTask] = asyncio.Queue()
        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._active_tasks = 0
        self._processor_factory = processor_factory
        self._worker_task: asyncio.Task | None = None
        self._retry_poller_task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self._retry_queue = retry_queue
        self._background_tasks: set[asyncio.Task] = set()

    # --------------------
    # Lifecycle
    # --------------------

    async def start(self) -> None:
        """Start the worker loop and, if a retry queue was provided, the retry poller.

        Safe to call after :meth:`stop`; the internal stop flag and task
        references are reset automatically so processing resumes cleanly.
        """
        # Reset stop flag and stale task references so start() is idempotent
        # across stop()/start() cycles (e.g. config reload).
        self._stopped.clear()
        if self._worker_task is not None and self._worker_task.done():
            self._worker_task = None
        if self._retry_poller_task is not None and self._retry_poller_task.done():
            self._retry_poller_task = None

        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker_loop())
            _LOGGER.info("ANS task queue started")

        if self._retry_queue and self._retry_poller_task is None:
            self._retry_poller_task = asyncio.create_task(self._retry_poller_loop())
            _LOGGER.info("ANS retry queue poller started")

    async def stop(self) -> None:
        """Stop the worker loop, retry poller, and all in-flight background tasks.

        After this call the queue is quiescent.  All running delivery coroutines
        are allowed to complete naturally (via semaphore drain); only the worker
        and poller *control* tasks are cancelled.  :meth:`start` can be called
        again afterwards.
        """
        self._stopped.set()

        # Cancel delayed-enqueue background tasks that may be sleeping.
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        # Cancel the worker and poller immediately instead of waiting up to
        # 1 s (_worker_loop timeout) or 10 s (_retry_poller_loop sleep) for
        # the stop flag to be re-checked at the top of the loop.
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None
            _LOGGER.info("ANS task queue stopped")
        if self._retry_poller_task:
            self._retry_poller_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._retry_poller_task
            self._retry_poller_task = None
            _LOGGER.info("ANS retry queue poller stopped")

    # --------------------
    # Diagnostics
    # --------------------

    @property
    def pending_count(self) -> int:
        """Number of tasks currently waiting in the queue (not yet dispatched)."""
        return self._queue.qsize()

    @property
    def active_task_count(self) -> int:
        """Number of delivery coroutines currently executing."""
        return self._active_tasks

    @property
    def is_running(self) -> bool:
        """``True`` if the worker loop is active and not stopped."""
        return (
            not self._stopped.is_set()
            and self._worker_task is not None
            and not self._worker_task.done()
        )

    def __repr__(self) -> str:
        """Diagnostic string representation of the queue state."""
        return (
            f"<{type(self).__name__} "
            f"running={self.is_running} "
            f"pending={self.pending_count} "
            f"active={self.active_task_count} "
            f"max_concurrency={self._max_concurrency}>"
        )

    # --------------------
    # Public API
    # --------------------

    async def enqueue(self, task: NotificationDeliveryTask) -> None:
        """Enqueue a delivery task for immediate processing.

        Args:
            task: Delivery task to enqueue.

        """
        await self._queue.put(task)

    async def add_task(
        self, task: NotificationDeliveryTask, delay: timedelta | None = None
    ) -> None:
        """Add a delivery task to the queue, optionally after a delay.

        Args:
            task: Delivery task to process.
            delay: If provided *and* positive, the task is scheduled for
                enqueuing after this duration.  Pass ``None`` or a
                non-positive timedelta for immediate enqueuing.

        """
        if delay and delay.total_seconds() > 0:
            task_ref = asyncio.create_task(self._delayed_enqueue(task, delay))
            self._background_tasks.add(task_ref)
            task_ref.add_done_callback(self._background_tasks.discard)
        else:
            await self.enqueue(task)

    # --------------------
    # Worker loop
    # --------------------

    async def _delayed_enqueue(
        self, task: NotificationDeliveryTask, delay: timedelta
    ) -> None:
        """Sleep for *delay* then enqueue *task* unless already stopped.

        Args:
            task: Task to enqueue.
            delay: Duration to wait before enqueuing.

        """
        await asyncio.sleep(delay.total_seconds())
        if not self._stopped.is_set():
            await self.enqueue(task)
            _LOGGER.debug(
                "Delayed task enqueued: job_id=%s (delayed by %s)",
                task.job_id,
                delay,
            )

    async def _retry_poller_loop(self) -> None:
        """Poll the retry queue every 10 seconds and re-enqueue overdue tasks."""
        while not self._stopped.is_set():
            pending_count = 0
            last_job_id = None
            try:
                await asyncio.sleep(10)

                if not self._retry_queue:
                    continue

                pending_retries = await self._retry_queue.get_pending_retries()
                pending_count = len(pending_retries)
                if pending_count > 0:
                    _LOGGER.debug(
                        "Retry poller ran: %d pending entries checked",
                        pending_count,
                    )
                now = datetime.now(UTC)

                for job_id, scheduled_at, task_snapshot in pending_retries:
                    last_job_id = job_id
                    if scheduled_at > now:
                        continue

                    # Max-age guard: drop retries for notifications older than
                    # _MAX_RETRY_AGE to avoid stale deliveries (e.g. waking a
                    # user hours after the original event, or bypassing a DND
                    # window that has since closed).
                    try:
                        notification_ts = datetime.fromisoformat(
                            task_snapshot["payload"]["timestamp"]
                        )
                        if now - notification_ts > _MAX_RETRY_AGE:
                            _LOGGER.warning(
                                "Dropping stale retry for job_id=%s: notification "
                                "age exceeds %s (notification_id=%s)",
                                job_id,
                                _MAX_RETRY_AGE,
                                task_snapshot.get("payload", {}).get(
                                    "notification_id", "unknown"
                                ),
                            )
                            await self._retry_queue.remove_retry(job_id)
                            continue
                    except (KeyError, ValueError):
                        pass  # Malformed/old snapshot; proceed to from_snapshot()

                    _LOGGER.info(
                        "Executing pending retry for job_id=%s (scheduled at %s)",
                        job_id,
                        scheduled_at.isoformat(),
                    )

                    try:
                        task = NotificationDeliveryTask.from_snapshot(
                            job_id, task_snapshot
                        )
                        await self._retry_queue.remove_retry(job_id)
                        await self.enqueue(task)
                    except (ValueError, KeyError, AttributeError) as exc:
                        _LOGGER.error(
                            "Failed to reconstruct/enqueue retry task %s: %s",
                            job_id,
                            exc,
                        )
                        await self._retry_queue.remove_retry(job_id)

            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Error in retry poller loop (pending_count=%d, last_job_id=%s)",
                    pending_count,
                    last_job_id,
                )

    async def _worker_loop(self) -> None:
        """Dequeue tasks and dispatch them as concurrent background coroutines.

        Uses an event-driven shutdown: waits on either a new queue item *or*
        the ``_stopped`` event, whichever arrives first, so the loop exits
        promptly on :meth:`stop` without a polling delay.
        """
        stop_waiter: asyncio.Future = asyncio.ensure_future(self._stopped.wait())
        try:
            while not self._stopped.is_set():
                get_waiter: asyncio.Task = asyncio.create_task(self._queue.get())
                done, _ = await asyncio.wait(
                    {get_waiter, stop_waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_waiter in done:
                    get_waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await get_waiter
                    break
                task = get_waiter.result()
                background_task = asyncio.create_task(self._run_task(task))
                self._background_tasks.add(background_task)
                background_task.add_done_callback(self._background_tasks.discard)
        finally:
            stop_waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_waiter

    async def _run_task(self, task: NotificationDeliveryTask) -> None:
        """Acquire the concurrency semaphore and process a single delivery task.

        Args:
            task: Task to process.

        """
        try:
            async with self._semaphore:
                self._active_tasks += 1
                try:
                    processor = self._processor_factory()
                    await processor.process(task)
                except Exception:  # noqa: BLE001
                    # Last-resort handler: processor.process() has its own error
                    # handling and logs; this only fires for truly unexpected
                    # exceptions (e.g. programming errors) that escape the
                    # processor entirely.
                    _LOGGER.exception(
                        "Unhandled exception while processing task job_id=%s "
                        "notification_id=%s",
                        task.job_id,
                        task.payload.notification_id,
                    )
                finally:
                    self._active_tasks -= 1
        finally:
            self._queue.task_done()
