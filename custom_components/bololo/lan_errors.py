"""LAN failures distinguish safe fallback from an uncertain physical command."""

from .api import BololoError


class LanError(BololoError):
    """A LAN operation failed without establishing a successful result."""


class LanUnavailableError(LanError):
    """The operation failed before any physical control bytes could be sent.

    A transport may raise this after discovery, connection, or authentication
    failure. It must never use it after attempting to write a device command.
    """


class LanCommandUncertainError(LanError):
    """A control command may have been sent; another transport must not replay it."""


class LanProtocolError(LanError):
    """A device response or protocol value is invalid or unsupported."""
