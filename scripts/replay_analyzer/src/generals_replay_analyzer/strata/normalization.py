"""Exact replay-name preservation and comparison normalization."""

from __future__ import annotations

import unicodedata

from .contracts import QueryName


class InvalidQueryNameError(ValueError):
    """Raised when a replay name cannot be safely represented by the resolver."""

    code = "invalid_query_name"


# TheSuperHackers @feature Leex 27/08/2026 Preserve replay aliases exactly while deriving separate comparison keys.
def normalize_query_name(value: str) -> QueryName:
    """Remove recorder NUL padding and derive Unicode comparison forms."""
    if not isinstance(value, str):
        raise InvalidQueryNameError("query name must be a string")
    raw = value.rstrip("\x00")
    if not raw:
        raise InvalidQueryNameError("query name is empty after terminal NUL padding")
    if "\x00" in raw:
        raise InvalidQueryNameError("query name contains an embedded NUL")
    if len(raw) > 255:
        raise InvalidQueryNameError("query name exceeds 255 Unicode code points")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in raw):
        raise InvalidQueryNameError("query name contains an unpaired surrogate")
    nfc = unicodedata.normalize("NFC", raw)
    casefold = unicodedata.normalize("NFC", nfc.casefold())
    return QueryName(raw=raw, nfc=nfc, casefold=casefold)
