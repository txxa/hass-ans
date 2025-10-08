"""ANS notify service registration and integration glue.

This module wires the routing pipeline and persistent worker together. It registers the
`notify.ans` service via hass.services and ensures the worker and persisted rate limiter are
started and stopped with the config entry lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.notify.legacy import BaseNotificationService
from homeassistant.core import HomeAssistant

from .__init__ import get_config_repository
from .models import DeliveryJob
from .rate_limiter import PersistedTokenBucket
from ._routing import run_routing_pipeline
from ._worker import DeliveryWorker

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
