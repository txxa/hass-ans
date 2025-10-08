# orchestrator.py
from __future__ import annotations

import asyncio
import heapq
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    ConfigSnapshot,
    Identity,
    IdentityConfig,
    NotificationEnvelope,
    ProcessingSummary,
    RetryEntry,
)

_LOGGER = logging.getLogger(__name__)


# Special channel value used for identity-level retries (pre-routing)
IDENTITY_LEVEL_RETRY_CHANNEL = "__IDENTITY_RETRY__"


class Orchestrator:
    """
    Orchestrator coordinates the ANS processing pipeline:
      - snapshots config from config_repository
      - dispatches per-identity processing in a bounded worker pool
      - schedules identity-level retries (persisted or in-memory)
      - starts/stops background retry dispatcher
      - delegates channel delivery to DeliveryManager (which handles per-channel retries)
    """

    def __init__(
        self,
        hass: Any,
        config_repository: Any,
        persistence: Any,
        *,
        max_concurrent_identities: int = 10,
        identity_retry_poll_interval: float = 5.0,
    ) -> None:
        """
        Args:
            hass: Home Assistant instance
            config_repository: object exposing .snapshot_config()
            persistence: persistence helper (expects save_retry_entry / load_retry_entries / get_due_retry_entries optional)
            max_concurrent_identities: concurrency cap for per-identity processing
            identity_retry_poll_interval: poll interval for identity-level retry dispatcher
        """
        self.hass = hass
        self.config_repository = config_repository
        self.persistence = persistence

        # Reusable module instances
        from .filter_engine import FilterEngine
        from .rate_limiter import RateLimiter
        from .router import Router
        from .delivery_manager import DeliveryManager

        self.filter_engine = FilterEngine()
        self.rate_limiter = RateLimiter(persistence)
        self.router = Router()
        self.delivery_manager = DeliveryManager(hass, persistence)

        # Worker pool & tracking
        self._semaphore = asyncio.Semaphore(max_concurrent_identities)
        self._pending_tasks: Dict[str, asyncio.Task] = {}
        self._shutdown = False

        # In-memory identity retry heap: (next_attempt_ts, counter, RetryEntry)
        self._identity_retry_heap: List[Tuple[float, int, RetryEntry]] = []
        self._identity_retry_counter = 0
        self._identity_retry_lock = asyncio.Lock()
        self._identity_retry_poll_interval = identity_retry_poll_interval
        self._identity_retry_task: Optional[asyncio.Task] = None

        # Start DeliveryManager retry handler as needed (idempotent)
        self._dm_retry_started = False

    async def start(self) -> None:
        """Start orchestrator background tasks and DeliveryManager retry handler."""
        if self._shutdown:
            raise RuntimeError("Orchestrator already shut down")

        # Start DeliveryManager retry handler
        if not self._dm_retry_started:
            try:
                await self.delivery_manager.start_retry_handler(
                    self._identity_retry_poll_interval
                )
            except Exception:
                # Some DeliveryManager versions might not require explicit start; ignore failures
                _LOGGER.debug(
                    "Orchestrator: DeliveryManager.start_retry_handler failed or not available"
                )
            self._dm_retry_started = True

        # Start identity-level retry dispatcher
        if self._identity_retry_task is None:
            self._identity_retry_task = asyncio.create_task(
                self._identity_retry_dispatcher()
            )
            _LOGGER.debug("Orchestrator: identity retry dispatcher started")

    async def stop(self) -> None:
        """Stop background tasks and wait for pending identity tasks to finish."""
        self._shutdown = True

        # Stop identity retry dispatcher
        if self._identity_retry_task:
            self._identity_retry_task.cancel()
            try:
                await self._identity_retry_task
            except Exception:
                pass
            self._identity_retry_task = None

        # Stop DeliveryManager retry handler
        try:
            await self.delivery_manager.stop_retry_handler()
        except Exception:
            _LOGGER.debug(
                "Orchestrator: DeliveryManager.stop_retry_handler failed or not available"
            )

        # Cancel outstanding per-identity tasks
        for task in list(self._pending_tasks.values()):
            task.cancel()
        self._pending_tasks.clear()

    # -----------------------
    # Public entrypoint
    # -----------------------
    async def handle_incoming(
        self, envelope: NotificationEnvelope, *, wait_for_completion: bool = False
    ) -> ProcessingSummary:
        """
        Public entrypoint for sending a notification envelope into ANS.

        If wait_for_completion is True, this coroutine waits for all per-identity tasks to complete
        and returns a detailed ProcessingSummary. Otherwise it returns a lightweight accepted summary.
        """
        if not hasattr(envelope, "notification_id") or envelope.notification_id is None:
            raise ValueError("Envelope must have notification_id")

        _LOGGER.info("ans.notification_received %s", envelope.notification_id)
        # Snapshot config (immutable for this processing)
        config_snapshot: ConfigSnapshot = self.config_repository.snapshot_config()

        # Resolve target identities (expand broadcast)
        if getattr(envelope, "target_identities", None):
            identity_ids = [
                i
                for i in envelope.target_identities
                if i in config_snapshot.identity_configs
            ]
        else:
            identity_ids = list(config_snapshot.identity_configs.keys())

        # track counters
        accepted = 0
        filtered_list: List[str] = []
        rate_limited_list: List[str] = []
        errors: List[Dict[str, Any]] = []

        tasks = []
        for identity_id in identity_ids:
            await self._semaphore.acquire()
            task = asyncio.create_task(
                self._run_identity_task(identity_id, envelope, config_snapshot)
            )
            self._pending_tasks[identity_id] = task
            # release semaphore when done
            task.add_done_callback(
                lambda t, sem=self._semaphore, iid=identity_id: self._on_task_done(
                    iid, sem, t
                )
            )
            tasks.append(task)
            accepted += 1

        if wait_for_completion:
            results = await asyncio.gather(*tasks, return_exceptions=False)
            processed_count = 0
            for r in results:
                processed_count += 1
                status = r.get("status")
                if status == "FILTERED":
                    filtered_list.append(r.get("identity_id"))
                if status == "RATE_LIMITED":
                    rate_limited_list.append(r.get("identity_id"))
                if status == "ERROR":
                    errors.append(
                        {"identity_id": r.get("identity_id"), "error": r.get("error")}
                    )
            summary = ProcessingSummary(
                notification_id=envelope.notification_id,
                accepted_identities=accepted,
                processed_identities=processed_count,
                filtered_identities=filtered_list,
                rate_limited_identities=rate_limited_list,
                errors=errors,
            )
            # emit final process event
            try:
                self.hass.bus.async_fire(
                    "ans.notification_processed",
                    {
                        "notification_id": envelope.notification_id,
                        "summary": {
                            "accepted": accepted,
                            "processed": processed_count,
                            "filtered": filtered_list,
                            "rate_limited": rate_limited_list,
                            "errors": errors,
                        },
                    },
                )
            except Exception:
                _LOGGER.debug(
                    "Orchestrator: failed to emit final notification_processed event"
                )
            return summary
        else:
            # Return a lightweight accepted summary
            return ProcessingSummary(
                notification_id=envelope.notification_id,
                accepted_identities=accepted,
                processed_identities=0,
                filtered_identities=[],
                rate_limited_identities=[],
                errors=[],
            )

    # -----------------------
    # Per-identity pipeline
    # -----------------------
    async def _run_identity_task(
        self,
        identity_id: str,
        envelope: NotificationEnvelope,
        config_snapshot: ConfigSnapshot,
    ) -> Dict[str, Any]:
        """
        Per-identity pipeline:
          - filter
          - rate limit check
          - route channels
          - deliver via DeliveryManager
        Returns a dict summarizing per-identity outcome.
        """
        try:
            identity = config_snapshot.identities.get(identity_id)
            identity_config = config_snapshot.identity_configs.get(identity_id)

            # 1) Filter
            filter_result = self.filter_engine.evaluate(
                identity, identity_config, envelope
            )
            if not filter_result.allowed:
                self.hass.bus.async_fire(
                    "ans.notification_filtered",
                    {
                        "notification_id": envelope.notification_id,
                        "identity_id": identity_id,
                        "reason": filter_result.reason,
                    },
                )
                return {
                    "identity_id": identity_id,
                    "status": "FILTERED",
                    "reason": filter_result.reason,
                }

            # 2) Rate limiter
            rate_result = await self.rate_limiter.check_and_consume(
                identity_id, cost=1, config_snapshot=config_snapshot
            )
            if not rate_result.allowed:
                # Schedule identity-level retry (persisted if available)
                await self._schedule_identity_retry(
                    envelope, identity_id, identity_config, rate_result
                )
                self.hass.bus.async_fire(
                    "ans.notification_rate_limited",
                    {
                        "notification_id": envelope.notification_id,
                        "identity_id": identity_id,
                        "retry_after": rate_result.retry_after_seconds,
                        "reason": rate_result.reason,
                    },
                )
                return {
                    "identity_id": identity_id,
                    "status": "RATE_LIMITED",
                    "retry_after": rate_result.retry_after_seconds,
                }

            # 3) Route channels
            channels = self.router.resolve_channels(
                identity_config,
                config_snapshot.system_config,
                getattr(envelope, "criticality", "LOW"),
            )
            if not channels:
                self.hass.bus.async_fire(
                    "ans.no_channels",
                    {
                        "notification_id": envelope.notification_id,
                        "identity_id": identity_id,
                    },
                )
                return {"identity_id": identity_id, "status": "NO_CHANNELS"}

            # 4) Deliver
            delivery_results = await self.delivery_manager.deliver_to_channels(
                envelope=envelope,
                identity=identity,
                channels=channels,
                identity_config=identity_config,
                config_snapshot=config_snapshot,
                sequential=False,
            )
            # Build result summary
            summary_list = []
            for dr in delivery_results:
                # dr may be dataclass or dict-like
                status = getattr(dr, "status", None) or dr.get("status")
                summary_list.append(
                    {
                        "channel": getattr(dr, "channel", None) or dr.get("channel"),
                        "status": status,
                    }
                )
            return {
                "identity_id": identity_id,
                "status": "DELIVERED",
                "delivery_results": summary_list,
            }
        except Exception as exc:
            _LOGGER.exception(
                "Orchestrator: error processing identity %s for notification %s: %s",
                identity_id,
                getattr(envelope, "notification_id", None),
                exc,
            )
            try:
                self.hass.bus.async_fire(
                    "ans.notification_processing_error",
                    {
                        "notification_id": getattr(envelope, "notification_id", None),
                        "identity_id": identity_id,
                        "error": str(exc),
                    },
                )
            except Exception:
                pass
            return {"identity_id": identity_id, "status": "ERROR", "error": str(exc)}

    def _on_task_done(
        self, identity_id: str, sem: asyncio.Semaphore, task: asyncio.Task
    ) -> None:
        """Cleanup when a per-identity task completes."""
        try:
            sem.release()
        except Exception:
            _LOGGER.debug(
                "Orchestrator._on_task_done: semaphore release failed for %s",
                identity_id,
            )
        self._pending_tasks.pop(identity_id, None)

    # -----------------------
    # Identity-level retry scheduling & dispatcher
    # -----------------------
    async def _schedule_identity_retry(
        self,
        envelope: NotificationEnvelope,
        identity_id: str,
        identity_config: IdentityConfig,
        rate_result: Any,
    ) -> bool:
        """
        Schedule a retry for the identity-level (pre-routing). This creates a RetryEntry
        with channel=IDENTITY_LEVEL_RETRY_CHANNEL so DeliveryManager won't treat it as per-channel retry.
        """
        # compute next_attempt_ts: prefer rate_result.retry_after_seconds if provided
        next_after = (
            rate_result.retry_after_seconds
            if getattr(rate_result, "retry_after_seconds", None)
            else 30.0
        )
        next_ts = time.time() + float(next_after)

        max_attempts = getattr(identity_config, "retry_attempts", None) or getattr(
            getattr(self.config_repository.snapshot_config(), "system_config", None),
            "retry_attempts_max",
            3,
        )

        retry = RetryEntry(
            notification_id=envelope.notification_id,
            identity_id=identity_id,
            channel=IDENTITY_LEVEL_RETRY_CHANNEL,
            envelope_meta={
                "source": getattr(envelope, "source", None),
                "type": getattr(envelope, "type", None),
                "criticality": getattr(envelope, "criticality", None),
                "metadata": getattr(envelope, "metadata", None) or {},
            },
            next_attempt_ts=next_ts,
            attempts_done=0,
            max_attempts=max_attempts,
            last_error_code=getattr(rate_result, "reason", None),
            created_ts=time.time(),
            version=1,
        )

        # Try to persist using persistence helper API or fall back to in-memory
        if self.persistence:
            save_fn = getattr(self.persistence, "save_retry_entry", None) or getattr(
                self.persistence, "save", None
            )
            if save_fn:
                try:
                    res = save_fn(retry)
                    if asyncio.iscoroutine(res):
                        await res
                    return True
                except Exception as exc:
                    _LOGGER.warning(
                        "Orchestrator: persistence.save_retry_entry failed: %s; falling back to memory",
                        exc,
                    )

        # Fallback to in-memory heap
        try:
            async with self._identity_retry_lock:
                self._identity_retry_counter += 1
                heapq.heappush(
                    self._identity_retry_heap,
                    (retry.next_attempt_ts, self._identity_retry_counter, retry),
                )
            return True
        except Exception as exc:
            _LOGGER.error(
                "Orchestrator: failed to enqueue identity retry in memory: %s", exc
            )
            return False

    async def _identity_retry_dispatcher(self) -> None:
        """
        Background loop that scans for due identity-level retries and re-enqueues their per-identity pipeline.
        """
        _LOGGER.debug("Orchestrator: identity retry dispatcher started")
        try:
            while not self._shutdown:
                now_ts = time.time()
                due: List[RetryEntry] = []

                # 1) Fetch due from persistence if available
                if self.persistence:
                    get_due = getattr(self.persistence, "get_due_retry_entries", None)
                    load_all = getattr(
                        self.persistence, "load_retry_entries", None
                    ) or getattr(self.persistence, "load", None)
                    if get_due:
                        try:
                            res = get_due(now_ts)
                            entries = await res if asyncio.iscoroutine(res) else res
                            # filter for identity-level channel
                            due.extend(
                                [
                                    e
                                    for e in (entries or [])
                                    if getattr(e, "channel", "")
                                    == IDENTITY_LEVEL_RETRY_CHANNEL
                                ]
                            )
                        except Exception as exc:
                            _LOGGER.debug(
                                "Orchestrator: persistence.get_due_retry_entries failed: %s",
                                exc,
                            )
                    elif load_all:
                        try:
                            res = load_all()
                            all_entries = await res if asyncio.iscoroutine(res) else res
                            for e in all_entries or []:
                                if (
                                    getattr(e, "channel", "")
                                    == IDENTITY_LEVEL_RETRY_CHANNEL
                                    and getattr(e, "next_attempt_ts", 0) <= now_ts
                                ):
                                    due.append(e)
                        except Exception as exc:
                            _LOGGER.debug(
                                "Orchestrator: persistence.load_retry_entries failed: %s",
                                exc,
                            )

                # 2) From in-memory heap fallback
                async with self._identity_retry_lock:
                    while (
                        self._identity_retry_heap
                        and self._identity_retry_heap[0][0] <= now_ts
                    ):
                        _, _, entry = heapq.heappop(self._identity_retry_heap)
                        due.append(entry)

                if not due:
                    await asyncio.sleep(self._identity_retry_poll_interval)
                    continue

                # Process due entries concurrently (bounded)
                for entry in due:
                    # If attempts exhausted -> delete/publish exhausted
                    if getattr(entry, "attempts_done", 0) >= getattr(
                        entry, "max_attempts", 1
                    ):
                        _LOGGER.info(
                            "Orchestrator: identity retry exhausted for %s/%s",
                            entry.notification_id,
                            entry.identity_id,
                        )
                        # attempt to delete from persistence if present
                        if self.persistence:
                            try:
                                delete_fn = getattr(
                                    self.persistence, "delete_retry_entry", None
                                ) or getattr(self.persistence, "delete", None)
                                if delete_fn:
                                    res = delete_fn(entry)
                                    if asyncio.iscoroutine(res):
                                        await res
                            except Exception as exc:
                                _LOGGER.debug(
                                    "Orchestrator: persistence.delete_retry_entry failed: %s",
                                    exc,
                                )
                        try:
                            self.hass.bus.async_fire(
                                "ans.retry_exhausted",
                                {
                                    "notification_id": entry.notification_id,
                                    "identity_id": entry.identity_id,
                                },
                            )
                        except Exception:
                            pass
                        continue

                    # Re-run per-identity pipeline by scheduling a task (respect concurrency)
                    # Increment attempts_done preemptively to avoid tight loops and persist
                    entry.attempts_done = int(entry.attempts_done) + 1
                    # Persist updated attempts if persistence supports update
                    if self.persistence:
                        try:
                            save_fn = getattr(
                                self.persistence, "save_retry_entry", None
                            ) or getattr(self.persistence, "save", None)
                            if save_fn:
                                res = save_fn(entry)
                                if asyncio.iscoroutine(res):
                                    await res
                        except Exception as exc:
                            _LOGGER.debug(
                                "Orchestrator: persistence.save_retry_entry update failed: %s",
                                exc,
                            )

                    # Reconstruct minimal envelope and dispatch
                    env = NotificationEnvelope(
                        notification_id=entry.notification_id,
                        source=entry.envelope_meta.get("source"),
                        type=entry.envelope_meta.get("type"),
                        criticality=entry.envelope_meta.get("criticality"),
                        metadata=entry.envelope_meta.get("metadata", {}),
                    )

                    # Schedule the task
                    await self._semaphore.acquire()
                    task = asyncio.create_task(
                        self._run_identity_task(
                            entry.identity_id,
                            env,
                            self.config_repository.snapshot_config(),
                        )
                    )
                    self._pending_tasks[entry.identity_id] = task
                    task.add_done_callback(
                        lambda t,
                        sem=self._semaphore,
                        iid=entry.identity_id: self._on_task_done(iid, sem, t)
                    )

                    # Compute next attempt timestamp if not processed immediately (we simply rely on attempts_done updates and persistence)
                    # If persistence not used, and attempts remain, requeue with exponential backoff
                    if not self.persistence:
                        if entry.attempts_done < entry.max_attempts:
                            backoff = min(30 * (2 ** (entry.attempts_done - 1)), 3600)
                            entry.next_attempt_ts = time.time() + backoff
                            async with self._identity_retry_lock:
                                self._identity_retry_counter += 1
                                heapq.heappush(
                                    self._identity_retry_heap,
                                    (
                                        entry.next_attempt_ts,
                                        self._identity_retry_counter,
                                        entry,
                                    ),
                                )

                # brief sleep to yield
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            _LOGGER.debug("Orchestrator: identity retry dispatcher cancelled")
        except Exception as exc:
            _LOGGER.exception(
                "Orchestrator: identity retry dispatcher unexpected error: %s", exc
            )
        finally:
            _LOGGER.debug("Orchestrator: identity retry dispatcher exiting")

    # -----------------------
    # Utilities
    # -----------------------
    def _on_task_done(
        self, identity_id: str, sem: asyncio.Semaphore, task: asyncio.Task
    ) -> None:
        """Release semaphore and cleanup task tracking after a per-identity task finishes."""
        try:
            sem.release()
        except Exception:
            _LOGGER.debug("Orchestrator: semaphore release failed for %s", identity_id)
        self._pending_tasks.pop(identity_id, None)
