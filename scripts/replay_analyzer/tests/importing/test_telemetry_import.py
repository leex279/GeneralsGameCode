"""Transactional normalization tests for validated telemetry and map observations."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from map_asset_support import write_test_map_asset
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import (
    CombatEvent,
    EconomyEvent,
    Entity,
    EntitySample,
    EvidenceItem,
    ManagedAsset,
    Map,
    MapRegion,
    MapResource,
    Player,
    PlayerAlias,
    ProductionEvent,
    Replay,
    ReplayQualityIssue,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.importing.jobs import StageFailure
from generals_replay_analyzer.importing.map_import import normalize_map_asset
from generals_replay_analyzer.importing.parser_import import ParserImportResult
from generals_replay_analyzer.importing.service import StageDependencyOutput, StageExecutionContext
from generals_replay_analyzer.importing.telemetry_import import (
    ManagedTelemetryArtifact,
    ObservationImportHandler,
    TelemetryAttempt,
    TelemetryImportResult,
    TelemetryObservationImporter,
)
from generals_replay_analyzer.telemetry import load_validated_telemetry_bundle
from generals_replay_analyzer.telemetry.map_asset import BridgeFeature, Position3, WaypointFeature
from generals_replay_analyzer.telemetry.order_coverage import canonical_order_coverage

NOW = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
ENGINE_IDENTITY = "zero-hour-test-exe-00000000-ini-00000000"


class DeterministicUUIDs:
    def __init__(self, start: int = 20_000) -> None:
        self._value = start

    def __call__(self) -> UUID:
        value = UUID(int=self._value)
        self._value += 1
        return value


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


def _replay(session_factory: sessionmaker[Session], sha256: str = "a" * 64) -> str:
    with session_factory.begin() as session:
        session.add(
            Replay(
                public_id=f"00000000-0000-0000-0000-{sha256[:12]}",
                sha256=sha256,
                managed_asset_id=None,
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
    return TelemetryAttempt(
        run_id=run_id,
        runner_status="success",
        replay_quality="complete",
        strategy_analysis_scope="full",
        process_exit_code=0,
        engine_build=ENGINE_IDENTITY if trace.name == "trace.ndjson" else "historical-build",
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


def test_v2_import_uses_validated_map_and_stable_raw_event_evidence(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch map inference, path identity leakage, or duplicate telemetry graphs."""
    replay_sha256 = _replay(session_factory)
    run_id = "123e4567-e89b-12d3-a456-426614174000"
    trace = _write_v2_bundle(settings.data_root / "runs" / run_id, run_id)
    assert len(load_validated_telemetry_bundle(trace).records) == 6
    attempt = _attempt(session_factory, settings, trace, run_id)
    importer = _importer(session_factory, settings)

    result = importer.import_replay(replay_sha256, attempt)
    cached = importer.import_replay(replay_sha256, attempt)
    with pytest.raises(ValueError, match="collides with another immutable attempt"):
        importer.import_replay(replay_sha256, replace(attempt, replay_quality="partial"))
    assert result.status == "succeeded" and result.event_count == 6, _run_diagnostics(session_factory, run_id)
    assert cached.run_id == run_id and cached.cache_hit is True
    with session_factory() as session:
        events = list(session.scalars(select(TelemetryEvent).order_by(TelemetryEvent.sequence)))
        evidence = list(session.scalars(select(EvidenceItem).order_by(EvidenceItem.source_key)))
        map_row = session.scalar(select(Map))
        resources = list(session.scalars(select(MapResource).order_by(MapResource.stable_key)))
        assert [event.sequence for event in events] == [0, 1, 2, 3, 4, 5]
        assert events[0].raw_record_json["event_type"] == "manifest"
        assert {item.source_key for item in evidence} == {
            f"telemetry:{run_id}:sequence:{sequence}" for sequence in range(6)
        }
        assert map_row is not None and map_row.content_sha256 == events[0].payload_json["map_asset"]["content_sha256"]
        assert [row.stable_key for row in resources] == ["start:0", "static:77"]
        assert resources[1].x == 4.0 and resources[1].payload_json["template_name"] == "SupplyDock"
        assert session.scalar(select(func.count(MapRegion.id))) == 0
        assert session.scalar(select(func.count(Player.id))) == 0
        assert session.scalar(select(func.count(PlayerAlias.id))) == 0
        replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
        assert replay is not None and replay.lifecycle_state == "engine_verified" and replay.map_id == map_row.id


def test_v1_import_preserves_direct_entity_and_event_family_fields_without_map(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch v2-only fabrication or loss of historical direct event-family facts."""
    replay_sha256 = _replay(session_factory, "c" * 64)
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
        assert sample is not None and (sample.x, sample.y, sample.z, sample.speed) == (2.0, 3.0, 4.0, 5.0)
        assert production is not None and production.producer_entity_id == entity.id and production.item_name == "Tank"
        assert economy is not None and (economy.balance_before, economy.amount_delta, economy.balance_after) == (1000, -100, 900)
        assert combat is not None and combat.victim_entity_id == entity.id and combat.applied_amount == 10.0
        assert session.scalar(select(func.count(Map.id))) == 0


def test_invalid_managed_trace_fails_atomically_while_retaining_attempt_and_assets(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch validation failures that leak even one telemetry observation child."""
    replay_sha256 = _replay(session_factory, "d" * 64)
    run_id = "323e4567-e89b-12d3-a456-426614174000"
    trace = _write_v1_bundle(settings.data_root / "runs" / run_id, run_id)
    attempt = _attempt(session_factory, settings, trace, run_id)
    trace.write_bytes(trace.read_bytes() + b"corrupt")

    result = _importer(session_factory, settings).import_replay(replay_sha256, attempt)
    assert result.status == "failed" and result.event_count == 0
    with session_factory() as session:
        run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
        assert run is not None and run.status == "failed" and run.trace_asset_id is not None
        assert session.scalar(select(func.count(TelemetryEvent.id)).where(TelemetryEvent.telemetry_run_id == run.id)) == 0
        assert session.scalar(select(func.count(Entity.id)).where(Entity.telemetry_run_id == run.id)) == 0
        issue = session.scalar(select(ReplayQualityIssue).where(ReplayQualityIssue.telemetry_run_id == run.id))
        assert issue is not None and issue.issue_code == "invalid_trace"


def test_runner_failure_without_trace_retains_metadata_without_observations(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch a runner failure being discarded merely because no trace was produced."""
    replay_sha256 = _replay(session_factory, "1" * 64)
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


def test_registered_descriptor_mismatch_retains_failed_shell_and_registered_asset(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch descriptor validation happening before durable failed-attempt creation."""
    replay_sha256 = _replay(session_factory, "2" * 64)
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
    replay_sha256 = _replay(session_factory, "3" * 64)
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
    replay_sha256 = _replay(session_factory, "4" * 64)
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
    replay_sha256 = _replay(session_factory, "6" * 64)
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
    replay_sha256 = _replay(session_factory, "5" * 64)
    run_id = "c23e4567-e89b-12d3-a456-426614174000"
    trace = _write_v1_bundle(settings.data_root / "runs" / run_id, run_id)
    attempt = _attempt(session_factory, settings, trace, run_id)
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    records[2]["payload"]["object_id"] = 999
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
        _replay(session_factory, replay_sha256)
        trace = _write_v1_bundle(settings.data_root / "runs" / run_id, run_id, **terminal)
        result = _importer(session_factory, settings, 40_000 + index * 1_000).import_replay(
            replay_sha256, _attempt(session_factory, settings, trace, run_id)
        )
        assert result.status == "succeeded" and result.event_count == 7, _run_diagnostics(session_factory, run_id)
        with session_factory() as session:
            replay = session.scalar(select(Replay).where(Replay.sha256 == replay_sha256))
            run = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == run_id))
            issue = session.scalar(select(ReplayQualityIssue).where(ReplayQualityIssue.telemetry_run_id == run.id))
            assert replay is not None and replay.lifecycle_state == lifecycle
            assert issue is not None and issue.issue_code == issue_code


def test_unsupported_lifecycle_precedes_later_crc_quality_evidence(
    session_factory: sessionmaker[Session], settings: AnalyzerSettings
) -> None:
    """Catch terminal quality overwriting a higher-precedence accepted lifecycle fact."""
    replay_sha256 = _replay(session_factory, "7" * 64)
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
            self.calls: list[tuple[str, str]] = []

        def import_replay(self, replay_sha256: str, *, parser_version: str) -> ParserImportResult:
            self.calls.append((replay_sha256, parser_version))
            return ParserImportResult("parser-run", "succeeded", "complete", 9, False)

    class TelemetryStub:
        def __init__(self) -> None:
            self.attempts: list[TelemetryAttempt] = []

        def import_replay(self, replay_sha256: str, attempt: TelemetryAttempt) -> TelemetryImportResult:
            assert replay_sha256 == "a" * 64
            self.attempts.append(attempt)
            return TelemetryImportResult(attempt.run_id, "succeeded", 7, False)

    parser = ParserStub()
    telemetry = TelemetryStub()
    handler = ObservationImportHandler(parser, telemetry)  # type: ignore[arg-type]
    parse_dependency = StageDependencyOutput("parse-job", "parse", "1", {"parser_version": "parser-v1"})
    parser_only = StageExecutionContext(
        "import-job",
        "import_observations:1:key",
        "replay-public-id",
        "a" * 64,
        "import_observations",
        "1",
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
        "telemetry-job",
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
            "diagnostics": ({"code": "ok"},),
            "artifacts": (descriptor,),
        },
    )
    with_telemetry = StageExecutionContext(
        "import-job",
        "import_observations:1:key",
        "replay-public-id",
        "a" * 64,
        "import_observations",
        "1",
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
            diagnostics=({"code": "ok"},),
            artifacts=(
                ManagedTelemetryArtifact(
                    "00000000-0000-0000-0000-000000000001",
                    "telemetry_trace",
                    "trace.ndjson",
                    "b" * 64,
                    123,
                ),
            ),
        )
    ]

    missing_parse = StageExecutionContext(
        "import-job", "key", "replay-public-id", "a" * 64, "import_observations", "1", {}, ()
    )
    with pytest.raises(StageFailure, match="parser dependency output is missing"):
        handler(missing_parse)
    invalid_parse = StageExecutionContext(
        "import-job",
        "key",
        "replay-public-id",
        "a" * 64,
        "import_observations",
        "1",
        {},
        (StageDependencyOutput("parse-job", "parse", "1", {"parser_version": 1}),),
    )
    with pytest.raises(StageFailure, match="parser dependency version is invalid"):
        handler(invalid_parse)
    incomplete_telemetry = StageExecutionContext(
        "import-job",
        "key",
        "replay-public-id",
        "a" * 64,
        "import_observations",
        "1",
        {},
        (
            parse_dependency,
            StageDependencyOutput(
                "telemetry-job",
                "telemetry",
                "1",
                {"artifacts": ({"asset_public_id": "missing-fields"},)},
            ),
        ),
    )
    with pytest.raises(StageFailure, match="telemetry artifact descriptor is incomplete"):
        handler(incomplete_telemetry)


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
