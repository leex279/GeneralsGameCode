from __future__ import annotations

import json
import os
import random
import struct
import subprocess
import sys
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import select

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.models import (
    CombatEvent,
    Entity,
    EntitySample,
    EvidenceItem,
    ManagedAsset,
    Map,
    MapResource,
    ParserRun,
    Replay,
    ReplayPlayer,
    Report,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.features.evidence import thaw_canonical
from generals_replay_analyzer.report.query import FixedReportQuery, ReportGraphNotFoundError
from generals_replay_analyzer.spatial.query import (
    MapRasterReadQuery,
    MapSceneContractError,
    MapSceneIndexReadQuery,
    MapSceneQueryService,
    MapSceneReadQuery,
    RasterUnavailableError,
    SceneSample,
    downsample_samples,
)


def _uuid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"map-query-test:{label}"))


def _sample(index: int, entity: str, frame: int, reason: str = "changed") -> SceneSample:
    return SceneSample(
        sample_public_id=_uuid(f"sample:{index}"),
        entity_public_id=_uuid(f"entity:{entity}"),
        replay_player_public_id=_uuid("player:0"),
        frame=frame,
        raw_position=(float(index), float(index + 1), 0.0),
        orientation=0.0,
        sample_reason=reason,
        locomotor_surface=None,
        evidence_public_id=_uuid(f"evidence:{index}"),
    )


def test_downsampling_retains_entity_endpoints_and_forced_events_at_integer_midpoints() -> None:
    # Break caught: dropping an endpoint/forced event or selecting float-rounded candidate ranks.
    samples = tuple(
        _sample(index, "a", index, "order_forced" if index == 2 else "changed")
        for index in range(103)
    )

    selected, result = downsample_samples(tuple(reversed(samples)), 100)

    assert tuple(item.sample_public_id for item in selected) == tuple(
        samples[index].sample_public_id for index in range(103) if index not in (18, 51, 85)
    )
    assert result == {
        "algorithm_version": "event-forced-stratified-v1",
        "requested_sample_budget": 100,
        "original_sample_count": 103,
        "mandatory_sample_count": 3,
        "returned_sample_count": 100,
        "budget_exceeded_by_mandatory": False,
    }


def test_scene_index_query_rejects_unbounded_or_empty_page_inputs() -> None:
    # Break caught: allowing a direct caller to bypass the bounded database page contract.
    with pytest.raises(ValueError):
        MapSceneIndexReadQuery(page=0)
    with pytest.raises(ValueError):
        MapSceneIndexReadQuery(page_size=101)
    with pytest.raises(ValueError):
        MapSceneIndexReadQuery(search="")


def test_downsampling_soft_cap_returns_every_mandatory_sample_without_duplicates() -> None:
    # Break caught: treating the evidence-forced budget as a hard cap or counting one sample twice.
    samples = tuple(_sample(index, "a", index, "lifecycle_forced") for index in range(101))

    selected, result = downsample_samples((*samples, samples[50]), 100)

    assert selected == samples
    assert result["original_sample_count"] == 101
    assert result["mandatory_sample_count"] == 101
    assert result["returned_sample_count"] == 101
    assert result["budget_exceeded_by_mandatory"] is True


def test_downsampling_is_stable_across_one_thousand_permutations() -> None:
    samples = tuple(
        _sample(index, str(index % 7), index, "state_forced" if index % 19 == 0 else "changed")
        for index in range(240)
    )
    expected = tuple(item.sample_public_id for item in downsample_samples(samples, 100)[0])
    generator = random.Random(20260823)

    for _ in range(1000):
        shuffled = list(samples)
        generator.shuffle(shuffled)
        assert tuple(item.sample_public_id for item in downsample_samples(shuffled, 100)[0]) == expected


def test_downsampling_is_stable_across_python_hash_seeds() -> None:
    script = """
import json
from generals_replay_analyzer.spatial.query import SceneSample, downsample_samples
from uuid import NAMESPACE_URL, uuid5
def uid(label): return str(uuid5(NAMESPACE_URL, 'hash-seed:' + label))
values = [SceneSample(uid('s'+str(i)), uid('e'+str(i % 5)), uid('p'), i, (float(i), 0.0, 0.0), 0.0, 'state_forced' if i % 17 == 0 else 'changed', None, uid('v'+str(i))) for i in range(180)]
print(json.dumps([item.sample_public_id for item in downsample_samples(tuple(set(values)), 100)[0]]))
"""
    outputs = []
    for seed in ("1", "77", "999"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        outputs.append(json.loads(completed.stdout))
    assert outputs[0] == outputs[1] == outputs[2]


@dataclass(frozen=True)
class _FakeDocument:
    report_public_id: str
    report_version: str
    replay_public_id: str
    replay_player_public_id: str | None
    lifecycle: object
    quality_issues: tuple[object, ...]
    evidence_availability: tuple[object, ...]
    observed: tuple[object, ...]
    derived: tuple[object, ...]
    inferred: tuple[object, ...]


class _ReportAuthority:
    def __init__(self, replay_id: str, report_id: str, evidence_ids: tuple[str, ...], player_id: str) -> None:
        references = tuple(SimpleNamespace(public_id=value, tier="observed") for value in evidence_ids)
        value = SimpleNamespace(evidence=references)
        document = _FakeDocument(
            report_id,
            "replay-report-v1",
            replay_id,
            None,
            SimpleNamespace(lifecycle_state="engine_verified", telemetry_runner_status="success"),
            (),
            (value,),
            (),
            (),
            (),
        )
        self.graph = SimpleNamespace(
            selected=SimpleNamespace(document=document),
            identity=SimpleNamespace(players=(SimpleNamespace(public_id=player_id, display_name="Alice"),)),
        )

    def get_report(self, query: FixedReportQuery) -> object:
        if (query.replay_public_id, query.report_public_id) != (
            self.graph.selected.document.replay_public_id,
            self.graph.selected.document.report_public_id,
        ):
            raise ReportGraphNotFoundError("missing")
        return self.graph


class _CountingReportAuthority:
    def __init__(self, replay_id: str, evidence_ids: tuple[str, ...], player_id: str) -> None:
        self.replay_id = replay_id
        self.evidence_ids = evidence_ids
        self.player_id = player_id
        self.calls: list[FixedReportQuery] = []

    def get_report(self, query: FixedReportQuery) -> object:
        self.calls.append(query)
        if query.replay_public_id != self.replay_id:
            raise ReportGraphNotFoundError("missing")
        return _ReportAuthority(
            query.replay_public_id,
            query.report_public_id,
            self.evidence_ids,
            self.player_id,
        ).graph


def _seed_query_service(
    tmp_path: Path,
    *,
    combat_x: float = 10.0,
    out_of_bounds_sample_x: float = 25.0,
    visibility_x: float = 5.0,
    duplicate_manifest: bool = False,
    map_display_name: str = "Tournament Desert",
    corrupt_projection: bool = False,
    engine_native_map_events: bool = False,
) -> tuple[MapSceneQueryService, dict[str, str]]:
    settings = AnalyzerSettings(data_root=tmp_path / "map-query-data")
    settings.ensure_directories()
    upgrade_database(settings.database_path)
    engine = create_database_engine(settings.database_path)
    factory = create_session_factory(engine)
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    ids = {name: _uuid(name) for name in ("replay", "report", "player:0", "parser", "telemetry", "map")}
    manifest_evidence_id = _uuid("manifest-evidence")
    sample_evidence_id = _uuid("sample-evidence")
    combat_evidence_id = _uuid("combat-evidence")
    oob_evidence_id = _uuid("oob-evidence")
    duplicate_manifest_evidence_id = _uuid("duplicate-manifest-evidence")
    visibility_evidence_id = _uuid("visibility-evidence")
    players_initialized_evidence_id = _uuid("players-initialized-evidence")
    visibility_summary_evidence_id = _uuid("visibility-summary-evidence")
    heuristic_evidence_id = _uuid("heuristic-evidence")
    manifest_asset_id = _uuid("manifest-asset")
    projection = {
        "amphibious_passable": [True, True, True, True],
        "content_sha256": "d" * 64,
        "engine_data_identity": "zh-1.04-catalog",
        "ground_passable": [True, False, True, True],
        "map_identity": "maps/tournament-desert.map",
        "pathing": {
            "bounds": {
                "maximum_exclusive": {"x": 20.0, "y": 20.0},
                "minimum_inclusive": {"x": 0.0, "y": 0.0},
            },
            "cell_size": {"x": 10.0, "y": 10.0},
            "dimension_source": "validated map grid",
            "height": 2,
            "index_origin": {"x": 0, "y": 0},
            "sample_point": "cell_center",
            "storage_order": "row_major_y_then_x_x_fastest",
            "width": 2,
        },
        "schema_version": 2,
        "world_bounds": {
            "maximum": {"x": 20.0, "y": 20.0, "z": 10.0},
            "maximum_inclusive": True,
            "minimum": {"x": 0.0, "y": 0.0, "z": -10.0},
            "minimum_inclusive": True,
        },
        "zone_ids": [1, 0, 1, 1],
    }
    if corrupt_projection:
        projection["pathing"]["bounds"]["maximum_exclusive"]["x"] = 21.0
    with factory() as session:
        manifest_asset = ManagedAsset(
            public_id=manifest_asset_id,
            sha256="e" * 64,
            kind="telemetry_map_asset",
            relative_path="ignored/private/manifest.json",
            size_bytes=1,
            created_at=now,
        )
        session.add(manifest_asset)
        session.flush()
        map_row = Map(
            public_id=ids["map"],
            content_sha256="d" * 64,
            manifest_asset_id=manifest_asset.id,
            schema_version=2,
            engine_data_identity="zh-1.04-catalog",
            map_identity="maps/tournament-desert.map",
            display_name=map_display_name,
            exporter_version="zero-hour-replay-map-export-v2",
            min_x=0.0,
            min_y=0.0,
            min_z=-10.0,
            max_x=20.0,
            max_y=20.0,
            max_z=10.0,
            pathing_width=2,
            pathing_height=2,
            pathing_cell_size=10.0,
            terrain_width=2,
            terrain_height=2,
            terrain_cell_size=10.0,
            metadata_json={"validated_spatial_projection": projection},
            created_at=now,
        )
        session.add(map_row)
        session.flush()
        replay = Replay(
            public_id=ids["replay"],
            sha256="a" * 64,
            managed_asset_id=None,
            map_id=map_row.id,
            replay_name="fixture.rep",
            version_string="1.04",
            version_number=104,
            frame_count=120,
            start_time=1,
            end_time=2,
            exe_crc=1,
            ini_crc=2,
            map_crc=3,
            map_name="Tournament Desert",
            seed=4,
            starting_cash=10000,
            header_json={},
            lifecycle_state="engine_verified",
            created_at=now,
            updated_at=now,
        )
        session.add(replay)
        session.flush()
        parser = ParserRun(
            run_id=ids["parser"],
            replay_id=replay.id,
            parser_version="parser-v1",
            schema_version=1,
            input_sha256=replay.sha256,
            result_sha256="b" * 64,
            status="running",
            completion_status=None,
            warnings_json=[],
            started_at=now,
            completed_at=now,
        )
        session.add(parser)
        session.flush()
        player = ReplayPlayer(
            public_id=ids["player:0"],
            replay_id=replay.id,
            parser_run_id=parser.id,
            slot_index=0,
            slot_kind="human",
            original_name="Alice",
            normalized_name="alice",
            player_index=None,
            start_position=0,
            observed_json={},
        )
        session.add(player)
        session.flush()
        telemetry = TelemetryRun(
            run_id=ids["telemetry"],
            replay_id=replay.id,
            map_asset_id=manifest_asset.id,
            map_id=map_row.id,
            schema_version=2,
            engine_build="zh-1.04-catalog",
            engine_executable_sha256="c" * 64,
            settings_json={"parser_run_id": parser.run_id},
            status="running",
            runner_status="success",
            final_frame=120,
            command_count=0,
            trace_sha256="f" * 64,
            diagnostics_json=[],
            started_at=now,
            completed_at=now,
        )
        session.add(telemetry)
        session.flush()
        evidence_rows = []
        evidence_specs = [
            (manifest_evidence_id, 0),
            (players_initialized_evidence_id, 7),
            (sample_evidence_id, 1),
            (combat_evidence_id, 2),
            (oob_evidence_id, 3),
        ]
        if engine_native_map_events:
            evidence_specs.extend(
                (
                    (visibility_evidence_id, 4),
                    (visibility_summary_evidence_id, 5),
                    (heuristic_evidence_id, 6),
                )
            )
        if duplicate_manifest:
            evidence_specs.append((duplicate_manifest_evidence_id, 99))
        for public_id, sequence in evidence_specs:
            evidence_rows.append(
                EvidenceItem(
                    public_id=public_id,
                    replay_id=replay.id,
                    telemetry_run_id=telemetry.id,
                    tier="observed",
                    source_kind="telemetry_event",
                    source_key=f"telemetry:{telemetry.run_id}:sequence:{sequence}",
                    schema_version=2,
                    created_at=now,
                )
            )
        session.add_all(evidence_rows)
        session.flush()
        manifest_event = TelemetryEvent(
            telemetry_run_id=telemetry.id,
            sequence=0,
            frame=0,
            logic_time_seconds=0.0,
            schema_version=2,
            event_type="manifest",
            payload_json={"engine_build": telemetry.engine_build},
            raw_record_json={},
            evidence_item_id=evidence_rows[0].id,
        )
        players_initialized_event = TelemetryEvent(
            telemetry_run_id=telemetry.id,
            sequence=7,
            frame=1,
            logic_time_seconds=1 / 30.0,
            schema_version=2,
            event_type="players_initialized",
            payload_json={
                "slots": [{"slot_index": 0, "player_index": 0, "resolution_status": "resolved"}]
            },
            raw_record_json={},
            evidence_item_id=evidence_rows[1].id,
        )
        sample_event = TelemetryEvent(
            telemetry_run_id=telemetry.id,
            sequence=1,
            frame=30,
            logic_time_seconds=1.0,
            schema_version=2,
            event_type="entity_sample",
            payload_json={},
            raw_record_json={},
            evidence_item_id=evidence_rows[2].id,
        )
        combat_event = TelemetryEvent(
            telemetry_run_id=telemetry.id,
            sequence=2,
            frame=40,
            logic_time_seconds=1.33,
            schema_version=2,
            event_type="damage_applied",
            payload_json={},
            raw_record_json={},
            evidence_item_id=evidence_rows[3].id,
        )
        oob_event = TelemetryEvent(
            telemetry_run_id=telemetry.id,
            sequence=3,
            frame=35,
            logic_time_seconds=1.16,
            schema_version=2,
            event_type="entity_sample",
            payload_json={},
            raw_record_json={},
            evidence_item_id=evidence_rows[4].id,
        )
        events = [manifest_event, players_initialized_event, sample_event, combat_event, oob_event]
        if engine_native_map_events:
            events.extend(
                (
                    TelemetryEvent(
                        telemetry_run_id=telemetry.id,
                        sequence=4,
                        frame=15,
                        logic_time_seconds=0.5,
                        schema_version=2,
                        event_type="object_visibility_changed",
                        payload_json={
                            "player_index": 0,
                            "object_id": 42,
                            "template_name": "AmericaRanger",
                            "previous_status": "unseen",
                            "status": "clear",
                            "first_observed_clear": True,
                            "position": {"x": visibility_x, "y": 10.0, "z": 0.0},
                            "sampling_cycle_id": 0,
                        },
                        raw_record_json={},
                        evidence_item_id=evidence_rows[5].id,
                    ),
                    TelemetryEvent(
                        telemetry_run_id=telemetry.id,
                        sequence=5,
                        frame=15,
                        logic_time_seconds=0.5,
                        schema_version=2,
                        event_type="visibility_sampling_summary",
                        payload_json={
                            "eligible_pair_count": 9000,
                            "sampled_pair_count": 8192,
                            "maximum_pairs_per_pass": 8192,
                            "sampling_cycle_id": 0,
                            "cycle_complete": False,
                        },
                        raw_record_json={},
                        evidence_item_id=evidence_rows[6].id,
                    ),
                    TelemetryEvent(
                        telemetry_run_id=telemetry.id,
                        sequence=6,
                        frame=120,
                        logic_time_seconds=4.0,
                        schema_version=2,
                        event_type="partition_engine_grid_sample",
                        payload_json={
                            "player_index": 0,
                            "sampling_scheme": "uniform_partition_lattice_v1",
                            "grid": {"complete": False},
                            "cells": [
                                {
                                    "cell_x": 0,
                                    "cell_y": 0,
                                    "world_position": {"x": 5.0, "y": 5.0, "z": 0.0},
                                    "shroud_status": "clear",
                                    "threat_value": 12,
                                    "cash_value": 4,
                                }
                            ],
                        },
                        raw_record_json={},
                        evidence_item_id=evidence_rows[7].id,
                    ),
                )
            )
        if duplicate_manifest:
            events.append(
                TelemetryEvent(
                    telemetry_run_id=telemetry.id,
                    sequence=99,
                    frame=0,
                    logic_time_seconds=0.0,
                    schema_version=2,
                    event_type="manifest",
                    payload_json={"engine_build": telemetry.engine_build},
                    raw_record_json={},
                    evidence_item_id=next(
                        item.id for item in evidence_rows if item.public_id == duplicate_manifest_evidence_id
                    ),
                )
            )
        session.add_all(events)
        session.flush()
        entity = Entity(
            public_id=_uuid("entity-row"),
            telemetry_run_id=telemetry.id,
            replay_id=replay.id,
            object_id=42,
            template_name="AmericaRanger",
            initial_owner_player_index=0,
            kind_of_flags_json=["MOBILE"],
            creation_sequence=1,
            creation_frame=10,
            observed_json={},
        )
        session.add(entity)
        session.flush()
        session.add(
            EntitySample(
                telemetry_run_id=telemetry.id,
                entity_id=entity.id,
                telemetry_event_id=sample_event.id,
                sequence=1,
                frame=30,
                x=5.0,
                y=10.0,
                z=0.0,
                orientation=0.5,
                current_state="MOVING",
                source="engine",
                sample_reason="order_forced",
                payload_json={},
            )
        )
        session.add(
            EntitySample(
                telemetry_run_id=telemetry.id,
                entity_id=entity.id,
                telemetry_event_id=oob_event.id,
                sequence=3,
                frame=35,
                x=out_of_bounds_sample_x,
                y=10.0,
                z=0.0,
                orientation=0.5,
                current_state="MOVING",
                source="engine",
                sample_reason="state_forced",
                payload_json={},
            )
        )
        session.add(
            CombatEvent(
                telemetry_run_id=telemetry.id,
                telemetry_event_id=combat_event.id,
                replay_id=replay.id,
                frame=40,
                event_type="damage_applied",
                attacker_entity_id=entity.id,
                attacker_replay_player_id=player.id,
                applied_amount=25.0,
                killing_blow=True,
                location_x=combat_x,
                location_y=10.0,
                location_z=0.0,
                payload_json={},
            )
        )
        session.add_all(
            (
                MapResource(
                    map_id=map_row.id,
                    stable_key="start:0",
                    resource_kind="start_position",
                    owner_player_index=0,
                    x=5.0,
                    y=5.0,
                    z=0.0,
                    payload_json={"name": "Start Zero", "slot_indices": [0], "waypoint_id": 10},
                ),
                MapResource(
                    map_id=map_row.id,
                    stable_key="static:77",
                    resource_kind="static_object",
                    source_object_id=77,
                    template_name="SupplyDock",
                    x=15.0,
                    y=15.0,
                    z=0.0,
                    payload_json={
                        "categories": [
                            {
                                "name": "supply_source",
                                "source": "ThingTemplate::isKindOf(KINDOF_SUPPLY_SOURCE)",
                            }
                        ]
                    },
                ),
            )
        )
        parser.status = "succeeded"
        parser.completion_status = "complete"
        telemetry.status = "succeeded"
        session.commit()
    service = MapSceneQueryService(
        factory,
        _ReportAuthority(
            ids["replay"],
            ids["report"],
            (
                manifest_evidence_id,
                sample_evidence_id,
                combat_evidence_id,
                oob_evidence_id,
                *((visibility_evidence_id, visibility_summary_evidence_id, heuristic_evidence_id) if engine_native_map_events else ()),
                *((duplicate_manifest_evidence_id,) if duplicate_manifest else ()),
            ),
            ids["player:0"],
        ),
    )
    ids["manifest_evidence"] = manifest_evidence_id
    ids["sample_evidence"] = sample_evidence_id
    ids["combat_evidence"] = combat_evidence_id
    ids["duplicate_manifest_evidence"] = duplicate_manifest_evidence_id
    ids["visibility_evidence"] = visibility_evidence_id
    ids["visibility_summary_evidence"] = visibility_summary_evidence_id
    ids["heuristic_evidence"] = heuristic_evidence_id
    return service, ids


def test_service_reads_only_report_bound_normalized_spatial_evidence(tmp_path: Path) -> None:
    # Break caught: selecting a sibling run, leaking a path, or inventing terrain/locomotor evidence.
    service, ids = _seed_query_service(tmp_path)

    result = service.get_scene(
        MapSceneReadQuery(ids["replay"], ids["report"], 0, 120, sample_budget=100)
    )
    payload = thaw_canonical(result.payload)

    assert isinstance(payload, dict)
    assert payload["replay_public_id"] == ids["replay"]
    assert payload["map_public_id"] == ids["map"]
    assert payload["samples"][0]["entity_public_id"] == _uuid("entity-row")
    assert payload["samples"][0]["position"]["map_normalized"] == {"u": 0.25, "v": 0.5}
    assert payload["samples"][0]["locomotor_surface"] is None
    assert payload["downsampling"]["original_sample_count"] == 1
    assert {item["locomotor_surface"] for item in payload["rasters"]} == {"ground", "amphibious"}
    assert "terrain_grid_not_persisted" in payload["availability"]["reason_codes"]
    assert payload["routes"] == []
    assert payload["engagements"] == []
    assert payload["casualties"][0]["frame"] == 40
    assert "ignored/private/manifest.json" not in repr(payload)


def test_evidence_authority_load_batches_large_fixed_report_without_loss(tmp_path: Path) -> None:
    service, ids = _seed_query_service(tmp_path)
    evidence_ids = tuple(_uuid(f"large-report-evidence:{index}") for index in range(1101))
    with service.session_factory() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == ids["replay"]))
        assert replay is not None
        session.add_all(
            EvidenceItem(
                public_id=public_id,
                replay_id=replay.id,
                tier="derived",
                source_kind="large-report-test",
                source_key=public_id,
                schema_version=1,
            )
            for public_id in evidence_ids
        )
        session.commit()
        rows = service._evidence_rows(session, evidence_ids)

    assert tuple(row.public_id for row in rows) == evidence_ids
def test_map_v2_projects_scouting_and_only_opted_in_latest_engine_heuristics(tmp_path: Path) -> None:
    service, ids = _seed_query_service(tmp_path, engine_native_map_events=True)
    default_payload = thaw_canonical(
        service.get_scene(
            MapSceneReadQuery(ids["replay"], ids["report"], 0, 120, sample_budget=100)
        ).payload
    )
    opted_in_payload = thaw_canonical(
        service.get_scene(
            MapSceneReadQuery(
                ids["replay"],
                ids["report"],
                0,
                120,
                sample_budget=100,
                include_engine_heuristics=True,
            )
        ).payload
    )
    before_sample_payload = thaw_canonical(
        service.get_scene(
            MapSceneReadQuery(
                ids["replay"],
                ids["report"],
                0,
                100,
                sample_budget=100,
                include_engine_heuristics=True,
            )
        ).payload
    )
    structures_only_payload = thaw_canonical(
        service.get_scene(
            MapSceneReadQuery(
                ids["replay"],
                ids["report"],
                0,
                120,
                event_families=("structures",),
                sample_budget=100,
                include_engine_heuristics=True,
            )
        ).payload
    )
    heuristics_only_payload = thaw_canonical(
        service.get_scene(
            MapSceneReadQuery(
                ids["replay"],
                ids["report"],
                0,
                120,
                event_families=("engine_heuristics",),
                sample_budget=100,
                include_engine_heuristics=True,
            )
        ).payload
    )

    assert isinstance(default_payload, dict)
    assert isinstance(opted_in_payload, dict)
    assert isinstance(before_sample_payload, dict)
    assert default_payload["schema_version"] == "replay-map-scene-v2"
    assert default_payload["visibility_transitions"][0]["first_observed_clear"] is True
    assert default_payload["visibility_sampling_summaries"][0]["coverage_state"] == "incomplete"
    assert default_payload["engine_heuristic_overlays"] == []
    assert before_sample_payload["engine_heuristic_overlays"] == []
    assert structures_only_payload["visibility_transitions"] == []
    assert structures_only_payload["visibility_sampling_summaries"] == []
    assert structures_only_payload["engine_heuristic_overlays"] == []
    assert heuristics_only_payload["visibility_transitions"] == []
    assert len(heuristics_only_payload["engine_heuristic_overlays"]) == 1
    [overlay] = opted_in_payload["engine_heuristic_overlays"]
    assert overlay["frame"] == 120
    assert overlay["threat_label"] == "Engine AI threat heuristic"
    assert overlay["cash_label"] == "Engine AI cash-value heuristic"
    assert overlay["evidence"][0]["evidence_public_id"] == ids["heuristic_evidence"]


def test_listed_scene_window_resolves_against_authoritative_telemetry(tmp_path: Path) -> None:
    # Break caught: advertising replay metadata beyond the fixed report's telemetry authority.
    service, ids = _seed_query_service(tmp_path)
    with service.session_factory() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == ids["replay"]))
        assert replay is not None
        replay.frame_count = 180
        session.add(
            Report(
                public_id=ids["report"],
                replay_id=replay.id,
                report_version="replay-report-v1",
                input_digest="1" * 64,
                cache_key="2" * 64,
                report_json={},
                created_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
            )
        )
        session.commit()

    index = thaw_canonical(service.list_scenes(MapSceneIndexReadQuery()).payload)
    assert isinstance(index, dict)
    [listed] = index["items"]
    advertised = listed["frame_window"]
    scene = thaw_canonical(
        service.get_scene(
            MapSceneReadQuery(
                listed["replay_public_id"],
                listed["report_public_id"],
                advertised["frame_start"],
                advertised["frame_end"],
            )
        ).payload
    )

    assert isinstance(scene, dict)
    assert advertised == scene["available_frame_window"] == {"frame_start": 0, "frame_end": 120}
    with pytest.raises(ValueError, match="available frame window"):
        service.get_scene(MapSceneReadQuery(ids["replay"], ids["report"], 0, 121))


def test_scene_index_fails_closed_on_duplicate_report_bound_manifest(tmp_path: Path) -> None:
    # Break caught: advertising a candidate that detail rejects because manifest authority is ambiguous.
    service, ids = _seed_query_service(tmp_path, duplicate_manifest=True)
    with service.session_factory() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == ids["replay"]))
        assert replay is not None
        session.add(
            Report(
                public_id=ids["report"],
                replay_id=replay.id,
                report_version="replay-report-v1",
                input_digest="3" * 64,
                cache_key="4" * 64,
                report_json={},
                created_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
            )
        )
        session.commit()

    with pytest.raises(MapSceneContractError, match="selected map index candidate scene is unresolved"):
        service.list_scenes(MapSceneIndexReadQuery())


def test_scene_index_does_not_mask_poisoned_report_as_absent(tmp_path: Path) -> None:
    # Break caught: silently dropping a persisted report whose fixed report graph cannot be resolved.
    service, ids = _seed_query_service(tmp_path)
    poison_report_id = _uuid("poison-report")
    with service.session_factory() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == ids["replay"]))
        assert replay is not None
        session.add(
            Report(
                public_id=poison_report_id,
                replay_id=replay.id,
                report_version="replay-report-v1",
                input_digest="5" * 64,
                cache_key="6" * 64,
                report_json={},
                created_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
            )
        )
        session.commit()

    with pytest.raises(MapSceneContractError, match="fixed report is unresolved"):
        service.list_scenes(MapSceneIndexReadQuery())


def test_scene_index_bounds_authority_validation_to_selected_database_page(tmp_path: Path) -> None:
    # Break caught: validating every persisted report before slicing one requested page.
    service, ids = _seed_query_service(tmp_path)
    report_ids = tuple(_uuid(f"page-report:{index}") for index in range(6))
    with service.session_factory() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == ids["replay"]))
        assert replay is not None
        for index, report_id in enumerate(report_ids):
            session.add(
                Report(
                    public_id=report_id,
                    replay_id=replay.id,
                    report_version="replay-report-v1",
                    input_digest=f"{index + 10:064x}",
                    cache_key=f"{index + 20:064x}",
                    report_json={},
                    created_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
                )
            )
        session.commit()
    authority = _CountingReportAuthority(
        ids["replay"],
        (ids["manifest_evidence"], ids["sample_evidence"], ids["combat_evidence"]),
        ids["player:0"],
    )
    bounded = MapSceneQueryService(service.session_factory, authority)

    page = thaw_canonical(bounded.list_scenes(MapSceneIndexReadQuery(page=2, page_size=2)).payload)

    assert isinstance(page, dict)
    assert page["total_items"] == 6
    assert len(page["items"]) == 2
    assert len(authority.calls) == 2
    assert {call.report_public_id for call in authority.calls} == set(sorted(report_ids)[2:4])

    authority.calls.clear()
    empty = thaw_canonical(
        bounded.list_scenes(MapSceneIndexReadQuery(search="no such map")).payload
    )
    assert isinstance(empty, dict)
    assert empty["total_items"] == 0
    assert authority.calls == []

    unavailable = thaw_canonical(
        bounded.list_scenes(MapSceneIndexReadQuery(availability="available")).payload
    )
    assert isinstance(unavailable, dict)
    assert unavailable["total_items"] == 0
    assert authority.calls == []


def test_scene_index_search_preserves_unicode_casefold_semantics(tmp_path: Path) -> None:
    # Break caught: delegating Unicode search to SQLite's ASCII-only lower() implementation.
    service, ids = _seed_query_service(tmp_path, map_display_name="Große Straße")
    with service.session_factory() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == ids["replay"]))
        assert replay is not None
        session.add(
            Report(
                public_id=ids["report"],
                replay_id=replay.id,
                report_version="replay-report-v1",
                input_digest="7" * 64,
                cache_key="8" * 64,
                report_json={},
                created_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
            )
        )
        session.commit()

    page = thaw_canonical(
        service.list_scenes(MapSceneIndexReadQuery(search="STRASSE")).payload
    )

    assert isinstance(page, dict)
    assert page["total_items"] == 1
    assert [item["map_display_name"] for item in page["items"]] == ["Große Straße"]


def test_scene_index_search_fails_closed_above_lightweight_candidate_cap(tmp_path: Path) -> None:
    # Break caught: scanning an unbounded report collection to implement Python casefold search.
    service, ids = _seed_query_service(tmp_path)
    with service.session_factory() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == ids["replay"]))
        assert replay is not None
        for index in range(1001):
            session.add(
                Report(
                    public_id=_uuid(f"search-cap-report:{index}"),
                    replay_id=replay.id,
                    report_version="replay-report-v1",
                    input_digest=f"{index + 100:064x}",
                    cache_key=f"{index + 1200:064x}",
                    report_json={},
                    created_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
                )
            )
        session.commit()
    authority = _CountingReportAuthority(
        ids["replay"],
        (ids["manifest_evidence"], ids["sample_evidence"], ids["combat_evidence"]),
        ids["player:0"],
    )

    with pytest.raises(MapSceneContractError, match="search candidate limit exceeded"):
        MapSceneQueryService(service.session_factory, authority).list_scenes(
            MapSceneIndexReadQuery(search="Tournament")
        )
    assert authority.calls == []


def test_scene_index_huge_offset_skips_hydration_and_preserves_collection_availability(
    tmp_path: Path,
) -> None:
    # Break caught: reporting no completed scenes when only the requested page is empty.
    service, ids = _seed_query_service(tmp_path)
    with service.session_factory() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == ids["replay"]))
        assert replay is not None
        session.add(
            Report(
                public_id=ids["report"],
                replay_id=replay.id,
                report_version="replay-report-v1",
                input_digest="9" * 64,
                cache_key="a" * 64,
                report_json={},
                created_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
            )
        )
        session.commit()
    authority = _CountingReportAuthority(
        ids["replay"],
        (ids["manifest_evidence"], ids["sample_evidence"], ids["combat_evidence"]),
        ids["player:0"],
    )

    page = thaw_canonical(
        MapSceneQueryService(service.session_factory, authority)
        .list_scenes(MapSceneIndexReadQuery(page=2**63, page_size=100))
        .payload
    )

    assert isinstance(page, dict)
    assert page["total_items"] == 1
    assert page["items"] == []
    assert page["availability"] == {
        "state": "partial",
        "reason_codes": ["requested_page_empty"],
        "evidence_references": [],
    }
    assert authority.calls == []


def test_scene_index_normalizes_corrupt_projection_failure(tmp_path: Path) -> None:
    # Break caught: leaking a raw validated-projection ValueError from a selected report row.
    service, ids = _seed_query_service(tmp_path, corrupt_projection=True)
    with service.session_factory() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == ids["replay"]))
        assert replay is not None
        session.add(
            Report(
                public_id=ids["report"],
                replay_id=replay.id,
                report_version="replay-report-v1",
                input_digest="b" * 64,
                cache_key="c" * 64,
                report_json={},
                created_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
            )
        )
        session.commit()

    with pytest.raises(MapSceneContractError, match="selected map index candidate scene is unresolved"):
        service.list_scenes(MapSceneIndexReadQuery())


def test_scene_index_normalizes_missing_telemetry_authority(tmp_path: Path) -> None:
    # Break caught: leaking a selected report's internal authority failure instead of one index error.
    service, ids = _seed_query_service(tmp_path)
    with service.session_factory() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == ids["replay"]))
        assert replay is not None
        session.add(
            Report(
                public_id=ids["report"],
                replay_id=replay.id,
                report_version="replay-report-v1",
                input_digest="d" * 64,
                cache_key="e" * 64,
                report_json={},
                created_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
            )
        )
        session.commit()
    missing = MapSceneQueryService(
        service.session_factory,
        _ReportAuthority(ids["replay"], ids["report"], (), ids["player:0"]),
    )

    with pytest.raises(MapSceneContractError, match="selected map index candidate scene is unresolved"):
        missing.list_scenes(MapSceneIndexReadQuery())


def test_pathability_raster_is_stable_png_and_terrain_is_explicitly_unavailable(tmp_path: Path) -> None:
    # Break caught: reopening a managed sidecar or serving a mismatched/cross-map raster.
    service, ids = _seed_query_service(tmp_path)
    ground_id = service.raster_public_id(ids["map"], "pathability", "ground")

    first = service.get_raster(MapRasterReadQuery(ids["map"], ground_id))
    second = service.get_raster(MapRasterReadQuery(ids["map"], ground_id))

    assert first == second
    assert first.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert first.descriptor["width"] == 2
    assert first.descriptor["height"] == 2
    terrain_id = service.raster_public_id(ids["map"], "terrain_cell_type", None)
    with pytest.raises(RasterUnavailableError, match="terrain_grid_not_persisted"):
        service.get_raster(MapRasterReadQuery(ids["map"], terrain_id))


def test_pathability_png_flips_minimum_world_y_source_row_to_png_bottom(tmp_path: Path) -> None:
    # Break caught: displaying source row zero at the top mirrors authoritative map coordinates vertically.
    service, ids = _seed_query_service(tmp_path)
    ground_id = service.raster_public_id(ids["map"], "pathability", "ground")
    resource = service.get_raster(MapRasterReadQuery(ids["map"], ground_id))
    content = resource.content
    offset = 8
    compressed = bytearray()
    while offset < len(content):
        length = struct.unpack(">I", content[offset : offset + 4])[0]
        kind = content[offset + 4 : offset + 8]
        payload = content[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IDAT":
            compressed.extend(payload)
    scanlines = zlib.decompress(bytes(compressed))
    row_bytes = 1 + 2 * 3
    top = scanlines[:row_bytes]
    bottom = scanlines[row_bytes : row_bytes * 2]

    assert top == bytes((0, 126, 231, 135, 126, 231, 135))
    assert bottom == bytes((0, 126, 231, 135, 31, 41, 55))


def test_service_rejects_report_that_is_not_a_completed_fixed_member(tmp_path: Path) -> None:
    # Break caught: falling forward from an unknown fixed report to any latest report.
    service, ids = _seed_query_service(tmp_path)

    with pytest.raises(ReportGraphNotFoundError):
        service.get_scene(MapSceneReadQuery(ids["replay"], _uuid("other-report"), 0, 120))


def test_service_rejects_unresolved_fixed_report_evidence_membership(tmp_path: Path) -> None:
    # Break caught: silently ignoring a cited report evidence ID that has no persisted immutable row.
    service, ids = _seed_query_service(tmp_path)
    authority = _ReportAuthority(
        ids["replay"],
        ids["report"],
        (ids["manifest_evidence"], _uuid("unresolved-evidence")),
        ids["player:0"],
    )
    broken = MapSceneQueryService(service.session_factory, authority)

    with pytest.raises(MapSceneContractError, match="report evidence membership"):
        broken.get_scene(MapSceneReadQuery(ids["replay"], ids["report"], 0, 120))


def test_out_of_bounds_casualty_is_omitted_with_an_explicit_reason(tmp_path: Path) -> None:
    service, ids = _seed_query_service(
        tmp_path, combat_x=25.0, out_of_bounds_sample_x=15.0
    )

    payload = thaw_canonical(
        service.get_scene(
            MapSceneReadQuery(ids["replay"], ids["report"], 0, 120, sample_budget=100)
        ).payload
    )

    assert isinstance(payload, dict)
    assert payload["casualties"] == []
    assert "map_coordinate_out_of_bounds" in payload["availability"]["reason_codes"]


def test_out_of_bounds_visibility_is_omitted_with_an_explicit_reason(tmp_path: Path) -> None:
    service, ids = _seed_query_service(tmp_path, visibility_x=25.0, engine_native_map_events=True)

    payload = thaw_canonical(
        service.get_scene(
            MapSceneReadQuery(ids["replay"], ids["report"], 0, 120, sample_budget=100)
        ).payload
    )

    assert isinstance(payload, dict)
    assert payload["visibility_transitions"] == []
    assert "map_coordinate_out_of_bounds" in payload["availability"]["reason_codes"]


def test_in_bounds_visibility_remains_projected_when_other_rows_are_omitted(tmp_path: Path) -> None:
    service, ids = _seed_query_service(tmp_path, visibility_x=5.0, engine_native_map_events=True)

    payload = thaw_canonical(
        service.get_scene(
            MapSceneReadQuery(ids["replay"], ids["report"], 0, 120, sample_budget=100)
        ).payload
    )

    assert isinstance(payload, dict)
    assert len(payload["visibility_transitions"]) == 1
    assert payload["visibility_transitions"][0]["position"]["raw"]["x"] == 5.0


def test_seed_uses_migrated_sqlite_and_not_route_sql(tmp_path: Path) -> None:
    service, ids = _seed_query_service(tmp_path)
    with service.session_factory() as session:
        assert session.scalar(select(Replay.public_id).where(Replay.public_id == ids["replay"])) == ids["replay"]
