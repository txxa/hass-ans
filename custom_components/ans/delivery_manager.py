# delivery_manager.py
from __future__ import annotations

import asyncio
import heapq
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from homeassistant.core import HomeAssistant

from .models import (  # type: ignore
    ConfigSnapshot,
    DeliveryResult,
    Identity,
    IdentityConfig,
    NotificationEnvelope,
    RetryEntry,
)
from .models import DeliveryResult as ModelsDeliveryResult  # type: ignore
from .models import RetryEntry as ModelsRetryEntry  # type: ignore

_LOGGER = logging.getLogger(__name__)


class DeliveryManager:
    """Delivery manager that performs deliveries and handles per-channel retries.

    Persistence helper (optional) API we respect (best-effort):
      - save_retry_entry(retry_entry) -> sync or awaitable
      - delete_retry_entry(retry_entry) -> sync or awaitable
      - get_due_retry_entries(now_ts) -> list[RetryEntry] (sync or awaitable)   [optional]
      - load_retry_entries() -> list[RetryEntry] (sync or awaitable)           [optional]
    """

    DEFAULT_DELIVERY_TIMEOUT = 10.0  # seconds
    DEFAULT_BASE_BACKOFF = 30.0
    DEFAULT_MAX_BACKOFF = 3600.0
    DEFAULT_JITTER_FRAC = 0.2

    def __init__(
        self,
        hass: HomeAssistant,
        persistence: Optional[Any],
        *,
        max_global_concurrency: int = 20,
        per_identity_concurrency: int = 3,
        delivery_timeout: float = DEFAULT_DELIVERY_TIMEOUT,
        base_backoff: float = DEFAULT_BASE_BACKOFF,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
        jitter_frac: float = DEFAULT_JITTER_FRAC,
    ) -> None:
        self.hass = hass
        self.persistence = persistence
        self._global_sema = asyncio.Semaphore(max_global_concurrency)
        self._per_identity_limit = per_identity_concurrency
        self._identity_semaphores: Dict[str, asyncio.Semaphore] = {}
        self._delivery_timeout = float(delivery_timeout)
        self._base_backoff = float(base_backoff)
        self._max_backoff = float(max_backoff)
        self._jitter_frac = float(jitter_frac)

        # In-memory retry heap fallback: (next_attempt_ts, counter, RetryEntry)
        self._inmemory_retry_heap: List[Tuple[float, int, RetryEntry]] = []
        self._inmemory_counter = 0
        self._inmemory_lock = asyncio.Lock()

        self._running = False
        self._retry_handler_task: Optional[asyncio.Task] = None

    # -----------------------
    # Public API
    # -----------------------
    async def start_retry_handler(self, poll_interval: float = 5.0) -> None:
        """Start background retry handler task (idempotent)."""
        if self._running:
            return
        self._running = True
        self._retry_handler_task = asyncio.create_task(
            self.retry_handler(poll_interval)
        )

    async def stop_retry_handler(self) -> None:
        """Stop background retry handler task gracefully."""
        self._running = False
        if self._retry_handler_task:
            self._retry_handler_task.cancel()
            try:
                await self._retry_handler_task
            except Exception:
                pass
            self._retry_handler_task = None

    async def deliver_to_channels(
        self,
        envelope: NotificationEnvelope,
        identity: Identity,
        channels: list[str],
        identity_config: IdentityConfig,
        config_snapshot: ConfigSnapshot,
        sequential: bool = False,
    ) -> List[DeliveryResult]:
        """Deliver envelope to channels. Returns list of DeliveryResult.

        On TRANSIENT_FAIL, schedules per-channel retry entries (channel included).
        """
        results: List[DeliveryResult] = []

        if sequential:
            for idx, ch in enumerate(channels, start=1):
                res = await self.deliver_single(envelope, identity, ch, attempt_no=1)
                results.append(res)
        else:
            sem = self._identity_semaphores.get(identity.id)
            if sem is None:
                sem = asyncio.Semaphore(self._per_identity_limit)
                self._identity_semaphores[identity.id] = sem

            async def _deliver_ch(ch: str) -> DeliveryResult:
                async with self._global_sema:
                    async with sem:
                        return await self.deliver_single(
                            envelope, identity, ch, attempt_no=1
                        )

            tasks = [asyncio.create_task(_deliver_ch(ch)) for ch in channels]
            gathered = await asyncio.gather(*tasks, return_exceptions=False)
            results.extend(gathered)

        # Schedule retries per-channel for transient failures
        for dr in results:
            if dr.status == "TRANSIENT_FAIL":
                max_attempts = getattr(
                    identity_config, "retry_attempts", None
                ) or getattr(
                    getattr(config_snapshot, "system_config", None),
                    "retry_attempts_max",
                    3,
                )
                retry_entry = self._build_retry_entry_from_result(
                    envelope, identity, dr, identity_config, int(max_attempts)
                )
                ok = await self.schedule_retry(retry_entry)
                if ok:
                    _LOGGER.info(
                        "DeliveryManager: scheduled retry for %s/%s channel=%s next=%s",
                        retry_entry.notification_id,
                        retry_entry.identity_id,
                        retry_entry.channel,
                        retry_entry.next_attempt_ts,
                    )
        return results

    async def deliver_single(
        self,
        envelope: NotificationEnvelope,
        identity: Identity,
        channel: str,
        attempt_no: int = 1,
    ) -> DeliveryResult:
        """Execute a single-channel delivery attempt.

        Returns DeliveryResult with status SUCCESS / TRANSIENT_FAIL / PERMANENT_FAIL.
        """
        ts_iso = datetime.now(timezone.utc).isoformat()
        if not isinstance(channel, str) or "." not in channel:
            return self._make_delivery_result(
                channel,
                "PERMANENT_FAIL",
                attempt_no,
                ts_iso,
                error_code="invalid_channel",
                details="Channel must be 'domain.service'",
            )

        domain, service = channel.split(".", 1)
        title = getattr(envelope, "title", None) or ""
        body = getattr(envelope, "body", None) or ""
        payload = {
            "message": body,
            "title": title,
            "data": {
                "notification_id": getattr(envelope, "notification_id", None),
                "source": getattr(envelope, "source", None),
                "type": getattr(envelope, "type", None),
            },
        }

        try:
            coro = self.hass.services.async_call(
                domain, service, payload, blocking=True
            )
            await asyncio.wait_for(coro, timeout=self._delivery_timeout)
            # success
            dr = self._make_delivery_result(channel, "SUCCESS", attempt_no, ts_iso)
            try:
                self.hass.bus.async_fire(
                    "ans.delivery_succeeded",
                    {
                        "notification_id": getattr(envelope, "notification_id", None),
                        "identity_id": getattr(identity, "id", None),
                        "channel": channel,
                        "attempt_no": attempt_no,
                    },
                )
            except Exception:
                _LOGGER.debug(
                    "DeliveryManager: failed to fire delivery_succeeded event"
                )
            return dr
        except Exception as exc:
            status, reason = self.classify_error(exc)
            dr = self._make_delivery_result(
                channel, status, attempt_no, ts_iso, error_code=reason, details=str(exc)
            )
            # Fire attempt/failure events
            try:
                if status == "TRANSIENT_FAIL":
                    self.hass.bus.async_fire(
                        "ans.delivery_attempt",
                        {
                            "notification_id": getattr(
                                envelope, "notification_id", None
                            ),
                            "identity_id": getattr(identity, "id", None),
                            "channel": channel,
                            "attempt_no": attempt_no,
                            "status": status,
                            "error": reason,
                        },
                    )
                else:
                    self.hass.bus.async_fire(
                        "ans.delivery_failed",
                        {
                            "notification_id": getattr(
                                envelope, "notification_id", None
                            ),
                            "identity_id": getattr(identity, "id", None),
                            "channel": channel,
                            "attempt_no": attempt_no,
                            "status": status,
                            "error": reason,
                        },
                    )
            except Exception:
                _LOGGER.debug("DeliveryManager: failed to emit delivery event")
            return dr

    def classify_error(self, exception_or_response: Any) -> Tuple[str, str]:
        """Classify an exception into (status, reason).

        Favor transient for network/timeouts; permanent for payload/service errors.
        """
        if isinstance(exception_or_response, asyncio.TimeoutError):
            return "TRANSIENT_FAIL", "timeout"
        if isinstance(exception_or_response, (OSError, ConnectionError)):
            return "TRANSIENT_FAIL", exception_or_response.__class__.__name__.lower()
        if isinstance(exception_or_response, (ValueError, KeyError, TypeError)):
            return "PERMANENT_FAIL", exception_or_response.__class__.__name__.lower()
        msg = str(exception_or_response).lower()
        if "timeout" in msg:
            return "TRANSIENT_FAIL", "timeout"
        if "not found" in msg or "service not found" in msg or "unknown service" in msg:
            return "PERMANENT_FAIL", "service_not_found"
        return "TRANSIENT_FAIL", exception_or_response.__class__.__name__.lower()

    def calculate_backoff(
        self,
        attempt_no: int,
        base_delay: Optional[float] = None,
        cap: Optional[float] = None,
    ) -> float:
        base = base_delay if base_delay is not None else self._base_backoff
        cap_val = cap if cap is not None else self._max_backoff
        exp = base * (2 ** (max(0, attempt_no - 1)))
        delay = min(exp, cap_val)
        jitter = delay * self._jitter_frac
        return max(0.0, delay + random.uniform(-jitter, jitter))

    async def schedule_retry(self, retry_entry: RetryEntry) -> bool:
        """Persist/schedule a retry entry. Use persistence if available; else push to in-memory heap."""
        now_ts = time.time()
        if getattr(retry_entry, "created_ts", 0.0) in (0.0, None):
            retry_entry.created_ts = now_ts

        if self.persistence:
            save_fn = getattr(self.persistence, "save_retry_entry", None) or getattr(
                self.persistence, "save", None
            )
            if save_fn:
                try:
                    res = save_fn(retry_entry)
                    if asyncio.iscoroutine(res):
                        await res
                    try:
                        self.hass.bus.async_fire(
                            "ans.retry_scheduled",
                            {
                                "notification_id": retry_entry.notification_id,
                                "identity_id": retry_entry.identity_id,
                                "channel": retry_entry.channel,
                                "next_attempt_ts": retry_entry.next_attempt_ts,
                                "attempts_done": retry_entry.attempts_done,
                            },
                        )
                    except Exception:
                        _LOGGER.debug(
                            "DeliveryManager: failed to emit retry_scheduled event"
                        )
                    return True
                except Exception as exc:
                    _LOGGER.warning(
                        "DeliveryManager.schedule_retry: persistence save failed: %s; falling back to memory",
                        exc,
                    )

        # fallback to in-memory
        try:
            async with self._inmemory_lock:
                self._inmemory_counter += 1
                heapq.heappush(
                    self._inmemory_retry_heap,
                    (retry_entry.next_attempt_ts, self._inmemory_counter, retry_entry),
                )
            try:
                self.hass.bus.async_fire(
                    "ans.retry_scheduled",
                    {
                        "notification_id": retry_entry.notification_id,
                        "identity_id": retry_entry.identity_id,
                        "channel": retry_entry.channel,
                        "next_attempt_ts": retry_entry.next_attempt_ts,
                        "attempts_done": retry_entry.attempts_done,
                    },
                )
            except Exception:
                pass
            return True
        except Exception as exc:
            _LOGGER.error(
                "DeliveryManager.schedule_retry: failed to queue retry in memory: %s",
                exc,
            )
            return False

    def _build_retry_entry_from_result(
        self,
        envelope: NotificationEnvelope,
        identity: Identity,
        delivery_result: DeliveryResult,
        identity_config: IdentityConfig,
        max_attempts: int,
    ) -> RetryEntry:
        """Build RetryEntry including channel so retry handler can redeliver to same channel."""
        attempts_done = getattr(delivery_result, "attempt_no", 1)
        meta = {
            "source": getattr(envelope, "source", None),
            "type": getattr(envelope, "type", None),
            "criticality": getattr(envelope, "criticality", None),
            "metadata": getattr(envelope, "metadata", None) or {},
        }
        next_try = time.time() + self.calculate_backoff(attempts_done + 1)
        return RetryEntry(
            notification_id=getattr(envelope, "notification_id", None),
            identity_id=getattr(identity, "id", None),
            channel=getattr(delivery_result, "channel", ""),
            envelope_meta=meta,
            next_attempt_ts=next_try,
            attempts_done=attempts_done,
            max_attempts=max_attempts,
            last_error_code=getattr(delivery_result, "error_code", None),
            created_ts=time.time(),
            version=1,
        )

    async def retry_handler(self, poll_interval: float = 5.0) -> None:
        """Background loop processing due RetryEntry items and redelivering per-channel."""
        self._running = True
        _LOGGER.debug("DeliveryManager.retry_handler started")
        try:
            while self._running:
                now_ts = time.time()
                due: List[RetryEntry] = []

                # Try persistence-provided due entries
                if self.persistence:
                    get_due = getattr(self.persistence, "get_due_retry_entries", None)
                    load_all = getattr(
                        self.persistence, "load_retry_entries", None
                    ) or getattr(self.persistence, "load", None)
                    if get_due:
                        try:
                            res = get_due(now_ts)
                            entries = await res if asyncio.iscoroutine(res) else res
                            due.extend(entries or [])
                        except Exception as exc:
                            _LOGGER.debug(
                                "DeliveryManager.retry_handler: persistence.get_due_retry_entries failed: %s",
                                exc,
                            )
                    elif load_all:
                        try:
                            res = load_all()
                            all_entries = await res if asyncio.iscoroutine(res) else res
                            due.extend(
                                [
                                    e
                                    for e in (all_entries or [])
                                    if getattr(e, "next_attempt_ts", 0) <= now_ts
                                ]
                            )
                        except Exception as exc:
                            _LOGGER.debug(
                                "DeliveryManager.retry_handler: persistence.load_retry_entries failed: %s",
                                exc,
                            )

                # Fallback: in-memory queue
                if not due:
                    async with self._inmemory_lock:
                        while (
                            self._inmemory_retry_heap
                            and self._inmemory_retry_heap[0][0] <= now_ts
                        ):
                            _, _, entry = heapq.heappop(self._inmemory_retry_heap)
                            due.append(entry)

                if not due:
                    await asyncio.sleep(poll_interval)
                    continue

                # Process entries concurrently (bounded)
                tasks = [
                    asyncio.create_task(self._process_retry_entry(entry))
                    for entry in due
                ]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=False)
        except asyncio.CancelledError:
            _LOGGER.debug("DeliveryManager.retry_handler cancelled")
        except Exception as exc:
            _LOGGER.exception("DeliveryManager.retry_handler unexpected error: %s", exc)
        finally:
            _LOGGER.debug("DeliveryManager.retry_handler exiting")

    async def _process_retry_entry(self, entry: RetryEntry) -> None:
        """Redeliver the stored channel for the retry entry, update or delete entry accordingly."""
        try:
            if entry.attempts_done >= entry.max_attempts:
                _LOGGER.info(
                    "Retry exhausted for %s/%s channel=%s",
                    entry.notification_id,
                    entry.identity_id,
                    entry.channel,
                )
                await self._delete_retry_entry(entry)
                try:
                    self.hass.bus.async_fire(
                        "ans.retry_exhausted",
                        {
                            "notification_id": entry.notification_id,
                            "identity_id": entry.identity_id,
                            "channel": entry.channel,
                            "attempts_done": entry.attempts_done,
                        },
                    )
                except Exception:
                    pass
                return

            # Recreate minimal envelope & identity objects
            class MinEnv:
                pass

            env = MinEnv()
            env.notification_id = entry.notification_id
            env.source = entry.envelope_meta.get("source")
            env.type = entry.envelope_meta.get("type")
            env.criticality = entry.envelope_meta.get("criticality")
            env.metadata = entry.envelope_meta.get("metadata", {})
            env.title = None
            env.body = None

            class MinIdentity:
                pass

            ident = MinIdentity()
            ident.id = entry.identity_id

            # Attempt delivery: attempt_no = previous attempts_done + 1
            attempt_no = int(entry.attempts_done) + 1
            dr = await self.deliver_single(
                env, ident, entry.channel, attempt_no=attempt_no
            )

            if dr.status == "SUCCESS":
                _LOGGER.info(
                    "Retry success for %s/%s channel=%s",
                    entry.notification_id,
                    entry.identity_id,
                    entry.channel,
                )
                await self._delete_retry_entry(entry)
                try:
                    self.hass.bus.async_fire(
                        "ans.retry_succeeded",
                        {
                            "notification_id": entry.notification_id,
                            "identity_id": entry.identity_id,
                            "channel": entry.channel,
                            "attempt_no": attempt_no,
                        },
                    )
                except Exception:
                    pass
                return
            elif dr.status == "PERMANENT_FAIL":
                _LOGGER.warning(
                    "Retry permanent failure for %s/%s channel=%s: %s",
                    entry.notification_id,
                    entry.identity_id,
                    entry.channel,
                    dr.details,
                )
                await self._delete_retry_entry(entry)
                try:
                    self.hass.bus.async_fire(
                        "ans.retry_failed",
                        {
                            "notification_id": entry.notification_id,
                            "identity_id": entry.identity_id,
                            "channel": entry.channel,
                            "attempt_no": attempt_no,
                            "error": dr.error_code,
                        },
                    )
                except Exception:
                    pass
                return
            else:
                # TRANSIENT_FAIL: increment attempts, set next_attempt_ts, persist/reschedule
                entry.attempts_done = attempt_no
                backoff = self.calculate_backoff(entry.attempts_done)
                entry.next_attempt_ts = time.time() + backoff
                entry.last_error_code = dr.error_code

                ok = await self._save_or_update_retry_entry(entry)
                if not ok:
                    # fallback to in-memory queue
                    async with self._inmemory_lock:
                        self._inmemory_counter += 1
                        heapq.heappush(
                            self._inmemory_retry_heap,
                            (entry.next_attempt_ts, self._inmemory_counter, entry),
                        )
                try:
                    self.hass.bus.async_fire(
                        "ans.retry_scheduled",
                        {
                            "notification_id": entry.notification_id,
                            "identity_id": entry.identity_id,
                            "channel": entry.channel,
                            "next_attempt_ts": entry.next_attempt_ts,
                            "attempts_done": entry.attempts_done,
                        },
                    )
                except Exception:
                    pass
                return
        except Exception as exc:
            _LOGGER.exception(
                "_process_retry_entry unexpected error for %s/%s: %s",
                entry.notification_id,
                entry.identity_id,
                exc,
            )

    # -----------------------
    # Persistence helpers
    # -----------------------
    async def _save_or_update_retry_entry(self, entry: RetryEntry) -> bool:
        """Attempt to save/update retry entry using persistence if available."""
        if not self.persistence:
            return False
        save_fn = getattr(self.persistence, "save_retry_entry", None) or getattr(
            self.persistence, "save", None
        )
        if not save_fn:
            return False
        try:
            res = save_fn(entry)
            if asyncio.iscoroutine(res):
                await res
            return True
        except Exception as exc:
            _LOGGER.warning(
                "_save_or_update_retry_entry: persistence save failed: %s", exc
            )
            return False

    async def _delete_retry_entry(self, entry: RetryEntry) -> None:
        """Attempt to delete retry entry from persistence (best-effort)."""
        if not self.persistence:
            return
        delete_fn = getattr(self.persistence, "delete_retry_entry", None) or getattr(
            self.persistence, "delete", None
        )
        if not delete_fn:
            return
        try:
            res = delete_fn(entry)
            if asyncio.iscoroutine(res):
                await res
        except Exception as exc:
            _LOGGER.warning("_delete_retry_entry: persistence delete failed: %s", exc)

    # -----------------------
    # Utilities
    # -----------------------
    def _make_delivery_result(
        self,
        channel: str,
        status: str,
        attempt_no: int,
        timestamp_iso: str,
        error_code: Optional[str] = None,
        details: Optional[str] = None,
    ) -> DeliveryResult:
        """Create a DeliveryResult instance (local or models-based)."""
        if ModelsDeliveryResult:
            try:
                return ModelsDeliveryResult(
                    channel=channel,
                    status=status,
                    attempt_no=attempt_no,
                    timestamp=timestamp_iso,
                    error_code=error_code,
                    details=details,
                )  # type: ignore
            except Exception:
                pass
        return DeliveryResult(
            channel=channel,
            status=status,
            attempt_no=attempt_no,
            timestamp=timestamp_iso,
            error_code=error_code,
            details=details,
        )

    async def shutdown(self) -> None:
        """Graceful shutdown: stop retry handler and wait briefly for in-flight tasks."""
        await self.stop_retry_handler()
        await asyncio.sleep(0.05)
