"""Deliver notifications via Signal."""

import logging

import aiohttp

from ..models import DeliveryResult, NotificationPayload, RecipientContactInfo
from .base import DeliveryAdapter

_LOGGER = logging.getLogger(__name__)


class SignalDeliveryAdapter(DeliveryAdapter):
    """Deliver notifications via Signal messenger REST API.

    Attributes
    ----------
    channel : str
        The channel identifier "signal".

    """

    channel = "notify.signal"
    is_system_channel = False  # Signal delivers to specific recipients

    def __init__(self, *, api_url: str, timeout: float = 10.0) -> None:
        """Initialize Signal adapter with API endpoint and timeout.

        Parameters
        ----------
        api_url : str
            Base URL for Signal REST API service.
        timeout : float, optional
            Request timeout in seconds, by default 10.0.

        """
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout

    async def deliver(
        self,
        *,
        payload: NotificationPayload,
        contact_info: RecipientContactInfo,
        idempotency_key: str,
    ) -> DeliveryResult:
        """Deliver notification via Signal API.

        Parameters
        ----------
        payload : NotificationPayload
            The notification content to send.
        contact_info : RecipientContactInfo
            Recipient contact information including phone number.
        idempotency_key : str
            Unique key for idempotent retries.

        Returns
        -------
        DeliveryResult
            Result of delivery attempt (success, transient, or permanent failure).

        """
        # Signal adapter uses phone number
        if not contact_info.phone_number:
            return self.permanent_failure(
                error="No phone number configured for Signal delivery"
            )

        body = {
            "recipient": contact_info.phone_number,
            "title": payload.title,
            "message": payload.message,
            "idempotency_key": idempotency_key,
            "metadata": payload.metadata,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(self._api_url + "/send", json=body) as resp,
            ):
                if resp.status == 200:
                    data = await resp.json()
                    return self.success(remote_id=data.get("message_id"))

                if resp.status in (408, 429, 500, 502, 503, 504):
                    return self.transient_failure(
                        error=f"signal transient HTTP {resp.status}"
                    )

                text = await resp.text()
                return self.permanent_failure(
                    error=f"signal permanent HTTP {resp.status}: {text}"
                )

        except TimeoutError:
            return self.transient_failure(error="signal timeout")
        except aiohttp.ClientError as exc:
            return self.transient_failure(error=f"signal client error: {exc}")
        except Exception as exc:
            _LOGGER.exception("Unexpected Signal adapter failure")
            return self.permanent_failure(error=str(exc))
