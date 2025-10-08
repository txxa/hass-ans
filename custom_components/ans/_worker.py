"""Delivery worker with persistent pending job store and resume-on-restart.

Key design points implemented here:
- Uses helpers.storage.Store for persisting pending jobs with a versioned payload.
- On start() the worker loads pending jobs and enqueues them for processing.
- When enqueue() is called the job is persisted immediately (durable acknowledgement) before
  being placed on the in-memory queue.
- On successful delivery or permanent failure the job is removed from the pending store.
- Migration hook demonstrates how to upgrade older store versions safely.

Limitations / TODO:
- The worker persists only the pending job queue. Completed job history is not stored here; if you
  need a long-term audit log, implement a separate retention-backed store.
- The per-identity rate-limiter is persisted separately in `rate_limiter.py`.
- The pending store does not trim itself; consider retention/compaction strategies.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage
from homeassistant.util import dt as dt_util

from .models import DeliveryJob

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 2
STORE_FILE = "ans_pending_deliveries.json"


class PersistentPendingStore:
    """Encapsulates a helpers.storage.Store for pending DeliveryJobs.

    Format:
    {
        "version": int,
        "jobs": [ {DeliveryJob.to_dict()}, ... ]
    }
    """

    def __init__(self, hass: HomeAssistant):
        self._store = storage.Store(hass, STORE_VERSION, STORE_FILE)

    async def async_load(self) -> list[DeliveryJob]:
        raw = await self._store.async_load()
        if not raw:
            return []
        version = int(raw.get("version", 1))
        jobs_raw = raw.get("jobs", [])
        # migration example
        if version < STORE_VERSION:
            jobs_raw = self._migrate(jobs_raw, version)
        return [DeliveryJob.from_dict(j) for j in jobs_raw]

    async def async_save(self, jobs: list[DeliveryJob]) -> None:
        payload = {"version": STORE_VERSION, "jobs": [j.to_dict() for j in jobs]}
        await self._store.async_save(payload)

    def _migrate(
        self, jobs_raw: list[dict[str, Any]], version: int
    ) -> list[dict[str, Any]]:
        # Example migration path; expand as needed when changing shapes.
        _LOGGER.info("Migrating pending store from v%s to v%s", version, STORE_VERSION)
        if version == 1:
            # In version 1 we stored next_try_ts as ISO string; convert to unix ts
            migrated: list[dict[str, Any]] = []
            for j in jobs_raw:
                nj = dict(j)
                nts = nj.get("next_try_ts")
                if isinstance(nts, str):
                    try:
                        from homeassistant.util import dt as dt_util

                        dt = dt_util.parse_datetime(nts)
                        nj["next_try_ts"] = dt.timestamp() if dt else None
                    except Exception:
                        nj["next_try_ts"] = None
                migrated.append(nj)
            return migrated
        return jobs_raw


class DeliveryWorker:
    def __init__(
        self, hass: HomeAssistant, *, concurrency: int = 4, default_timeout: int = 10
    ):
        self.hass = hass
        self._queue: asyncio.Queue[DeliveryJob] = asyncio.Queue()
        self._running = False
        self._task: asyncio.Task | None = None
        self._semaphore = asyncio.Semaphore(concurrency)
        self._default_timeout = default_timeout
        self._pending_store = PersistentPendingStore(hass)
        self._pending_index: dict[str, DeliveryJob] = {}

    async def start(self) -> None:
        if self._running:
            return
        _LOGGER.debug("Starting persistent DeliveryWorker")
        # load pending jobs from store and enqueue
        pending = await self._pending_store.async_load()
        for job in pending:
            self._pending_index[job.job_id] = job
            await self._queue.put(job)
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if not self._running:
            return
        _LOGGER.debug("Stopping DeliveryWorker: waiting for queue to drain")
        self._running = False
        if self._task:
            await self._task

    async def enqueue(self, job: DeliveryJob) -> None:
        # Persist immediately
        self._pending_index[job.job_id] = job
        await self._persist_pending()
        await self._queue.put(job)

    async def _persist_pending(self) -> None:
        await self._pending_store.async_save(list(self._pending_index.values()))

    async def _run_loop(self) -> None:
        while self._running:
            try:
                job: DeliveryJob = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except TimeoutError:
                continue

            # handle next_try_ts
            if job.next_try_ts:
                now_ts = dt_util.utcnow().timestamp()
                if job.next_try_ts > now_ts:
                    await asyncio.sleep(job.next_try_ts - now_ts)

            await self._semaphore.acquire()
            asyncio.create_task(self._execute_job(job))

    async def _execute_job(self, job: DeliveryJob) -> None:
        try:
            _LOGGER.debug(
                "Executing job %s (attempt %d/%d)",
                job.job_id,
                job.attempts + 1,
                job.max_attempts,
            )
            result = await self._attempt_delivery(job)
            if result:
                _LOGGER.info("Job %s delivered successfully", job.job_id)
                # remove from pending
                self._pending_index.pop(job.job_id, None)
                await self._persist_pending()
            else:
                job.attempts += 1
                if job.attempts < job.max_attempts:
                    backoff = min(300, (2**job.attempts) + random.random() * 2)
                    job.next_try_ts = dt_util.utcnow().timestamp() + backoff
                    _LOGGER.debug(
                        "Rescheduling job %s after %.1fs", job.job_id, backoff
                    )
                    # persist updated next_try_ts
                    self._pending_index[job.job_id] = job
                    await self._persist_pending()
                    await self._queue.put(job)
                else:
                    _LOGGER.error(
                        "Job %s exhausted retries — removed from pending", job.job_id
                    )
                    self._pending_index.pop(job.job_id, None)
                    await self._persist_pending()
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.exception(
                "Unhandled exception while executing job %s: %s", job.job_id, exc
            )
        finally:
            self._semaphore.release()

    async def _attempt_delivery(self, job: DeliveryJob) -> bool:
        domain, svc = job.channel.split(".", 1)
        if not self.hass.services.has_service(domain, svc):
            _LOGGER.warning(
                "Target service %s.%s not found for job %s", domain, svc, job.job_id
            )
            return False
        service_data = dict(job.data)
        service_data.setdefault("message", job.message)
        if job.title:
            service_data.setdefault("title", job.title)
        try:
            await asyncio.wait_for(
                self.hass.services.async_call(domain, svc, service_data, blocking=True),
                timeout=self._default_timeout,
            )
            return True
        except asyncio.TimeoutError:
            _LOGGER.warning("Delivery attempt for job %s timed out", job.job_id)
            return False
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.warning("Delivery attempt for job %s failed: %s", job.job_id, exc)
            return False
