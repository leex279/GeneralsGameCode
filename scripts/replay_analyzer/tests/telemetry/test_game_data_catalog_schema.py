"""Strict contract tests for semantic game-data catalog weapon-set fidelity."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]


def _weapon_slot(ordinal: int, slot: str, weapon_name: str | None) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "slot": slot,
        "weapon_name": weapon_name,
        "auto_choose_mask": 5,
        "auto_choose_sources": ["FROM_PLAYER", "FROM_AI"],
        "preferred_against_kind_of": ["VEHICLE"],
    }


def test_catalog_schema_preserves_weapon_set_conditions_slots_and_choice_metadata() -> None:
    """Catch flattening distinct weapon-set behavior into one template-level name union."""
    schema_path = Path(__file__).parents[2] / "contracts" / "game-data-catalog-v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    catalog = {
        "schema_version": 1,
        "type": "game_data_catalog",
        "engine_data_identity": "test-engine",
        "weapon_scope": "referenced_by_thing_templates",
        "locomotor_scope": "referenced_by_thing_templates",
        "thing_templates": [
            {
                "ordinal": 0,
                "name": "TestTank",
                "faction": "FactionTest",
                "kind_of_flags": [],
                "behavior_modules": [],
                "build_cost": 100,
                "configured_build_time_seconds": 1.0,
                "prerequisites": [],
                "locomotor_sets": [],
                "production_capable": False,
                "weapon_sets": [
                    {
                        "ordinal": 0,
                        "condition_mask": 0,
                        "condition_names": [],
                        "shared_reload_time": False,
                        "weapon_lock_shared_across_sets": False,
                        "slots": [
                            _weapon_slot(0, "PRIMARY", "MainGun"),
                            _weapon_slot(1, "SECONDARY", None),
                            _weapon_slot(2, "TERTIARY", None),
                        ],
                    },
                    {
                        "ordinal": 1,
                        "condition_mask": 1,
                        "condition_names": ["VETERAN"],
                        "shared_reload_time": True,
                        "weapon_lock_shared_across_sets": True,
                        "slots": [
                            _weapon_slot(0, "PRIMARY", "VeteranGun"),
                            _weapon_slot(1, "SECONDARY", "Missile"),
                            _weapon_slot(2, "TERTIARY", None),
                        ],
                    },
                ],
                "derived_weapon_names": ["MainGun", "Missile", "VeteranGun"],
                "category_tags": ["WEAPON_CAPABLE"],
            }
        ],
        "upgrades": [],
        "sciences": [],
        "weapons": [
            {"ordinal": 0, "name": "MainGun"},
            {"ordinal": 1, "name": "Missile"},
            {"ordinal": 2, "name": "VeteranGun"},
        ],
        "locomotors": [],
    }

    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(catalog), key=lambda error: list(error.absolute_path))
    assert errors == []
