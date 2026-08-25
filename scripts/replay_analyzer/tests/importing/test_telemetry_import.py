"""Transactional normalization tests for validated telemetry and map observations."""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from map_asset_support import write_test_map_asset
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from telemetry.test_combat_outcome_contract import _valid_trace as _valid_combat_trace
from telemetry.test_economy_production_contract import _valid_trace as _valid_economy_trace

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import (
    CombatEvent,
    EconomyEvent,
    Entity,
    EntitySample,
    EvidenceItem,
    Job,
    ManagedAsset,
    Map,
    MapRegion,
    MapResource,
    ParserRun,
    Player,
    PlayerAlias,
    ProductionEvent,
    Replay,
    ReplayCommand,
    ReplayPlayer,
    ReplayQualityIssue,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.identity.service import PlayerIdentityService
from generals_replay_analyzer.importing import (
    AcquisitionDiagnostic,
    ImportRequest,
    ImportService,
    StageHandlerRegistration,
    TelemetryArtifact,
    TerminalDependencyPolicy,
)
from generals_replay_analyzer.importing import telemetry_import as telemetry_import_module
from generals_replay_analyzer.importing.evidence_identity import telemetry_event_evidence_identity
from generals_replay_analyzer.importing.identity_import import IdentityResolvingParserObservationImporter
from generals_replay_analyzer.importing.jobs import StageFailure
from generals_replay_analyzer.importing.map_import import normalize_map_asset
from generals_replay_analyzer.importing.parser_import import ParserImportResult, ParserObservationImporter
from generals_replay_analyzer.importing.service import StageDependencyOutput, StageExecutionContext
from generals_replay_analyzer.importing.stages import IMPORT_OBSERVATIONS_VERSION, canonical_json
from generals_replay_analyzer.importing.telemetry_import import (
    ManagedTelemetryArtifact,
    ObservationImportHandler,
    TelemetryAttempt,
    TelemetryImportResult,
    TelemetryObservationImporter,
    _normalized_record_json,
)
from generals_replay_analyzer.parser import ParsedReplay, parse_replay
from generals_replay_analyzer.storage import ContentAddressedStore, ContentStorageError, StoredContent
from generals_replay_analyzer.telemetry import load_validated_telemetry_bundle
from generals_replay_analyzer.telemetry.map_asset import BridgeFeature, Position3, WaypointFeature
from generals_replay_analyzer.telemetry.order_coverage import canonical_order_coverage

from .conftest import MutableClock

NOW = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
ENGINE_IDENTITY = "zero-hour-test-exe-00000000-ini-00000000"
PARSE_JOB_PUBLIC_ID = "00000000-0000-0000-0000-000000000101"
TELEMETRY_JOB_PUBLIC_ID = "00000000-0000-0000-0000-000000000102"


def _parse_dependency_output(replay_sha256: str = "a" * 64) -> dict[str, object]:
    return {
        "parser_version": "parser-v1",
        "content_sha256": replay_sha256,
        "completion_status": "complete",
        "command_count": 9,
        "warning_codes": (),
        "command_stream_offset": 1,
        "end_offset": 2,
    }


class DeterministicUUIDs:
    def __init__(self, start: int = 20_000) -> None:
        self._value = start

    def __call__(self) -> UUID:
        value = UUID(int=self._value)
        self._value += 1
        return value


def _identity_parser_importer(
    session_factory: sessionmaker[Session],
    data_root: Path,
    *,
    parser: Callable[[Path], ParsedReplay],
    parser_version: str,
    schema_version: int,
    clock: Callable[[], datetime],
    uuid_factory: Callable[[], UUID],
) -> IdentityResolvingParserObservationImporter:
    """Use the production identity boundary in observation-handler integration tests."""
    return IdentityResolvingParserObservationImporter(
        ParserObservationImporter(
            session_factory,
            data_root,
            parser=parser,
            parser_version=parser_version,
            schema_version=schema_version,
            clock=clock,
            uuid_factory=uuid_factory,
        ),
        PlayerIdentityService(session_factory, now_factory=clock),
    )


def _record(version: int, run_id: str, sequence: int, event_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": version,
        "run_id": run_id,
        "sequence": sequence,
        "frame": sequence,
        "logic_time_seconds": sequence / 30.0,
        "event_type": event_type,
        "payload": payload,
    }


def _completion(version: int, run_id: str, records: list[dict[str, object]], **terminal: object) -> dict[str, object]:
    prior = b"".join(json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records)
    counts: dict[str, int] = {}
    for record in records:
        event_type = str(record["event_type"])
        counts[event_type] = counts.get(event_type, 0) + 1
    counts["complete"] = 1
    payload: dict[str, object] = {
        "final_frame": len(records),
        "command_count": 0,
        "event_counts": counts,
        "terminal_reason": "clean_completion",
        "crc_mismatch": False,
        "replay_truncated": False,
        "clean_shutdown": True,
        "writer_error": None,
        "trace_sha256": hashlib.sha256(prior).hexdigest(),
        "map_assets": [],
    }
    payload.update(terminal)
    return _record(version, run_id, len(records), "complete", payload)


def _write_records(path: Path, records: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records))
    return path


def _write_catalog(directory: Path) -> dict[str, object]:
    payload = {
        "schema_version": 1,
        "type": "game_data_catalog",
        "engine_data_identity": ENGINE_IDENTITY,
        "weapon_scope": "referenced_by_thing_templates",
        "locomotor_scope": "referenced_by_thing_templates",
        "thing_templates": [
            {
                "ordinal": 0,
                "name": "SupplyDock",
                "faction": None,
                "kind_of_flags": [{"ordinal": 0, "name": "SUPPLY_SOURCE"}],
                "behavior_modules": [],
                "build_cost": 0,
                "configured_build_time_seconds": 0.0,
                "prerequisites": [],
                "locomotor_sets": [],
                "production_capable": False,
                "weapon_sets": [],
                "derived_weapon_names": [],
                "category_tags": [],
            }
        ],
        "upgrades": [],
        "sciences": [],
        "weapons": [],
        "locomotors": [],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
    sha256 = hashlib.sha256(raw).hexdigest()
    name = f"game-data-catalog-v1-{sha256}.json"
    (directory / name).write_bytes(raw)
    return {"type": "game_data_catalog", "path": name, "sha256": sha256, "engine_data_identity": ENGINE_IDENTITY}


def _write_v2_bundle(root: Path, run_id: str) -> Path:
    root.mkdir(parents=True)
    catalog = _write_catalog(root)
    map_reference = write_test_map_asset(
        root,
        ENGINE_IDENTITY,
        "maps/test.map",
        static_objects=[
            {
                "bounds_policy": "pathfinder_xy_closed",
                "categories": [
                    {"name": "supply_source", "source": "ThingTemplate::isKindOf(KINDOF_SUPPLY_SOURCE)"}
                ],
                "creation_source": "map_loaded",
                "object_id": 77,
                "orientation": 0.25,
                "position": {"x": 4.0, "y": 5.0, "z": 6.0},
                "snapshot_scope": "post_map_initialization",
                "template_name": "SupplyDock",
            }
        ],
    )
    manifest_payload = {
        "engine_build": ENGINE_IDENTITY,
        "replay_version": "1.04",
        "map_identity": "maps/test.map",
        "initial_seed": 7,
        "logic_frames_per_second": 30,
        "exporter_settings": {
            "movement_sample_frames": 15,
            "audio_enabled": False,
            "order_coverage": canonical_order_coverage(),
        },
        "game_data_catalog": catalog,
        "map_asset": map_reference,
    }
    players_payload = {
        "header_local_slot_index": 0,
        "slots": [
            {
                "slot_index": index,
                "slot_state": "human" if index == 0 else "closed",
                "occupied": index == 0,
                "resolution_status": "resolved" if index == 0 else "not_applicable",
                "replay_name": "Observed" if index == 0 else None,
                "player_index": 0 if index == 0 else None,
                "team_id": 0 if index == 0 else None,
                "faction_template_name": "FactionAmerica" if index == 0 else None,
                "color": 0 if index == 0 else None,
                "start_position_status": "resolved" if index == 0 else "not_applicable",
                "start_position": {"x": 1.0, "y": 2.0, "z": 3.0} if index == 0 else None,
                "controller": "human" if index == 0 else None,
                "is_human": index == 0,
                "is_header_local_slot": index == 0,
                "is_resolved_local_player": True if index == 0 else None,
            }
            for index in range(8)
        ],
        "engine_player_indices": [0],
        "game_data_catalog": catalog,
    }
    records = [
        _record(2, run_id, 0, "manifest", manifest_payload),
        _record(2, run_id, 1, "players_initialized", players_payload),
        _record(
            2,
            run_id,
            2,
            "object_created",
            {
                "object_id": 77,
                "template_name": "SupplyDock",
                "owner_player_index": None,
                "team_id": None,
                "position_status": "placed",
                "position": {"x": 4.0, "y": 5.0, "z": 6.0},
                "orientation": 0.25,
                "kind_of_flags": ["SUPPLY_SOURCE"],
                "initial_status": [],
                "creation_source": "map_loaded",
                "initialization_snapshot_status": "present",
                "creation_context": {
                    "registration_frame": 0,
                    "producer_object_id": None,
                    "producer_player_index": None,
                },
            },
        ),
        _record(
            2,
            run_id,
            3,
            "object_destroyed",
            {
                "object_id": 77,
                "previous_state": "alive",
                "new_state": "destroyed",
                "owner_player_index": None,
                "team_id": None,
                "destruction_source": "destroy_object",
            },
        ),
        _record(
            2,
            run_id,
            4,
            "match_outcome",
            {
                "status": "unknown",
                "source": "unavailable",
                "winner_player_indices": [],
                "loser_player_indices": [],
                "engine_player_indices": [0],
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
    for record in records[3:]:
        record["frame"] = 2
        record["logic_time_seconds"] = 2 / 30.0
    records.append(
        _completion(
            2,
            run_id,
            records,
            map_assets=[map_reference],
            crc_mismatch_frame=None,
            quit_early=False,
            replay_header_desync=False,
            replay_header_disconnected_slots=[],
            final_cash_balances=[{"player_index": 0, "has_money": True, "balance": 0}],
        )
    )
    records[-1]["frame"] = 2
    records[-1]["logic_time_seconds"] = 2 / 30.0
    complete_payload = records[-1]["payload"]
    assert isinstance(complete_payload, dict)
    complete_payload["final_frame"] = 2
    return _write_records(root / "trace.ndjson", records)


def _write_v1_bundle(root: Path, run_id: str, **terminal: object) -> Path:
    records = [
        _record(
            1,
            run_id,
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
            run_id,
            1,
            "object_created",
            {
                "object_id": 10,
                "template_name": "Tank",
                "owner_player_index": 2,
                "team_id": 3,
                "position": {"x": 1.0, "y": 2.0, "z": 3.0},
                "orientation": 0.5,
                "kind_of_flags": ["VEHICLE"],
                "creation_source": "runtime",
            },
        ),
        _record(
            1,
            run_id,
            2,
            "entity_sample",
            {
                "object_id": 10,
                "position": {"x": 2.0, "y": 3.0, "z": 4.0},
                "orientation": 0.75,
                "speed": 5.0,
                "current_state": "MOVING",
                "layer": 0,
                "sample_reason": "interval",
            },
        ),
        _record(
            1,
            run_id,
            3,
            "production_queued",
            {"production_id": 8, "producer_object_id": 10, "player_index": 2, "template_name": "Tank"},
        ),
        _record(
            1,
            run_id,
            4,
            "cash_changed",
            {"player_index": 2, "before": 1000, "delta": -100, "after": 900, "track_income": False, "reason": "build"},
        ),
        _record(
            1,
            run_id,
            5,
            "damage_applied",
            {
                "victim_object_id": 10,
                "attacker_object_id": 10,
                "weapon_name": "TestWeapon",
                "attempted_amount": 10.0,
                "applied_amount": 10.0,
                "prior_health": 100.0,
                "new_health": 90.0,
                "damage_type": "EXPLOSION",
                "death_type": "NORMAL",
                "location": {"x": 2.0, "y": 3.0, "z": 4.0},
                "killing_blow": False,
            },
        ),
    ]
    records.append(_completion(1, run_id, records, **terminal))
    return _write_records(root / "historical.ndjson", records)


def _write_v1_player_bundle(root: Path, run_id: str, *, complete_projection: bool = True) -> Path:
    """Build direct v1 family evidence with an exact replay-slot to engine-player mapping."""
    base = _write_v1_bundle(root, run_id)
    records = [json.loads(line) for line in base.read_text(encoding="utf-8").splitlines()]
    slots = [
        {
            "slot_index": index,
            "slot_state": "human" if index == 0 else "closed",
            "occupied": index == 0,
            "resolution_status": "resolved" if index == 0 else "not_applicable",
            "replay_name": "Observed" if index == 0 else None,
            "player_index": 2 if index == 0 else None,
            "team_id": 3 if index == 0 else None,
            "faction_template_name": "FactionAmerica" if index == 0 else None,
            "color": 0 if index == 0 else None,
            "start_position_status": "unknown" if index == 0 else "not_applicable",
            "start_position": None,
            "controller": "human" if index == 0 else None,
            "is_human": index == 0,
            "is_header_local_slot": index == 0,
            "is_resolved_local_player": True if index == 0 else None,
        }
        for index in range(8)
    ]
    players = _record(
        1,
        run_id,
        1,
        "players_initialized",
        {
            "header_local_slot_index": 0,
            "slots": slots,
            "engine_player_indices": [2],
            "game_data_catalog": {"path": "catalog.json", "sha256": "a" * 64},
        },
    )
    records = [records[0], players, *records[1:-1]]
    if complete_projection:
        sample_payload = records[3]["payload"]
        production_payload = records[4]["payload"]
        combat_payload = records[6]["payload"]
        assert isinstance(sample_payload, dict)
        assert isinstance(production_payload, dict)
        assert isinstance(combat_payload, dict)
        sample_payload["current_state_source"] = "direct_engine_state"
        production_payload.update({"quantity": 0, "state": "queued"})
        combat_payload.update({"source_player_index": 2, "victim_player_index": 2})
    for sequence, record in enumerate(records):
        record["sequence"] = sequence
        record["frame"] = sequence
        record["logic_time_seconds"] = sequence / 30.0
    records.append(_completion(1, run_id, records))
    return _write_records(base, records)


def _selected_parser_player(
    session_factory: sessionmaker[Session], replay_sha256: str, *, run_id: str, slot_count: int = 1
) -> tuple[str, tuple[str, ...]]:
    parser_run_id = str(UUID(run_id))
    player_public_ids = tuple(
        str(UUID(int=(UUID(run_id).int + 500 + slot_index) % (1 << 128)))
        for slot_index in range(slot_count)
    )
    with session_factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
        assert replay is not None
        parser_run = ParserRun(
            run_id=parser_run_id,
            replay_id=replay.id,
            parser_version="selected-parser",
            schema_version=1,
            input_sha256=replay_sha256,
            result_sha256="f" * 64,
            status="running",
            completion_status="complete",
            command_stream_offset=1,
            end_offset=2,
            warnings_json=[],
            error_json=None,
            started_at=NOW,
            completed_at=NOW,
        )
        session.add(parser_run)
        session.flush()
        session.add_all(
            [
                ReplayPlayer(
                public_id=player_public_ids[slot_index],
                replay_id=replay.id,
                parser_run_id=parser_run.id,
                player_id=None,
                slot_index=slot_index,
                slot_kind="human",
                original_name=f"Observed {slot_index}",
                normalized_name=None,
                player_index=None,
                team_id=slot_index,
                faction="FactionAmerica",
                color="0",
                start_position=None,
                result=None,
                observed_json={"index": slot_index, "kind": "human", "name": f"Observed {slot_index}"},
            )
                for slot_index in range(slot_count)
            ]
        )
        session.flush()
        parser_run.status = "succeeded"
    return parser_run_id, player_public_ids


def _replay(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings, identity_seed: str = "a" * 64
) -> str:
    source = settings.data_root / f"source-{identity_seed[:12]}.rep"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(f"managed replay {identity_seed}".encode())
    stored = ContentAddressedStore(settings.managed_replay_directory).store_file(source)
    sha256 = stored.sha256
    with session_factory.begin() as session:
        asset = ManagedAsset(
            public_id=str(UUID(int=int(sha256[:32], 16))),
            sha256=sha256,
            kind="replay",
            relative_path=stored.path.relative_to(settings.data_root).as_posix(),
            size_bytes=stored.size,
            media_type=None,
        )
        session.add(asset)
        session.flush()
        session.add(
            Replay(
                public_id=f"00000000-0000-0000-0000-{sha256[:12]}",
                sha256=sha256,
                managed_asset_id=asset.id,
                map_id=None,
                replay_name="fixture.rep",
                version_string="1.04",
                version_number=1,
                frame_count=0,
                start_time=0,
                end_time=0,
                exe_crc=0,
                ini_crc=0,
                map_crc=0,
                map_name="maps/test.map",
                seed=0,
                starting_cash=None,
                header_json={},
                lifecycle_state="parsed",
                updated_at=NOW,
            )
        )
    return sha256


def _attempt(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings, trace: Path, run_id: str
) -> TelemetryAttempt:
    files = [trace]
    if trace.name == "trace.ndjson":
        files.extend(path for path in trace.parent.rglob("*") if path.is_file() and path != trace)
    artifacts: list[ManagedTelemetryArtifact] = []
    with session_factory.begin() as session:
        for index, path in enumerate(sorted(files), start=1):
            raw = path.read_bytes()
            sha256 = hashlib.sha256(raw).hexdigest()
            logical_path = path.relative_to(trace.parent).as_posix()
            kind = "telemetry_trace"
            if path != trace:
                kind = "telemetry_catalog" if path.name.startswith("game-data-catalog") else "telemetry_map_asset"
            public_id = str(UUID(int=(UUID(run_id).int + index) % (1 << 128)))
            asset = ManagedAsset(
                public_id=public_id,
                sha256=sha256,
                kind=kind,
                relative_path=path.relative_to(settings.data_root).as_posix(),
                size_bytes=len(raw),
                media_type=None,
            )
            session.add(asset)
            artifacts.append(ManagedTelemetryArtifact(public_id, kind, logical_path, sha256, len(raw)))
    manifest_record = json.loads(trace.read_text(encoding="utf-8").splitlines()[0])
    return TelemetryAttempt(
        run_id=run_id,
        runner_status="success",
        replay_quality="complete",
        strategy_analysis_scope="full",
        process_exit_code=0,
        engine_build=manifest_record["payload"]["engine_build"],
        engine_executable_sha256="b" * 64,
        diagnostics=(),
        artifacts=tuple(artifacts),
    )


def _importer(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings, uuid_start: int = 20_000
) -> TelemetryObservationImporter:
    return TelemetryObservationImporter(
        session_factory,
        settings.data_root,
        clock=lambda: NOW,
        uuid_factory=DeterministicUUIDs(uuid_start),
    )


def _refresh_registered_artifact(
    session_factory: sessionmaker[Session], attempt: TelemetryAttempt, path: Path
) -> TelemetryAttempt:
    raw = path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    logical_path = next(
        artifact.logical_path for artifact in attempt.artifacts if artifact.logical_path.endswith(path.name)
    )
    refreshed: list[ManagedTelemetryArtifact] = []
    with session_factory.begin() as session:
        for descriptor in attempt.artifacts:
            if descriptor.logical_path == logical_path:
                asset = session.scalar(select(ManagedAsset).where(ManagedAsset.public_id == descriptor.asset_public_id))
                assert asset is not None
                asset.sha256 = sha256
                asset.size_bytes = len(raw)
                descriptor = replace(descriptor, sha256=sha256, size_bytes=len(raw))
            refreshed.append(descriptor)
    return replace(attempt, artifacts=tuple(refreshed))


def _run_diagnostics(session_factory: sessionmaker[Session], run_id: str) -> object:
    with session_factory() as session:
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        return None if run is None else run.diagnostics_json


def test_v2_outcome_normalization_does_not_reintroduce_legacy_fields(tmp_path: Path) -> None:
    """Catch validated v2 outcomes gaining deprecated null fields before persistence."""
    run_id = "123e4567-e89b-12d3-a456-426614174000"
    trace = _write_v2_bundle(tmp_path / run_id, run_id)
    record = next(
        item for item in load_validated_telemetry_bundle(trace).records if item.event_type == "match_outcome"
    )
    raw_record, payload = _normalized_record_json(record)
    assert "outcome" not in payload
    assert "winner_player_index" not in payload
    assert "outcome" not in raw_record["payload"]
    assert "winner_player_index" not in raw_record["payload"]


def test_v2_import_uses_validated_map_and_stable_raw_event_evidence(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch map inference, path identity leakage, or duplicate telemetry graphs."""
    replay_sha256 = _replay(session_factory, settings)
    with session_factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
        assert replay is not None
        replay.header_json = {
            "timebase": {
                "logic_frames_per_second": 60,
                "source": "replay_header_wall_clock",
                "observed_frames_per_second": 59.578,
            }
        }
    run_id = "123e4567-e89b-12d3-a456-426614174000"
    trace = _write_v2_bundle(settings.data_root / "runs" / run_id, run_id)
    assert len(load_validated_telemetry_bundle(trace).records) == 6
    attempt = _attempt(session_factory, settings, trace, run_id)
    importer = _importer(session_factory, settings)

    result = importer.import_replay(replay_sha256, attempt)
    cached = _importer(session_factory, settings, 80_000).import_replay(replay_sha256, attempt)
    with pytest.raises(ValueError, match="collides with another immutable attempt"):
        importer.import_replay(replay_sha256, replace(attempt, replay_quality="partial"))
    assert result.status == "succeeded" and result.event_count == 6, _run_diagnostics(session_factory, run_id)
    assert cached.run_id == run_id and cached.cache_hit is True
    with session_factory() as session:
        events = list(session.scalars(select(TelemetryEvent).order_by(TelemetryEvent.sequence)))
        map_row = session.scalar(select(Map))
        resources = list(session.scalars(select(MapResource).order_by(MapResource.stable_key)))
        assert [event.sequence for event in events] == [0, 1, 2, 3, 4, 5]
        assert events[0].raw_record_json["event_type"] == "manifest"
        event_evidence = list(
            session.execute(
                select(TelemetryEvent, EvidenceItem)
                .join(EvidenceItem, EvidenceItem.id == TelemetryEvent.evidence_item_id)
                .order_by(TelemetryEvent.sequence)
            )
        )
        assert len(event_evidence) == 6
        for event, item in event_evidence:
            expected = telemetry_event_evidence_identity(run_id, event.sequence)
            assert (item.public_id, item.source_kind, item.source_key) == (
                expected.public_id,
                expected.source_kind,
                expected.source_key,
            )
            assert UUID(item.public_id).version == 5
        assert map_row is not None and map_row.content_sha256 == events[0].payload_json["map_asset"]["content_sha256"]
        projection = map_row.metadata_json["validated_spatial_projection"]
        assert projection == {
            "amphibious_passable": [True, True, False, False],
            "content_sha256": map_row.content_sha256,
            "engine_data_identity": ENGINE_IDENTITY,
            "ground_passable": [True, False, False, False],
            "map_identity": "maps/test.map",
            "pathing": {
                "bounds": {
                    "maximum_exclusive": {"x": 1_000_000.0, "y": 1_000_000.0},
                    "minimum_inclusive": {"x": -1_000_000.0, "y": -1_000_000.0},
                },
                "cell_size": {"x": 1_000_000.0, "y": 1_000_000.0},
                "dimension_source": "synthetic contract fixture",
                "height": 2,
                "index_origin": {"x": -1, "y": -1},
                "sample_point": "cell_center",
                "storage_order": "row_major_y_then_x_x_fastest",
                "width": 2,
            },
            "schema_version": 2,
            "world_bounds": {
                "maximum": {"x": 1_000_000.0, "y": 1_000_000.0, "z": 10_000.0},
                "maximum_inclusive": True,
                "minimum": {"x": -1_000_000.0, "y": -1_000_000.0, "z": -10_000.0},
                "minimum_inclusive": True,
            },
            "zone_ids": [1, 2, 3, 4],
        }
        assert "start_positions" not in projection and "static_objects" not in projection
        assert "path" not in projection and "loader" not in projection
        assert [row.stable_key for row in resources] == ["start:0", "static:77"]
        assert resources[1].x == 4.0 and resources[1].payload_json["template_name"] == "SupplyDock"
        assert session.scalar(select(func.count(MapRegion.id))) == 0
        assert session.scalar(select(func.count(Player.id))) == 0
        assert session.scalar(select(func.count(PlayerAlias.id))) == 0
        replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
        assert replay is not None and replay.lifecycle_state == "engine_verified" and replay.map_id == map_row.id
        telemetry_run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert telemetry_run is not None
        assert telemetry_run.settings_json["logic_frames_per_second"] == 30
        assert replay.header_json["timebase"] == {
            "logic_frames_per_second": 30,
            "source": "engine_manifest",
            "observed_frames_per_second": 59.578,
            "parser_inferred_logic_frames_per_second": 60,
            "parser_inference_source": "replay_header_wall_clock",
        }


def test_v1_import_preserves_direct_entity_and_event_family_fields_without_map(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch v2-only fabrication or loss of historical direct event-family facts."""
    replay_sha256 = _replay(session_factory, settings, "c" * 64)
    run_id = "223e4567-e89b-12d3-a456-426614174000"
    trace = _write_v1_bundle(settings.data_root / "runs" / run_id, run_id)
    assert len(load_validated_telemetry_bundle(trace).records) == 7
    result = _importer(session_factory, settings).import_replay(
        replay_sha256, _attempt(session_factory, settings, trace, run_id)
    )
    assert result.status == "succeeded" and result.event_count == 7, _run_diagnostics(session_factory, run_id)
    with session_factory() as session:
        entity = session.scalar(select(Entity))
        sample = session.scalar(select(EntitySample))
        production = session.scalar(select(ProductionEvent))
        economy = session.scalar(select(EconomyEvent))
        combat = session.scalar(select(CombatEvent))
        assert entity is not None and entity.object_id == 10 and entity.initial_owner_player_index == 2
        assert sample is None
        assert production is None
        assert economy is not None and (economy.balance_before, economy.amount_delta, economy.balance_after) == (1000, -100, 900)
        assert combat is not None and combat.victim_entity_id == entity.id and combat.applied_amount == 10.0
        assert session.scalar(select(func.count(Map.id))) == 0
        issue = session.scalar(select(ReplayQualityIssue).where(ReplayQualityIssue.issue_code == "projection_unavailable"))
        assert issue is not None
        assert issue.details_json == {
            "projections": [
                {"event_type": "entity_sample", "missing_fields": ["current_state_source"], "sequence": 2},
                {"event_type": "production_queued", "missing_fields": ["quantity", "state"], "sequence": 3},
            ]
        }


def test_explicit_zero_layer_and_quantity_are_preserved_without_default_fabrication(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch falsey direct observations being replaced by symbolic/default values."""
    replay_sha256 = _replay(session_factory, settings, "explicit-zero")
    run_id = "228e4567-e89b-12d3-a456-426614174000"
    trace = _write_v1_bundle(settings.data_root / "runs" / run_id, run_id)
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    records[2]["payload"]["current_state_source"] = "direct_engine_state"
    records[3]["payload"].update({"quantity": 0, "state": "queued"})
    prior = b"".join(json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records[:-1])
    records[-1]["payload"]["trace_sha256"] = hashlib.sha256(prior).hexdigest()
    _write_records(trace, records)

    result = _importer(session_factory, settings).import_replay(
        replay_sha256,
        _attempt(session_factory, settings, trace, run_id),
    )

    assert result.status == "succeeded"
    with session_factory() as session:
        sample = session.scalar(select(EntitySample))
        production = session.scalar(select(ProductionEvent))
        assert sample is not None and sample.layer == "0" and sample.source == "direct_engine_state"
        assert production is not None and production.quantity == 0 and production.state == "queued"
        assert session.scalar(
            select(func.count(ReplayQualityIssue.id)).where(ReplayQualityIssue.issue_code == "projection_unavailable")
        ) == 0


@pytest.mark.parametrize("family", ["production_economy", "combat"])
def test_selected_parser_slot_mapping_links_all_direct_player_families_without_mutation(
    family: str, session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch provenance guesses, latest-run selection, or ReplayPlayer mutation replacing exact slot resolution."""
    replay_sha256 = _replay(session_factory, settings, f"resolved-player-{family}")
    run_id = "823e4567-e89b-12d3-a456-426614174000" if family == "production_economy" else "a23e4567-e89b-12d3-a456-426614174000"
    parser_run_id, player_public_ids = _selected_parser_player(
        session_factory,
        replay_sha256,
        run_id=run_id,
        slot_count=1 if family == "production_economy" else 2,
    )
    root = settings.data_root / "runs" / run_id
    root.mkdir(parents=True)
    if family == "production_economy":
        trace = _valid_economy_trace(root, "trace.ndjson")
    else:
        produced = _valid_combat_trace(root)
        trace = produced.rename(root / "trace.ndjson")
    assert load_validated_telemetry_bundle(trace).manifest.schema_version == 2
    attempt = replace(
        _attempt(session_factory, settings, trace, run_id),
        parser_run_id=parser_run_id,
    )

    result = _importer(session_factory, settings).import_replay(replay_sha256, attempt)

    assert result.status == "succeeded", _run_diagnostics(session_factory, run_id)
    with session_factory() as session:
        players = list(
            session.scalars(select(ReplayPlayer).where(ReplayPlayer.public_id.in_(player_public_ids)).order_by(ReplayPlayer.slot_index))
        )
        assert len(players) == len(player_public_ids)
        assert all(player.player_index is None and player.player_id is None for player in players)
        if family == "production_economy":
            production = session.scalar(select(ProductionEvent).where(ProductionEvent.event_type == "production_queued"))
            economy = session.scalar(select(EconomyEvent))
            assert production is not None and production.replay_player_id == players[0].id
            assert economy is not None and economy.replay_player_id == players[0].id
        else:
            combat = session.scalar(select(CombatEvent).where(CombatEvent.event_type == "damage_applied"))
            assert combat is not None
            assert (combat.attacker_replay_player_id, combat.victim_replay_player_id) == (
                players[0].id,
                players[1].id,
            )


def test_selected_parser_mapping_rejects_unknown_or_nonmatching_run(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch silently falling back to the latest parser run when the selected dependency is unavailable."""
    replay_sha256 = _replay(session_factory, settings, "unknown-parser-run")
    run_id = "823e4567-e89b-12d3-a456-426614174000"
    _selected_parser_player(session_factory, replay_sha256, run_id=run_id)
    root = settings.data_root / "runs" / run_id
    root.mkdir(parents=True)
    trace = _valid_economy_trace(root, "trace.ndjson")
    attempt = replace(
        _attempt(session_factory, settings, trace, run_id),
        parser_run_id="00000000-0000-0000-0000-000000000099",
    )

    result = _importer(session_factory, settings).import_replay(replay_sha256, attempt)

    assert result.status == "failed" and result.event_count == 0
    with session_factory() as session:
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert run is not None and run.status == "failed"
        assert set(
            session.scalars(
                select(ReplayQualityIssue.issue_code).where(ReplayQualityIssue.telemetry_run_id == run.id)
            )
        ) == {"invalid_trace"}
        for model in (TelemetryEvent, Entity, EntitySample, ProductionEvent, EconomyEvent, CombatEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_selected_parser_mapping_is_exact_and_rejects_duplicate_resolved_slots(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch two telemetry players ambiguously resolving through the same selected parser slot."""
    replay_sha256 = _replay(session_factory, settings, "direct-slot-mapping")
    parser_run_id, player_public_ids = _selected_parser_player(
        session_factory,
        replay_sha256,
        run_id="243e4567-e89b-12d3-a456-426614174000",
        slot_count=2,
    )
    importer = _importer(session_factory, settings)
    payload = {
        "slots": [
            {"resolution_status": "resolved", "slot_index": 1, "player_index": 7},
            {"resolution_status": "resolved", "slot_index": 0, "player_index": 3},
        ]
    }
    with session_factory() as session:
        replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
        assert replay is not None
        resolved = importer._parser_player_map(session, replay.id, parser_run_id, (payload,))
        assert {index: player.public_id for index, player in resolved.items()} == {
            3: player_public_ids[0],
            7: player_public_ids[1],
        }
        duplicate_slot = {
            "slots": [
                {"resolution_status": "resolved", "slot_index": 0, "player_index": 3},
                {"resolution_status": "resolved", "slot_index": 0, "player_index": 7},
            ]
        }
        with pytest.raises(ValueError, match="ambiguous"):
            importer._parser_player_map(session, replay.id, parser_run_id, (duplicate_slot,))


def test_successful_uuid_cache_distinguishes_exact_nullable_engine_build(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch DB display sentinels replacing nullable immutable runner evidence in cache identity."""
    replay_sha256 = _replay(session_factory, settings, "nullable-engine-build")
    run_id = "253e4567-e89b-12d3-a456-426614174000"
    trace = _write_v1_bundle(settings.data_root / "runs" / run_id, run_id)
    attempt = replace(_attempt(session_factory, settings, trace, run_id), engine_build=None)
    importer = _importer(session_factory, settings)

    first = importer.import_replay(replay_sha256, attempt, idempotency_key="stable")
    cached = importer.import_replay(replay_sha256, attempt, idempotency_key="stable")

    assert first.status == cached.status == "succeeded" and cached.cache_hit is True
    with pytest.raises(ValueError, match="collides with another immutable attempt"):
        importer.import_replay(
            replay_sha256,
            replace(attempt, engine_build="historical-build"),
            idempotency_key="stable",
        )
    with session_factory() as session:
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert run is not None and run.settings_json["attempt_engine_build"] is None


@pytest.mark.parametrize(
    "field",
    [
        "runner_status",
        "replay_quality",
        "strategy_analysis_scope",
        "process_exit_code",
        "engine_build",
        "engine_executable_sha256",
        "diagnostics",
        "artifacts",
        "parser_run_id",
    ],
)
def test_successful_uuid_collision_matrix_compares_every_immutable_attempt_field(
    field: str, session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch any immutable runner/manifest field being omitted from same-UUID collision identity."""
    replay_sha256 = _replay(session_factory, settings, f"success-collision-{field}")
    run_id = str(UUID(int=25_200 + len(field)))
    trace = _write_v1_bundle(settings.data_root / "success-collision" / run_id, run_id)
    attempt = _attempt(session_factory, settings, trace, run_id)
    importer = _importer(session_factory, settings)
    assert importer.import_replay(replay_sha256, attempt, idempotency_key="stable").status == "succeeded"
    changes: dict[str, object] = {
        "runner_status": "changed",
        "replay_quality": "partial",
        "strategy_analysis_scope": "limited",
        "process_exit_code": 1,
        "engine_build": "changed-build",
        "engine_executable_sha256": "c" * 64,
        "diagnostics": ({"code": "changed", "message": "changed"},),
        "artifacts": (replace(attempt.artifacts[0], size_bytes=attempt.artifacts[0].size_bytes + 1),),
        "parser_run_id": "00000000-0000-0000-0000-000000000099",
    }
    with pytest.raises(ValueError, match="collides with another immutable attempt"):
        importer.import_replay(
            replay_sha256,
            replace(attempt, **{field: changes[field]}),
            idempotency_key="stable",
        )


@pytest.mark.parametrize("tamper", ["public_id", "source_kind", "source_key"])
def test_successful_telemetry_cache_rejects_observed_evidence_identity_drift(
    tamper: str,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
) -> None:
    """Catch same-run reuse accepting an event citation that no longer matches its sequence locator."""
    replay_sha256 = _replay(session_factory, settings, f"telemetry-evidence-drift-{tamper}")
    run_id = str(UUID(int=25_400 + len(tamper)))
    trace = _write_v1_bundle(settings.data_root / "evidence-drift" / run_id, run_id)
    attempt = _attempt(session_factory, settings, trace, run_id)
    canonical_identities = telemetry_import_module.telemetry_event_evidence_identities

    def poisoned_identities(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        identities = list(canonical_identities(*args, **kwargs))  # type: ignore[arg-type]
        identity = identities[0]
        if tamper == "public_id":
            identities[0] = replace(identity, public_id="00000000-0000-0000-0000-00000000ffff")
        elif tamper == "source_kind":
            identities[0] = replace(identity, source_kind="parser_command")
        else:
            identities[0] = replace(identity, source_key=f"{identity.source_key}:drift")
        return tuple(identities)

    monkeypatch.setattr(telemetry_import_module, "telemetry_event_evidence_identities", poisoned_identities)
    importer = _importer(session_factory, settings)
    assert importer.import_replay(replay_sha256, attempt).status == "succeeded"
    monkeypatch.setattr(telemetry_import_module, "telemetry_event_evidence_identities", canonical_identities)

    with pytest.raises(ValueError, match="collides with another immutable attempt"):
        _importer(session_factory, settings, 90_000).import_replay(replay_sha256, attempt)


@pytest.mark.parametrize("collision", ["public_id", "source_key"])
def test_telemetry_evidence_locator_collision_fails_without_overwriting_existing_citation(
    collision: str,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
) -> None:
    """Catch one frozen telemetry locator being silently rebound to a foreign evidence row."""
    replay_sha256 = _replay(session_factory, settings, f"telemetry-evidence-collision-{collision}")
    run_id = str(UUID(int=25_450 + len(collision)))
    trace = _write_v1_bundle(settings.data_root / "evidence-collision" / run_id, run_id)
    attempt = _attempt(session_factory, settings, trace, run_id)
    expected = telemetry_event_evidence_identity(run_id, 0)
    with session_factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
        assert replay is not None
        session.add(
            EvidenceItem(
                public_id=(
                    expected.public_id
                    if collision == "public_id"
                    else "00000000-0000-0000-0000-00000000fffe"
                ),
                replay_id=replay.id,
                parser_run_id=None,
                telemetry_run_id=None,
                tier="observed",
                source_kind="telemetry_event",
                source_key=(expected.source_key if collision == "source_key" else "foreign-telemetry-citation"),
                schema_version=1,
                created_at=NOW,
            )
        )

    result = _importer(session_factory, settings).import_replay(replay_sha256, attempt)
    assert result.status == "failed" and result.event_count == 0
    with session_factory() as session:
        poison = session.scalar(
            select(EvidenceItem).where(
                (EvidenceItem.public_id == expected.public_id)
                if collision == "public_id"
                else (EvidenceItem.source_key == expected.source_key)
            )
        )
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == result.run_id))
        assert poison is not None and run is not None and run.status == "failed"
        assert session.scalar(select(func.count(TelemetryEvent.id)).where(TelemetryEvent.telemetry_run_id == run.id)) == 0


@pytest.mark.parametrize(
    "tamper",
    ["asset_public_id", "asset_kind", "asset_size", "asset_bytes", "run_link", "trace_sha", "nullable_engine"],
)
def test_failed_uuid_cache_revalidates_full_retained_evidence_before_reuse(
    tamper: str, session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch crash-reuse accepting a changed managed asset, link, trace fact, or nullable runner fact."""
    replay_sha256 = _replay(session_factory, settings, f"failed-cache-{tamper}")
    run_id = str(UUID(int=25_500 + len(tamper)))
    trace = _write_v1_bundle(settings.data_root / "failed-cache" / run_id, run_id)
    attempt = replace(
        _attempt(session_factory, settings, trace, run_id),
        engine_build=None,
        upstream_failure_code="invalid_telemetry_artifact",
        upstream_quality_issue_code="invalid_trace",
        upstream_failure_message="telemetry artifact validation failed",
    )
    importer = _importer(session_factory, settings)
    first = importer.import_replay(replay_sha256, attempt, idempotency_key="stable-failed-cache")
    assert first.status == "failed"
    if tamper == "nullable_engine":
        changed_attempt = replace(attempt, engine_build="unavailable")
    else:
        changed_attempt = attempt
        descriptor = attempt.artifacts[0]
        with session_factory.begin() as session:
            run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
            asset = session.scalar(select(ManagedAsset).where(ManagedAsset.public_id == descriptor.asset_public_id))
            assert run is not None and asset is not None
            managed = settings.data_root / Path(*asset.relative_path.split("/"))
            if tamper == "asset_public_id":
                asset.public_id = "00000000-0000-0000-0000-00000000ffff"
            elif tamper == "asset_kind":
                asset.kind = "telemetry_stdout"
            elif tamper == "asset_size":
                asset.size_bytes += 1
            elif tamper == "asset_bytes":
                managed.write_bytes(managed.read_bytes() + b"tampered")
            elif tamper == "run_link":
                run.trace_asset_id = None
            else:
                run.trace_sha256 = "0" * 64

    with pytest.raises(ValueError, match="collides with another immutable attempt"):
        importer.import_replay(
            replay_sha256,
            changed_attempt,
            idempotency_key="stable-failed-cache",
        )


@pytest.mark.parametrize("tamper", ["replay_bytes", "asset_bytes", "asset_registration"])
def test_final_success_transaction_reverifies_replay_and_every_managed_descriptor(
    tamper: str,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
) -> None:
    """Catch replay or descriptor swaps between validated loading and final child insertion."""
    replay_sha256 = _replay(session_factory, settings, f"final-tamper-{tamper}")
    run_id = str(UUID(int=26_000 + len(tamper)))
    trace = _write_v2_bundle(settings.data_root / "runs" / run_id, run_id)
    attempt = _attempt(session_factory, settings, trace, run_id)
    importer = _importer(session_factory, settings)
    original = importer._load_normalized_bundle

    def tampering_load(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        normalized = original(*args, **kwargs)  # type: ignore[arg-type]
        with session_factory.begin() as session:
            if tamper == "replay_bytes":
                replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
                assert replay is not None and replay.managed_asset_id is not None
                asset = session.get(ManagedAsset, replay.managed_asset_id)
                assert asset is not None
                (settings.data_root / Path(*asset.relative_path.split("/"))).write_bytes(b"swapped replay")
            else:
                descriptor = next(item for item in attempt.artifacts if item.kind == "telemetry_map_asset")
                asset = session.scalar(select(ManagedAsset).where(ManagedAsset.public_id == descriptor.asset_public_id))
                assert asset is not None
                if tamper == "asset_registration":
                    asset.kind = "telemetry_catalog"
                else:
                    (settings.data_root / Path(*asset.relative_path.split("/"))).write_bytes(b"swapped asset")
        return normalized

    monkeypatch.setattr(importer, "_load_normalized_bundle", tampering_load)

    result = importer.import_replay(replay_sha256, attempt)

    assert result.status == "failed" and result.event_count == 0
    with session_factory() as session:
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert run is not None and run.status == "failed"
        for model in (TelemetryEvent, Entity, EntitySample, ProductionEvent, EconomyEvent, CombatEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_final_reverify_allows_exact_same_kind_asset_reuse_but_rejects_descriptor_mismatch(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch content-addressed map-member dedup being mistaken for a duplicate logical descriptor."""
    managed = settings.data_root / "managed-artifacts" / "shared-map-member.bin"
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_bytes(b"same validated compressed member bytes")
    sha256 = hashlib.sha256(managed.read_bytes()).hexdigest()
    public_id = "00000000-0000-0000-0000-000000000321"
    with session_factory.begin() as session:
        session.add(
            ManagedAsset(
                public_id=public_id,
                sha256=sha256,
                kind="telemetry_map_asset",
                relative_path=managed.relative_to(settings.data_root).as_posix(),
                size_bytes=managed.stat().st_size,
                media_type=None,
            )
        )
    descriptors = tuple(
        ManagedTelemetryArtifact(
            public_id,
            "telemetry_map_asset",
            f"map-assets-v2/{'a' * 64}/{name}",
            sha256,
            managed.stat().st_size,
        )
        for name in ("pathing-amphibious.u8.zlib", "pathing-ground.u8.zlib")
    )
    importer = _importer(session_factory, settings)

    with session_factory() as session:
        verified = importer._reverify_artifacts(session, descriptors)
        assert [item.asset_id for item in verified] == [verified[0].asset_id, verified[0].asset_id]
        for mismatched in (
            replace(descriptors[1], kind="telemetry_catalog"),
            replace(descriptors[1], sha256="0" * 64),
            replace(descriptors[1], size_bytes=descriptors[1].size_bytes + 1),
        ):
            with pytest.raises(ValueError, match="registration changed"):
                importer._reverify_artifacts(session, (descriptors[0], mismatched))


def test_forced_family_constraint_failure_rolls_back_raw_and_projection_graph(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
) -> None:
    """Catch a late DB uniqueness failure leaving earlier raw telemetry children committed."""
    replay_sha256 = _replay(session_factory, settings, "forced-family-constraint")
    run_id = "263e4567-e89b-12d3-a456-426614174000"
    trace = _write_v1_player_bundle(settings.data_root / "runs" / run_id, run_id)
    attempt = _attempt(session_factory, settings, trace, run_id)
    importer = _importer(session_factory, settings)
    original = importer._add_economy

    def duplicate_projection(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)  # type: ignore[arg-type]
        original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importer, "_add_economy", duplicate_projection)
    result = importer.import_replay(replay_sha256, attempt)

    assert result.status == "failed" and result.event_count == 0
    with session_factory() as session:
        for model in (TelemetryEvent, Entity, EntitySample, ProductionEvent, EconomyEvent, CombatEvent, EvidenceItem):
            assert session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize(
    ("quality_code", "runner_status", "expected_codes", "expected_lifecycle"),
    [
        ("invalid_trace", "timeout", {"invalid_trace", "exporter_failure"}, "parsed"),
        ("asset_invalid", "success", {"asset_invalid"}, "parsed"),
        ("invalid_catalog", "success", {"invalid_catalog"}, "parsed"),
        ("invalid_map_asset", "success", {"invalid_map_asset"}, "parsed"),
        ("missing_telemetry", "success", {"missing_telemetry"}, "parsed"),
        ("version_mismatch", "success", {"version_mismatch"}, "unsupported"),
    ],
)
def test_failure_quality_mapping_and_lifecycle_precedence_are_independent(
    quality_code: str,
    runner_status: str,
    expected_codes: set[str],
    expected_lifecycle: str,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
) -> None:
    """Catch terminal runner evidence being collapsed into one quality or lifecycle shortcut."""
    replay_sha256 = _replay(session_factory, settings, f"quality-{quality_code}-{runner_status}")
    run_id = str(UUID(int=27_000 + sum(ord(character) for character in quality_code + runner_status)))
    trace = _write_v1_bundle(settings.data_root / "runs" / run_id, run_id)
    attempt = replace(
        _attempt(session_factory, settings, trace, run_id),
        runner_status=runner_status,
        upstream_failure_code="terminal_evidence_failure",
        upstream_quality_issue_code=quality_code,
        upstream_failure_message="telemetry evidence is unavailable",
    )

    result = _importer(session_factory, settings).import_replay(replay_sha256, attempt)

    assert result.status == "failed"
    with session_factory() as session:
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
        assert run is not None and run.runner_status == runner_status
        assert replay is not None and replay.lifecycle_state == expected_lifecycle
        assert set(
            session.scalars(
                select(ReplayQualityIssue.issue_code).where(ReplayQualityIssue.telemetry_run_id == run.id)
            )
        ) == expected_codes


def test_invalid_managed_trace_fails_atomically_while_retaining_attempt_and_assets(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch validation failures that leak even one telemetry observation child."""
    replay_sha256 = _replay(session_factory, settings, "d" * 64)
    run_id = "323e4567-e89b-12d3-a456-426614174000"
    trace = _write_v1_bundle(settings.data_root / "runs" / run_id, run_id)
    attempt = _attempt(session_factory, settings, trace, run_id)
    trace.write_bytes(trace.read_bytes() + b"corrupt")

    importer = _importer(session_factory, settings)
    result = importer.import_replay(
        replay_sha256,
        attempt,
        idempotency_key="import_observations:1:invalid-trace",
    )
    assert result.status == "failed" and result.event_count == 0
    with pytest.raises(ValueError, match="collides with another immutable attempt"):
        importer.import_replay(
            replay_sha256,
            attempt,
            idempotency_key="import_observations:1:invalid-trace",
        )
    with pytest.raises(ValueError, match="collides with another immutable attempt"):
        importer.import_replay(
            replay_sha256,
            attempt,
            idempotency_key="import_observations:1:changed-key",
        )
    with session_factory() as session:
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert run is not None and run.status == "failed" and run.trace_asset_id is not None
        assert session.scalar(select(func.count(TelemetryEvent.id)).where(TelemetryEvent.telemetry_run_id == run.id)) == 0
        assert session.scalar(select(func.count(Entity.id)).where(Entity.telemetry_run_id == run.id)) == 0
        issue = session.scalar(select(ReplayQualityIssue).where(ReplayQualityIssue.telemetry_run_id == run.id))
        assert issue is not None and issue.issue_code == "asset_invalid"


def test_runner_failure_without_trace_retains_metadata_without_observations(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch a runner failure being discarded merely because no trace was produced."""
    replay_sha256 = _replay(session_factory, settings, "1" * 64)
    run_id = "623e4567-e89b-12d3-a456-426614174000"
    attempt = TelemetryAttempt(
        run_id=run_id,
        runner_status="invalid_trace",
        replay_quality="failed",
        strategy_analysis_scope="unavailable",
        process_exit_code=2,
        engine_build=None,
        engine_executable_sha256=None,
        diagnostics=({"code": "trace_missing"},),
        artifacts=(),
    )

    result = _importer(session_factory, settings).import_replay(replay_sha256, attempt)

    assert result.status == "failed" and result.event_count == 0
    with session_factory() as session:
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert run is not None and run.status == "failed" and run.trace_asset_id is None
        assert run.runner_status == "invalid_trace" and run.process_exit_code == 2
        assert session.scalar(select(func.count(TelemetryEvent.id)).where(TelemetryEvent.telemetry_run_id == run.id)) == 0
        issue = session.scalar(select(ReplayQualityIssue).where(ReplayQualityIssue.telemetry_run_id == run.id))
        assert issue is not None and issue.issue_code == "exporter_failure"


def test_typed_failed_attempt_rejects_unregistered_retained_asset_before_run_shell(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch a tampered failure envelope persisting or linking an unregistered managed asset ID."""
    replay_sha256 = _replay(session_factory, settings, "8" * 64)
    attempt = TelemetryAttempt(
        run_id="683e4567-e89b-12d3-a456-426614174000",
        runner_status="success",
        replay_quality="complete",
        strategy_analysis_scope="full",
        process_exit_code=0,
        engine_build=ENGINE_IDENTITY,
        engine_executable_sha256="c" * 64,
        diagnostics=(),
        artifacts=(
            ManagedTelemetryArtifact(
                "693e4567-e89b-12d3-a456-426614174000",
                "telemetry_stdout",
                "stdout.log",
                "d" * 64,
                4,
            ),
        ),
        upstream_failure_code="artifact_copy_failed",
        upstream_quality_issue_code="invalid_trace",
        upstream_failure_message="telemetry artifact copy failed",
    )

    with pytest.raises(ValueError, match="unregistered"):
        _importer(session_factory, settings).import_replay(
            replay_sha256,
            attempt,
            idempotency_key="import_observations:1:tampered-retained-asset",
        )
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(TelemetryRun)) == 0


@pytest.mark.parametrize("tamper", ["registration", "missing", "directory", "content"])
def test_typed_failed_attempt_revalidates_every_retained_managed_asset(
    tamper: str,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
) -> None:
    """Catch a retained failure link surviving registration, path, or byte-identity tampering."""
    replay_sha256 = _replay(session_factory, settings, f"{9 + len(tamper):x}"[-1] * 64)
    run_id = str(UUID(int=70_000 + len(tamper)))
    trace = _write_v1_bundle(settings.data_root / "runs" / run_id, run_id)
    attempt = replace(
        _attempt(session_factory, settings, trace, run_id),
        upstream_failure_code="artifact_copy_failed",
        upstream_quality_issue_code="invalid_trace",
        upstream_failure_message="telemetry artifact copy failed",
    )
    descriptor = next(item for item in attempt.artifacts if item.kind == "telemetry_trace")
    with session_factory.begin() as session:
        asset = session.scalar(select(ManagedAsset).where(ManagedAsset.public_id == descriptor.asset_public_id))
        assert asset is not None
        managed_path = settings.data_root / Path(*asset.relative_path.split("/"))
        if tamper == "registration":
            asset.kind = "telemetry_catalog"
        elif tamper == "missing":
            managed_path.unlink()
        elif tamper == "directory":
            directory = settings.data_root / "retained-directory"
            directory.mkdir()
            asset.relative_path = directory.relative_to(settings.data_root).as_posix()
        else:
            managed_path.write_bytes(managed_path.read_bytes() + b"tampered")

    with pytest.raises(ValueError):
        _importer(session_factory, settings).import_replay(
            replay_sha256,
            attempt,
            idempotency_key=f"import_observations:1:retained-{tamper}",
        )
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(TelemetryRun)) == 0


def test_registered_descriptor_mismatch_retains_failed_shell_and_registered_asset(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch descriptor validation happening before durable failed-attempt creation."""
    replay_sha256 = _replay(session_factory, settings, "2" * 64)
    run_id = "723e4567-e89b-12d3-a456-426614174000"
    trace = _write_v1_bundle(settings.data_root / "runs" / run_id, run_id)
    attempt = _attempt(session_factory, settings, trace, run_id)
    trace_descriptor = next(artifact for artifact in attempt.artifacts if artifact.kind == "telemetry_trace")
    with session_factory.begin() as session:
        asset = session.scalar(select(ManagedAsset).where(ManagedAsset.public_id == trace_descriptor.asset_public_id))
        assert asset is not None
        asset.kind = "telemetry_catalog"

    result = _importer(session_factory, settings).import_replay(replay_sha256, attempt)

    assert result.status == "failed" and result.event_count == 0
    with session_factory() as session:
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert run is not None and run.status == "failed" and run.trace_asset_id == asset.id
        assert session.scalar(select(func.count(TelemetryEvent.id)).where(TelemetryEvent.telemetry_run_id == run.id)) == 0


@pytest.mark.parametrize("tamper", ["nonmonotonic", "missing_complete", "bad_digest", "unsupported_major"])
def test_invalid_trace_contracts_retain_shell_and_zero_observation_graph(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings, tamper: str
) -> None:
    """Catch reader-contract failures leaking a normalized prefix into the database."""
    replay_sha256 = _replay(session_factory, settings, "3" * 64)
    run_id = "a23e4567-e89b-12d3-a456-426614174000"
    trace = _write_v1_bundle(settings.data_root / "runs" / run_id, run_id)
    attempt = _attempt(session_factory, settings, trace, run_id)
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    if tamper == "nonmonotonic":
        records[2]["sequence"] = records[1]["sequence"]
    elif tamper == "missing_complete":
        records.pop()
    elif tamper == "bad_digest":
        records[-1]["payload"]["trace_sha256"] = "0" * 64
    else:
        records[0]["schema_version"] = 99
    _write_records(trace, records)
    attempt = _refresh_registered_artifact(session_factory, attempt, trace)

    result = _importer(session_factory, settings).import_replay(replay_sha256, attempt)

    assert result.status == "failed" and result.event_count == 0
    with session_factory() as session:
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert run is not None and run.status == "failed"
        assert session.scalar(select(func.count(TelemetryEvent.id)).where(TelemetryEvent.telemetry_run_id == run.id)) == 0
        assert session.scalar(select(func.count(Entity.id)).where(Entity.telemetry_run_id == run.id)) == 0
        assert (
            session.scalar(
                select(func.count(EvidenceItem.id)).where(
                    EvidenceItem.telemetry_run_id == run.id,
                    EvidenceItem.source_kind == "telemetry_event",
                )
            )
            == 0
        )


@pytest.mark.parametrize("asset_role", ["catalog", "map_member"])
def test_v2_catalog_or_map_member_mismatch_fails_before_observations(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings, asset_role: str
) -> None:
    """Catch substituting registered but invalid v2 catalog or closed map-member bytes."""
    replay_sha256 = _replay(session_factory, settings, "4" * 64)
    run_id = "b23e4567-e89b-12d3-a456-426614174000"
    trace = _write_v2_bundle(settings.data_root / "runs" / run_id, run_id)
    attempt = _attempt(session_factory, settings, trace, run_id)
    if asset_role == "catalog":
        descriptor = next(artifact for artifact in attempt.artifacts if artifact.kind == "telemetry_catalog")
    else:
        descriptor = next(
            artifact
            for artifact in attempt.artifacts
            if artifact.kind == "telemetry_map_asset" and artifact.logical_path.endswith("height.f32.zlib")
        )
    path = trace.parent / Path(*descriptor.logical_path.split("/"))
    path.write_bytes(path.read_bytes() + b"substitution")
    attempt = _refresh_registered_artifact(session_factory, attempt, path)

    result = _importer(session_factory, settings).import_replay(replay_sha256, attempt)

    assert result.status == "failed" and result.event_count == 0
    with session_factory() as session:
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert run is not None
        issue = session.scalar(select(ReplayQualityIssue).where(ReplayQualityIssue.telemetry_run_id == run.id))
        assert session.scalar(
            select(func.count(TelemetryEvent.id)).where(TelemetryEvent.telemetry_run_id == run.id)
        ) == 0
        assert issue is not None and issue.issue_code == (
            "invalid_catalog" if asset_role == "catalog" else "invalid_map_asset"
        )


def test_runner_and_manifest_engine_build_disagreement_fails_atomically(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch selected runner configuration being silently replaced by trace metadata."""
    replay_sha256 = _replay(session_factory, settings, "6" * 64)
    run_id = "d23e4567-e89b-12d3-a456-426614174000"
    trace = _write_v1_bundle(settings.data_root / "runs" / run_id, run_id)
    attempt = replace(_attempt(session_factory, settings, trace, run_id), engine_build="different-build")

    result = _importer(session_factory, settings).import_replay(replay_sha256, attempt)

    assert result.status == "failed" and result.event_count == 0
    with session_factory() as session:
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert run is not None and run.status == "failed"
        assert session.scalar(select(func.count(TelemetryEvent.id)).where(TelemetryEvent.telemetry_run_id == run.id)) == 0


def test_unresolved_v1_entity_reference_rolls_back_the_complete_final_graph(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch an import invariant failure retaining raw events written earlier in the final transaction."""
    replay_sha256 = _replay(session_factory, settings, "5" * 64)
    run_id = "c23e4567-e89b-12d3-a456-426614174000"
    trace = _write_v1_bundle(settings.data_root / "runs" / run_id, run_id)
    attempt = _attempt(session_factory, settings, trace, run_id)
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    records[2]["payload"]["object_id"] = 999
    records[2]["payload"]["current_state_source"] = "state_machine"
    records[2]["payload"]["sample_reason"] = "periodic"
    prior = b"".join(json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records[:-1])
    records[-1]["payload"]["trace_sha256"] = hashlib.sha256(prior).hexdigest()
    _write_records(trace, records)
    attempt = _refresh_registered_artifact(session_factory, attempt, trace)

    result = _importer(session_factory, settings).import_replay(replay_sha256, attempt)

    assert result.status == "failed" and result.event_count == 0
    with session_factory() as session:
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert run is not None and run.status == "failed"
        for model in (TelemetryEvent, Entity, EntitySample, ProductionEvent, EconomyEvent, CombatEvent):
            assert session.scalar(select(func.count(model.id)).where(model.telemetry_run_id == run.id)) == 0


def test_complete_crc_and_truncation_import_full_rows_with_distinct_lifecycle(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch terminal quality being confused with trace completeness or runner lifecycle."""
    cases = (
        ("423e4567-e89b-12d3-a456-426614174000", "e" * 64, {"crc_mismatch": True}, "desynced", "crc_mismatch"),
        (
            "523e4567-e89b-12d3-a456-426614174000",
            "f" * 64,
            {"replay_truncated": True, "terminal_reason": "replay_truncated"},
            "partial",
            "telemetry_truncated",
        ),
    )
    for index, (run_id, replay_sha256, terminal, lifecycle, issue_code) in enumerate(cases):
        replay_sha256 = _replay(session_factory, settings, replay_sha256)
        trace = _write_v1_bundle(settings.data_root / "runs" / run_id, run_id, **terminal)
        result = _importer(session_factory, settings, 40_000 + index * 1_000).import_replay(
            replay_sha256, _attempt(session_factory, settings, trace, run_id)
        )
        assert result.status == "succeeded" and result.event_count == 7, _run_diagnostics(session_factory, run_id)
        with session_factory() as session:
            replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
            run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
            issue = session.scalar(
                select(ReplayQualityIssue).where(
                    ReplayQualityIssue.telemetry_run_id == run.id,
                    ReplayQualityIssue.issue_code == issue_code,
                )
            )
            assert replay is not None and replay.lifecycle_state == lifecycle
            assert issue is not None and issue.issue_code == issue_code


def test_successful_degraded_telemetry_imports_rows_without_claiming_engine_verification(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Keep mechanics-only full traces useful while preserving their non-authoritative quality."""
    replay_sha256 = _replay(session_factory, settings, "6" * 64)
    run_id = "623e4567-e89b-12d3-a456-426614174000"
    trace = _write_v1_bundle(settings.data_root / "runs" / run_id, run_id)
    attempt = replace(
        _attempt(session_factory, settings, trace, run_id),
        replay_quality="partial",
        strategy_analysis_scope="mechanics_only",
    )

    result = _importer(session_factory, settings).import_replay(replay_sha256, attempt)

    assert result.status == "succeeded" and result.event_count == 7
    with session_factory() as session:
        replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        issue = session.scalar(
            select(ReplayQualityIssue).where(
                ReplayQualityIssue.telemetry_run_id == run.id,
                ReplayQualityIssue.issue_code == "telemetry_quality_degraded",
            )
        )
        assert replay is not None and replay.lifecycle_state == "partial"
        assert run is not None and run.status == "succeeded"
        assert issue is not None and issue.details_json == {
            "replay_quality": "partial",
            "strategy_analysis_scope": "mechanics_only",
        }


def test_unsupported_lifecycle_precedes_later_crc_quality_evidence(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch terminal quality overwriting a higher-precedence accepted lifecycle fact."""
    replay_sha256 = _replay(session_factory, settings, "7" * 64)
    with session_factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
        assert replay is not None
        replay.lifecycle_state = "unsupported"
    run_id = "e23e4567-e89b-12d3-a456-426614174000"
    trace = _write_v1_bundle(settings.data_root / "runs" / run_id, run_id, crc_mismatch=True)

    result = _importer(session_factory, settings).import_replay(
        replay_sha256, _attempt(session_factory, settings, trace, run_id)
    )

    assert result.status == "succeeded" and result.event_count == 7
    with session_factory() as session:
        replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
        assert replay is not None and replay.lifecycle_state == "unsupported"


def test_public_handler_consumes_frozen_task3_outputs_and_keeps_parser_only_distinct() -> None:
    """Catch reliance on Task 3 runner internals or invented missing-telemetry diagnostics."""

    class ParserStub:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str | None]] = []

        def import_replay(
            self,
            replay_sha256: str,
            *,
            replay_public_id: str,
            parser_version: str,
            idempotency_key: str | None = None,
        ) -> ParserImportResult:
            assert replay_public_id == "replay-public-id"
            self.calls.append((replay_sha256, parser_version, idempotency_key))
            return ParserImportResult("parser-run", "succeeded", "complete", 9, False)

    class TelemetryStub:
        def __init__(self) -> None:
            self.attempts: list[TelemetryAttempt] = []
            self.idempotency_keys: list[str | None] = []

        def import_replay(
            self,
            replay_sha256: str,
            attempt: TelemetryAttempt,
            *,
            idempotency_key: str | None = None,
        ) -> TelemetryImportResult:
            assert replay_sha256 == "a" * 64
            self.attempts.append(attempt)
            self.idempotency_keys.append(idempotency_key)
            return TelemetryImportResult(attempt.run_id, "succeeded", 7, False)

    parser = ParserStub()
    telemetry = TelemetryStub()
    handler = ObservationImportHandler(parser, telemetry)  # type: ignore[arg-type]
    parse_dependency = StageDependencyOutput(PARSE_JOB_PUBLIC_ID, "parse", "1", _parse_dependency_output())
    parser_only = StageExecutionContext(
        "import-job",
        "import_observations:1:key",
        "replay-public-id",
        "a" * 64,
        "import_observations",
        IMPORT_OBSERVATIONS_VERSION,
        {},
        (parse_dependency,),
    )
    assert handler(parser_only) == {
        "idempotency_key": "import_observations:1:key",
        "parser_run_id": "parser-run",
        "parser_command_count": 9,
        "telemetry_run_id": None,
        "telemetry_event_count": 0,
    }
    assert telemetry.attempts == []

    descriptor = {
        "asset_public_id": "00000000-0000-0000-0000-000000000001",
        "kind": "telemetry_trace",
        "logical_path": "trace.ndjson",
        "sha256": "b" * 64,
        "size_bytes": 123,
    }
    telemetry_dependency = StageDependencyOutput(
        TELEMETRY_JOB_PUBLIC_ID,
        "telemetry",
        "1",
        {
            "run_id": "823e4567-e89b-12d3-a456-426614174000",
            "runner_status": "success",
            "replay_quality": "complete",
            "strategy_analysis_scope": "full",
            "exit_code": 0,
            "engine_build": ENGINE_IDENTITY,
            "engine_executable_sha256": "c" * 64,
            "diagnostics": ({"code": "ok", "message": "validated"},),
            "artifacts": (descriptor,),
        },
    )
    with_telemetry = StageExecutionContext(
        "import-job",
        "import_observations:1:key",
        "replay-public-id",
        "a" * 64,
        "import_observations",
        IMPORT_OBSERVATIONS_VERSION,
        {},
        (parse_dependency, telemetry_dependency),
    )
    assert handler(with_telemetry)["telemetry_event_count"] == 7
    assert telemetry.attempts == [
        TelemetryAttempt(
            run_id="823e4567-e89b-12d3-a456-426614174000",
            runner_status="success",
            replay_quality="complete",
            strategy_analysis_scope="full",
            process_exit_code=0,
            engine_build=ENGINE_IDENTITY,
            engine_executable_sha256="c" * 64,
            diagnostics=({"code": "ok", "message": "validated"},),
            artifacts=(
                ManagedTelemetryArtifact(
                    "00000000-0000-0000-0000-000000000001",
                    "telemetry_trace",
                    "trace.ndjson",
                    "b" * 64,
                    123,
                ),
            ),
            parser_run_id="parser-run",
        )
    ]
    assert telemetry.idempotency_keys == ["import_observations:1:key"]

    missing_parse = StageExecutionContext(
        "import-job",
        "key",
        "replay-public-id",
        "a" * 64,
        "import_observations",
        IMPORT_OBSERVATIONS_VERSION,
        {},
        (),
    )
    with pytest.raises(StageFailure, match="parser dependency output is missing"):
        handler(missing_parse)
    invalid_parse = StageExecutionContext(
        "import-job",
        "key",
        "replay-public-id",
        "a" * 64,
        "import_observations",
        IMPORT_OBSERVATIONS_VERSION,
        {},
        (
            StageDependencyOutput(
                PARSE_JOB_PUBLIC_ID,
                "parse",
                "1",
                dict(_parse_dependency_output(), parser_version=1),
            ),
        ),
    )
    with pytest.raises(StageFailure, match="parser dependency evidence is invalid"):
        handler(invalid_parse)
    incomplete_telemetry = StageExecutionContext(
        "import-job",
        "key",
        "replay-public-id",
        "a" * 64,
        "import_observations",
        IMPORT_OBSERVATIONS_VERSION,
        {},
        (
            parse_dependency,
            StageDependencyOutput(
                TELEMETRY_JOB_PUBLIC_ID,
                "telemetry",
                "1",
                {"artifacts": ({"asset_public_id": "missing-fields"},)},
            ),
        ),
    )
    with pytest.raises(StageFailure, match="telemetry failure attempt fields are incomplete"):
        handler(incomplete_telemetry)


@pytest.mark.parametrize(
    "malformation",
    [
        "extra_output_field",
        "missing_output_field",
        "wrong_run_id",
        "wrong_runner_type",
        "bool_exit_code",
        "wrong_engine_build_type",
        "uppercase_engine_hash",
        "mutable_diagnostics",
        "extra_diagnostic_field",
        "wrong_descriptor_type",
        "bool_descriptor_size",
        "duplicate_logical_path",
        "noncanonical_manifest",
        "pathlike_metadata",
        "pathlike_diagnostic",
    ],
)
def test_succeeded_telemetry_dependency_contract_damage_is_typed_and_nonretryable(malformation: str) -> None:
    """Catch raw KeyError/TypeError/casts or path provenance crossing the public handler boundary."""

    class ParserStub:
        def import_replay(self, *_args: object, **_kwargs: object) -> ParserImportResult:
            return ParserImportResult("parser-run", "succeeded", "complete", 1, False)

    class TelemetryStub:
        def import_replay(self, *_args: object, **_kwargs: object) -> TelemetryImportResult:
            raise AssertionError("damaged dependency must not reach telemetry persistence")

    descriptor: dict[str, object] = {
        "asset_public_id": "00000000-0000-0000-0000-000000000001",
        "kind": "telemetry_trace",
        "logical_path": "trace.ndjson",
        "sha256": "b" * 64,
        "size_bytes": 1,
    }
    output: dict[str, object] = {
        "run_id": "823e4567-e89b-12d3-a456-426614174000",
        "runner_status": "success",
        "replay_quality": "complete",
        "strategy_analysis_scope": "full",
        "exit_code": 0,
        "engine_build": ENGINE_IDENTITY,
        "engine_executable_sha256": "c" * 64,
        "diagnostics": ({"code": "ok", "message": "validated"},),
        "artifacts": (descriptor,),
    }
    if malformation == "extra_output_field":
        output["unexpected"] = True
    elif malformation == "missing_output_field":
        output.pop("run_id")
    elif malformation == "wrong_run_id":
        output["run_id"] = "not-a-uuid"
    elif malformation == "wrong_runner_type":
        output["runner_status"] = 1
    elif malformation == "bool_exit_code":
        output["exit_code"] = True
    elif malformation == "wrong_engine_build_type":
        output["engine_build"] = 1
    elif malformation == "uppercase_engine_hash":
        output["engine_executable_sha256"] = "C" * 64
    elif malformation == "mutable_diagnostics":
        output["diagnostics"] = [{"code": "ok", "message": "validated"}]
    elif malformation == "extra_diagnostic_field":
        output["diagnostics"] = ({"code": "ok", "message": "validated", "extra": True},)
    elif malformation == "wrong_descriptor_type":
        descriptor["kind"] = 1
    elif malformation == "bool_descriptor_size":
        descriptor["size_bytes"] = True
    elif malformation == "duplicate_logical_path":
        output["artifacts"] = (descriptor, dict(descriptor))
    elif malformation == "noncanonical_manifest":
        second = dict(descriptor, asset_public_id="00000000-0000-0000-0000-000000000002", logical_path="a.ndjson")
        output["artifacts"] = (descriptor, second)
    elif malformation == "pathlike_metadata":
        output["engine_build"] = "failure at C:\\private\\engine.exe"
    else:
        output["diagnostics"] = ({"code": "ok", "message": "open file:///private/trace.ndjson"},)
    handler = ObservationImportHandler(ParserStub(), TelemetryStub())  # type: ignore[arg-type]
    context = StageExecutionContext(
        "import-job",
        "import_observations:1:key",
        "replay-public-id",
        "a" * 64,
        "import_observations",
        IMPORT_OBSERVATIONS_VERSION,
        {},
        (
            StageDependencyOutput(PARSE_JOB_PUBLIC_ID, "parse", "1", _parse_dependency_output()),
            StageDependencyOutput(TELEMETRY_JOB_PUBLIC_ID, "telemetry", "1", output),
        ),
    )

    with pytest.raises(StageFailure) as failure:
        handler(context)
    assert failure.value.code == "telemetry_dependency_invalid" and failure.value.retryable is False


def test_dependency_stage_collision_is_rejected_before_any_importer_call() -> None:
    """Catch dict materialization silently choosing one of two same-stage frozen dependencies."""

    class ImporterStub:
        def import_replay(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("dependency collision must fail first")

    handler = ObservationImportHandler(ImporterStub(), ImporterStub())  # type: ignore[arg-type]
    parse = StageDependencyOutput(PARSE_JOB_PUBLIC_ID, "parse", "1", _parse_dependency_output())
    context = StageExecutionContext(
        "import-job",
        "key",
        "replay",
        "a" * 64,
        "import_observations",
        IMPORT_OBSERVATIONS_VERSION,
        {},
        (parse, replace(parse, job_public_id="other")),
    )
    with pytest.raises(StageFailure) as failure:
        handler(context)
    assert failure.value.code == "dependency_contract_invalid" and failure.value.retryable is False


@pytest.mark.parametrize(
    "malformation",
    [
        "concrete_type",
        "component_version",
        "public_id",
        "public_id_type",
        "public_id_subclass",
        "succeeded_error_evidence",
        "failed_output_evidence",
        "duplicate_public_id",
    ],
)
def test_handler_rejects_malformed_dependency_identity_and_terminal_shape_before_import(
    malformation: str,
) -> None:
    """Catch malformed public dependency snapshots escaping as retryable generic worker failures."""

    class ImporterStub:
        def import_replay(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("malformed dependency must fail before observation persistence")

        def record_failed_dependency(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("malformed dependency must fail before failed-shell persistence")

    parse = StageDependencyOutput(PARSE_JOB_PUBLIC_ID, "parse", "1", _parse_dependency_output())
    dependencies: tuple[object, ...] = (parse,)
    if malformation == "concrete_type":
        dependencies = (object(),)
    elif malformation == "component_version":
        dependencies = (replace(parse, component_version="2"),)
    elif malformation == "public_id":
        dependencies = (replace(parse, job_public_id="not-a-uuid"),)
    elif malformation == "public_id_type":
        dependencies = (replace(parse, job_public_id=cast(Any, 1)),)
    elif malformation == "public_id_subclass":
        class HostileString(str):
            def replace(self, *_args: object, **_kwargs: object) -> str:
                raise AttributeError("hostile replace")

        dependencies = (replace(parse, job_public_id=HostileString(PARSE_JOB_PUBLIC_ID)),)
    elif malformation == "succeeded_error_evidence":
        dependencies = (replace(parse, error_code="unexpected_error"),)
    elif malformation == "failed_output_evidence":
        dependencies = (
            replace(
                parse,
                status="failed",
                error_code="parser_failed",
                error_message="parser dependency failed",
                error_details={},
            ),
        )
    else:
        dependencies = (
            parse,
            StageDependencyOutput(PARSE_JOB_PUBLIC_ID, "telemetry", "1", {}),
        )
    handler = ObservationImportHandler(ImporterStub(), ImporterStub())  # type: ignore[arg-type]
    context = StageExecutionContext(
        "import-job",
        "import_observations:1:key",
        "replay-public-id",
        "a" * 64,
        "import_observations",
        IMPORT_OBSERVATIONS_VERSION,
        {},
        cast(Any, dependencies),
    )

    with pytest.raises(StageFailure) as failure:
        handler(context)
    assert failure.value.code == "dependency_contract_invalid" and failure.value.retryable is False


@pytest.mark.parametrize(
    "malformation",
    [
        "extra_envelope_field",
        "wrong_envelope_type",
        "mismatched_failure_code",
        "missing_attempt_field",
        "empty_run_id",
        "invalid_run_id",
        "wrong_exit_type",
        "wrong_engine_hash_type",
        "extra_descriptor_field",
        "unsorted_descriptors",
        "mutable_diagnostics",
        "mutable_artifacts",
        "incomplete_diagnostic",
    ],
)
def test_typed_telemetry_failure_envelope_rejects_contract_malformations(malformation: str) -> None:
    """Catch Task 4 accepting an untyped, incomplete, or mutable Task 3 failure attempt."""
    attempt: dict[str, object] = {
        "artifacts": (),
        "diagnostics": ({"code": "copy_failed", "message": "managed copy failed"},),
        "engine_build": ENGINE_IDENTITY,
        "engine_executable_sha256": "a" * 64,
        "exit_code": 0,
        "replay_quality": "complete",
        "run_id": "e23e4567-e89b-12d3-a456-426614174000",
        "runner_status": "success",
        "strategy_analysis_scope": "full",
    }
    envelope: dict[str, object] = {
        "attempt": attempt,
        "failure_code": "artifact_copy_failed",
        "failure_message": "telemetry artifact copy failed",
        "quality_issue_code": "invalid_trace",
        "type": "telemetry_artifact_failure",
        "version": 1,
    }
    if malformation == "extra_envelope_field":
        envelope["unexpected"] = True
    elif malformation == "wrong_envelope_type":
        envelope["type"] = "untyped"
    elif malformation == "mismatched_failure_code":
        envelope["failure_code"] = "invalid_telemetry_artifact"
    elif malformation == "missing_attempt_field":
        attempt.pop("run_id")
    elif malformation == "empty_run_id":
        attempt["run_id"] = ""
    elif malformation == "invalid_run_id":
        attempt["run_id"] = "not-a-uuid"
    elif malformation == "wrong_exit_type":
        attempt["exit_code"] = "0"
    elif malformation == "wrong_engine_hash_type":
        attempt["engine_executable_sha256"] = 7
    elif malformation == "extra_descriptor_field":
        attempt["artifacts"] = (
            {
                "asset_public_id": "f23e4567-e89b-12d3-a456-426614174000",
                "kind": "telemetry_stdout",
                "logical_path": "stdout.log",
                "sha256": "b" * 64,
                "size_bytes": 4,
                "source_path": "C:/private/stdout.log",
            },
        )
    elif malformation == "unsorted_descriptors":
        attempt["artifacts"] = tuple(
            {
                "asset_public_id": public_id,
                "kind": "telemetry_stdout",
                "logical_path": logical_path,
                "sha256": sha256,
                "size_bytes": 4,
            }
            for public_id, logical_path, sha256 in (
                ("f23e4567-e89b-12d3-a456-426614174000", "z.log", "b" * 64),
                ("f33e4567-e89b-12d3-a456-426614174000", "a.log", "c" * 64),
            )
        )
    elif malformation == "mutable_diagnostics":
        attempt["diagnostics"] = [{"code": "copy_failed", "message": "managed copy failed"}]
    elif malformation == "mutable_artifacts":
        attempt["artifacts"] = []
    else:
        attempt["diagnostics"] = ({"code": "copy_failed"},)
    dependency = StageDependencyOutput(
        TELEMETRY_JOB_PUBLIC_ID,
        "telemetry",
        "1",
        None,
        status="failed",
        error_code="artifact_copy_failed",
        error_message="telemetry artifact copy failed",
        error_details={"failure_envelope": envelope},
    )
    assert dependency.error_details is not None
    with pytest.raises(StageFailure) as failure:
        telemetry_import_module._attempt_from_failed_dependency(
            dependency,
            "artifact_copy_failed",
            dependency.error_details,
        )
    assert failure.value.code == "telemetry_dependency_invalid"


@pytest.mark.parametrize(
    "path_text",
    [
        r"diagnostic C:\private\trace.ndjson",
        r"diagnostic \\server\share\trace.ndjson",
        r"diagnostic \\?\C:\private\trace.ndjson",
        "diagnostic /private/trace.ndjson",
        "diagnostic file:///private/trace.ndjson",
        r"diagnostic=C:\private\trace.ndjson",
    ],
)
def test_consumer_rejects_every_path_form_in_typed_failure_envelope(path_text: str) -> None:
    """Catch source filesystem provenance entering a retained attempt through any diagnostic path spelling."""
    envelope = {
        "type": "telemetry_artifact_failure",
        "version": 1,
        "failure_code": "artifact_copy_failed",
        "failure_message": "telemetry artifact copy failed",
        "quality_issue_code": "invalid_trace",
        "attempt": {
            "artifacts": (),
            "diagnostics": ({"code": "copy_failed", "message": path_text},),
            "engine_build": ENGINE_IDENTITY,
            "engine_executable_sha256": "a" * 64,
            "exit_code": 0,
            "replay_quality": "complete",
            "run_id": "e23e4567-e89b-12d3-a456-426614174000",
            "runner_status": "success",
            "strategy_analysis_scope": "full",
        },
    }
    dependency = StageDependencyOutput(
        TELEMETRY_JOB_PUBLIC_ID,
        "telemetry",
        "1",
        None,
        status="failed",
        error_code="artifact_copy_failed",
        error_message="telemetry artifact copy failed",
        error_details={"failure_envelope": envelope},
    )
    assert dependency.error_details is not None
    with pytest.raises(StageFailure) as failure:
        telemetry_import_module._attempt_from_failed_dependency(
            dependency,
            "artifact_copy_failed",
            dependency.error_details,
        )
    assert failure.value.code == "telemetry_dependency_invalid" and failure.value.retryable is False


@pytest.mark.parametrize("failure_mode", ["registration_exception", "cross_kind_same_bytes"])
def test_real_dag_post_copy_registration_failure_is_typed_and_links_only_verified_assets(
    failure_mode: str,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    """Catch post-copy metadata errors escaping StageFailure or publishing a cross-role manifest."""
    run_id = str(UUID(int=61_000 + len(failure_mode)))
    root = tmp_path / failure_mode
    root.mkdir()
    trace = root / "trace.ndjson"
    stdout = root / "stdout.log"
    trace.write_bytes(b"same bytes")
    stdout.write_bytes(trace.read_bytes() if failure_mode == "cross_kind_same_bytes" else b"stdout")
    artifact = TelemetryArtifact(
        run_id=run_id,
        runner_status="success",
        replay_quality="complete",
        strategy_analysis_scope="full",
        trace_path=trace,
        catalog_path=None,
        map_asset_paths=(),
        outcome_path=None,
        stdout_path=stdout,
        stderr_path=None,
        exit_code=0,
        engine_build=ENGINE_IDENTITY,
        engine_executable_sha256="d" * 64,
        diagnostics=(AcquisitionDiagnostic("fixture", "registration fixture"),),
    )

    class Acquirer:
        def acquire(self, _replay: Path, _sha256: str) -> TelemetryArtifact:
            return artifact

    handler = ObservationImportHandler(
        _identity_parser_importer(
            session_factory,
            settings.data_root,
            parser=parse_replay,
            parser_version="test-parser-1",
            schema_version=1,
            clock=clock,
            uuid_factory=DeterministicUUIDs(62_000),
        ),
        TelemetryObservationImporter(
            session_factory,
            settings.data_root,
            clock=clock,
            uuid_factory=DeterministicUUIDs(63_000),
        ),
    )
    service = ImportService(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        parser=parse_replay,
        telemetry_acquirer=Acquirer(),
        clock=clock,
        parser_version="test-parser-1",
        telemetry_acquirer_version=f"{failure_mode}-1",
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations",
                IMPORT_OBSERVATIONS_VERSION,
                handler,
                terminal_dependency_policy=TerminalDependencyPolicy(failed_stages=frozenset({"parse", "telemetry"})),
            ),
        ),
    )
    if failure_mode == "registration_exception":
        original_register = service._register_asset

        def fail_registration(session: Session, stored: StoredContent, kind: str) -> object:
            if kind.startswith("telemetry_"):
                raise RuntimeError(r"registration failed at C:\private\asset.bin")
            return original_register(session, stored, kind)

        monkeypatch.setattr(service, "_register_asset", fail_registration)

    service.submit(ImportRequest(replay_file, request_telemetry=True))
    completed = service.run_available("registration-worker", limit=10)

    assert tuple(job.stage for job in completed)[-2:] == ("telemetry", "import_observations")
    assert (completed[-2].status, completed[-1].status) == ("failed", "succeeded")
    with session_factory() as session:
        job = session.scalar(select(Job).where(Job.stage == "telemetry"))
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert job is not None and job.error_code == "artifact_registration_failed"
        envelope = job.error_details_json["failure_envelope"]
        retained = envelope["attempt"]["artifacts"]
        assert [item["kind"] for item in retained] == ([] if failure_mode == "registration_exception" else ["telemetry_trace"])
        assert r"C:\private" not in json.dumps(job.error_details_json)
        assert run is not None and run.status == "failed"
        assert set(
            session.scalars(
                select(ReplayQualityIssue.issue_code).where(ReplayQualityIssue.telemetry_run_id == run.id)
            )
        ) == {"asset_invalid"}
        for model in (TelemetryEvent, Entity, EntitySample, ProductionEvent, EconomyEvent, CombatEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize(
    "path_text",
    [
        r"failed at C:\private\trace.ndjson",
        r"failed at \\server\share\trace.ndjson",
        r"failed at \\?\C:\private\trace.ndjson",
        "failed at /private/trace.ndjson",
        "failed at file:///private/trace.ndjson",
    ],
)
def test_real_dag_producer_rejects_and_redacts_residual_pathlike_diagnostics(
    path_text: str,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    """Catch embedded source paths surviving Task 3 producer redaction into a Task 4 failure envelope."""
    run_id = str(UUID(int=64_000 + len(path_text)))
    root = tmp_path / f"unsafe-{len(path_text)}"
    root.mkdir()
    trace = root / "trace.ndjson"
    trace.write_bytes(b"trace")
    artifact = TelemetryArtifact(
        run_id=run_id,
        runner_status="success",
        replay_quality="complete",
        strategy_analysis_scope="full",
        trace_path=trace,
        catalog_path=None,
        map_asset_paths=(),
        outcome_path=None,
        stdout_path=None,
        stderr_path=None,
        exit_code=0,
        engine_build=ENGINE_IDENTITY,
        engine_executable_sha256="e" * 64,
        diagnostics=(AcquisitionDiagnostic("unsafe_metadata", path_text),),
    )

    class Acquirer:
        def acquire(self, _replay: Path, _sha256: str) -> TelemetryArtifact:
            return artifact

    handler = ObservationImportHandler(
        _identity_parser_importer(
            session_factory, settings.data_root, parser=parse_replay, parser_version="test-parser-1",
            schema_version=1, clock=clock, uuid_factory=DeterministicUUIDs(65_000),
        ),
        TelemetryObservationImporter(
            session_factory, settings.data_root, clock=clock, uuid_factory=DeterministicUUIDs(66_000)
        ),
    )
    service = ImportService(
        session_factory, settings, replay_store, artifact_store, parser=parse_replay,
        telemetry_acquirer=Acquirer(), clock=clock, parser_version="test-parser-1",
        telemetry_acquirer_version=f"unsafe-metadata-{len(path_text)}",
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations", IMPORT_OBSERVATIONS_VERSION, handler,
                terminal_dependency_policy=TerminalDependencyPolicy(failed_stages=frozenset({"parse", "telemetry"})),
            ),
        ),
    )
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    completed = service.run_available("unsafe-metadata-worker", limit=10)

    assert tuple(job.stage for job in completed)[-2:] == ("telemetry", "import_observations")
    assert (completed[-2].status, completed[-1].status) == ("failed", "succeeded")
    with session_factory() as session:
        job = session.scalar(select(Job).where(Job.stage == "telemetry"))
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        persisted = json.dumps(job.error_details_json, sort_keys=True)
        assert job is not None and job.error_code == "invalid_telemetry_metadata"
        assert path_text not in persisted and "[redacted-path]" in persisted
        assert run is not None and run.status == "failed"


def test_real_dag_failed_telemetry_persists_attempt_assets_issues_and_zero_children(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    """Catch real Task 3 terminal telemetry evidence never reaching the Task 4 transaction."""
    run_id = "a23e4567-e89b-12d3-a456-426614174000"
    artifact_root = tmp_path / "failed-telemetry"
    artifact_root.mkdir()
    stdout = artifact_root / "engine-stdout.log"
    outcome = artifact_root / "engine-outcome.json"
    stdout.write_text("engine failed after launch", encoding="utf-8")
    outcome.write_text('{"runner_status":"invalid_trace"}\n', encoding="utf-8")
    artifact = TelemetryArtifact(
        run_id=run_id,
        runner_status="invalid_trace",
        replay_quality="failed",
        strategy_analysis_scope="none",
        trace_path=None,
        catalog_path=None,
        map_asset_paths=(),
        outcome_path=outcome,
        stdout_path=stdout,
        stderr_path=None,
        exit_code=7,
        engine_build=ENGINE_IDENTITY,
        engine_executable_sha256="c" * 64,
        diagnostics=(AcquisitionDiagnostic("invalid_trace", "trace completion record was invalid"),),
    )

    class FailedAcquirer:
        def acquire(self, replay: Path, replay_sha256: str) -> TelemetryArtifact:
            assert replay.is_file() and len(replay_sha256) == 64
            return artifact

    parser_importer = _identity_parser_importer(
        session_factory,
        settings.data_root,
        parser=parse_replay,
        parser_version="test-parser-1",
        schema_version=1,
        clock=clock,
        uuid_factory=DeterministicUUIDs(30_000),
    )
    telemetry_importer = TelemetryObservationImporter(
        session_factory,
        settings.data_root,
        clock=clock,
        uuid_factory=DeterministicUUIDs(40_000),
    )
    handler = ObservationImportHandler(parser_importer, telemetry_importer)
    service = ImportService(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        parser=parse_replay,
        telemetry_acquirer=FailedAcquirer(),
        clock=clock,
        parser_version="test-parser-1",
        telemetry_acquirer_version="failed-acquirer-1",
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations",
                IMPORT_OBSERVATIONS_VERSION,
                handler,
                terminal_dependency_policy=TerminalDependencyPolicy(
                    failed_stages=frozenset({"parse", "telemetry"})
                ),
            ),
        ),
    )

    service.submit(ImportRequest(replay_file, request_telemetry=True))
    completed = service.run_available("task-4-integration", limit=10)
    assert tuple(job.stage for job in completed)[-2:] == ("telemetry", "import_observations")
    assert completed[-2].status == "failed"
    assert completed[-1].status == "succeeded"

    with session_factory() as session:
        import_job = session.scalar(select(Job).where(Job.stage == "import_observations"))
        telemetry_run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert import_job is not None and import_job.status == "succeeded"
        assert telemetry_run is not None and telemetry_run.status == "failed"
        assert telemetry_run.runner_status == "invalid_trace"
        assert telemetry_run.trace_asset_id is None
        manifest = telemetry_run.settings_json["artifact_manifest"]
        assert [item["logical_path"] for item in manifest] == ["outcome.json", "stdout.log"]
        assert all("/" not in item["logical_path"] for item in manifest)
        retained_ids = {item["asset_public_id"] for item in manifest}
        assert set(
            session.scalars(select(ManagedAsset.public_id).where(ManagedAsset.public_id.in_(retained_ids)))
        ) == retained_ids
        issues = set(
            session.scalars(
                select(ReplayQualityIssue.issue_code).where(
                    ReplayQualityIssue.telemetry_run_id == telemetry_run.id
                )
            )
        )
        assert issues == {"exporter_failure"}
        assert session.scalar(
            select(func.count()).select_from(EvidenceItem).where(EvidenceItem.telemetry_run_id == telemetry_run.id)
        ) == 0
        for model in (
            TelemetryEvent,
            Entity,
            EntitySample,
            ProductionEvent,
            EconomyEvent,
            CombatEvent,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_real_dag_artifact_validation_failure_retains_typed_attempt_and_zero_children(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    """Catch post-acquisition path validation discarding the canonical telemetry attempt."""
    run_id = "b23e4567-e89b-12d3-a456-426614174000"
    private_root = tmp_path / "private-invalid-bundle"
    private_root.mkdir()
    trace = private_root / "trace.ndjson"
    trace.write_text('{"type":"fixture"}\n', encoding="utf-8")
    outside = tmp_path / "private-outside.log"
    outside.write_text("outside bundle", encoding="utf-8")
    artifact = TelemetryArtifact(
        run_id=run_id,
        runner_status="success",
        replay_quality="complete",
        strategy_analysis_scope="full",
        trace_path=trace,
        catalog_path=None,
        map_asset_paths=(),
        outcome_path=None,
        stdout_path=outside,
        stderr_path=None,
        exit_code=0,
        engine_build=ENGINE_IDENTITY,
        engine_executable_sha256="d" * 64,
        diagnostics=(
            AcquisitionDiagnostic(
                "artifact_outside_bundle",
                f"trace={trace} rejected={outside}",
            ),
        ),
    )

    class InvalidBundleAcquirer:
        def acquire(self, replay: Path, replay_sha256: str) -> TelemetryArtifact:
            assert replay.is_file() and len(replay_sha256) == 64
            return artifact

    handler = ObservationImportHandler(
        _identity_parser_importer(
            session_factory,
            settings.data_root,
            parser=parse_replay,
            parser_version="test-parser-1",
            schema_version=1,
            clock=clock,
            uuid_factory=DeterministicUUIDs(41_000),
        ),
        TelemetryObservationImporter(
            session_factory,
            settings.data_root,
            clock=clock,
            uuid_factory=DeterministicUUIDs(42_000),
        ),
    )
    service = ImportService(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        parser=parse_replay,
        telemetry_acquirer=InvalidBundleAcquirer(),
        clock=clock,
        parser_version="test-parser-1",
        telemetry_acquirer_version="invalid-bundle-acquirer-1",
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations",
                IMPORT_OBSERVATIONS_VERSION,
                handler,
                terminal_dependency_policy=TerminalDependencyPolicy(
                    failed_stages=frozenset({"parse", "telemetry"})
                ),
            ),
        ),
    )

    service.submit(ImportRequest(replay_file, request_telemetry=True))
    completed = service.run_available("artifact-validation-worker", limit=10)
    assert tuple(job.stage for job in completed)[-2:] == ("telemetry", "import_observations")
    assert (completed[-2].status, completed[-1].status) == ("failed", "succeeded")

    with session_factory() as session:
        telemetry_job = session.scalar(select(Job).where(Job.stage == "telemetry"))
        telemetry_run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert telemetry_job is not None and telemetry_job.error_code == "invalid_telemetry_artifact"
        envelope = telemetry_job.error_details_json["failure_envelope"]
        assert envelope["type"] == "telemetry_artifact_failure"
        assert envelope["version"] == 1
        assert envelope["failure_code"] == "invalid_telemetry_artifact"
        assert envelope["quality_issue_code"] == "invalid_trace"
        attempt = envelope["attempt"]
        assert attempt == {
            "artifacts": [],
            "diagnostics": [
                {
                    "code": "artifact_outside_bundle",
                    "message": "trace=[artifact] rejected=[artifact]",
                }
            ],
            "engine_build": ENGINE_IDENTITY,
            "engine_executable_sha256": "d" * 64,
            "exit_code": 0,
            "replay_quality": "complete",
            "run_id": run_id,
            "runner_status": "success",
            "strategy_analysis_scope": "full",
        }
        assert str(private_root) not in json.dumps(telemetry_job.error_details_json, sort_keys=True)
        assert str(outside) not in json.dumps(telemetry_job.error_details_json, sort_keys=True)
        assert telemetry_run is not None and telemetry_run.status == "failed"
        assert telemetry_run.runner_status == "success"
        assert telemetry_run.settings_json["artifact_manifest"] == []
        assert telemetry_run.trace_asset_id is None
        assert set(
            session.scalars(
                select(ReplayQualityIssue.issue_code).where(
                    ReplayQualityIssue.telemetry_run_id == telemetry_run.id
                )
            )
        ) == {"invalid_trace"}
        assert session.scalar(
            select(func.count()).select_from(EvidenceItem).where(EvidenceItem.telemetry_run_id == telemetry_run.id)
        ) == 0
        for model in (TelemetryEvent, Entity, EntitySample, ProductionEvent, EconomyEvent, CombatEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_real_dag_mid_copy_failure_links_only_completed_asset_and_zero_children(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    """Catch a later artifact copy failure discarding an earlier verified managed asset."""
    run_id = "c23e4567-e89b-12d3-a456-426614174000"
    private_root = tmp_path / "private-mid-copy-bundle"
    private_root.mkdir()
    trace = private_root / "trace.ndjson"
    stdout = private_root / "stdout.log"
    trace.write_text('{"type":"fixture"}\n', encoding="utf-8")
    stdout.write_text("engine stdout", encoding="utf-8")
    artifact = TelemetryArtifact(
        run_id=run_id,
        runner_status="success",
        replay_quality="complete",
        strategy_analysis_scope="full",
        trace_path=trace,
        catalog_path=None,
        map_asset_paths=(),
        outcome_path=None,
        stdout_path=stdout,
        stderr_path=None,
        exit_code=0,
        engine_build=ENGINE_IDENTITY,
        engine_executable_sha256="e" * 64,
        diagnostics=(AcquisitionDiagnostic("copy_fixture", f"bundle={private_root}"),),
    )

    class SuccessfulAcquirer:
        def acquire(self, replay: Path, replay_sha256: str) -> TelemetryArtifact:
            assert replay.is_file() and len(replay_sha256) == 64
            return artifact

    class FailEverySecondCopy:
        def __init__(self, delegate: ContentAddressedStore) -> None:
            self._delegate = delegate
            self.calls = 0

        def store_file(self, source: Path, *, expected_sha256: str | None = None) -> StoredContent:
            self.calls += 1
            if self.calls % 2 == 0:
                raise ContentStorageError(f"copy failed for private source {source}")
            return self._delegate.store_file(source, expected_sha256=expected_sha256)

    failing_store = FailEverySecondCopy(artifact_store)
    handler = ObservationImportHandler(
        _identity_parser_importer(
            session_factory,
            settings.data_root,
            parser=parse_replay,
            parser_version="test-parser-1",
            schema_version=1,
            clock=clock,
            uuid_factory=DeterministicUUIDs(43_000),
        ),
        TelemetryObservationImporter(
            session_factory,
            settings.data_root,
            clock=clock,
            uuid_factory=DeterministicUUIDs(44_000),
        ),
    )
    service = ImportService(
        session_factory,
        settings,
        replay_store,
        failing_store,  # type: ignore[arg-type]
        parser=parse_replay,
        telemetry_acquirer=SuccessfulAcquirer(),
        clock=clock,
        parser_version="test-parser-1",
        telemetry_acquirer_version="mid-copy-acquirer-1",
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations",
                IMPORT_OBSERVATIONS_VERSION,
                handler,
                terminal_dependency_policy=TerminalDependencyPolicy(
                    failed_stages=frozenset({"parse", "telemetry"})
                ),
            ),
        ),
    )

    service.submit(ImportRequest(replay_file, request_telemetry=True))
    first = service.run_available("mid-copy-worker", limit=10)
    assert first[-1].stage == "telemetry" and first[-1].status == "pending"
    clock.advance(seconds=5)
    second = service.run_available("mid-copy-worker", limit=10)
    assert len(second) == 1 and second[0].status == "pending"
    clock.advance(seconds=10)
    exhausted = service.run_available("mid-copy-worker", limit=10)
    assert tuple(job.stage for job in exhausted) == ("telemetry", "import_observations")
    assert (exhausted[0].status, exhausted[0].retryable, exhausted[1].status) == (
        "failed",
        False,
        "succeeded",
    )

    trace_sha256 = hashlib.sha256(trace.read_bytes()).hexdigest()
    with session_factory() as session:
        telemetry_job = session.scalar(select(Job).where(Job.stage == "telemetry"))
        telemetry_run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert telemetry_job is not None and telemetry_job.error_code == "artifact_copy_failed"
        envelope = telemetry_job.error_details_json["failure_envelope"]
        assert envelope["failure_code"] == "artifact_copy_failed"
        assert envelope["quality_issue_code"] == "invalid_trace"
        retained = envelope["attempt"]["artifacts"]
        assert retained == [
            {
                "asset_public_id": retained[0]["asset_public_id"],
                "kind": "telemetry_trace",
                "logical_path": "trace.ndjson",
                "sha256": trace_sha256,
                "size_bytes": trace.stat().st_size,
            }
        ]
        persisted = json.dumps(telemetry_job.error_details_json, sort_keys=True)
        assert str(private_root) not in persisted
        retained_asset = session.scalar(
            select(ManagedAsset).where(ManagedAsset.public_id == retained[0]["asset_public_id"])
        )
        assert retained_asset is not None and retained_asset.sha256 == trace_sha256
        assert telemetry_run is not None and telemetry_run.status == "failed"
        assert telemetry_run.trace_asset_id == retained_asset.id
        assert telemetry_run.settings_json["artifact_manifest"] == retained
        assert set(
            session.scalars(
                select(ReplayQualityIssue.issue_code).where(
                    ReplayQualityIssue.telemetry_run_id == telemetry_run.id
                )
            )
        ) == {"invalid_trace"}
        assert session.scalar(
            select(func.count()).select_from(EvidenceItem).where(EvidenceItem.telemetry_run_id == telemetry_run.id)
        ) == 0
        for model in (TelemetryEvent, Entity, EntitySample, ProductionEvent, EconomyEvent, CombatEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_real_dag_failed_parser_persists_failure_shell_and_zero_parser_children(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    """Catch parser terminal evidence being lost before Task 4 can retain its failed attempt shell."""

    def failing_parser(_path: Path) -> object:
        raise ValueError("parser rejected fixture bytes")

    parser_importer = _identity_parser_importer(
        session_factory,
        settings.data_root,
        parser=parse_replay,
        parser_version="test-parser-1",
        schema_version=1,
        clock=clock,
        uuid_factory=DeterministicUUIDs(50_000),
    )
    telemetry_importer = TelemetryObservationImporter(
        session_factory,
        settings.data_root,
        clock=clock,
        uuid_factory=DeterministicUUIDs(60_000),
    )
    handler = ObservationImportHandler(parser_importer, telemetry_importer)
    service = ImportService(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        parser=failing_parser,  # type: ignore[arg-type]
        clock=clock,
        parser_version="test-parser-1",
        telemetry_acquirer_version="unused-acquirer-1",
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations",
                IMPORT_OBSERVATIONS_VERSION,
                handler,
                terminal_dependency_policy=TerminalDependencyPolicy(
                    failed_stages=frozenset({"parse", "telemetry"})
                ),
            ),
        ),
    )

    service.submit(ImportRequest(replay_file))
    completed = service.run_available("task-4-integration", limit=10)
    assert tuple(job.stage for job in completed)[-2:] == ("parse", "import_observations")
    assert completed[-2].status == "failed"
    assert completed[-1].status == "succeeded"

    with session_factory() as session:
        parser_run = session.scalar(select(ParserRun))
        import_job = session.scalar(select(Job).where(Job.stage == "import_observations"))
        assert parser_run is not None and parser_run.status == "failed"
        assert parser_run.error_json["code"] == "parser_failed"
        assert import_job is not None and import_job.status == "succeeded"
        assert session.scalar(select(func.count()).select_from(ReplayCommand)) == 0
        assert session.scalar(select(func.count()).select_from(ReplayPlayer)) == 0
        assert session.scalar(
            select(func.count()).select_from(EvidenceItem).where(EvidenceItem.parser_run_id == parser_run.id)
        ) == 0
        assert set(
            session.scalars(
                select(ReplayQualityIssue.issue_code).where(
                    ReplayQualityIssue.parser_run_id == parser_run.id
                )
            )
        ) == {"parser_failure"}


@pytest.mark.parametrize("family", ["production_economy", "combat"])
def test_handler_failed_parser_and_succeeded_telemetry_imports_with_null_player_links(
    family: str,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    clock: MutableClock,
) -> None:
    """Catch a failed parser shell being injected as authority for independently valid dependency evidence."""
    replay_sha256 = _replay(session_factory, settings, f"failed-parser-valid-telemetry-{family}")
    root = settings.data_root / "runs" / f"failed-parser-valid-telemetry-{family}"
    root.mkdir(parents=True)
    if family == "production_economy":
        run_id = "823e4567-e89b-12d3-a456-426614174000"
        trace = _valid_economy_trace(root, "trace.ndjson")
    else:
        run_id = "a23e4567-e89b-12d3-a456-426614174000"
        trace = _valid_combat_trace(root).rename(root / "trace.ndjson")
    bundle = load_validated_telemetry_bundle(trace)
    attempt = _attempt(session_factory, settings, trace, run_id)
    handler = ObservationImportHandler(
        _identity_parser_importer(
            session_factory,
            settings.data_root,
            parser=parse_replay,
            parser_version="test-parser-1",
            schema_version=1,
            clock=clock,
            uuid_factory=DeterministicUUIDs(80_000),
        ),
        TelemetryObservationImporter(
            session_factory,
            settings.data_root,
            clock=clock,
            uuid_factory=DeterministicUUIDs(81_000),
        ),
    )
    parse_dependency = StageDependencyOutput(
        PARSE_JOB_PUBLIC_ID,
        "parse",
        "1",
        None,
        status="failed",
        error_code="parser_failed",
        error_message="parser rejected mixed-evidence fixture",
        error_details={"exception_type": "ValueError"},
    )
    telemetry_dependency = StageDependencyOutput(
        TELEMETRY_JOB_PUBLIC_ID,
        "telemetry",
        "1",
        {
            "run_id": attempt.run_id,
            "runner_status": attempt.runner_status,
            "replay_quality": attempt.replay_quality,
            "strategy_analysis_scope": attempt.strategy_analysis_scope,
            "exit_code": attempt.process_exit_code,
            "engine_build": attempt.engine_build,
            "engine_executable_sha256": attempt.engine_executable_sha256,
            "diagnostics": attempt.diagnostics,
            "artifacts": tuple(
                {
                    "asset_public_id": artifact.asset_public_id,
                    "kind": artifact.kind,
                    "logical_path": artifact.logical_path,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                }
                for artifact in attempt.artifacts
            ),
        },
    )
    context = StageExecutionContext(
        "00000000-0000-0000-0000-000000000103",
        "import_observations:1:mixed-evidence",
        "00000000-0000-0000-0000-000000000104",
        replay_sha256,
        "import_observations",
        IMPORT_OBSERVATIONS_VERSION,
        {
            "branch_recipe": {
                "parse": {"parser_version": "test-parser-1"},
            }
        },
        (parse_dependency, telemetry_dependency),
    )

    result = handler(context)

    assert result == {
        "idempotency_key": "import_observations:1:mixed-evidence",
        "parser_run_id": cast(str, result["parser_run_id"]),
        "parser_command_count": 0,
        "telemetry_run_id": run_id,
        "telemetry_event_count": len(bundle.records),
    }
    with session_factory() as session:
        parser_run = session.scalar(select(ParserRun))
        telemetry_run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        replay = session.scalar(select(Replay))
        assert parser_run is not None and parser_run.status == "failed"
        assert telemetry_run is not None and telemetry_run.status == "succeeded"
        assert telemetry_run.settings_json["parser_run_id"] is None
        expected_lifecycle = "engine_verified" if family == "production_economy" else "desynced"
        assert replay is not None and replay.lifecycle_state == expected_lifecycle
        assert session.scalar(
            select(func.count(TelemetryEvent.id)).where(TelemetryEvent.telemetry_run_id == telemetry_run.id)
        ) == len(bundle.records)
        production = list(
            session.scalars(
                select(ProductionEvent).where(ProductionEvent.telemetry_run_id == telemetry_run.id)
            )
        )
        economy = list(
            session.scalars(select(EconomyEvent).where(EconomyEvent.telemetry_run_id == telemetry_run.id))
        )
        combat = list(
            session.scalars(select(CombatEvent).where(CombatEvent.telemetry_run_id == telemetry_run.id))
        )
        assert bool(production) is (family == "production_economy")
        assert bool(economy) is (family == "production_economy")
        assert bool(combat) is (family == "combat")
        assert all(event.replay_player_id is None for event in (*production, *economy))
        assert all(
            event.attacker_replay_player_id is None and event.victim_replay_player_id is None
            for event in combat
        )


def test_parser_failure_handler_replay_after_lease_expiry_reuses_exact_attempt(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a crash after failed-parser persistence creating duplicate attempt history on lease replay."""
    contexts: list[StageExecutionContext] = []

    def failing_parser(_path: Path) -> object:
        raise ValueError("parser rejected crash-window fixture")

    handler = ObservationImportHandler(
        _identity_parser_importer(
            session_factory,
            settings.data_root,
            parser=parse_replay,
            parser_version="test-parser-1",
            schema_version=1,
            clock=clock,
            uuid_factory=DeterministicUUIDs(51_000),
        ),
        TelemetryObservationImporter(
            session_factory,
            settings.data_root,
            clock=clock,
            uuid_factory=DeterministicUUIDs(52_000),
        ),
    )

    def capture(context: StageExecutionContext) -> dict[str, object]:
        contexts.append(context)
        return dict(handler(context))

    service = ImportService(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        parser=failing_parser,  # type: ignore[arg-type]
        clock=clock,
        parser_version="test-parser-1",
        telemetry_acquirer_version="unused-acquirer-1",
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations",
                IMPORT_OBSERVATIONS_VERSION,
                capture,
                terminal_dependency_policy=TerminalDependencyPolicy(failed_stages=frozenset({"parse"})),
            ),
        ),
    )
    original_succeed = service._jobs.succeed
    crashed = False

    def crash_before_settlement(
        job_public_id: str,
        worker_id: str,
        output_json: dict[str, object],
    ) -> object:
        nonlocal crashed
        snapshot = service._jobs.snapshot(job_public_id)
        if snapshot.stage == "import_observations" and not crashed:
            crashed = True
            raise RuntimeError("simulated crash before job settlement")
        return original_succeed(job_public_id, worker_id, output_json)

    monkeypatch.setattr(service._jobs, "succeed", crash_before_settlement)
    service.submit(ImportRequest(replay_file))
    with pytest.raises(RuntimeError, match="simulated crash"):
        service.run_available("crashing-observation-worker", limit=10)

    with session_factory() as session:
        import_job = session.scalar(select(Job).where(Job.stage == "import_observations"))
        parser_runs = list(session.scalars(select(ParserRun)))
        assert import_job is not None and import_job.status == "running"
        assert len(parser_runs) == 1 and parser_runs[0].status == "failed"
        assert parser_runs[0].error_json["import_observations_idempotency_key"] == import_job.idempotency_key
        first_run_id = parser_runs[0].run_id
        first_key = import_job.idempotency_key

    monkeypatch.setattr(service._jobs, "succeed", original_succeed)
    clock.advance(seconds=301)
    assert service.run_available("restarted-observation-worker", limit=10) == ()
    clock.advance(seconds=5)
    settled = service.run_available("restarted-observation-worker", limit=10)
    assert len(settled) == 1
    assert (settled[0].stage, settled[0].status, settled[0].attempt_count) == (
        "import_observations",
        "succeeded",
        2,
    )
    assert len(contexts) == 2 and contexts[0].idempotency_key == contexts[1].idempotency_key == first_key

    with session_factory() as session:
        import_job = session.scalar(select(Job).where(Job.stage == "import_observations"))
        parser_runs = list(session.scalars(select(ParserRun).order_by(ParserRun.id)))
        assert import_job is not None and import_job.output_json["parser_run_id"] == first_run_id
        assert [run.run_id for run in parser_runs] == [first_run_id]

    changed_key = replace(contexts[-1], idempotency_key=f"{first_key}:changed")
    changed_key_result = handler(changed_key)
    assert changed_key_result["parser_run_id"] != first_run_id
    dependency = contexts[-1].dependencies[0]
    changed_evidence = replace(
        contexts[-1],
        dependencies=(replace(dependency, error_message="changed parser evidence"),),
    )
    changed_evidence_result = handler(changed_evidence)
    assert changed_evidence_result["parser_run_id"] not in {
        first_run_id,
        changed_key_result["parser_run_id"],
    }
    with session_factory() as session:
        parser_runs = list(session.scalars(select(ParserRun).order_by(ParserRun.id)))
        assert len(parser_runs) == 3
        assert all(run.status == "failed" for run in parser_runs)
        assert session.scalar(select(func.count()).select_from(ReplayCommand)) == 0
        assert session.scalar(select(func.count()).select_from(ReplayPlayer)) == 0
        assert session.scalar(select(func.count()).select_from(EvidenceItem).where(EvidenceItem.parser_run_id.is_not(None))) == 0


def test_telemetry_failure_handler_replay_after_lease_expiry_reuses_exact_attempt(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a crash after failed-telemetry persistence turning the same job replay into a UUID collision."""
    run_id = "d23e4567-e89b-12d3-a456-426614174000"
    contexts: list[StageExecutionContext] = []
    artifact_root = tmp_path / "lease-replay-telemetry"
    artifact_root.mkdir()
    stdout = artifact_root / "stdout.log"
    stdout.write_text("failed engine output", encoding="utf-8")
    artifact = TelemetryArtifact(
        run_id=run_id,
        runner_status="invalid_trace",
        replay_quality="failed",
        strategy_analysis_scope="none",
        trace_path=None,
        catalog_path=None,
        map_asset_paths=(),
        outcome_path=None,
        stdout_path=stdout,
        stderr_path=None,
        exit_code=7,
        engine_build=ENGINE_IDENTITY,
        engine_executable_sha256="f" * 64,
        diagnostics=(AcquisitionDiagnostic("invalid_trace", "trace was rejected"),),
    )

    class FailedAcquirer:
        def acquire(self, replay: Path, replay_sha256: str) -> TelemetryArtifact:
            assert replay.is_file() and len(replay_sha256) == 64
            return artifact

    handler = ObservationImportHandler(
        _identity_parser_importer(
            session_factory,
            settings.data_root,
            parser=parse_replay,
            parser_version="test-parser-1",
            schema_version=1,
            clock=clock,
            uuid_factory=DeterministicUUIDs(53_000),
        ),
        TelemetryObservationImporter(
            session_factory,
            settings.data_root,
            clock=clock,
            uuid_factory=DeterministicUUIDs(54_000),
        ),
    )

    def capture(context: StageExecutionContext) -> dict[str, object]:
        contexts.append(context)
        return dict(handler(context))

    service = ImportService(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        parser=parse_replay,
        telemetry_acquirer=FailedAcquirer(),
        clock=clock,
        parser_version="test-parser-1",
        telemetry_acquirer_version="lease-replay-acquirer-1",
        stage_handlers=(
            StageHandlerRegistration(
                "import_observations",
                IMPORT_OBSERVATIONS_VERSION,
                capture,
                terminal_dependency_policy=TerminalDependencyPolicy(
                    failed_stages=frozenset({"parse", "telemetry"})
                ),
            ),
        ),
    )
    original_succeed = service._jobs.succeed
    crashed = False

    def crash_before_settlement(
        job_public_id: str,
        worker_id: str,
        output_json: dict[str, object],
    ) -> object:
        nonlocal crashed
        snapshot = service._jobs.snapshot(job_public_id)
        if snapshot.stage == "import_observations" and not crashed:
            crashed = True
            raise RuntimeError("simulated crash before job settlement")
        return original_succeed(job_public_id, worker_id, output_json)

    monkeypatch.setattr(service._jobs, "succeed", crash_before_settlement)
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    with pytest.raises(RuntimeError, match="simulated crash"):
        service.run_available("crashing-observation-worker", limit=10)

    with session_factory() as session:
        import_job = session.scalar(select(Job).where(Job.stage == "import_observations"))
        telemetry_runs = list(session.scalars(select(TelemetryRun)))
        assert import_job is not None and import_job.status == "running"
        assert len(telemetry_runs) == 1 and telemetry_runs[0].status == "failed"
        assert telemetry_runs[0].settings_json["import_observations_idempotency_key"] == import_job.idempotency_key
        first_key = import_job.idempotency_key

    monkeypatch.setattr(service._jobs, "succeed", original_succeed)
    clock.advance(seconds=301)
    assert service.run_available("restarted-observation-worker", limit=10) == ()
    clock.advance(seconds=5)
    settled = service.run_available("restarted-observation-worker", limit=10)
    assert len(settled) == 1
    assert (settled[0].stage, settled[0].status, settled[0].attempt_count) == (
        "import_observations",
        "succeeded",
        2,
    )
    assert len(contexts) == 2 and contexts[0].idempotency_key == contexts[1].idempotency_key == first_key

    def assert_nonretryable_collision(context: StageExecutionContext) -> None:
        with pytest.raises(StageFailure) as failure:
            handler(context)
        assert failure.value.code == "telemetry_import_collision" and failure.value.retryable is False

    assert_nonretryable_collision(replace(contexts[-1], idempotency_key=f"{first_key}:changed"))
    telemetry_dependency = next(item for item in contexts[-1].dependencies if item.stage == "telemetry")
    changed_message_dependency = replace(
        telemetry_dependency,
        error_message="changed terminal failure evidence",
    )
    changed_message_context = replace(
        contexts[-1],
        dependencies=tuple(
            changed_message_dependency if item.stage == "telemetry" else item
            for item in contexts[-1].dependencies
        ),
    )
    assert_nonretryable_collision(changed_message_context)
    assert telemetry_dependency.error_details is not None
    changed_details = dict(telemetry_dependency.error_details)
    diagnostics = changed_details["diagnostics"]
    assert isinstance(diagnostics, tuple)
    changed_details["diagnostics"] = (*diagnostics, {"code": "changed", "message": "changed evidence"})
    changed_dependency = replace(telemetry_dependency, error_details=changed_details)
    changed_context = replace(
        contexts[-1],
        dependencies=tuple(
            changed_dependency if item.stage == "telemetry" else item
            for item in contexts[-1].dependencies
        ),
    )
    assert_nonretryable_collision(changed_context)

    with session_factory.begin() as session:
        issue = session.scalar(
            select(ReplayQualityIssue).where(ReplayQualityIssue.telemetry_run_id == telemetry_runs[0].id)
        )
        assert issue is not None
        issue.details_json = {"runner_status": "tampered"}
    assert_nonretryable_collision(contexts[-1])

    with session_factory() as session:
        import_job = session.scalar(select(Job).where(Job.stage == "import_observations"))
        telemetry_runs = list(session.scalars(select(TelemetryRun)))
        assert import_job is not None and import_job.output_json["telemetry_run_id"] == run_id
        assert len(telemetry_runs) == 1 and telemetry_runs[0].run_id == run_id
        telemetry_run_id = telemetry_runs[0].id
        assert session.scalar(
            select(func.count()).select_from(EvidenceItem).where(EvidenceItem.telemetry_run_id == telemetry_run_id)
        ) == 0
        for model in (TelemetryEvent, Entity, EntitySample, ProductionEvent, EconomyEvent, CombatEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_exact_retry_recovers_childless_running_telemetry_shell(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch a killed importer leaving an exact running shell that permanently collides on retry."""
    replay_sha256 = _replay(session_factory, settings, "childless-running-retry")
    run_id = "e23e4567-e89b-12d3-a456-426614174000"
    trace = _write_v1_bundle(settings.data_root / "childless-running" / run_id, run_id)
    attempt = _attempt(session_factory, settings, trace, run_id)
    importer = _importer(session_factory, settings)
    importer._create_attempt(replay_sha256, attempt, NOW, "stable-import-key")

    result = importer.import_replay(replay_sha256, attempt, idempotency_key="stable-import-key")

    assert result.status == "succeeded" and result.cache_hit is False
    with session_factory() as session:
        runs = list(session.scalars(select(TelemetryRun).where(TelemetryRun.run_id == run_id)))
        assert len(runs) == 1 and runs[0].status == "succeeded"


def test_running_telemetry_shell_with_any_child_is_an_unsafe_collision(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch partial telemetry graphs being deleted or appended to during retry recovery."""
    replay_sha256 = _replay(session_factory, settings, "partial-running-collision")
    run_id = "f23e4567-e89b-12d3-a456-426614174000"
    trace = _write_v1_bundle(settings.data_root / "partial-running" / run_id, run_id)
    attempt = _attempt(session_factory, settings, trace, run_id)
    importer = _importer(session_factory, settings)
    importer._create_attempt(replay_sha256, attempt, NOW, "stable-import-key")
    with session_factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert replay is not None and run is not None
        session.add(
            EvidenceItem(
                public_id="f33e4567-e89b-12d3-a456-426614174000",
                replay_id=replay.id,
                parser_run_id=None,
                telemetry_run_id=run.id,
                tier="observed",
                source_kind="telemetry_event",
                source_key=f"telemetry:{run_id}:sequence:0",
                schema_version=1,
                created_at=NOW,
            )
        )

    with pytest.raises(ValueError, match="collides"):
        importer.import_replay(replay_sha256, attempt, idempotency_key="stable-import-key")

    with session_factory() as session:
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert run is not None and run.status == "running"
        assert session.scalar(select(func.count(EvidenceItem.id)).where(EvidenceItem.telemetry_run_id == run.id)) == 1


def test_large_valid_trace_is_persisted_in_bounded_batches(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch large imports rebuilding one trace-sized identity/raw/payload/ORM collection."""
    replay_sha256 = _replay(session_factory, settings, "bounded-large-trace")
    run_id = "a33e4567-e89b-12d3-a456-426614174000"
    records = [
        _record(
            1,
            run_id,
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
    for sequence in range(1, 1_251):
        records.append(
            _record(
                1,
                run_id,
                sequence,
                "cash_changed",
                {
                    "player_index": 0,
                    "before": sequence,
                    "delta": 1,
                    "after": sequence + 1,
                    "track_income": False,
                    "reason": "synthetic_load",
                },
            )
        )
    records.append(_completion(1, run_id, records))
    trace = _write_records(settings.data_root / "bounded-large" / run_id / "trace.ndjson", records)
    attempt = _attempt(session_factory, settings, trace, run_id)
    canonical = telemetry_import_module.telemetry_event_evidence_identities
    batch_sizes: list[int] = []

    def measured_identities(public_id: object, sequences: object):  # type: ignore[no-untyped-def]
        materialized = tuple(cast(Any, sequences))
        batch_sizes.append(len(materialized))
        return canonical(public_id, materialized)

    monkeypatch.setattr(telemetry_import_module, "telemetry_event_evidence_identities", measured_identities)
    result = _importer(session_factory, settings).import_replay(replay_sha256, attempt)

    assert result.status == "succeeded" and result.event_count == 1_252
    assert batch_sizes == [500, 500, 252]


def test_seeded_map_feature_permutations_are_semantically_canonical_and_duplicates_fail(
    tmp_path: Path,
) -> None:
    """Catch insertion order entering map payloads; seed 0x4A11CE reproduces failures."""
    seed = 0x4A11CE
    run_id = "923e4567-e89b-12d3-a456-426614174000"
    bundle = load_validated_telemetry_bundle(_write_v2_bundle(tmp_path / "bundle", run_id))
    assert bundle.map_asset is not None
    waypoints = (
        WaypointFeature(
            bidirectional=True,
            bounds_policy="pathfinder_xy_closed",
            labels=["path"],
            link_names=["Bravo"],
            link_waypoint_ids=[2],
            name="Alpha",
            position=Position3(x=10.0, y=20.0, z=0.0),
            waypoint_id=1,
        ),
        WaypointFeature(
            bidirectional=True,
            bounds_policy="pathfinder_xy_closed",
            labels=["path"],
            link_names=["Alpha"],
            link_waypoint_ids=[1],
            name="Bravo",
            position=Position3(x=30.0, y=40.0, z=0.0),
            waypoint_id=2,
        ),
    )
    bridges = tuple(
        BridgeFeature.model_validate(
            {
                "bounds_policy": "pathfinder_xy_closed",
                "bridge_index": index,
                "bridge_width": 10.0,
                "category_source": "TerrainLogic::getFirstBridge",
                "corners": [
                    {"x": float(index + offset), "y": float(offset), "z": 0.0} for offset in range(4)
                ],
                "from": {"x": float(index), "y": 0.0, "z": 0.0},
                "layer_id": 1,
                "object_id": 100 + index,
                "template_name": "MapBridge",
                "to": {"x": float(index + 3), "y": 3.0, "z": 0.0},
            }
        )
        for index in (1, 2)
    )
    enriched = bundle.map_asset.model_copy(update={"waypoints": waypoints, "bridges": bridges})
    baseline = normalize_map_asset(enriched)
    randomizer = random.Random(seed)
    for _ in range(100):
        shuffled_waypoints = list(waypoints)
        shuffled_bridges = list(bridges)
        randomizer.shuffle(shuffled_waypoints)
        randomizer.shuffle(shuffled_bridges)
        projection = normalize_map_asset(
            enriched.model_copy(update={"waypoints": tuple(shuffled_waypoints), "bridges": tuple(shuffled_bridges)})
        )
        assert (projection.metadata, projection.resources, projection.regions) == (
            baseline.metadata,
            baseline.resources,
            baseline.regions,
        )
    assert [region.stable_key for region in baseline.regions] == [
        "bridge:1",
        "bridge:2",
        "waypoint:1",
        "waypoint:2",
    ]
    with pytest.raises(ValueError, match="duplicate semantic map stable key"):
        normalize_map_asset(enriched.model_copy(update={"waypoints": (waypoints[0], waypoints[0])}))


def test_hundred_persisted_telemetry_key_permutations_have_byte_identical_canonical_projections(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch JSON member presentation order entering any persisted raw-family projection; seed 0x4A11CE."""
    randomizer = random.Random(0x4A11CE)
    baseline: tuple[str, ...] | None = None

    def permute(value: object) -> object:
        if isinstance(value, dict):
            items = list(value.items())
            randomizer.shuffle(items)
            return {key: permute(item) for key, item in items}
        if isinstance(value, list):
            return [permute(item) for item in value]
        return value

    for index in range(100):
        run_id = str(UUID(int=100_000 + index))
        replay_sha256 = _replay(session_factory, settings, f"telemetry-permutation-{index:03}")
        trace = _write_v1_bundle(settings.data_root / "permutations" / run_id, run_id)
        records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        records = [permute(record) for record in records]
        assert all(isinstance(record, dict) for record in records)
        prior = b"".join(json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records[:-1])
        records[-1]["payload"]["trace_sha256"] = hashlib.sha256(prior).hexdigest()  # type: ignore[index]
        _write_records(trace, records)  # type: ignore[arg-type]
        result = _importer(session_factory, settings, 120_000 + index * 100).import_replay(
            replay_sha256,
            _attempt(session_factory, settings, trace, run_id),
        )
        assert result.status == "succeeded"
        with session_factory() as session:
            run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
            assert run is not None
            projection = tuple(
                canonical_json(event.payload_json)
                for event in session.scalars(
                    select(TelemetryEvent)
                    .where(
                        TelemetryEvent.telemetry_run_id == run.id,
                        TelemetryEvent.event_type.not_in({"manifest", "complete"}),
                    )
                    .order_by(TelemetryEvent.sequence)
                )
            )
        baseline = projection if baseline is None else baseline
        assert projection == baseline

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(TelemetryRun)) == 100
        assert session.scalar(select(func.count()).select_from(TelemetryEvent)) == 700
