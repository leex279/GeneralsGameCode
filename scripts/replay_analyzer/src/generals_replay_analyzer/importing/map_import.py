"""Deterministic projection of directly observed telemetry map facts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Map, MapRegion, MapResource
from ..telemetry.map_asset import MapAsset, Position3


@dataclass(frozen=True)
class MapResourceSpec:
    stable_key: str
    resource_kind: str
    source_object_id: int | None
    template_name: str | None
    owner_player_index: int | None
    x: float | None
    y: float | None
    z: float | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class MapRegionSpec:
    stable_key: str
    region_kind: str
    label: str | None
    geometry: dict[str, Any]
    relationships: dict[str, Any]
    payload: dict[str, Any]


@dataclass(frozen=True)
class NormalizedMap:
    asset: MapAsset
    metadata: dict[str, Any]
    resources: tuple[MapResourceSpec, ...]
    regions: tuple[MapRegionSpec, ...]


def _position(position: Position3) -> dict[str, float]:
    return {"x": position.x, "y": position.y, "z": position.z}


def _unique(specs: tuple[MapResourceSpec, ...] | tuple[MapRegionSpec, ...]) -> None:
    keys = [spec.stable_key for spec in specs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate semantic map stable key")


# TheSuperHackers @feature Leex 22/08/2026 Persist only explicit typed map features under collision-free semantic keys. (#TBD)
def normalize_map_asset(asset: MapAsset) -> NormalizedMap:
    """Build a path-free immutable map projection outside the database transaction."""
    if asset.pathing.cell_size.x != asset.pathing.cell_size.y or asset.terrain.cell_size.x != asset.terrain.cell_size.y:
        raise ValueError("map grid has anisotropic cell size unsupported by the accepted schema")
    resources: list[MapResourceSpec] = []
    for start in asset.start_positions:
        payload = start.model_dump(mode="json")
        payload["slot_indices"] = sorted(start.slot_indices)
        for slot_index in sorted(start.slot_indices):
            resources.append(
                MapResourceSpec(
                    f"start:{slot_index}",
                    "start_position",
                    None,
                    start.name,
                    slot_index,
                    start.position.x,
                    start.position.y,
                    start.position.z,
                    payload,
                )
            )
    for static_object in asset.static_objects:
        payload = static_object.model_dump(mode="json")
        payload["categories"] = sorted(payload["categories"], key=lambda item: (item["name"], item["source"]))
        resources.append(
            MapResourceSpec(
                f"static:{static_object.object_id}",
                "static_object",
                static_object.object_id,
                static_object.template_name,
                None,
                static_object.position.x,
                static_object.position.y,
                static_object.position.z,
                payload,
            )
        )

    regions: list[MapRegionSpec] = []
    for waypoint in asset.waypoints:
        payload = waypoint.model_dump(mode="json")
        link_ids = sorted(waypoint.link_waypoint_ids or [])
        link_names = sorted(waypoint.link_names)
        payload["link_waypoint_ids"] = link_ids
        payload["link_names"] = link_names
        regions.append(
            MapRegionSpec(
                f"waypoint:{waypoint.waypoint_id}",
                "waypoint",
                waypoint.name,
                {"point": _position(waypoint.position)},
                {"bidirectional": waypoint.bidirectional, "link_waypoint_ids": link_ids, "link_names": link_names},
                payload,
            )
        )
    for bridge in asset.bridges:
        payload = bridge.model_dump(mode="json", by_alias=True)
        regions.append(
            MapRegionSpec(
                f"bridge:{bridge.bridge_index}",
                "bridge",
                bridge.template_name,
                {
                    "from": _position(bridge.from_),
                    "to": _position(bridge.to),
                    "corners": [_position(corner) for corner in bridge.corners],
                },
                {"object_id": bridge.object_id, "template_name": bridge.template_name, "layer_id": bridge.layer_id},
                payload,
            )
        )
    resource_specs = tuple(sorted(resources, key=lambda spec: spec.stable_key))
    region_specs = tuple(sorted(regions, key=lambda spec: spec.stable_key))
    _unique(resource_specs)
    _unique(region_specs)
    metadata = {
        "declared_position_policies": sorted(asset.declared_position_policies),
        "pathing": asset.pathing.model_dump(mode="json"),
        "terrain": asset.terrain.model_dump(mode="json"),
        "bounds": asset.bounds.model_dump(mode="json"),
        # TheSuperHackers @fix Leex 22/08/2026 Retain the validated path grids for path-free downstream evidence. (#TBD)
        "validated_spatial_projection": {
            "schema_version": asset.schema_version,
            "content_sha256": asset.content_sha256,
            "map_identity": asset.map_identity,
            "engine_data_identity": asset.engine_data_identity,
            "pathing": asset.pathing.model_dump(mode="json"),
            "world_bounds": asset.bounds.model_dump(mode="json"),
            "ground_passable": list(asset.ground_passable),
            "amphibious_passable": list(asset.amphibious_passable),
            "zone_ids": list(asset.zone_ids),
        },
        "feature_counts": {
            "start_positions": len(asset.start_positions),
            "waypoints": len(asset.waypoints),
            "bridges": len(asset.bridges),
            "static_objects": len(asset.static_objects),
        },
    }
    return NormalizedMap(asset, metadata, resource_specs, region_specs)


def persist_normalized_map(
    session: Session,
    normalized: NormalizedMap,
    manifest_asset_id: int,
    uuid_factory: Callable[[], UUID],
) -> Map:
    """Insert one immutable content-addressed map, or reuse its exact prior projection."""
    asset = normalized.asset
    existing = session.scalar(select(Map).where(Map.content_sha256 == asset.content_sha256))
    if existing is not None:
        if existing.manifest_asset_id != manifest_asset_id:
            raise ValueError("map content identity is bound to another manifest asset")
        return existing
    row = Map(
        public_id=str(uuid_factory()),
        content_sha256=asset.content_sha256,
        manifest_asset_id=manifest_asset_id,
        schema_version=asset.schema_version,
        engine_data_identity=asset.engine_data_identity,
        map_identity=asset.map_identity,
        display_name=None,
        exporter_version=f"zero-hour-replay-map-export-v{asset.schema_version}",
        min_x=asset.bounds.minimum.x,
        min_y=asset.bounds.minimum.y,
        min_z=asset.bounds.minimum.z,
        max_x=asset.bounds.maximum.x,
        max_y=asset.bounds.maximum.y,
        max_z=asset.bounds.maximum.z,
        pathing_width=asset.pathing.width,
        pathing_height=asset.pathing.height,
        pathing_cell_size=asset.pathing.cell_size.x,
        terrain_width=asset.terrain.width,
        terrain_height=asset.terrain.height,
        terrain_cell_size=asset.terrain.cell_size.x,
        metadata_json=normalized.metadata,
    )
    session.add(row)
    session.flush()
    for resource in normalized.resources:
        session.add(
            MapResource(
                map_id=row.id,
                stable_key=resource.stable_key,
                resource_kind=resource.resource_kind,
                source_object_id=resource.source_object_id,
                template_name=resource.template_name,
                owner_player_index=resource.owner_player_index,
                amount=None,
                x=resource.x,
                y=resource.y,
                z=resource.z,
                payload_json=resource.payload,
            )
        )
    for region in normalized.regions:
        session.add(
            MapRegion(
                map_id=row.id,
                stable_key=region.stable_key,
                region_kind=region.region_kind,
                label=region.label,
                geometry_json=region.geometry,
                relationships_json=region.relationships,
                payload_json=region.payload,
            )
        )
    session.flush()
    return row
