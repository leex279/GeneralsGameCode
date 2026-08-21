"""Strict contracts for authoritative, content-addressed engine map assets."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import generals_replay_analyzer.telemetry.map_asset as map_asset_module
from generals_replay_analyzer.telemetry.map_asset import (
    MAX_MANIFEST_BYTES,
    MapAssetValidationError,
    load_map_asset,
)
from generals_replay_analyzer.telemetry.reader import (
    iter_validated_trace,
)

RUN_ID = "12345678-1234-4234-8234-123456789abc"
ENGINE_ID = "zero-hour-104-exe-11111111-ini-22222222"
MAP_ID = "Maps\\Test\\Test.map"
MEMBER_NAMES = (
    "height.f32.zlib",
    "pathing-amphibious.u8.zlib",
    "pathing-ground.u8.zlib",
    "terrain.u8.zlib",
    "zones.i32.zlib",
)


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _rebind_manifest(asset_dir: Path, reference: dict[str, str], manifest: dict[str, Any]) -> Path:
    manifest["content_sha256"] = "0" * 64
    content_hash = hashlib.sha256(_canonical_bytes(manifest)).hexdigest()
    manifest["content_sha256"] = content_hash
    manifest_bytes = _canonical_bytes(manifest)
    rebound = asset_dir.parent / content_hash
    asset_dir.rename(rebound)
    manifest_path = rebound / "manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    reference.update(
        content_sha256=content_hash,
        path=f"map-assets-v1/{content_hash}/manifest.json",
        sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )
    return manifest_path


def _compressed_member(raw: bytes, *, dtype: str, width: int = 2, height: int = 2) -> tuple[bytes, dict[str, Any]]:
    compressed = zlib.compress(raw, level=9)
    return compressed, {
        "compression": "zlib",
        "compression_level": 9,
        "compressed_sha256": hashlib.sha256(compressed).hexdigest(),
        "compressed_size": len(compressed),
        "dtype": dtype,
        "element_count": width * height,
        "endianness": "little",
        "grid": "pathing",
        "uncompressed_sha256": hashlib.sha256(raw).hexdigest(),
        "uncompressed_size": len(raw),
    }


def _write_asset(root: Path) -> tuple[Path, dict[str, str]]:
    """Write one hand-derived canonical 2x2 asset and return its strict trace reference."""
    raws = {
        "height.f32.zlib": struct.pack("<4f", 0.0, 1.5, 2.0, 3.25),
        "terrain.u8.zlib": bytes((0, 1, 2, 4)),
        "pathing-ground.u8.zlib": bytes((1, 0, 0, 0)),
        "pathing-amphibious.u8.zlib": bytes((1, 1, 0, 0)),
        "zones.i32.zlib": struct.pack("<4i", 1, 2, 3, 4),
    }
    dtype = {
        "height.f32.zlib": "float32",
        "terrain.u8.zlib": "uint8",
        "pathing-ground.u8.zlib": "uint8",
        "pathing-amphibious.u8.zlib": "uint8",
        "zones.i32.zlib": "int32",
    }
    compressed: dict[str, bytes] = {}
    members: dict[str, dict[str, Any]] = {}
    for name in MEMBER_NAMES:
        compressed[name], members[name] = _compressed_member(raws[name], dtype=dtype[name])

    manifest: dict[str, Any] = {
        "classification": {
            "amphibious_passable_cell_types": [0, 1],
            "cell_types": [
                {"name": "CELL_CLEAR", "value": 0},
                {"name": "CELL_WATER", "value": 1},
                {"name": "CELL_CLIFF", "value": 2},
                {"name": "CELL_RUBBLE", "value": 3},
                {"name": "CELL_OBSTACLE", "value": 4},
                {"name": "CELL_BRIDGE_IMPASSABLE", "value": 5},
                {"name": "CELL_IMPASSABLE", "value": 6},
            ],
            "ground_passable_cell_types": [0],
            "pathing_derivation_source": "Pathfinder::validLocomotorSurfacesForCellType",
            "raw_cell_type_source": "PathfindCell::getType",
            "raw_zone_source": "PathfindCell::getZone",
        },
        "content_sha256": "0" * 64,
        "coordinate_system": {
            "axes": ["engine_world_x", "engine_world_y", "engine_world_z"],
            "bounds": {
                "maximum": {"x": 20.0, "y": 20.0, "z": 20.0},
                "maximum_inclusive": True,
                "minimum": {"x": 0.0, "y": 0.0, "z": 0.0},
                "minimum_inclusive": True,
            },
            "entity_sample_policy": {
                "bounded_layer_statuses": ["stable", "dynamic_bridge_layer", "unknown_engine_value"],
                "bounded_position_policies": ["pathfinder_xy_closed"],
                "exempt_position_policies": [
                    "exempt_kindof_aircraft", "exempt_kindof_bridge",
                    "exempt_kindof_projectile", "exempt_kindof_parachutable",
                    "exempt_locomotor_air_surface",
                    "exempt_map_loaded_unclassified_immobile",
                ],
                "policy": "pathfinder_xy_closed_except_explicit_engine_category",
                "policy_source": "ReplayMovementSampler KindOf, map-loaded lifecycle KindOf, or catalog-bound current locomotor AIR surface",
            },
            "float_encoding": "IEEE-754-binary32",
            "units": "engine_world_unit",
        },
        "engine_data_identity": ENGINE_ID,
        "features": {
            "bridges": [],
            "start_positions": [
                {
                    "bounds_policy": "pathfinder_xy_closed",
                    "category_source": "GameSlot::getStartPos + TerrainLogic::getWaypointByName",
                    "name": "Player_1_Start",
                    "position": {"x": 0.0, "y": 20.0, "z": 1.5},
                    "slot_indices": [0],
                    "waypoint_id": 1,
                }
            ],
            "static_objects": [],
            "waypoints": [],
        },
        "grids": {
            "pathing": {
                "bounds": {
                    "maximum_exclusive": {"x": 20.0, "y": 20.0},
                    "minimum_inclusive": {"x": 0.0, "y": 0.0},
                },
                "cell_size": {"x": 10.0, "y": 10.0},
                "dimension_source": "Pathfinder::replayAnalyzerGetExtent",
                "height": 2,
                "index_origin": {"x": 0, "y": 0},
                "sample_point": "cell_center",
                "storage_order": "row_major_y_then_x_x_fastest",
                "width": 2,
            },
            "terrain": {
                "bounds": {
                    "maximum_exclusive": {"x": 20.0, "y": 20.0},
                    "minimum_inclusive": {"x": 0.0, "y": 0.0},
                },
                "cell_size": {"x": 10.0, "y": 10.0},
                "dimension_source": "Pathfinder::replayAnalyzerGetExtent",
                "height": 2,
                "index_origin": {"x": 0, "y": 0},
                "sample_point": "cell_center",
                "storage_order": "row_major_y_then_x_x_fastest",
                "width": 2,
            },
        },
        "map_identity": MAP_ID,
        "members": members,
        "producer": {"name": "zero-hour-replay-map-export", "version": 1, "zlib_version": zlib.ZLIB_VERSION},
        "schema_version": 1,
        "type": "map_asset",
    }
    placeholder = _canonical_bytes(manifest)
    content_hash = hashlib.sha256(placeholder).hexdigest()
    manifest["content_sha256"] = content_hash
    manifest_bytes = _canonical_bytes(manifest)
    asset_dir = root / "map-assets-v1" / content_hash
    asset_dir.mkdir(parents=True)
    for name, data in compressed.items():
        (asset_dir / name).write_bytes(data)
    (asset_dir / "manifest.json").write_bytes(manifest_bytes)
    reference = {
        "content_sha256": content_hash,
        "engine_data_identity": ENGINE_ID,
        "map_identity": MAP_ID,
        "path": f"map-assets-v1/{content_hash}/manifest.json",
        "schema_version": 1,
        "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "type": "map_asset",
    }
    return asset_dir, reference


def _trace_line(sequence: int, event_type: str, payload: dict[str, Any], frame: int = 0) -> bytes:
    return (
        json.dumps(
            {
                "schema_version": 2,
                "run_id": RUN_ID,
                "sequence": sequence,
                "frame": frame,
                "logic_time_seconds": frame / 30.0,
                "event_type": event_type,
                "payload": payload,
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def test_loader_strictly_decodes_authoritative_grids_and_features(tmp_path: Path) -> None:
    asset_dir, reference = _write_asset(tmp_path)

    asset = load_map_asset(
        asset_dir / "manifest.json",
        expected_reference=reference,
        expected_engine_data_identity=ENGINE_ID,
        expected_map_identity=MAP_ID,
    )

    assert asset.content_sha256 == reference["content_sha256"]
    assert asset.pathing.width == 2
    assert asset.pathing.height == 2
    assert asset.height_values == pytest.approx((0.0, 1.5, 2.0, 3.25))
    assert asset.terrain_cell_types == (0, 1, 2, 4)
    assert asset.ground_passable == (True, False, False, False)
    assert asset.amphibious_passable == (True, True, False, False)
    assert asset.zone_ids == (1, 2, 3, 4)
    assert asset.start_positions[0].position.y == 20.0


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("compressed_bytes", "compressed sha256"),
        ("trailing_stream", "trailing"),
        ("short_grid", "uncompressed size"),
        ("bad_terrain_flag", "cell type"),
        ("bad_pathing_flag", "pathing"),
        ("bad_zone", "zone"),
        ("nonfinite_height", "finite float32"),
        ("extra_file", "unexpected asset member"),
        ("content_directory", "content hash directory"),
    ],
)
def test_loader_rejects_tampered_or_partial_assets(tmp_path: Path, mutation: str, message: str) -> None:
    asset_dir, reference = _write_asset(tmp_path)
    manifest_path = asset_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "compressed_bytes":
        member = asset_dir / "height.f32.zlib"
        member.write_bytes(member.read_bytes()[:-1] + b"x")
    elif mutation == "trailing_stream":
        member = asset_dir / "height.f32.zlib"
        data = member.read_bytes() + zlib.compress(b"trailing")
        member.write_bytes(data)
        manifest["members"][member.name]["compressed_size"] = len(data)
        manifest["members"][member.name]["compressed_sha256"] = hashlib.sha256(data).hexdigest()
    elif mutation in {"short_grid", "bad_terrain_flag", "bad_pathing_flag", "bad_zone", "nonfinite_height"}:
        name, raw = {
            "short_grid": ("height.f32.zlib", struct.pack("<3f", 0.0, 1.0, 2.0)),
            "bad_terrain_flag": ("terrain.u8.zlib", bytes((0, 1, 2, 7))),
            "bad_pathing_flag": ("pathing-ground.u8.zlib", bytes((1, 0, 2, 0))),
            "bad_zone": ("zones.i32.zlib", struct.pack("<4i", 1, 2, 3, 16_384)),
            "nonfinite_height": ("height.f32.zlib", struct.pack("<4f", 0.0, 1.0, float("inf"), 3.0)),
        }[mutation]
        data = zlib.compress(raw, level=9)
        (asset_dir / name).write_bytes(data)
        metadata = manifest["members"][name]
        metadata["compressed_size"] = len(data)
        metadata["compressed_sha256"] = hashlib.sha256(data).hexdigest()
        metadata["uncompressed_size"] = len(raw)
        metadata["uncompressed_sha256"] = hashlib.sha256(raw).hexdigest()
    elif mutation == "extra_file":
        (asset_dir / "extra.bin").write_bytes(b"caller-owned")
    elif mutation == "content_directory":
        wrong = asset_dir.parent / ("f" * 64)
        asset_dir.rename(wrong)
        manifest_path = wrong / "manifest.json"

    if mutation not in {"compressed_bytes", "extra_file", "content_directory"}:
        manifest["content_sha256"] = "0" * 64
        placeholder = (json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
        manifest["content_sha256"] = hashlib.sha256(placeholder).hexdigest()
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
        manifest_path.write_bytes(manifest_bytes)
        reference = dict(reference)
        reference["content_sha256"] = manifest["content_sha256"]
        reference["path"] = f"map-assets-v1/{manifest['content_sha256']}/manifest.json"
        reference["sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
        # Keep the directory binding valid so each case reaches the intended member validation.
        rebound = manifest_path.parents[1] / manifest["content_sha256"]
        manifest_path.parent.rename(rebound)
        manifest_path = rebound / "manifest.json"

    with pytest.raises(MapAssetValidationError, match=message):
        load_map_asset(manifest_path, expected_reference=reference)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("small_declared_zip_bomb", "overlong|uncompressed size"),
        ("concatenated_streams", "trailing"),
        ("truncated_stream", "truncated|invalid zlib"),
    ],
)
def test_loader_streams_with_a_declared_output_budget_and_one_complete_zlib_stream(
    tmp_path: Path, mutation: str, message: str
) -> None:
    asset_dir, reference = _write_asset(tmp_path)
    manifest_path = asset_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    member = asset_dir / "height.f32.zlib"
    if mutation == "small_declared_zip_bomb":
        compressed = zlib.compress(b"\0" * (2 * 1024 * 1024), level=9)
    elif mutation == "concatenated_streams":
        compressed = member.read_bytes() + zlib.compress(b"second-stream", level=9)
    else:
        compressed = member.read_bytes()[:-1]
    member.write_bytes(compressed)
    metadata = manifest["members"][member.name]
    metadata["compressed_size"] = len(compressed)
    metadata["compressed_sha256"] = hashlib.sha256(compressed).hexdigest()
    manifest_path = _rebind_manifest(asset_dir, reference, manifest)

    with pytest.raises(MapAssetValidationError, match=message):
        load_map_asset(manifest_path, expected_reference=reference)


def test_loader_preflights_manifest_and_member_sizes_without_path_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_dir, reference = _write_asset(tmp_path)

    def forbidden_read_bytes(_path: Path) -> bytes:
        raise AssertionError("unbounded Path.read_bytes must not be used for hostile assets")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    asset = load_map_asset(asset_dir / "manifest.json", expected_reference=reference)
    assert asset.pathing.width == 2


def test_loader_rejects_oversized_sparse_manifest_and_member_before_read(tmp_path: Path) -> None:
    asset_dir, reference = _write_asset(tmp_path / "manifest-case")
    manifest_path = asset_dir / "manifest.json"
    with manifest_path.open("r+b") as output:
        output.truncate(MAX_MANIFEST_BYTES + 1)
    with pytest.raises(MapAssetValidationError, match="manifest.*size"):
        load_map_asset(manifest_path, expected_reference=reference)

    asset_dir, reference = _write_asset(tmp_path / "member-case")
    member = asset_dir / "terrain.u8.zlib"
    with member.open("r+b") as output:
        output.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(MapAssetValidationError, match="size.*closed bound|compressed size"):
        load_map_asset(asset_dir / "manifest.json", expected_reference=reference)


@pytest.mark.parametrize(
    "mutation",
    ["storage_order", "origin", "world_bounds", "different_terrain_grid"],
)
def test_loader_rejects_rehashed_grid_descriptor_tampering(tmp_path: Path, mutation: str) -> None:
    asset_dir, reference = _write_asset(tmp_path)
    manifest = json.loads((asset_dir / "manifest.json").read_text(encoding="utf-8"))
    if mutation == "storage_order":
        manifest["grids"]["pathing"]["storage_order"] = "x_major"
    elif mutation == "origin":
        manifest["grids"]["pathing"]["index_origin"]["x"] = 1
    elif mutation == "world_bounds":
        manifest["coordinate_system"]["bounds"]["maximum"]["x"] = 19.0
    else:
        manifest["grids"]["terrain"]["dimension_source"] = "different grid"
    manifest_path = _rebind_manifest(asset_dir, reference, manifest)

    with pytest.raises(MapAssetValidationError, match="schema|grid|origin|world.*bounds|descriptor"):
        load_map_asset(manifest_path, expected_reference=reference)


def test_loader_rejects_oob_features_and_never_clamps(tmp_path: Path) -> None:
    asset_dir, reference = _write_asset(tmp_path)
    manifest_path = asset_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["features"]["start_positions"][0]["position"]["x"] = 20.000001
    manifest["content_sha256"] = "0" * 64
    placeholder = (json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
    manifest["content_sha256"] = hashlib.sha256(placeholder).hexdigest()
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
    rebound = asset_dir.parent / manifest["content_sha256"]
    asset_dir.rename(rebound)
    manifest_path = rebound / "manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    reference.update(
        content_sha256=manifest["content_sha256"],
        path=f"map-assets-v1/{manifest['content_sha256']}/manifest.json",
        sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )

    with pytest.raises(MapAssetValidationError, match="outside pathfinder bounds"):
        load_map_asset(manifest_path, expected_reference=reference)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_start_slot",
        "dangling_waypoint_link",
        "mismatched_waypoint_link_name",
        "duplicate_bridge_index",
        "duplicate_static_object_id",
        "forged_category_source",
    ],
)
def test_loader_rejects_rehashed_feature_identity_and_provenance_tampering(
    tmp_path: Path, mutation: str
) -> None:
    asset_dir, reference = _write_asset(tmp_path)
    manifest = json.loads((asset_dir / "manifest.json").read_text(encoding="utf-8"))
    start = manifest["features"]["start_positions"][0]
    waypoint = {
        "bidirectional": False,
        "bounds_policy": "pathfinder_xy_closed",
        "labels": [],
        "link_names": [],
        "link_waypoint_ids": [],
        "name": "Waypoint_A",
        "position": {"x": 10.0, "y": 10.0, "z": 1.0},
        "waypoint_id": 8,
    }
    bridge = {
        "bounds_policy": "pathfinder_xy_closed",
        "bridge_index": 3,
        "bridge_width": 4.0,
        "category_source": "TerrainLogic::getFirstBridge",
        "corners": [
            {"x": 1.0, "y": 1.0, "z": 0.0},
            {"x": 2.0, "y": 1.0, "z": 0.0},
            {"x": 1.0, "y": 2.0, "z": 0.0},
            {"x": 2.0, "y": 2.0, "z": 0.0},
        ],
        "from": {"x": 1.0, "y": 1.5, "z": 0.0},
        "layer_id": 2,
        "object_id": None,
        "template_name": None,
        "to": {"x": 2.0, "y": 1.5, "z": 0.0},
    }
    static_object = {
        "bounds_policy": "pathfinder_xy_closed",
        "categories": [
            {"name": "supply_source", "source": "ThingTemplate::isKindOf(KINDOF_SUPPLY_SOURCE)"}
        ],
        "creation_source": "map_loaded",
        "object_id": 44,
        "orientation": 0.0,
        "position": {"x": 5.0, "y": 5.0, "z": 0.0},
        "snapshot_scope": "post_map_initialization",
        "template_name": "SupplyPile",
    }
    if mutation == "duplicate_start_slot":
        manifest["features"]["start_positions"].append({**start, "name": "Player_2_Start", "waypoint_id": 2})
    elif mutation == "dangling_waypoint_link":
        manifest["features"]["waypoints"] = [
            {**waypoint, "link_names": ["Missing"], "link_waypoint_ids": [999]}
        ]
    elif mutation == "mismatched_waypoint_link_name":
        linked = {**waypoint, "name": "Duplicate", "waypoint_id": 9}
        manifest["features"]["waypoints"] = [
            {**waypoint, "name": "Duplicate", "link_names": ["Forged"], "link_waypoint_ids": [9]},
            linked,
        ]
    elif mutation == "duplicate_bridge_index":
        manifest["features"]["bridges"] = [bridge, {**bridge, "layer_id": 3}]
    elif mutation == "duplicate_static_object_id":
        manifest["features"]["static_objects"] = [static_object, static_object]
    else:
        forged = json.loads(json.dumps(static_object))
        forged["categories"][0]["source"] = "template name contains Supply"
        manifest["features"]["static_objects"] = [forged]
    manifest_path = _rebind_manifest(asset_dir, reference, manifest)

    diagnostic = (
        "map feature static object IDs must be uniquely ordered"
        if mutation == "duplicate_static_object_id"
        else "feature|unique|duplicate|link|category|source"
    )
    with pytest.raises(MapAssetValidationError, match=diagnostic):
        load_map_asset(manifest_path, expected_reference=reference)


def test_loader_preserves_duplicate_waypoint_names_and_binds_links_by_unique_id(tmp_path: Path) -> None:
    """Retail maps may repeat display names; only IDs are authoritative for waypoint links."""
    asset_dir, reference = _write_asset(tmp_path)
    manifest = json.loads((asset_dir / "manifest.json").read_text(encoding="utf-8"))
    first = {
        "bidirectional": False,
        "bounds_policy": "pathfinder_xy_closed",
        "labels": [],
        "link_names": ["Waypoint 128"],
        "link_waypoint_ids": [31],
        "name": "Waypoint 128",
        "position": {"x": 10.0, "y": 10.0, "z": 1.0},
        "waypoint_id": 8,
    }
    second = {
        **first,
        "link_names": [],
        "link_waypoint_ids": [],
        "position": {"x": 11.0, "y": 10.0, "z": 1.0},
        "waypoint_id": 31,
    }
    manifest["features"]["waypoints"] = [first, second]
    manifest_path = _rebind_manifest(asset_dir, reference, manifest)

    asset = load_map_asset(manifest_path, expected_reference=reference)

    assert [waypoint.name for waypoint in asset.waypoints] == ["Waypoint 128", "Waypoint 128"]
    assert asset.waypoints[0].link_waypoint_ids == [31]


def test_entity_bounds_accept_edges_reject_oob_and_require_source_grounded_exemption(tmp_path: Path) -> None:
    asset_dir, reference = _write_asset(tmp_path)
    asset = load_map_asset(asset_dir / "manifest.json", expected_reference=reference)
    payload = SimpleNamespace(
        object_id=1,
        template_name="GroundUnit",
        layer_name="LAYER_GROUND",
        layer_name_status="stable",
        position=SimpleNamespace(x=20.0, y=0.0),
        path_goal=SimpleNamespace(x=0.0, y=20.0),
        position_bounds_policy="pathfinder_xy_closed",
        current_locomotor_template_name=None,
        locomotor_set_id=0,
        locomotor_set_name="LOCOMOTORSET_NORMAL",
    )
    asset.require_entity_position(payload, frozenset(), frozenset())

    payload.position.x = 20.000001
    with pytest.raises(MapAssetValidationError, match="outside pathfinder bounds"):
        asset.require_entity_position(payload, frozenset(), frozenset())

    payload.position_bounds_policy = "exempt_kindof_aircraft"
    with pytest.raises(MapAssetValidationError, match="contradicts game-data KindOf"):
        asset.require_entity_position(payload, frozenset(), frozenset())
    asset.require_entity_position(payload, frozenset({"AIRCRAFT"}), frozenset())

    payload.position_bounds_policy = "exempt_locomotor_air_surface"
    payload.current_locomotor_template_name = "BirdLocomotor"
    with pytest.raises(MapAssetValidationError, match="catalog-bound AIR locomotor"):
        asset.require_entity_position(payload, frozenset(), frozenset())
    asset.require_entity_position(payload, frozenset(), frozenset({"BirdLocomotor"}))

    payload.position_bounds_policy = "pathfinder_xy_closed"
    payload.layer_name_status = "unknown_engine_value"
    with pytest.raises(MapAssetValidationError, match="outside pathfinder bounds"):
        asset.require_entity_position(payload, frozenset(), frozenset())


@pytest.mark.parametrize(
    "forged_policy",
    ["exempt_module_wander_ai", "exempt_physics_without_ai_pathing", "unknown_policy"],
)
def test_entity_bounds_rejects_unprovable_or_unknown_exemptions(tmp_path: Path, forged_policy: str) -> None:
    asset_dir, reference = _write_asset(tmp_path)
    asset = load_map_asset(asset_dir / "manifest.json", expected_reference=reference)
    payload = SimpleNamespace(
        object_id=1,
        template_name="GroundUnit",
        layer_name=None,
        layer_name_status="unknown_engine_value",
        position=SimpleNamespace(x=20.000001, y=0.0),
        path_goal=None,
        position_bounds_policy=forged_policy,
        current_locomotor_template_name=None,
        locomotor_set_id=None,
        locomotor_set_name=None,
    )
    with pytest.raises(MapAssetValidationError, match="unknown position bounds policy"):
        asset.require_entity_position(payload, frozenset(), frozenset())


def test_loader_rejects_hardlinked_or_symlinked_members(tmp_path: Path) -> None:
    asset_dir, reference = _write_asset(tmp_path)
    member = asset_dir / "terrain.u8.zlib"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(member.read_bytes())
    member.unlink()
    os.link(outside, member)
    with pytest.raises(MapAssetValidationError, match="hardlink"):
        load_map_asset(asset_dir / "manifest.json", expected_reference=reference)

    member.unlink()
    try:
        member.symlink_to(outside)
    except OSError:
        pytest.skip("host does not permit unprivileged symlink creation")
    with pytest.raises(MapAssetValidationError, match="reparse|symlink"):
        load_map_asset(asset_dir / "manifest.json", expected_reference=reference)


def test_loader_rejects_member_replaced_between_lstat_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_dir, reference = _write_asset(tmp_path)
    target = asset_dir / "terrain.u8.zlib"
    replacement = tmp_path / "replacement.zlib"
    replacement.write_bytes(target.read_bytes())
    original_open = Path.open
    raced = False

    def racing_open(path: Path, *args: object, **kwargs: object) -> object:
        nonlocal raced
        if path == target and not raced:
            raced = True
            os.replace(replacement, target)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)

    with pytest.raises(MapAssetValidationError, match="opened identity differs from preflight"):
        load_map_asset(asset_dir / "manifest.json", expected_reference=reference)


@pytest.mark.parametrize("component", ["cache", "asset"])
def test_loader_rejects_directory_replaced_between_validation_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    asset_dir, reference = _write_asset(tmp_path)
    manifest_path = asset_dir / "manifest.json"
    cache_dir = asset_dir.parent
    original_open = Path.open
    raced = False

    def racing_open(path: Path, *args: object, **kwargs: object) -> object:
        nonlocal raced
        if path == manifest_path and not raced:
            raced = True
            if component == "cache":
                saved = tmp_path / "saved-cache"
                cache_dir.rename(saved)
                cache_dir.mkdir()
                (saved / asset_dir.name).rename(cache_dir / asset_dir.name)
            else:
                saved = tmp_path / "saved-asset"
                asset_dir.rename(saved)
                asset_dir.mkdir()
                for member in saved.iterdir():
                    member.rename(asset_dir / member.name)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)

    with pytest.raises(MapAssetValidationError, match=f"{component} directory identity changed"):
        load_map_asset(manifest_path, expected_reference=reference)


@pytest.mark.parametrize("opened_kind", ["nonregular", "reparse"])
def test_loader_rejects_unsafe_final_opened_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_kind: str
) -> None:
    asset_dir, reference = _write_asset(tmp_path)
    original_fstat = map_asset_module.os.fstat
    injected = False

    def unsafe_fstat(descriptor: int) -> object:
        nonlocal injected
        info = original_fstat(descriptor)
        if injected:
            return info
        injected = True
        mode = stat.S_IFDIR | stat.S_IMODE(info.st_mode) if opened_kind == "nonregular" else info.st_mode
        attributes = 0x400 if opened_kind == "reparse" else getattr(info, "st_file_attributes", 0)
        return SimpleNamespace(
            st_mode=mode,
            st_nlink=info.st_nlink,
            st_size=info.st_size,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_file_attributes=attributes,
        )

    monkeypatch.setattr(map_asset_module.os, "fstat", unsafe_fstat)

    with pytest.raises(MapAssetValidationError, match="opened handle is not a safe regular file"):
        load_map_asset(asset_dir / "manifest.json", expected_reference=reference)


def test_loader_fails_closed_when_opened_identity_fields_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_dir, reference = _write_asset(tmp_path)
    original_fstat = map_asset_module.os.fstat
    injected = False

    def incomplete_fstat(descriptor: int) -> object:
        nonlocal injected
        info = original_fstat(descriptor)
        if injected:
            return info
        injected = True
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_nlink=info.st_nlink,
            st_size=info.st_size,
            st_dev=info.st_dev,
            st_file_attributes=getattr(info, "st_file_attributes", 0),
        )

    monkeypatch.setattr(map_asset_module.os, "fstat", incomplete_fstat)

    with pytest.raises(MapAssetValidationError, match="filesystem identity is unavailable"):
        load_map_asset(asset_dir / "manifest.json", expected_reference=reference)


def test_loader_rejects_reparse_parent_escape_from_trusted_trace_directory(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    outside = tmp_path / "outside"
    trusted.mkdir()
    asset_dir, reference = _write_asset(outside)
    linked_root = trusted / "map-assets-v1"
    try:
        linked_root.symlink_to(asset_dir.parent, target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit unprivileged directory symlink creation")
    escaped_manifest = linked_root / asset_dir.name / "manifest.json"

    with pytest.raises(MapAssetValidationError, match="reparse|symlink|escapes"):
        load_map_asset(
            escaped_manifest,
            expected_reference=reference,
            trusted_trace_directory=trusted,
        )


def test_loader_rejects_contained_but_noncanonical_cache_path_alias(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    asset_dir, reference = _write_asset(trusted)
    aliased = trusted / "nested" / "map-assets-v1" / asset_dir.name
    shutil.copytree(asset_dir, aliased)

    with pytest.raises(MapAssetValidationError, match="canonical map asset cache path"):
        load_map_asset(
            aliased / "manifest.json",
            expected_reference=reference,
            trusted_trace_directory=trusted,
        )


def test_map_schema_is_packaged_verbatim(repository_root: Path) -> None:
    canonical = repository_root / "scripts/replay_analyzer/contracts/map-asset-v1.schema.json"
    pyproject = (repository_root / "scripts/replay_analyzer/pyproject.toml").read_text(encoding="utf-8")
    assert canonical.is_file()
    assert '"contracts/map-asset-v1.schema.json" = "generals_replay_analyzer/data/map-asset-v1.schema.json"' in pyproject


def test_map_export_sources_remain_modern_zero_hour_read_only(repository_root: Path) -> None:
    cmake = (repository_root / "GeneralsMD/Code/GameEngine/CMakeLists.txt").read_text(encoding="utf-8")
    header = (repository_root / "Core/GameEngine/Include/GameLogic/AIPathfind.h").read_text(encoding="utf-8")
    exporter = repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayMapExport.cpp"
    assert "ReplayMapExport.cpp" in cmake.split("if(NOT IS_VS6_BUILD)", 1)[1]
    assert "replayAnalyzerGetExtent" in header
    assert exporter.is_file()
    source = exporter.read_text(encoding="utf-8")
    assert "forceMapRecalculation" not in source
    assert "validMovementPosition(" not in source
    assert "rand(" not in source
    assert "KINDOF_SUPPLY_SOURCE" in source
    assert "SupplyWarehouseDockUpdate" in source
    assert "AutoDepositUpdate" in source
    assert "strstr(" not in source
    assert 'find("Supply' not in source
    assert 'find("Oil' not in source
    assert "bridge_index" in source
    assert "info.bridgeIndex" in source
    assert "row_major_y_then_x_x_fastest" in source
    sampler = (repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayMovementSampler.cpp").read_text(
        encoding="utf-8"
    )
    assert "LOCOMOTORSURFACE_AIR" in sampler
    assert "exempt_physics_without_ai_pathing" not in sampler
    assert "exempt_module_wander_ai" not in sampler
    assert "current_locomotor_template_name" in sampler
    assert "positionBoundsPolicy(object)" in sampler
    assert "!ReplayMapExport::isClassifiedStaticObject(object)" in sampler
    assert "ReplayMapExport::isClassifiedStaticObject" in source
    assert "getTemplate()->getName()" not in sampler.split("const char *positionBoundsPolicy", 1)[1].split(
        "Bool emitSample", 1
    )[0]
    telemetry = (repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp").read_text(
        encoding="utf-8"
    )
    assert telemetry.count("ReplayMapExport::reset();") >= 4


@pytest.mark.engine
def test_pinned_replay_exports_strict_deterministic_map_asset(
    tmp_path: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """A real initialized map must export once and reuse validated bytes without touching them."""
    replay_hash = hashlib.sha256(pinned_replay.read_bytes()).hexdigest()
    traces: list[Path] = []
    references: list[Any] = []
    snapshots: list[dict[str, tuple[bytes, int]]] = []
    for index in range(2):
        trace = tmp_path / f"natural-{index}.ndjson"
        completed = subprocess.run(
            [
                str(zero_hour_runtime_executable), "-headless", "-noaudio", "-replay", str(pinned_replay),
                "-telemetry", str(trace), "-telemetry-run-id", str(UUID(int=index + 1)),
                "-telemetry-movement-frames", "15",
            ],
            cwd=zero_hour_runtime_executable.parent,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert completed.returncode != 0  # pinned fixture's established CRC mismatch
        records = tuple(iter_validated_trace(trace))
        reference = records[0].payload.map_asset
        assert reference is not None
        assert records[-1].payload.map_assets == [reference]
        load_map_asset(
            trace.parent / reference.path,
            expected_reference=reference,
            expected_engine_data_identity=records[0].payload.engine_build,
            expected_map_identity=records[0].payload.map_identity,
        )
        assert any(record.event_type == "entity_sample" for record in records)
        asset_dir = (trace.parent / reference.path).parent
        snapshots.append({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in asset_dir.iterdir()})
        traces.append(trace)
        references.append(reference)

    assert references[0] == references[1]
    assert snapshots[0] == snapshots[1]
    assert hashlib.sha256(pinned_replay.read_bytes()).hexdigest() == replay_hash


@pytest.mark.engine
def test_telemetry_disabled_replay_creates_no_map_asset(
    tmp_path: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    existing_asset_cache = zero_hour_runtime_executable.parent / "map-assets-v1"
    before = (
        {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in existing_asset_cache.rglob("*") if path.is_file()}
        if existing_asset_cache.exists()
        else None
    )
    completed = subprocess.run(
        [str(zero_hour_runtime_executable), "-headless", "-noaudio", "-replay", str(pinned_replay)],
        cwd=zero_hour_runtime_executable.parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode != 0
    assert not (tmp_path / "map-assets-v1").exists()
    after = (
        {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in existing_asset_cache.rglob("*") if path.is_file()}
        if existing_asset_cache.exists()
        else None
    )
    assert after == before


@pytest.mark.engine
@pytest.mark.parametrize("failure", ["nonfinite", "before_publish"])
def test_map_export_failure_discards_only_owned_trace_and_temporaries(
    tmp_path: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
    failure: str,
) -> None:
    replay_hash = hashlib.sha256(pinned_replay.read_bytes()).hexdigest()
    baseline = subprocess.run(
        [str(zero_hour_runtime_executable), "-headless", "-noaudio", "-replay", str(pinned_replay)],
        cwd=zero_hour_runtime_executable.parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    parent = tmp_path / failure
    parent.mkdir()
    trace = parent / "failed.ndjson"
    environment = os.environ.copy()
    environment["GENERALS_REPLAY_MAP_EXPORT_TEST_FAIL"] = failure
    completed = subprocess.run(
        [
            str(zero_hour_runtime_executable), "-headless", "-noaudio", "-replay", str(pinned_replay),
            "-telemetry", str(trace), "-telemetry-run-id", RUN_ID,
        ],
        cwd=zero_hour_runtime_executable.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == baseline.returncode
    assert not trace.exists()
    assert list(parent.iterdir()) == []
    assert hashlib.sha256(pinned_replay.read_bytes()).hexdigest() == replay_hash


@pytest.mark.engine
@pytest.mark.parametrize("mutation", ["corrupt", "partial"])
def test_corrupt_or_partial_map_cache_fails_closed_without_rewrite(
    tmp_path: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
    mutation: str,
) -> None:
    first_trace = tmp_path / "first.ndjson"
    first = subprocess.run(
        [
            str(zero_hour_runtime_executable), "-headless", "-noaudio", "-replay", str(pinned_replay),
            "-telemetry", str(first_trace), "-telemetry-run-id", RUN_ID,
        ],
        cwd=zero_hour_runtime_executable.parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert first.returncode != 0
    reference = next(iter(iter_validated_trace(first_trace))).payload.map_asset
    assert reference is not None
    asset_dir = (tmp_path / reference.path).parent
    member = asset_dir / "terrain.u8.zlib"
    if mutation == "corrupt":
        changed = member.read_bytes() + b"caller-corruption"
        member.write_bytes(changed)
    else:
        member.unlink()
        changed = None

    second_trace = tmp_path / "second.ndjson"
    second = subprocess.run(
        [
            str(zero_hour_runtime_executable), "-headless", "-noaudio", "-replay", str(pinned_replay),
            "-telemetry", str(second_trace), "-telemetry-run-id", "22345678-1234-4234-8234-123456789abc",
        ],
        cwd=zero_hour_runtime_executable.parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert second.returncode == first.returncode
    assert not second_trace.exists()
    assert member.exists() is (mutation == "corrupt")
    if changed is not None:
        assert member.read_bytes() == changed
    assert not any(".tmp." in path.name for path in (tmp_path / "map-assets-v1").iterdir())


@pytest.mark.engine
def test_writer_failure_discards_trace_but_preserves_valid_published_map_cache(
    tmp_path: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    trace = tmp_path / "writer-failed.ndjson"
    environment = os.environ.copy()
    environment["GENERALS_REPLAY_TELEMETRY_TEST_FAIL_AFTER_COMPLETE_WRITE"] = "1"
    completed = subprocess.run(
        [
            str(zero_hour_runtime_executable), "-headless", "-noaudio", "-replay", str(pinned_replay),
            "-telemetry", str(trace), "-telemetry-run-id", RUN_ID,
        ],
        cwd=zero_hour_runtime_executable.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode != 0
    assert not trace.exists()
    asset_directories = list((tmp_path / "map-assets-v1").iterdir())
    assert len(asset_directories) == 1
    load_map_asset(asset_directories[0] / "manifest.json")
    assert not any(".tmp." in path.name for path in asset_directories)
