"""Strict contracts for authoritative, content-addressed engine map assets."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from generals_replay_analyzer.telemetry.map_asset import (
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
                "bounded_layer_statuses": ["stable", "dynamic_bridge_layer"],
                "bounded_position_policies": ["pathfinder_xy_closed"],
                "exempt_position_policies": [
                    "exempt_kindof_aircraft", "exempt_kindof_bridge",
                    "exempt_kindof_projectile", "exempt_locomotor_air_surface",
                    "exempt_module_wander_ai",
                    "exempt_physics_without_ai_pathing",
                ],
                "policy": "pathfinder_xy_closed_except_explicit_engine_category",
                "policy_source": (
                    "ReplayMovementSampler KindOf, current locomotor AIR surface, "
                    "WanderAIUpdate, or physics without AI pathing"
                ),
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
                "width": 2,
            },
            "terrain": {
                "bounds": {
                    "maximum_exclusive": {"x": 20.0, "y": 20.0},
                    "minimum_inclusive": {"x": 0.0, "y": 0.0},
                },
                "cell_size": {"x": 10.0, "y": 10.0},
                "dimension_source": "initialized TerrainLogic sampled on authoritative path cells",
                "height": 2,
                "index_origin": {"x": 0, "y": 0},
                "sample_point": "cell_center",
                "width": 2,
            },
        },
        "map_identity": MAP_ID,
        "members": members,
        "producer": {"name": "zero-hour-replay-map-export", "version": 1, "zlib_version": zlib.ZLIB_VERSION},
        "schema_version": 1,
        "type": "map_asset",
    }
    placeholder = (json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
    content_hash = hashlib.sha256(placeholder).hexdigest()
    manifest["content_sha256"] = content_hash
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
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
    )
    asset.require_entity_position(payload, frozenset())

    payload.position.x = 20.000001
    with pytest.raises(MapAssetValidationError, match="outside pathfinder bounds"):
        asset.require_entity_position(payload, frozenset())

    payload.position_bounds_policy = "exempt_kindof_aircraft"
    with pytest.raises(MapAssetValidationError, match="contradicts game-data KindOf"):
        asset.require_entity_position(payload, frozenset())
    asset.require_entity_position(payload, frozenset({"AIRCRAFT"}))


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
    sampler = (repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayMovementSampler.cpp").read_text(
        encoding="utf-8"
    )
    assert "LOCOMOTORSURFACE_AIR" in sampler
    assert "exempt_physics_without_ai_pathing" in sampler
    assert "positionBoundsPolicy(object)" in sampler
    assert "getTemplate()->getName()" not in sampler.split("const char *positionBoundsPolicy", 1)[1].split(
        "Bool emitSample", 1
    )[0]


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
        asset = load_map_asset(
            trace.parent / reference.path,
            expected_reference=reference,
            expected_engine_data_identity=records[0].payload.engine_build,
            expected_map_identity=records[0].payload.map_identity,
        )
        for record in records:
            if record.event_type != "entity_sample":
                continue
            asset.require_entity_position(record.payload, frozenset())
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
