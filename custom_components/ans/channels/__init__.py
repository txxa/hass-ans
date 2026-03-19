"""Channel and adapter management for ANS integration."""

from .base import (
    AdapterFactory,
    AdapterFailureType,
    AdapterType,
    ChannelRecord,
    ChannelStatus,
    DeliveryAdapter,
)
from .channel_manager import (
    ChannelManager,
    detect_media_players,
    detect_notification_channels,
    detect_tts_entities,
)
from .mobile_app import MobileAppDeliveryAdapter
from .persistent_notification import PersistentNotificationAdapter
from .signal import SignalDeliveryAdapter
from .tts_mediaplayer import TTSMediaPlayerAdapter

__all__ = [
    "AdapterFactory",
    "AdapterFailureType",
    "AdapterType",
    "ChannelManager",
    "ChannelRecord",
    "ChannelStatus",
    "DeliveryAdapter",
    "MobileAppDeliveryAdapter",
    "PersistentNotificationAdapter",
    "SignalDeliveryAdapter",
    "TTSMediaPlayerAdapter",
    "detect_media_players",
    "detect_notification_channels",
    "detect_tts_entities",
]
