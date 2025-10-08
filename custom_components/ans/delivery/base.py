"""define the delivery adapter contract and shared error semantics."""

from abc import ABC, abstractmethod
from enum import Enum

from ..models import (
    DeliveryResult,
    DeliveryStatus,
    NotificationPayload,
    RecipientContactInfo,
)


class AdapterFailureType(str, Enum):
    """Classification of delivery adapter failures.

    Values
    ------
    TRANSIENT : str
        Temporary failure, safe to retry (e.g., network timeout).
    PERMANENT : str
        Permanent failure, do not retry (e.g., invalid recipient).

    """

    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"


class DeliveryAdapter(ABC):
    """Base class for all channel delivery adapters.

    One adapter = one physical channel implementation.
    """

    channel: str  # logical channel name, e.g. "signal", "email"

    @abstractmethod
    async def deliver(
        self,
        *,
        payload: NotificationPayload,
        contact_info: RecipientContactInfo,
        idempotency_key: str,
    ) -> DeliveryResult:
        """Perform exactly one delivery attempt.

        Must:
        - be async
        - respect idempotency_key if supported by provider
        - never raise for expected failures
        """
        raise NotImplementedError

    # ------------------------------------------------------------
    # Helper factories (optional, but recommended)
    # ------------------------------------------------------------

    def success(self, *, remote_id: str | None = None) -> DeliveryResult:
        """Create a successful delivery result.

        Parameters
        ----------
        remote_id : str, optional
            Remote service message ID if available.

        Returns
        -------
        DeliveryResult
            Success result object.

        """
        return DeliveryResult(status=DeliveryStatus.SUCCESS, remote_id=remote_id)

    def transient_failure(self, *, error: str) -> DeliveryResult:
        """Create a transient failure result for retrying.

        Parameters
        ----------
        error : str
            Error description for logging.

        Returns
        -------
        DeliveryResult
            Transient failure result object.

        """
        return DeliveryResult(status=DeliveryStatus.TRANSIENT_FAIL, error=error)

    def permanent_failure(self, *, error: str) -> DeliveryResult:
        """Create a permanent failure result.

        Parameters
        ----------
        error : str
            Error description for logging.

        Returns
        -------
        DeliveryResult
            Permanent failure result object.

        """
        return DeliveryResult(status=DeliveryStatus.PERMANENT_FAIL, error=error)
