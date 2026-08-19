"""Typed failures raised while parsing replay bytes."""

from dataclasses import dataclass


# TheSuperHackers @feature Leex 19/08/2026 Preserve machine-readable replay parser failure context.
@dataclass(eq=False)
class ReplayParseError(Exception):
    """Base error containing a stable code and absolute replay byte offset."""

    code: str
    offset: int
    message: str

    def __str__(self) -> str:
        """Render parser context without discarding the stable error fields."""
        return f"[{self.code}] offset {self.offset}: {self.message}"


class InvalidMagicError(ReplayParseError):
    """Raised when a replay does not begin with its expected signature."""


class InvalidSliceRangeError(ReplayParseError):
    """Raised when a raw evidence slice does not name a valid replay byte range."""


class InvalidStringEncodingError(ReplayParseError):
    """Raised when encoded replay string bytes cannot be decoded by their declared codec."""


class TruncatedReplayError(ReplayParseError):
    """Raised when a requested field is not fully present in replay bytes."""


class UnsupportedArgumentTypeError(ReplayParseError):
    """Raised when a command references an unknown argument type."""


class InvalidStringLengthError(ReplayParseError):
    """Raised when an encoded replay string exceeds the safety limit."""
