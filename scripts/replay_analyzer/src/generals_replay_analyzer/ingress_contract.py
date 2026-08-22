"""Closed cross-platform identity contract for watched replay ingress."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from uuid import RFC_4122, UUID

_MAX_RELATIVE_NAME_LENGTH = 1024
_WINDOWS_INVALID_COMPONENT_CHARACTERS = frozenset('<>:"\\|?*')
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
    | {f"com{number}" for number in ("\u00b9", "\u00b2", "\u00b3")}
    | {f"lpt{number}" for number in ("\u00b9", "\u00b2", "\u00b3")}
)


class IngressIdentityErrorCode(StrEnum):
    """Stable path-free validation reason at every watched-ingress bind."""

    ROOT_PUBLIC_ID_INVALID = "root_public_id_invalid"
    REPLAY_RELATIVE_NAME_INVALID = "replay_relative_name_invalid"


class IngressIdentityError(ValueError):
    """Typed contract failure that never includes a caller locator."""

    def __init__(self, code: IngressIdentityErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


def validate_root_public_id(value: str) -> str:
    """Require one canonical lowercase RFC-4122 UUID version 4."""
    if not isinstance(value, str):
        raise IngressIdentityError(IngressIdentityErrorCode.ROOT_PUBLIC_ID_INVALID)
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError):
        raise IngressIdentityError(IngressIdentityErrorCode.ROOT_PUBLIC_ID_INVALID) from None
    if str(parsed) != value or parsed.version != 4 or parsed.variant != RFC_4122:
        raise IngressIdentityError(IngressIdentityErrorCode.ROOT_PUBLIC_ID_INVALID)
    return value


def validate_replay_relative_name(value: str) -> tuple[str, ...]:
    """Return exact POSIX components for one unambiguous NFC lowercase-.rep name."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_RELATIVE_NAME_LENGTH
        or value.startswith(("/", "\\"))
        or value.endswith(("/", "\\"))
        or "\\" in value
        or ":" in value
        or "%" in value
        or unicodedata.normalize("NFC", value) != value
        or any(character in _WINDOWS_INVALID_COMPONENT_CHARACTERS for character in value)
        or any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)
    ):
        raise IngressIdentityError(IngressIdentityErrorCode.REPLAY_RELATIVE_NAME_INVALID)
    components = value.split("/")
    if any(component in {"", ".", ".."} or component.endswith((".", " ")) for component in components):
        raise IngressIdentityError(IngressIdentityErrorCode.REPLAY_RELATIVE_NAME_INVALID)
    if any(
        component.split(".", 1)[0].rstrip(" .").casefold() in _WINDOWS_RESERVED_BASENAMES
        for component in components
    ):
        raise IngressIdentityError(IngressIdentityErrorCode.REPLAY_RELATIVE_NAME_INVALID)
    filename = components[-1]
    stem = filename[: -len(".rep")] if filename.endswith(".rep") else ""
    if not stem or stem.endswith(".rep"):
        raise IngressIdentityError(IngressIdentityErrorCode.REPLAY_RELATIVE_NAME_INVALID)
    return tuple(components)


# TheSuperHackers @feature Leex 22/08/2026 Bind every watched replay handoff to one closed public identity. (#TBD)
@dataclass(frozen=True, slots=True)
class ReplayIngressIdentity:
    """Validated opaque root plus exact safe relative replay name."""

    root_public_id: str
    relative_name: str

    def __post_init__(self) -> None:
        validate_root_public_id(self.root_public_id)
        validate_replay_relative_name(self.relative_name)
