"""Versioned authoritative Zero Hour combat type identities."""

import json
from importlib import resources
from pathlib import Path
from typing import cast


def _load_contract() -> dict[str, object]:
    filename = "zero-hour-combat-types-v1.json"
    source = Path(__file__).resolve().parents[3] / "contracts" / filename
    if source.is_file():
        decoded = json.loads(source.read_text(encoding="utf-8"))
    else:
        packaged = resources.files("generals_replay_analyzer").joinpath("data", filename)
        decoded = json.loads(packaged.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise TypeError("Zero Hour combat type contract must be an object")
    return cast(dict[str, object], decoded)


def _closed_mapping(contract: dict[str, object], key: str) -> dict[int, str]:
    values = contract.get(key)
    if not isinstance(values, list):
        raise TypeError(f"Zero Hour combat type contract {key} must be an array")
    result: dict[int, str] = {}
    for expected_id, item in enumerate(values):
        if not isinstance(item, dict) or set(item) != {"id", "name"}:
            raise RuntimeError(f"Zero Hour combat type contract {key} contains an invalid entry")
        type_id = item.get("id")
        name = item.get("name")
        if type(type_id) is not int or type_id != expected_id or not isinstance(name, str) or not name:
            raise RuntimeError(f"Zero Hour combat type contract {key} must use contiguous IDs and names")
        result[type_id] = name
    return result


_CONTRACT = _load_contract()
if set(_CONTRACT) != {"schema_version", "game", "damage_types", "death_types"}:
    raise RuntimeError("Zero Hour combat type contract has unexpected fields")
if _CONTRACT["schema_version"] != 1 or _CONTRACT["game"] != "zero_hour":
    raise RuntimeError("Zero Hour combat type contract identity is unsupported")

DAMAGE_TYPE_NAMES = _closed_mapping(_CONTRACT, "damage_types")
DEATH_TYPE_NAMES = _closed_mapping(_CONTRACT, "death_types")


def require_combat_type_pair(type_kind: str, type_id: int, name: str) -> None:
    """Fail closed when an emitted numeric/name pair differs from the pinned engine contract."""
    mapping = DAMAGE_TYPE_NAMES if type_kind == "damage" else DEATH_TYPE_NAMES
    expected = mapping.get(type_id)
    if expected is None:
        raise ValueError(f"unknown Zero Hour {type_kind} type id {type_id}")
    if name != expected:
        raise ValueError(f"{type_kind} type id {type_id} must be named {expected}")
