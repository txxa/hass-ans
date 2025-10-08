# rate_limiter.py
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

from .models import RateResult

_LOGGER = logging.getLogger(__name__)


class RateLimiter:
    """Per-identity + global token-bucket rate limiter.

    - Each identity has a token bucket with capacity = identity_config.rate_limit
      and refill rate = capacity / rate_limit_window_seconds.

    - Global bucket enforces system-wide throughput using system_config.rate_limit_max.

    - The limiter is concurrency-safe using an asyncio.Lock for the entire limiter state.
      For higher throughput you can later move to per-identity locks.

    Persistence:
      - Optional. If a `persistence` object is provided and implements:
            - async save_limiter_state(state: dict)
            - async load_limiter_state() -> dict
        the limiter will persist minimal state (per-identity tokens and last_refill timestamps,
        plus global tokens and last_refill) when persist_state() is called.
    """

    # Defaults used if config snapshot lacks values
    DEFAULT_RATE_LIMIT = 10  # tokens per window for identity if not provided
    DEFAULT_RATE_LIMIT_WINDOW = 60  # seconds
    DEFAULT_GLOBAL_RATE_LIMIT = 100  # tokens per window globally

    def __init__(
        self,
        persistence: Any | None = None,
        *,
        default_window_seconds: int = DEFAULT_RATE_LIMIT_WINDOW,
    ) -> None:
        """
        Args:
            persistence: optional persistence helper with async save_limiter_state/load_limiter_state
            default_window_seconds: fallback window in seconds if system config not provided
        """
        self._persistence = persistence

        # Per-identity state: identity_id -> {"tokens": float, "last_refill": float, "capacity": float}
        self._state: dict[str, dict[str, float]] = {}

        # Identity-specific locks and a lock protecting the locks map
        self._identity_locks: dict[str, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()  # protects _identity_locks map

        # Global token state and lock
        self._global_tokens: float = float(self.DEFAULT_GLOBAL_RATE_LIMIT)
        self._global_last_refill: float = time.time()
        self._global_lock = asyncio.Lock()

        self._default_window = default_window_seconds

    # -----------------------
    # Public API
    # -----------------------
    async def check_and_consume(
        self, identity_id: str, cost: int = 1, config_snapshot: Any | None = None
    ) -> RateResult:
        """Atomically check allowance and consume tokens if allowed.

        Args:
            identity_id: identity identifier
            cost: number of tokens to consume (default 1)
            config_snapshot: ConfigSnapshot (used to read identity/system limits)

        Returns:
            RateResult
        """
        if cost <= 0:
            cost = 1

        now = time.time()

        # Extract config values with fallbacks
        identity_cfg = None
        system_cfg = None
        if config_snapshot is not None:
            # snapshot may provide identity_configs and system_config
            try:
                identity_cfg = config_snapshot.identity_configs.get(identity_id)
            except Exception:
                identity_cfg = None
            system_cfg = getattr(config_snapshot, "system_config", None)

        identity_capacity = (
            getattr(identity_cfg, "rate_limit", None) or self.DEFAULT_RATE_LIMIT
        )
        window_seconds = (
            getattr(system_cfg, "rate_limit_window", None) or self._default_window
        )
        global_capacity = (
            getattr(system_cfg, "rate_limit_max", None)
            or self.DEFAULT_GLOBAL_RATE_LIMIT
        )

        # Acquire global lock first, then identity lock to avoid deadlocks.
        await self._global_lock.acquire()
        try:
            identity_lock = await self._get_identity_lock(identity_id)
            await identity_lock.acquire()
            try:
                # Refill global tokens
                self._refill_global(now, global_capacity, window_seconds)

                # Prepare identity bucket
                state = self._state.get(identity_id)
                if state is None:
                    state = {
                        "tokens": float(identity_capacity),
                        "last_refill": now,
                        "capacity": float(identity_capacity),
                    }
                    self._state[identity_id] = state
                else:
                    # If capacity has changed (config change), adjust capacity and clamp tokens
                    if state.get("capacity") != float(identity_capacity):
                        state["capacity"] = float(identity_capacity)
                        state["tokens"] = min(
                            state.get("tokens", 0.0), state["capacity"]
                        )

                # Refill identity tokens
                self._refill_identity(state, now, identity_capacity, window_seconds)

                # Check availability
                tokens_available = state["tokens"]
                global_available = self._global_tokens

                _LOGGER.debug(
                    "RateLimiter: identity=%s tokens=%.3f/%.3f global=%.3f/%.3f cost=%s",
                    identity_id,
                    tokens_available,
                    state["capacity"],
                    global_available,
                    global_capacity,
                    cost,
                )

                if tokens_available >= cost and global_available >= cost:
                    # Consume tokens
                    state["tokens"] = tokens_available - cost
                    self._global_tokens = global_available - cost
                    # Best-effort persist
                    try:
                        await self._maybe_persist_state_locked()
                    except Exception as exc:
                        _LOGGER.warning(
                            "RateLimiter: persistence failed while saving state: %s",
                            exc,
                        )
                    tokens_left = int(math.floor(state["tokens"]))
                    return RateResult(
                        True,
                        reason=None,
                        retry_after_seconds=None,
                        tokens_left=tokens_left,
                    )
                else:
                    # Compute retry_after for identity and global; choose whichever is later (both must be available)
                    retry_after_identity = None
                    retry_after_global = None

                    if tokens_available < cost:
                        retry_after_identity = self._seconds_until_tokens(
                            state, cost, identity_capacity, window_seconds
                        )

                    if global_available < cost:
                        retry_after_global = self._seconds_until_global_tokens(
                            cost, global_capacity, window_seconds
                        )

                    # Choose max of the two (need both to be available)
                    candidates = [
                        t
                        for t in (retry_after_identity, retry_after_global)
                        if t is not None
                    ]
                    retry_after = max(candidates) if candidates else None

                    reason_parts = []
                    if tokens_available < cost:
                        reason_parts.append("identity_rate_limited")
                    if global_available < cost:
                        reason_parts.append("global_rate_limited")
                    reason = ",".join(reason_parts) if reason_parts else "rate_limited"

                    _LOGGER.debug(
                        "RateLimiter: denying identity=%s cost=%s tokens_available=%.3f global_available=%.3f retry_after=%s",
                        identity_id,
                        cost,
                        tokens_available,
                        global_available,
                        retry_after,
                    )

                    return RateResult(
                        False,
                        reason=reason,
                        retry_after_seconds=retry_after,
                        tokens_left=int(math.floor(tokens_available)),
                    )
            finally:
                identity_lock.release()
        finally:
            self._global_lock.release()

    def peek(
        self, identity_id: str, cost: int = 1, config_snapshot: Any | None = None
    ) -> RateResult:
        """Non-mutating check that returns what would happen if we consumed `cost` tokens.

        This method does not acquire the async lock and may observe slightly stale data.
        For strict correctness, use check_and_consume.
        """
        if cost <= 0:
            cost = 1

        now = time.time()

        identity_cfg = None
        system_cfg = None
        if config_snapshot is not None:
            try:
                identity_cfg = config_snapshot.identity_configs.get(identity_id)
            except Exception:
                identity_cfg = None
            system_cfg = getattr(config_snapshot, "system_config", None)

        identity_capacity = (
            getattr(identity_cfg, "rate_limit", None) or self.DEFAULT_RATE_LIMIT
        )
        window_seconds = (
            getattr(system_cfg, "rate_limit_window", None) or self._default_window
        )
        global_capacity = (
            getattr(system_cfg, "rate_limit_max", None)
            or self.DEFAULT_GLOBAL_RATE_LIMIT
        )

        state = self._state.get(identity_id)
        if state is None:
            tokens_available = float(identity_capacity)
            last_refill = now
            capacity = float(identity_capacity)
        else:
            # compute a provisional refill without modifying state
            last_refill = float(state.get("last_refill", now))
            capacity = float(state.get("capacity", identity_capacity))
            # refill
            elapsed = max(0.0, now - last_refill)
            refill_rate = capacity / max(1.0, window_seconds)
            tokens_available = min(
                capacity, float(state.get("tokens", 0.0)) + elapsed * refill_rate
            )

        # provisional global refill
        elapsed_g = max(0.0, now - self._global_last_refill)
        global_refill_rate = global_capacity / max(1.0, window_seconds)
        global_available = min(
            global_capacity, float(self._global_tokens) + elapsed_g * global_refill_rate
        )

        if tokens_available >= cost and global_available >= cost:
            return RateResult(
                True,
                reason=None,
                retry_after_seconds=None,
                tokens_left=int(math.floor(tokens_available)),
            )
        else:
            retry_after_identity = None
            retry_after_global = None
            if tokens_available < cost:
                # seconds until identity tokens reach `cost`
                needed = cost - tokens_available
                refill_rate = capacity / max(1.0, window_seconds)
                retry_after_identity = needed / refill_rate if refill_rate > 0 else None
            if global_available < cost:
                needed_g = cost - global_available
                refill_rate_g = global_capacity / max(1.0, window_seconds)
                retry_after_global = (
                    needed_g / refill_rate_g if refill_rate_g > 0 else None
                )
            candidates = [
                t for t in (retry_after_identity, retry_after_global) if t is not None
            ]
            retry_after = max(candidates) if candidates else None
            reason_parts = []
            if tokens_available < cost:
                reason_parts.append("identity_rate_limited")
            if global_available < cost:
                reason_parts.append("global_rate_limited")
            reason = ",".join(reason_parts) if reason_parts else "rate_limited"
            return RateResult(
                False,
                reason=reason,
                retry_after_seconds=retry_after,
                tokens_left=int(math.floor(tokens_available)),
            )

    async def reset_identity(self, identity_id: str) -> None:
        """Admin/test helper: reset token state for an identity."""
        # Acquire per-identity lock to safely remove state
        identity_lock = await self._get_identity_lock(identity_id)
        await identity_lock.acquire()
        try:
            self._state.pop(identity_id, None)
        finally:
            identity_lock.release()

    async def persist_state(self) -> None:
        """Persist limiter state if persistence layer configured (best-effort)."""
        # Acquire global lock to get a consistent view of global tokens, then copy state
        await self._global_lock.acquire()
        try:
            # Snapshot of state (best-effort). We avoid acquiring all identity locks to prevent long blocking.
            snapshot = {
                "global_tokens": self._global_tokens,
                "global_last_refill": self._global_last_refill,
                "identities": {
                    iid: {
                        "tokens": st.get("tokens", 0.0),
                        "last_refill": st.get("last_refill", 0.0),
                        "capacity": st.get("capacity", 0.0),
                    }
                    for iid, st in self._state.items()
                },
            }
        finally:
            self._global_lock.release()

        # Best-effort persist (the persistence API may be sync or async)
        if self._persistence is None:
            return
        try:
            save_fn = getattr(self._persistence, "save_limiter_state", None) or getattr(
                self._persistence, "save", None
            )
            if save_fn is None:
                _LOGGER.debug(
                    "RateLimiter.persist_state: persistence helper has no save method; skipping"
                )
                return
            result = save_fn(snapshot)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            _LOGGER.warning(
                "RateLimiter.persist_state: failed to persist limiter state: %s", exc
            )

    # -----------------------
    # Internal helpers
    # -----------------------
    async def load_state(self) -> None:
        """
        Load persisted limiter state if persistence helper implements load_limiter_state()/load().
        Best-effort: failures are logged and ignored.
        """
        if self._persistence is None:
            return

        load_fn = getattr(self._persistence, "load_limiter_state", None) or getattr(
            self._persistence, "load", None
        )
        if load_fn is None:
            _LOGGER.debug(
                "RateLimiter.load_state: persistence helper has no load method; skipping"
            )
            return

        try:
            result = load_fn()
            state = await result if asyncio.iscoroutine(result) else result
            if not state:
                return
            # Restore state under locks to avoid races
            await self._global_lock.acquire()
            try:
                self._global_tokens = float(
                    state.get("global_tokens", self._global_tokens)
                )
                self._global_last_refill = float(
                    state.get("global_last_refill", self._global_last_refill)
                )
            finally:
                self._global_lock.release()

            identities = state.get("identities", {}) or {}
            for identity_id, entry in identities.items():
                # Acquire per-identity lock while writing state
                identity_lock = await self._get_identity_lock(identity_id)
                await identity_lock.acquire()
                try:
                    self._state[identity_id] = {
                        "tokens": float(entry.get("tokens", 0.0)),
                        "last_refill": float(entry.get("last_refill", time.time())),
                        "capacity": float(
                            entry.get("capacity", self.DEFAULT_RATE_LIMIT)
                        ),
                    }
                finally:
                    identity_lock.release()
        except Exception as exc:
            _LOGGER.warning(
                "RateLimiter.load_state: failed to load limiter state: %s", exc
            )

    async def _get_identity_lock(self, identity_id: str) -> asyncio.Lock:
        """
        Return (and create if missing) the asyncio.Lock for the given identity.
        Protected by _locks_lock.
        """
        async with self._locks_lock:
            lock = self._identity_locks.get(identity_id)
            if lock is None:
                lock = asyncio.Lock()
                self._identity_locks[identity_id] = lock
            return lock

    def _refill_identity(
        self,
        state: dict[str, float],
        now_ts: float,
        capacity: float,
        window_seconds: float,
    ) -> None:
        """Refill identity bucket in-place."""
        last_refill = float(state.get("last_refill", now_ts))
        elapsed = max(0.0, now_ts - last_refill)
        if elapsed <= 0:
            return
        refill_rate = float(capacity) / max(1.0, window_seconds)
        added = elapsed * refill_rate
        state["tokens"] = min(float(state.get("tokens", 0.0)) + added, float(capacity))
        state["last_refill"] = now_ts

    def _refill_global(
        self, now_ts: float, global_capacity: float, window_seconds: float
    ) -> None:
        """Refill global bucket in-place."""
        elapsed = max(0.0, now_ts - self._global_last_refill)
        if elapsed <= 0:
            return
        refill_rate = float(global_capacity) / max(1.0, window_seconds)
        added = elapsed * refill_rate
        self._global_tokens = min(
            float(self._global_tokens) + added, float(global_capacity)
        )
        self._global_last_refill = now_ts

    def _seconds_until_tokens(
        self, state: dict[str, float], cost: int, capacity: float, window_seconds: float
    ) -> float | None:
        """Return seconds until identity tokens reach 'cost', or None if impossible."""
        tokens = float(state.get("tokens", 0.0))
        if tokens >= cost:
            return 0.0
        refill_rate = float(capacity) / max(1.0, window_seconds)
        if refill_rate <= 0:
            return None
        needed = float(cost) - tokens
        return needed / refill_rate

    def _seconds_until_global_tokens(
        self, cost: int, global_capacity: float, window_seconds: float
    ) -> float | None:
        """Return seconds until global tokens reach 'cost', or None if impossible."""
        global_avail = float(self._global_tokens)
        if global_avail >= cost:
            return 0.0
        refill_rate = float(global_capacity) / max(1.0, window_seconds)
        if refill_rate <= 0:
            return None
        needed = float(cost) - global_avail
        return needed / refill_rate

    async def _maybe_persist_state_locked(self) -> None:
        """Persist minimal limiter state (internal). Called while caller holds _lock.

        Persistence interface expected (best-effort):
            - async save_limiter_state(state_dict)
        """
        if self._persistence is None:
            return

        # Build minimal serializable state
        try:
            serial_state = {
                "global_tokens": self._global_tokens,
                "global_last_refill": self._global_last_refill,
                "identities": {
                    identity_id: {
                        "tokens": state.get("tokens", 0.0),
                        "last_refill": state.get("last_refill", 0.0),
                        "capacity": state.get("capacity", 0.0),
                    }
                    for identity_id, state in self._state.items()
                },
            }
            save_fn = getattr(self._persistence, "save_limiter_state", None)
            if save_fn is None:
                # Backwards compatibility: some persistence helpers may implement generic save()
                save_fn = getattr(self._persistence, "save", None)
            if save_fn is None:
                _LOGGER.debug(
                    "RateLimiter: persistence helper has no save_limiter_state/save method; skipping persistence"
                )
                return

            # call save function (awaitable)
            result = save_fn(serial_state)
            if asyncio.iscoroutine(result):
                await result
            else:
                # sync save succeeded
                pass
        except Exception as exc:
            _LOGGER.warning("RateLimiter: failed to persist limiter state: %s", exc)
            # swallow persistence errors; do not let persistence break rate limiting

    # async def load_state(self) -> None:
    #     """Load persisted limiter state if persistence helper implements load_limiter_state().

    #     Best-effort: failures are logged and ignored.
    #     """
    #     if self._persistence is None:
    #         return

    #     load_fn = getattr(self._persistence, "load_limiter_state", None)
    #     if load_fn is None:
    #         load_fn = getattr(self._persistence, "load", None)

    #     if load_fn is None:
    #         _LOGGER.debug(
    #             "RateLimiter: persistence helper has no load_limiter_state/load method; skipping load"
    #         )
    #         return

    #     try:
    #         result = load_fn()
    #         if asyncio.iscoroutine(result):
    #             state = await result
    #         else:
    #             state = result
    #         if not state:
    #             return
    #         async with self._lock:
    #             self._global_tokens = float(
    #                 state.get("global_tokens", self._global_tokens)
    #             )
    #             self._global_last_refill = float(
    #                 state.get("global_last_refill", self._global_last_refill)
    #             )
    #             identities = state.get("identities", {}) or {}
    #             for identity_id, entry in identities.items():
    #                 try:
    #                     self._state[identity_id] = {
    #                         "tokens": float(entry.get("tokens", 0.0)),
    #                         "last_refill": float(entry.get("last_refill", time.time())),
    #                         "capacity": float(
    #                             entry.get("capacity", self.DEFAULT_RATE_LIMIT)
    #                         ),
    #                     }
    #                 except Exception:
    #                     _LOGGER.debug(
    #                         "RateLimiter: skipping invalid persisted entry for identity %s",
    #                         identity_id,
    #                     )
    #     except Exception as exc:
    #         _LOGGER.warning(
    #             "RateLimiter: failed to load persisted limiter state: %s", exc
    #         )
    #         # ignore and continue with in-memory defaults
