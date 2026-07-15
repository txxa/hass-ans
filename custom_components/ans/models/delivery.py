"""Delivery task and attempt models for the ANS system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import StrEnum
from typing import Any
from uuid import UUID

from .channel import ChannelInfo, ChannelScope
from .notification import NotificationCriticality, NotificationPayload, NotificationType
from .policy import DoNotDisturbConfig, RecipientNotificationPolicy
from .recipient import RecipientContactInfo, TTSSettings


def _dnd_config_to_dict(dnd: DoNotDisturbConfig | None) -> dict | None:
    """Serialize a DoNotDisturbConfig for persistence, or None if unset."""
    if dnd is None:
        return None
    return {
        "start": dnd.start.isoformat() if dnd.start else None,
        "end": dnd.end.isoformat() if dnd.end else None,
        "allowed_sources_regex": dnd.allowed_sources_regex,
        "allowed_criticalities": (
            sorted(c.value for c in dnd.allowed_criticalities)
            if dnd.allowed_criticalities is not None
            else None
        ),
        "allowed_types": (
            sorted(t.value for t in dnd.allowed_types)
            if dnd.allowed_types is not None
            else None
        ),
    }


def _dnd_config_from_dict(data: dict | None) -> DoNotDisturbConfig | None:
    """Reconstruct a DoNotDisturbConfig from its persisted form, or None if unset."""
    if data is None:
        return None
    return DoNotDisturbConfig(
        start=time.fromisoformat(data["start"]) if data.get("start") else None,
        end=time.fromisoformat(data["end"]) if data.get("end") else None,
        allowed_sources_regex=data.get("allowed_sources_regex"),
        allowed_criticalities=(
            frozenset(NotificationCriticality(c) for c in data["allowed_criticalities"])
            if data.get("allowed_criticalities") is not None
            else None
        ),
        allowed_types=(
            frozenset(NotificationType(t) for t in data["allowed_types"])
            if data.get("allowed_types") is not None
            else None
        ),
    )


class DeliveryStatus(StrEnum):
    """Status of a delivery attempt.

    Values
    ------
    FILTERED : str
        Notification filtered by policy (terminal state).
    RATE_LIMITED : str
        Notification rate-limited, scheduled for retry.
    IN_PROGRESS : str
        Delivery attempt in flight.
    TRANSIENT_FAIL : str
        Temporary failure, retryable (e.g., network timeout).
    PERMANENT_FAIL : str
        Permanent failure, no retry (e.g., invalid config).
    SUCCESS : str
        Delivery succeeded (terminal state).

    """

    FILTERED = "FILTERED"
    RATE_LIMITED = "RATE_LIMITED"
    IN_PROGRESS = "IN_PROGRESS"
    TRANSIENT_FAIL = "TRANSIENT_FAIL"
    PERMANENT_FAIL = "PERMANENT_FAIL"
    SUCCESS = "SUCCESS"


class TaskOutcome(StrEnum):
    """Terminal outcome of a delivery task as reported to the orchestrator.

    Values
    ------
    DELIVERED : str
        Task delivered successfully.
    FAILED : str
        Task failed permanently (no further retries).
    FILTERED : str
        Task filtered out by policy before delivery.

    """

    DELIVERED = "delivered"
    FAILED = "failed"
    FILTERED = "filtered"


@dataclass(frozen=True)
class NotificationDeliveryTask:
    """One task = one (notification × recipient × channel) delivery.

    Immutable description of work. Self-contained with all information needed
    for deterministic processing (crash recovery, replay).
    """

    job_id: UUID
    recipient_id: str
    channel_info: ChannelInfo
    payload: NotificationPayload
    policy: RecipientNotificationPolicy
    contact_info: RecipientContactInfo
    tts_settings: TTSSettings | None  # NEW: TTS settings for this delivery task

    created_at: datetime
    snapshot_id: str | None = None
    is_retry: bool = False

    @classmethod
    def from_snapshot(cls, job_id: UUID, snapshot: dict) -> NotificationDeliveryTask:
        """Reconstruct task from persisted snapshot.

        Used for retry recovery after Home Assistant restart.

        Args:
            job_id: Job identifier.
            snapshot: Persisted task data dictionary.

        Returns:
            Reconstructed NotificationDeliveryTask.

        Raises:
            ValueError: If snapshot data is invalid or incomplete.

        """
        from datetime import UTC  # noqa: PLC0415

        try:
            # Reconstruct ChannelInfo
            channel_data = snapshot["channel_info"]
            channel_info = ChannelInfo(
                id=channel_data["id"],
                label=channel_data.get("name", channel_data["id"]),  # Fallback to id
                scope=ChannelScope(
                    channel_data.get("scope", ChannelScope.RECIPIENT.value)
                ),
                integration=channel_data.get("adapter_type"),
            )

            # Reconstruct NotificationPayload
            payload_data = snapshot["payload"]
            payload = NotificationPayload(
                notification_id=payload_data.get("notification_id", str(job_id)),
                source=payload_data.get(
                    "source",
                    payload_data.get("metadata", {}).get("source", "ans_recovery"),
                ),
                title=payload_data["title"],
                message=payload_data["message"],
                type=NotificationType(
                    payload_data.get("type", NotificationType.INFO.value)
                ),
                criticality=NotificationCriticality(
                    payload_data.get(
                        "criticality", NotificationCriticality.MEDIUM.value
                    )
                ),
                created_at=datetime.fromisoformat(payload_data["timestamp"]),
                context=payload_data.get("context", payload_data.get("metadata", {})),
                link=payload_data.get("link"),
                image=payload_data.get("image"),
                video=payload_data.get("video"),
                file=payload_data.get("file"),
                actions=payload_data.get("actions", []),
                channel_data=payload_data.get("channel_data", {}),
            )

            # Reconstruct RecipientNotificationPolicy
            policy_data = snapshot["policy"]
            _allowed_types_raw = policy_data.get(
                "allowed_types", [t.value for t in NotificationType]
            )
            policy = RecipientNotificationPolicy(
                rate_limit=policy_data["rate_limit"],
                rate_limit_window=policy_data["rate_limit_window"],
                retry_attempts=policy_data["retry_attempts"],
                allowed_types=frozenset(
                    NotificationType(t) for t in _allowed_types_raw
                ),
                blocked_sources_regex=policy_data.get("blocked_sources_regex"),
                # DND is replayed exactly as it was when the notification was
                # originally sent (docs/advanced.md: "retries use the
                # configuration that was active when the notification was
                # originally sent"). Whether the window is *active* is still
                # re-evaluated against wall-clock time on every attempt by
                # FilterEngine — only the window/bypass configuration itself
                # is persisted here, not a point-in-time verdict.
                dnd=_dnd_config_from_dict(policy_data.get("dnd")),
            )

            # Reconstruct RecipientContactInfo
            contact_data = snapshot["contact_info"]
            contact_info = RecipientContactInfo(
                email_address=contact_data.get("email"),
                phone_number=contact_data.get("phone"),
            )

            # Reconstruct TTSSettings if present (for TTS recipients)
            tts_settings = None
            if "tts_settings" in snapshot and snapshot["tts_settings"] is not None:
                from .recipient import TTSSettings  # noqa: PLC0415

                tts_settings = TTSSettings.from_dict(snapshot["tts_settings"])

            return cls(
                job_id=job_id,
                recipient_id=snapshot["recipient_id"],
                channel_info=channel_info,
                payload=payload,
                policy=policy,
                contact_info=contact_info,
                tts_settings=tts_settings,
                created_at=datetime.now(UTC),  # New timestamp for retry
                snapshot_id=None,
                is_retry=True,
            )

        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"Invalid task snapshot data: {e}") from e

    def to_dict(self) -> dict:
        """Serialize task to dictionary for persistence.

        Returns:
            Dictionary representation of task suitable for JSON storage.

        """
        data = {
            "job_id": str(self.job_id),
            "recipient_id": self.recipient_id,
            "channel_info": {
                "id": self.channel_info.id,
                "name": self.channel_info.label,
                "adapter_type": self.channel_info.integration,
                "scope": self.channel_info.scope.value,
            },
            "payload": {
                "notification_id": self.payload.notification_id,
                "source": self.payload.source,
                "title": self.payload.title,
                "message": self.payload.message,
                "type": self.payload.type.value,
                "criticality": self.payload.criticality.value,
                "timestamp": self.payload.created_at.isoformat(),
                "context": self.payload.context,
                "link": self.payload.link,
                "image": self.payload.image,
                "video": self.payload.video,
                "file": self.payload.file,
                "actions": self.payload.actions,
                "channel_data": self.payload.channel_data,
            },
            "policy": {
                "rate_limit": self.policy.rate_limit,
                "rate_limit_window": self.policy.rate_limit_window,
                "retry_attempts": self.policy.retry_attempts,
                "allowed_types": sorted(t.value for t in self.policy.allowed_types),
                "blocked_sources_regex": self.policy.blocked_sources_regex,
                "dnd": _dnd_config_to_dict(self.policy.dnd),
            },
            "contact_info": {
                "email": self.contact_info.email_address,
                "phone": self.contact_info.phone_number,
            },
            "created_at": self.created_at.isoformat(),
            "is_retry": self.is_retry,
        }

        # Include TTS settings if present
        if self.tts_settings is not None:
            data["tts_settings"] = self.tts_settings.to_dict()

        return data


@dataclass
class Attempt:
    """One concrete execution attempt for a delivery task.

    Persisted and idempotency-relevant.
    """

    attempt_id: UUID
    job_id: UUID
    attempt_number: int
    idempotency_key: str
    status: DeliveryStatus
    started_at: datetime

    ended_at: datetime | None = None
    endpoint: str | None = None
    remote_id: str | None = None
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryResult:
    """Result returned by a channel adapter."""

    status: DeliveryStatus
    remote_id: str | None = None
    error: str | None = None
    mobile_tag: str | None = None


@dataclass(frozen=True)
class DeliveryState:
    """Aggregate state for a delivery task."""

    job_id: UUID
    status: DeliveryStatus
    last_attempt_number: int
    updated_at: datetime
    failure_reason: str | None = None


@dataclass(frozen=True)
class RetrySchedule:
    """Persisted retry intent."""

    job_id: UUID
    attempt_number: int
    run_at: datetime
    reason: str | None = None
