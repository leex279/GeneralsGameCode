"""Feature context materialization, cache, race, and rollback tests."""

from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.models import (
    Entity,
    EvidenceItem,
    Feature,
    FeatureEvidence,
    FeatureSet,
    ManagedAsset,
    Map,
    MapResource,
    ParserRun,
    Replay,
    ReplayCommand,
    ReplayPlayer,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.features.base import (
    FeatureBundle,
    FeatureScope,
    FeatureValue,
    FeatureWindow,
    validate_feature_value,
)
from generals_replay_analyzer.features.build_order import BuildOrderExtractor
from generals_replay_analyzer.features.context import FeatureContext, cache_key, canonical_json, input_digest
from generals_replay_analyzer.features.evidence import EvidenceRef, ObservedEvidence, thaw_canonical
from generals_replay_analyzer.features.registry import (
    REGISTRY_SCHEMA,
    FeatureDefinition,
    FeatureRegistry,
)
from generals_replay_analyzer.features.service import (
    ExtractFeaturesRequest,
    FeatureExtractionError,
    FeatureExtractionService,
    _evidence_id_batches,
)


@pytest.fixture
def feature_engine(tmp_path: Path) -> Engine:
    database = tmp_path / "external-data-root" / "library.sqlite3"
    database.parent.mkdir()
    upgrade_database(database)
    engine = create_database_engine(database)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def feature_factory(feature_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(feature_engine)


def test_evidence_id_batches_stay_below_sqlite_bind_limit() -> None:
    public_ids = tuple(f"evidence-{index}" for index in range(1_201))

    batches = tuple(_evidence_id_batches(public_ids))

    assert tuple(len(batch) for batch in batches) == (500, 500, 201)
    assert tuple(public_id for batch in batches for public_id in batch) == public_ids


def _seed_replay(
    factory: sessionmaker[Session],
    *,
    replay_public_id: str = "00000000-0000-4000-8000-000000000301",
    replay_sha256: str = "a" * 64,
    replay_player_public_id: str = "00000000-0000-4000-8000-000000000302",
    parser_run_id: str = "00000000-0000-4000-8000-000000000303",
    telemetry_run_id: str = "00000000-0000-4000-8000-000000000304",
    telemetry_parser_run_id: str | None = None,
    finalize: bool = True,
) -> tuple[str, str, tuple[str, ...]]:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    player_index = 0
    events = (
        (
            10,
            "object_created",
            {"object_id": 7, "owner_player_index": player_index, "template_name": "FabricatedObjectTemplate"},
        ),
        (
            20,
            "construction_completed",
            {"object_id": 7, "owner_player_index": player_index, "template_name": "FabricatedCompletionTemplate"},
        ),
        (
            120,
            "complete",
            {
                "final_frame": 120,
                "terminal_reason": "clean_completion",
                "final_cash_balances": [{"player_index": player_index, "has_money": True, "balance": 500}],
            },
        ),
    )
    with factory() as session:
        replay = Replay(
            public_id=replay_public_id,
            sha256=replay_sha256,
            replay_name="fixture",
            version_string="1.04",
            version_number=1,
            frame_count=120,
            start_time=0,
            end_time=120,
            exe_crc=1,
            ini_crc=2,
            map_crc=3,
            map_name="fixture.map",
            seed=4,
            header_json={},
            lifecycle_state="engine_verified",
            updated_at=now,
            created_at=now,
        )
        session.add(replay)
        session.flush()
        parser = ParserRun(
            run_id=parser_run_id,
            replay_id=replay.id,
            parser_version="parser-v2",
            schema_version=1,
            input_sha256=replay_sha256,
            status="running",
            warnings_json=[],
            started_at=now,
        )
        session.add(parser)
        session.flush()
        replay_player = ReplayPlayer(
            public_id=replay_player_public_id,
            replay_id=replay.id,
            parser_run_id=parser.id,
            slot_index=0,
            slot_kind="human",
            original_name="Player",
            normalized_name="player",
            player_index=player_index,
            observed_json={"player_index": player_index},
        )
        session.add(replay_player)
        telemetry = TelemetryRun(
            run_id=telemetry_run_id,
            replay_id=replay.id,
            schema_version=2,
            engine_build="fixture",
            settings_json={"parser_run_id": telemetry_parser_run_id or parser_run_id},
            status="running",
            runner_status="succeeded",
            diagnostics_json={},
            started_at=now,
        )
        session.add(telemetry)
        session.flush()
        session.add(
            Entity(
                public_id=str(uuid5(NAMESPACE_URL, f"{telemetry_run_id}:entity:7")),
                telemetry_run_id=telemetry.id,
                replay_id=replay.id,
                object_id=7,
                template_name="ChinaPowerPlant",
                initial_owner_player_index=player_index,
                kind_of_flags_json=["STRUCTURE"],
                creation_sequence=0,
                creation_frame=10,
                observed_json={"source": "object_created"},
            )
        )
        evidence_ids: list[str] = []
        for sequence, (frame, event_type, payload) in enumerate(events):
            public_id = str(uuid5(NAMESPACE_URL, f"{telemetry_run_id}:{sequence}"))
            evidence = EvidenceItem(
                public_id=public_id,
                replay_id=replay.id,
                telemetry_run_id=telemetry.id,
                tier="observed",
                source_kind="telemetry",
                source_key=f"telemetry:{telemetry_run_id}:sequence:{sequence}",
                schema_version=2,
                created_at=now,
            )
            session.add(evidence)
            session.flush()
            session.add(
                TelemetryEvent(
                    telemetry_run_id=telemetry.id,
                    sequence=sequence,
                    frame=frame,
                    logic_time_seconds=frame / 30.0,
                    schema_version=2,
                    event_type=event_type,
                    payload_json=payload,
                    raw_record_json={"event_type": event_type, "payload": payload},
                    evidence_item_id=evidence.id,
                )
            )
            evidence_ids.append(public_id)
        parser_evidence_public_id = str(uuid5(NAMESPACE_URL, f"{parser_run_id}:command:0"))
        parser_evidence = EvidenceItem(
            public_id=parser_evidence_public_id,
            replay_id=replay.id,
            parser_run_id=parser.id,
            tier="observed",
            source_kind="parser",
            source_key=f"parser:{parser_run_id}:command:0",
            schema_version=1,
            created_at=now,
        )
        session.add(parser_evidence)
        session.flush()
        session.add(
            ReplayCommand(
                parser_run_id=parser.id,
                replay_id=replay.id,
                replay_player_id=replay_player.id,
                command_index=0,
                frame=5,
                player_index=player_index,
                message_type=1074,
                message_name="MSG_DO_STOP",
                start_offset=1,
                end_offset=2,
                arguments_json=[],
                evidence_item_id=parser_evidence.id,
            )
        )
        evidence_ids.append(parser_evidence_public_id)
        orphan_public_id = str(uuid5(NAMESPACE_URL, f"{telemetry_run_id}:orphan"))
        session.add(
            EvidenceItem(
                public_id=orphan_public_id,
                replay_id=replay.id,
                telemetry_run_id=telemetry.id,
                tier="observed",
                source_kind="telemetry",
                source_key=f"telemetry:{telemetry_run_id}:orphan",
                schema_version=2,
                created_at=now,
            )
        )
        evidence_ids.append(orphan_public_id)
        session.commit()
        if finalize:
            parser.status = "succeeded"
            parser.completion_status = "complete"
            parser.result_sha256 = "b" * 64
            parser.completed_at = now
            telemetry.status = "succeeded"
            telemetry.final_frame = 120
            telemetry.command_count = 0
            telemetry.trace_sha256 = "c" * 64
            telemetry.completed_at = now
            session.commit()
    return replay_public_id, replay_player_public_id, tuple(evidence_ids)


def _request(replay: str, player: str, *extractors: str, settings: object = ()) -> ExtractFeaturesRequest:
    return ExtractFeaturesRequest(replay, player, tuple(extractors), settings)  # type: ignore[arg-type]


def _attach_spatial_projection(
    factory: sessionmaker[Session], replay_public_id: str, telemetry_run_id: str, *, defect: str | None = None
) -> tuple[int, int]:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    content_sha256 = "d" * 64
    manifest_sha256 = "e" * 64
    catalog_sha256 = "f" * 64
    projection = {
        "amphibious_passable": [True, True],
        "content_sha256": content_sha256,
        "engine_data_identity": "fixture-engine",
        "ground_passable": [True, False],
        "map_identity": "maps/fixture.map",
        "pathing": {
            "bounds": {
                "maximum_exclusive": {"x": 20.0, "y": 10.0},
                "minimum_inclusive": {"x": 0.0, "y": 0.0},
            },
            "cell_size": {"x": 10.0, "y": 10.0},
            "dimension_source": "fixture grid",
            "height": 1,
            "index_origin": {"x": 0, "y": 0},
            "sample_point": "cell_center",
            "storage_order": "row_major_y_then_x_x_fastest",
            "width": 2,
        },
        "schema_version": 2,
        "world_bounds": {
            "maximum": {"x": 20.0, "y": 10.0, "z": 5.0},
            "maximum_inclusive": True,
            "minimum": {"x": 0.0, "y": 0.0, "z": -5.0},
            "minimum_inclusive": True,
        },
        "zone_ids": [1, 2],
    }
    if defect == "grid":
        projection["ground_passable"] = [True]
    elif defect == "numeric_string":
        cast(dict[str, object], projection["pathing"])["width"] = "2"
    start_payload = {
        "bounds_policy": "pathfinder_xy_closed",
        "category_source": "GameSlot::getStartPos + TerrainLogic::getWaypointByName",
        "name": "Start Zero",
        "position": {"x": 5.0, "y": 5.0, "z": 0.0},
        "slot_indices": [0],
        "waypoint_id": 10,
    }
    static_payload = {
        "bounds_policy": "pathfinder_xy_closed",
        "categories": [
            {"name": "supply_source", "source": "ThingTemplate::isKindOf(KINDOF_SUPPLY_SOURCE)"}
        ],
        "creation_source": "map_loaded",
        "object_id": 77,
        "orientation": 0.0,
        "position": {"x": 15.0, "y": 5.0, "z": 0.0},
        "snapshot_scope": "post_map_initialization",
        "template_name": "SupplyDock",
    }
    if defect == "resource_numeric_string":
        static_payload["orientation"] = "0.0"
    elif defect == "negative_zero":
        static_payload["orientation"] = -0.0
    with factory.begin() as session:
        replay = session.scalar(select(Replay).where(Replay.public_id == replay_public_id))
        telemetry = session.scalar(select(TelemetryRun).where(TelemetryRun.run_id == telemetry_run_id))
        assert replay is not None and telemetry is not None
        parser = session.scalar(select(ParserRun).where(ParserRun.replay_id == replay.id))
        assert parser is not None
        manifest_asset = ManagedAsset(
            public_id="00000000-0000-4000-8000-000000000401",
            sha256=manifest_sha256,
            kind="telemetry_map_asset",
            relative_path="fixtures/map/manifest.json",
            size_bytes=1,
            created_at=now,
        )
        catalog_asset = ManagedAsset(
            public_id="00000000-0000-4000-8000-000000000402",
            sha256=catalog_sha256,
            kind="telemetry_catalog",
            relative_path="fixtures/catalog.json",
            size_bytes=1,
            created_at=now,
        )
        session.add_all((manifest_asset, catalog_asset))
        session.flush()
        map_row = Map(
            public_id="00000000-0000-4000-8000-000000000403",
            content_sha256=content_sha256,
            manifest_asset_id=manifest_asset.id,
            schema_version=2,
            engine_data_identity="fixture-engine",
            map_identity="maps/fixture.map",
            exporter_version="zero-hour-replay-map-export-v2",
            min_x=0.0,
            min_y=0.0,
            min_z=-5.0,
            max_x=20.0,
            max_y=10.0,
            max_z=5.0,
            pathing_width=2,
            pathing_height=1,
            pathing_cell_size=10.0,
            terrain_width=2,
            terrain_height=1,
            terrain_cell_size=10.0,
            metadata_json={}
            if defect == "missing_projection"
            else {"validated_spatial_projection": projection},
            created_at=now,
        )
        session.add(map_row)
        session.flush()
        # Deliberately insert reverse semantic order; context order must be canonical.
        session.add_all(
            (
                MapResource(
                    map_id=map_row.id,
                    stable_key="static:77",
                    resource_kind="static_object",
                    source_object_id=77,
                    template_name="SupplyDock",
                    owner_player_index=None,
                    amount=None,
                    x=15.0,
                    y=5.0,
                    z=0.0,
                    payload_json=static_payload,
                ),
                MapResource(
                    map_id=map_row.id,
                    stable_key="start:0",
                    resource_kind="start_position",
                    source_object_id=None,
                    template_name="Start Zero",
                    owner_player_index=0,
                    amount=None,
                    x=5.0,
                    y=5.0,
                    z=0.0,
                    payload_json=start_payload,
                ),
            )
        )
        replay.map_id = map_row.id
        if defect == "telemetry_map":
            wrong_map = Map(
                public_id="00000000-0000-4000-8000-000000000406",
                content_sha256="8" * 64,
                manifest_asset_id=manifest_asset.id,
                schema_version=2,
                engine_data_identity="fixture-engine",
                map_identity="maps/wrong.map",
                exporter_version="zero-hour-replay-map-export-v2",
                min_x=0.0,
                min_y=0.0,
                min_z=-5.0,
                max_x=20.0,
                max_y=10.0,
                max_z=5.0,
                pathing_width=2,
                pathing_height=1,
                pathing_cell_size=10.0,
                terrain_width=2,
                terrain_height=1,
                terrain_cell_size=10.0,
                metadata_json={},
                created_at=now,
            )
            session.add(wrong_map)
            session.flush()
            telemetry.map_id = wrong_map.id
        else:
            telemetry.map_id = map_row.id
        if defect == "manifest_asset":
            wrong = ManagedAsset(
                public_id="00000000-0000-4000-8000-000000000405",
                sha256="9" * 64,
                kind="telemetry_map_asset",
                relative_path="fixtures/wrong-manifest.json",
                size_bytes=1,
                created_at=now,
            )
            session.add(wrong)
            session.flush()
            telemetry.map_asset_id = wrong.id
        else:
            telemetry.map_asset_id = manifest_asset.id
        if defect == "catalog_asset":
            wrong_catalog = ManagedAsset(
                public_id="00000000-0000-4000-8000-000000000407",
                sha256="7" * 64,
                kind="telemetry_catalog",
                relative_path="fixtures/wrong-catalog.json",
                size_bytes=1,
                created_at=now,
            )
            session.add(wrong_catalog)
            session.flush()
            telemetry.catalog_asset_id = wrong_catalog.id
        else:
            telemetry.catalog_asset_id = catalog_asset.id
        telemetry.engine_build = "wrong-engine" if defect == "engine_identity" else "fixture-engine"
        manifest_payload = {
            "engine_build": "fixture-engine",
            "replay_version": "1.04",
            "map_identity": "maps/fixture.map",
            "initial_seed": 4,
            "exporter_settings": {
                "audio_enabled": False,
                "movement_sample_frames": 15,
                "private_locator": "C:\\private\\telemetry-run",
            },
            "game_data_catalog": {
                "type": "game_data_catalog",
                "path": f"game-data-catalog-v1-{catalog_sha256}.json",
                "sha256": catalog_sha256,
                "engine_data_identity": "fixture-engine",
            },
            "map_asset": {
                "type": "map_asset",
                "schema_version": 2,
                "path": f"map-assets-v2/{content_sha256}/manifest.json",
                "sha256": manifest_sha256,
                "content_sha256": content_sha256,
                "engine_data_identity": "fixture-engine",
                "map_identity": "maps/fixture.map",
            },
        }
        evidence = EvidenceItem(
            public_id="00000000-0000-4000-8000-000000000404",
            replay_id=replay.id,
            telemetry_run_id=telemetry.id,
            tier="observed",
            source_kind="telemetry",
            source_key=f"telemetry:{telemetry_run_id}:sequence:10",
            schema_version=2,
            created_at=now,
        )
        session.add(evidence)
        session.flush()
        session.add(
            TelemetryEvent(
                telemetry_run_id=telemetry.id,
                sequence=10,
                frame=0,
                logic_time_seconds=0.0,
                schema_version=2,
                event_type="manifest",
                payload_json=manifest_payload,
                raw_record_json={"event_type": "manifest", "payload": manifest_payload},
                evidence_item_id=evidence.id,
            )
        )
        if defect == "null_identity_graph":
            telemetry.map_id = None
            telemetry.map_asset_id = None
            telemetry.catalog_asset_id = None
        if defect in ("resource", "resource_kind"):
            static_resource = session.scalar(
                select(MapResource).where(MapResource.map_id == map_row.id, MapResource.stable_key == "static:77")
            )
            assert static_resource is not None
            if defect == "resource":
                static_resource.x = 14.0
            else:
                static_resource.resource_kind = "unvalidated_resource"
        telemetry.status = "succeeded"
        telemetry.final_frame = 120
        telemetry.command_count = 0
        telemetry.trace_sha256 = "c" * 64
        telemetry.completed_at = now
        parser.status = "succeeded"
        parser.completion_status = "complete"
        parser.result_sha256 = "b" * 64
        parser.completed_at = now
        return map_row.id, manifest_asset.id


def _spatial_manifest_facts(
    factory: sessionmaker[Session], replay: str, player: str
) -> tuple[dict[str, object], FeatureContext]:
    context = FeatureExtractionService(factory)._build_context(_request(replay, player, "build"))
    manifests = [item for item in context.observed if item.event_type == "manifest"]
    assert len(manifests) == 1
    return cast(dict[str, object], thaw_canonical(manifests[0].facts)), context


def test_context_enriches_only_manifest_with_canonical_persisted_spatial_projection(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory, finalize=False)
    _attach_spatial_projection(feature_factory, replay, "00000000-0000-4000-8000-000000000304")

    facts, context = _spatial_manifest_facts(feature_factory, replay, player)

    projection = cast(dict[str, object], facts["validated_spatial_projection"])
    assert projection["content_sha256"] == "d" * 64
    assert projection["ground_passable"] == [True, False]
    assert projection["amphibious_passable"] == [True, True]
    assert projection["zone_ids"] == [1, 2]
    assert projection["start_positions"] == [
        {
            "bounds_policy": "pathfinder_xy_closed",
            "category_source": "GameSlot::getStartPos + TerrainLogic::getWaypointByName",
            "name": "Start Zero",
            "position": {"x": 5.0, "y": 5.0, "z": 0.0},
            "slot_indices": [0],
            "waypoint_id": 10,
        }
    ]
    assert projection["static_objects"] == [
        {
            "bounds_policy": "pathfinder_xy_closed",
            "categories": [
                {"name": "supply_source", "source": "ThingTemplate::isKindOf(KINDOF_SUPPLY_SOURCE)"}
            ],
            "creation_source": "map_loaded",
            "object_id": 77,
            "orientation": 0.0,
            "position": {"x": 15.0, "y": 5.0, "z": 0.0},
            "snapshot_scope": "post_map_initialization",
            "template_name": "SupplyDock",
        }
    ]
    assert "path" not in projection and "loader" not in projection
    manifest = next(item for item in context.observed if item.event_type == "manifest")
    assert manifest.ref.public_id == "00000000-0000-4000-8000-000000000404"
    with feature_factory() as session:
        stored = session.scalar(select(TelemetryEvent).where(TelemetryEvent.event_type == "manifest"))
        assert stored is not None and "validated_spatial_projection" not in stored.payload_json


def test_manifest_context_is_exact_path_free_and_source_locator_independent(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory, finalize=False)
    _attach_spatial_projection(feature_factory, replay, "00000000-0000-4000-8000-000000000304")
    service = FeatureExtractionService(feature_factory)
    facts, context = _spatial_manifest_facts(feature_factory, replay, player)
    assert set(facts) == {
        "audio_enabled",
        "engine_build",
        "game_data_catalog",
        "initial_seed",
        "map_asset",
        "map_identity",
        "movement_sample_frames",
        "order_coverage",
        "replay_version",
        "validated_spatial_projection",
    }
    assert set(cast(dict[str, object], facts["game_data_catalog"])) == {
        "engine_data_identity",
        "sha256",
        "type",
    }
    assert set(cast(dict[str, object], facts["map_asset"])) == {
        "content_sha256",
        "engine_data_identity",
        "map_identity",
        "schema_version",
        "sha256",
        "type",
    }

    with feature_factory() as session:
        stored = session.scalar(select(TelemetryEvent).where(TelemetryEvent.event_type == "manifest"))
        assert stored is not None
        alternate_payload = deepcopy(stored.payload_json)
    cast(dict[str, object], alternate_payload["game_data_catalog"])["path"] = "private/catalog/source.json"
    cast(dict[str, object], alternate_payload["map_asset"])["path"] = "private/map/source.json"
    cast(dict[str, object], alternate_payload["exporter_settings"])["private_locator"] = "D:\\other\\run"
    alternate_event = TelemetryEvent(event_type="manifest", payload_json=alternate_payload)
    alternate_facts = service._event_facts(alternate_event, {}, {}, None)
    alternate_facts["validated_spatial_projection"] = facts["validated_spatial_projection"]
    alternate_observed = tuple(
        replace(item, facts=alternate_facts) if item.event_type == "manifest" else item for item in context.observed
    )
    assert input_digest(replace(context, observed=alternate_observed)) == input_digest(context)


@pytest.mark.parametrize(
    "defect",
    ["telemetry_map", "manifest_asset", "catalog_asset", "engine_identity"],
)
def test_context_rejects_mismatched_successful_telemetry_spatial_identity(
    feature_factory: sessionmaker[Session], defect: str
) -> None:
    replay, player, _ = _seed_replay(feature_factory, finalize=False)
    _attach_spatial_projection(
        feature_factory,
        replay,
        "00000000-0000-4000-8000-000000000304",
        defect=defect,
    )

    with pytest.raises(FeatureExtractionError, match="spatial identity mismatch"):
        FeatureExtractionService(feature_factory)._build_context(_request(replay, player, "build"))


@pytest.mark.parametrize(
    "defect",
    ["grid", "resource", "resource_kind", "numeric_string", "resource_numeric_string", "negative_zero"],
)
def test_context_rejects_malformed_persisted_spatial_projection(
    feature_factory: sessionmaker[Session], defect: str
) -> None:
    replay, player, _ = _seed_replay(feature_factory, finalize=False)
    _attach_spatial_projection(
        feature_factory,
        replay,
        "00000000-0000-4000-8000-000000000304",
        defect=defect,
    )

    with pytest.raises(FeatureExtractionError, match="malformed validated spatial projection"):
        FeatureExtractionService(feature_factory)._build_context(_request(replay, player, "build"))


def test_context_rejects_v2_manifest_when_selected_run_has_null_spatial_identity_graph(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory, finalize=False)
    _attach_spatial_projection(
        feature_factory,
        replay,
        "00000000-0000-4000-8000-000000000304",
        defect="null_identity_graph",
    )
    with pytest.raises(FeatureExtractionError, match="spatial identity mismatch"):
        FeatureExtractionService(feature_factory)._build_context(_request(replay, player, "build"))


def test_context_leaves_missing_validated_spatial_projection_absent(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory, finalize=False)
    _attach_spatial_projection(
        feature_factory,
        replay,
        "00000000-0000-4000-8000-000000000304",
        defect="missing_projection",
    )
    context = FeatureExtractionService(feature_factory)._build_context(_request(replay, player, "build"))
    manifest = next(item for item in context.observed if item.event_type == "manifest")
    assert "validated_spatial_projection" not in cast(dict[str, object], thaw_canonical(manifest.facts))


def test_spatial_projection_semantic_change_changes_context_digest_without_mutating_manifest(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory, finalize=False)
    _attach_spatial_projection(feature_factory, replay, "00000000-0000-4000-8000-000000000304")
    _, before = _spatial_manifest_facts(feature_factory, replay, player)
    changed_observed = []
    for observation in before.observed:
        if observation.event_type != "manifest":
            changed_observed.append(observation)
            continue
        facts = cast(dict[str, object], thaw_canonical(observation.facts))
        projection = cast(dict[str, object], facts["validated_spatial_projection"])
        projection["ground_passable"] = [False, False]
        changed_observed.append(replace(observation, facts=facts))
    after = replace(before, observed=tuple(changed_observed))
    assert input_digest(before) != input_digest(after)
    with feature_factory() as session:
        stored = session.scalar(select(TelemetryEvent).where(TelemetryEvent.event_type == "manifest"))
        assert stored is not None and "validated_spatial_projection" not in stored.payload_json


def test_service_reuses_immutable_success_and_persists_direct_same_replay_links(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, evidence_ids = _seed_replay(feature_factory)
    service = FeatureExtractionService(feature_factory)
    first = service.extract(_request(replay, player, "build"))[0]
    second = service.extract(_request(replay, player, "build"))[0]
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.feature_set_public_id == first.feature_set_public_id
    assert second.cache_key == first.cache_key
    assert tuple(value.name for value in second.features) == tuple(sorted(value.name for value in second.features))
    assert len(first.derived_evidence) == len(first.features) == 3
    assert second.derived_evidence == first.derived_evidence
    assert all(ref.tier == "derived" and ref.source_kind == "feature" for ref in first.derived_evidence)
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(FeatureSet).where(FeatureSet.status == "succeeded")) == 1
        assert session.scalar(select(func.count()).select_from(Feature)) == 3
        telemetry_id = session.scalar(
            select(TelemetryRun.id).where(
                TelemetryRun.run_id == "00000000-0000-4000-8000-000000000304"
            )
        )
        parser_id = session.scalar(
            select(ParserRun.id).where(
                ParserRun.run_id == "00000000-0000-4000-8000-000000000303"
            )
        )
        derived_owners = tuple(
            session.execute(
                select(EvidenceItem.parser_run_id, EvidenceItem.telemetry_run_id)
                .join(Feature, Feature.evidence_item_id == EvidenceItem.id)
                .order_by(Feature.public_id)
            )
        )
        assert parser_id is not None and telemetry_id is not None
        assert derived_owners == ((parser_id, telemetry_id),) * 3
        linked = session.execute(
            select(EvidenceItem.public_id, EvidenceItem.replay_id)
            .join(FeatureEvidence, FeatureEvidence.evidence_item_id == EvidenceItem.id)
            .join(Feature, Feature.id == FeatureEvidence.feature_id)
        ).all()
        replay_id = session.scalar(select(Replay.id).where(Replay.public_id == replay))
        assert {public_id for public_id, _ in linked} <= set(evidence_ids)
        assert all(link_replay_id == replay_id for _, link_replay_id in linked)


def test_persistence_rejects_ownerless_derived_feature_evidence_before_writes(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)

    class OwnerlessExtractor:
        name = "ownerless"
        version = "ownerless-v1"
        feature_names = ("build.completed_count",)

        def extract(self, _context: FeatureContext) -> FeatureBundle:
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        1,
                        "count",
                        FeatureScope("player", player, player),
                        FeatureWindow(0, 120),
                        "complete",
                        None,
                        (),
                    ),
                ),
            )

    with pytest.raises(FeatureExtractionError, match="feature extraction failed") as caught:
        FeatureExtractionService(
            feature_factory,
            extractors=(OwnerlessExtractor(),),
        ).extract(_request(replay, player, "ownerless"))

    assert caught.value.__cause__ is not None
    assert str(caught.value.__cause__) == "available feature requires direct observed input evidence"
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(Feature)) == 0
        assert session.scalar(select(func.count()).select_from(EvidenceItem).where(EvidenceItem.tier == "derived")) == 0


def test_extraction_rejects_cross_attempt_parser_and_telemetry_owners_before_writes(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(
        feature_factory,
        telemetry_parser_run_id="00000000-0000-4000-8000-000000000399",
    )

    with pytest.raises(FeatureExtractionError, match="parser.*telemetry|branch"):
        FeatureExtractionService(feature_factory).extract(_request(replay, player, "build"))

    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(FeatureSet)) == 0
        assert session.scalar(select(func.count()).select_from(Feature)) == 0


def test_build_template_uses_and_links_authoritative_object_creation_evidence(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    receipt = FeatureExtractionService(feature_factory).extract(_request(replay, player, "build"))[0]
    sequence = next(value for value in receipt.features if value.name == "build.completed_sequence")
    assert sequence.raw_value == ((("frame", 20), ("template_name", "ChinaPowerPlant")),)
    assert {ref.source_key for ref in sequence.input_evidence} == {
        "telemetry:00000000-0000-4000-8000-000000000304:sequence:0",
        "telemetry:00000000-0000-4000-8000-000000000304:sequence:1",
    }
    with feature_factory() as session:
        persisted = session.scalar(select(Feature).where(Feature.name == "build.completed_sequence"))
        assert persisted is not None and persisted.json_value == [{"frame": 20, "template_name": "ChinaPowerPlant"}]
        linked = session.scalars(
            select(EvidenceItem.source_key)
            .join(FeatureEvidence, FeatureEvidence.evidence_item_id == EvidenceItem.id)
            .where(FeatureEvidence.feature_id == persisted.id)
        ).all()
        assert set(linked) == {
            "telemetry:00000000-0000-4000-8000-000000000304:sequence:0",
            "telemetry:00000000-0000-4000-8000-000000000304:sequence:1",
        }


@pytest.mark.parametrize("source_kind", ["parser", "telemetry"])
@pytest.mark.parametrize("defect", ["tier", "replay", "run"])
def test_context_boundary_rejects_malformed_persisted_evidence_links(
    feature_factory: sessionmaker[Session],
    feature_engine: Engine,
    source_kind: str,
    defect: str,
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    other_replay, _, _ = _seed_replay(
        feature_factory,
        replay_public_id="00000000-0000-4000-8000-000000000311",
        replay_sha256="d" * 64,
        replay_player_public_id="00000000-0000-4000-8000-000000000312",
        parser_run_id="00000000-0000-4000-8000-000000000313",
        telemetry_run_id="00000000-0000-4000-8000-000000000314",
    )
    with feature_engine.begin() as connection:
        connection.execute(text("DROP TRIGGER trg_evidence_items_observed_no_update"))
        evidence_id = connection.execute(
            text(
                "SELECT id FROM evidence_items WHERE source_kind = :source_kind AND replay_id = "
                "(SELECT id FROM replays WHERE public_id = :replay) ORDER BY id LIMIT 1"
            ),
            {"source_kind": source_kind, "replay": replay},
        ).scalar_one()
        if defect == "tier":
            connection.execute(
                text("UPDATE evidence_items SET tier = 'derived' WHERE id = :id"), {"id": evidence_id}
            )
        elif defect == "replay":
            connection.execute(
                text(
                    "UPDATE evidence_items SET replay_id = (SELECT id FROM replays WHERE public_id = :other) "
                    "WHERE id = :id"
                ),
                {"other": other_replay, "id": evidence_id},
            )
        else:
            run_column = "parser_run_id" if source_kind == "parser" else "telemetry_run_id"
            run_table = "parser_runs" if source_kind == "parser" else "telemetry_runs"
            connection.execute(
                text(
                    f"UPDATE evidence_items SET {run_column} = "
                    f"(SELECT id FROM {run_table} WHERE replay_id = "
                    "(SELECT id FROM replays WHERE public_id = :other)) WHERE id = :id"
                ),
                {"other": other_replay, "id": evidence_id},
            )
    with pytest.raises(FeatureExtractionError, match="malformed persisted evidence"):
        FeatureExtractionService(feature_factory).extract(_request(replay, player, "build"))


def test_cache_changes_for_settings_and_extractor_version_without_mutating_old_sets(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    original = FeatureExtractionService(feature_factory).extract(_request(replay, player, "build"))[0]
    changed_settings = FeatureExtractionService(feature_factory).extract(
        _request(replay, player, "build", settings={"policy": "strict"})
    )[0]

    class BuildV2(BuildOrderExtractor):
        version = "build-v2"

    changed_version = FeatureExtractionService(feature_factory, extractors=(BuildV2(),)).extract(
        _request(replay, player, "build")
    )[0]
    assert len({original.cache_key, changed_settings.cache_key, changed_version.cache_key}) == 3
    with feature_factory() as session:
        sets = session.scalars(select(FeatureSet).where(FeatureSet.status == "succeeded")).all()
        assert len(sets) == 3
        first = session.scalar(select(FeatureSet).where(FeatureSet.public_id == original.feature_set_public_id))
        assert first is not None and first.cache_key == original.cache_key and first.status == "succeeded"


def test_competing_misses_return_one_winner(feature_factory: sessionmaker[Session]) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    request = _request(replay, player, "build")
    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(lambda _: FeatureExtractionService(feature_factory).extract(request)[0], range(2)))
    assert len({receipt.feature_set_public_id for receipt in receipts}) == 1
    assert sorted(receipt.cache_hit for receipt in receipts) == [False, True]
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(FeatureSet).where(FeatureSet.status == "succeeded")) == 1


def test_persistence_failure_rolls_back_all_children_and_retains_typed_failure(
    feature_factory: sessionmaker[Session], feature_engine: Engine
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    with feature_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER task6_fail_feature BEFORE INSERT ON features "
                "WHEN NEW.name = 'build.completed_sequence' BEGIN SELECT RAISE(ABORT, 'forced task6 failure'); END"
            )
        )
    with pytest.raises(FeatureExtractionError, match="persist"):
        FeatureExtractionService(feature_factory).extract(_request(replay, player, "build"))
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(Feature)) == 0
        assert session.scalar(select(func.count()).select_from(FeatureEvidence)) == 0
        assert session.scalar(select(func.count()).select_from(EvidenceItem).where(EvidenceItem.tier == "derived")) == 0
        failed = session.scalars(select(FeatureSet).where(FeatureSet.status == "failed")).all()
        assert len(failed) == 1 and failed[0].error_json["code"] == "feature_persistence_failed"


def test_cross_replay_or_inferred_input_is_rejected_without_persisted_children(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    other_replay, _, other_evidence = _seed_replay(
        feature_factory,
        replay_public_id="00000000-0000-4000-8000-000000000311",
        replay_sha256="d" * 64,
        replay_player_public_id="00000000-0000-4000-8000-000000000312",
        parser_run_id="00000000-0000-4000-8000-000000000313",
        telemetry_run_id="00000000-0000-4000-8000-000000000314",
    )
    assert other_replay != replay

    class BadExtractor:
        name = "bad"
        version = "bad-v1"
        feature_names = ("build.completed_count",)

        def __init__(self, tier: str) -> None:
            self._tier = tier

        def extract(self, context: object) -> FeatureBundle:
            ref = EvidenceRef(other_evidence[0], cast(object, self._tier), "telemetry", "foreign", "telemetry-v2")  # type: ignore[arg-type]
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        1,
                        "count",
                        FeatureScope("player", player, player),
                        FeatureWindow(0, 120),
                        "complete",
                        None,
                        (ref,),
                    ),
                ),
            )

    for tier in ("observed", "inferred"):
        with pytest.raises(FeatureExtractionError):
            FeatureExtractionService(feature_factory, extractors=(BadExtractor(tier),)).extract(
                _request(replay, player, "bad")
            )
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(Feature)) == 0
        assert session.scalar(select(func.count()).select_from(FeatureEvidence)) == 0


@pytest.mark.parametrize("source", ["absent_context", "other_run"])
def test_persistence_rejects_same_replay_evidence_not_authorized_by_exact_context(
    feature_factory: sessionmaker[Session],
    source: str,
) -> None:
    replay, player, evidence_ids = _seed_replay(feature_factory)
    with feature_factory() as session:
        replay_row = session.scalar(select(Replay).where(Replay.public_id == replay))
        assert replay_row is not None
        if source == "absent_context":
            evidence = session.scalar(select(EvidenceItem).where(EvidenceItem.public_id == evidence_ids[-1]))
        else:
            run = TelemetryRun(
                run_id="00000000-0000-4000-8000-000000000318",
                replay_id=replay_row.id,
                schema_version=2,
                engine_build="fixture-other",
                settings_json={},
                status="failed",
                runner_status="failed",
                diagnostics_json={},
                started_at=datetime(2026, 8, 22, tzinfo=UTC),
                completed_at=datetime(2026, 8, 22, tzinfo=UTC),
            )
            session.add(run)
            session.flush()
            evidence = EvidenceItem(
                public_id="00000000-0000-4000-8000-000000000319",
                replay_id=replay_row.id,
                telemetry_run_id=run.id,
                tier="observed",
                source_kind="telemetry",
                source_key="telemetry:other-run:orphan",
                schema_version=2,
                created_at=datetime(2026, 8, 22, tzinfo=UTC),
            )
            session.add(evidence)
            session.commit()
        assert evidence is not None
        ref = EvidenceRef(
            evidence.public_id,
            "observed",
            evidence.source_kind,
            evidence.source_key,
            f"{evidence.source_kind}-v{evidence.schema_version}",
        )

    class DefectiveExtractor:
        name = "defective"
        version = "defective-v1"
        feature_names = ("build.completed_count",)

        def extract(self, context: object) -> FeatureBundle:
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        1,
                        "count",
                        FeatureScope("player", player, player),
                        FeatureWindow(0, 120),
                        "complete",
                        None,
                        (ref,),
                    ),
                ),
            )

    with pytest.raises(FeatureExtractionError, match="feature persistence failed") as caught:
        FeatureExtractionService(feature_factory, extractors=(DefectiveExtractor(),)).extract(
            _request(replay, player, "defective")
        )
    assert caught.value.__cause__ is not None
    assert str(caught.value.__cause__) == "feature evidence is not authorized by exact feature context"
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(Feature)) == 0
        assert session.scalar(select(func.count()).select_from(FeatureEvidence)) == 0


def test_persistence_rejects_forged_input_hidden_by_exact_later_cross_role_duplicates(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    base_service = FeatureExtractionService(feature_factory)
    context = base_service._build_context(_request(replay, player, "build"))
    exact = context.observed[0].ref
    forged = replace(exact, source_key=f"{exact.source_key}:forged")

    class CrossRoleCollisionExtractor:
        name = "cross_role_collision"
        version = "cross-role-collision-v1"
        feature_names = ("build.completed_count",)

        def extract(self, context: FeatureContext) -> FeatureBundle:
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        1,
                        "count",
                        FeatureScope("player", player, player),
                        FeatureWindow(0, 120),
                        "complete",
                        None,
                        (forged,),
                        (exact,),
                        (exact,),
                    ),
                ),
            )

    with pytest.raises(FeatureExtractionError, match="feature persistence failed") as caught:
        FeatureExtractionService(
            feature_factory,
            extractors=(CrossRoleCollisionExtractor(),),
        ).extract(_request(replay, player, "cross_role_collision"))
    assert caught.value.__cause__ is not None
    assert str(caught.value.__cause__) == "feature evidence is not authorized by exact feature context"
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(Feature)) == 0
        assert session.scalar(select(func.count()).select_from(FeatureEvidence)) == 0


def test_persistence_rejects_same_reference_as_supporting_and_contradicting_with_full_rollback(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    context = FeatureExtractionService(feature_factory)._build_context(_request(replay, player, "build"))
    exact = context.observed[0].ref

    class ExactRolesExtractor:
        name = "exact_roles"
        version = "exact-roles-v1"
        feature_names = ("build.completed_count",)

        def extract(self, context: FeatureContext) -> FeatureBundle:
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        1,
                        "count",
                        FeatureScope("player", player, player),
                        FeatureWindow(0, 120),
                        "complete",
                        None,
                        (exact,),
                        (exact,),
                        (exact,),
                    ),
                ),
            )

    with pytest.raises(
        FeatureExtractionError,
        match="feature evidence cannot be both supporting and contradicting",
    ):
        FeatureExtractionService(
            feature_factory,
            extractors=(ExactRolesExtractor(),),
        ).extract(_request(replay, player, "exact_roles"))
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(FeatureSet)) == 0
        assert session.scalar(select(func.count()).select_from(Feature)) == 0
        assert session.scalar(select(func.count()).select_from(FeatureEvidence)) == 0
        assert (
            session.scalar(
                select(func.count()).select_from(EvidenceItem).where(EvidenceItem.tier == "derived")
            )
            == 0
        )


def test_persistence_allows_input_and_supporting_reuse_without_contradiction(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    context = FeatureExtractionService(feature_factory)._build_context(_request(replay, player, "build"))
    exact = context.observed[0].ref

    class ExactInputAndSupportingExtractor:
        name = "exact_input_supporting"
        version = "exact-input-supporting-v1"
        feature_names = ("build.completed_count",)

        def extract(self, context: FeatureContext) -> FeatureBundle:
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        1,
                        "count",
                        FeatureScope("player", player, player),
                        FeatureWindow(0, 120),
                        "complete",
                        None,
                        (exact,),
                        (exact,),
                        (),
                    ),
                ),
            )

    receipt = FeatureExtractionService(
        feature_factory,
        extractors=(ExactInputAndSupportingExtractor(),),
    ).extract(_request(replay, player, "exact_input_supporting"))[0]
    assert receipt.features[0].input_evidence == (exact,)
    assert receipt.features[0].supporting_evidence == (exact,)
    assert receipt.features[0].contradicting_evidence == ()
    with feature_factory() as session:
        assert tuple(
            session.scalars(select(FeatureEvidence.role).order_by(FeatureEvidence.role)).all()
        ) == ("input", "supporting")


def _permuted_json(value: object, randomizer: random.Random) -> object:
    if isinstance(value, dict):
        items = list(value.items())
        randomizer.shuffle(items)
        return {key: _permuted_json(item, randomizer) for key, item in items}
    if isinstance(value, list):
        return [_permuted_json(item, randomizer) for item in value]
    return value


def test_one_hundred_semantic_permutations_have_identical_context_cache_receipt_and_persistence(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    base_service = FeatureExtractionService(feature_factory)
    base = base_service._build_context(_request(replay, player, "build"))
    randomizer = random.Random(0x6A11CE)
    contexts: list[FeatureContext] = []
    evidence_link_orders: list[tuple[EvidenceRef, ...]] = []
    for _ in range(100):
        observed_items = [
            ObservedEvidence(
                item.ref,
                item.frame,
                item.event_type,
                _permuted_json(thaw_canonical(item.facts), randomizer),
            )
            for item in base.observed
        ]
        randomizer.shuffle(observed_items)
        link_order = [item.ref for item in observed_items]
        randomizer.shuffle(link_order)
        evidence_link_orders.append(tuple(link_order))
        settings_items = [("alpha", {"x": 1, "y": 2}), ("beta", [3, 2, 1]), ("gamma", True)]
        randomizer.shuffle(settings_items)
        contexts.append(
            replace(
                base,
                observed=tuple(observed_items),
                settings=_permuted_json(dict(settings_items), randomizer),
            )
        )
    expected_json = canonical_json(contexts[0])
    expected_digest = input_digest(contexts[0])
    expected_key = cache_key(contexts[0], "permutation", "permutation-v1")
    assert all(canonical_json(context) == expected_json for context in contexts)
    assert all(input_digest(context) == expected_digest for context in contexts)
    assert all(cache_key(context, "permutation", "permutation-v1") == expected_key for context in contexts)

    class PermutationExtractor:
        name = "permutation"
        version = "permutation-v1"
        feature_names = ("build.completed_count",)

        def __init__(self) -> None:
            self.calls = 0

        def extract(self, context: FeatureContext) -> FeatureBundle:
            refs = [item.ref for item in context.observed]
            random.Random(self.calls).shuffle(refs)
            self.calls += 1
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        len(context.observed),
                        "count",
                        context.scope,
                        FeatureWindow(0, context.final_frame or 0),
                        "complete",
                        None,
                        tuple(refs),
                    ),
                ),
            )

    class PermutedContextService(FeatureExtractionService):
        def __init__(self, contexts: list[FeatureContext], extractor: PermutationExtractor) -> None:
            super().__init__(feature_factory, extractors=(extractor,))
            self._contexts = iter(contexts)

        def _build_context(self, request: ExtractFeaturesRequest) -> FeatureContext:
            return next(self._contexts)

    extractor = PermutationExtractor()
    service = PermutedContextService(contexts, extractor)
    receipts = [service.extract(_request(replay, player, "permutation"))[0] for _ in range(100)]
    expected_receipt = (
        receipts[0].feature_set_public_id,
        receipts[0].input_digest,
        receipts[0].cache_key,
        receipts[0].features,
    )
    assert all(
        (receipt.feature_set_public_id, receipt.input_digest, receipt.cache_key, receipt.features) == expected_receipt
        for receipt in receipts
    )
    assert extractor.calls == 1
    for link_order in evidence_link_orders:
        validated = validate_feature_value(
            replace(receipts[0].features[0], input_evidence=link_order),
            service._registry,
        )
        assert validated.input_evidence == receipts[0].features[0].input_evidence
    with feature_factory() as session:
        persisted = session.scalar(select(Feature).where(Feature.name == "build.completed_count"))
        assert persisted is not None and persisted.integer_value == len(base.observed)
        links = session.scalars(
            select(EvidenceItem.public_id)
            .join(FeatureEvidence, FeatureEvidence.evidence_item_id == EvidenceItem.id)
            .where(FeatureEvidence.feature_id == persisted.id)
            .order_by(EvidenceItem.source_kind, EvidenceItem.source_key, EvidenceItem.public_id)
        ).all()
        assert tuple(links) == tuple(ref.public_id for ref in receipts[0].features[0].input_evidence)


def test_service_cache_invalidates_for_observed_fact_scope_catalog_and_schema_identity(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    base = FeatureExtractionService(feature_factory)._build_context(_request(replay, player, "build"))
    first_observation = base.observed[0]
    changed_facts = replace(
        first_observation,
        facts={
            **cast(dict[str, object], thaw_canonical(first_observation.facts)),
            "semantic_change": "observed",
        },
    )
    variants = [
        base,
        replace(base, observed=(changed_facts,) + base.observed[1:]),
        replace(
            base,
            replay_player_public_id=None,
            scope=FeatureScope("replay", base.replay_public_id),
        ),
        replace(base, catalog_identity="f" * 64),
        replace(base, observation_schema_versions=(("parser", "1:changed"), ("telemetry", "2:changed"))),
    ]
    registry = FeatureRegistry(
        REGISTRY_SCHEMA,
        (
            FeatureDefinition(
                "build.completed_count",
                "integer",
                "count",
                ("player", "replay"),
                "inclusive",
                "observed",
                "build",
            ),
        ),
    )

    class InvalidationExtractor:
        name = "invalidation"
        version = "invalidation-v1"
        feature_names = ("build.completed_count",)

        def extract(self, context: FeatureContext) -> FeatureBundle:
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        len(context.observed),
                        "count",
                        context.scope,
                        FeatureWindow(0, context.final_frame or 0),
                        "complete",
                        None,
                        (context.observed[0].ref,),
                    ),
                ),
            )

    class VariantService(FeatureExtractionService):
        def __init__(self) -> None:
            super().__init__(feature_factory, extractors=(InvalidationExtractor(),), registry=registry)
            self._variants = iter(variants)

        def _build_context(self, request: ExtractFeaturesRequest) -> FeatureContext:
            return next(self._variants)

    service = VariantService()
    receipts = [service.extract(_request(replay, player, "invalidation"))[0] for _ in variants]
    assert len({receipt.input_digest for receipt in receipts}) == len(variants)
    assert len({receipt.cache_key for receipt in receipts}) == len(variants)
    assert all(receipt.cache_hit is False for receipt in receipts)
    with feature_factory() as session:
        assert session.scalar(select(func.count()).select_from(FeatureSet).where(FeatureSet.status == "succeeded")) == 5


def test_service_rejects_unknown_identity_extractor_and_duplicate_configuration(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    service = FeatureExtractionService(feature_factory)
    with pytest.raises(FeatureExtractionError, match="unknown extractor"):
        service.extract(_request(replay, player, "missing"))
    with pytest.raises(FeatureExtractionError, match="unknown replay"):
        service.extract(_request("00000000-0000-4000-8000-000000000399", player, "build"))
    with pytest.raises(FeatureExtractionError, match="does not belong"):
        service.extract(_request(replay, "00000000-0000-4000-8000-000000000399", "build"))
    with pytest.raises(ValueError, match="unique"):
        FeatureExtractionService(feature_factory, extractors=(BuildOrderExtractor(), BuildOrderExtractor()))
    with pytest.raises(ValueError, match="unique"):
        ExtractFeaturesRequest(replay, player, ("build", "build"))


def _seed_replay_wide_observations(
    factory: sessionmaker[Session],
    *,
    parser_run_setting: object = "00000000-0000-4000-8000-000000000303",
    slots: object | None = None,
    include_conflicting_parser: bool = True,
    entity_owner_player_index: int = 0,
    engine_player_indices: object | None = None,
) -> tuple[str, str, str, str, dict[str, str]]:
    replay, target, _ = _seed_replay(factory, finalize=False)
    peer = "00000000-0000-4000-8000-000000000501"
    conflicting_peer = "00000000-0000-4000-8000-000000000502"
    now = datetime(2026, 8, 22, tzinfo=UTC)
    evidence_ids: dict[str, str] = {}
    with factory() as session:
        replay_row = session.scalar(select(Replay).where(Replay.public_id == replay))
        parser = session.scalar(
            select(ParserRun).where(ParserRun.run_id == "00000000-0000-4000-8000-000000000303")
        )
        telemetry = session.scalar(
            select(TelemetryRun).where(TelemetryRun.run_id == "00000000-0000-4000-8000-000000000304")
        )
        assert replay_row is not None and parser is not None and telemetry is not None
        entity = session.scalar(select(Entity).where(Entity.telemetry_run_id == telemetry.id, Entity.object_id == 7))
        assert entity is not None
        entity.initial_owner_player_index = entity_owner_player_index
        session.add_all(
            Entity(
                public_id=str(uuid5(NAMESPACE_URL, f"{telemetry.run_id}:entity:{object_id}")),
                telemetry_run_id=telemetry.id,
                replay_id=replay_row.id,
                object_id=object_id,
                template_name=template_name,
                initial_owner_player_index=owner_player_index,
                kind_of_flags_json=kind_of_flags,
                creation_sequence=None,
                creation_frame=0,
                observed_json={"source": "object_created"},
            )
            for object_id, template_name, owner_player_index, kind_of_flags in (
                (10, "TargetUnit", 0, ["MOBILE"]),
                (20, "PeerUnit", 1, ["MOBILE"]),
                (77, "SupplyDock", None, ["SUPPLY_SOURCE"]),
            )
        )
        session.add(
            ReplayPlayer(
                public_id=peer,
                replay_id=replay_row.id,
                parser_run_id=parser.id,
                slot_index=1,
                slot_kind="human",
                original_name="Peer",
                normalized_name="peer",
                player_index=1,
                observed_json={"player_index": 1},
            )
        )
        if include_conflicting_parser:
            conflicting = ParserRun(
                run_id="00000000-0000-4000-8000-000000000509",
                replay_id=replay_row.id,
                parser_version="parser-conflicting-v1",
                schema_version=1,
                input_sha256=replay_row.sha256,
                status="running",
                warnings_json=[],
                started_at=now,
            )
            session.add(conflicting)
            session.flush()
            session.add_all(
                (
                    ReplayPlayer(
                        public_id="00000000-0000-4000-8000-000000000503",
                        replay_id=replay_row.id,
                        parser_run_id=conflicting.id,
                        slot_index=0,
                        slot_kind="human",
                        original_name="Wrong Target",
                        normalized_name="wrong target",
                        player_index=0,
                        observed_json={"player_index": 0},
                    ),
                    ReplayPlayer(
                        public_id=conflicting_peer,
                        replay_id=replay_row.id,
                        parser_run_id=conflicting.id,
                        slot_index=1,
                        slot_kind="human",
                        original_name="Wrong Peer",
                        normalized_name="wrong peer",
                        player_index=1,
                        observed_json={"player_index": 1},
                    ),
                )
            )
            session.flush()
            conflicting.status = "succeeded"
            conflicting.completion_status = "complete"
            conflicting.result_sha256 = "9" * 64
            conflicting.completed_at = now
        telemetry.settings_json = {"parser_run_id": parser_run_setting}
        resolved_slots = (
            [
                {"slot_index": 0, "player_index": 0, "resolution_status": "resolved"},
                {"slot_index": 1, "player_index": 1, "resolution_status": "resolved"},
                {"slot_index": 7, "player_index": None, "resolution_status": "not_applicable"},
            ]
            if slots is None
            else slots
        )
        event_specs = (
            (
                0,
                "players_initialized",
                {
                    "slots": resolved_slots,
                    "engine_player_indices": [0, 1] if engine_player_indices is None else engine_player_indices,
                    "game_data_catalog": {"path": "C:\\private\\catalog.json"},
                    "private_locator": "must-not-survive",
                },
            ),
            (30, "supply_collected", {"player_index": 0, "source_object_id": 77, "amount": 100.0}),
            (31, "supply_collected", {"player_index": 1, "source_object_id": 77, "amount": 200.0}),
            (
                40,
                "entity_sample",
                {
                    "object_id": 10,
                    "owner_player_index": 0,
                    "position": {"x": 1.0, "y": 2.0, "z": 0.0},
                    "position_status": "bounded",
                },
            ),
            (
                41,
                "entity_sample",
                {
                    "object_id": 20,
                    "owner_player_index": 1,
                    "position": {"x": 3.0, "y": 4.0, "z": 0.0},
                    "position_status": "bounded",
                },
            ),
        )
        for offset, (frame, event_type, payload) in enumerate(event_specs, start=3):
            public_id = str(uuid5(NAMESPACE_URL, f"{telemetry.run_id}:{offset}"))
            evidence = EvidenceItem(
                public_id=public_id,
                replay_id=replay_row.id,
                telemetry_run_id=telemetry.id,
                tier="observed",
                source_kind="telemetry",
                source_key=f"telemetry:{telemetry.run_id}:sequence:{offset}",
                schema_version=2,
                created_at=now,
            )
            session.add(evidence)
            session.flush()
            session.add(
                TelemetryEvent(
                    telemetry_run_id=telemetry.id,
                    sequence=offset,
                    frame=frame,
                    logic_time_seconds=frame / 30.0,
                    schema_version=2,
                    event_type=event_type,
                    payload_json=payload,
                    raw_record_json={"event_type": event_type, "payload": payload},
                    evidence_item_id=evidence.id,
                )
            )
            evidence_ids[f"{event_type}:{frame}"] = public_id
        parser.status = "succeeded"
        parser.completion_status = "complete"
        parser.result_sha256 = "b" * 64
        parser.completed_at = now
        telemetry.status = "succeeded"
        telemetry.final_frame = 120
        telemetry.command_count = 0
        telemetry.trace_sha256 = "c" * 64
        telemetry.completed_at = now
        session.commit()
    return replay, target, peer, conflicting_peer, evidence_ids


def _context_capture_extractor(policy: str | None = None) -> object:
    class ContextCaptureExtractor:
        name = "context_capture"
        version = "context-capture-v1"
        feature_names = ("build.completed_count",)

        def __init__(self) -> None:
            self.context: FeatureContext | None = None

        def extract(self, context: FeatureContext) -> FeatureBundle:
            self.context = context
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        len(context.observed),
                        "count",
                        context.scope,
                        FeatureWindow(0, context.final_frame or 0),
                        "complete",
                        None,
                        (context.observed[0].ref,),
                    ),
                ),
            )

    extractor = ContextCaptureExtractor()
    if policy is not None:
        extractor.observation_policy = policy  # type: ignore[attr-defined]
    return extractor


def test_mixed_scope_extractor_emits_only_definitions_registered_for_context_scope(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    registry = FeatureRegistry(
        REGISTRY_SCHEMA,
        (
            FeatureDefinition("mixed.player_metric", "integer", "count", ("player",), "inclusive", "observed", "mixed"),
            FeatureDefinition("mixed.replay_metric", "integer", "count", ("replay",), "inclusive", "observed", "mixed"),
        ),
    )

    class MixedScopeExtractor:
        name = "mixed"
        version = "mixed-v1"
        feature_names = ("mixed.player_metric", "mixed.replay_metric")

        def extract(self, context: FeatureContext) -> FeatureBundle:
            name = "mixed.player_metric" if context.scope.scope_type == "player" else "mixed.replay_metric"
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        name,
                        "integer",
                        1,
                        "count",
                        context.scope,
                        FeatureWindow(0, context.final_frame or 0),
                        "complete",
                        None,
                        (context.observed[0].ref,),
                    ),
                ),
            )

    service = FeatureExtractionService(feature_factory, extractors=(MixedScopeExtractor(),), registry=registry)
    player_receipt = service.extract(_request(replay, player, "mixed"))[0]
    replay_receipt = service.extract(ExtractFeaturesRequest(replay, None, ("mixed",)))[0]
    assert tuple(value.name for value in player_receipt.features) == ("mixed.player_metric",)
    assert tuple(value.name for value in replay_receipt.features) == ("mixed.replay_metric",)


@pytest.mark.parametrize("defect", ("missing", "extra", "duplicate", "wrong_scope"))
def test_mixed_scope_bundle_still_rejects_nonexact_or_wrong_scope_values(
    feature_factory: sessionmaker[Session], defect: str
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    registry = FeatureRegistry(
        REGISTRY_SCHEMA,
        (
            FeatureDefinition("mixed.player_metric", "integer", "count", ("player",), "inclusive", "observed", "mixed"),
            FeatureDefinition("mixed.replay_metric", "integer", "count", ("replay",), "inclusive", "observed", "mixed"),
        ),
    )

    class MixedScopeExtractor:
        name = "mixed_invalid"
        version = "mixed-invalid-v1"
        feature_names = ("mixed.player_metric", "mixed.replay_metric")

        def extract(self, context: FeatureContext) -> FeatureBundle:
            raise AssertionError("not called")

    extractor = MixedScopeExtractor()
    service = FeatureExtractionService(feature_factory, extractors=(extractor,), registry=registry)
    context = service._build_context(_request(replay, player, "mixed_invalid"))

    def value(name: str, scope: FeatureScope) -> FeatureValue:
        return FeatureValue(
            name,
            "integer",
            1,
            "count",
            scope,
            FeatureWindow(0, context.final_frame or 0),
            "complete",
            None,
            (context.observed[0].ref,),
        )

    player_value = value("mixed.player_metric", context.scope)
    defects = {
        "missing": (),
        "extra": (player_value, value("mixed.replay_metric", FeatureScope("replay", replay))),
        "duplicate": (player_value, player_value),
        "wrong_scope": (value("mixed.player_metric", FeatureScope("replay", replay)),),
    }
    bundle = FeatureBundle(extractor.name, extractor.version, defects[defect])
    with pytest.raises((FeatureExtractionError, ValueError)):
        service._validated_bundle(bundle, extractor, context)


def test_default_observation_policy_keeps_target_player_filtering(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, target, peer, _, _ = _seed_replay_wide_observations(feature_factory)
    extractor = _context_capture_extractor()
    FeatureExtractionService(feature_factory, extractors=(cast(object, extractor),)).extract(
        _request(replay, target, "context_capture")
    )
    context = cast(FeatureContext, extractor.context)  # type: ignore[attr-defined]
    facts = [cast(dict[str, object], thaw_canonical(item.facts)) for item in context.observed]
    assert any(item.get("replay_player_public_id") == target for item in facts)
    assert all(item.get("replay_player_public_id") != peer for item in facts)
    assert [item.frame for item in context.observed if item.event_type == "entity_sample"] == [40]


def test_replay_wide_policy_includes_peer_facts_and_projects_selected_parser_owners(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, target, peer, conflicting_peer, evidence_ids = _seed_replay_wide_observations(feature_factory)
    extractor = _context_capture_extractor("replay_wide_telemetry")
    FeatureExtractionService(feature_factory, extractors=(cast(object, extractor),)).extract(
        _request(replay, target, "context_capture")
    )
    context = cast(FeatureContext, extractor.context)  # type: ignore[attr-defined]
    observations = {
        (item.event_type, item.frame): cast(dict[str, object], thaw_canonical(item.facts))
        for item in context.observed
    }
    assert observations[("entity_sample", 40)] == {
        "object_id": 10,
        "object_key": "object:10",
        "owner_player_index": 0,
        "owner_scope_key": target,
        "position": {"x": 1.0, "y": 2.0, "z": 0.0},
        "position_status": "bounded",
        "replay_player_public_id": target,
    }
    assert observations[("entity_sample", 41)]["owner_scope_key"] == peer
    assert observations[("entity_sample", 41)]["replay_player_public_id"] == peer
    assert observations[("supply_collected", 31)]["replay_player_public_id"] == peer
    assert conflicting_peer not in canonical_json(context)
    assert "private_locator" not in canonical_json(context)
    assert "catalog.json" not in canonical_json(context)
    slots = cast(list[dict[str, object]], observations[("players_initialized", 0)]["slots"])
    assert slots == [
        {
            "owner_scope_key": target,
            "player_index": 0,
            "replay_player_public_id": target,
            "resolution_status": "resolved",
            "slot_index": 0,
        },
        {
            "owner_scope_key": peer,
            "player_index": 1,
            "replay_player_public_id": peer,
            "resolution_status": "resolved",
            "slot_index": 1,
        },
    ]
    initialized = next(item for item in context.observed if item.event_type == "players_initialized")
    assert initialized.ref.public_id == evidence_ids["players_initialized:0"]
    assert all(item.event_type != "player_start_assignment" for item in context.observed)


def test_replay_wide_peer_evidence_is_authorized_and_changes_digest(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, target, _, _, evidence_ids = _seed_replay_wide_observations(feature_factory)

    class PeerEvidenceExtractor:
        name = "peer_evidence"
        version = "peer-evidence-v1"
        feature_names = ("build.completed_count",)
        observation_policy = "replay_wide_telemetry"

        def __init__(self) -> None:
            self.context: FeatureContext | None = None

        def extract(self, context: FeatureContext) -> FeatureBundle:
            self.context = context
            peer = next(item for item in context.observed if item.ref.public_id == evidence_ids["supply_collected:31"])
            return FeatureBundle(
                self.name,
                self.version,
                (
                    FeatureValue(
                        "build.completed_count",
                        "integer",
                        1,
                        "count",
                        context.scope,
                        FeatureWindow(0, context.final_frame or 0),
                        "complete",
                        None,
                        (peer.ref,),
                    ),
                ),
            )

    extractor = PeerEvidenceExtractor()
    receipt = FeatureExtractionService(feature_factory, extractors=(extractor,)).extract(
        _request(replay, target, "peer_evidence")
    )[0]
    assert receipt.features[0].input_evidence[0].public_id == evidence_ids["supply_collected:31"]
    assert extractor.context is not None
    peer_observation = next(
        item for item in extractor.context.observed if item.ref.public_id == evidence_ids["supply_collected:31"]
    )
    changed = replace(
        peer_observation,
        facts={**cast(dict[str, object], thaw_canonical(peer_observation.facts)), "amount": 201.0},
    )
    changed_context = replace(
        extractor.context,
        observed=tuple(changed if item == peer_observation else item for item in extractor.context.observed),
    )
    assert input_digest(changed_context) != receipt.input_digest
    assert cache_key(changed_context, extractor.name, extractor.version) != receipt.cache_key


@pytest.mark.parametrize(
    ("parser_run_setting", "slots"),
    (
        (None, None),
        (123, None),
        ("00000000-0000-4000-8000-000000000599", None),
        ("00000000-0000-4000-8000-000000000303", []),
        ("00000000-0000-4000-8000-000000000303", "not-a-slot-list"),
        ("00000000-0000-4000-8000-000000000303", [None]),
        (
            "00000000-0000-4000-8000-000000000303",
            [
                {"slot_index": 0, "player_index": 0, "resolution_status": "resolved"},
                {"slot_index": 0, "player_index": 1, "resolution_status": "resolved"},
            ],
        ),
        (
            "00000000-0000-4000-8000-000000000303",
            [{"slot_index": 7, "player_index": 0, "resolution_status": "resolved"}],
        ),
    ),
)
def test_replay_wide_policy_rejects_missing_ambiguous_or_mismatched_parser_slot_graph(
    feature_factory: sessionmaker[Session], parser_run_setting: object, slots: object | None
) -> None:
    replay, target, _, _, _ = _seed_replay_wide_observations(
        feature_factory,
        parser_run_setting=parser_run_setting,
        slots=slots,
        include_conflicting_parser=False,
    )
    extractor = _context_capture_extractor("replay_wide_telemetry")
    with pytest.raises(FeatureExtractionError, match="telemetry player mapping"):
        FeatureExtractionService(feature_factory, extractors=(cast(object, extractor),)).extract(
            _request(replay, target, "context_capture")
        )


def test_replay_wide_policy_rejects_request_player_from_a_different_parser_attempt(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, target, _, _, _ = _seed_replay_wide_observations(
        feature_factory,
        parser_run_setting="00000000-0000-4000-8000-000000000509",
    )
    extractor = _context_capture_extractor("replay_wide_telemetry")
    with pytest.raises(FeatureExtractionError, match="telemetry player mapping"):
        FeatureExtractionService(feature_factory, extractors=(cast(object, extractor),)).extract(
            _request(replay, target, "context_capture")
        )


def test_service_rejects_unknown_extractor_observation_policy(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, target, _, _, _ = _seed_replay_wide_observations(feature_factory)
    extractor = _context_capture_extractor("all_database_rows")
    with pytest.raises(FeatureExtractionError, match="observation policy"):
        FeatureExtractionService(feature_factory, extractors=(cast(object, extractor),)).extract(
            _request(replay, target, "context_capture")
        )


def test_bundle_validation_rejects_unregistered_duplicate_identity_and_context_scope(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, player, _ = _seed_replay(feature_factory)
    registry = FeatureRegistry(
        REGISTRY_SCHEMA,
        (
            FeatureDefinition(
                "mixed.shared_metric",
                "integer",
                "count",
                ("player", "replay"),
                "inclusive",
                "observed",
                "mixed",
            ),
        ),
    )
    context = FeatureExtractionService(feature_factory)._build_context(_request(replay, player, "build"))

    class DeclaredExtractor:
        name = "declared"
        version = "declared-v1"
        feature_names = ("mixed.shared_metric",)

        def extract(self, context: FeatureContext) -> FeatureBundle:
            raise AssertionError("not called")

    service = FeatureExtractionService(feature_factory, extractors=(DeclaredExtractor(),), registry=registry)
    replay_scoped = FeatureValue(
        "mixed.shared_metric",
        "integer",
        1,
        "count",
        FeatureScope("replay", replay),
        FeatureWindow(0, context.final_frame or 0),
        "complete",
        None,
        (context.observed[0].ref,),
    )
    with pytest.raises(FeatureExtractionError, match="scope does not match context"):
        service._validated_bundle(
            FeatureBundle("declared", "declared-v1", (replay_scoped,)),
            DeclaredExtractor(),
            context,
        )

    class DuplicateExtractor(DeclaredExtractor):
        feature_names = ("mixed.shared_metric", "mixed.shared_metric")

    player_scoped = replace(replay_scoped, scope=context.scope)
    with pytest.raises(FeatureExtractionError, match="duplicate logical feature identity"):
        service._validated_bundle(
            FeatureBundle("declared", "declared-v1", (player_scoped, player_scoped)),
            DuplicateExtractor(),
            context,
        )

    class UnregisteredExtractor(DeclaredExtractor):
        feature_names = ("missing.feature",)

    with pytest.raises(FeatureExtractionError, match="unregistered feature"):
        service._validated_bundle(
            FeatureBundle("declared", "declared-v1", ()),
            UnregisteredExtractor(),
            context,
        )


@pytest.mark.parametrize(
    "payload",
    (
        {"object_id": "10", "owner_player_index": 0},
        {"object_id": 10, "owner_player_index": True},
        {"object_id": 10, "owner_player_index": 99},
    ),
)
def test_entity_sample_projection_rejects_malformed_or_unmapped_identity(
    feature_factory: sessionmaker[Session], payload: object
) -> None:
    event = TelemetryEvent(event_type="entity_sample", payload_json=payload)
    with pytest.raises(FeatureExtractionError, match="entity sample"):
        FeatureExtractionService(feature_factory)._event_facts(
            event,
            {0: "00000000-0000-4000-8000-000000000302"},
            {10: ("Unit", "00000000-0000-4000-8000-000000000302", None)},
            None,
            [],
        )


def test_entity_sample_projection_rejects_object_outside_selected_entity_graph(
    feature_factory: sessionmaker[Session],
) -> None:
    event = TelemetryEvent(
        event_type="entity_sample",
        schema_version=2,
        payload_json={"object_id": 99, "owner_player_index": 0},
    )
    with pytest.raises(FeatureExtractionError, match="entity sample object"):
        FeatureExtractionService(feature_factory)._event_facts(
            event,
            {0: "00000000-0000-4000-8000-000000000302"},
            {10: ("Unit", "00000000-0000-4000-8000-000000000302", None)},
            None,
            [],
        )


def test_entity_sample_projection_accepts_object_in_selected_entity_graph(
    feature_factory: sessionmaker[Session],
) -> None:
    target = "00000000-0000-4000-8000-000000000302"
    event = TelemetryEvent(
        event_type="entity_sample",
        schema_version=2,
        payload_json={"object_id": 10, "owner_player_index": None},
    )
    facts = FeatureExtractionService(feature_factory)._event_facts(
        event,
        {0: target},
        {10: ("Unit", target, None)},
        None,
        [],
    )
    assert facts["object_key"] == "object:10"
    assert facts["owner_scope_key"] is None
    assert facts["replay_player_public_id"] is None


def test_replay_wide_supply_projection_rejects_unmapped_engine_owner(
    feature_factory: sessionmaker[Session],
) -> None:
    event = TelemetryEvent(
        event_type="supply_collected",
        payload_json={"player_index": 99, "source_object_id": 77, "amount": 100.0},
    )
    with pytest.raises(FeatureExtractionError, match="economy owner mapping"):
        FeatureExtractionService(feature_factory)._event_facts(
            event,
            {0: "00000000-0000-4000-8000-000000000302"},
            {},
            None,
            [],
        )


def test_replay_wide_economy_projection_allows_declared_neutral_engine_owner(
    feature_factory: sessionmaker[Session],
) -> None:
    event = TelemetryEvent(
        event_type="cash_changed",
        payload_json={"player_index": 1, "delta": 10000.0, "balance": 10000.0},
    )

    facts = FeatureExtractionService(feature_factory)._event_facts(
        event,
        {2: "00000000-0000-4000-8000-000000000302"},
        {},
        None,
        [],
        frozenset({1, 2}),
    )

    assert facts["replay_player_public_id"] is None


def test_replay_wide_entity_sample_allows_declared_neutral_engine_owner(
    feature_factory: sessionmaker[Session],
) -> None:
    event = TelemetryEvent(
        event_type="entity_sample",
        payload_json={"object_id": 77, "owner_player_index": 1, "position": {"x": 1.0, "y": 2.0}},
    )

    facts = FeatureExtractionService(feature_factory)._event_facts(
        event,
        {2: "00000000-0000-4000-8000-000000000302"},
        {77: ("NeutralStructure", None, "00000000-0000-4000-8000-000000000777")},
        None,
        [],
        frozenset({1, 2}),
    )

    assert facts["owner_scope_key"] is None
    assert facts["replay_player_public_id"] is None


@pytest.mark.parametrize("source_object_id", ("77", True, -1, 99))
def test_replay_wide_supply_projection_rejects_malformed_or_unknown_source_object(
    feature_factory: sessionmaker[Session], source_object_id: object
) -> None:
    event = TelemetryEvent(
        event_type="supply_collected",
        schema_version=2,
        payload_json={"player_index": 0, "source_object_id": source_object_id, "amount": 100.0},
    )
    with pytest.raises(FeatureExtractionError, match="supply source object"):
        FeatureExtractionService(feature_factory)._event_facts(
            event,
            {0: "00000000-0000-4000-8000-000000000302"},
            {77: ("SupplyDock", None, None)},
            None,
            [],
        )


@pytest.mark.parametrize("source_object_id", (None, 77))
def test_replay_wide_supply_projection_preserves_legitimate_nullable_source_object(
    feature_factory: sessionmaker[Session], source_object_id: object
) -> None:
    event = TelemetryEvent(
        event_type="supply_collected",
        schema_version=2,
        payload_json={"player_index": 0, "source_object_id": source_object_id, "amount": 100.0},
    )
    facts = FeatureExtractionService(feature_factory)._event_facts(
        event,
        {0: "00000000-0000-4000-8000-000000000302"},
        {77: ("SupplyDock", None, None)},
        None,
        [],
    )
    assert facts["source_object_id"] is source_object_id


@pytest.mark.parametrize(
    "payload",
    (
        {"source_player_mask": 1, "source_player_indices": [0, 99], "victim_player_index": 1},
        {"source_player_mask": 1, "source_player_indices": [0, True], "victim_player_index": 1},
        {"source_player_mask": 1, "source_player_indices": [0, 0], "victim_player_index": 1},
        {"source_player_mask": 1, "source_player_indices": "0", "victim_player_index": 1},
        {"source_player_mask": 1, "source_player_indices": [0], "victim_player_index": 99},
        {"source_player_mask": 1, "source_player_indices": [0], "victim_player_index": True},
        {"source_player_indices": [0], "victim_player_index": 1},
        {"source_player_mask": None, "source_player_indices": [0], "victim_player_index": 1},
        {"source_player_mask": True, "source_player_indices": [0], "victim_player_index": 1},
        {"source_player_mask": 2**32, "source_player_indices": [0], "victim_player_index": 1},
        {"source_player_mask": 2, "source_player_indices": [0], "victim_player_index": 1},
        {"source_player_mask": 0, "source_player_indices": None, "victim_player_index": 1},
    ),
)
def test_replay_wide_damage_projection_rejects_unmapped_ambiguous_or_malformed_players(
    feature_factory: sessionmaker[Session], payload: object
) -> None:
    event = TelemetryEvent(event_type="damage_applied", schema_version=2, payload_json=payload)
    with pytest.raises(FeatureExtractionError, match="telemetry damage"):
        FeatureExtractionService(feature_factory)._event_facts(
            event,
            {
                0: "00000000-0000-4000-8000-000000000302",
                1: "00000000-0000-4000-8000-000000000501",
            },
            {10: ("Structure", "00000000-0000-4000-8000-000000000302", None)},
            None,
            [],
        )


@pytest.mark.parametrize("source_indices", (None, []))
def test_replay_wide_damage_projection_preserves_explicit_unknown_players(
    feature_factory: sessionmaker[Session], source_indices: object
) -> None:
    event = TelemetryEvent(
        event_type="damage_applied",
        schema_version=1,
        payload_json={"source_player_indices": source_indices, "victim_player_index": None},
    )
    facts = FeatureExtractionService(feature_factory)._event_facts(
        event,
        {0: "00000000-0000-4000-8000-000000000302"},
        {},
        None,
        [],
    )
    assert facts["source_replay_player_public_ids"] == []
    assert facts["victim_replay_player_public_id"] is None


@pytest.mark.parametrize("source_mask", (True, "1", -1, 2**32))
def test_replay_wide_damage_v1_projection_rejects_malformed_optional_source_mask(
    feature_factory: sessionmaker[Session], source_mask: object
) -> None:
    event = TelemetryEvent(
        event_type="damage_applied",
        schema_version=1,
        payload_json={
            "source_player_mask": source_mask,
            "source_player_indices": None,
            "victim_player_index": None,
        },
    )
    with pytest.raises(FeatureExtractionError, match="telemetry damage source player mask"):
        FeatureExtractionService(feature_factory)._event_facts(event, {}, {}, None, [])


@pytest.mark.parametrize("source_mask", (None, 0, 0xFFFFFFFF))
def test_replay_wide_damage_v1_projection_accepts_nullable_uint32_source_mask_boundaries(
    feature_factory: sessionmaker[Session], source_mask: object
) -> None:
    event = TelemetryEvent(
        event_type="damage_applied",
        schema_version=1,
        payload_json={
            "source_player_mask": source_mask,
            "source_player_indices": None,
            "victim_player_index": None,
        },
    )
    facts = FeatureExtractionService(feature_factory)._event_facts(event, {}, {}, None, [])
    assert facts["source_player_mask"] is source_mask
    assert facts["source_replay_player_public_ids"] == []


@pytest.mark.parametrize("source_index", (32, 2**63))
def test_replay_wide_damage_v2_projection_rejects_out_of_uint32_mask_source_index_before_shift(
    feature_factory: sessionmaker[Session], source_index: int
) -> None:
    event = TelemetryEvent(
        event_type="damage_applied",
        schema_version=2,
        payload_json={
            "source_player_mask": 0,
            "source_player_indices": [source_index],
            "victim_player_index": None,
        },
    )
    with pytest.raises(FeatureExtractionError, match="telemetry damage source player mapping"):
        FeatureExtractionService(feature_factory)._event_facts(
            event,
            {source_index: f"player-{source_index}"},
            {},
            None,
            [],
        )


def test_replay_wide_damage_v2_projection_accepts_uint32_index_and_mask_boundaries(
    feature_factory: sessionmaker[Session],
) -> None:
    indices = list(range(32))
    players = {index: f"player-{index}" for index in indices}
    event = TelemetryEvent(
        event_type="damage_applied",
        schema_version=2,
        payload_json={
            "source_player_mask": 0xFFFFFFFF,
            "source_player_indices": indices,
            "victim_player_index": None,
        },
    )
    facts = FeatureExtractionService(feature_factory)._event_facts(event, players, {}, None, [])
    assert facts["source_replay_player_public_ids"] == [players[index] for index in indices]


@pytest.mark.parametrize(
    ("schema_version", "source_mask", "source_indices", "expected_sources"),
    (
        (1, None, None, []),
        (2, 0, [], []),
        (2, 3, [0, 1], [
            "00000000-0000-4000-8000-000000000302",
            "00000000-0000-4000-8000-000000000501",
        ]),
    ),
)
def test_replay_wide_damage_projection_resolves_every_present_player_in_source_order(
    feature_factory: sessionmaker[Session],
    schema_version: int,
    source_mask: object,
    source_indices: object,
    expected_sources: list[str],
) -> None:
    target = "00000000-0000-4000-8000-000000000302"
    peer = "00000000-0000-4000-8000-000000000501"
    event = TelemetryEvent(
        event_type="damage_applied",
        schema_version=schema_version,
        payload_json={
            "source_player_mask": source_mask,
            "source_player_indices": source_indices,
            "victim_player_index": 1,
        },
    )
    facts = FeatureExtractionService(feature_factory)._event_facts(
        event,
        {0: target, 1: peer},
        {},
        None,
        [],
    )
    assert facts["source_replay_player_public_ids"] == expected_sources
    assert facts["victim_replay_player_public_id"] == peer


def test_replay_wide_damage_projection_rejects_unknown_schema_contract(
    feature_factory: sessionmaker[Session],
) -> None:
    event = TelemetryEvent(
        event_type="damage_applied",
        schema_version=3,
        payload_json={"source_player_indices": None, "victim_player_index": None},
    )
    with pytest.raises(FeatureExtractionError, match="telemetry damage schema"):
        FeatureExtractionService(feature_factory)._event_facts(event, {}, {}, None, [])


@pytest.mark.parametrize(
    "payload",
    (
        {"object_id": "10", "owner_player_index": 0, "responsible_player_index": 0},
        {"object_id": 10, "owner_player_index": 99, "responsible_player_index": None},
        {"object_id": 10, "owner_player_index": 0, "responsible_player_index": True},
        {"object_id": 10, "owner_player_index": 0, "responsible_player_index": 1},
    ),
)
def test_replay_wide_construction_projection_rejects_malformed_or_ambiguous_owner_identity(
    feature_factory: sessionmaker[Session], payload: object
) -> None:
    event = TelemetryEvent(event_type="construction_completed", schema_version=2, payload_json=payload)
    with pytest.raises(FeatureExtractionError, match="telemetry construction"):
        FeatureExtractionService(feature_factory)._event_facts(
            event,
            {
                0: "00000000-0000-4000-8000-000000000302",
                1: "00000000-0000-4000-8000-000000000501",
            },
            {10: ("Structure", "00000000-0000-4000-8000-000000000302", None)},
            None,
            [],
        )


def test_replay_wide_construction_projection_rejects_object_outside_selected_entity_graph(
    feature_factory: sessionmaker[Session],
) -> None:
    event = TelemetryEvent(
        event_type="construction_completed",
        schema_version=2,
        payload_json={"object_id": 11, "owner_player_index": 0, "responsible_player_index": None},
    )
    with pytest.raises(FeatureExtractionError, match="construction object"):
        FeatureExtractionService(feature_factory)._event_facts(
            event,
            {0: "00000000-0000-4000-8000-000000000302"},
            {10: ("Structure", "00000000-0000-4000-8000-000000000302", None)},
            None,
            [],
        )


def test_replay_wide_construction_projection_preserves_explicit_unknown_owner(
    feature_factory: sessionmaker[Session],
) -> None:
    target = "00000000-0000-4000-8000-000000000302"
    event = TelemetryEvent(
        event_type="construction_completed",
        schema_version=2,
        payload_json={"object_id": 10, "owner_player_index": None, "responsible_player_index": None},
    )
    facts = FeatureExtractionService(feature_factory)._event_facts(
        event,
        {0: target},
        {10: ("Structure", target, None)},
        None,
        [],
    )
    assert facts["replay_player_public_id"] is None
    assert facts["template_name"] == "Structure"


def test_replay_wide_construction_projection_resolves_nullable_responsible_owner(
    feature_factory: sessionmaker[Session],
) -> None:
    target = "00000000-0000-4000-8000-000000000302"
    event = TelemetryEvent(
        event_type="construction_completed",
        schema_version=2,
        payload_json={"object_id": 10, "owner_player_index": 0, "responsible_player_index": None},
    )
    facts = FeatureExtractionService(feature_factory)._event_facts(
        event,
        {0: target},
        {10: ("Structure", target, None)},
        None,
        [],
    )
    assert facts["replay_player_public_id"] == target


def test_replay_wide_context_rejects_unmapped_persisted_entity_owner(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, target, _, _, _ = _seed_replay_wide_observations(
        feature_factory,
        entity_owner_player_index=99,
    )
    extractor = _context_capture_extractor("replay_wide_telemetry")
    with pytest.raises(FeatureExtractionError, match="telemetry entity owner mapping"):
        FeatureExtractionService(feature_factory, extractors=(cast(object, extractor),)).extract(
            _request(replay, target, "context_capture")
        )


def test_replay_wide_context_allows_declared_neutral_engine_entity_owner(
    feature_factory: sessionmaker[Session],
) -> None:
    replay, target, _, _, _ = _seed_replay_wide_observations(
        feature_factory,
        entity_owner_player_index=2,
        engine_player_indices=[0, 1, 2],
    )
    extractor = _context_capture_extractor("replay_wide_telemetry")

    receipts = FeatureExtractionService(
        feature_factory,
        extractors=(cast(object, extractor),),
    ).extract(_request(replay, target, "context_capture"))

    assert len(receipts) == 1 and receipts[0].cache_hit is False


def test_event_facts_projects_only_the_requested_player_from_aggregate_engine_snapshots(
    feature_factory: sessionmaker[Session],
) -> None:
    service = FeatureExtractionService(feature_factory)
    players = {0: "player-a", 1: "player-b"}
    score = SimpleNamespace(
        event_type="scorekeeper_snapshot",
        schema_version=2,
        payload_json={
            "scoring_enabled": True,
            "players": [
                {"player_index": 0, "money_earned": 100, "money_spent": 50, "units_built": 1, "units_lost": 2,
                 "units_destroyed": 3, "buildings_built": 4, "buildings_lost": 5, "buildings_destroyed": 6,
                 "tech_buildings_captured": 7, "faction_buildings_captured": 8},
                {"player_index": 1, "money_earned": 200, "money_spent": 60, "units_built": 9, "units_lost": 10,
                 "units_destroyed": 11, "buildings_built": 12, "buildings_lost": 13, "buildings_destroyed": 14,
                 "tech_buildings_captured": 15, "faction_buildings_captured": 16},
            ],
        },
    )
    cpm = SimpleNamespace(
        event_type="cash_per_minute_snapshot",
        schema_version=2,
        payload_json={
            "players": [
                {"player_index": 0, "has_money": True, "cash_per_minute": 111},
                {"player_index": 1, "has_money": False, "cash_per_minute": None},
            ],
        },
    )

    score_facts = service._event_facts(score, players, {}, SimpleNamespace(public_id="player-b"))  # type: ignore[arg-type]
    cpm_facts = service._event_facts(cpm, players, {}, SimpleNamespace(public_id="player-b"))  # type: ignore[arg-type]

    assert score_facts == {
        "buildings_built": 12,
        "buildings_destroyed": 14,
        "buildings_lost": 13,
        "faction_buildings_captured": 16,
        "money_earned": 200,
        "money_spent": 60,
        "replay_player_public_id": "player-b",
        "scoring_enabled": True,
        "tech_buildings_captured": 15,
        "units_built": 9,
        "units_destroyed": 11,
        "units_lost": 10,
    }
    assert cpm_facts == {
        "cash_per_minute": None,
        "has_money": False,
        "replay_player_public_id": "player-b",
    }
