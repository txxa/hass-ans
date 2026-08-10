"""Configuration validation facade.

Recomposes the per-domain validators (``common``, ``channels``, ``recipient``,
``tts``, ``system``) into a single ``ConfigValidator`` surface so existing
callers (``ConfigValidator.validate_x(...)``) keep working unchanged. Only the
methods actually called from outside this package are exposed here; the
private ``_validate_*`` helpers live in and are imported directly from their
owning domain module.
"""

from __future__ import annotations

from . import channels, recipient, system, tts
from .common import FieldValidationError, ValidationContext
from .tts import validate_tts_service


class ConfigValidator:
    """Facade exposing the public validation API backed by per-domain modules."""

    validate_recipient_definition_schema = staticmethod(
        recipient.validate_recipient_definition_schema
    )
    validate_recipient_basic_settings_schema = staticmethod(
        recipient.validate_recipient_basic_settings_schema
    )
    validate_recipient_dnd_settings_schema = staticmethod(
        recipient.validate_recipient_dnd_settings_schema
    )
    validate_recipient_consistency = staticmethod(
        recipient.validate_recipient_consistency
    )
    validate_recipient = staticmethod(recipient.validate_recipient)
    validate_recipient_config = staticmethod(recipient.validate_recipient_config)

    validate_recipient_channel_mapping_schema = staticmethod(
        channels.validate_recipient_channel_mapping_schema
    )

    validate_recipient_tts_settings_schema = staticmethod(
        tts.validate_recipient_tts_settings_schema
    )

    validate_system_settings_schema = staticmethod(
        system.validate_system_settings_schema
    )
    validate_system_config = staticmethod(system.validate_system_config)


__all__ = [
    "ConfigValidator",
    "FieldValidationError",
    "ValidationContext",
    "validate_tts_service",
]
