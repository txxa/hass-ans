"""Channel primitives for the ANS system."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ChannelScope(StrEnum):
    """Scope of a notification channel.

    Values
    ------
    SYSTEM : str
        Channel delivers to the HA instance itself, not specific recipients.
        Example: persistent_notification
    RECIPIENT : str
        Channel delivers to individual recipients.
        Examples: mobile_app, email, SMS
    TTS : str
        Channel delivers via text-to-speech to a media player entity.
        Example: media_player.living_room
    """

    SYSTEM = "SYSTEM"
    RECIPIENT = "RECIPIENT"
    TTS = "TTS"


@dataclass(frozen=True)
class ChannelInfo:
    """Immutable channel definition with scope and metadata.

    Attributes
    ----------
    id : str
        Unique channel identifier (e.g., "notify.persistent_notification")
    label : str
        Human-readable display name
    scope : ChannelScope
        Whether channel is system-wide or recipient-specific
    integration : str | None
        Source integration domain (e.g., "mobile_app", "persistent_notification")

    """

    id: str
    label: str
    scope: ChannelScope
    integration: str | None = None
