"""Compatibility and atomic-validation tests for the mandatory telemetry v2 catalog contract."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from generals_replay_analyzer.telemetry.reader import TelemetryTraceValidationError, iter_validated_trace

RUN_ID = "723e4567-e89b-12d3-a456-426614174000"
ENGINE_IDENTITY = "zero-hour-test-exe-00000000-ini-00000000"


def _record(version: int, sequence: int, event_type: str, payload: dict[str, object]) -> dict[str, object]:
    """Build a literal frame-zero envelope without production serialization helpers."""
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
    """Write exact compact NDJSON bytes used for the terminal digest."""
    path.write_bytes(
        b"".join(json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n" for record in records)
    )
    return path


def _completion(version: int, prior_records: list[dict[str, object]]) -> dict[str, object]:
    prior_bytes = b"".join(
        json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n" for record in prior_records
    )
    counts: dict[str, int] = {}
    for record in prior_records:
        event_type = str(record["event_type"])
        counts[event_type] = counts.get(event_type, 0) + 1
    counts["complete"] = 1
    return _record(
        version,
        len(prior_records),
        "complete",
        {
            "final_frame": 0,
            "command_count": 0,
            "event_counts": counts,
            "crc_mismatch": False,
            "replay_truncated": False,
            "clean_shutdown": True,
            "writer_error": None,
            "trace_sha256": hashlib.sha256(prior_bytes).hexdigest(),
            "map_assets": [],
        },
    )


def _catalog() -> dict[str, object]:
    return {
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


def _write_catalog_value(directory: Path, catalog: object) -> dict[str, object]:
    catalog_bytes = json.dumps(catalog, separators=(",", ":")).encode("utf-8") + b"\n"
    sha256 = hashlib.sha256(catalog_bytes).hexdigest()
    name = f"game-data-catalog-v1-{sha256}.json"
    (directory / name).write_bytes(catalog_bytes)
    return {
        "type": "game_data_catalog",
        "path": name,
        "sha256": sha256,
        "engine_data_identity": ENGINE_IDENTITY,
    }


def _write_catalog(directory: Path) -> dict[str, object]:
    return _write_catalog_value(directory, _catalog())


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
            "start_position": {"x": 1.0, "y": 2.0, "z": 3.0},
            "controller": "human",
            "is_human": True,
            "is_header_local_slot": True,
            "is_resolved_local_player": True,
        },
        {
            "slot_index": 1,
            "slot_state": "easy_ai",
            "occupied": True,
            "resolution_status": "unresolved",
            "replay_name": "Easy AI",
            "player_index": None,
            "team_id": -1,
            "faction_template_name": None,
            "color": None,
            "start_position_status": "unknown",
            "start_position": None,
            "controller": "ai",
            "is_human": False,
            "is_header_local_slot": False,
            "is_resolved_local_player": None,
        },
    ]
    for slot_index in range(2, 8):
        slots.append(
            {
                "slot_index": slot_index,
                "slot_state": "open" if slot_index % 2 == 0 else "closed",
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


def _write_v2_trace(
    directory: Path,
    reference: dict[str, object],
    *,
    player_payloads: list[dict[str, object]] | None = None,
    name: str = "trace.ndjson",
) -> Path:
    payloads = [_v2_players(reference)] if player_payloads is None else player_payloads
    records = [_record(2, 0, "manifest", _v2_manifest(reference))]
    records.extend(_record(2, index, "players_initialized", payload) for index, payload in enumerate(payloads, start=1))
    records.append(_completion(2, records))
    return _write_records(directory / name, records)


def test_reader_preserves_historical_v1_without_catalog_or_players(tmp_path: Path) -> None:
    """Catch a v2 migration that retroactively makes the original Task 1/2 v1 manifest invalid."""
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
        )
    ]
    records.append(_completion(1, records))

    assert [record.event_type for record in iter_validated_trace(_write_records(tmp_path / "v1.ndjson", records))] == [
        "manifest",
        "complete",
    ]


def test_reader_preserves_historical_v1_player_snapshot_without_manifest_catalog(tmp_path: Path) -> None:
    """Catch v2 cross-record binding being applied retroactively to the original v1 player payload."""
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
        _record(
            1,
            1,
            "players_initialized",
            {
                "players": [
                    {
                        "replay_name": "Historical Player",
                        "player_index": 0,
                        "team_id": 0,
                        "faction_template_name": "FactionAmerica",
                        "color": 0,
                        "is_human": True,
                        "is_local_player": True,
                    }
                ],
                "game_data_catalog": {"path": "historical-catalog.json", "sha256": "a" * 64},
            },
        ),
    ]
    records.append(_completion(1, records))

    validated = tuple(iter_validated_trace(_write_records(tmp_path / "v1-players.ndjson", records)))
    assert [record.event_type for record in validated] == ["manifest", "players_initialized", "complete"]


def test_reader_accepts_v2_only_with_a_catalog_and_one_complete_slot_snapshot(tmp_path: Path) -> None:
    """Catch a version selector that cannot consume the new mandatory v2 evidence contract."""
    reference = _write_catalog(tmp_path)
    records = [
        _record(2, 0, "manifest", _v2_manifest(reference)),
        _record(2, 1, "players_initialized", _v2_players(reference)),
    ]
    records.append(_completion(2, records))

    validated = tuple(iter_validated_trace(_write_records(tmp_path / "v2.ndjson", records)))
    assert [record.event_type for record in validated] == [
        "manifest",
        "players_initialized",
        "complete",
    ]
    slot_states = [slot.slot_state for slot in validated[1].payload.slots]
    assert slot_states[:3] == ["human", "easy_ai", "open"]
    assert len(slot_states) == 8


@pytest.mark.parametrize(
    ("mutate", "diagnostic"),
    [
        (lambda slots, payload: slots.reverse(), "slots must be ordered by slot_index 0 through 7"),
        (
            lambda slots, payload: slots[1].update(
                {
                    "resolution_status": "resolved",
                    "player_index": 0,
                    "faction_template_name": "FactionChina",
                    "is_resolved_local_player": False,
                }
            ),
            "player_index",
        ),
        (lambda slots, payload: slots[2].update({"occupied": True}), "unoccupied slot"),
        (lambda slots, payload: slots[0].update({"player_index": None}), "resolved slot"),
        (lambda slots, payload: payload.update({"header_local_slot_index": 3}), "header local slot"),
    ],
)
def test_v2_reader_rejects_ambiguous_or_inconsistent_slot_snapshots(
    tmp_path: Path,
    mutate: Callable[[list[dict[str, object]], dict[str, object]], object],
    diagnostic: str,
) -> None:
    """Catch slot omission, reordering, duplicate mappings, or contradictory resolution provenance."""
    reference = _write_catalog(tmp_path)
    payload = _v2_players(reference)
    slots = payload["slots"]
    assert isinstance(slots, list) and all(isinstance(slot, dict) for slot in slots)
    mutate(slots, payload)  # type: ignore[arg-type]
    trace = _write_v2_trace(tmp_path, reference, player_payloads=[payload], name="bad-slots.ndjson")

    with pytest.raises(TelemetryTraceValidationError, match=diagnostic):
        tuple(iter_validated_trace(trace))


@pytest.mark.parametrize(
    ("damage", "diagnostic"),
    [
        (lambda path: path.unlink(), "catalog asset does not exist"),
        (lambda path: path.write_bytes(b"corrupt\n"), "catalog sha256 does not match"),
        (lambda path: path.write_bytes(b"\xff\n"), "catalog sha256 does not match"),
    ],
)
def test_v2_reader_validates_catalog_bytes_before_exposing_manifest(
    tmp_path: Path, damage: Callable[[Path], object], diagnostic: str
) -> None:
    """Catch a buffered reader that exposes records before required asset existence and bytes are proven."""
    reference = _write_catalog(tmp_path)
    trace = _write_v2_trace(tmp_path, reference)
    damage(tmp_path / str(reference["path"]))

    with pytest.raises(TelemetryTraceValidationError, match=diagnostic):
        next(iter_validated_trace(trace))


@pytest.mark.parametrize("player_event_count", [0, 2])
def test_v2_reader_requires_exactly_one_players_initialized_event(tmp_path: Path, player_event_count: int) -> None:
    """Catch a completed v2 trace that omits or duplicates the authoritative slot snapshot."""
    reference = _write_catalog(tmp_path)
    payloads = [_v2_players(reference) for _ in range(player_event_count)]
    trace = _write_v2_trace(tmp_path, reference, player_payloads=payloads, name=f"players-{player_event_count}.ndjson")

    with pytest.raises(TelemetryTraceValidationError, match="exactly one players_initialized"):
        tuple(iter_validated_trace(trace))


@pytest.mark.parametrize(
    ("catalog", "diagnostic"),
    [
        ({"schema_version": 1}, "catalog schema"),
        ({**_catalog(), "engine_data_identity": "substituted-engine"}, "engine_data_identity"),
    ],
)
def test_v2_reader_rejects_schema_invalid_or_substituted_catalogs(
    tmp_path: Path, catalog: dict[str, object], diagnostic: str
) -> None:
    """Catch hash-valid bytes that are not the exact catalog contract bound by the manifest."""
    reference = _write_catalog_value(tmp_path, catalog)
    trace = _write_v2_trace(tmp_path, reference)

    with pytest.raises(TelemetryTraceValidationError, match=diagnostic):
        tuple(iter_validated_trace(trace))


@pytest.mark.parametrize("unsafe_path", ["../catalog.json", r"C:\catalog.json", "nested/catalog.json"])
def test_v2_reader_rejects_catalog_paths_outside_the_trace_directory(tmp_path: Path, unsafe_path: str) -> None:
    """Catch asset resolution that permits traversal, absolute paths, or hidden nested ownership."""
    reference = _write_catalog(tmp_path)
    reference["path"] = unsafe_path
    trace = _write_v2_trace(tmp_path, reference, name="unsafe.ndjson")

    with pytest.raises(TelemetryTraceValidationError, match="game_data_catalog|catalog path"):
        tuple(iter_validated_trace(trace))


def test_reader_rejects_nonstandard_nonfinite_json_constants_in_trace_and_catalog(tmp_path: Path) -> None:
    """Catch Python's permissive NaN parser accepting tokens that the C++ writer must never publish."""
    reference = _write_catalog(tmp_path)
    player_payload = _v2_players(reference)
    slots = player_payload["slots"]
    assert isinstance(slots, list) and isinstance(slots[0], dict)
    position = slots[0]["start_position"]
    assert isinstance(position, dict)
    position["x"] = float("nan")
    trace = _write_v2_trace(tmp_path, reference, player_payloads=[player_payload], name="nan-trace.ndjson")

    with pytest.raises(TelemetryTraceValidationError, match="non-standard numeric constant NaN"):
        tuple(iter_validated_trace(trace))

    catalog = _catalog()
    catalog["thing_templates"] = [
        {
            "ordinal": 0,
            "name": "Broken",
            "faction": None,
            "kind_of_flags": [],
            "build_cost": 0,
            "configured_build_time_seconds": float("inf"),
            "prerequisites": [],
            "locomotor_sets": [],
            "production_capable": False,
            "weapon_names": [],
            "category_tags": [],
        }
    ]
    bad_reference = _write_catalog_value(tmp_path, catalog)
    catalog_trace = _write_v2_trace(tmp_path, bad_reference, name="infinity-catalog.ndjson")

    with pytest.raises(TelemetryTraceValidationError, match="non-standard numeric constant Infinity"):
        tuple(iter_validated_trace(catalog_trace))
