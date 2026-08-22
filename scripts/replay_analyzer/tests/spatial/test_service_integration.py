"""Public FeatureExtractionService integration for the spatial plugin."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.models import (
    Entity,
    EvidenceItem,
    ManagedAsset,
    Map,
    MapResource,
    ParserRun,
    Replay,
    ReplayPlayer,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.features.context import FeatureContext, cache_key, input_digest
from generals_replay_analyzer.features.evidence import thaw_canonical
from generals_replay_analyzer.features.service import ExtractFeaturesRequest, FeatureExtractionService
from generals_replay_analyzer.spatial.features import SPATIAL_REGISTRY, SpatialFeatureExtractor

PLAYER_NAMES = (
    "army_route.observed_reachable_distance",
    "army_route.observed_route_segment_count",
    "expansion.completed_structure_positions",
    "expansion.forward_completed_structure_count",
    "movement_density.sample_count_heatmap",
    "movement_density.sample_count_heatmap_bootstrap_interval",
    "resource_control.observed_supply_collected_amount",
    "resource_control.observed_supply_collection_share",
)


@pytest.fixture
def spatial_service_factory(tmp_path: Path) -> sessionmaker[Session]:
    database = tmp_path / "external-data-root" / "library.sqlite3"
    database.parent.mkdir()
    upgrade_database(database)
    engine = create_database_engine(database)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


def _settings() -> dict[str, object]:
    return {
        "spatial": {
            "algorithm_schema": "spatial-algorithm-settings-v1",
            "bootstrap_algorithm_version": "pcg64-bootstrap-v1",
            "bootstrap_resamples": 32,
            "engagement_algorithm_version": "engagement-cluster-v1",
            "engagement_gap_frames": 90,
            "engagement_reachable_radius_cells": 12,
            "heatmap_algorithm_version": "sample-count-heatmap-v1",
            "max_sample_gap_frames": 300,
            "route_algorithm_version": "grid-route-v1",
        }
    }


def _seed_service_spatial(factory: sessionmaker[Session]) -> tuple[str, str, str, dict[str, str]]:
    replay_public_id = "00000000-0000-4000-8000-000000007001"
    target = "00000000-0000-4000-8000-000000007002"
    peer = "00000000-0000-4000-8000-000000007003"
    parser_run_id = "00000000-0000-4000-8000-000000007004"
    telemetry_run_id = "00000000-0000-4000-8000-000000007005"
    now = datetime(2026, 8, 22, tzinfo=UTC)
    map_content_sha256 = "d" * 64
    map_manifest_sha256 = "e" * 64
    catalog_sha256 = "f" * 64
    event_evidence: dict[str, str] = {}
    with factory() as session:
        replay = Replay(
            public_id=replay_public_id,
            sha256="a" * 64,
            replay_name="spatial-service-fixture",
            version_string="1.04",
            version_number=1,
            frame_count=120,
            start_time=0,
            end_time=120,
            exe_crc=1,
            ini_crc=2,
            map_crc=3,
            map_name="maps/fixture.map",
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
            parser_version="parser-v1",
            schema_version=1,
            input_sha256=replay.sha256,
            status="running",
            warnings_json=[],
            started_at=now,
        )
        session.add(parser)
        session.flush()
        session.add_all(
            (
                ReplayPlayer(
                    public_id=target,
                    replay_id=replay.id,
                    parser_run_id=parser.id,
                    slot_index=0,
                    slot_kind="human",
                    original_name="Target",
                    normalized_name="target",
                    player_index=0,
                    observed_json={"player_index": 0},
                ),
                ReplayPlayer(
                    public_id=peer,
                    replay_id=replay.id,
                    parser_run_id=parser.id,
                    slot_index=1,
                    slot_kind="human",
                    original_name="Peer",
                    normalized_name="peer",
                    player_index=1,
                    observed_json={"player_index": 1},
                ),
            )
        )
        session.flush()
        parser.status = "succeeded"
        parser.completion_status = "complete"
        parser.result_sha256 = "b" * 64
        parser.completed_at = now
        manifest_asset = ManagedAsset(
            public_id="00000000-0000-4000-8000-000000007006",
            sha256=map_manifest_sha256,
            kind="telemetry_map_asset",
            relative_path="fixtures/map/manifest.json",
            size_bytes=1,
            created_at=now,
        )
        catalog_asset = ManagedAsset(
            public_id="00000000-0000-4000-8000-000000007007",
            sha256=catalog_sha256,
            kind="telemetry_catalog",
            relative_path="fixtures/catalog.json",
            size_bytes=1,
            created_at=now,
        )
        session.add_all((manifest_asset, catalog_asset))
        session.flush()
        projection = {
            "amphibious_passable": [True, True],
            "content_sha256": map_content_sha256,
            "engine_data_identity": "fixture-engine",
            "ground_passable": [True, True],
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
            "zone_ids": [1, 1],
        }
        map_row = Map(
            public_id="00000000-0000-4000-8000-000000007008",
            content_sha256=map_content_sha256,
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
            metadata_json={"validated_spatial_projection": projection},
            created_at=now,
        )
        session.add(map_row)
        session.flush()
        start_source = "GameSlot::getStartPos + TerrainLogic::getWaypointByName"
        session.add_all(
            (
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
                    payload_json={
                        "bounds_policy": "pathfinder_xy_closed",
                        "category_source": start_source,
                        "name": "Start Zero",
                        "position": {"x": 5.0, "y": 5.0, "z": 0.0},
                        "slot_indices": [0],
                        "waypoint_id": 10,
                    },
                ),
                MapResource(
                    map_id=map_row.id,
                    stable_key="start:1",
                    resource_kind="start_position",
                    source_object_id=None,
                    template_name="Start One",
                    owner_player_index=1,
                    amount=None,
                    x=15.0,
                    y=5.0,
                    z=0.0,
                    payload_json={
                        "bounds_policy": "pathfinder_xy_closed",
                        "category_source": start_source,
                        "name": "Start One",
                        "position": {"x": 15.0, "y": 5.0, "z": 0.0},
                        "slot_indices": [1],
                        "waypoint_id": 20,
                    },
                ),
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
                    payload_json={
                        "bounds_policy": "pathfinder_xy_closed",
                        "categories": [
                            {
                                "name": "supply_source",
                                "source": "ThingTemplate::isKindOf(KINDOF_SUPPLY_SOURCE)",
                            }
                        ],
                        "creation_source": "map_loaded",
                        "object_id": 77,
                        "orientation": 0.0,
                        "position": {"x": 15.0, "y": 5.0, "z": 0.0},
                        "snapshot_scope": "post_map_initialization",
                        "template_name": "SupplyDock",
                    },
                ),
            )
        )
        replay.map_id = map_row.id
        telemetry = TelemetryRun(
            run_id=telemetry_run_id,
            replay_id=replay.id,
            schema_version=2,
            engine_build="fixture-engine",
            settings_json={"parser_run_id": parser_run_id},
            status="running",
            runner_status="succeeded",
            diagnostics_json={},
            catalog_asset_id=catalog_asset.id,
            map_id=map_row.id,
            map_asset_id=manifest_asset.id,
            started_at=now,
        )
        session.add(telemetry)
        session.flush()
        session.add_all(
            (
                Entity(
                    public_id="00000000-0000-4000-8000-000000007077",
                    telemetry_run_id=telemetry.id,
                    replay_id=replay.id,
                    object_id=77,
                    template_name="SupplyDock",
                    initial_owner_player_index=None,
                    initial_team_id=None,
                    kind_of_flags_json=["SUPPLY_SOURCE"],
                    creation_sequence=None,
                    creation_frame=None,
                    destruction_sequence=None,
                    destruction_frame=None,
                    observed_json={"source": "map_loaded"},
                ),
                Entity(
                    public_id="00000000-0000-4000-8000-000000007010",
                    telemetry_run_id=telemetry.id,
                    replay_id=replay.id,
                    object_id=10,
                    template_name="TargetUnit",
                    initial_owner_player_index=0,
                    initial_team_id=None,
                    kind_of_flags_json=["MOBILE"],
                    creation_sequence=None,
                    creation_frame=None,
                    destruction_sequence=None,
                    destruction_frame=None,
                    observed_json={"source": "entity_sample"},
                ),
                Entity(
                    public_id="00000000-0000-4000-8000-000000007020",
                    telemetry_run_id=telemetry.id,
                    replay_id=replay.id,
                    object_id=20,
                    template_name="PeerUnit",
                    initial_owner_player_index=1,
                    initial_team_id=None,
                    kind_of_flags_json=["MOBILE"],
                    creation_sequence=None,
                    creation_frame=None,
                    destruction_sequence=None,
                    destruction_frame=None,
                    observed_json={"source": "entity_sample"},
                ),
            )
        )
        manifest = {
            "engine_build": "fixture-engine",
            "game_data_catalog": {
                "engine_data_identity": "fixture-engine",
                "path": f"game-data-catalog-v1-{catalog_sha256}.json",
                "sha256": catalog_sha256,
                "type": "game_data_catalog",
            },
            "map_asset": {
                "content_sha256": map_content_sha256,
                "engine_data_identity": "fixture-engine",
                "map_identity": "maps/fixture.map",
                "path": f"map-assets-v2/{map_content_sha256}/manifest.json",
                "schema_version": 2,
                "sha256": map_manifest_sha256,
                "type": "map_asset",
            },
            "map_identity": "maps/fixture.map",
        }
        sample = {
            "is_disabled": False,
            "is_engine_moving": True,
            "is_mobile": True,
            "is_structure": False,
            "locomotor_surface": "ground",
            "path_goal": None,
            "position_bounds_policy": "pathfinder_xy_closed",
        }
        events = (
            (0, "manifest", manifest),
            (
                1,
                "players_initialized",
                {
                    "slots": [
                        {"player_index": 0, "resolution_status": "resolved", "slot_index": 0},
                        {"player_index": 1, "resolution_status": "resolved", "slot_index": 1},
                    ]
                },
            ),
            (10, "supply_collected", {"amount": 100.0, "player_index": 0, "source_object_id": 77}),
            (11, "supply_collected", {"amount": 200.0, "player_index": 1, "source_object_id": 77}),
            (20, "entity_sample", {**sample, "object_id": 10, "owner_player_index": 0, "position": {"x": 5.0, "y": 5.0, "z": 0.0}}),
            (30, "entity_sample", {**sample, "object_id": 10, "owner_player_index": 0, "position": {"x": 15.0, "y": 5.0, "z": 0.0}}),
            (25, "entity_sample", {**sample, "object_id": 20, "owner_player_index": 1, "position": {"x": 5.0, "y": 5.0, "z": 0.0}}),
        )
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
            event_evidence[f"{event_type}:{frame}"] = public_id
        telemetry.status = "succeeded"
        telemetry.final_frame = 120
        telemetry.command_count = 0
        telemetry.trace_sha256 = "c" * 64
        telemetry.completed_at = now
        session.commit()
    return replay_public_id, target, peer, event_evidence


def test_spatial_extractor_service_receives_replay_wide_peer_evidence_and_persists_player_subset(
    spatial_service_factory: sessionmaker[Session],
) -> None:
    replay, target, peer, evidence_ids = _seed_service_spatial(spatial_service_factory)

    class CapturingSpatialFeatureExtractor(SpatialFeatureExtractor):
        def __init__(self) -> None:
            self.context: FeatureContext | None = None

        def extract(self, context: FeatureContext):
            self.context = context
            return super().extract(context)

    extractor = CapturingSpatialFeatureExtractor()
    receipt = FeatureExtractionService(
        spatial_service_factory,
        extractors=(extractor,),
        registry=SPATIAL_REGISTRY,
    ).extract(ExtractFeaturesRequest(replay, target, ("spatial",), _settings()))[0]

    assert extractor.context is not None
    facts = [cast(dict[str, object], thaw_canonical(item.facts)) for item in extractor.context.observed]
    assert any(item.get("replay_player_public_id") == peer for item in facts)
    assert any(item.get("owner_scope_key") == peer for item in facts)
    assert tuple(value.name for value in receipt.features) == PLAYER_NAMES
    values = {value.name: value for value in receipt.features}
    assert values["resource_control.observed_supply_collected_amount"].raw_value == 100.0
    assert values["resource_control.observed_supply_collection_share"].raw_value == 1 / 3
    assert evidence_ids["supply_collected:11"] in {
        reference.public_id
        for reference in values["resource_control.observed_supply_collection_share"].input_evidence
    }
    initialized = next(item for item in extractor.context.observed if item.event_type == "players_initialized")
    assert initialized.ref.public_id == evidence_ids["players_initialized:1"]
    bootstrap_details = cast(
        dict[str, object],
        thaw_canonical(values["movement_density.sample_count_heatmap_bootstrap_interval"].details),
    )
    assert bootstrap_details["input_evidence_public_ids"] == sorted(
        (evidence_ids["entity_sample:20"], evidence_ids["entity_sample:30"])
    )

    peer_sample = next(
        item
        for item in extractor.context.observed
        if item.event_type == "entity_sample"
        and cast(dict[str, object], thaw_canonical(item.facts)).get("owner_scope_key") == peer
    )
    peer_facts = cast(dict[str, object], thaw_canonical(peer_sample.facts))
    changed_peer_sample = replace(
        peer_sample,
        facts={**peer_facts, "position": {"x": 6.0, "y": 5.0, "z": 0.0}},
    )
    changed_context = replace(
        extractor.context,
        observed=tuple(
            changed_peer_sample if item == peer_sample else item for item in extractor.context.observed
        ),
    )
    assert input_digest(changed_context) != receipt.input_digest
    assert cache_key(
        changed_context,
        extractor.name,
        extractor.version,
        registry_schema=SPATIAL_REGISTRY.schema_version,
    ) != receipt.cache_key
