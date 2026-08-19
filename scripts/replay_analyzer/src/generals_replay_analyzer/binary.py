"""Fail-closed little-endian primitives for Zero Hour replay parsing."""

import struct
from dataclasses import dataclass
from typing import BinaryIO, cast

from .errors import InvalidStringLengthError, TruncatedReplayError

MAX_ENCODED_STRING_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Coord3D:
    """Engine-compatible location stored as three float32 values."""

    x: float
    y: float
    z: float


@dataclass(frozen=True)
class ICoord2D:
    """Engine-compatible integer coordinate stored as two signed int32 values."""

    x: int
    y: int


@dataclass(frozen=True)
class IRegion2D:
    """Engine-compatible rectangular pixel region stored as low then high coordinates."""

    lo: ICoord2D
    hi: ICoord2D


# TheSuperHackers @feature Leex 19/08/2026 Centralize bounded replay byte reads before field decoding.
class BinaryReader:
    """Read immutable replay bytes while retaining absolute offsets and raw slices."""

    def __init__(self, source: bytes | BinaryIO) -> None:
        """Create a reader from bytes or the remaining contents of a seekable binary stream."""
        if isinstance(source, bytes):
            self._data = source
        else:
            source_offset = source.tell()
            self._data = source.read()
            source.seek(source_offset)
        self._offset = 0

    @property
    def offset(self) -> int:
        """Return the number of bytes consumed from this reader's input."""
        return self._offset

    def read_exact(self, size: int) -> bytes:
        """Return exactly ``size`` bytes or raise at the field's starting offset."""
        field_offset = self._offset
        end_offset = field_offset + size
        value = self._data[field_offset:end_offset]
        if len(value) != size:
            raise TruncatedReplayError(
                "truncated_replay",
                field_offset,
                f"expected {size} bytes, found {len(value)}",
            )
        self._offset = end_offset
        return value

    def slice(self, start: int, end: int) -> bytes:
        """Return the raw immutable bytes in the half-open ``[start, end)`` range."""
        if end > len(self._data):
            available = max(len(self._data) - start, 0)
            raise TruncatedReplayError(
                "truncated_replay",
                start,
                f"expected {end - start} bytes, found {available}",
            )
        return self._data[start:end]

    def read_u8(self) -> int:
        """Read an unsigned 8-bit value."""
        return cast(int, struct.unpack("<B", self.read_exact(1))[0])

    def read_i8(self) -> int:
        """Read a signed 8-bit value."""
        return cast(int, struct.unpack("<b", self.read_exact(1))[0])

    def read_u16(self) -> int:
        """Read an unsigned little-endian 16-bit value."""
        return cast(int, struct.unpack("<H", self.read_exact(2))[0])

    def read_i16(self) -> int:
        """Read a signed little-endian 16-bit value."""
        return cast(int, struct.unpack("<h", self.read_exact(2))[0])

    def read_u32(self) -> int:
        """Read an unsigned little-endian 32-bit value."""
        return cast(int, struct.unpack("<I", self.read_exact(4))[0])

    def read_i32(self) -> int:
        """Read a signed little-endian 32-bit value."""
        return cast(int, struct.unpack("<i", self.read_exact(4))[0])

    def read_f32(self) -> float:
        """Read an IEEE-754 little-endian 32-bit floating-point value."""
        return cast(float, struct.unpack("<f", self.read_exact(4))[0])

    def read_coord3d(self) -> Coord3D:
        """Read a ``Coord3D`` in the engine's x, y, z float32 field order."""
        x, y, z = cast(tuple[float, float, float], struct.unpack("<fff", self.read_exact(12)))
        return Coord3D(x=x, y=y, z=z)

    def read_icoord2d(self) -> ICoord2D:
        """Read an ``ICoord2D`` in the engine's x, y signed-int32 field order."""
        x, y = cast(tuple[int, int], struct.unpack("<ii", self.read_exact(8)))
        return ICoord2D(x=x, y=y)

    def read_iregion2d(self) -> IRegion2D:
        """Read an ``IRegion2D`` in the engine's low then high coordinate order."""
        lo_x, lo_y, hi_x, hi_y = cast(tuple[int, int, int, int], struct.unpack("<iiii", self.read_exact(16)))
        return IRegion2D(lo=ICoord2D(x=lo_x, y=lo_y), hi=ICoord2D(x=hi_x, y=hi_y))

    def read_ascii(self) -> str:
        """Read a uint32-length-prefixed ASCII string."""
        return self._read_string_bytes().decode("ascii")

    def read_utf16le(self) -> str:
        """Read a uint32-length-prefixed UTF-16LE string."""
        return self._read_string_bytes().decode("utf-16-le")

    def _read_string_bytes(self) -> bytes:
        """Read an encoded string payload after enforcing its byte-length cap."""
        length_offset = self._offset
        encoded_length = self.read_u32()
        if encoded_length > MAX_ENCODED_STRING_BYTES:
            raise InvalidStringLengthError(
                "invalid_string_length",
                length_offset,
                f"encoded string length {encoded_length} exceeds {MAX_ENCODED_STRING_BYTES} bytes",
            )
        return self.read_exact(encoded_length)
