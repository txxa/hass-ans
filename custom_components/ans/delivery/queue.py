"""Asynchronous task queue for notification delivery.

Manages concurrent delivery processing with configurable concurrency limits.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ..models import NotificationDeliveryTask
from .processor import NotificationDeliveryProcessor

_LOGGER = logging.getLogger(__name__)


class NotificationDeliveryTaskQueue:
    """Task queue for asynchronous notification delivery processing.

    Manages a worker pool that processes delivery tasks with configurable concurrency.
    """

    def __init__(
        self,
        *,
        max_concurrency: int,
        processor_factory: Callable[[], NotificationDeliveryProcessor],
        retry_queue=None,
    ):
        """Initialize the task queue.

        Args:
            max_concurrency: Maximum number of concurrent deliveries.
            processor_factory: Callable that returns a NotificationDeliveryProcessor.
            retry_queue: Optional RetryQueue instance for polling pending retries.

        """
        self._queue: asyncio.Queue[NotificationDeliveryTask] = asyncio.Queue()
        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
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
        """Start the worker loop and retry poller."""
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker_loop())
            _LOGGER.info("ANS task queue started")

        if self._retry_queue and self._retry_poller_task is None:
            self._retry_poller_task = asyncio.create_task(self._retry_poller_loop())
            _LOGGER.info("ANS retry queue poller started")

    async def stop(self) -> None:
        """Stop the worker loop and retry poller, wait for completion."""
        self._stopped.set()
        if self._worker_task:
            await self._worker_task
            _LOGGER.info("ANS task queue stopped")
        if self._retry_poller_task:
            await self._retry_poller_task
            _LOGGER.info("ANS retry queue poller stopped")

    # --------------------
    # Public API
    # --------------------

    async def enqueue(self, task: NotificationDeliveryTask) -> None:
        """Enqueue a delivery task for processing.

        Args:
            task: Delivery task to process.

        """
        await self._queue.put(task)

    async def add_task(
        self, task: NotificationDeliveryTask, delay: timedelta | None = None
    ) -> None:
        """Add a task to the queue, optionally with a delay.

        Args:
            task: Delivery task to process.
            delay: Optional delay before processing.

        """
        if delay and delay.total_seconds() > 0:
            # Schedule delayed execution and track task
            task_ref = asyncio.create_task(self._delayed_enqueue(task, delay))
            self._background_tasks.add(task_ref)
            task_ref.add_done_callback(self._background_tasks.discard)
        else:
            # Immediate execution
            await self.enqueue(task)

    # --------------------
    # Worker loop
    # --------------------

    async def _delayed_enqueue(
        self, task: NotificationDeliveryTask, delay: timedelta
    ) -> None:
        """Enqueue a task after a delay.

        Args:
            task: Task to enqueue.
            delay: Delay before enqueuing.

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
        """Poll retry queue and enqueue ready tasks."""
        while not self._stopped.is_set():
            _pending_count = 0
            _last_job_id = None
            try:
                # Check every 10 seconds for pending retries
                await asyncio.sleep(10)

                if not self._retry_queue:
                    continue

                pending_retries = await self._retry_queue.get_pending_retries()
                _pending_count = len(pending_retries)
                # Only log when there's work to report — silent at idle to avoid noise
                if _pending_count > 0:
                    _LOGGER.debug(
                        "Retry poller ran: %d pending entries checked",
                        _pending_count,
                    )
                now = datetime.now(UTC)

                for job_id, scheduled_at, task_snapshot in pending_retries:
                    _last_job_id = job_id
                    # Check if retry is due
                    if scheduled_at <= now:
                        _LOGGER.info(
                            "Executing pending retry for job_id=%s (scheduled at %s)",
                            job_id,
                            scheduled_at.isoformat(),
                        )

                        # Reconstruct task from snapshot
                        try:
                            task = NotificationDeliveryTask.from_snapshot(
                                job_id, task_snapshot
                            )
                            # Remove from retry queue
                            await self._retry_queue.remove_retry(job_id)
                            # Enqueue for immediate execution
                            await self.enqueue(task)
                        except (ValueError, KeyError, AttributeError) as e:
                            _LOGGER.error(
                                "Failed to reconstruct/enqueue retry task %s: %s",
                                job_id,
                                e,
                            )
                            # Remove failed retry from queue
                            await self._retry_queue.remove_retry(job_id)

            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Error in retry poller loop (pending_count=%d, last_job_id=%s)",
                    _pending_count,
                    _last_job_id,
                )

    async def _worker_loop(self) -> None:
        """Process tasks from the queue in a loop."""
        while not self._stopped.is_set():
            try:
                # Wait for next task with a timeout to check stop flag periodically
                task = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                background_task = asyncio.create_task(self._run_task(task))
                self._background_tasks.add(background_task)
                background_task.add_done_callback(self._background_tasks.discard)
            except TimeoutError:
                # Check stopped flag and continue
                continue

    async def _run_task(self, task: NotificationDeliveryTask) -> None:
        """Process a single delivery task with concurrency limit.

        Args:
            task: Task to process.

        """
        async with self._semaphore:
            try:
                processor = self._processor_factory()
                await processor.process(task)

            except Exception:  # noqa: BLE001
                # Last-resort handler: processor.process() has its own error handling
                # and logs; this only fires for truly unexpected exceptions (e.g.
                # programming errors) that escape the processor entirely. It is NOT
                # double-logging in normal failure scenarios.
                _LOGGER.exception(
                    "Unhandled exception while processing task job_id=%s "
                    "notification_id=%s",
                    task.job_id,
                    task.payload.notification_id,
                )
            finally:
                self._queue.task_done()
