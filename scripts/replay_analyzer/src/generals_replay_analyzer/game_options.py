"""Strict parser for the GameInfo ASCII grammar written into replay headers."""

from dataclasses import dataclass
from typing import NoReturn

from .errors import InvalidGameOptionsError
from .model import ParseWarning, ReplaySlot

MAX_SLOTS = 8
REQUIRED_TOKENS = frozenset({"M", "MC", "MS", "SD", "C", "S"})
KNOWN_OPTIONAL_TOKENS = frozenset({"US", "SR", "SC", "O"})


@dataclass(frozen=True)
class ParsedGameOptions:
    """Known GameInfo fields plus warnings for source-compatible extensions."""

    map: str
    map_contents_mask: int
    map_crc: int
    map_size: int
    seed: int
    crc_interval: int
    use_stats: int | None
    superweapon_restriction: int | None
    starting_cash: int | None
    old_factions_only: bool | None
    slots: tuple[ReplaySlot, ...]
    warnings: tuple[ParseWarning, ...]


# TheSuperHackers @feature Leex 19/08/2026 Mirror GameInfo.cpp H/C/O/X slots without filename-derived identities.
def parse_game_options(options: str, offset: int) -> ParsedGameOptions:
    """Parse the GameInfo serialization that RecorderClass validates before replay playback."""
    values: dict[str, str] = {}
    warnings: list[ParseWarning] = []
    for part in options.split(";"):
        if not part:
            continue
        if "=" not in part:
            _invalid(offset, "game-options token is missing '='")
        key, value = part.split("=", 1)
        if not key or not value:
            _invalid(offset, "game-options token has an empty key or value")
        if key in values:
            _invalid(offset, f"duplicate game-options token '{key}'")
        if key not in REQUIRED_TOKENS and key not in KNOWN_OPTIONAL_TOKENS:
            warnings.append(
                ParseWarning(
                    code="unknown_optional_token",
                    message=f"ignored game-options token '{key}'",
                    token=key,
                )
            )
            continue
        values[key] = value

    missing = REQUIRED_TOKENS.difference(values)
    if missing:
        _invalid(offset, f"missing required game-options token '{min(missing)}'")

    map_value = values["M"]
    if len(map_value) < 3:
        _invalid(offset, "map token must contain two hex mask digits and a map path")
    map_contents_mask = _integer(map_value[:2], 16, offset, "map contents mask")
    map_name = map_value[2:]
    if not map_name:
        _invalid(offset, "map token has an empty map path")

    return ParsedGameOptions(
        map=map_name,
        map_contents_mask=map_contents_mask,
        map_crc=_integer(values["MC"], 16, offset, "map CRC"),
        map_size=_integer(values["MS"], 10, offset, "map size"),
        seed=_integer(values["SD"], 10, offset, "seed"),
        crc_interval=_integer(values["C"], 10, offset, "CRC interval"),
        use_stats=_optional_integer(values, "US", 10, offset),
        superweapon_restriction=_optional_integer(values, "SR", 10, offset),
        starting_cash=_optional_integer(values, "SC", 10, offset),
        old_factions_only=_optional_old_factions(values, offset),
        slots=_parse_slots(values["S"], offset),
        warnings=tuple(warnings),
    )


def _optional_integer(values: dict[str, str], key: str, base: int, offset: int) -> int | None:
    """Return a parsed optional integer only when its token is present."""
    return _integer(values[key], base, offset, key) if key in values else None


def _optional_old_factions(values: dict[str, str], offset: int) -> bool | None:
    """Parse the Zero Hour old-factions flag without silently accepting other values."""
    if "O" not in values:
        return None
    if values["O"].upper() == "Y":
        return True
    if values["O"].upper() == "N":
        return False
    _invalid(offset, "old-factions token must be Y or N")


def _parse_slots(serialized_slots: str, offset: int) -> tuple[ReplaySlot, ...]:
    """Parse all eight slot records explicitly, retaining their source index."""
    raw_slots = serialized_slots.split(":")
    if raw_slots and raw_slots[-1] == "":
        raw_slots.pop()
    if len(raw_slots) != MAX_SLOTS:
        _invalid(offset, f"expected {MAX_SLOTS} slot records, found {len(raw_slots)}")
    return tuple(_parse_slot(index, raw_slot, offset) for index, raw_slot in enumerate(raw_slots))


def _parse_slot(index: int, raw_slot: str, offset: int) -> ReplaySlot:
    """Parse one H/C/O/X GameInfo slot record according to GameInfo.cpp."""
    if raw_slot == "O":
        return ReplaySlot(index=index, kind="open")
    if raw_slot == "X":
        return ReplaySlot(index=index, kind="closed")
    if raw_slot.startswith("H"):
        return _parse_human_slot(index, raw_slot, offset)
    if raw_slot.startswith("C"):
        return _parse_ai_slot(index, raw_slot, offset)
    _invalid(offset, f"unknown slot type '{raw_slot[:1]}'")


def _parse_human_slot(index: int, raw_slot: str, offset: int) -> ReplaySlot:
    """Parse the engine's H-name,IP,port,flags,color,faction,start,team,NAT grammar."""
    fields = raw_slot[1:].split(",")
    if len(fields) != 9 or any(not field for field in fields):
        _invalid(offset, "human slot must contain name plus eight comma-separated fields")
    accepted_map = fields[3]
    if len(accepted_map) != 2 or accepted_map[0] not in "TF" or accepted_map[1] not in "TF":
        _invalid(offset, "human slot acceptance/map flags must be TT, TF, FT, or FF")
    return ReplaySlot(
        index=index,
        kind="human",
        name=fields[0],
        ip=_integer(fields[1], 16, offset, "human IP"),
        port=_integer(fields[2], 10, offset, "human port"),
        accepted=accepted_map[0] == "T",
        has_map=accepted_map[1] == "T",
        color=_integer(fields[4], 10, offset, "human color"),
        player_template=_integer(fields[5], 10, offset, "human template"),
        start_position=_integer(fields[6], 10, offset, "human start position"),
        team=_integer(fields[7], 10, offset, "human team"),
        nat_behavior=_integer(fields[8], 10, offset, "human NAT behavior"),
    )


def _parse_ai_slot(index: int, raw_slot: str, offset: int) -> ReplaySlot:
    """Parse the engine's C-difficulty,color,faction,start,team grammar."""
    fields = raw_slot[1:].split(",")
    if len(fields) != 5 or not fields[0] or fields[0] not in {"E", "M", "H"}:
        _invalid(offset, "AI slot must begin with E, M, or H and contain four numeric fields")
    if any(not field for field in fields[1:]):
        _invalid(offset, "AI slot contains an empty numeric field")
    difficulty = {"E": "easy", "M": "medium", "H": "brutal"}[fields[0]]
    return ReplaySlot(
        index=index,
        kind="ai",
        color=_integer(fields[1], 10, offset, "AI color"),
        player_template=_integer(fields[2], 10, offset, "AI template"),
        start_position=_integer(fields[3], 10, offset, "AI start position"),
        team=_integer(fields[4], 10, offset, "AI team"),
        ai_difficulty=difficulty,
    )


def _integer(value: str, base: int, offset: int, field: str) -> int:
    """Decode an entire integer token, refusing atoi-style partial values."""
    try:
        return int(value, base)
    except ValueError:
        _invalid(offset, f"invalid {field} value '{value}'")


def _invalid(offset: int, message: str) -> NoReturn:
    """Raise the stable GameInfo grammar failure at the options field origin."""
    raise InvalidGameOptionsError("invalid_game_options", offset, message)
