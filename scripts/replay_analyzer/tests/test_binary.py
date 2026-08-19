"""Behavioral tests for fail-closed replay binary primitives."""

from io import BytesIO

import pytest

from generals_replay_analyzer.binary import BinaryReader, Coord3D, ICoord2D, IRegion2D
from generals_replay_analyzer.errors import (
    InvalidMagicError,
    InvalidSliceRangeError,
    InvalidStringEncodingError,
    InvalidStringLengthError,
    ReplayParseError,
    TruncatedReplayError,
    UnsupportedArgumentTypeError,
)


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        (InvalidMagicError, "invalid_magic"),
        (InvalidSliceRangeError, "invalid_slice_range"),
        (InvalidStringEncodingError, "invalid_string_encoding"),
        (TruncatedReplayError, "truncated_replay"),
        (UnsupportedArgumentTypeError, "unsupported_argument_type"),
        (InvalidStringLengthError, "invalid_string_length"),
    ],
)
def test_parser_errors_preserve_the_domain_code_field_offset_and_message(
    error_type: type[ReplayParseError], code: str
) -> None:
    """Reject error types that lose parser-specific failure context."""
    error = error_type(code, 17, "hand-derived parser failure")

    assert isinstance(error, ReplayParseError)
    assert not isinstance(error, ValueError)
    assert error.code == code
    assert error.offset == 17
    assert error.message == "hand-derived parser failure"
    assert str(error) == f"[{code}] offset 17: hand-derived parser failure"


def test_read_u8_consumes_one_unsigned_little_endian_byte_and_advances_offset() -> None:
    """Reject interpreting the one-byte field as signed or leaving its offset unchanged."""
    reader = BinaryReader(b"\xff")

    assert reader.read_u8() == 255
    assert reader.offset == 1


def test_read_i8_interprets_the_high_bit_as_a_negative_value() -> None:
    """Reject decoding signed command fields as their unsigned byte value."""
    assert BinaryReader(b"\x80").read_i8() == -128


def test_read_u16_decodes_little_endian_bytes() -> None:
    """Reject reversing the two-byte unsigned field order."""
    assert BinaryReader(b"\x34\x12").read_u16() == 0x1234


def test_read_i16_decodes_negative_little_endian_values() -> None:
    """Reject treating a signed 16-bit field as an unsigned coordinate."""
    assert BinaryReader(b"\xfe\xff").read_i16() == -2


def test_read_u32_decodes_little_endian_bytes() -> None:
    """Reject reversing four-byte replay frame values."""
    assert BinaryReader(b"\x78\x56\x34\x12").read_u32() == 0x12345678


def test_read_i32_decodes_negative_little_endian_values() -> None:
    """Reject treating signed 32-bit engine integers as object identifiers."""
    assert BinaryReader(b"\xfe\xff\xff\xff").read_i32() == -2


def test_read_f32_decodes_ieee_754_little_endian_bytes() -> None:
    """Reject decoding the 32-bit real representation with the wrong byte order."""
    assert BinaryReader(b"\x00\x00\x60\xc0").read_f32() == -3.5


def test_read_coord3d_follows_the_engine_x_y_z_float32_layout() -> None:
    """Reject changing the engine's three-float location field order or width."""
    reader = BinaryReader(b"\x00\x00\x80\x3f\x00\x00\x00\xc0\x00\x00\x60\x40")

    assert reader.read_coord3d() == Coord3D(x=1.0, y=-2.0, z=3.5)
    assert reader.offset == 12


def test_read_icoord2d_follows_the_engine_x_y_signed_int32_layout() -> None:
    """Reject decoding pixel coordinates as unsigned or in a swapped order."""
    reader = BinaryReader(b"\xfe\xff\xff\xff\x15\x03\x00\x00")

    assert reader.read_icoord2d() == ICoord2D(x=-2, y=789)
    assert reader.offset == 8


def test_read_iregion2d_follows_the_engine_low_then_high_coordinate_layout() -> None:
    """Reject flattening a pixel region in an order other than low then high x/y."""
    reader = BinaryReader(
        b"\xff\xff\xff\xff\x02\x00\x00\x00\x0a\x00\x00\x00\xfb\xff\xff\xff"
    )

    assert reader.read_iregion2d() == IRegion2D(lo=ICoord2D(x=-1, y=2), hi=ICoord2D(x=10, y=-5))
    assert reader.offset == 16


def test_read_ascii_uses_a_uint32_encoded_byte_length_prefix() -> None:
    """Reject treating the ASCII length as a character count of another width."""
    reader = BinaryReader(b"\x03\x00\x00\x00USA")

    assert reader.read_ascii() == "USA"
    assert reader.offset == 7


def test_read_utf16le_uses_an_encoded_byte_length_prefix() -> None:
    """Reject treating UTF-16 length as code units or decoding it with a host byte order."""
    reader = BinaryReader(b"\x06\x00\x00\x00A\x00\xa9\x03Z\x00")

    assert reader.read_utf16le() == "AΩZ"
    assert reader.offset == 10


def test_read_ascii_converts_malformed_payload_bytes_to_a_typed_parser_error() -> None:
    """Reject leaking UnicodeDecodeError beyond the replay parser boundary."""
    reader = BinaryReader(b"\x01\x00\x00\x00\xff")

    with pytest.raises(InvalidStringEncodingError) as raised:
        reader.read_ascii()

    assert raised.value.code == "invalid_string_encoding"
    assert raised.value.offset == 4
    assert reader.offset == 5


@pytest.mark.parametrize(
    ("replay_bytes", "expected_offset"),
    [
        (b"\x01\x00\x00\x00\x00", 5),
        (b"\x02\x00\x00\x00\x00\xd8", 6),
    ],
)
def test_read_utf16le_converts_odd_or_invalid_payloads_to_a_typed_parser_error(
    replay_bytes: bytes, expected_offset: int
) -> None:
    """Reject exposing codec-specific UTF-16 failures instead of replay parser context."""
    reader = BinaryReader(replay_bytes)

    with pytest.raises(InvalidStringEncodingError) as raised:
        reader.read_utf16le()

    assert raised.value.code == "invalid_string_encoding"
    assert raised.value.offset == 4
    assert reader.offset == expected_offset


def test_read_ascii_rejects_a_length_larger_than_one_mebibyte_at_the_length_field() -> None:
    """Reject allocating unbounded ASCII payloads or reporting their payload offset."""
    reader = BinaryReader(b"\x01\x00\x10\x00")

    with pytest.raises(InvalidStringLengthError) as raised:
        reader.read_ascii()

    assert raised.value.code == "invalid_string_length"
    assert raised.value.offset == 0
    assert reader.offset == 4


def test_read_utf16le_caps_encoded_bytes_not_utf16_code_units() -> None:
    """Reject allowing oversized UTF-16 payloads through a code-unit based cap."""
    reader = BinaryReader(b"\x01\x00\x10\x00")

    with pytest.raises(InvalidStringLengthError) as raised:
        reader.read_utf16le()

    assert raised.value.code == "invalid_string_length"
    assert raised.value.offset == 0
    assert reader.offset == 4


def test_slice_captures_the_exact_half_open_range_without_changing_offset() -> None:
    """Reject including adjacent replay bytes when retaining raw parser evidence."""
    reader = BinaryReader(b"\x10\x20\x30\x40")
    reader.read_u8()

    assert reader.slice(1, 3) == b"\x20\x30"
    assert reader.offset == 1


def test_slice_rejects_a_range_that_cannot_capture_every_requested_byte() -> None:
    """Reject silently shortening raw evidence when its end offset exceeds replay data."""
    reader = BinaryReader(b"\x10")

    with pytest.raises(InvalidSliceRangeError) as raised:
        reader.slice(0, 2)

    assert raised.value.code == "invalid_slice_range"
    assert raised.value.offset == 0
    assert reader.offset == 0


@pytest.mark.parametrize(
    ("start", "end", "expected_offset"),
    [
        (-1, 0, 0),
        (2, 1, 2),
        (0, 2, 0),
    ],
)
def test_slice_rejects_negative_reversed_and_past_end_bounds(
    start: int, end: int, expected_offset: int
) -> None:
    """Reject Python slice normalization that would alter requested replay evidence bounds."""
    reader = BinaryReader(b"\x10")

    with pytest.raises(InvalidSliceRangeError) as raised:
        reader.slice(start, end)

    assert raised.value.code == "invalid_slice_range"
    assert raised.value.offset == expected_offset
    assert reader.offset == 0


def test_slice_permits_exact_empty_and_full_replay_boundaries() -> None:
    """Keep valid half-open evidence ranges available at both ends of the replay bytes."""
    reader = BinaryReader(b"\x10")

    assert reader.slice(0, 1) == b"\x10"
    assert reader.slice(1, 1) == b""


@pytest.mark.parametrize("available_bytes", range(16))
def test_read_exact_rejects_eof_at_every_byte_boundary(available_bytes: int) -> None:
    """Reject accepting every possible partial prefix of a 16-byte replay field."""
    reader = BinaryReader(b"\x00" * available_bytes)

    with pytest.raises(TruncatedReplayError) as raised:
        reader.read_exact(16)

    assert raised.value.code == "truncated_replay"
    assert raised.value.offset == 0
    assert reader.offset == 0


def test_truncated_string_payload_reports_the_payload_field_offset() -> None:
    """Reject reporting a string's four-byte length field after its payload starts."""
    reader = BinaryReader(b"\x03\x00\x00\x00US")

    with pytest.raises(TruncatedReplayError) as raised:
        reader.read_ascii()

    assert raised.value.offset == 4
    assert reader.offset == 4


def test_reader_uses_remaining_bytes_from_a_seekable_binary_stream() -> None:
    """Keep reader offsets relative to its current stream-position input window."""
    source = BytesIO(b"X\x34\x12")
    source.seek(1)
    reader = BinaryReader(source)

    assert reader.read_u16() == 0x1234
    assert reader.offset == 2
    assert source.tell() == 1
