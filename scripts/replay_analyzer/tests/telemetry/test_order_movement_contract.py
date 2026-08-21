"""Strict v2 order, engine-state, and bounded movement observation contracts."""

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from generals_replay_analyzer.telemetry.model import (
    EntitySampleRecord,
    EntityStateChangedRecord,
    OrderIssuedRecord,
)
from generals_replay_analyzer.telemetry.order_coverage import SUPPORTED_ORDER_COVERAGE, canonical_order_coverage
from generals_replay_analyzer.telemetry.reader import TelemetryTraceValidationError, iter_validated_trace

RUN_ID = "923e4567-e89b-12d3-a456-426614174000"
ENGINE_IDENTITY = "zero-hour-test-exe-00000000-ini-00000000"

SUPPORTED_ORDERS = list(SUPPORTED_ORDER_COVERAGE)


def _record(sequence: int, frame: int, event_type: str, payload: dict[str, object]) -> dict[str, object]:
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
        "thing_templates": [
            {
                "ordinal": ordinal,
                "name": name,
                "faction": None,
                "kind_of_flags": [],
                "build_cost": 0,
                "configured_build_time_seconds": 0.0,
                "prerequisites": [],
                "locomotor_sets": [],
                "production_capable": False,
                "weapon_sets": [],
                "derived_weapon_names": [],
                "category_tags": [],
            }
            for ordinal, name in enumerate(["AmericaVehicleHumvee", "TargetTemplate"])
        ],
        "upgrades": [],
        "sciences": [],
        "weapons": [],
        "locomotors": [],
    }
    content = json.dumps(catalog, separators=(",", ":")).encode() + b"\n"
    digest = hashlib.sha256(content).hexdigest()
    name = f"game-data-catalog-v1-{digest}.json"
    (directory / name).write_bytes(content)
    return {"type": "game_data_catalog", "path": name, "sha256": digest, "engine_data_identity": ENGINE_IDENTITY}


def _coverage() -> dict[str, object]:
    return canonical_order_coverage()


def _players(reference: dict[str, object]) -> dict[str, object]:
    slots: list[dict[str, object]] = []
    for slot_index in range(8):
        occupied = slot_index == 0
        slots.append(
            {
                "slot_index": slot_index,
                "slot_state": "human" if occupied else "open",
                "occupied": occupied,
                "resolution_status": "resolved" if occupied else "not_applicable",
                "replay_name": "Human" if occupied else None,
                "player_index": 0 if occupied else None,
                "team_id": 0 if occupied else None,
                "faction_template_name": "FactionAmerica" if occupied else None,
                "color": 0 if occupied else None,
                "start_position_status": "resolved" if occupied else "not_applicable",
                "start_position": {"x": 1.0, "y": 2.0, "z": 3.0} if occupied else None,
                "controller": "human" if occupied else None,
                "is_human": occupied,
                "is_header_local_slot": occupied,
                "is_resolved_local_player": True if occupied else None,
            }
        )
    return {
        "header_local_slot_index": 0,
        "slots": slots,
        "engine_player_indices": [0, 1],
        "game_data_catalog": reference,
    }


def _creation(object_id: int, template_name: str) -> dict[str, object]:
    return {
        "object_id": object_id,
        "template_name": template_name,
        "owner_player_index": 0,
        "team_id": 0,
        "position_status": "placed",
        "position": {"x": 10.0, "y": 20.0, "z": 0.0},
        "orientation": 0.5,
        "kind_of_flags": ["VEHICLE"] if object_id == 101 else ["STRUCTURE"],
        "initial_status": [],
        "creation_source": "starting_object",
        "creation_context": {
            "registration_frame": 0,
            "producer_object_id": None,
            "producer_player_index": None,
        },
    }


def _order() -> dict[str, object]:
    return {
        "order_id": 1,
        "command_frame": 5,
        "message_type": 1059,
        "message_name": "MSG_DO_ATTACK_OBJECT",
        "source_player_index": 0,
        "selected_object_ids": [101],
        "selected_entities": [{"object_id": 101, "template_name": "AmericaVehicleHumvee"}],
        "target_kind": "object",
        "target_object_id": 202,
        "target_template_name": "TargetTemplate",
        "target_location": None,
        "command_source": "recorded_network_player_command",
        "ai_command_source_id": 0,
        "ai_command_source_name": "CMD_FROM_PLAYER",
    }


def _state() -> dict[str, object]:
    return {
        "object_id": 101,
        "template_name": "AmericaVehicleHumvee",
        "owner_player_index": 0,
        "previous_state": "idle",
        "previous_state_source": "ai_idle_state",
        "current_state": "moving",
        "current_state_source": "ai_moving_state",
        "previous_ai_state_id": 0,
        "current_ai_state_id": 1,
        "previous_ai_state_name": "AI_IDLE",
        "current_ai_state_name": "AI_MOVE_TO",
        "previous_ai_state_name_status": "stable",
        "current_ai_state_name_status": "stable",
        "previous_locomotor_set_id": 0,
        "current_locomotor_set_id": 0,
        "previous_locomotor_set_name": "LOCOMOTORSET_NORMAL",
        "current_locomotor_set_name": "LOCOMOTORSET_NORMAL",
        "previous_locomotor_set_name_status": "stable",
        "current_locomotor_set_name_status": "stable",
        "previous_is_engine_moving": False,
        "current_is_engine_moving": True,
        "current_order_id": 1,
        "transition_source": "end_of_game_logic_update",
    }


def _sample(reason: str) -> dict[str, object]:
    return {
        "object_id": 101,
        "template_name": "AmericaVehicleHumvee",
        "owner_player_index": 0,
        "position": {"x": 11.0, "y": 21.0, "z": 0.0},
        "orientation": 0.5,
        "layer_id": 1,
        "layer_name": "LAYER_GROUND",
        "layer_name_status": "stable",
        "speed_status": "measured_physics_velocity",
        "speed": 3.25,
        "current_state": "moving",
        "current_state_source": "ai_moving_state",
        "ai_state_id": 1,
        "ai_state_name": "AI_MOVE_TO",
        "ai_state_name_status": "stable",
        "locomotor_set_id": 0,
        "locomotor_set_name": "LOCOMOTORSET_NORMAL",
        "locomotor_set_name_status": "stable",
        "current_order_id": 1,
        "current_order_message_type": 1059,
        "current_order_message_name": "MSG_DO_ATTACK_OBJECT",
        "path_goal_status": "path_tail",
        "path_goal": {"x": 50.0, "y": 60.0, "z": 0.0},
        "is_mobile": True,
        "is_structure": False,
        "is_disabled": False,
        "is_engine_moving": True,
        "sample_reason": reason,
    }


def _lifecycle_sample(object_id: int, template_name: str, *, mobile: bool, structure: bool) -> dict[str, object]:
    sample = _sample("lifecycle_forced")
    sample.update(
        {
            "object_id": object_id,
            "template_name": template_name,
            "speed": 0.0,
            "current_state": "idle" if mobile else "unknown",
            "current_state_source": "ai_idle_state" if mobile else "ai_interface_unavailable",
            "ai_state_id": 0 if mobile else None,
            "ai_state_name": "AI_IDLE" if mobile else None,
            "ai_state_name_status": "stable" if mobile else "unavailable_no_ai",
            "locomotor_set_id": 0 if mobile else None,
            "locomotor_set_name": "LOCOMOTORSET_NORMAL" if mobile else None,
            "locomotor_set_name_status": "stable" if mobile else "unavailable_no_ai",
            "current_order_id": None,
            "current_order_message_type": None,
            "current_order_message_name": None,
            "path_goal_status": "unavailable_no_path" if mobile else "unavailable_no_ai",
            "path_goal": None,
            "is_mobile": mobile,
            "is_structure": structure,
            "is_engine_moving": False,
        }
    )
    return sample


def _valid_records(tmp_path: Path, movement_sample_frames: int = 15) -> list[dict[str, object]]:
    reference = _write_catalog(tmp_path)
    return [
        _record(
            0,
            0,
            "manifest",
            {
                "engine_build": ENGINE_IDENTITY,
                "replay_version": "1.04",
                "map_identity": "maps/test.map",
                "initial_seed": 7,
                "exporter_settings": {
                    "movement_sample_frames": movement_sample_frames,
                    "audio_enabled": False,
                    "order_coverage": _coverage(),
                },
                "game_data_catalog": reference,
            },
        ),
        _record(1, 0, "players_initialized", _players(reference)),
        _record(2, 0, "object_created", _creation(101, "AmericaVehicleHumvee")),
        _record(3, 0, "object_created", _creation(202, "TargetTemplate")),
        _record(4, 0, "entity_sample", _lifecycle_sample(101, "AmericaVehicleHumvee", mobile=True, structure=False)),
        _record(5, 0, "entity_sample", _lifecycle_sample(202, "TargetTemplate", mobile=False, structure=True)),
        _record(6, 5, "order_issued", _order()),
        _record(7, 5, "entity_state_changed", _state()),
        _record(8, 5, "entity_sample", _sample("state_forced")),
        _record(9, 20, "entity_sample", _sample("periodic_moving_heartbeat")),
        _record(
            10,
            20,
            "match_outcome",
            {
                "status": "unknown",
                "source": "unavailable",
                "winner_player_indices": [],
                "loser_player_indices": [],
                "engine_player_indices": [0, 1],
                "terminal_reason": "clean_completion",
                "quit_early": False,
                "replay_header_desync": False,
                "replay_header_disconnected_slots": [],
                "crc_mismatch": False,
                "crc_mismatch_frame": None,
                "clean_shutdown": True,
            },
        ),
    ]


def _write_complete(path: Path, records: list[dict[str, object]]) -> Path:
    prior = b"".join(json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records)
    counts: dict[str, int] = {}
    for record in records:
        event_type = str(record["event_type"])
        counts[event_type] = counts.get(event_type, 0) + 1
    counts["complete"] = 1
    final_frame = int(records[-1]["frame"])
    complete = _record(
        len(records),
        final_frame,
        "complete",
        {
            "final_frame": final_frame,
            "command_count": 1,
            "event_counts": counts,
            "terminal_reason": "clean_completion",
            "crc_mismatch": False,
            "crc_mismatch_frame": None,
            "replay_truncated": False,
            "quit_early": False,
            "replay_header_desync": False,
            "replay_header_disconnected_slots": [],
            "clean_shutdown": True,
            "writer_error": None,
            "trace_sha256": hashlib.sha256(prior).hexdigest(),
            "map_assets": [],
            "final_cash_balances": [
                {"player_index": 0, "has_money": True, "balance": 0},
                {"player_index": 1, "has_money": False, "balance": None},
            ],
        },
    )
    path.write_bytes(prior + json.dumps(complete, separators=(",", ":")).encode() + b"\n")
    return path


def test_v2_reader_accepts_source_grounded_order_state_and_bounded_samples(tmp_path: Path) -> None:
    trace = _write_complete(tmp_path / "valid.ndjson", _valid_records(tmp_path))

    records = tuple(iter_validated_trace(trace))

    assert isinstance(records[6], OrderIssuedRecord)
    assert isinstance(records[7], EntityStateChangedRecord)
    assert isinstance(records[8], EntitySampleRecord)
    assert isinstance(records[9], EntitySampleRecord)
    assert records[9].frame - records[8].frame == 15


def test_v2_reader_accepts_non_task7_fact_between_order_and_ordered_sampler_block(tmp_path: Path) -> None:
    records = _valid_records(tmp_path)
    records.insert(
        7,
        _record(
            7,
            5,
            "cash_changed",
            {
                "player_index": 0,
                "before": 0,
                "delta": 0,
                "after": 0,
                "track_income": False,
                "reason": "unknown",
            },
        ),
    )
    for sequence, record in enumerate(records):
        record["sequence"] = sequence

    trace = _write_complete(tmp_path / "mixed-producer-order.ndjson", records)

    assert tuple(iter_validated_trace(trace))[-1].event_type == "complete"


def _move_order_after_sampler(records: list[dict[str, object]]) -> None:
    order = records.pop(6)
    state_payload = records[6]["payload"]
    sample_payload = records[7]["payload"]
    assert isinstance(state_payload, dict) and isinstance(sample_payload, dict)
    state_payload["current_order_id"] = None
    sample_payload["current_order_id"] = None
    sample_payload["current_order_message_type"] = None
    sample_payload["current_order_message_name"] = None
    records.insert(8, order)


def _move_state_after_sample(records: list[dict[str, object]]) -> None:
    state = records.pop(7)
    sample_payload = records[7]["payload"]
    assert isinstance(sample_payload, dict)
    sample_payload.update(
        {
            "current_state": "idle",
            "current_state_source": "ai_idle_state",
            "ai_state_id": 0,
            "ai_state_name": "AI_IDLE",
            "is_engine_moving": False,
            "sample_reason": "order_forced",
        }
    )
    records.insert(8, state)


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        (_move_order_after_sampler, "order.*sampler"),
        (_move_state_after_sample, "sample.*state"),
        (lambda records: records.insert(4, records.pop(5)), "numeric object_id"),
        (lambda records: records.insert(8, copy.deepcopy(records[7])), "duplicate entity_state_changed"),
        (lambda records: records.insert(9, copy.deepcopy(records[8])), "duplicate entity_sample"),
    ],
    ids=[
        "order-after-state-and-sample",
        "sample-before-state",
        "descending-sampler-object-ids",
        "duplicate-state",
        "duplicate-sample",
    ],
)
def test_v2_reader_rejects_producer_impossible_same_frame_task7_ordering(
    tmp_path: Path,
    mutation: Callable[[list[dict[str, object]]], object],
    diagnostic: str,
) -> None:
    records = copy.deepcopy(_valid_records(tmp_path))
    mutation(records)
    for sequence, record in enumerate(records):
        record["sequence"] = sequence
    trace = _write_complete(tmp_path / "impossible-producer-order.ndjson", records)

    with pytest.raises(TelemetryTraceValidationError, match=diagnostic):
        tuple(iter_validated_trace(trace))


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        (lambda records: records[6]["payload"].__setitem__("message_name", "MSG_DO_MOVETO"), "message"),
        (lambda records: records[6]["payload"].__setitem__("source_player_index", 9), "player"),
        (
            lambda records: records[6]["payload"]["selected_entities"][0].__setitem__(
                "template_name", "TargetTemplate"
            ),
            "template",
        ),
        (lambda records: records[6]["payload"].__setitem__("command_frame", 4), "command_frame"),
        (lambda records: records[8]["payload"]["position"].__setitem__("x", 4.0e38), "maximum"),
        (
            lambda records: (records[9].__setitem__("frame", 21), records[9].__setitem__("logic_time_seconds", 21 / 30.0)),
            "movement_sample_frames",
        ),
        (lambda records: records[8]["payload"].__setitem__("current_state", "idle"), "state"),
        (lambda records: records[7]["payload"].__setitem__("previous_ai_state_name", "AI_MOVE_TO"), "AI state"),
        (
            lambda records: (
                records[7]["payload"].__setitem__("previous_ai_state_id", 999),
                records[7]["payload"].__setitem__("previous_ai_state_name", None),
            ),
            "AI state",
        ),
        (lambda records: records[8]["payload"].__setitem__("ai_state_name", "AI_IDLE"), "AI state"),
        (lambda records: records[7]["payload"].__setitem__("current_is_engine_moving", False), "engine-moving"),
        (lambda records: records[8]["payload"].__setitem__("locomotor_set_name", None), "locomotor"),
        (lambda records: records[8]["payload"].__setitem__("layer_name", "LAYER_WALL"), "layer"),
        (
            lambda records: (
                records[8]["payload"].__setitem__("layer_id", 99),
                records[8]["payload"].__setitem__("layer_name", None),
            ),
            "layer",
        ),
        (lambda records: records[8]["payload"].__setitem__("current_state_source", "ai_idle_state"), "classification"),
    ],
)
def test_v2_reader_rejects_malformed_order_state_and_sample_contracts(
    tmp_path: Path,
    mutation: Callable[[list[dict[str, object]]], object],
    diagnostic: str,
) -> None:
    records = copy.deepcopy(_valid_records(tmp_path))
    mutation(records)
    trace = _write_complete(tmp_path / "invalid.ndjson", records)

    with pytest.raises(TelemetryTraceValidationError, match=diagnostic):
        tuple(iter_validated_trace(trace))


def test_v2_reader_rejects_sample_after_authoritative_destroy(tmp_path: Path) -> None:
    records = _valid_records(tmp_path)
    records.insert(
        9,
        _record(
            7,
            10,
            "object_destroyed",
            {
                "object_id": 101,
                "previous_state": "alive",
                "new_state": "destroyed",
                "owner_player_index": 0,
                "team_id": 0,
                "destruction_source": "destroy_object",
            },
        ),
    )
    for sequence, record in enumerate(records):
        record["sequence"] = sequence
    trace = _write_complete(tmp_path / "after-destroy.ndjson", records)

    with pytest.raises(TelemetryTraceValidationError, match="after object_destroyed"):
        tuple(iter_validated_trace(trace))


def test_v2_reader_retires_same_frame_forced_sample_when_ordered_object_is_destroyed(tmp_path: Path) -> None:
    records = _valid_records(tmp_path)
    records[7:10] = [
        _record(
            5,
            5,
            "object_destroyed",
            {
                "object_id": 101,
                "previous_state": "alive",
                "new_state": "destroyed",
                "owner_player_index": 0,
                "team_id": 0,
                "destruction_source": "destroy_object",
            },
        )
    ]
    for sequence, record in enumerate(records):
        record["sequence"] = sequence
    trace = _write_complete(tmp_path / "same-frame-destroy.ndjson", records)

    assert tuple(iter_validated_trace(trace))[-1].event_type == "complete"


def test_v2_reader_requires_same_frame_lifecycle_baseline_for_every_created_object(tmp_path: Path) -> None:
    records = _valid_records(tmp_path)
    minimal_records = [*records[:4], records[-1]]
    for sequence, record in enumerate(minimal_records):
        record["sequence"] = sequence
    trace = _write_complete(tmp_path / "missing-lifecycle-samples.ndjson", minimal_records)

    with pytest.raises(TelemetryTraceValidationError, match="lifecycle.*entity_sample"):
        tuple(iter_validated_trace(trace))


@pytest.mark.parametrize(
    ("sample_index", "reason"),
    [
        (4, "changed"),
        (8, "order_forced"),
    ],
    ids=["lifecycle-cannot-claim-changed", "state-force-wins-over-order-force"],
)
def test_v2_reader_requires_exact_forced_sample_reason_priority(
    tmp_path: Path, sample_index: int, reason: str
) -> None:
    records = _valid_records(tmp_path)
    records[sample_index]["payload"]["sample_reason"] = reason
    trace = _write_complete(tmp_path / f"wrong-forced-reason-{sample_index}.ndjson", records)

    with pytest.raises(TelemetryTraceValidationError, match="sample_reason"):
        tuple(iter_validated_trace(trace))


def test_v2_reader_rejects_changed_payload_claiming_periodic_heartbeat(tmp_path: Path) -> None:
    records = _valid_records(tmp_path)
    records[9]["payload"]["position"]["x"] = 12.0
    trace = _write_complete(tmp_path / "changed-heartbeat.ndjson", records)

    with pytest.raises(TelemetryTraceValidationError, match="sample_reason|heartbeat"):
        tuple(iter_validated_trace(trace))


def _records_with_owner_transfer_before_unchanged_sample(tmp_path: Path, reason: str) -> list[dict[str, object]]:
    records = _valid_records(tmp_path)
    records.insert(
        9,
        _record(
            9,
            10,
            "owner_changed",
            {
                "object_id": 101,
                "previous_owner_player_index": 0,
                "new_owner_player_index": 1,
                "previous_team_id": 0,
                "new_team_id": 1,
            },
        ),
    )
    records[10]["payload"]["owner_player_index"] = 1
    records[10]["payload"]["sample_reason"] = reason
    for sequence, record in enumerate(records):
        record["sequence"] = sequence
    return records


def test_v2_reader_excludes_owner_identity_from_engine_snapshot_change_detection(tmp_path: Path) -> None:
    trace = _write_complete(
        tmp_path / "owner-transfer-heartbeat.ndjson",
        _records_with_owner_transfer_before_unchanged_sample(tmp_path, "periodic_moving_heartbeat"),
    )

    assert tuple(iter_validated_trace(trace))[-1].event_type == "complete"


def test_v2_reader_requires_state_owner_to_match_current_lifecycle_owner(tmp_path: Path) -> None:
    records = _valid_records(tmp_path)
    records[7]["payload"]["owner_player_index"] = 1
    trace = _write_complete(tmp_path / "wrong-state-owner.ndjson", records)

    with pytest.raises(TelemetryTraceValidationError, match="state owner identity"):
        tuple(iter_validated_trace(trace))


@pytest.mark.parametrize("new_owner", [1, None], ids=["player-transfer", "neutral-transfer"])
def test_v2_reader_binds_state_and_sample_owner_after_owner_change(
    tmp_path: Path, new_owner: int | None
) -> None:
    records = _valid_records(tmp_path)
    records.insert(
        7,
        _record(
            7,
            5,
            "owner_changed",
            {
                "object_id": 101,
                "previous_owner_player_index": 0,
                "new_owner_player_index": new_owner,
                "previous_team_id": 0,
                "new_team_id": 1 if new_owner is not None else None,
            },
        ),
    )
    records[8]["payload"]["owner_player_index"] = new_owner
    records[9]["payload"]["owner_player_index"] = new_owner
    records[10]["payload"]["owner_player_index"] = new_owner
    for sequence, record in enumerate(records):
        record["sequence"] = sequence
    trace = _write_complete(tmp_path / f"owner-transfer-{new_owner}.ndjson", records)

    assert tuple(iter_validated_trace(trace))[-1].event_type == "complete"


def test_v2_reader_rejects_stale_state_owner_after_owner_change(tmp_path: Path) -> None:
    records = _valid_records(tmp_path)
    records.insert(
        7,
        _record(
            7,
            5,
            "owner_changed",
            {
                "object_id": 101,
                "previous_owner_player_index": 0,
                "new_owner_player_index": 1,
                "previous_team_id": 0,
                "new_team_id": 1,
            },
        ),
    )
    for sequence, record in enumerate(records):
        record["sequence"] = sequence
    trace = _write_complete(tmp_path / "stale-state-owner-after-transfer.ndjson", records)

    with pytest.raises(TelemetryTraceValidationError, match="state owner identity"):
        tuple(iter_validated_trace(trace))


def test_v2_reader_rejects_changed_reason_caused_only_by_owner_identity(tmp_path: Path) -> None:
    trace = _write_complete(
        tmp_path / "owner-transfer-changed.ndjson",
        _records_with_owner_transfer_before_unchanged_sample(tmp_path, "changed"),
    )

    with pytest.raises(TelemetryTraceValidationError, match="sample_reason"):
        tuple(iter_validated_trace(trace))


def test_v2_reader_rejects_interval_sample_for_interval_ineligible_entity(tmp_path: Path) -> None:
    records = _valid_records(tmp_path)
    structure_sample = records[5]["payload"]
    assert isinstance(structure_sample, dict)
    structure_sample.update(
        {
            "current_state": "moving",
            "current_state_source": "ai_moving_state",
            "ai_state_id": 1,
            "ai_state_name": "AI_MOVE_TO",
            "ai_state_name_status": "stable",
            "locomotor_set_id": 0,
            "locomotor_set_name": "LOCOMOTORSET_NORMAL",
            "locomotor_set_name_status": "stable",
            "path_goal_status": "unavailable_no_path",
            "is_engine_moving": True,
        }
    )
    periodic_sample = copy.deepcopy(structure_sample)
    periodic_sample["position"]["x"] = 12.0
    periodic_sample["sample_reason"] = "changed"
    records.insert(9, _record(10, 15, "entity_sample", periodic_sample))
    for sequence, record in enumerate(records):
        record["sequence"] = sequence
    trace = _write_complete(tmp_path / "structure-changed.ndjson", records)

    with pytest.raises(TelemetryTraceValidationError, match="sample_reason"):
        tuple(iter_validated_trace(trace))


def test_v2_reader_checks_moving_tail_before_destroy_retires_sample_history(tmp_path: Path) -> None:
    records = _valid_records(tmp_path)
    records.insert(
        -1,
        _record(
            8,
            36,
            "object_destroyed",
            {
                "object_id": 101,
                "previous_state": "alive",
                "new_state": "destroyed",
                "owner_player_index": 0,
                "team_id": 0,
                "destruction_source": "destroy_object",
            },
        ),
    )
    records[-1]["frame"] = 36
    records[-1]["logic_time_seconds"] = 36 / 30.0
    for sequence, record in enumerate(records):
        record["sequence"] = sequence
    trace = _write_complete(tmp_path / "moving-tail-before-destroy.ndjson", records)

    with pytest.raises(TelemetryTraceValidationError, match="moving entity sample tail gap"):
        tuple(iter_validated_trace(trace))


def test_v2_reader_uses_producer_interval_eligibility_for_moving_structure(tmp_path: Path) -> None:
    records = _valid_records(tmp_path)
    structure_sample = records[5]["payload"]
    assert isinstance(structure_sample, dict)
    structure_sample.update(
        {
            "current_state": "moving",
            "current_state_source": "ai_moving_state",
            "ai_state_id": 1,
            "ai_state_name": "AI_MOVE_TO",
            "ai_state_name_status": "stable",
            "locomotor_set_id": 0,
            "locomotor_set_name": "LOCOMOTORSET_NORMAL",
            "locomotor_set_name_status": "stable",
            "path_goal_status": "unavailable_no_path",
            "is_engine_moving": True,
        }
    )
    trace = _write_complete(tmp_path / "moving-structure-no-periodic-sample.ndjson", records)

    assert tuple(iter_validated_trace(trace))[-1].event_type == "complete"


def test_v2_manifest_rejects_unbounded_movement_interval(tmp_path: Path) -> None:
    trace = _write_complete(tmp_path / "unbounded.ndjson", _valid_records(tmp_path, movement_sample_frames=3601))

    with pytest.raises(TelemetryTraceValidationError, match="movement_sample_frames"):
        tuple(iter_validated_trace(trace))


def test_v2_reader_rejects_unbounded_moving_sample_tail(tmp_path: Path) -> None:
    records = _valid_records(tmp_path)
    records[-1]["frame"] = 36
    records[-1]["logic_time_seconds"] = 36 / 30.0
    trace = _write_complete(tmp_path / "moving-tail-gap.ndjson", records)

    with pytest.raises(TelemetryTraceValidationError, match="moving entity sample tail gap"):
        tuple(iter_validated_trace(trace))


def test_v2_reader_requires_order_coverage_even_without_order_events(tmp_path: Path) -> None:
    records = _valid_records(tmp_path)
    minimal_records = [records[0], records[1], records[-1]]
    cast_settings = minimal_records[0]["payload"]["exporter_settings"]
    assert isinstance(cast_settings, dict)
    cast_settings.pop("order_coverage")
    for sequence, record in enumerate(minimal_records):
        record["sequence"] = sequence
    trace = _write_complete(tmp_path / "missing-order-coverage.ndjson", minimal_records)

    with pytest.raises(TelemetryTraceValidationError, match="order_coverage"):
        tuple(iter_validated_trace(trace))


@pytest.mark.parametrize("mutation", ["partial", "wrong_target_index"])
def test_v2_reader_requires_exact_canonical_order_coverage(tmp_path: Path, mutation: str) -> None:
    records = _valid_records(tmp_path)
    settings = records[0]["payload"]["exporter_settings"]
    assert isinstance(settings, dict)
    coverage = settings["order_coverage"]
    assert isinstance(coverage, dict)
    commands = coverage["supported_commands"]
    assert isinstance(commands, list)
    if mutation == "partial":
        coverage["supported_commands"] = [commands[2]]
    else:
        command = commands[2]
        assert isinstance(command, dict)
        command["target_argument_index"] = 1
    trace = _write_complete(tmp_path / f"{mutation}.ndjson", records)

    with pytest.raises(TelemetryTraceValidationError, match="order_coverage|canonical closed supported-order coverage"):
        tuple(iter_validated_trace(trace))
