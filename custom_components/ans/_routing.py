"""Routing pipeline for ANS — now wired to use the persisted token-bucket when provided.

The pipeline signature accepts an optional `rate_limiter` object exposing `consume(identity_id, capacity, window)`.
If none is provided the pipeline falls back to the in-memory placeholder limiter (not persisted).
"""

from __future__ import annotations

import random
import re
from typing import Any

from homeassistant.util import dt as dt_util

from custom_components.ans.rate_limiter import PersistedTokenBucket

from .models import (
    DeliveryJob,
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
) -> list[str]:
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
    rate_limiter: PersistedTokenBucket | None = None,
) -> list[DeliveryJob]:
    """Produce DeliveryJob list for payload.

    rate_limiter: optional object exposing `consume(identity_id, capacity, window)` coroutine.
    """
    message = payload.get("message", "")
    title = payload.get("title")
    source = payload.get("source", "unknown")
    ntype = _parse_notification_type(payload.get("notification_type"))
    criticality = _parse_criticality(payload.get("criticality"), ntype)

    jobs: list[DeliveryJob] = []

    identities: list[tuple[Receiver, ReceiverConfig]] = config_repo.iter_identities()

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
