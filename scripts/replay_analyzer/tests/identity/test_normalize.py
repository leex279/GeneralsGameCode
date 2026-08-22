"""Strict normalization tests for embedded replay player names."""

import unicodedata

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from generals_replay_analyzer.identity.normalize import InvalidPlayerNameError, normalize_embedded_name

VALID_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    min_size=1,
    max_size=80,
).filter(lambda value: bool(unicodedata.normalize("NFC", value).strip().casefold()))


@settings(max_examples=200, derandomize=True)
@given(VALID_TEXT)
def test_normalization_is_idempotent_and_nfc(value: str) -> None:
    """Catch a noncanonical or second-pass-changing normalization result."""
    normalized = normalize_embedded_name(value)
    assert normalize_embedded_name(normalized) == normalized
    assert unicodedata.is_normalized("NFC", normalized)


@settings(max_examples=200, derandomize=True)
@given(VALID_TEXT)
def test_canonical_equivalents_and_casefold_equivalents_match(value: str) -> None:
    """Catch failure to identify Unicode-equivalent embedded spellings exactly."""
    expected = normalize_embedded_name(value)
    assert normalize_embedded_name(unicodedata.normalize("NFD", value)) == expected
    assert normalize_embedded_name(value.swapcase()) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  LeEx279  ", "leex279"),
        ("Straße", "strasse"),
        ("İ", "i\u0307"),
        ("aα", "aα"),
        ("A-B!", "a-b!"),
        ("A\u00a0 B", "a\u00a0 b"),
        ("A\u200bB", "a\u200bb"),
        ("e\u0301", "é"),
    ],
)
def test_normalization_uses_only_nfc_strip_and_casefold(value: str, expected: str) -> None:
    """Catch transliteration, punctuation removal, or interior-space collapse."""
    assert normalize_embedded_name(value) == expected


@pytest.mark.parametrize("value", [None, 4, "", "   ", "\x00name", "bad\ud800name", "x" * 256])
def test_invalid_names_raise_the_typed_error(value: object) -> None:
    """Catch invalid names crossing the identity boundary as usable aliases."""
    with pytest.raises(InvalidPlayerNameError):
        normalize_embedded_name(value)  # type: ignore[arg-type]


def test_confusable_and_distinct_normalized_names_remain_distinct() -> None:
    """Catch accidental fuzzy or homoglyph normalization."""
    assert normalize_embedded_name("alpha") != normalize_embedded_name("αlpha")
    assert normalize_embedded_name("FOX27") != normalize_embedded_name("FOX-27")
