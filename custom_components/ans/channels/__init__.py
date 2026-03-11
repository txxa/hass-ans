"""Channel and adapter management for ANS integration."""

from .adapter_lifecycle import AdapterLifecycleManager, AdapterType
from .adapter_registry import AdapterRegistry
from .base import AdapterFactory, AdapterFailureType, DeliveryAdapter
from .channel_registry import (
    ChannelRegistry,
    detect_media_players,
    detect_notification_channels,
)
from .mobile_app import MobileAppDeliveryAdapter
from .persistent_notification import PersistentNotificationAdapter
from .signal import SignalDeliveryAdapter
from .tts_mediaplayer import TTSMediaPlayerAdapter

__all__ = [
    "AdapterFactory",
    "AdapterFailureType",
    "AdapterLifecycleManager",
    "AdapterRegistry",
    "AdapterType",
    "ChannelRegistry",
    "DeliveryAdapter",
    "MobileAppDeliveryAdapter",
    "PersistentNotificationAdapter",
    "SignalDeliveryAdapter",
    "TTSMediaPlayerAdapter",
    "detect_media_players",
    "detect_notification_channels",
]
