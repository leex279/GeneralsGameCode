"""Strict v2 combat, player-terminal-state, and match-outcome contracts."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from generals_replay_analyzer.telemetry.model import DamageAppliedRecord, MatchOutcomeRecord
from generals_replay_analyzer.telemetry.reader import TelemetryTraceValidationError, iter_validated_trace

RUN_ID = "a23e4567-e89b-12d3-a456-426614174000"
ENGINE_IDENTITY = "zero-hour-combat-test-exe-00000000-ini-00000000"


def _record(sequence: int, event_type: str, payload: dict[str, object], frame: int = 0) -> dict[str, object]:
    return {
        "schema_version": 2,
        "run_id": RUN_ID,
        "sequence": sequence,
        "frame": frame,
        "logic_time_seconds": frame / 30.0,
        "event_type": event_type,
        "payload": payload,
    }


def _write_catalog(directory: Path) -> dict[str, object]:
    catalog = {
        "schema_version": 1,
        "type": "game_data_catalog",
        "engine_data_identity": ENGINE_IDENTITY,
        "weapon_scope": "referenced_by_thing_templates",
        "locomotor_scope": "referenced_by_thing_templates",
        "thing_templates": [],
        "upgrades": [],
        "sciences": [],
        "weapons": [],
        "locomotors": [],
    }
    content = json.dumps(catalog, separators=(",", ":")).encode() + b"\n"
    digest = hashlib.sha256(content).hexdigest()
    name = f"game-data-catalog-v1-{digest}.json"
    (directory / name).write_bytes(content)
    return {
        "type": "game_data_catalog",
        "path": name,
        "sha256": digest,
        "engine_data_identity": ENGINE_IDENTITY,
    }


def _players(reference: dict[str, object]) -> dict[str, object]:
    slots: list[dict[str, object]] = []
    for slot_index, player_index in enumerate((0, 1)):
        slots.append(
            {
                "slot_index": slot_index,
                "slot_state": "human",
                "occupied": True,
                "resolution_status": "resolved",
                "replay_name": f"Player{player_index}",
                "player_index": player_index,
                "team_id": player_index,
                "faction_template_name": "FactionAmerica",
                "color": player_index,
                "start_position_status": "resolved",
                "start_position": {"x": float(player_index), "y": 0.0, "z": 0.0},
                "controller": "human",
                "is_human": True,
                "is_header_local_slot": slot_index == 0,
                "is_resolved_local_player": slot_index == 0,
            }
        )
    for slot_index in range(2, 8):
        slots.append(
            {
                "slot_index": slot_index,
                "slot_state": "open",
                "occupied": False,
                "resolution_status": "not_applicable",
                "replay_name": None,
                "player_index": None,
                "team_id": None,
                "faction_template_name": None,
                "color": None,
                "start_position_status": "not_applicable",
                "start_position": None,
                "controller": None,
                "is_human": False,
                "is_header_local_slot": False,
                "is_resolved_local_player": None,
            }
        )
    return {
        "header_local_slot_index": 0,
        "slots": slots,
        "engine_player_indices": [0, 1, 9],
        "game_data_catalog": reference,
    }


def _created(object_id: int, owner: int, template: str) -> dict[str, object]:
    return {
        "object_id": object_id,
        "template_name": template,
        "owner_player_index": owner,
        "team_id": owner,
        "position_status": "placed",
        "position": {"x": float(object_id), "y": 5.0, "z": 0.0},
        "orientation": 0.0,
        "kind_of_flags": [],
        "initial_status": [],
        "creation_source": "starting_object",
        "creation_context": {
            "registration_frame": 0,
            "producer_object_id": None,
            "producer_player_index": None,
        },
    }


def _damage(*, prior: float = 100.0, new: float = 60.0, killing: bool = False) -> dict[str, object]:
    applied = prior - new
    return {
        "victim_object_id": 20,
        "victim_player_index": 1,
        "attacker_object_id": 10,
        "attacker_player_index": 0,
        "attacker_template_name": "AmericaTankCrusader",
        "weapon_name": None,
        "attempted_amount": 50.0,
        "calculated_amount": applied,
        "applied_amount": applied,
        "prior_health": prior,
        "new_health": new,
        "damage_type_id": 2,
        "damage_type": "ARMOR_PIERCING",
        "death_type_id": 0,
        "death_type": "NORMAL",
        "location": {"x": 20.0, "y": 5.0, "z": 0.0},
        "killing_blow": killing,
    }


def _outcome(*, status: str = "unknown", winners: list[int] | None = None, losers: list[int] | None = None) -> dict[str, object]:
    return {
        "status": status,
        "source": "victory_conditions" if status == "decided" else "unavailable",
        "winner_player_indices": winners or [],
        "loser_player_indices": losers or [],
        "engine_player_indices": [0, 1, 9],
        "terminal_reason": "crc_mismatch",
        "quit_early": False,
        "replay_header_desync": False,
        "replay_header_disconnected_slots": [],
        "crc_mismatch": True,
        "crc_mismatch_frame": 108,
        "clean_shutdown": False,
    }


def _base(directory: Path) -> list[dict[str, object]]:
    reference = _write_catalog(directory)
    return [
        _record(
            0,
            "manifest",
            {
                "engine_build": ENGINE_IDENTITY,
                "replay_version": "1.04",
                "map_identity": "maps/test.map",
                "initial_seed": 7,
                "exporter_settings": {"movement_sample_frames": 15, "audio_enabled": False},
                "game_data_catalog": reference,
            },
        ),
        _record(1, "players_initialized", _players(reference)),
        _record(2, "object_created", _created(10, 0, "AmericaTankCrusader")),
        _record(3, "object_created", _created(20, 1, "ChinaTankBattleMaster")),
    ]


def _finish(path: Path, records: list[dict[str, object]], *, outcome: dict[str, object] | None = None) -> Path:
    if outcome is not None:
        records.append(_record(len(records), "match_outcome", outcome, frame=108))
    prior = b"".join(json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records)
    counts: dict[str, int] = {}
    for record in records:
        event_type = str(record["event_type"])
        counts[event_type] = counts.get(event_type, 0) + 1
    counts["complete"] = 1
    records.append(
        _record(
            len(records),
            "complete",
            {
                "final_frame": 108,
                "command_count": 16,
                "event_counts": counts,
                "crc_mismatch": True,
                "crc_mismatch_frame": 108,
                "replay_truncated": False,
                "quit_early": False,
                "replay_header_desync": False,
                "replay_header_disconnected_slots": [],
                "clean_shutdown": False,
                "writer_error": None,
                "trace_sha256": hashlib.sha256(prior).hexdigest(),
                "map_assets": [],
                "final_cash_balances": [
                    {"player_index": index, "has_money": False, "balance": None}
                    for index in (0, 1, 9)
                ],
            },
            frame=108,
        )
    )
    path.write_bytes(b"".join(json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records))
    return path


def _valid_trace(directory: Path) -> Path:
    records = _base(directory)
    records.extend(
        [
            _record(4, "damage_applied", _damage(), frame=10),
            _record(
                5,
                "healing_applied",
                {
                    "target_object_id": 20,
                    "target_player_index": 1,
                    "source_object_id": 10,
                    "source_player_index": 0,
                    "attempted_amount": 10.0,
                    "calculated_amount": 10.0,
                    "applied_amount": 10.0,
                    "prior_health": 60.0,
                    "new_health": 70.0,
                    "location": {"x": 20.0, "y": 5.0, "z": 0.0},
                },
                frame=11,
            ),
            _record(
                6,
                "veterancy_changed",
                {
                    "object_id": 10,
                    "owner_player_index": 0,
                    "previous_level_id": 0,
                    "previous_level": "REGULAR",
                    "new_level_id": 1,
                    "new_level": "VETERAN",
                },
                frame=12,
            ),
            _record(
                7,
                "player_defeated",
                {
                    "player_index": 1,
                    "previous_status": "active",
                    "new_status": "defeated",
                    "source": "victory_conditions",
                    "replay_slot_index": 1,
                },
                frame=90,
            ),
        ]
    )
    return _finish(directory / "combat.ndjson", records, outcome=_outcome(status="decided", winners=[0], losers=[1]))


def test_reader_accepts_authoritative_combat_and_terminal_outcome_contract(tmp_path: Path) -> None:
    records = tuple(iter_validated_trace(_valid_trace(tmp_path)))

    assert records[-2].event_type == "match_outcome"
    assert records[-2].payload.winner_player_indices == [0]
    assert records[-1].payload.crc_mismatch_frame == 108
    assert records[4].payload.applied_amount == 40.0


def test_task6_pydantic_payloads_are_closed_for_v2_without_tightening_v1() -> None:
    damage = _record(4, "damage_applied", _damage(), frame=10)
    damage_payload = damage["payload"]
    assert isinstance(damage_payload, dict)
    damage_payload["unexpected"] = "rejected-in-v2"
    location = damage_payload["location"]
    assert isinstance(location, dict)
    location["unexpected"] = "rejected-in-v2"

    with pytest.raises(ValueError, match="unexpected"):
        DamageAppliedRecord.model_validate(damage)

    damage["schema_version"] = 1
    assert DamageAppliedRecord.model_validate(damage).payload.model_extra == {"unexpected": "rejected-in-v2"}

    outcome = _record(4, "match_outcome", _outcome(), frame=108)
    outcome_payload = outcome["payload"]
    assert isinstance(outcome_payload, dict)
    outcome_payload["winner_player_index"] = None
    with pytest.raises(ValueError, match="legacy"):
        MatchOutcomeRecord.model_validate(outcome)


@pytest.mark.parametrize(
    ("mutate", "diagnostic"),
    [
        (lambda damage: damage.update({"applied_amount": 39.0}), "health arithmetic"),
        (lambda damage: damage.update({"new_health": 101.0}), "health arithmetic"),
        (lambda damage: damage.update({"killing_blow": True}), "killing_blow"),
        (lambda damage: damage.update({"victim_player_index": 0}), "victim_player_index"),
        (lambda damage: damage.update({"attacker_player_index": 1}), "attacker_player_index"),
    ],
)
def test_reader_rejects_impossible_damage_or_owner_attribution(
    tmp_path: Path,
    mutate: Callable[[dict[str, object]], object],
    diagnostic: str,
) -> None:
    records = _base(tmp_path)
    damage = _damage()
    mutate(damage)
    records.append(_record(4, "damage_applied", damage, frame=10))
    trace = _finish(tmp_path / f"bad-damage-{diagnostic}.ndjson", records, outcome=_outcome())

    with pytest.raises(TelemetryTraceValidationError, match=diagnostic):
        tuple(iter_validated_trace(trace))


def test_reader_requires_killing_damage_before_destruction_and_rejects_later_health_changes(tmp_path: Path) -> None:
    records = _base(tmp_path)
    records.extend(
        [
            _record(4, "damage_applied", _damage(prior=100.0, new=0.0, killing=True), frame=10),
            _record(
                5,
                "object_destroyed",
                {
                    "object_id": 20,
                    "previous_state": "alive",
                    "new_state": "destroyed",
                    "owner_player_index": 1,
                    "team_id": 1,
                    "destruction_source": "destroy_object",
                },
                frame=10,
            ),
            _record(6, "damage_applied", _damage(prior=1.0, new=0.0, killing=True), frame=11),
        ]
    )
    trace = _finish(tmp_path / "damage-after-destroy.ndjson", records, outcome=_outcome())

    with pytest.raises(TelemetryTraceValidationError, match="after object_destroyed"):
        tuple(iter_validated_trace(trace))


def test_reader_rejects_duplicate_veterancy_transition_without_intervening_level(tmp_path: Path) -> None:
    records = _base(tmp_path)
    veterancy = {
        "object_id": 10,
        "owner_player_index": 0,
        "previous_level_id": 0,
        "previous_level": "REGULAR",
        "new_level_id": 1,
        "new_level": "VETERAN",
    }
    records.extend(
        [
            _record(4, "veterancy_changed", veterancy, frame=10),
            _record(5, "veterancy_changed", veterancy, frame=11),
        ]
    )
    trace = _finish(tmp_path / "duplicate-veterancy.ndjson", records, outcome=_outcome())

    with pytest.raises(TelemetryTraceValidationError, match="veterancy continuity"):
        tuple(iter_validated_trace(trace))


@pytest.mark.parametrize("event_type", ["player_defeated", "player_surrendered", "player_disconnected"])
def test_reader_rejects_duplicate_or_conflicting_player_terminal_transitions(tmp_path: Path, event_type: str) -> None:
    status = event_type.removeprefix("player_")
    records = _base(tmp_path)
    payload = {
        "player_index": 1,
        "previous_status": "active",
        "new_status": status,
        "source": (
            "victory_conditions"
            if status == "defeated"
            else "replay_header_disconnect_plus_executed_surrender"
            if status == "disconnected"
            else "replay_command"
        ),
        "replay_slot_index": 1,
    }
    records.extend(
        [
            _record(4, event_type, payload, frame=50),
            _record(5, event_type, payload, frame=51),
        ]
    )
    trace = _finish(tmp_path / f"duplicate-{event_type}.ndjson", records, outcome=_outcome())

    with pytest.raises(TelemetryTraceValidationError, match="terminal player transition"):
        tuple(iter_validated_trace(trace))


def test_reader_requires_exactly_one_outcome_immediately_before_complete(tmp_path: Path) -> None:
    missing = _finish(tmp_path / "missing-outcome.ndjson", _base(tmp_path))
    with pytest.raises(TelemetryTraceValidationError, match="exactly one match_outcome"):
        tuple(iter_validated_trace(missing))

    records = _base(tmp_path)
    records.append(_record(4, "match_outcome", _outcome(), frame=108))
    records.append(_record(5, "veterancy_changed", {
        "object_id": 10,
        "owner_player_index": 0,
        "previous_level_id": 0,
        "previous_level": "REGULAR",
        "new_level_id": 1,
        "new_level": "VETERAN",
    }, frame=108))
    nonterminal = _finish(tmp_path / "nonterminal-outcome.ndjson", records)
    with pytest.raises(TelemetryTraceValidationError, match="immediately before complete"):
        tuple(iter_validated_trace(nonterminal))


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        ("winner_player_indices", [99], "engine player domain"),
        ("loser_player_indices", [0, 99], "engine player domain"),
        ("engine_player_indices", [0, 1], "exactly equal"),
        ("crc_mismatch_frame", 107, "completion facts"),
        ("clean_shutdown", True, "completion facts"),
    ],
)
def test_reader_binds_outcome_to_player_domain_and_completion_facts(
    tmp_path: Path,
    field: str,
    value: object,
    diagnostic: str,
) -> None:
    outcome = _outcome()
    outcome[field] = value
    if field in {"winner_player_indices", "loser_player_indices"}:
        outcome["status"] = "decided"
        outcome["source"] = "victory_conditions"
        if field == "loser_player_indices":
            outcome["winner_player_indices"] = [1]
    trace = _finish(tmp_path / f"bad-outcome-{field}.ndjson", _base(tmp_path), outcome=outcome)

    with pytest.raises(TelemetryTraceValidationError, match=diagnostic):
        tuple(iter_validated_trace(trace))
