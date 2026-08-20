"""Strict entity-lifecycle payload and cross-record identity tests."""

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from generals_replay_analyzer.telemetry.model import ObjectCreatedRecord
from generals_replay_analyzer.telemetry.reader import TelemetryTraceValidationError, iter_validated_trace

RUN_ID = "723e4567-e89b-12d3-a456-426614174000"
ENGINE_IDENTITY = "zero-hour-test-exe-00000000-ini-00000000"


def _record(version: int, sequence: int, event_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": version,
        "run_id": RUN_ID,
        "sequence": sequence,
        "frame": 0,
        "logic_time_seconds": 0.0,
        "event_type": event_type,
        "payload": payload,
    }


def _write_records(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_bytes(
        b"".join(json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records)
    )
    return path


def _completion(version: int, prior_records: list[dict[str, object]]) -> dict[str, object]:
    prior = b"".join(json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in prior_records)
    counts: dict[str, int] = {}
    for record in prior_records:
        event_type = str(record["event_type"])
        counts[event_type] = counts.get(event_type, 0) + 1
    counts["complete"] = 1
    payload: dict[str, object] = {
        "final_frame": 0,
        "command_count": 0,
        "event_counts": counts,
        "crc_mismatch": False,
        "replay_truncated": False,
        "clean_shutdown": True,
        "writer_error": None,
        "trace_sha256": hashlib.sha256(prior).hexdigest(),
        "map_assets": [],
    }
    if version == 2:
        cash_after = [
            record["payload"]["after"]
            for record in prior_records
            if record["event_type"] == "cash_changed"
        ]
        payload["final_cash_balances"] = [
            {
                "player_index": 0,
                "has_money": True,
                "balance": cash_after[-1] if cash_after else 0,
            }
        ]
    return _record(
        version,
        len(prior_records),
        "complete",
        payload,
    )


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
            for ordinal, name in enumerate(
                ["AmericaVehicleHumvee", "TargetTemplate", "DifferentTargetTemplate"]
            )
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


def _v2_manifest(reference: dict[str, object]) -> dict[str, object]:
    return {
        "engine_build": ENGINE_IDENTITY,
        "replay_version": "1.04",
        "map_identity": "maps/test.map",
        "initial_seed": 7,
        "exporter_settings": {"movement_sample_frames": 15, "audio_enabled": False},
        "game_data_catalog": reference,
    }


def _v2_players(reference: dict[str, object]) -> dict[str, object]:
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
        "engine_player_indices": [0],
        "game_data_catalog": reference,
    }


def _creation(
    object_id: int = 101,
    template_name: str = "AmericaVehicleHumvee",
    initial_status: list[str] | None = None,
) -> dict[str, object]:
    return {
        "object_id": object_id,
        "template_name": template_name,
        "owner_player_index": 0,
        "team_id": 11,
        "position_status": "placed",
        "position": {"x": 12.5, "y": -3.0, "z": 0.25},
        "orientation": 1.5,
        "kind_of_flags": ["CAN_ATTACK", "VEHICLE"],
        "initial_status": [] if initial_status is None else initial_status,
        "creation_source": "player_production",
        "creation_context": {
            "registration_frame": 7,
            "producer_object_id": None,
            "producer_player_index": None,
        },
    }


def _lifecycle_payloads() -> list[tuple[str, dict[str, object]]]:
    common = {
        "object_id": 101,
        "owner_player_index": 0,
        "team_id": 11,
        "producer_object_id": None,
        "builder_object_id": None,
        "responsible_player_index": 0,
    }
    return [
        ("object_created", _creation(initial_status=["UNDER_CONSTRUCTION"])),
        (
            "construction_started",
            {**common, "previous_state": "not_present", "new_state": "under_construction"},
        ),
        (
            "construction_completed",
            {**common, "previous_state": "under_construction", "new_state": "complete"},
        ),
        (
            "owner_changed",
            {
                "object_id": 101,
                "previous_owner_player_index": 0,
                "new_owner_player_index": None,
                "previous_team_id": 11,
                "new_team_id": None,
            },
        ),
        (
            "sold",
            {
                "object_id": 101,
                "previous_state": "available",
                "new_state": "sold",
                "owner_player_index": None,
                "team_id": None,
            },
        ),
        (
            "object_destroyed",
            {
                "object_id": 101,
                "previous_state": "sold",
                "new_state": "destroyed",
                "owner_player_index": None,
                "team_id": None,
                "destruction_source": "destroy_object",
            },
        ),
    ]


def _trace(tmp_path: Path, events: list[tuple[str, dict[str, object]]], name: str = "lifecycle.ndjson") -> Path:
    reference = _write_catalog(tmp_path)
    records = [
        _record(2, 0, "manifest", _v2_manifest(reference)),
        _record(2, 1, "players_initialized", _v2_players(reference)),
    ]
    records.extend(_record(2, sequence, event, payload) for sequence, (event, payload) in enumerate(events, 2))
    records.append(_completion(2, records))
    return _write_records(tmp_path / name, records)


def test_v2_accepts_one_ordered_lifecycle_with_explicit_nullable_identity_and_states(tmp_path: Path) -> None:
    records = tuple(iter_validated_trace(_trace(tmp_path, _lifecycle_payloads())))

    assert [record.event_type for record in records[2:-1]] == [event for event, _ in _lifecycle_payloads()]


def test_v2_accepts_explicit_unplaced_unknown_creation_without_fabricated_coordinates(tmp_path: Path) -> None:
    creation = _creation()
    creation.update(
        {
            "owner_player_index": None,
            "team_id": None,
            "position_status": "unplaced",
            "position": None,
            "creation_source": "unknown",
            "creation_context": {
                "registration_frame": 0,
                "producer_object_id": None,
                "producer_player_index": None,
            },
        }
    )

    record = tuple(iter_validated_trace(_trace(tmp_path, [("object_created", creation)])))[2]
    assert isinstance(record, ObjectCreatedRecord)
    assert record.payload.position is None


@pytest.mark.parametrize(
    ("mutate", "diagnostic"),
    [
        (lambda payload: payload.update({"object_id": 0}), "object_id"),
        (lambda payload: payload.update({"creation_source": "owner_guess"}), "creation_source"),
        (lambda payload: payload.update({"position_status": "placed", "position": None}), "payload.position"),
        (
            lambda payload: payload.update(
                {"position_status": "unplaced", "position": {"x": 0.0, "y": 0.0, "z": 0.0}}
            ),
            "payload.position",
        ),
        (lambda payload: payload.update({"initial_status": ["UNDER_CONSTRUCTION", "UNDER_CONSTRUCTION"]}), "initial_status"),
    ],
)
def test_v2_rejects_ambiguous_or_invalid_creation_identity(
    tmp_path: Path, mutate: Callable[[dict[str, object]], object], diagnostic: str
) -> None:
    creation = _creation()
    mutate(creation)

    with pytest.raises(TelemetryTraceValidationError, match=diagnostic):
        tuple(iter_validated_trace(_trace(tmp_path, [("object_created", creation)])))


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        (
            "construction_completed",
            {
                "object_id": 101,
                "previous_state": "under_construction",
                "new_state": "complete",
                "owner_player_index": 0,
                "team_id": 11,
                "producer_object_id": None,
                "builder_object_id": None,
                "responsible_player_index": None,
            },
        ),
        (
            "owner_changed",
            {
                "object_id": 101,
                "previous_owner_player_index": 0,
                "new_owner_player_index": 1,
                "previous_team_id": 11,
                "new_team_id": 12,
            },
        ),
        (
            "production_queued",
            {
                "production_id": 1,
                "engine_production_id": 1,
                "producer_object_id": 101,
                "player_index": 0,
                "template_name": "TargetTemplate",
                "queue_position": 0,
                "queued_frame": 0,
                "cost": 0,
                "quantity": 1,
                "state": "queued",
            },
        ),
        (
            "damage_applied",
            {
                "victim_object_id": 101,
                "attacker_object_id": None,
                "weapon_name": None,
                "attempted_amount": 1.0,
                "applied_amount": 1.0,
                "prior_health": 2.0,
                "new_health": 1.0,
                "damage_type": "DAMAGE_NORMAL",
                "death_type": "DEATH_NORMAL",
                "location": {"x": 0.0, "y": 0.0, "z": 0.0},
                "killing_blow": False,
            },
        ),
        (
            "order_issued",
            {
                "message_type": 1,
                "message_name": "MSG_ATTACK_OBJECT",
                "source_player_index": 0,
                "selected_object_ids": [101],
                "target_object_id": None,
                "target_location": None,
                "command_source": "replay",
            },
        ),
        (
            "entity_sample",
            {
                "object_id": 101,
                "position": {"x": 0.0, "y": 0.0, "z": 0.0},
                "orientation": 0.0,
                "layer": 0,
                "speed": 0.0,
                "current_state": "idle",
            },
        ),
    ],
)
def test_v2_rejects_every_object_reference_before_creation(
    tmp_path: Path, event: str, payload: dict[str, object]
) -> None:
    with pytest.raises(TelemetryTraceValidationError, match="references object_id 101 before object_created"):
        tuple(iter_validated_trace(_trace(tmp_path, [(event, payload)], f"reference-{event}.ndjson")))


@pytest.mark.parametrize(
    ("events", "diagnostic"),
    [
        ([*( _lifecycle_payloads()[:1]), ("object_created", _creation())], "duplicate object_created"),
        ([*_lifecycle_payloads()[:5], _lifecycle_payloads()[4]], "duplicate sold"),
        ([*_lifecycle_payloads(), _lifecycle_payloads()[-1]], "duplicate object_destroyed"),
        ([*_lifecycle_payloads(), ("entity_state_changed", {"object_id": 101, "previous_state": "x", "current_state": "y"})], "after object_destroyed"),
    ],
)
def test_v2_rejects_duplicate_or_post_destroy_lifecycle_transitions(
    tmp_path: Path, events: list[tuple[str, dict[str, object]]], diagnostic: str
) -> None:
    with pytest.raises(TelemetryTraceValidationError, match=diagnostic):
        tuple(iter_validated_trace(_trace(tmp_path, events)))


def test_v2_checks_template_identity_only_for_direct_object_identity_fields(tmp_path: Path) -> None:
    direct = _creation()
    conflicting = {
        "object_id": 101,
        "template_name": "DifferentTemplate",
        "previous_state": "idle",
        "current_state": "moving",
    }
    with pytest.raises(TelemetryTraceValidationError, match="template identity"):
        tuple(iter_validated_trace(_trace(tmp_path, [("object_created", direct), ("entity_state_changed", conflicting)])))

    production = {
        "production_id": 1,
        "engine_production_id": 1,
        "producer_object_id": 101,
        "player_index": 0,
        "template_name": "DifferentTargetTemplate",
        "queue_position": 0,
        "queued_frame": 0,
        "cost": 0,
        "quantity": 1,
        "state": "queued",
    }
    assert tuple(iter_validated_trace(_trace(tmp_path, [("object_created", direct), ("production_queued", production)])))


def test_v1_historical_lifecycle_is_not_retroactively_subject_to_v2_cross_record_rules(tmp_path: Path) -> None:
    records = [
        _record(
            1,
            0,
            "manifest",
            {
                "engine_build": "historical-build",
                "replay_version": "1.04",
                "map_identity": "maps/historical.map",
                "initial_seed": 1,
                "exporter_settings": {"movement_sample_frames": 15},
            },
        ),
        _record(1, 1, "sold", {"object_id": 7, "owner_player_index": 0}),
    ]
    records.append(_completion(1, records))

    validated = tuple(iter_validated_trace(_write_records(tmp_path / "historical-v1.ndjson", records)))
    assert [record.event_type for record in validated] == ["manifest", "sold", "complete"]


def test_lifecycle_validation_state_is_trace_local_and_resets_between_replays(tmp_path: Path) -> None:
    events = [("object_created", copy.deepcopy(_creation()))]

    first = tuple(iter_validated_trace(_trace(tmp_path, events, "first.ndjson")))
    second = tuple(iter_validated_trace(_trace(tmp_path, events, "second.ndjson")))

    assert json.dumps(first[2].payload.model_dump(), default=str, sort_keys=True) == json.dumps(
        second[2].payload.model_dump(), default=str, sort_keys=True
    )


def test_preinitialization_create_place_destroy_snapshots_flush_after_players_in_observed_order(tmp_path: Path) -> None:
    creation = _creation(object_id=303, template_name="MapBridge")
    creation.update(
        {
            "owner_player_index": None,
            "team_id": None,
            "creation_source": "map_loaded",
            "creation_context": {
                "registration_frame": 0,
                "producer_object_id": None,
                "producer_player_index": None,
            },
        }
    )
    destroyed = {
        "object_id": 303,
        "previous_state": "alive",
        "new_state": "destroyed",
        "owner_player_index": None,
        "team_id": None,
        "destruction_source": "destroy_object",
    }

    records = tuple(
        iter_validated_trace(_trace(tmp_path, [("object_created", creation), ("object_destroyed", destroyed)]))
    )

    assert [record.event_type for record in records[:4]] == [
        "manifest",
        "players_initialized",
        "object_created",
        "object_destroyed",
    ]


def test_v2_rejects_lifecycle_payload_identity_that_differs_from_tracked_owner_and_team(tmp_path: Path) -> None:
    creation = _creation()
    creation["initial_status"] = []
    sold = {
        "object_id": 101,
        "previous_state": "available",
        "new_state": "sold",
        "owner_player_index": 1,
        "team_id": 99,
    }

    with pytest.raises(TelemetryTraceValidationError, match="sold owner/team differs from object state"):
        tuple(iter_validated_trace(_trace(tmp_path, [("object_created", creation), ("sold", sold)])))


def test_v2_rejects_initial_under_construction_status_without_its_observed_transition(tmp_path: Path) -> None:
    with pytest.raises(TelemetryTraceValidationError, match="missing construction_started"):
        tuple(
            iter_validated_trace(
                _trace(tmp_path, [("object_created", _creation(initial_status=["UNDER_CONSTRUCTION"]))])
            )
        )


def test_v2_accepts_registration_identity_for_initial_construction_before_owner_change(tmp_path: Path) -> None:
    construction = {
        "object_id": 101,
        "previous_state": "not_present",
        "new_state": "under_construction",
        "owner_player_index": 0,
        "team_id": 11,
        "producer_object_id": None,
        "builder_object_id": None,
        "responsible_player_index": None,
    }
    owner_change = {
        "object_id": 101,
        "previous_owner_player_index": 0,
        "new_owner_player_index": 1,
        "previous_team_id": 11,
        "new_team_id": 12,
    }

    records = tuple(
        iter_validated_trace(
            _trace(
                tmp_path,
                [
                    ("object_created", _creation(initial_status=["UNDER_CONSTRUCTION"])),
                    ("construction_started", construction),
                    ("owner_changed", owner_change),
                ],
                "initial-construction-before-owner-change.ndjson",
            )
        )
    )

    assert [record.event_type for record in records[2:-1]] == [
        "object_created",
        "construction_started",
        "owner_changed",
    ]


def _destroyed_provenance_events() -> list[tuple[str, dict[str, object]]]:
    destroyed = {
        "object_id": 101,
        "previous_state": "alive",
        "new_state": "destroyed",
        "owner_player_index": 0,
        "team_id": 11,
        "destruction_source": "destroy_object",
    }
    return [("object_created", _creation()), ("object_destroyed", destroyed)]


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        (
            "damage_applied",
            {
                "victim_object_id": 202,
                "attacker_object_id": 101,
                "weapon_name": "TestWeapon",
                "attempted_amount": 1.0,
                "applied_amount": 1.0,
                "prior_health": 2.0,
                "new_health": 1.0,
                "damage_type": "DAMAGE_NORMAL",
                "death_type": "DEATH_NORMAL",
                "location": {"x": 0.0, "y": 0.0, "z": 0.0},
                "killing_blow": False,
            },
        ),
        (
            "supply_collected",
            {
                "collector_object_id": 202,
                "source_object_id": 101,
                "source_status": "resolved",
                "dropoff_object_id": 202,
                "player_index": 0,
                "amount": 75,
                "location": {"x": 0.0, "y": 0.0, "z": 0.0},
            },
        ),
        (
            "construction_started",
            {
                "object_id": 202,
                "previous_state": "not_present",
                "new_state": "under_construction",
                "owner_player_index": 0,
                "team_id": 11,
                "producer_object_id": 101,
                "builder_object_id": 101,
                "responsible_player_index": 0,
            },
        ),
    ],
)
def test_v2_accepts_destroyed_objects_as_historical_provenance(
    tmp_path: Path, event: str, payload: dict[str, object]
) -> None:
    current = _creation(object_id=202)
    if event == "construction_started":
        current["initial_status"] = ["UNDER_CONSTRUCTION"]
    events = [*_destroyed_provenance_events(), ("object_created", current), (event, payload)]
    if event == "supply_collected":
        events.append(
            (
                "cash_changed",
                {
                    "player_index": 0,
                    "before": 0,
                    "delta": 75,
                    "after": 75,
                    "track_income": True,
                    "reason": "supply_income",
                },
            )
        )

    records = tuple(iter_validated_trace(_trace(tmp_path, events, f"historical-{event}.ndjson")))

    assert event in [record.event_type for record in records[2:-1]]


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        (
            "damage_applied",
            {
                "victim_object_id": 101,
                "attacker_object_id": None,
                "weapon_name": None,
                "attempted_amount": 1.0,
                "applied_amount": 1.0,
                "prior_health": 1.0,
                "new_health": 0.0,
                "damage_type": "DAMAGE_NORMAL",
                "death_type": "DEATH_NORMAL",
                "location": {"x": 0.0, "y": 0.0, "z": 0.0},
                "killing_blow": True,
            },
        ),
        (
            "supply_collected",
            {
                "collector_object_id": 101,
                "source_object_id": 202,
                "source_status": "resolved",
                "dropoff_object_id": 202,
                "player_index": 0,
                "amount": 75,
                "location": {"x": 0.0, "y": 0.0, "z": 0.0},
            },
        ),
        (
            "construction_completed",
            {
                "object_id": 101,
                "previous_state": "under_construction",
                "new_state": "complete",
                "owner_player_index": 0,
                "team_id": 11,
                "producer_object_id": None,
                "builder_object_id": None,
                "responsible_player_index": None,
            },
        ),
    ],
)
def test_v2_rejects_destroyed_objects_as_current_subjects(
    tmp_path: Path, event: str, payload: dict[str, object]
) -> None:
    events = _destroyed_provenance_events()
    if event == "supply_collected":
        events.insert(1, ("object_created", _creation(object_id=202)))

    with pytest.raises(TelemetryTraceValidationError, match="after object_destroyed"):
        tuple(iter_validated_trace(_trace(tmp_path, [*events, (event, payload)], f"dead-subject-{event}.ndjson")))
