"""Channel and adapter management for ANS integration."""

from .adapter_lifecycle import AdapterFactory, AdapterLifecycleManager, AdapterType
from .adapter_registry import AdapterRegistry
from .base import AdapterFailureType, DeliveryAdapter
from .channel_registry import ChannelRegistry
from .mobile_app import MobileAppDeliveryAdapter
from .persistent_notification import PersistentNotificationAdapter
from .signal import SignalDeliveryAdapter

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
]
