"""Validated contracts for generated Zero Hour replay metadata artifacts."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from types import MappingProxyType
from typing import cast

_SCHEMA_VERSION = 1
_CATALOG_KEYS = frozenset(
    {
        "schema_version",
        "game",
        "patch",
        "engine_build",
        "source_header_path",
        "generated_at_utc",
        "generated",
        "generation_note",
        "message_types",
    }
)
_METADATA_STRING_KEYS = (
    "game",
    "patch",
    "engine_build",
    "source_header_path",
    "generated_at_utc",
    "generation_note",
)
_MESSAGE_NAME = re.compile(r"MSG_[A-Z0-9_]+$")


class MessageCatalogValidationError(ValueError):
    """Raised when a generated message catalog cannot be trusted as a schema artifact."""


@dataclass(frozen=True)
class MessageCatalog:
    """Validated generated metadata plus the immutable numeric-to-symbolic lookup."""

    schema_version: int
    game: str
    patch: str
    engine_build: str
    source_header_path: str
    generated_at_utc: str
    generated: bool
    generation_note: str
    names_by_id: Mapping[int, str]


def _source_checkout_catalog_path() -> Path:
    """Return the canonical artifact only as an editable-source fallback when wheel data is absent."""
    return Path(__file__).resolve().parents[2] / "contracts" / "zero_hour_1_04_message_types.json"


# TheSuperHackers @feature Leex 19/08/2026 Validate provisional generated replay message names before symbolic decoding. (#TBD)
def load_message_catalog(path: Path | Traversable) -> MessageCatalog:
    """Load a schema-complete catalog while retaining duplicate detection from the entry list."""
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MessageCatalogValidationError(f"cannot read message catalog '{path}': {error}") from error
    if not isinstance(decoded, dict) or set(decoded) != _CATALOG_KEYS:
        raise MessageCatalogValidationError("catalog root must contain exactly the required schema keys")
    if decoded["schema_version"] != _SCHEMA_VERSION or type(decoded["schema_version"]) is not int:
        raise MessageCatalogValidationError(f"unsupported schema_version: {decoded['schema_version']!r}")
    for key in _METADATA_STRING_KEYS:
        if not isinstance(decoded[key], str) or not decoded[key]:
            raise MessageCatalogValidationError(f"catalog {key} must be a non-empty string")
    generated_at_utc = cast(str, decoded["generated_at_utc"])
    if not generated_at_utc.endswith("Z"):
        raise MessageCatalogValidationError("catalog generated_at_utc must use UTC Z notation")
    try:
        datetime.fromisoformat(generated_at_utc.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise MessageCatalogValidationError("catalog generated_at_utc must be an ISO-8601 timestamp") from error
    if decoded["generated"] is not True:
        raise MessageCatalogValidationError("catalog generated must be true for this generated artifact")
    entries = decoded["message_types"]
    if not isinstance(entries, list) or not entries:
        raise MessageCatalogValidationError("catalog message_types must be a non-empty list")

    names_by_id: dict[int, str] = {}
    ids_by_name: dict[str, int] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"id", "name"}:
            raise MessageCatalogValidationError(f"catalog message_types[{index}] must contain id and name")
        message_id = entry["id"]
        name = entry["name"]
        if type(message_id) is not int:
            raise MessageCatalogValidationError(f"catalog message_types[{index}].id must be an integer")
        if not isinstance(name, str) or not _MESSAGE_NAME.fullmatch(name):
            raise MessageCatalogValidationError(f"catalog message_types[{index}].name must be an MSG_* identifier")
        if message_id in names_by_id:
            raise MessageCatalogValidationError(f"duplicate message id {message_id}")
        if name in ids_by_name:
            raise MessageCatalogValidationError(f"duplicate message name {name}")
        names_by_id[message_id] = name
        ids_by_name[name] = message_id

    return MessageCatalog(
        schema_version=_SCHEMA_VERSION,
        game=cast(str, decoded["game"]),
        patch=cast(str, decoded["patch"]),
        engine_build=cast(str, decoded["engine_build"]),
        source_header_path=cast(str, decoded["source_header_path"]),
        generated_at_utc=generated_at_utc,
        generated=True,
        generation_note=cast(str, decoded["generation_note"]),
        names_by_id=MappingProxyType(names_by_id),
    )


@cache
def default_message_catalog() -> MessageCatalog:
    """Return wheel-packaged catalog data, falling back only for editable source checkouts."""
    packaged_catalog = resources.files("generals_replay_analyzer").joinpath(
        "data", "zero_hour_1_04_message_types.json"
    )
    if packaged_catalog.is_file():
        return load_message_catalog(packaged_catalog)
    source_checkout_catalog = _source_checkout_catalog_path()
    if source_checkout_catalog.is_file():
        return load_message_catalog(source_checkout_catalog)
    raise MessageCatalogValidationError("generated Zero Hour 1.04 message catalog is missing from package data")


def message_name_for(message_type: int) -> str | None:
    """Return a known symbolic name without rejecting an otherwise usable numeric command."""
    return default_message_catalog().names_by_id.get(message_type)
