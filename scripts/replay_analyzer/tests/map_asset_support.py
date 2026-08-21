"""Create one strict bounded map asset for synthetic telemetry traces."""

import hashlib
import json
import struct
import zlib
from pathlib import Path


def write_test_map_asset(directory: Path, engine_identity: str, map_identity: str) -> dict[str, object]:
    """Write deterministic 2x2 authoritative-format bytes and return their strict reference."""
    raw_members = {
        "height.f32.zlib": ("float32", struct.pack("<4f", 0.0, 0.0, 0.0, 0.0)),
        "pathing-amphibious.u8.zlib": ("uint8", bytes((1, 1, 0, 0))),
        "pathing-ground.u8.zlib": ("uint8", bytes((1, 0, 0, 0))),
        "terrain.u8.zlib": ("uint8", bytes((0, 1, 2, 4))),
        "zones.i32.zlib": ("int32", struct.pack("<4i", 1, 2, 3, 4)),
    }
    compressed: dict[str, bytes] = {}
    members: dict[str, object] = {}
    for name, (dtype, raw) in raw_members.items():
        data = zlib.compress(raw, level=9)
        compressed[name] = data
        members[name] = {
            "compressed_sha256": hashlib.sha256(data).hexdigest(),
            "compressed_size": len(data),
            "compression": "zlib",
            "compression_level": 9,
            "dtype": dtype,
            "element_count": 4,
            "endianness": "little",
            "grid": "pathing",
            "uncompressed_sha256": hashlib.sha256(raw).hexdigest(),
            "uncompressed_size": len(raw),
        }
    grid = {
        "bounds": {
            "maximum_exclusive": {"x": 1_000_000.0, "y": 1_000_000.0},
            "minimum_inclusive": {"x": -1_000_000.0, "y": -1_000_000.0},
        },
        "cell_size": {"x": 1_000_000.0, "y": 1_000_000.0},
        "dimension_source": "synthetic contract fixture",
        "height": 2,
        "index_origin": {"x": -1, "y": -1},
        "sample_point": "cell_center",
        "width": 2,
    }
    manifest: dict[str, object] = {
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
                "maximum": {"x": 1_000_000.0, "y": 1_000_000.0, "z": 10_000.0},
                "maximum_inclusive": True,
                "minimum": {"x": -1_000_000.0, "y": -1_000_000.0, "z": -10_000.0},
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
        "engine_data_identity": engine_identity,
        "features": {"bridges": [], "start_positions": [], "static_objects": [], "waypoints": []},
        "grids": {"pathing": grid, "terrain": grid},
        "map_identity": map_identity,
        "members": members,
        "producer": {"name": "zero-hour-replay-map-export", "version": 1, "zlib_version": zlib.ZLIB_VERSION},
        "schema_version": 1,
        "type": "map_asset",
    }
    placeholder = (json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
    content_hash = hashlib.sha256(placeholder).hexdigest()
    manifest["content_sha256"] = content_hash
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
    asset_dir = directory / "map-assets-v1" / content_hash
    asset_dir.mkdir(parents=True, exist_ok=True)
    for name, data in compressed.items():
        (asset_dir / name).write_bytes(data)
    (asset_dir / "manifest.json").write_bytes(manifest_bytes)
    return {
        "type": "map_asset",
        "schema_version": 1,
        "path": f"map-assets-v1/{content_hash}/manifest.json",
        "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "content_sha256": content_hash,
        "engine_data_identity": engine_identity,
        "map_identity": map_identity,
    }
