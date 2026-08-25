"""Create one strict bounded map asset for synthetic telemetry traces."""

import hashlib
import json
import struct
import zlib
from pathlib import Path


def write_test_map_asset(
    directory: Path,
    engine_identity: str,
    map_identity: str,
    *,
    bridges: list[dict[str, object]] | None = None,
    start_positions: list[dict[str, object]] | None = None,
    static_objects: list[dict[str, object]] | None = None,
    grid_width: int = 2,
    grid_height: int = 2,
    grid_origin_x: int = -1,
    grid_origin_y: int = -1,
    grid_cell_size: float = 1_000_000.0,
) -> dict[str, object]:
    """Write deterministic authoritative-format map bytes and return their strict reference."""
    element_count = grid_width * grid_height
    # TheSuperHackers @fix Leex 25/08/2026 Keep synthetic logical map members content-distinct for managed-asset identity tests. (#TBD)
    raw_members = {
        "height.f32.zlib": ("float32", struct.pack(f"<{element_count}f", *([0.0] * element_count))),
        "pathing-amphibious.u8.zlib": (
            "uint8",
            bytes(1 if index % 4 < 2 else 0 for index in range(element_count)),
        ),
        "pathing-ground.u8.zlib": (
            "uint8",
            bytes(1 if index % 4 == 0 else 0 for index in range(element_count)),
        ),
        "terrain.u8.zlib": (
            "uint8",
            bytes((0, 1, 2, 4)[index % 4] for index in range(element_count)),
        ),
        "zones.i32.zlib": (
            "int32",
            struct.pack(f"<{element_count}i", *((index % 16_383) + 1 for index in range(element_count))),
        ),
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
            "element_count": element_count,
            "endianness": "little",
            "grid": "pathing",
            "uncompressed_sha256": hashlib.sha256(raw).hexdigest(),
            "uncompressed_size": len(raw),
        }
    grid = {
        "bounds": {
            "maximum_exclusive": {"x": (grid_origin_x + grid_width) * grid_cell_size, "y": (grid_origin_y + grid_height) * grid_cell_size},
            "minimum_inclusive": {"x": grid_origin_x * grid_cell_size, "y": grid_origin_y * grid_cell_size},
        },
        "cell_size": {"x": grid_cell_size, "y": grid_cell_size},
        "dimension_source": "synthetic contract fixture",
        "height": grid_height,
        "index_origin": {"x": grid_origin_x, "y": grid_origin_y},
        "sample_point": "cell_center",
        "storage_order": "row_major_y_then_x_x_fastest",
        "width": grid_width,
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
                "maximum": {"x": (grid_origin_x + grid_width) * grid_cell_size, "y": (grid_origin_y + grid_height) * grid_cell_size, "z": 10_000.0},
                "maximum_inclusive": True,
                "minimum": {"x": grid_origin_x * grid_cell_size, "y": grid_origin_y * grid_cell_size, "z": -10_000.0},
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
                    "exempt_trusted_visual_debris",
                    "exempt_catalog_railroad_behavior",
                ],
                "policy": "pathfinder_xy_closed_except_explicit_engine_category",
                "policy_source": "ReplayMovementSampler trusted visual-debris KindOf, map-loaded lifecycle KindOf, catalog-bound RailroadBehavior, or catalog-bound current locomotor AIR surface",
            },
            "float_encoding": "IEEE-754-binary32",
            "units": "engine_world_unit",
        },
        "engine_data_identity": engine_identity,
        "features": {
            "bridges": [] if bridges is None else bridges,
            "start_positions": [
                {
                    "bounds_policy": "pathfinder_xy_closed",
                    "category_source": "GameSlot::getStartPos + TerrainLogic::getWaypointByName",
                    "name": "Player_1_Start",
                    "position": {"x": 1.0, "y": 2.0, "z": 3.0},
                    "slot_indices": [0],
                    "waypoint_id": 1,
                }
            ] if start_positions is None else start_positions,
            "static_objects": [] if static_objects is None else static_objects,
            "waypoints": [],
        },
        "grids": {"pathing": grid, "terrain": grid},
        "map_identity": map_identity,
        "members": members,
        "producer": {"name": "zero-hour-replay-map-export", "version": 2, "zlib_version": zlib.ZLIB_VERSION},
        "schema_version": 2,
        "type": "map_asset",
    }
    placeholder = (json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
    content_hash = hashlib.sha256(placeholder).hexdigest()
    manifest["content_sha256"] = content_hash
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
    asset_dir = directory / "map-assets-v2" / content_hash
    asset_dir.mkdir(parents=True, exist_ok=True)
    for name, data in compressed.items():
        (asset_dir / name).write_bytes(data)
    (asset_dir / "manifest.json").write_bytes(manifest_bytes)
    return {
        "type": "map_asset",
        "schema_version": 2,
        "path": f"map-assets-v2/{content_hash}/manifest.json",
        "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "content_sha256": content_hash,
        "engine_data_identity": engine_identity,
        "map_identity": map_identity,
    }
