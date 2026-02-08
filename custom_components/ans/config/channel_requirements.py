"""Channel requirement checking for recipient configuration.

This module defines what contact information each notification channel requires
and provides utilities to filter available channels based on recipient data.
"""

from __future__ import annotations

from typing import TypedDict

from ..const import PERSISTENT_NOTIFICATION_CHANNEL


class ChannelRequirement(TypedDict):
    """Channel requirement definition.

    Attributes
    ----------
    requires_email : bool
        Whether this channel requires an email address
    requires_phone : bool
        Whether this channel requires a phone number
    requires_ha_user : bool
        Whether this channel requires linkage to a Home Assistant user
    description : str
        Human-readable description of the requirement

    """

    requires_email: bool
    requires_phone: bool
    requires_ha_user: bool
    description: str


# Channel requirement definitions
# Maps channel ID patterns to their requirements
CHANNEL_REQUIREMENTS: dict[str, ChannelRequirement] = {
    # Email-based channels
    "notify.smtp": {
        "requires_email": True,
        "requires_phone": False,
        "requires_ha_user": False,
        "description": "Requires email address",
    },
    "notify.sendgrid": {
        "requires_email": True,
        "requires_phone": False,
        "requires_ha_user": False,
        "description": "Requires email address",
    },
    "notify.gmail": {
        "requires_email": True,
        "requires_phone": False,
        "requires_ha_user": False,
        "description": "Requires email address",
    },
    # Phone-based channels
    "notify.signal": {
        "requires_email": False,
        "requires_phone": True,
        "requires_ha_user": False,
        "description": "Requires phone number",
    },
    "notify.sms": {
        "requires_email": False,
        "requires_phone": True,
        "requires_ha_user": False,
        "description": "Requires phone number",
    },
    "notify.twilio": {
        "requires_email": False,
        "requires_phone": True,
        "requires_ha_user": False,
        "description": "Requires phone number",
    },
    # HA user-based channels
    "notify.mobile_app": {
        "requires_email": False,
        "requires_phone": False,
        "requires_ha_user": True,
        "description": "Requires Home Assistant user",
    },
    # System channels (no requirements)
    PERSISTENT_NOTIFICATION_CHANNEL: {
        "requires_email": False,
        "requires_phone": False,
        "requires_ha_user": False,
        "description": "No contact info required",
    },
    "notify.tts": {
        "requires_email": False,
        "requires_phone": False,
        "requires_ha_user": False,
        "description": "No contact info required",
    },
}


def _matches_channel_pattern(channel_id: str, pattern: str) -> bool:
    """Check if a channel ID matches a requirement pattern.

    Supports prefix matching for dynamic channels (e.g., "notify.mobile_app" matches
    "notify.mobile_app_device123").

    Parameters
    ----------
    channel_id : str
        The full channel ID (e.g., "notify.mobile_app_sm_s911b")
    pattern : str
        The pattern to match against (e.g., "notify.mobile_app")

    Returns
    -------
    bool
        True if the channel matches the pattern

    """
    # Exact match
    if channel_id == pattern:
        return True

    # Prefix match for dynamic channels (e.g., notify.mobile_app_*)
    if channel_id.startswith(f"{pattern}_"):
        return True

    return False


def get_channel_requirements(channel_id: str) -> ChannelRequirement | None:
    """Get requirements for a specific channel.

    Parameters
    ----------
    channel_id : str
        The channel identifier (e.g., "notify.signal", "notify.mobile_app_device123")

    Returns
    -------
    ChannelRequirement | None
        The requirement object if found, None if channel has no defined requirements

    """
    # Check for exact or prefix match
    for pattern, requirement in CHANNEL_REQUIREMENTS.items():
        if _matches_channel_pattern(channel_id, pattern):
            return requirement

    # Unknown channel - assume no requirements (allows custom channels to work)
    return None


def filter_channels_by_contact_info(
    channel_ids: list[str],
    *,
    has_email: bool = False,
    has_phone: bool = False,
    has_ha_user: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """Filter channels based on available recipient contact information.

    Parameters
    ----------
    channel_ids : list[str]
        List of channel IDs to filter
    has_email : bool, optional
        Whether recipient has email configured, by default False
    has_phone : bool, optional
        Whether recipient has phone configured, by default False
    has_ha_user : bool, optional
        Whether recipient is linked to HA user, by default False

    Returns
    -------
    tuple[list[str], dict[str, str]]
        Tuple of (available_channels, unavailable_reasons)
        - available_channels: List of channel IDs that can be used
        - unavailable_reasons: Dict mapping unavailable channel IDs to reason strings

    """
    available: list[str] = []
    unavailable: dict[str, str] = {}

    for channel_id in channel_ids:
        requirement = get_channel_requirements(channel_id)

        # Unknown channel - allow it (conservative approach)
        if requirement is None:
            available.append(channel_id)
            continue

        # Check requirements
        missing_requirements: list[str] = []

        if requirement["requires_email"] and not has_email:
            missing_requirements.append("email address")
        if requirement["requires_phone"] and not has_phone:
            missing_requirements.append("phone number")
        if requirement["requires_ha_user"] and not has_ha_user:
            missing_requirements.append("Home Assistant user")

        if missing_requirements:
            # Build reason message
            if len(missing_requirements) == 1:
                reason = f"Missing {missing_requirements[0]}"
            else:
                reason = f"Missing {' and '.join(missing_requirements)}"
            unavailable[channel_id] = reason
        else:
            # All requirements met
            available.append(channel_id)

    return available, unavailable
