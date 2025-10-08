"""Exceptions for the Advanced Notification System."""


class ANSException(Exception):
    """Base exception for ANS."""


class ANSConfigError(ANSException):
    """Exception raised for configuration errors."""


class IdentityNotFoundError(ANSConfigError):
    """Exception raised when an identity is not found in the system."""
