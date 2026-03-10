"""Exceptions for the Advanced Notification System."""


class ANSException(Exception):
    """Base exception for ANS."""


class ANSConfigError(ANSException):
    """Exception raised for configuration errors."""


class IdentityNotFoundError(ANSConfigError):
    """Exception raised when an identity is not found in the system."""


class ConfigEntryNotFoundError(ANSConfigError):
    """Exception raised when a config entry is not found."""


# TTS Delivery Exceptions
class TTSDeliveryError(ANSException):
    """Base exception for TTS delivery failures.

    Attributes:
        message: Error description
        is_permanent: If True, failure is permanent (no retry).
                     If False, failure is transient (should retry).

    """

    def __init__(self, message: str, is_permanent: bool = False):
        """Initialize TTS delivery error.

        Args:
            message: Error description
            is_permanent: Whether this is a permanent failure

        """
        super().__init__(message)
        self.is_permanent = is_permanent


class TTSVolumeControlError(TTSDeliveryError):
    """Volume control operation failed (always transient).

    Volume control failures should always be treated as transient
    since media players may become available later.
    """

    def __init__(self, message: str):
        """Initialize volume control error.

        Args:
            message: Error description

        """
        super().__init__(message, is_permanent=False)
