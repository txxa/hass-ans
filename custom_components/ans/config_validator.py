"""Configuration validation module."""

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol

from .const import (
    CONFIG_VERSION_KEY,
    DEFAULT_RATE_LIMIT_MAX,
    DEFAULT_RETRIES_MAX,
    ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY,
    # ID_CONFIG_CONFIGURED_CHANNELS_KEY,
    ID_CONFIG_CHANNELS_KEY,
    ID_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY,
    ID_CONFIG_DND_ENABLED_KEY,
    ID_CONFIG_DND_END_KEY,
    ID_CONFIG_DND_END_MISSING_KEY,
    ID_CONFIG_DND_START_END_EQUALS_KEY,
    ID_CONFIG_DND_START_KEY,
    ID_CONFIG_DND_START_MISSING_KEY,
    ID_CONFIG_DND_TIMES_KEY,
    ID_CONFIG_EMAIL_KEY,
    ID_CONFIG_ID_KEY,
    # ID_CONFIG_ID_KEY,
    # ID_CONFIG_IDENTITY_ID_KEY,
    ID_CONFIG_NAME_KEY,
    ID_CONFIG_NOTIFICATION_TYPES_KEY,
    ID_CONFIG_PHONE_KEY,
    ID_CONFIG_RATE_LIMIT_KEY,
    ID_CONFIG_RETRY_ATTEMPTS_KEY,
    ID_CONFIG_TYPE_KEY,
    SYS_CONFIG_ENABLED_CHANNELS_KEY,
    SYS_CONFIG_RATE_LIMIT_MAX_KEY,
    SYS_CONFIG_RATE_LIMIT_WINDOW_KEY,
    SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY,
    # SYS_CONFIG_TTS_INTEGRATION_KEY,
)
from .models import Identity, IdentityConfig, SystemConfig

_LOGGER = logging.getLogger(__name__)


class FieldValidationError(Exception):
    """Custom exception for field validation errors."""

    def __init__(
        self, field: str, message: str, placeholders: dict[str, str] | None = None
    ):
        """Initialize with field name and error message."""
        self.field = field
        self.message = message
        self.placeholders = placeholders

    def __str__(self):
        """Format error message."""
        return f"{self.field}: {self.message}"
        # if self.field:
        #     return f"{self.field}: {self.message}"
        # return self.message


@dataclass
class ValidationContext:
    """Context object containing validation constraints and system limits."""

    system_limits: dict[str, Any] | None = None
    available_channels: list[str] = field(default_factory=list)

    def get_max_retry_attempts(self) -> int:
        """Get maximum retries from system limits."""
        if self.system_limits:
            return self.system_limits.get(
                SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY, DEFAULT_RETRIES_MAX
            )
        return DEFAULT_RETRIES_MAX

    def get_max_rate_limit(self) -> int:
        """Get maximum rate limit from system limits."""
        if self.system_limits:
            return self.system_limits.get(
                SYS_CONFIG_RATE_LIMIT_MAX_KEY, DEFAULT_RATE_LIMIT_MAX
            )
        return DEFAULT_RATE_LIMIT_MAX


# @dataclass
# class ValidationRule:
#     """Configuration for a validation rule."""

#     validator: Callable[[Any, str, ValidationContext], Any]
#     required: bool = False
#     default: Any = None
#     description: str = ""


class ConfigValidator:
    """Validates Identity, IdentityConfig and SystemConfig instances using voluptuous and regex."""

    # Time format regex pattern
    TIME_PATTERN = r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$"
    PHONE_PATTERN = r"^\+?[1-9]\d{1,14}$"  # E.164 format
    EMAIL_PATTERN = r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"  # RFC 5322

    @staticmethod
    def __time_to_sec(time: str) -> int:
        """Convert time string (HH:MM or HH:MM:SS) to seconds since midnight."""
        parts = time.split(":")
        if len(parts) == 2:
            h, m = parts
            s = 0
        elif len(parts) == 3:
            h, m, s = parts
        else:
            raise ValueError("Invalid time format, must be HH:MM or HH:MM:SS.")
        try:
            h = int(h)
            m = int(m)
            s = int(s)
        except (ValueError, TypeError) as e:
            raise ValueError("Time components must be integers.") from e
        if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
            raise ValueError("Time values out of range.")
        return h * 3600 + m * 60 + s

    @staticmethod
    def _validate_positive_integer(
        value: Any,
        min_val: int = 1,
        max_val: int | None = None,
    ) -> int:
        """Validate that value is a positive integer within optional bounds."""
        if not isinstance(value, int):
            raise TypeError("Value must be an integer.")

        if value < min_val:
            raise ValueError(f"Value must be >= {min_val}.")

        if max_val is not None and value > max_val:
            raise ValueError(f"Value must be <= {max_val}.")

        return value

    @staticmethod
    def _validate_non_negative_integer(
        value: Any,
        max_val: int | None = None,
    ) -> int:
        """Validate that value is a non-negative integer within optional bounds."""
        return ConfigValidator._validate_positive_integer(
            value, min_val=0, max_val=max_val
        )

    @staticmethod
    def _validate_string_or_none(value: Any) -> str | None:
        """Validate that value is a string or None."""
        if value is not None and not isinstance(value, str):
            raise TypeError("Value must be a string or None.")
        return value

    @staticmethod
    def _validate_string_list(
        value: Any,
        min_length: int = 0,
        allowed_values: list[str] | None = None,
        regex_pattern: str | None = None,
    ) -> list[str]:
        """Validate that value is a list of strings with optional constraints."""
        if not isinstance(value, list):
            raise TypeError("Value must be a list of strings.")

        if len(value) < min_length:
            raise ValueError(f"Value must contain at least {min_length} items.")

        # Compile regex pattern if provided
        regex = None
        if regex_pattern:
            try:
                regex = re.compile(regex_pattern)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern: {e}") from e

        for item in value:
            if not isinstance(item, str):
                raise TypeError("Each item in value must be a string.")

            if allowed_values and item not in allowed_values:
                raise ValueError(f"'{item}' is not a valid value.")

            if regex and not regex.match(item):
                raise ValueError(
                    f"'{item}' does not match the required pattern: {regex_pattern}"
                )

        return value

    @staticmethod
    def _validate_time_format(value: Any) -> str | None:
        """Validate time format (HH:MM and HH:MM:SS)."""
        if value is None:
            return None

        if not isinstance(value, str):
            raise TypeError("Value must be a string.")

        if not re.match(ConfigValidator.TIME_PATTERN, value):
            raise ValueError("Value must be in HH:MM or HH:MM:SS 24-hour format.")

        return value

    @staticmethod
    def _validate_email_format(value: Any) -> str | None:
        """Validate email format."""
        if value is None:
            return None

        if not isinstance(value, str):
            raise TypeError("Value must be a string.")

        try:
            vol.Email(value)
        except vol.Invalid as e:
            raise ValueError(f"Invalid value: {e}") from e

        return value

    @staticmethod
    def _validate_phone_format(value: Any) -> str | None:
        """Validate phone number format (E.164)."""
        if value is None:
            return None

        if not isinstance(value, str):
            raise TypeError("Value must be a string.")

        if not re.match(ConfigValidator.PHONE_PATTERN, value):
            raise ValueError("Value must be in E.164 format (e.g., +1234567890).")

        return value

    @staticmethod
    def _validate_regex_pattern(value: str) -> str:
        """Validate regex pattern."""
        if not isinstance(value, str):
            raise TypeError("Value must be a string.")

        try:
            re.compile(value)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern '{value}': {e}") from e

        return value

    @staticmethod
    def _validate_boolean(value: Any) -> bool:
        """Validate boolean value."""
        if not isinstance(value, bool):
            raise TypeError("Value must be a boolean.")
        return value

    @staticmethod
    def _validate_identity_basic_settings(
        retry_attempts: int,
        rate_limit: int,
        notification_types: list[Any],
        blocked_sources_pattern: str | None,
        validation_context: ValidationContext | None = None,
    ) -> None:
        """Validate basic identity settings consistency."""
        # Validate retry attempts
        try:
            max_val = None
            placeholders = {"min": "0"}
            if validation_context:
                max_val = validation_context.get_max_retry_attempts()
                placeholders["max"] = str(max_val)
            ConfigValidator._validate_non_negative_integer(retry_attempts, max_val)
        except ValueError as e:
            raise FieldValidationError(
                ID_CONFIG_RETRY_ATTEMPTS_KEY, str(e), placeholders
            ) from e
        except TypeError as e:
            raise FieldValidationError(ID_CONFIG_RETRY_ATTEMPTS_KEY, str(e)) from e

        # Validate rate limit
        try:
            max_val = None
            placeholders = {"min": "0"}
            if validation_context:
                max_val = validation_context.get_max_rate_limit()
                placeholders["max"] = str(max_val)
            ConfigValidator._validate_non_negative_integer(rate_limit, max_val)
        except ValueError as e:
            raise FieldValidationError(
                ID_CONFIG_RATE_LIMIT_KEY, str(e), placeholders
            ) from e
        except TypeError as e:
            raise FieldValidationError(ID_CONFIG_RATE_LIMIT_KEY, str(e)) from e

        # Validate notification types
        from .models import NotificationType

        for notification_type in notification_types:
            if isinstance(notification_type, str):
                try:
                    notification_type = NotificationType(notification_type)
                except ValueError as e:
                    raise FieldValidationError(
                        ID_CONFIG_NOTIFICATION_TYPES_KEY,
                        f"Invalid notification type '{notification_type}'.",
                    ) from e
            if not isinstance(notification_type, NotificationType):
                raise FieldValidationError(
                    ID_CONFIG_NOTIFICATION_TYPES_KEY,
                    f"Invalid notification type '{notification_type}'.",
                )

        # Validate blocked sources pattern if provided
        if blocked_sources_pattern:
            try:
                ConfigValidator._validate_regex_pattern(blocked_sources_pattern)
            except ValueError as e:
                raise FieldValidationError(
                    ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY, str(e)
                ) from e
            except TypeError as e:
                raise FieldValidationError(
                    ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY, str(e)
                ) from e

    @staticmethod
    def _validate_identity_channel_mapping(
        channels_low: list[str] | None,
        channels_medium: list[str] | None,
        channels_high: list[str] | None,
        channels_critical: list[str] | None,
        available_channels: list[str] | None = None,
    ) -> None:
        """Validate channel mapping consistency."""
        from .models import NotificationCriticality

        # Validate each channel list
        for channels, level in [
            (channels_low, NotificationCriticality.LOW.value.lower()),
            (channels_medium, NotificationCriticality.MEDIUM.value.lower()),
            (channels_high, NotificationCriticality.HIGH.value.lower()),
            (channels_critical, NotificationCriticality.CRITICAL.value.lower()),
        ]:
            if channels is not None:
                try:
                    ConfigValidator._validate_string_list(
                        channels,
                        min_length=0,
                        allowed_values=available_channels,
                        regex_pattern="^notify\\.[a-zA-Z0-9_]+$",
                    )
                except ValueError as e:
                    raise FieldValidationError(
                        f"{ID_CONFIG_CHANNELS_KEY}_{level}", str(e)
                    ) from e
                except TypeError as e:
                    raise FieldValidationError(
                        f"{ID_CONFIG_CHANNELS_KEY}_{level}", str(e)
                    ) from e

    @staticmethod
    def _validate_identity_dnd_settings(
        dnd_enabled: bool,
        dnd_start: str | None,
        dnd_end: str | None,
        dnd_allowed_sources_pattern: str | None,
    ) -> None:
        """Validate DND settings consistency."""
        s_sec = None
        e_sec = None

        if not isinstance(dnd_enabled, bool):
            raise FieldValidationError(ID_CONFIG_DND_ENABLED_KEY, "Must be a boolean.")

        if dnd_enabled:
            # When DND is enabled, start and end times must be set
            if dnd_start is None or dnd_end is None:
                raise FieldValidationError(
                    ID_CONFIG_DND_TIMES_KEY,
                    "Start and end times must be set when DND is enabled.",
                )

        # Validate start time if provided
        if dnd_start:
            # Validate time format
            try:
                ConfigValidator._validate_time_format(dnd_start)
            except ValueError as e:
                raise FieldValidationError(ID_CONFIG_DND_START_KEY, str(e)) from e
            except TypeError as e:
                raise FieldValidationError(ID_CONFIG_DND_START_KEY, str(e)) from e
            # Convert to seconds for comparison
            try:
                s_sec = ConfigValidator.__time_to_sec(dnd_start)
            except (ValueError, TypeError, IndexError) as e:
                raise FieldValidationError(
                    ID_CONFIG_DND_START_KEY,
                    "Invalid time format, must be HH:MM:SS.",
                ) from e

        # Validate end time if provided
        if dnd_end:
            # Validate time format
            try:
                ConfigValidator._validate_time_format(dnd_end)
            except ValueError as e:
                raise FieldValidationError(ID_CONFIG_DND_END_KEY, str(e)) from e
            except TypeError as e:
                raise FieldValidationError(ID_CONFIG_DND_END_KEY, str(e)) from e
            # Convert to seconds for comparison
            try:
                e_sec = ConfigValidator.__time_to_sec(dnd_end)
            except (ValueError, TypeError, IndexError) as e:
                raise FieldValidationError(
                    ID_CONFIG_DND_END_KEY,
                    "Invalid time format, must be HH:MM:SS.",
                ) from e

        # If both times are provided, ensure they are not the same
        if s_sec and e_sec:
            if s_sec == e_sec:
                raise FieldValidationError(
                    ID_CONFIG_DND_START_END_EQUALS_KEY,
                    "Start and end times cannot be the same.",
                )

        # Validate allowed sources pattern if provided
        if dnd_allowed_sources_pattern:
            try:
                ConfigValidator._validate_regex_pattern(dnd_allowed_sources_pattern)
            except ValueError as e:
                raise FieldValidationError(
                    ID_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY, str(e)
                ) from e
            except TypeError as e:
                raise FieldValidationError(
                    ID_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY, str(e)
                ) from e

    ########## Schema-based validation methods ##########

    @staticmethod
    def validate_system_settings_schema(config: dict) -> dict:
        """Validate the system configuration against a schema."""
        schema = vol.Schema(
            {
                vol.Required(SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY): vol.All(
                    int, vol.Range(min=0)
                ),
                vol.Required(SYS_CONFIG_RATE_LIMIT_MAX_KEY): vol.All(
                    int, vol.Range(min=0)
                ),
                vol.Required(SYS_CONFIG_RATE_LIMIT_WINDOW_KEY): vol.All(
                    int, vol.Range(min=1)
                ),
                vol.Required(SYS_CONFIG_ENABLED_CHANNELS_KEY): vol.Any(
                    vol.All([str], vol.Length(min=1)), vol.Length(min=0)
                ),
                # vol.Optional(SYS_CONFIG_TTS_INTEGRATION_KEY): vol.Any(str, None),
            },
            extra=vol.ALLOW_EXTRA,
        )
        return schema(config)

    @staticmethod
    def validate_identity_selection_schema(identity: dict) -> dict:
        """Validate the identity selection against a schema."""
        from .models import IdentityType

        schema = vol.Schema(
            {
                vol.Required(ID_CONFIG_ID_KEY): vol.All(str),
                vol.Required(ID_CONFIG_TYPE_KEY): vol.In(
                    [t.value for t in IdentityType]
                ),
            },
            extra=vol.ALLOW_EXTRA,
        )
        return schema(identity)

    @staticmethod
    def validate_identity_definition_schema(identity: dict) -> dict:
        """Validate the identity against a schema."""
        schema = vol.Schema(
            {
                vol.Required(ID_CONFIG_NAME_KEY): vol.All(str),
                vol.Optional(ID_CONFIG_EMAIL_KEY): vol.Any(
                    None, vol.Match(ConfigValidator.EMAIL_PATTERN)
                ),
                vol.Optional(ID_CONFIG_PHONE_KEY): vol.Any(
                    None, vol.Match(ConfigValidator.PHONE_PATTERN)
                ),
            },
            extra=vol.PREVENT_EXTRA,
        )
        return schema(identity)

    @staticmethod
    def validate_identity_basic_settings_schema(
        settings: dict, validation_context: ValidationContext
    ) -> dict:
        """Validate the basic identity settings against a schema."""
        from .models import NotificationType

        # Voluptuous based form validation
        schema = vol.Schema(
            {
                vol.Required(ID_CONFIG_RETRY_ATTEMPTS_KEY): vol.All(
                    int,
                    vol.Range(min=0, max=validation_context.get_max_retry_attempts()),
                ),
                vol.Required(ID_CONFIG_RATE_LIMIT_KEY): vol.All(
                    int,
                    vol.Range(min=0, max=validation_context.get_max_rate_limit()),
                ),
                vol.Required(ID_CONFIG_NOTIFICATION_TYPES_KEY): vol.All(
                    [vol.In([t.value for t in NotificationType])], vol.Length(min=1)
                ),
                vol.Optional(ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY): vol.All(
                    str, ConfigValidator._validate_regex_pattern
                ),
            },
            extra=vol.PREVENT_EXTRA,
        )
        # validate_input = schema(settings)

        # # Extended non-voluptuous based form validation
        # try:
        #     if validate_input.get(ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY):
        #         ConfigValidator._validate_regex_pattern(
        #             validate_input.get(ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY)
        #         )
        # except (ValueError, TypeError) as e:
        #     raise vol.Invalid(
        #         message=str(e), path=[ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY]
        #     ) from e

        # return validate_input
        return schema(settings)

    @staticmethod
    def validate_identity_channel_mapping_schema(
        settings: dict, validation_context: ValidationContext
    ) -> dict:
        """Validate the identity channel mapping against a schema."""
        from .models import NotificationCriticality

        criticality_levels = [c.value for c in NotificationCriticality]

        schema_dict = {}

        for crit in criticality_levels:
            key = f"{ID_CONFIG_CHANNELS_KEY}_{crit.lower()}"
            schema_dict[vol.Optional(key)] = vol.Any(
                vol.All(
                    [vol.In(validation_context.available_channels)], vol.Length(min=1)
                ),
                vol.Length(min=0),
            )

        schema = vol.Schema(
            schema_dict,
            extra=vol.PREVENT_EXTRA,
        )

        return schema(settings)

    @staticmethod
    def validate_identity_channel_mapping_schema1(
        settings: dict,
        validation_context: ValidationContext,
        values_required: bool = True,
    ) -> dict:
        """Validate the identity channel mapping against a schema."""
        from .models import NotificationCriticality

        criticality_levels = [c.value for c in NotificationCriticality]

        schema_dict = {}

        for crit in criticality_levels:
            key = f"{ID_CONFIG_CHANNELS_KEY}_{crit.lower()}"
            if values_required:
                schema_dict[vol.Optional(key)] = vol.All(
                    [vol.In(validation_context.available_channels)], vol.Length(min=1)
                )
            else:
                schema_dict[vol.Optional(key)] = vol.Length(min=0)

        schema = vol.Schema(
            schema_dict,
            extra=vol.PREVENT_EXTRA,
        )

        return schema(settings)

    @staticmethod
    def validate_identity_dnd_settings_schema(data: dict) -> dict:
        """Validate the DND settings against a schema."""
        schema = vol.Schema(
            {
                vol.Required(ID_CONFIG_DND_ENABLED_KEY): vol.All(bool),
                vol.Optional(ID_CONFIG_DND_START_KEY): vol.Match(
                    ConfigValidator.TIME_PATTERN
                ),
                vol.Optional(ID_CONFIG_DND_END_KEY): vol.Match(
                    ConfigValidator.TIME_PATTERN
                ),
                vol.Optional(ID_CONFIG_DND_ALLOWED_SOURCES_PATTERN_KEY): vol.All(
                    str, ConfigValidator._validate_regex_pattern
                ),
            },
            extra=vol.PREVENT_EXTRA,
        )
        validated_schema = schema(data)

        if validated_schema.get(ID_CONFIG_DND_ENABLED_KEY):
            if not validated_schema.get(ID_CONFIG_DND_START_KEY):
                raise vol.Invalid(
                    message="Start time must be set when DND is enabled.",
                    path=[ID_CONFIG_DND_START_MISSING_KEY],
                )
            if not validated_schema.get(ID_CONFIG_DND_END_KEY):
                raise vol.Invalid(
                    message="End time must be set when DND is enabled.",
                    path=[ID_CONFIG_DND_END_MISSING_KEY],
                )
        if ConfigValidator.__time_to_sec(
            validated_schema.get(ID_CONFIG_DND_START_KEY)
        ) == ConfigValidator.__time_to_sec(validated_schema.get(ID_CONFIG_DND_END_KEY)):
            raise vol.Invalid(
                message="Start and end times cannot be the same.",
                path=["base", ID_CONFIG_DND_START_END_EQUALS_KEY],
            )

        return validated_schema

    @staticmethod
    def validate_identity(identity: Identity) -> None:
        """Validate the identity."""

        # Validate id
        if not identity.id:
            raise FieldValidationError(ID_CONFIG_ID_KEY, "ID cannot be empty.")
        try:
            ConfigValidator._validate_string_or_none(identity.id)
        except TypeError as e:
            raise FieldValidationError(ID_CONFIG_ID_KEY, str(e)) from e

        # Validate type
        from .models import IdentityType

        if not isinstance(identity.type, IdentityType):
            raise FieldValidationError(
                ID_CONFIG_TYPE_KEY,
                "Type must be a string or IdentityType enum.",
            )

        # Validate name
        if not identity.name:
            raise FieldValidationError(ID_CONFIG_NAME_KEY, "Name cannot be empty.")
        try:
            ConfigValidator._validate_string_or_none(identity.name)
        except TypeError as e:
            raise FieldValidationError(ID_CONFIG_NAME_KEY, str(e)) from e

        # Validate email format
        if identity.email:
            try:
                ConfigValidator._validate_email_format(identity.email)
            except ValueError as e:
                raise FieldValidationError(ID_CONFIG_EMAIL_KEY, str(e)) from e
            except TypeError as e:
                raise FieldValidationError(ID_CONFIG_EMAIL_KEY, str(e)) from e

        # Validate phone format
        if identity.phone:
            try:
                ConfigValidator._validate_phone_format(identity.phone)
            except ValueError as e:
                raise FieldValidationError(ID_CONFIG_PHONE_KEY, str(e)) from e
            except TypeError as e:
                raise FieldValidationError(ID_CONFIG_PHONE_KEY, str(e)) from e

        # Validate config version
        if not isinstance(identity.version, int) or identity.version < 1:
            raise FieldValidationError(
                CONFIG_VERSION_KEY, "Must be a positive integer."
            )

    @staticmethod
    def validate_identity_config(config: IdentityConfig) -> None:
        """Validate the user configuration."""

        # Validate basic settings
        ConfigValidator._validate_identity_basic_settings(
            config.retry_attempts,
            config.rate_limit,
            config.notification_types,
            config.blocked_sources_pattern,
        )
        # Validate channel mapping
        ConfigValidator._validate_identity_channel_mapping(
            config.channels_low,
            config.channels_medium,
            config.channels_high,
            config.channels_critical,
        )
        # Validate DND settings
        ConfigValidator._validate_identity_dnd_settings(
            config.dnd_enabled,
            config.dnd_start,
            config.dnd_end,
            config.dnd_allowed_sources_pattern,
        )

        # Validate config version
        if not isinstance(config.version, int) or config.version < 1:
            raise FieldValidationError(
                CONFIG_VERSION_KEY, "Must be a positive integer."
            )

    @staticmethod
    def validate_system_config(config: SystemConfig) -> None:
        """Validate the system configuration."""
        # Validate retry attempts
        try:
            ConfigValidator._validate_non_negative_integer(config.retry_attempts_max)
        except ValueError as e:
            raise FieldValidationError(SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY, str(e)) from e
        except TypeError as e:
            raise FieldValidationError(SYS_CONFIG_RETRY_ATTEMPTS_MAX_KEY, str(e)) from e

        # Validate rate limit
        try:
            ConfigValidator._validate_non_negative_integer(config.rate_limit_max)
        except ValueError as e:
            raise FieldValidationError(SYS_CONFIG_RATE_LIMIT_MAX_KEY, str(e)) from e
        except TypeError as e:
            raise FieldValidationError(SYS_CONFIG_RATE_LIMIT_MAX_KEY, str(e)) from e

        # Validate rate limit window
        try:
            ConfigValidator._validate_positive_integer(config.rate_limit_window)
        except ValueError as e:
            raise FieldValidationError(SYS_CONFIG_RATE_LIMIT_WINDOW_KEY, str(e)) from e
        except TypeError as e:
            raise FieldValidationError(SYS_CONFIG_RATE_LIMIT_WINDOW_KEY, str(e)) from e

        # Validate enabled channels
        try:
            ConfigValidator._validate_string_list(
                config.enabled_channels,
                min_length=1,
                regex_pattern="^notify\\.[a-zA-Z0-9_]+$",
            )
        except ValueError as e:
            raise FieldValidationError(SYS_CONFIG_ENABLED_CHANNELS_KEY, str(e)) from e
        except TypeError as e:
            raise FieldValidationError(SYS_CONFIG_ENABLED_CHANNELS_KEY, str(e)) from e

        # Validate config version
        if not isinstance(config.version, int) or config.version < 1:
            raise FieldValidationError(
                CONFIG_VERSION_KEY, "Must be a positive integer."
            )

    # @staticmethod
    # def validate_identity_config(config: dict, system_limits: dict) -> dict:
    #     """Validate the user configuration against a schema."""
    #     from .models import NotificationCriticality, NotificationType

    #     schema = vol.Schema(
    #         {
    #             vol.Required(ID_CONFIG_IDENTITY_ID_KEY): vol.All(str),
    #             vol.Optional(ID_CONFIG_RATE_LIMIT_KEY): vol.All(
    #                 int,
    #                 vol.Range(min=0, max=system_limits[SYS_CONFIG_RATE_LIMIT_MAX_KEY]),
    #             ),
    #             vol.Optional(ID_CONFIG_RETRY_ATTEMPTS_KEY): vol.All(
    #                 int, vol.Range(min=0, max=system_limits[SYS_CONFIG_RETRIES_MAX_KEY])
    #             ),
    #             vol.Required(ID_CONFIG_CHANNELS_KEY): vol.All(
    #                 [vol.In([c.value for c in NotificationCriticality])],
    #                 vol.Length(min=1),
    #             ),
    #             vol.Required(ID_CONFIG_NOTIFICATION_TYPES_KEY): vol.All(
    #                 [vol.In([t.value for t in NotificationType])], vol.Length(min=1)
    #             ),
    #             vol.Required(ID_CONFIG_DND_ENABLED_KEY): bool,
    #             vol.Optional(ID_CONFIG_DND_START_KEY): vol.Match(
    #                 r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    #             ),
    #             vol.Optional(ID_CONFIG_DND_END_KEY): vol.Match(
    #                 r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    #             ),
    #             vol.Optional(ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY): [str],
    #         },
    #         extra=vol.PREVENT_EXTRA,
    #     )
    #     return schema(config)

    # @staticmethod
    # def validate_rate_limit(value: int, context: ValidationContext) -> int:
    #     """Validate rate limit against system maximum."""
    #     max_val = context.get_max_rate_limit()
    #     return ConfigValidator._validate_non_negative_integer(
    #         value, ID_CONFIG_RATE_LIMIT_KEY, max_val=max_val
    #     )

    # @staticmethod
    # def validate_retry_attempts(value: int, context: ValidationContext) -> int:
    #     """Validate retry attempts against system maximum."""
    #     max_val = context.get_max_retries()
    #     return ConfigValidator._validate_non_negative_integer(
    #         value, ID_CONFIG_RETRY_ATTEMPTS_KEY, max_val=max_val
    #     )

    # ##########################################################################

    # # @staticmethod
    # # def _validate_channel_list(
    # #     value: Any,
    # #     field_name: str,
    # #     context: ValidationContext,
    # #     allowed_channels: list[ChannelInfo],
    # #     min_length: int = 0,
    # # ) -> list[ChannelInfo]:
    # #     """Validate that value is a list of ChannelInfo with optional constraints."""
    # #     if not isinstance(value, list):
    # #         raise TypeError(f"{field_name} must be a list of ChannelInfo.")

    # #     if len(value) < min_length:
    # #         raise ValueError(f"{field_name} must contain at least {min_length} items.")

    # #     if not context or not context.available_channels:
    # #         raise ValueError("No available channels found in validation context.")

    # #     for item in value:
    # #         if not isinstance(item, ChannelInfo):
    # #             raise TypeError(f"Each item in {field_name} must be a ChannelInfo instance.")

    # #         for allowed_channel in context.available_channels:
    # #             if item.name == allowed_channel.name:
    # #                 raise ValueError(f"'{item.name}' is not a valid channel for {field_name}.")

    # #     return value

    # # @staticmethod
    # # def _validate_string_in_list(
    # #     value: Any,
    # #     field_name: str,
    # #     context: ValidationContext,
    # #     allowed_values: list[str],
    # # ) -> str | None:
    # #     """Validate that value is a string within allowed list."""
    # #     if value is None:
    # #         return None

    # #     if not isinstance(value, str):
    # #         raise TypeError(f"{field_name} must be a string.")

    # #     if value not in allowed_values:
    # #         raise ValueError(f"'{value}' is not a valid value for {field_name}.")

    # #     return value

    # # -------------------------
    # # Field-specific validators using generic helpers
    # # -------------------------

    # @staticmethod
    # def validate_retries_max(
    #     value: int, context: ValidationContext | None = None
    # ) -> int:
    #     """Validate max retries is a non-negative integer."""
    #     ctx = context or ValidationContext()
    #     return ConfigValidator._validate_non_negative_integer(value, "max_retries", ctx)

    # @staticmethod
    # def validate_rate_limit_max(
    #     value: int, context: ValidationContext | None = None
    # ) -> int:
    #     """Validate rate limit max is a non-negative integer."""
    #     ctx = context or ValidationContext()
    #     return ConfigValidator._validate_non_negative_integer(
    #         value, "rate_limit_max", ctx
    #     )

    # @staticmethod
    # def validate_rate_limit_window(
    #     value: int, context: ValidationContext | None = None
    # ) -> int:
    #     """Validate rate limit window is a positive integer."""
    #     ctx = context or ValidationContext()
    #     return ConfigValidator._validate_positive_integer(
    #         value, "rate_limit_window", ctx
    #     )

    # # @staticmethod
    # # def validate_tts_integration(
    # #     value: str | None, context: ValidationContext | None = None
    # # ) -> str | None:
    # #     """Validate TTS integration is a string or None."""
    # #     ctx = context or ValidationContext()
    # #     return ConfigValidator._validate_string_or_none(value, "tts_integration", ctx)

    # @staticmethod
    # def validate_enabled_channels(
    #     channels: list[ChannelInfo], context: ValidationContext | None = None
    # ) -> list[ChannelInfo]:
    #     """Validate enabled channels is a non-empty list of strings."""
    #     # ctx = context or ValidationContext()
    #     # Ensure all channels are valid
    #     # if not context or not context.available_channels:
    #     #     raise ValueError("Available channels are not defined.")

    #     # for channel in channels:
    #     #     for allowed_value in context.available_channels:
    #     #         if channel.id != allowed_value.id:
    #     #             raise ValueError(f"'{channel.name}' is not a valid value for {field_name}.")
    #     #             raise ValueError(f"Invalid channel ID: {channel.id}")

    #     return channels
    #     #     ConfigValidator._validate_string_in_list(
    #     #         channel.name,
    #     #         SYS_CONFIG_ENABLED_CHANNELS_KEY,
    #     #         ctx,
    #     #         allowed_values=ctx.available_channels,
    #     #     )
    #     # return ConfigValidator._validate_string_list(
    #     #     channels, SYS_CONFIG_ENABLED_CHANNELS_KEY, ctx, min_length=1
    #     # )

    # @staticmethod
    # def validate_configured_channels(
    #     channels: list[ChannelInfo], context: ValidationContext | None = None
    # ) -> list[ChannelInfo]:
    #     """Validate configured channels is a non-empty list of strings."""
    #     # ctx = context or ValidationContext()
    #     # available = ctx.available_channels if ctx.available_channels else None
    #     # return ConfigValidator._validate_string_list(
    #     #     channels,
    #     #     ID_CONFIG_CONFIGURED_CHANNELS_KEY,
    #     #     ctx,
    #     #     min_length=1,
    #     #     allowed_values=available,
    #     # )
    #     return channels

    # @staticmethod
    # def validate_email(
    #     value: str | None, context: ValidationContext | None = None
    # ) -> str | None:
    #     """Validate email format."""
    #     ctx = context or ValidationContext()
    #     return ConfigValidator._validate_email_format(value, ID_CONFIG_EMAIL_KEY, ctx)

    # @staticmethod
    # def validate_phone(
    #     value: str | None, context: ValidationContext | None = None
    # ) -> str | None:
    #     """Validate phone number format."""
    #     ctx = context or ValidationContext()
    #     return ConfigValidator._validate_phone_format(value, "phone", ctx)

    # @staticmethod
    # def validate_dnd_schedule(
    #     start: str | None,
    #     end: str | None,
    #     context: ValidationContext | None = None,
    # ) -> tuple[str | None, str | None]:
    #     """Validate DND schedule."""
    #     ctx = context or ValidationContext()
    #     validated_start = ConfigValidator._validate_time_format(start, "DND start", ctx)
    #     validated_end = ConfigValidator._validate_time_format(end, "DND end", ctx)
    #     return validated_start, validated_end

    # @staticmethod
    # def validate_blocked_sources_pattern(
    #     pattern: str, context: ValidationContext | None = None
    # ) -> str:
    #     """Validate blocked sources pattern is a valid regex pattern."""
    #     ctx = context or ValidationContext()
    #     return ConfigValidator._validate_regex_pattern(
    #         pattern, ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY, ctx
    #     )

    # @staticmethod
    # def validate_rate_limit_with_system_max(
    #     value: int, context: ValidationContext
    # ) -> int:
    #     """Validate rate limit against system maximum."""
    #     max_val = context.get_max_rate_limit()
    #     return ConfigValidator._validate_non_negative_integer(
    #         value, ID_CONFIG_RATE_LIMIT_KEY, context, max_val=max_val
    #     )

    # @staticmethod
    # def validate_retry_attempts_with_system_max(
    #     value: int, context: ValidationContext
    # ) -> int:
    #     """Validate retry attempts against system maximum."""
    #     max_val = context.get_max_retries()
    #     return ConfigValidator._validate_non_negative_integer(
    #         value, ID_CONFIG_RETRY_ATTEMPTS_KEY, context, max_val=max_val
    #     )

    # # -------------------------
    # # Schema-based validation methods (unchanged for compatibility)
    # # -------------------------

    # @staticmethod
    # def validate_system_config(config: dict) -> dict:
    #     """Validate the system configuration against a schema."""
    #     schema = vol.Schema(
    #         {
    #             vol.Required(SYS_CONFIG_RETRIES_MAX_KEY): vol.All(
    #                 int, vol.Range(min=0)
    #             ),
    #             vol.Required(SYS_CONFIG_RATE_LIMIT_MAX_KEY): vol.All(
    #                 int, vol.Range(min=0)
    #             ),
    #             vol.Required(SYS_CONFIG_RATE_LIMIT_WINDOW_KEY): vol.All(
    #                 int, vol.Range(min=1)
    #             ),
    #             vol.Required(SYS_CONFIG_ENABLED_CHANNELS_KEY): vol.Any(
    #                 vol.All([str], vol.Length(min=1)), vol.Length(min=0)
    #             ),
    #             # vol.Optional(SYS_CONFIG_TTS_INTEGRATION_KEY): vol.Any(str, None),
    #         },
    #         extra=vol.PREVENT_EXTRA,
    #     )
    #     return schema(config)

    # # @staticmethod
    # # def validate_identity(identity: dict) -> dict:
    # #     """Validate the identity against a schema."""
    # #     from .models import IdentityType

    # #     schema = vol.Schema(
    # #         {
    # #             vol.Required(ID_CONFIG_ID_KEY): str,
    # #             vol.Required(ID_CONFIG_TYPE_KEY): vol.In(
    # #                 [t.value for t in IdentityType]
    # #             ),
    # #             vol.Optional(ID_CONFIG_NAME_KEY): str,
    # #             vol.Optional(ID_CONFIG_EMAIL_KEY): vol.Any(str, None),
    # #             vol.Optional(ID_CONFIG_PHONE_KEY): vol.Any(str, None),
    # #         },
    # #         extra=vol.PREVENT_EXTRA,
    # #     )
    # #     return schema(identity)

    # @staticmethod
    # def validate_identity_config(config: dict, system_limits: dict) -> dict:
    #     """Validate the user configuration against a schema."""
    #     from .models import NotificationCriticality, NotificationType

    #     schema = vol.Schema(
    #         {
    #             vol.Required(ID_CONFIG_IDENTITY_ID_KEY): vol.All(str),
    #             vol.Optional(ID_CONFIG_RATE_LIMIT_KEY): vol.All(
    #                 int,
    #                 vol.Range(min=0, max=system_limits[SYS_CONFIG_RATE_LIMIT_MAX_KEY]),
    #             ),
    #             vol.Optional(ID_CONFIG_RETRY_ATTEMPTS_KEY): vol.All(
    #                 int, vol.Range(min=0, max=system_limits[SYS_CONFIG_RETRIES_MAX_KEY])
    #             ),
    #             vol.Required(ID_CONFIG_CHANNELS_KEY): vol.All(
    #                 [vol.In([c.value for c in NotificationCriticality])],
    #                 vol.Length(min=1),
    #             ),
    #             vol.Required(ID_CONFIG_NOTIFICATION_TYPES_KEY): vol.All(
    #                 [vol.In([t.value for t in NotificationType])], vol.Length(min=1)
    #             ),
    #             vol.Required(ID_CONFIG_DND_ENABLED_KEY): bool,
    #             vol.Optional(ID_CONFIG_DND_START_KEY): vol.Match(
    #                 ConfigValidator.TIME_PATTERN
    #             ),
    #             vol.Optional(ID_CONFIG_DND_END_KEY): vol.Match(
    #                 ConfigValidator.TIME_PATTERN
    #             ),
    #             vol.Optional(ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY): [str],
    #         },
    #         extra=vol.PREVENT_EXTRA,
    #     )
    #     return schema(config)

    # # -------------------------
    # # Configuration-driven validation
    # # -------------------------

    # @staticmethod
    # def get_validation_rules() -> dict[str, ValidationRule]:
    #     """Get validation rules for all configuration fields."""
    #     return {
    #         # System config rules
    #         SYS_CONFIG_RETRIES_MAX_KEY: ValidationRule(
    #             validator=lambda v,
    #             f,
    #             c: ConfigValidator._validate_non_negative_integer(v, f, c),
    #             required=True,
    #             description="Maximum number of retries allowed globally",
    #         ),
    #         SYS_CONFIG_RATE_LIMIT_MAX_KEY: ValidationRule(
    #             validator=lambda v,
    #             f,
    #             c: ConfigValidator._validate_non_negative_integer(v, f, c),
    #             required=True,
    #             description="Global rate limit maximum",
    #         ),
    #         SYS_CONFIG_RATE_LIMIT_WINDOW_KEY: ValidationRule(
    #             validator=lambda v, f, c: ConfigValidator._validate_positive_integer(
    #                 v, f, c
    #             ),
    #             required=True,
    #             description="Time window for rate limiting in seconds",
    #         ),
    #         # SYS_CONFIG_TTS_INTEGRATION_KEY: ValidationRule(
    #         #     validator=lambda v, f, c: ConfigValidator._validate_string_or_none(
    #         #         v, f, c
    #         #     ),
    #         #     required=False,
    #         #     description="TTS integration identifier",
    #         # ),
    #         SYS_CONFIG_ENABLED_CHANNELS_KEY: ValidationRule(
    #             validator=lambda v, f, c: ConfigValidator._validate_string_list(
    #                 v, f, c, min_length=0
    #             ),
    #             required=True,
    #             description="list of enabled notification channels",
    #         ),
    #         # Identity config rules
    #         ID_CONFIG_RATE_LIMIT_KEY: ValidationRule(
    #             validator=lambda v,
    #             f,
    #             c: ConfigValidator.validate_rate_limit_with_system_max(v, c),
    #             required=False,
    #             description="Identity-specific rate limit",
    #         ),
    #         ID_CONFIG_RETRY_ATTEMPTS_KEY: ValidationRule(
    #             validator=lambda v,
    #             f,
    #             c: ConfigValidator.validate_retry_attempts_with_system_max(v, c),
    #             required=False,
    #             description="Number of retry attempts for this identity",
    #         ),
    #         ID_CONFIG_DND_ENABLED_KEY: ValidationRule(
    #             validator=lambda v, f, c: ConfigValidator._validate_boolean(v, f, c),
    #             required=True,
    #             description="Whether Do Not Disturb is enabled",
    #         ),
    #         ID_CONFIG_DND_START_KEY: ValidationRule(
    #             validator=lambda v, f, c: ConfigValidator._validate_time_format(
    #                 v, f, c
    #             ),
    #             required=False,
    #             description="Do Not Disturb start time",
    #         ),
    #         ID_CONFIG_DND_END_KEY: ValidationRule(
    #             validator=lambda v, f, c: ConfigValidator._validate_time_format(
    #                 v, f, c
    #             ),
    #             required=False,
    #             description="Do Not Disturb end time",
    #         ),
    #         ID_CONFIG_BLOCKED_SOURCES_PATTERN_KEY: ValidationRule(
    #             validator=lambda v, f, c: ConfigValidator._validate_regex_pattern(
    #                 v, f, c
    #             ),
    #             required=False,
    #             description="list of regex patterns for blocking sources",
    #         ),
    #         # Identity rules
    #         ID_CONFIG_EMAIL_KEY: ValidationRule(
    #             validator=lambda v, f, c: ConfigValidator._validate_email_format(
    #                 v, f, c
    #             ),
    #             required=False,
    #             description="Email address",
    #         ),
    #         ID_CONFIG_PHONE_KEY: ValidationRule(
    #             validator=lambda v, f, c: ConfigValidator._validate_phone_format(
    #                 v, f, c
    #             ),
    #             required=False,
    #             description="Phone number in E.164 format",
    #         ),
    #     }

    # @staticmethod
    # def validate_field(
    #     field_key: str, value: Any, context: ValidationContext | None = None
    # ) -> Any:
    #     """Validate a single field using the configuration-driven approach."""
    #     rules = ConfigValidator.get_validation_rules()

    #     if field_key not in rules:
    #         raise ValueError(f"No validation rule found for field: {field_key}")

    #     rule = rules[field_key]
    #     ctx = context or ValidationContext()

    #     try:
    #         return rule.validator(value, field_key, ctx)
    #     except (TypeError, ValueError) as e:
    #         _LOGGER.error("Validation failed for field '%s': %s", field_key, e)
    #         raise

    # @staticmethod
    # def validate_config_dict(
    #     config: dict[str, Any], context: ValidationContext | None = None
    # ) -> dict[str, Any]:
    #     """Validate a configuration dictionary using the rule-based approach."""
    #     ctx = context or ValidationContext()
    #     rules = ConfigValidator.get_validation_rules()
    #     validated = {}

    #     for field_key, value in config.items():
    #         if field_key in rules:
    #             try:
    #                 validated[field_key] = ConfigValidator.validate_field(
    #                     field_key, value, ctx
    #                 )
    #             except (TypeError, ValueError) as e:
    #                 _LOGGER.error(
    #                     "Validation failed for field '%s' with value '%s': %s",
    #                     field_key,
    #                     value,
    #                     e,
    #                 )
    #                 raise
    #         else:
    #             # Pass through unknown fields (for backward compatibility)
    #             validated[field_key] = value

    #     return validated

    # # -------------------------
    # # Enhanced validation methods with context support
    # # -------------------------

    # @staticmethod
    # def validate_system_config_with_context(
    #     config: dict, context: ValidationContext | None = None
    # ) -> dict:
    #     """Validate system configuration with enhanced context support."""
    #     ctx = context or ValidationContext()

    #     # First validate using the existing schema for compatibility
    #     validated = ConfigValidator.validate_system_config(config)

    #     # Then apply additional context-aware validations
    #     for field_key, value in validated.items():
    #         if field_key in ConfigValidator.get_validation_rules():
    #             validated[field_key] = ConfigValidator.validate_field(
    #                 field_key, value, ctx
    #             )

    #     return validated

    # @staticmethod
    # def validate_identity_config_with_context(
    #     config: dict, system_limits: dict, context: ValidationContext | None = None
    # ) -> dict:
    #     """Validate identity configuration with enhanced context support."""
    #     ctx = context or ValidationContext(system_limits=system_limits)

    #     # First validate using the existing schema for compatibility
    #     validated = ConfigValidator.validate_identity_config(config, system_limits)

    #     # Then apply additional context-aware validations for specific fields
    #     if (
    #         ID_CONFIG_RATE_LIMIT_KEY in validated
    #         and validated[ID_CONFIG_RATE_LIMIT_KEY] is not None
    #     ):
    #         validated[ID_CONFIG_RATE_LIMIT_KEY] = (
    #             ConfigValidator.validate_rate_limit_with_system_max(
    #                 validated[ID_CONFIG_RATE_LIMIT_KEY], ctx
    #             )
    #         )

    #     if (
    #         ID_CONFIG_RETRY_ATTEMPTS_KEY in validated
    #         and validated[ID_CONFIG_RETRY_ATTEMPTS_KEY] is not None
    #     ):
    #         validated[ID_CONFIG_RETRY_ATTEMPTS_KEY] = (
    #             ConfigValidator.validate_retry_attempts_with_system_max(
    #                 validated[ID_CONFIG_RETRY_ATTEMPTS_KEY], ctx
    #             )
    #         )

    #     return validated

    # @staticmethod
    # def validate_identity_with_context(
    #     identity: dict, context: ValidationContext | None = None
    # ) -> dict:
    #     """Validate identity with enhanced context support."""
    #     ctx = context or ValidationContext()

    #     # First validate using the existing schema for compatibility
    #     validated = ConfigValidator.validate_identity(identity)

    #     # Then apply additional context-aware validations for contact fields
    #     if (
    #         ID_CONFIG_EMAIL_KEY in validated
    #         and validated[ID_CONFIG_EMAIL_KEY] is not None
    #     ):
    #         try:
    #             validated[ID_CONFIG_EMAIL_KEY] = ConfigValidator.validate_email(
    #                 validated[ID_CONFIG_EMAIL_KEY], ctx
    #             )
    #         except (TypeError, ValueError) as e:
    #             _LOGGER.warning(
    #                 "Invalid email for identity %s: %s",
    #                 validated.get(ID_CONFIG_ID_KEY),
    #                 e,
    #             )
    #             validated[ID_CONFIG_EMAIL_KEY] = None

    #     if (
    #         ID_CONFIG_PHONE_KEY in validated
    #         and validated[ID_CONFIG_PHONE_KEY] is not None
    #     ):
    #         try:
    #             validated[ID_CONFIG_PHONE_KEY] = ConfigValidator.validate_phone(
    #                 validated[ID_CONFIG_PHONE_KEY], ctx
    #             )
    #         except (TypeError, ValueError) as e:
    #             _LOGGER.warning(
    #                 "Invalid phone for identity %s: %s",
    #                 validated.get(ID_CONFIG_ID_KEY),
    #                 e,
    #             )
    #             validated[ID_CONFIG_PHONE_KEY] = None

    #     return validated
