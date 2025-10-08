"""Asynchronous task queue for notification delivery.

Manages concurrent delivery processing with configurable concurrency limits.
"""

import asyncio
import logging
from collections.abc import Callable

from .models import NotificationDeliveryTask
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
    ):
        """Initialize the task queue.

        Args:
            max_concurrency: Maximum number of concurrent deliveries.
            processor_factory: Callable that returns a NotificationDeliveryProcessor.

        """
        self._queue: asyncio.Queue[NotificationDeliveryTask] = asyncio.Queue()
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._processor_factory = processor_factory
        self._worker_task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    # --------------------
    # Lifecycle
    # --------------------

    async def start(self) -> None:
        """Start the worker loop."""
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker_loop())
            _LOGGER.info("ANS task queue started")

    async def stop(self) -> None:
        """Stop the worker loop and wait for completion."""
        self._stopped.set()
        if self._worker_task:
            await self._worker_task
            _LOGGER.info("ANS task queue stopped")

    # --------------------
    # Public API
    # --------------------

    async def enqueue(self, task: NotificationDeliveryTask) -> None:
        """Enqueue a delivery task for processing.

        Args:
            task: Delivery task to process.

        """
        await self._queue.put(task)

    # --------------------
    # Worker loop
    # --------------------

    async def _worker_loop(self) -> None:
        """Process tasks from the queue in a loop."""
        while not self._stopped.is_set():
            try:
                # Wait for next task with a timeout to check stop flag periodically
                task = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                background_task = asyncio.create_task(self._run_task(task))
                # Let task run in background without blocking
                _ = background_task
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

            except Exception:
                _LOGGER.exception(
                    "Unhandled exception while processing task job_id=%s",
                    task.job_id,
                )
            finally:
                self._queue.task_done()
