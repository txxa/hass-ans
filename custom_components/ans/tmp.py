### File: custom_components/ans/notify/__init__.py

"""Notify subpackage init.

This file exposes the public setup/unload functions used by your main integration async_setup_entry
and async_unload_entry. It keeps wiring minimal and uses the other modules for implementation.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .service import async_setup_notify_provider, async_unload_notify_provider

__all__ = ["async_setup_notify_provider", "async_unload_notify_provider"]


### File: custom_components/ans/notify/models.py

"""Models for the ANS notify subsystem.

Includes DeliveryJob dataclass and simple (de)serialization helpers used by the persistence store.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, dict, Optional


@dataclass
class DeliveryJob:
    job_id: str
    identity_id: str
    channel: str
    title: Optional[str]
    message: str
    data: dict[str, Any]
    attempts: int = 0
    max_attempts: int = 3
    next_try_ts: float | None = None  # unix timestamp

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "DeliveryJob":
        return DeliveryJob(
            job_id=d["job_id"],
            identity_id=d["identity_id"],
            channel=d["channel"],
            title=d.get("title"),
            message=d.get("message", ""),
            data=d.get("data", {}),
            attempts=int(d.get("attempts", 0)),
            max_attempts=int(d.get("max_attempts", 3)),
            next_try_ts=d.get("next_try_ts"),
        )


### File: custom_components/ans/notify/worker.py

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
from typing import Any, dict, List, Optional

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

    async def async_load(self) -> List[DeliveryJob]:
        raw = await self._store.async_load()
        if not raw:
            return []
        version = int(raw.get("version", 1))
        jobs_raw = raw.get("jobs", [])
        # migration example
        if version < STORE_VERSION:
            jobs_raw = self._migrate(jobs_raw, version)
        return [DeliveryJob.from_dict(j) for j in jobs_raw]

    async def async_save(self, jobs: List[DeliveryJob]) -> None:
        payload = {"version": STORE_VERSION, "jobs": [j.to_dict() for j in jobs]}
        await self._store.async_save(payload)

    def _migrate(
        self, jobs_raw: List[dict[str, Any]], version: int
    ) -> List[dict[str, Any]]:
        # Example migration path; expand as needed when changing shapes.
        _LOGGER.info("Migrating pending store from v%s to v%s", version, STORE_VERSION)
        if version == 1:
            # In version 1 we stored next_try_ts as ISO string; convert to unix ts
            migrated: List[dict[str, Any]] = []
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
        self._task: Optional[asyncio.Task] = None
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
            except asyncio.TimeoutError:
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


### File: custom_components/ans/notify/rate_limiter.py (UPDATED — per-identity locks)

"""Persisted token-bucket rate limiter for ANS with per-identity locks.

Design:
- Store-backed mapping of identity_id -> token bucket state.
- Each bucket stores: capacity, tokens (float), refill_rate (tokens/sec), last_refill_ts (unix ts).
- Per-identity asyncio.Lock instances serialize concurrent updates to each bucket, allowing different
  identities to be processed concurrently without contention.
- A global save lock ensures writes to helpers.storage.Store are serialized.

API:
- async_load(): load persisted state into memory.
- async_save(): persist entire bucket map (used internally).
- consume(identity_id, capacity, window, tokens=1): attempt to consume tokens; returns True/False.

Notes:
- This implementation persists eagerly (awaiting async_save on each successful consume) to avoid
  losing allowance across restarts. If performance is a concern, implement batched persistence.
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.helpers import storage
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
STORE_FILE = "ans_token_buckets.json"


class PersistedTokenBucket:
    def __init__(self, hass):
        self._store = storage.Store(hass, STORE_VERSION, STORE_FILE)
        # in-memory bucket state: identity_id -> {capacity, tokens, refill_rate, last_refill_ts}
        self._state: dict[str, dict] = {}
        # per-identity locks to allow concurrent updates for different identities
        self._locks: dict[str, asyncio.Lock] = {}
        # global lock for managing _locks dict and persisting state
        self._locks_lock = asyncio.Lock()
        # serialize storage writes
        self._save_lock = asyncio.Lock()

    async def async_load(self) -> None:
        raw = await self._store.async_load()
        if not raw:
            self._state = {}
            return
        self._state = raw.get("buckets", {}) or {}

    async def async_save(self) -> None:
        async with self._save_lock:
            payload = {"buckets": self._state}
            await self._store.async_save(payload)

    async def _get_lock_for(self, identity_id: str) -> asyncio.Lock:
        # ensure a lock exists for identity_id in a thread-safe way
        async with self._locks_lock:
            lock = self._locks.get(identity_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[identity_id] = lock
            return lock

    async def consume(
        self, identity_id: str, capacity: int, window: int, tokens: int = 1
    ) -> bool:
        """Try to consume `tokens` from the bucket for `identity_id`.

        capacity: max tokens in bucket (identity.rate_limit)
        window: seconds window -> refill_rate = capacity / window
        tokens: tokens to consume (default 1)

        Returns True if tokens were consumed; False otherwise.
        """
        now_ts = dt_util.utcnow().timestamp()
        lock = await self._get_lock_for(identity_id)
        async with lock:
            state = self._state.get(identity_id)
            refill_rate = (
                float(capacity) / float(window) if window > 0 else float(capacity)
            )
            if not state:
                # initialize with full capacity
                state = {
                    "capacity": int(capacity),
                    "tokens": float(capacity),
                    "refill_rate": float(refill_rate),
                    "last_refill_ts": float(now_ts),
                }
                self._state[identity_id] = state
            else:
                # update capacity/refill if changed
                state["capacity"] = int(capacity)
                state["refill_rate"] = float(refill_rate)
            # refill
            last = float(state.get("last_refill_ts", now_ts))
            delta = max(0.0, now_ts - last)
            if delta > 0:
                refill_amount = delta * state["refill_rate"]
                state["tokens"] = min(
                    state["capacity"], state.get("tokens", 0.0) + refill_amount
                )
                state["last_refill_ts"] = float(now_ts)
            # attempt consume
            if state["tokens"] >= tokens:
                state["tokens"] -= tokens
                # persist after change
                try:
                    await self.async_save()
                except Exception:
                    _LOGGER.exception(
                        "Failed to persist token bucket after consume for %s",
                        identity_id,
                    )
                return True
            # not enough tokens
            try:
                await self.async_save()
            except Exception:
                _LOGGER.exception(
                    "Failed to persist token bucket state (no consume) for %s",
                    identity_id,
                )
            return False

    async def dump_state(self) -> dict[str, dict]:
        """Return a copy of current in-memory state (helpful for debugging/tests)."""
        # shallow copy is fine since values are primitives
        return dict(self._state)


### File: custom_components/ans/notify/routing.py

"""Routing pipeline for ANS — now wired to use the persisted token-bucket when provided.

The pipeline signature accepts an optional `rate_limiter` object exposing `consume(identity_id, capacity, window)`.
If none is provided the pipeline falls back to the in-memory placeholder limiter (not persisted).
"""
from __future__ import annotations

import re
import random
from typing import Any, dict, List, Tuple, Optional

from homeassistant.util import dt as dt_util

from .models import DeliveryJob
from .models import (
    Receiver,
    ReceiverConfig,
    NotificationCriticality,
    NotificationType,
)


def _parse_notification_type(value: Any) -> NotificationType:
    if isinstance(value, NotificationType):
        return value
    if isinstance(value, str):
        try:
            return NotificationType(value)
        except Exception:
            pass
    return NotificationType.INFO


def _parse_criticality(value: Any, ntype: NotificationType) -> NotificationCriticality:
    if isinstance(value, NotificationCriticality):
        return value
    if isinstance(value, str):
        try:
            return NotificationCriticality(value)
        except Exception:
            pass
    mapping = {
        NotificationType.INFO: NotificationCriticality.LOW,
        NotificationType.EVENT: NotificationCriticality.MEDIUM,
        NotificationType.REMINDER: NotificationCriticality.MEDIUM,
        NotificationType.WARNING: NotificationCriticality.HIGH,
        NotificationType.ALERT: NotificationCriticality.HIGH,
        NotificationType.SECURITY: NotificationCriticality.CRITICAL,
    }
    return mapping.get(ntype, NotificationCriticality.LOW)


def is_in_dnd(now, ic: ReceiverConfig) -> bool:
    if not ic.dnd_enabled or not ic.dnd_start or not ic.dnd_end:
        return False
    try:
        sh, sm = map(int, ic.dnd_start.split(":"))
        eh, em = map(int, ic.dnd_end.split(":"))
    except Exception:
        return False
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if start == end:
        return True
    if start < end:
        return start <= now < end
    return now >= start or now < end


def channels_for_criticality(
    ic: ReceiverConfig, criticality: NotificationCriticality
) -> List[str]:
    if criticality == NotificationCriticality.LOW:
        return list(ic.channels_low or [])
    if criticality == NotificationCriticality.MEDIUM:
        return list(ic.channels_medium or [])
    if criticality == NotificationCriticality.HIGH:
        return list(ic.channels_high or [])
    if criticality == NotificationCriticality.CRITICAL:
        return list(ic.channels_critical or [])
    return []


# Placeholder rate limiter state — intentionally small and in-memory. Replace with persisted bucket.
_rate_state = {}


def _check_rate_limit_placeholder(identity_id: str, ic: ReceiverConfig) -> bool:
    now_ts = int(dt_util.utcnow().timestamp())
    state = _rate_state.setdefault(identity_id, {"window_start": now_ts, "count": 0})
    window = getattr(ic, "rate_limit_window", 60)
    if now_ts - state["window_start"] >= window:
        state["window_start"] = now_ts
        state["count"] = 0
    if state["count"] >= getattr(ic, "rate_limit", 0):
        return False
    state["count"] += 1
    return True


async def run_routing_pipeline(
    config_repo,
    payload: dict[str, Any],
    rate_limiter: Optional[object] = None,
) -> List[DeliveryJob]:
    """Produce DeliveryJob list for payload.

    rate_limiter: optional object exposing `consume(identity_id, capacity, window)` coroutine.
    """
    message = payload.get("message", "")
    title = payload.get("title")
    source = payload.get("source", "unknown")
    ntype = _parse_notification_type(payload.get("notification_type"))
    criticality = _parse_criticality(payload.get("criticality"), ntype)

    jobs: List[DeliveryJob] = []

    identities: List[Tuple[Receiver, ReceiverConfig]] = config_repo.iter_identities()

    for identity, ic in identities:
        # filter by notification type
        if ntype not in ic.notification_types:
            continue
        # blocked sources
        if ic.blocked_sources_pattern:
            try:
                if re.search(ic.blocked_sources_pattern, source):
                    continue
            except re.error:
                continue
        # DND
        now = dt_util.now()
        if is_in_dnd(now, ic):
            allowed = False
            if ic.dnd_allowed_sources_pattern:
                try:
                    if re.search(ic.dnd_allowed_sources_pattern, source):
                        allowed = True
                except re.error:
                    pass
            if not allowed:
                continue
        # rate limit: use provided persistent limiter when available
        allowed = True
        if rate_limiter is not None:
            try:
                allowed = await rate_limiter.consume(
                    identity.id,
                    getattr(ic, "rate_limit", 0),
                    config_repo.system_config.rate_limit_window,
                )
            except Exception:
                # if the rate limiter fails, fall back to in-memory placeholder
                allowed = _check_rate_limit_placeholder(identity.id, ic)
        else:
            allowed = _check_rate_limit_placeholder(identity.id, ic)

        if not allowed:
            continue

        # channels
        chs = channels_for_criticality(ic, criticality)
        if not chs:
            continue
        for ch in chs:
            job = DeliveryJob(
                job_id=f"{identity.id}-{ch}-{int(dt_util.utcnow().timestamp())}-{random.randint(0, 9999)}",
                identity_id=identity.id,
                channel=ch,
                title=title,
                message=message,
                data=payload.get("data", {}),
                attempts=0,
                max_attempts=min(
                    ic.retry_attempts, config_repo.system_config.retry_attempts_max
                ),
            )
            jobs.append(job)
    return jobs


### File: custom_components/ans/notify/service.py

"""ANS notify service registration and integration glue.

This module wires the routing pipeline and persistent worker together. It registers the
`notify.ans` service via hass.services and ensures the worker and persisted rate limiter are
started and stopped with the config entry lifecycle.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.notify import BaseNotificationService
from homeassistant.core import HomeAssistant

from ._worker import DeliveryWorker
from ._routing import run_routing_pipeline
from .models import DeliveryJob
from .rate_limiter import PersistedTokenBucket
from .__init__ import get_config_repository

_LOGGER = logging.getLogger(__name__)


class ANSNotifyService(BaseNotificationService):
    def __init__(
        self,
        hass: HomeAssistant,
        config_entry_id: str,
        worker: DeliveryWorker | None = None,
        rate_limiter: PersistedTokenBucket | None = None,
    ):
        self.hass = hass
        self._entry_id = config_entry_id
        self._worker = worker
        self._rate_limiter = rate_limiter

    async def async_send_message(self, message: str = "", **kwargs: Any) -> None:
        data = kwargs.get("data") or {}
        title = kwargs.get("title")
        source = data.get("source") or "unknown"
        notification_type = data.get("notification_type")
        criticality = data.get("criticality")

        config_repo = get_config_repository(self.hass)
        if config_repo is None:
            _LOGGER.error("Config repository missing — cannot route notification")
            return

        payload = {
            "message": message,
            "title": title,
            "data": data,
            "source": source,
            "notification_type": notification_type,
            "criticality": criticality,
        }

        jobs = await run_routing_pipeline(
            config_repo, payload, rate_limiter=self._rate_limiter
        )
        if not jobs:
            _LOGGER.debug(
                "No jobs returned by routing pipeline for message: %s", message
            )
            return

        if self._worker is None:
            self._worker = DeliveryWorker(self.hass)
            await self._worker.start()

        for job in jobs:
            await self._worker.enqueue(job)


async def async_setup_notify_provider(hass: HomeAssistant, config_entry) -> None:
    entry_id = config_entry.entry_id

    # Create persisted rate limiter and load its state
    rate_limiter = PersistedTokenBucket(hass)
    await rate_limiter.async_load()

    worker = DeliveryWorker(hass)
    await worker.start()

    provider = ANSNotifyService(
        hass, entry_id, worker=worker, rate_limiter=rate_limiter
    )

    def _handle_call(call):
        asyncio.create_task(provider.async_send_message(**call.data))

    hass.services.async_register("notify", "ans", _handle_call)
    hass.data.setdefault("ans", {})[f"worker_{entry_id}"] = worker
    hass.data.setdefault("ans", {})[f"ratelimiter_{entry_id}"] = rate_limiter


async def async_unload_notify_provider(hass: HomeAssistant, config_entry) -> None:
    entry_id = config_entry.entry_id
    worker = hass.data.get("ans", {}).pop(f"worker_{entry_id}", None)
    rate_limiter = hass.data.get("ans", {}).pop(f"ratelimiter_{entry_id}", None)
    if worker:
        await worker.stop()
    # persist token bucket state on unload
    if rate_limiter:
        try:
            await rate_limiter.async_save()
        except Exception:
            _LOGGER.exception("Failed to persist rate limiter state on unload")

    try:
        hass.services.async_remove("notify", "ans")
    except Exception:
        _LOGGER.debug("Failed to remove notify.ans (ignored)")
