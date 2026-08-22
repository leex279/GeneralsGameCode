"""Deterministic normalization for embedded replay player names."""

from __future__ import annotations

import unicodedata

EMBEDDED_REPLAY_NAME_NAMESPACE = "embedded_replay_name"
STRATA_FILENAME_NAMESPACE = "strata_filename"
EXTERNAL_NAMESPACE_PREFIX = "external:"


class InvalidPlayerNameError(ValueError):
    """Raised when an embedded replay name cannot be a safe exact alias."""


# TheSuperHackers @feature Leex 22/08/2026 Normalize embedded names without fuzzy or provenance-derived identity. (#TBD)
def normalize_embedded_name(value: str) -> str:
    """Return the sole exact-match form used for embedded replay names."""
    if not isinstance(value, str):
        raise InvalidPlayerNameError("embedded player name must be a string")
    normalized = unicodedata.normalize("NFC", value).strip().casefold()
    normalized = unicodedata.normalize("NFC", normalized)
    if not normalized:
        raise InvalidPlayerNameError("embedded player name must not be empty")
    if "\x00" in normalized:
        raise InvalidPlayerNameError("embedded player name must not contain NUL")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in normalized):
        raise InvalidPlayerNameError("embedded player name must not contain surrogate code points")
    if len(normalized) > 255:
        raise InvalidPlayerNameError("embedded player name must be at most 255 code points")
    return normalized
