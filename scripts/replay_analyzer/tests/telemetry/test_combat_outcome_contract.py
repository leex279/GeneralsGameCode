"""Strict v2 combat, player-terminal-state, and match-outcome contracts."""

import hashlib
import json
import re
import struct
from collections.abc import Callable
from pathlib import Path

import pytest

from generals_replay_analyzer.telemetry.model import FLOAT32_MAX, DamageAppliedRecord, MatchOutcomeRecord
from generals_replay_analyzer.telemetry.order_coverage import canonical_order_coverage
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
        "source_player_mask": 1,
        "source_player_indices": [0],
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


def _outcome(
    *,
    status: str = "unknown",
    winners: list[int] | None = None,
    losers: list[int] | None = None,
    terminal_reason: str = "crc_mismatch",
    quit_early: bool = False,
    replay_header_desync: bool = False,
    disconnected_slots: list[int] | None = None,
) -> dict[str, object]:
    crc_mismatch = terminal_reason == "crc_mismatch"
    return {
        "status": status,
        "source": "victory_conditions" if status == "decided" else "unavailable",
        "winner_player_indices": winners or [],
        "loser_player_indices": losers or [],
        "engine_player_indices": [0, 1, 9],
        "terminal_reason": terminal_reason,
        "quit_early": quit_early,
        "replay_header_desync": replay_header_desync,
        "replay_header_disconnected_slots": disconnected_slots or [],
        "crc_mismatch": crc_mismatch,
        "crc_mismatch_frame": 108 if crc_mismatch else None,
        "clean_shutdown": terminal_reason == "clean_completion",
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
                "exporter_settings": {
                    "movement_sample_frames": 15,
                    "audio_enabled": False,
                    "order_coverage": canonical_order_coverage(),
                },
                "game_data_catalog": reference,
            },
        ),
        _record(1, "players_initialized", _players(reference)),
        _record(2, "object_created", _created(10, 0, "AmericaTankCrusader")),
        _record(3, "object_created", _created(20, 1, "ChinaTankBattleMaster")),
    ]


def _inject_lifecycle_samples(records: list[dict[str, object]]) -> None:
    creations = {
        int(record["payload"]["object_id"]): record["payload"]
        for record in records
        if record["event_type"] == "object_created"
    }
    sampled = {
        int(record["payload"]["object_id"])
        for record in records
        if record["event_type"] == "entity_sample"
    }
    insert_at = next((index for index, record in enumerate(records) if int(record["frame"]) > 0), len(records))
    samples = []
    for object_id in sorted(creations.keys() - sampled):
        creation = creations[object_id]
        samples.append(
            _record(
                0,
                "entity_sample",
                {
                    "object_id": object_id,
                    "template_name": creation["template_name"],
                    "owner_player_index": creation["owner_player_index"],
                    "position": creation["position"],
                    "orientation": creation["orientation"],
                    "layer_id": 1,
                    "layer_name": "LAYER_GROUND",
                    "layer_name_status": "stable",
                    "speed_status": "unavailable_no_physics",
                    "speed": None,
                    "current_state": "unknown",
                    "current_state_source": "ai_interface_unavailable",
                    "ai_state_id": None,
                    "ai_state_name": None,
                    "ai_state_name_status": "unavailable_no_ai",
                    "locomotor_set_id": None,
                    "locomotor_set_name": None,
                    "locomotor_set_name_status": "unavailable_no_ai",
                    "current_order_id": None,
                    "current_order_message_type": None,
                    "current_order_message_name": None,
                    "path_goal_status": "unavailable_no_ai",
                    "path_goal": None,
                    "is_mobile": False,
                    "is_structure": False,
                    "is_disabled": False,
                    "is_engine_moving": False,
                    "sample_reason": "lifecycle_forced",
                },
            )
        )
    sample_count = len(samples)
    for offset, sample in enumerate(samples):
        sample["sequence"] = insert_at + offset
    for record in records[insert_at:]:
        record["sequence"] = int(record["sequence"]) + sample_count
    records[insert_at:insert_at] = samples


def _finish(
    path: Path,
    records: list[dict[str, object]],
    *,
    outcome: dict[str, object] | None = None,
    completion: dict[str, object] | None = None,
) -> Path:
    _inject_lifecycle_samples(records)
    if outcome is not None:
        records.append(_record(len(records), "match_outcome", outcome, frame=108))
    prior = b"".join(json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records)
    counts: dict[str, int] = {}
    for record in records:
        event_type = str(record["event_type"])
        counts[event_type] = counts.get(event_type, 0) + 1
    counts["complete"] = 1
    terminal = completion or _outcome()
    records.append(
        _record(
            len(records),
            "complete",
            {
                "final_frame": 108,
                "command_count": 16,
                "event_counts": counts,
                "terminal_reason": terminal["terminal_reason"],
                "crc_mismatch": terminal["crc_mismatch"],
                "crc_mismatch_frame": terminal["crc_mismatch_frame"],
                "replay_truncated": terminal["terminal_reason"] == "replay_truncated",
                "quit_early": terminal["quit_early"],
                "replay_header_desync": terminal["replay_header_desync"],
                "replay_header_disconnected_slots": terminal["replay_header_disconnected_slots"],
                "clean_shutdown": terminal["clean_shutdown"],
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
    assert records[6].payload.applied_amount == 40.0


def test_v2_engine_real_bound_matches_writer_nine_digit_float32_serialization() -> None:
    binary_float32_max = struct.unpack("!f", bytes.fromhex("7f7fffff"))[0]
    writer_text = (
        Path(__file__).parents[4]
        / "GeneralsMD/Code/GameEngine/Source/Common/ReplayCombat.cpp"
    ).read_text(encoding="utf-8")
    schema = json.loads(
        (Path(__file__).parents[2] / "contracts/telemetry-v2.schema.json").read_text(encoding="utf-8")
    )

    assert "std::chars_format::general, 9" in writer_text
    assert format(binary_float32_max, ".9g") == "3.40282347e+38"
    assert FLOAT32_MAX == float(format(binary_float32_max, ".9g"))
    assert schema["$defs"]["engineReal"] == {
        "type": "number",
        "minimum": -FLOAT32_MAX,
        "maximum": FLOAT32_MAX,
    }


@pytest.mark.parametrize("serialized_bound", [3.40282347e38, -3.40282347e38], ids=["positive", "negative"])
def test_reader_accepts_writer_serialized_float32_limits(tmp_path: Path, serialized_bound: float) -> None:
    records = _base(tmp_path)
    damage = _damage(prior=3.40282347e38, new=0.0, killing=True)
    damage["attempted_amount"] = serialized_bound
    location = damage["location"]
    assert isinstance(location, dict)
    location["x"] = serialized_bound
    records.append(_record(4, "damage_applied", damage, frame=10))

    validated = tuple(
        iter_validated_trace(
            _finish(tmp_path / f"float32-limit-{serialized_bound}.ndjson", records, outcome=_outcome())
        )
    )

    observed = next(record.payload for record in validated if record.event_type == "damage_applied")
    assert observed.attempted_amount == serialized_bound
    assert observed.location.x == serialized_bound
    assert observed.applied_amount == 3.40282347e38


@pytest.mark.parametrize("value", [3.40282348e38, -3.40282348e38, 1e39, -1e39])
def test_reader_rejects_values_above_writer_serialized_float32_limits(tmp_path: Path, value: float) -> None:
    records = _base(tmp_path)
    damage = _damage()
    location = damage["location"]
    assert isinstance(location, dict)
    location["x"] = value
    records.append(_record(4, "damage_applied", damage, frame=10))

    with pytest.raises(TelemetryTraceValidationError, match="float32"):
        tuple(iter_validated_trace(_finish(tmp_path / f"above-float32-{value}.ndjson", records, outcome=_outcome())))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_v2_combat_model_rejects_nonfinite_engine_reals(value: float) -> None:
    damage = _record(4, "damage_applied", _damage(), frame=10)
    payload = damage["payload"]
    assert isinstance(payload, dict)
    location = payload["location"]
    assert isinstance(location, dict)
    location["x"] = value

    with pytest.raises(ValueError, match="finite float32"):
        DamageAppliedRecord.model_validate(damage)


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

    legacy_payload = damage["payload"]
    assert isinstance(legacy_payload, dict)
    legacy_payload["attempted_amount"] = 3.5e38
    legacy_payload["calculated_amount"] = 3.5e38
    legacy_payload["applied_amount"] = 3.5e38
    legacy_payload["prior_health"] = 3.5e38
    legacy_payload["new_health"] = 0.0
    legacy_location = legacy_payload["location"]
    assert isinstance(legacy_location, dict)
    legacy_location["x"] = 3.5e38
    assert DamageAppliedRecord.model_validate(damage).payload.attempted_amount == 3.5e38

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


def test_health_events_are_independent_transitions_not_a_complete_health_ledger(tmp_path: Path) -> None:
    records = _base(tmp_path)
    records.extend(
        [
            _record(4, "damage_applied", _damage(prior=100.0, new=60.0), frame=10),
            _record(
                5,
                "veterancy_changed",
                {
                    "object_id": 20,
                    "owner_player_index": 1,
                    "previous_level_id": 0,
                    "previous_level": "REGULAR",
                    "new_level_id": 1,
                    "new_level": "VETERAN",
                },
                frame=11,
            ),
            _record(6, "damage_applied", _damage(prior=80.0, new=70.0), frame=12),
        ]
    )

    records = tuple(iter_validated_trace(_finish(tmp_path / "unobserved-rescale.ndjson", records, outcome=_outcome())))

    assert [record.payload.prior_health for record in records if record.event_type == "damage_applied"] == [100.0, 80.0]


def test_bridge_style_healing_after_killing_damage_is_allowed_until_object_destroyed(tmp_path: Path) -> None:
    records = _base(tmp_path)
    records.extend(
        [
            _record(4, "damage_applied", _damage(prior=50.0, new=0.0, killing=True), frame=10),
            _record(
                5,
                "healing_applied",
                {
                    "target_object_id": 20,
                    "target_player_index": 1,
                    "source_object_id": None,
                    "source_player_index": None,
                    "attempted_amount": 10.0,
                    "calculated_amount": 10.0,
                    "applied_amount": 10.0,
                    "prior_health": 0.0,
                    "new_health": 10.0,
                    "location": {"x": 20.0, "y": 5.0, "z": 0.0},
                },
                frame=11,
            ),
        ]
    )

    validated = tuple(iter_validated_trace(_finish(tmp_path / "bridge-repair.ndjson", records, outcome=_outcome())))

    assert [
        record.event_type for record in validated if record.event_type in {"damage_applied", "healing_applied"}
    ] == ["damage_applied", "healing_applied"]


def test_healing_requires_its_current_source_to_remain_alive(tmp_path: Path) -> None:
    records = _base(tmp_path)
    records.extend(
        [
            _record(
                4,
                "object_destroyed",
                {
                    "object_id": 10,
                    "previous_state": "alive",
                    "new_state": "destroyed",
                    "owner_player_index": 0,
                    "team_id": 0,
                    "destruction_source": "destroy_object",
                },
                frame=10,
            ),
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
                    "prior_health": 50.0,
                    "new_health": 60.0,
                    "location": {"x": 20.0, "y": 5.0, "z": 0.0},
                },
                frame=11,
            ),
        ]
    )

    with pytest.raises(TelemetryTraceValidationError, match="after object_destroyed"):
        tuple(iter_validated_trace(_finish(tmp_path / "destroyed-healer.ndjson", records, outcome=_outcome())))


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
            else "replay_header_disconnect_plus_executed_false_self_destruct"
            if status == "disconnected"
            else "executed_true_self_destruct"
        ),
        "replay_slot_index": 1,
    }
    records.extend(
        [
            _record(4, event_type, payload, frame=50),
            _record(5, event_type, payload, frame=51),
        ]
    )
    terminal = _outcome(disconnected_slots=[1] if event_type == "player_disconnected" else [])
    trace = _finish(
        tmp_path / f"duplicate-{event_type}.ndjson",
        records,
        outcome=terminal,
        completion=terminal,
    )

    with pytest.raises(TelemetryTraceValidationError, match="terminal player transition"):
        tuple(iter_validated_trace(trace))


def test_decided_team_outcome_keeps_eliminated_victorious_ally_out_of_losers(tmp_path: Path) -> None:
    records = _base(tmp_path)
    records.append(
        _record(
            4,
            "player_defeated",
            {
                "player_index": 1,
                "previous_status": "active",
                "new_status": "defeated",
                "source": "victory_conditions",
                "replay_slot_index": 1,
            },
            frame=80,
        )
    )
    outcome = _outcome(status="decided", winners=[0, 1], losers=[9])

    validated = tuple(iter_validated_trace(_finish(tmp_path / "team-outcome.ndjson", records, outcome=outcome)))

    assert validated[-2].payload.winner_player_indices == [0, 1]
    assert validated[-2].payload.loser_player_indices == [9]


def test_disconnect_requires_matching_header_slot_while_header_only_metadata_is_allowed(tmp_path: Path) -> None:
    disconnected = {
        "player_index": 1,
        "previous_status": "active",
        "new_status": "disconnected",
        "source": "replay_header_disconnect_plus_executed_false_self_destruct",
        "replay_slot_index": 1,
    }
    mismatched = _base(tmp_path)
    mismatched.append(_record(4, "player_disconnected", disconnected, frame=50))
    with pytest.raises(TelemetryTraceValidationError, match="disconnect metadata"):
        tuple(iter_validated_trace(_finish(tmp_path / "disconnect-without-header.ndjson", mismatched, outcome=_outcome())))

    header_only = _outcome(disconnected_slots=[1])
    validated = tuple(
        iter_validated_trace(
            _finish(
                tmp_path / "header-only-disconnect.ndjson",
                _base(tmp_path),
                outcome=header_only,
                completion=header_only,
            )
        )
    )
    assert all(record.event_type != "player_disconnected" for record in validated)


def test_true_surrender_stays_surrender_even_when_header_later_marks_slot_disconnected(tmp_path: Path) -> None:
    records = _base(tmp_path)
    records.append(
        _record(
            4,
            "player_surrendered",
            {
                "player_index": 1,
                "previous_status": "active",
                "new_status": "surrendered",
                "source": "executed_true_self_destruct",
                "replay_slot_index": 1,
            },
            frame=50,
        )
    )
    terminal = _outcome(disconnected_slots=[1])

    validated = tuple(
        iter_validated_trace(
            _finish(tmp_path / "surrender-before-later-disconnect.ndjson", records, outcome=terminal, completion=terminal)
        )
    )

    assert any(record.event_type == "player_surrendered" for record in validated)


@pytest.mark.parametrize("terminal_reason", ["clean_completion", "replay_truncated", "interrupted"])
def test_complete_carries_explicit_non_crc_termination_reason(tmp_path: Path, terminal_reason: str) -> None:
    terminal = _outcome(
        terminal_reason=terminal_reason,
        quit_early=terminal_reason == "clean_completion",
        replay_header_desync=terminal_reason == "interrupted",
    )

    validated = tuple(
        iter_validated_trace(
            _finish(
                tmp_path / f"{terminal_reason}.ndjson",
                _base(tmp_path),
                outcome=terminal,
                completion=terminal,
            )
        )
    )

    assert validated[-1].payload.terminal_reason == terminal_reason
    assert validated[-2].payload.terminal_reason == terminal_reason


def test_reader_rejects_termination_reason_that_contradicts_completion_flags(tmp_path: Path) -> None:
    terminal = _outcome(terminal_reason="interrupted")
    terminal["clean_shutdown"] = True
    trace = _finish(tmp_path / "contradictory-interruption.ndjson", _base(tmp_path), outcome=terminal, completion=terminal)

    with pytest.raises(TelemetryTraceValidationError, match="terminal_reason"):
        tuple(iter_validated_trace(trace))


@pytest.mark.parametrize("destroy_source", [False, True])
def test_damage_provenance_uses_immutable_mask_after_attacker_transfer_or_destroy(
    tmp_path: Path, destroy_source: bool
) -> None:
    records = _base(tmp_path)
    records.append(
        _record(
            4,
            "owner_changed",
            {
                "object_id": 10,
                "previous_owner_player_index": 0,
                "new_owner_player_index": 1,
                "previous_team_id": 0,
                "new_team_id": 1,
            },
            frame=10,
        )
    )
    if destroy_source:
        records.append(
            _record(
                5,
                "object_destroyed",
                {
                    "object_id": 10,
                    "previous_state": "alive",
                    "new_state": "destroyed",
                    "owner_player_index": 1,
                    "team_id": 1,
                    "destruction_source": "destroy_object",
                },
                frame=11,
            )
        )
    records.append(_record(len(records), "damage_applied", _damage(), frame=12))

    validated = tuple(iter_validated_trace(_finish(tmp_path / f"delayed-{destroy_source}.ndjson", records, outcome=_outcome())))
    damage = next(record for record in validated if record.event_type == "damage_applied")

    assert damage.payload.source_player_mask == 1
    assert damage.payload.source_player_indices == [0]


@pytest.mark.parametrize(
    ("mask", "indices", "diagnostic"),
    [
        (1, [1], "source_player_mask"),
        (1 << 5, [5], "engine player domain"),
    ],
)
def test_reader_rejects_damage_source_mask_mismatch_or_unknown_player_bit(
    tmp_path: Path, mask: int, indices: list[int], diagnostic: str
) -> None:
    records = _base(tmp_path)
    damage = _damage()
    damage["source_player_mask"] = mask
    damage["source_player_indices"] = indices
    records.append(_record(4, "damage_applied", damage, frame=10))

    with pytest.raises(TelemetryTraceValidationError, match=diagnostic):
        tuple(iter_validated_trace(_finish(tmp_path / f"bad-mask-{mask}.ndjson", records, outcome=_outcome())))


@pytest.mark.parametrize(
    ("mutate", "diagnostic"),
    [
        (lambda payload: payload.update({"damage_type_id": 3, "damage_type": "ARMOR_PIERCING"}), "damage type"),
        (lambda payload: payload.update({"death_type_id": 4, "death_type": "NORMAL"}), "death type"),
        (lambda payload: payload["location"].update({"x": 3.5e38}), "float32"),
    ],
)
def test_reader_rejects_combat_type_pair_drift_and_non_float32_real(
    tmp_path: Path,
    mutate: Callable[[dict[str, object]], object],
    diagnostic: str,
) -> None:
    records = _base(tmp_path)
    damage = _damage()
    mutate(damage)
    records.append(_record(4, "damage_applied", damage, frame=10))

    with pytest.raises(TelemetryTraceValidationError, match=diagnostic):
        tuple(iter_validated_trace(_finish(tmp_path / f"bad-combat-{diagnostic}.ndjson", records, outcome=_outcome())))


def test_zero_hour_combat_type_contract_matches_authoritative_engine_sources() -> None:
    project_root = Path(__file__).parents[2]
    repository_root = project_root.parents[1]
    contract = json.loads((project_root / "contracts" / "zero-hour-combat-types-v1.json").read_text(encoding="utf-8"))
    damage_source = (
        repository_root / "Core/GameEngine/Source/GameLogic/System/Damage.cpp"
    ).read_text(encoding="utf-8")
    damage_block = damage_source.split("DamageTypeFlags::s_bitNameList[] =", maxsplit=1)[1].split(
        "nullptr", maxsplit=1
    )[0]
    damage_block = re.sub(r"#if RTS_GENERALS.*?#endif", "", damage_block, flags=re.DOTALL)
    damage_names = re.findall(r'"([A-Z0-9_]+)"', damage_block)
    death_source = (repository_root / "Core/GameEngine/Include/GameLogic/Damage.h").read_text(encoding="utf-8")
    death_block = death_source.split("TheDeathNames[] =", maxsplit=1)[1].split("nullptr", maxsplit=1)[0]
    death_names = re.findall(r'"([A-Z0-9_]+)"', death_block)

    assert contract == {
        "schema_version": 1,
        "game": "zero_hour",
        "damage_types": [{"id": index, "name": name} for index, name in enumerate(damage_names)],
        "death_types": [{"id": index, "name": name} for index, name in enumerate(death_names)],
    }


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
