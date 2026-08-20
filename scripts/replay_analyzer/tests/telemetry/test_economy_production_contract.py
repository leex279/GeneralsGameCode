"""Strict v2 economy, production, science, and special-power trace contracts."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from generals_replay_analyzer.telemetry.reader import TelemetryTraceValidationError, iter_validated_trace

RUN_ID = "823e4567-e89b-12d3-a456-426614174000"
ENGINE_IDENTITY = "zero-hour-economy-test-exe-00000000-ini-00000000"


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
    return {"header_local_slot_index": 0, "slots": slots, "game_data_catalog": reference}


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
        _record(2, "object_created", _object_created(10, "AmericaWarFactory")),
        _record(3, "object_created", _object_created(20, "AmericaVehicleChinook")),
        _record(4, "object_created", _object_created(30, "SupplyWarehouse")),
        _record(5, "object_created", _object_created(40, "AmericaSupplyCenter")),
    ]


def _finish(path: Path, records: list[dict[str, object]], balances: list[dict[str, object]]) -> Path:
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
                "crc_mismatch": False,
                "replay_truncated": False,
                "clean_shutdown": True,
                "writer_error": None,
                "trace_sha256": hashlib.sha256(prior).hexdigest(),
                "map_assets": [],
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
    assert events["supply_collected"].sequence < events["cash_changed"].sequence
    assert records[-1].payload.final_cash_balances[0].balance == 4700


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
