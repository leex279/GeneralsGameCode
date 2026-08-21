"""Strict v2 economy, production, science, and special-power trace contracts."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from map_asset_support import write_test_map_asset
from pydantic import ValidationError

from generals_replay_analyzer.telemetry.model import (
    CashChangedPayload,
    FinalCashBalance,
    SupplyCollectedPayload,
)
from generals_replay_analyzer.telemetry.order_coverage import canonical_order_coverage
from generals_replay_analyzer.telemetry.reader import TelemetryTraceValidationError, iter_validated_trace

RUN_ID = "823e4567-e89b-12d3-a456-426614174000"
ENGINE_IDENTITY = "zero-hour-economy-test-exe-00000000-ini-00000000"
UINT32_MAX = 4_294_967_295


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


def _catalog() -> dict[str, object]:
    return {
        "schema_version": 1,
        "type": "game_data_catalog",
        "engine_data_identity": ENGINE_IDENTITY,
        "weapon_scope": "referenced_by_thing_templates",
        "locomotor_scope": "referenced_by_thing_templates",
        "thing_templates": [
            {
                "ordinal": 0,
                "name": "AmericaTankCrusader",
                "faction": "America",
                "kind_of_flags": [],
                "build_cost": 900,
                "configured_build_time_seconds": 10.0,
                "prerequisites": [],
                "locomotor_sets": [],
                "production_capable": False,
                "weapon_sets": [],
                "derived_weapon_names": [],
                "category_tags": [],
            }
        ],
        "upgrades": [{"ordinal": 0, "name": "Upgrade_AmericaCompositeArmor"}],
        "sciences": [{"ordinal": 0, "name": "SCIENCE_ArtilleryBarrage1"}],
        "weapons": [],
        "locomotors": [],
    }


def _write_catalog(directory: Path) -> dict[str, object]:
    content = json.dumps(_catalog(), separators=(",", ":")).encode() + b"\n"
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
    slots: list[dict[str, object]] = [
        {
            "slot_index": 0,
            "slot_state": "human",
            "occupied": True,
            "resolution_status": "resolved",
            "replay_name": "Human",
            "player_index": 0,
            "team_id": 0,
            "faction_template_name": "FactionAmerica",
            "color": 0,
            "start_position_status": "resolved",
            "start_position": {"x": 0.0, "y": 0.0, "z": 0.0},
            "controller": "human",
            "is_human": True,
            "is_header_local_slot": True,
            "is_resolved_local_player": True,
        }
    ]
    for index in range(1, 8):
        slots.append(
            {
                "slot_index": index,
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
        "engine_player_indices": [0],
        "game_data_catalog": reference,
    }


def _object_created(object_id: int, template_name: str) -> dict[str, object]:
    return {
        "object_id": object_id,
        "template_name": template_name,
        "owner_player_index": 0,
        "team_id": 0,
        "position_status": "placed",
        "position": {"x": float(object_id), "y": 2.0, "z": 0.0},
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


def _production(state: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "production_id": 1,
        "engine_production_id": 7,
        "producer_object_id": 10,
        "player_index": 0,
        "template_name": "AmericaTankCrusader",
        "queue_position": 0,
        "queued_frame": 1,
        "cost": 900,
        "quantity": 1,
        "state": state,
    }
    if state != "queued":
        payload["terminal_frame"] = 20
    return payload


def _upgrade(state: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "upgrade_queue_id": 1,
        "producer_object_id": 10,
        "player_index": 0,
        "upgrade_name": "Upgrade_AmericaCompositeArmor",
        "queue_position": 0,
        "queued_frame": 2,
        "cost": 1000,
        "state": state,
    }
    if state != "queued":
        payload["terminal_frame"] = 12
    return payload


def _base_records(directory: Path) -> list[dict[str, object]]:
    reference = _write_catalog(directory)
    map_reference = write_test_map_asset(directory, ENGINE_IDENTITY, "maps/test.map")
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
                "map_asset": map_reference,
            },
        ),
        _record(1, "players_initialized", _players(reference)),
        _record(2, "object_created", _object_created(10, "AmericaWarFactory")),
        _record(3, "object_created", _object_created(20, "AmericaVehicleChinook")),
        _record(4, "object_created", _object_created(30, "SupplyWarehouse")),
        _record(5, "object_created", _object_created(40, "AmericaSupplyCenter")),
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
    current_owners = {object_id: creation["owner_player_index"] for object_id, creation in creations.items()}
    for record in records[:insert_at]:
        if record["event_type"] == "owner_changed":
            payload = record["payload"]
            current_owners[int(payload["object_id"])] = payload["new_owner_player_index"]
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
                    "owner_player_index": current_owners[object_id],
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
                    "position_bounds_policy": "pathfinder_xy_closed",
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


def _finish(path: Path, records: list[dict[str, object]], balances: list[dict[str, object]]) -> Path:
    records[:] = [record for record in records if record["event_type"] != "match_outcome"]
    _inject_lifecycle_samples(records)
    players = next(record for record in records if record["event_type"] == "players_initialized")
    domain = players["payload"]["engine_player_indices"]
    records.append(
        _record(
            len(records),
            "match_outcome",
            {
                "status": "unknown",
                "source": "unavailable",
                "winner_player_indices": [],
                "loser_player_indices": [],
                "engine_player_indices": domain,
                "terminal_reason": "clean_completion",
                "quit_early": False,
                "replay_header_desync": False,
                "replay_header_disconnected_slots": [],
                "crc_mismatch": False,
                "crc_mismatch_frame": None,
                "clean_shutdown": True,
            },
            frame=20,
        )
    )
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
                "final_frame": 20,
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
                "map_assets": [records[0]["payload"]["map_asset"]],
                "final_cash_balances": balances,
            },
            frame=20,
        )
    )
    path.write_bytes(
        b"".join(json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records)
    )
    return path


def _valid_trace(directory: Path, name: str = "economy.ndjson") -> Path:
    records = _base_records(directory)
    records.extend(
        [
            _record(6, "production_queued", _production("queued"), frame=1),
            _record(7, "cash_changed", {"player_index": 0, "before": 5000, "delta": -900, "after": 4100, "track_income": False, "reason": "unit_cost"}, frame=1),
            _record(8, "production_completed", _production("completed"), frame=20),
            _record(9, "upgrade_queued", _upgrade("queued"), frame=2),
            _record(10, "upgrade_cancelled", _upgrade("cancelled"), frame=12),
            _record(11, "science_purchased", {"science_name": "SCIENCE_ArtilleryBarrage1", "player_index": 0, "purchase_cost_points": 1, "points_before": 1, "points_after": 0, "source_object_id": None}, frame=3),
            _record(12, "special_power_used", {"special_power_name": "SuperweaponSpySatellite", "player_index": 0, "source_object_id": 10, "target_object_id": None, "target_location": {"x": 50.0, "y": 60.0, "z": 0.0}}, frame=4),
            _record(13, "supply_collected", {"collector_object_id": 20, "source_object_id": 30, "source_status": "resolved", "dropoff_object_id": 40, "player_index": 0, "amount": 600, "location": {"x": 40.0, "y": 2.0, "z": 0.0}}, frame=15),
            _record(14, "cash_changed", {"player_index": 0, "before": 4100, "delta": 600, "after": 4700, "track_income": True, "reason": "supply_income"}, frame=15),
        ]
    )
    return _finish(directory / name, records, [{"player_index": 0, "has_money": True, "balance": 4700}])


def test_reader_accepts_structurally_distinct_authoritative_economy_and_production_records(tmp_path: Path) -> None:
    records = tuple(iter_validated_trace(_valid_trace(tmp_path)))
    events = {record.event_type: record for record in records}

    assert events["production_queued"].payload.production_id == 1
    assert events["upgrade_queued"].payload.upgrade_queue_id == 1
    assert events["science_purchased"].payload.purchase_cost_points == 1
    assert events["special_power_used"].payload.source_object_id == 10
    assert type(events["supply_collected"].payload.amount) is int
    assert events["supply_collected"].sequence < events["cash_changed"].sequence
    assert records[-1].payload.final_cash_balances[0].balance == 4700


def test_reader_preserves_full_player_domain_with_zero_cash_and_no_money_entries(tmp_path: Path) -> None:
    trace = _valid_trace(tmp_path, "source.ndjson")
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()][:-1]
    players = next(record for record in records if record["event_type"] == "players_initialized")
    players["payload"]["engine_player_indices"] = [0, 4, 9]
    balances = [
        {"player_index": 0, "has_money": True, "balance": 4700},
        {"player_index": 4, "has_money": True, "balance": 0},
        {"player_index": 9, "has_money": False, "balance": None},
    ]
    domain_trace = _finish(tmp_path / "full-player-domain.ndjson", records, balances)

    validated = tuple(iter_validated_trace(domain_trace))
    assert validated[1].payload.engine_player_indices == [0, 4, 9]
    assert [entry.player_index for entry in validated[-1].payload.final_cash_balances] == [0, 4, 9]


def test_reader_rejects_resolved_replay_slot_omitted_from_engine_player_domain(tmp_path: Path) -> None:
    records = _base_records(tmp_path)[:2]
    players = records[1]
    players["payload"]["engine_player_indices"] = [4]
    trace = _finish(
        tmp_path / "resolved-slot-outside-domain.ndjson",
        records,
        [{"player_index": 4, "has_money": False, "balance": None}],
    )

    with pytest.raises(TelemetryTraceValidationError, match="resolved replay slot player_index.*engine player domain"):
        tuple(iter_validated_trace(trace))


@pytest.mark.parametrize("transition", ["object_created", "owner_changed"])
def test_reader_rejects_lifecycle_owner_omitted_from_engine_player_domain(
    tmp_path: Path,
    transition: str,
) -> None:
    records = _base_records(tmp_path)[:2]
    records.append(_record(2, "object_created", _object_created(10, "AmericaWarFactory")))
    if transition == "object_created":
        records[-1]["payload"]["owner_player_index"] = 4
    else:
        records.append(_record(3, "owner_changed", _owner_changed(10, 4)))
    trace = _finish(
        tmp_path / f"lifecycle-owner-outside-domain-{transition}.ndjson",
        records,
        [{"player_index": 0, "has_money": False, "balance": None}],
    )

    with pytest.raises(TelemetryTraceValidationError, match="lifecycle owner.*engine player domain"):
        tuple(iter_validated_trace(trace))


def test_reader_accepts_neutral_and_map_lifecycle_owners_in_full_player_domain(tmp_path: Path) -> None:
    records = _base_records(tmp_path)[:2]
    players = records[1]
    players["payload"]["engine_player_indices"] = [0, 4, 9]
    neutral = _object_created(10, "AmericaWarFactory")
    neutral.update({"owner_player_index": 9, "team_id": 9})
    records.extend(
        [
            _record(2, "object_created", neutral),
            _record(
                3,
                "owner_changed",
                {
                    "object_id": 10,
                    "previous_owner_player_index": 9,
                    "new_owner_player_index": 4,
                    "previous_team_id": 9,
                    "new_team_id": 4,
                },
            ),
        ]
    )
    trace = _finish(
        tmp_path / "neutral-map-owner-domain.ndjson",
        records,
        [
            {"player_index": 0, "has_money": False, "balance": None},
            {"player_index": 4, "has_money": False, "balance": None},
            {"player_index": 9, "has_money": False, "balance": None},
        ],
    )

    validated = tuple(iter_validated_trace(trace))
    assert validated[2].payload.owner_player_index == 9
    assert validated[3].payload.new_owner_player_index == 4


@pytest.mark.parametrize(
    ("mutate", "diagnostic"),
    [
        (lambda records: records.insert(7, _record(7, "production_queued", _production("queued"), frame=1)), "duplicate production_queued"),
        (lambda records: records.insert(8, _record(8, "production_cancelled", _production("cancelled"), frame=20)), "mutually exclusive terminal"),
        (lambda records: records[7]["payload"].update({"template_name": "ChangedTemplate"}), "production identity"),
        (lambda records: records.__setitem__(8, _record(8, "upgrade_completed", _upgrade("completed"), frame=12)), "requires upgrade_queued"),
        (
            lambda records: records.__setitem__(
                7,
                _record(
                    7,
                    "production_completed",
                    {**_production("completed"), "terminal_frame": 0},
                    frame=0,
                ),
            ),
            "precedes queued_frame",
        ),
    ],
)
def test_reader_atomically_rejects_duplicate_or_inconsistent_queue_state(
    tmp_path: Path,
    mutate: Callable[[list[dict[str, object]]], object],
    diagnostic: str,
) -> None:
    records = _base_records(tmp_path)
    records.extend(
        [
            _record(6, "production_queued", _production("queued"), frame=1),
            _record(7, "production_completed", _production("completed"), frame=20),
            _record(8, "upgrade_queued", _upgrade("queued"), frame=2),
        ]
    )
    mutate(records)
    for sequence, record in enumerate(records):
        record["sequence"] = sequence
    trace = _finish(tmp_path / f"bad-queue-{diagnostic.split()[0]}.ndjson", records, [{"player_index": 0, "has_money": True, "balance": 0}])

    with pytest.raises(TelemetryTraceValidationError, match=diagnostic):
        next(iter_validated_trace(trace))


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        ("delta", 599, "exact cash delta"),
        ("before", 4099, "cash continuity"),
        ("reason", "guessed_category", "payload.reason"),
    ],
)
def test_reader_rejects_broken_cash_arithmetic_continuity_or_reason(
    tmp_path: Path, field: str, value: object, diagnostic: str
) -> None:
    trace = _valid_trace(tmp_path, f"bad-cash-{field}.ndjson")
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    cash = [record for record in records if record["event_type"] == "cash_changed"][-1]
    cash["payload"][field] = value
    if field == "before":
        cash["payload"]["delta"] = cash["payload"]["after"] - value
    records.pop()
    damaged = _finish(tmp_path / f"damaged-cash-{field}.ndjson", records, [{"player_index": 0, "has_money": True, "balance": 4700}])

    with pytest.raises(TelemetryTraceValidationError, match=diagnostic):
        tuple(iter_validated_trace(damaged))


@pytest.mark.parametrize(
    ("balances", "diagnostic"),
    [
        ([], "final_cash_balances"),
        ([{"player_index": 0, "has_money": True, "balance": 999}], "final cash balance"),
        ([{"player_index": 1, "has_money": False, "balance": None}, {"player_index": 0, "has_money": True, "balance": 4700}], "ordered by player_index"),
        ([{"player_index": 0, "has_money": True, "balance": 4700}, {"player_index": 0, "has_money": True, "balance": 4700}], "duplicate player_index"),
    ],
)
def test_reader_reconciles_cash_chains_with_ordered_terminal_engine_balances(
    tmp_path: Path, balances: list[dict[str, object]], diagnostic: str
) -> None:
    trace = _valid_trace(tmp_path, "source.ndjson")
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()][:-1]
    damaged = _finish(tmp_path / f"bad-final-{len(balances)}.ndjson", records, balances)

    with pytest.raises(TelemetryTraceValidationError, match=diagnostic):
        next(iter_validated_trace(damaged))


@pytest.mark.parametrize(
    ("domain", "balances"),
    [
        ([0, 4], [{"player_index": 0, "has_money": True, "balance": 4700}]),
        (
            [0],
            [
                {"player_index": 0, "has_money": True, "balance": 4700},
                {"player_index": 4, "has_money": False, "balance": None},
            ],
        ),
    ],
)
def test_reader_requires_terminal_balances_to_equal_the_initialized_player_domain(
    tmp_path: Path,
    domain: list[int],
    balances: list[dict[str, object]],
) -> None:
    trace = _valid_trace(tmp_path, "source.ndjson")
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()][:-1]
    players = next(record for record in records if record["event_type"] == "players_initialized")
    players["payload"]["engine_player_indices"] = domain
    damaged = _finish(tmp_path / f"bad-domain-{len(domain)}-{len(balances)}.ndjson", records, balances)

    with pytest.raises(TelemetryTraceValidationError, match="exactly equal engine_player_indices"):
        next(iter_validated_trace(damaged))


@pytest.mark.parametrize(
    "event_type",
    [
        "production_queued",
        "production_completed",
        "upgrade_queued",
        "upgrade_cancelled",
        "science_purchased",
        "special_power_used",
        "cash_changed",
        "supply_collected",
    ],
)
def test_reader_requires_task_player_indices_in_the_initialized_engine_domain(
    tmp_path: Path, event_type: str
) -> None:
    trace = _valid_trace(tmp_path, "source.ndjson")
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()][:-1]
    target = next(record for record in records if record["event_type"] == event_type)
    target["payload"]["player_index"] = 99
    if event_type == "supply_collected":
        paired_cash = records[records.index(target) + 1]
        paired_cash["payload"]["player_index"] = 99
    damaged = _finish(
        tmp_path / f"bad-player-domain-{event_type}.ndjson",
        records,
        [{"player_index": 0, "has_money": True, "balance": 4700}],
    )

    with pytest.raises(TelemetryTraceValidationError, match="engine player domain"):
        next(iter_validated_trace(damaged))


def _owner_changed(object_id: int, new_owner: int | None) -> dict[str, object]:
    return {
        "object_id": object_id,
        "previous_owner_player_index": 0,
        "new_owner_player_index": new_owner,
        "previous_team_id": 0,
        "new_team_id": new_owner,
    }


def _destroyed(object_id: int) -> dict[str, object]:
    return {
        "object_id": object_id,
        "previous_state": "alive",
        "new_state": "destroyed",
        "owner_player_index": 0,
        "team_id": 0,
        "destruction_source": "destroy_object",
    }


def _insert_before_event(
    records: list[dict[str, object]],
    event_type: str,
    inserted_event_type: str,
    payload: dict[str, object],
) -> None:
    target = next(record for record in records if record["event_type"] == event_type)
    target_index = records.index(target)
    records.insert(
        target_index,
        _record(0, inserted_event_type, payload, frame=int(target["frame"])),
    )
    for sequence, record in enumerate(records):
        record["sequence"] = sequence


@pytest.mark.parametrize(
    ("event_type", "subject_object_id"),
    [
        ("production_queued", 10),
        ("upgrade_queued", 10),
        ("special_power_used", 10),
        ("supply_collected", 20),
        ("supply_collected", 40),
    ],
)
def test_reader_requires_live_current_subjects_for_task_events(
    tmp_path: Path,
    event_type: str,
    subject_object_id: int,
) -> None:
    trace = _valid_trace(tmp_path, "source.ndjson")
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()][:-1]
    _insert_before_event(records, event_type, "object_destroyed", _destroyed(subject_object_id))
    damaged = _finish(
        tmp_path / f"destroyed-{event_type}-{subject_object_id}.ndjson",
        records,
        [{"player_index": 0, "has_money": True, "balance": 4700}],
    )

    with pytest.raises(TelemetryTraceValidationError, match="after object_destroyed"):
        tuple(iter_validated_trace(damaged))


@pytest.mark.parametrize(
    ("event_type", "subject_object_id"),
    [
        ("production_queued", 10),
        ("production_completed", 10),
        ("upgrade_queued", 10),
        ("upgrade_cancelled", 10),
        ("special_power_used", 10),
        ("supply_collected", 20),
        ("supply_collected", 40),
    ],
)
def test_reader_requires_task_subject_owner_to_equal_event_player(
    tmp_path: Path,
    event_type: str,
    subject_object_id: int,
) -> None:
    trace = _valid_trace(tmp_path, "source.ndjson")
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()][:-1]
    players = next(record for record in records if record["event_type"] == "players_initialized")
    players["payload"]["engine_player_indices"] = [0, 4]
    _insert_before_event(records, event_type, "owner_changed", _owner_changed(subject_object_id, 4))
    damaged = _finish(
        tmp_path / f"wrong-owner-{event_type}-{subject_object_id}.ndjson",
        records,
        [
            {"player_index": 0, "has_money": True, "balance": 4700},
            {"player_index": 4, "has_money": False, "balance": None},
        ],
    )

    with pytest.raises(TelemetryTraceValidationError, match="current owner"):
        tuple(iter_validated_trace(damaged))


@pytest.mark.parametrize(
    ("queued_event", "queued_payload", "terminal_event", "terminal_payload", "terminal_frame"),
    [
        ("production_queued", _production("queued"), "production_completed", _production("completed"), 20),
        ("upgrade_queued", _upgrade("queued"), "upgrade_cancelled", _upgrade("cancelled"), 12),
    ],
)
def test_reader_preserves_queue_player_identity_across_producer_owner_transfer(
    tmp_path: Path,
    queued_event: str,
    queued_payload: dict[str, object],
    terminal_event: str,
    terminal_payload: dict[str, object],
    terminal_frame: int,
) -> None:
    records = _base_records(tmp_path)
    records[1]["payload"]["engine_player_indices"] = [0, 4]
    records.extend(
        [
            _record(6, queued_event, queued_payload, frame=int(queued_payload["queued_frame"])),
            _record(7, "owner_changed", _owner_changed(10, 4), frame=3),
            _record(8, terminal_event, terminal_payload, frame=terminal_frame),
        ]
    )
    trace = _finish(
        tmp_path / f"queue-transfer-{terminal_event}.ndjson",
        records,
        [
            {"player_index": 0, "has_money": False, "balance": None},
            {"player_index": 4, "has_money": False, "balance": None},
        ],
    )

    validated = tuple(iter_validated_trace(trace))
    terminal = next(record for record in validated if record.event_type == terminal_event)
    assert terminal.payload.player_index == 0


@pytest.mark.parametrize(
    ("queued_event", "queued_payload", "terminal_event", "terminal_payload", "terminal_frame"),
    [
        ("production_queued", _production("queued"), "production_completed", _production("completed"), 20),
        ("upgrade_queued", _upgrade("queued"), "upgrade_cancelled", _upgrade("cancelled"), 12),
    ],
)
def test_reader_rejects_terminal_queue_identity_drift_after_owner_transfer(
    tmp_path: Path,
    queued_event: str,
    queued_payload: dict[str, object],
    terminal_event: str,
    terminal_payload: dict[str, object],
    terminal_frame: int,
) -> None:
    records = _base_records(tmp_path)
    records[1]["payload"]["engine_player_indices"] = [0, 4]
    terminal_payload["player_index"] = 4
    records.extend(
        [
            _record(6, queued_event, queued_payload, frame=int(queued_payload["queued_frame"])),
            _record(7, "owner_changed", _owner_changed(10, 4), frame=3),
            _record(8, terminal_event, terminal_payload, frame=terminal_frame),
        ]
    )
    trace = _finish(
        tmp_path / f"queue-transfer-drift-{terminal_event}.ndjson",
        records,
        [
            {"player_index": 0, "has_money": False, "balance": None},
            {"player_index": 4, "has_money": False, "balance": None},
        ],
    )

    with pytest.raises(TelemetryTraceValidationError, match="identity changed"):
        tuple(iter_validated_trace(trace))


@pytest.mark.parametrize("event_type", ["production_queued", "special_power_used"])
def test_reader_requires_owned_producer_and_special_power_source(
    tmp_path: Path,
    event_type: str,
) -> None:
    trace = _valid_trace(tmp_path, "source.ndjson")
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()][:-1]
    _insert_before_event(records, event_type, "owner_changed", _owner_changed(10, None))
    damaged = _finish(
        tmp_path / f"unowned-{event_type}.ndjson",
        records,
        [{"player_index": 0, "has_money": True, "balance": 4700}],
    )

    with pytest.raises(TelemetryTraceValidationError, match="non-null current owner"):
        tuple(iter_validated_trace(damaged))


def test_reader_rejects_resolved_supply_source_equal_to_dropoff(tmp_path: Path) -> None:
    trace = _valid_trace(tmp_path, "source.ndjson")
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()][:-1]
    supply = next(record for record in records if record["event_type"] == "supply_collected")
    supply["payload"]["source_object_id"] = supply["payload"]["dropoff_object_id"]
    damaged = _finish(
        tmp_path / "same-supply-source-dropoff.ndjson",
        records,
        [{"player_index": 0, "has_money": True, "balance": 4700}],
    )

    with pytest.raises(TelemetryTraceValidationError, match="source_object_id must differ from dropoff_object_id"):
        tuple(iter_validated_trace(damaged))


@pytest.mark.parametrize(
    "damage",
    [
        "missing_cash",
        "intervening",
        "wrong_sequence",
        "wrong_frame",
        "wrong_player",
        "wrong_amount",
        "wrong_reason",
        "negative_delta",
        "zero_delta",
    ],
)
def test_reader_requires_atomic_supply_income_cash_pair(tmp_path: Path, damage: str) -> None:
    trace = _valid_trace(tmp_path, "source.ndjson")
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()][:-1]
    supply = next(record for record in records if record["event_type"] == "supply_collected")
    supply_index = records.index(supply)
    cash = records[supply_index + 1]
    balances = [{"player_index": 0, "has_money": True, "balance": 4700}]
    if damage == "missing_cash":
        records.pop(supply_index + 1)
        balances[0]["balance"] = 4100
    elif damage == "intervening":
        records.insert(
            supply_index + 1,
            _record(
                0,
                "special_power_used",
                {
                    "special_power_name": "SuperweaponSpySatellite",
                    "player_index": 0,
                    "source_object_id": 10,
                    "target_object_id": None,
                    "target_location": None,
                },
                frame=15,
            ),
        )
    elif damage == "wrong_frame":
        cash["frame"] = 16
        cash["logic_time_seconds"] = 16 / 30.0
    elif damage == "wrong_player":
        players = next(record for record in records if record["event_type"] == "players_initialized")
        players["payload"]["engine_player_indices"] = [0, 4]
        cash["payload"].update({"player_index": 4, "before": 0, "delta": 600, "after": 600})
        balances = [
            {"player_index": 0, "has_money": True, "balance": 4100},
            {"player_index": 4, "has_money": True, "balance": 600},
        ]
    elif damage == "wrong_amount":
        supply["payload"]["amount"] = 599
    elif damage == "wrong_reason":
        cash["payload"]["reason"] = "unknown"
    elif damage == "negative_delta":
        cash["payload"].update({"delta": -600, "after": 3500})
        balances[0]["balance"] = 3500
    elif damage == "zero_delta":
        cash["payload"].update({"delta": 0, "after": 4100})
        balances[0]["balance"] = 4100
    for sequence, record in enumerate(records):
        record["sequence"] = sequence
    if damage == "wrong_sequence":
        cash["sequence"] = int(supply["sequence"]) + 2
    damaged = _finish(tmp_path / f"bad-supply-pair-{damage}.ndjson", records, balances)

    with pytest.raises(TelemetryTraceValidationError, match="supply_collected cash pair"):
        tuple(iter_validated_trace(damaged))


def test_reader_rejects_orphan_supply_income_cash_event(tmp_path: Path) -> None:
    trace = _valid_trace(tmp_path, "source.ndjson")
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()][:-1]
    supply = next(record for record in records if record["event_type"] == "supply_collected")
    records.remove(supply)
    for sequence, record in enumerate(records):
        record["sequence"] = sequence
    damaged = _finish(
        tmp_path / "orphan-supply-income.ndjson",
        records,
        [{"player_index": 0, "has_money": True, "balance": 4700}],
    )

    with pytest.raises(TelemetryTraceValidationError, match="orphan supply_income"):
        tuple(iter_validated_trace(damaged))


def _supply_only_trace(
    directory: Path,
    *,
    before: int,
    amount: int,
    after: int,
    delta: int,
    track_income: bool = True,
    name: str = "supply-only.ndjson",
) -> Path:
    records = _base_records(directory)
    records.extend(
        [
            _record(
                6,
                "supply_collected",
                {
                    "collector_object_id": 20,
                    "source_object_id": 30,
                    "source_status": "resolved",
                    "dropoff_object_id": 40,
                    "player_index": 0,
                    "amount": amount,
                    "location": {"x": 40.0, "y": 2.0, "z": 0.0},
                },
                frame=15,
            ),
            _record(
                7,
                "cash_changed",
                {
                    "player_index": 0,
                    "before": before,
                    "delta": delta,
                    "after": after,
                    "track_income": track_income,
                    "reason": "supply_income",
                },
                frame=15,
            ),
        ]
    )
    return _finish(
        directory / name,
        records,
        [{"player_index": 0, "has_money": True, "balance": after}],
    )


def _cash_only_trace(
    directory: Path,
    *,
    before: int,
    after: int,
    delta: int,
    final_balance: int,
    name: str,
) -> Path:
    records = _base_records(directory)
    records.append(
        _record(
            6,
            "cash_changed",
            {
                "player_index": 0,
                "before": before,
                "delta": delta,
                "after": after,
                "track_income": False,
                "reason": "unknown",
            },
            frame=15,
        )
    )
    return _finish(
        directory / name,
        records,
        [{"player_index": 0, "has_money": True, "balance": final_balance}],
    )


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (
            lambda: CashChangedPayload(
                player_index=0,
                before=UINT32_MAX + 1,
                delta=-1,
                after=UINT32_MAX,
                track_income=False,
                reason="unknown",
            ),
            "before",
        ),
        (
            lambda: CashChangedPayload(
                player_index=0,
                before=UINT32_MAX,
                delta=1,
                after=UINT32_MAX + 1,
                track_income=False,
                reason="unknown",
            ),
            "after",
        ),
        (
            lambda: SupplyCollectedPayload(
                collector_object_id=20,
                source_object_id=30,
                source_status="resolved",
                dropoff_object_id=40,
                player_index=0,
                amount=UINT32_MAX + 1,
                location={"x": 40.0, "y": 2.0, "z": 0.0},
            ),
            "amount",
        ),
        (
            lambda: FinalCashBalance(
                player_index=0,
                has_money=True,
                balance=UINT32_MAX + 1,
            ),
            "balance",
        ),
    ],
)
def test_v2_models_reject_values_above_engine_uint32_range(
    factory: Callable[[], object],
    field: str,
) -> None:
    with pytest.raises(ValidationError, match=field):
        factory()


def test_v2_models_accept_exact_engine_uint32_boundaries() -> None:
    cash = CashChangedPayload(
        player_index=0,
        before=0,
        delta=UINT32_MAX,
        after=UINT32_MAX,
        track_income=False,
        reason="unknown",
    )
    supply = SupplyCollectedPayload(
        collector_object_id=20,
        source_object_id=30,
        source_status="resolved",
        dropoff_object_id=40,
        player_index=0,
        amount=UINT32_MAX,
        location={"x": 40.0, "y": 2.0, "z": 0.0},
    )

    assert (cash.before, cash.after) == (0, UINT32_MAX)
    assert supply.amount == UINT32_MAX
    assert FinalCashBalance(player_index=0, has_money=True, balance=0).balance == 0
    assert FinalCashBalance(player_index=0, has_money=True, balance=UINT32_MAX).balance == UINT32_MAX


@pytest.mark.parametrize("field", ["before", "after"])
def test_reader_rejects_cash_values_above_engine_uint32_range(tmp_path: Path, field: str) -> None:
    if field == "before":
        trace = _cash_only_trace(
            tmp_path,
            before=UINT32_MAX + 1,
            after=UINT32_MAX,
            delta=-1,
            final_balance=UINT32_MAX,
            name="cash-before-overflow.ndjson",
        )
    else:
        trace = _cash_only_trace(
            tmp_path,
            before=UINT32_MAX,
            after=UINT32_MAX + 1,
            delta=1,
            final_balance=UINT32_MAX + 1,
            name="cash-after-overflow.ndjson",
        )

    with pytest.raises(TelemetryTraceValidationError, match=rf"payload\.{field}.*maximum"):
        tuple(iter_validated_trace(trace))


def test_reader_rejects_supply_amount_above_engine_uint32_range(tmp_path: Path) -> None:
    trace = _supply_only_trace(
        tmp_path,
        before=0,
        amount=UINT32_MAX + 1,
        after=0,
        delta=0,
        name="supply-amount-overflow.ndjson",
    )

    with pytest.raises(TelemetryTraceValidationError, match=r"payload\.amount.*maximum"):
        tuple(iter_validated_trace(trace))


def test_reader_rejects_final_balance_above_engine_uint32_range(tmp_path: Path) -> None:
    records = _base_records(tmp_path)
    trace = _finish(
        tmp_path / "final-balance-overflow.ndjson",
        records,
        [{"player_index": 0, "has_money": True, "balance": UINT32_MAX + 1}],
    )

    with pytest.raises(TelemetryTraceValidationError, match=r"final_cash_balances.*balance.*maximum"):
        tuple(iter_validated_trace(trace))


def test_reader_rejects_out_of_range_modulo_congruent_supply_tuple(tmp_path: Path) -> None:
    trace = _supply_only_trace(
        tmp_path,
        before=UINT32_MAX + 1,
        amount=1,
        after=1,
        delta=-UINT32_MAX,
        name="impossible-modulo-congruent-supply.ndjson",
    )

    with pytest.raises(TelemetryTraceValidationError, match=r"payload\.before.*maximum"):
        tuple(iter_validated_trace(trace))


def test_reader_accepts_exact_engine_uint32_boundaries(tmp_path: Path) -> None:
    trace = _supply_only_trace(
        tmp_path,
        before=0,
        amount=UINT32_MAX,
        after=UINT32_MAX,
        delta=UINT32_MAX,
        name="supply-uint32-boundaries.ndjson",
    )

    records = tuple(iter_validated_trace(trace))
    cash = next(record for record in records if record.event_type == "cash_changed")
    assert (cash.payload.before, cash.payload.after) == (0, UINT32_MAX)
    assert records[-1].payload.final_cash_balances[0].balance == UINT32_MAX


def test_reader_requires_supply_income_pair_to_track_income(tmp_path: Path) -> None:
    trace = _supply_only_trace(
        tmp_path,
        before=100,
        amount=20,
        after=120,
        delta=20,
        track_income=False,
        name="supply-track-income-false.ndjson",
    )

    with pytest.raises(TelemetryTraceValidationError, match="supply_collected cash pair"):
        tuple(iter_validated_trace(trace))


def test_reader_accepts_unsigned_32_bit_supply_deposit_wraparound(tmp_path: Path) -> None:
    trace = _supply_only_trace(
        tmp_path,
        before=UINT32_MAX,
        amount=1,
        after=0,
        delta=-UINT32_MAX,
        name="supply-uint32-wrap.ndjson",
    )

    records = tuple(iter_validated_trace(trace))
    cash = next(record for record in records if record.event_type == "cash_changed")
    assert cash.payload.delta == -UINT32_MAX
    assert cash.payload.after == 0


@pytest.mark.parametrize(
    ("field", "before", "amount", "after", "delta"),
    [
        ("amount", 100, 19, 120, 20),
        ("before", 101, 20, 120, 19),
        ("after", 100, 20, 121, 21),
    ],
)
def test_reader_rejects_tampered_unsigned_supply_deposit_relation(
    tmp_path: Path,
    field: str,
    before: int,
    amount: int,
    after: int,
    delta: int,
) -> None:
    trace = _supply_only_trace(
        tmp_path,
        before=before,
        amount=amount,
        after=after,
        delta=delta,
        name=f"tampered-supply-{field}.ndjson",
    )

    with pytest.raises(TelemetryTraceValidationError, match="supply_collected cash pair"):
        tuple(iter_validated_trace(trace))


@pytest.mark.parametrize(
    ("event_type", "field", "value", "diagnostic"),
    [
        ("supply_collected", "amount", 0, "minimum"),
        ("supply_collected", "source_status", "invented", "source_status"),
        ("science_purchased", "points_after", 1, "science point transition"),
        ("science_purchased", "science_name", "SCIENCE_NotInCatalog", "science_name"),
        ("production_queued", "template_name", "NotInCatalog", "template_name"),
        ("upgrade_queued", "upgrade_name", "NotInCatalog", "upgrade_name"),
    ],
)
def test_reader_rejects_invalid_supply_science_and_catalog_identities(
    tmp_path: Path, event_type: str, field: str, value: object, diagnostic: str
) -> None:
    trace = _valid_trace(tmp_path, "source.ndjson")
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()][:-1]
    target = next(record for record in records if record["event_type"] == event_type)
    target["payload"][field] = value
    damaged = _finish(tmp_path / f"bad-{event_type}-{field}.ndjson", records, [{"player_index": 0, "has_money": True, "balance": 4700}])

    with pytest.raises(TelemetryTraceValidationError, match=diagnostic):
        next(iter_validated_trace(damaged))
