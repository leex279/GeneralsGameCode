"""Strict, bounded loader for authoritative Zero Hour map-export assets."""

from __future__ import annotations

import hashlib
import json
import math
import stat
import struct
import zlib
from collections.abc import Mapping
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Protocol, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

FLOAT32_MAX = 3.40282347e38
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_GRID_ELEMENTS = 16 * 1024 * 1024
MEMBER_NAMES = frozenset(
    {
        "height.f32.zlib",
        "pathing-amphibious.u8.zlib",
        "pathing-ground.u8.zlib",
        "terrain.u8.zlib",
        "zones.i32.zlib",
    }
)
ASSET_NAMES = MEMBER_NAMES | {"manifest.json"}
CELL_TYPES = (
    (0, "CELL_CLEAR"),
    (1, "CELL_WATER"),
    (2, "CELL_CLIFF"),
    (3, "CELL_RUBBLE"),
    (4, "CELL_OBSTACLE"),
    (5, "CELL_BRIDGE_IMPASSABLE"),
    (6, "CELL_IMPASSABLE"),
)
DTYPE_SIZES = {"float32": 4, "uint8": 1, "int32": 4}


class MapAssetValidationError(ValueError):
    """Raised before any partially validated map data can escape."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key}")
        result[key] = value
    return result


def _schema() -> dict[str, object]:
    filename = "map-asset-v1.schema.json"
    source = Path(__file__).resolve().parents[3] / "contracts" / filename
    if source.is_file():
        return cast(dict[str, object], json.loads(source.read_text(encoding="utf-8")))
    packaged = resources.files("generals_replay_analyzer").joinpath("data", filename)
    return cast(dict[str, object], json.loads(packaged.read_text(encoding="utf-8")))


_SCHEMA_VALIDATOR = Draft202012Validator(_schema())


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False) + "\n").encode()


def _require_canonical_json(raw: bytes, parsed: object) -> None:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise MapAssetValidationError("map manifest must not contain a UTF-8 BOM")
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n") or b"\r" in raw:
        raise MapAssetValidationError("map manifest must contain exactly one terminal LF")
    try:
        canonical = _canonical_json(parsed)
    except (TypeError, ValueError) as error:
        raise MapAssetValidationError(f"map manifest cannot be canonicalized: {error}") from error
    if raw != canonical:
        raise MapAssetValidationError("map manifest is not canonical sorted compact UTF-8 JSON")


def _is_reparse(path: Path) -> bool:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _require_plain_owned_file(path: Path) -> None:
    try:
        if _is_reparse(path):
            raise MapAssetValidationError(f"map asset member is a symlink or reparse point: {path.name}")
        info = path.stat()
    except FileNotFoundError as error:
        raise MapAssetValidationError(f"map asset member is missing: {path.name}") from error
    except OSError as error:
        raise MapAssetValidationError(f"cannot inspect map asset member {path.name}: {error}") from error
    if not path.is_file():
        raise MapAssetValidationError(f"map asset member is not a regular file: {path.name}")
    if info.st_nlink != 1:
        raise MapAssetValidationError(f"map asset member is a hardlink: {path.name}")


def _finite_engine_real(value: float, field: str) -> float:
    if not math.isfinite(value) or abs(value) > FLOAT32_MAX:
        raise ValueError(f"{field} must be a finite float32 engine Real")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Position2(StrictModel):
    x: float
    y: float

    @field_validator("x", "y")
    @classmethod
    def _finite(cls, value: float) -> float:
        return _finite_engine_real(value, "coordinate")


class Position3(Position2):
    z: float

    @field_validator("z")
    @classmethod
    def _finite_z(cls, value: float) -> float:
        return _finite_engine_real(value, "coordinate")


class WorldBounds(StrictModel):
    maximum: Position3
    maximum_inclusive: Literal[True]
    minimum: Position3
    minimum_inclusive: Literal[True]

    @model_validator(mode="after")
    def _ordered(self) -> WorldBounds:
        if any(
            low > high
            for low, high in zip(
                (self.minimum.x, self.minimum.y, self.minimum.z),
                (self.maximum.x, self.maximum.y, self.maximum.z),
                strict=True,
            )
        ):
            raise ValueError("world bounds minimum exceeds maximum")
        return self


class EntitySamplePolicy(StrictModel):
    bounded_layer_statuses: list[str]
    bounded_position_policies: list[str]
    exempt_position_policies: list[str]
    policy: Literal["pathfinder_xy_closed_except_explicit_engine_category"]
    policy_source: Literal[
        "ReplayMovementSampler KindOf, current locomotor AIR surface, WanderAIUpdate, "
        "or physics without AI pathing"
    ]

    @model_validator(mode="after")
    def _exact_policy_lists(self) -> EntitySamplePolicy:
        if self.bounded_layer_statuses != ["stable", "dynamic_bridge_layer"]:
            raise ValueError("bounded layer statuses must equal the closed manifest policy")
        if self.bounded_position_policies != ["pathfinder_xy_closed"]:
            raise ValueError("bounded position policies must equal the closed source-grounded policy")
        if self.exempt_position_policies != [
            "exempt_kindof_aircraft",
            "exempt_kindof_bridge",
            "exempt_kindof_projectile",
            "exempt_locomotor_air_surface",
            "exempt_module_wander_ai",
            "exempt_physics_without_ai_pathing",
        ]:
            raise ValueError("exempt position policies must equal the closed source-grounded policy")
        return self


class CoordinateSystem(StrictModel):
    axes: list[str]
    bounds: WorldBounds
    entity_sample_policy: EntitySamplePolicy
    float_encoding: Literal["IEEE-754-binary32"]
    units: Literal["engine_world_unit"]

    @field_validator("axes")
    @classmethod
    def _exact_axes(cls, value: list[str]) -> list[str]:
        if value != ["engine_world_x", "engine_world_y", "engine_world_z"]:
            raise ValueError("axes must equal the engine world coordinate convention")
        return value


class GridBounds(StrictModel):
    maximum_exclusive: Position2
    minimum_inclusive: Position2


class IndexOrigin(StrictModel):
    x: int
    y: int


class GridSpec(StrictModel):
    bounds: GridBounds
    cell_size: Position2
    dimension_source: Annotated[str, Field(min_length=1, max_length=4096)]
    height: Annotated[int, Field(ge=1, le=16384)]
    index_origin: IndexOrigin
    sample_point: Literal["cell_center"]
    width: Annotated[int, Field(ge=1, le=16384)]

    @model_validator(mode="after")
    def _coherent_extent(self) -> GridSpec:
        if self.width * self.height > MAX_GRID_ELEMENTS:
            raise ValueError("grid dimensions exceed the bounded element limit")
        if self.cell_size.x <= 0 or self.cell_size.y <= 0:
            raise ValueError("grid cell sizes must be positive")
        expected_max_x = self.bounds.minimum_inclusive.x + self.width * self.cell_size.x
        expected_max_y = self.bounds.minimum_inclusive.y + self.height * self.cell_size.y
        if self.bounds.maximum_exclusive.x != expected_max_x or self.bounds.maximum_exclusive.y != expected_max_y:
            raise ValueError("grid dimensions, origin, cell size, and bounds disagree")
        return self


class Grids(StrictModel):
    pathing: GridSpec
    terrain: GridSpec


class CellType(StrictModel):
    name: str
    value: Annotated[int, Field(ge=0, le=6)]


class Classification(StrictModel):
    amphibious_passable_cell_types: list[int]
    cell_types: list[CellType]
    ground_passable_cell_types: list[int]
    pathing_derivation_source: Literal["Pathfinder::validLocomotorSurfacesForCellType"]
    raw_cell_type_source: Literal["PathfindCell::getType"]
    raw_zone_source: Literal["PathfindCell::getZone"]

    @model_validator(mode="after")
    def _exact_cell_type_domain(self) -> Classification:
        if self.amphibious_passable_cell_types != [0, 1] or self.ground_passable_cell_types != [0]:
            raise ValueError("passable cell type sets must equal the source-grounded closed mapping")
        if tuple((entry.value, entry.name) for entry in self.cell_types) != CELL_TYPES:
            raise ValueError("cell type catalog must exactly match PathfindCell::CellType")
        return self


class MemberMetadata(StrictModel):
    compression: Literal["zlib"]
    compression_level: Literal[9]
    compressed_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compressed_size: Annotated[int, Field(ge=1, le=MAX_MEMBER_BYTES)]
    dtype: Literal["float32", "uint8", "int32"]
    element_count: Annotated[int, Field(ge=1, le=MAX_GRID_ELEMENTS)]
    endianness: Literal["little"]
    grid: Literal["pathing"]
    uncompressed_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    uncompressed_size: Annotated[int, Field(ge=1, le=MAX_MEMBER_BYTES)]

    @model_validator(mode="after")
    def _exact_dtype_length(self) -> MemberMetadata:
        if self.uncompressed_size != self.element_count * DTYPE_SIZES[self.dtype]:
            raise ValueError("member uncompressed size disagrees with dtype and element count")
        return self


BoundsPolicy = Literal["pathfinder_xy_closed", "not_asserted_by_source"]


class StartPosition(StrictModel):
    bounds_policy: Literal["pathfinder_xy_closed"]
    category_source: Literal["GameSlot::getStartPos + TerrainLogic::getWaypointByName"]
    name: Annotated[str, Field(min_length=1, max_length=1024)]
    position: Position3
    slot_indices: list[Annotated[int, Field(ge=0, le=7)]]
    waypoint_id: Annotated[int, Field(ge=0)]


class WaypointFeature(StrictModel):
    bidirectional: bool
    bounds_policy: BoundsPolicy
    labels: Annotated[list[str], Field(max_length=3)]
    link_ids: Annotated[list[Annotated[int, Field(ge=0)]], Field(max_length=8)]
    name: Annotated[str, Field(min_length=1, max_length=1024)]
    position: Position3
    waypoint_id: Annotated[int, Field(ge=0)]


class BridgeFeature(StrictModel):
    bounds_policy: Literal["pathfinder_xy_closed"]
    bridge_width: Annotated[float, Field(ge=0)]
    category_source: Literal["TerrainLogic::getFirstBridge"]
    corners: Annotated[list[Position3], Field(min_length=4, max_length=4)]
    from_: Position3 = Field(alias="from")
    layer_id: Annotated[int, Field(ge=0)]
    object_id: Annotated[int, Field(gt=0)] | None
    template_name: Annotated[str, Field(min_length=1, max_length=1024)]
    to: Position3


class ObjectCategory(StrictModel):
    name: Literal[
        "static_blocker",
        "supply_source",
        "supply_warehouse",
        "capturable",
        "tech_building",
        "cash_generator",
        "oil_income",
        "bridge",
    ]
    source: Annotated[str, Field(min_length=1, max_length=4096)]


class StaticObjectFeature(StrictModel):
    bounds_policy: Literal["pathfinder_xy_closed"]
    categories: Annotated[list[ObjectCategory], Field(min_length=1)]
    creation_source: Literal["map_loaded"]
    object_id: Annotated[int, Field(gt=0)]
    orientation: float
    position: Position3
    snapshot_scope: Literal["post_map_initialization"]
    template_name: Annotated[str, Field(min_length=1, max_length=1024)]

    @field_validator("orientation")
    @classmethod
    def _finite_orientation(cls, value: float) -> float:
        return _finite_engine_real(value, "orientation")


class Features(StrictModel):
    bridges: list[BridgeFeature]
    start_positions: list[StartPosition]
    static_objects: list[StaticObjectFeature]
    waypoints: list[WaypointFeature]


class Producer(StrictModel):
    name: Literal["zero-hour-replay-map-export"]
    version: Literal[1]
    zlib_version: Annotated[str, Field(min_length=1, max_length=128)]


class MapAssetManifest(StrictModel):
    classification: Classification
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coordinate_system: CoordinateSystem
    engine_data_identity: Annotated[str, Field(min_length=1, max_length=4096)]
    features: Features
    grids: Grids
    map_identity: Annotated[str, Field(min_length=1, max_length=4096)]
    members: dict[str, MemberMetadata]
    producer: Producer
    schema_version: Literal[1]
    type: Literal["map_asset"]

    @model_validator(mode="after")
    def _exact_members_and_grid_lengths(self) -> MapAssetManifest:
        if set(self.members) != MEMBER_NAMES:
            raise ValueError("map asset manifest must declare exactly the five canonical members")
        count = self.grids.pathing.width * self.grids.pathing.height
        # V1 supports distinct metadata, but every current required member explicitly names the pathing grid.
        if (
            self.grids.terrain != self.grids.pathing
            and self.grids.terrain.width * self.grids.terrain.height != count
        ):
            raise ValueError("v1 height/terrain and pathing grid element counts differ without separate members")
        if any(member.element_count != count for member in self.members.values()):
            raise ValueError("member element count disagrees with declared pathing grid dimensions")
        expected_dtypes = {
            "height.f32.zlib": "float32",
            "pathing-amphibious.u8.zlib": "uint8",
            "pathing-ground.u8.zlib": "uint8",
            "terrain.u8.zlib": "uint8",
            "zones.i32.zlib": "int32",
        }
        for name, expected in expected_dtypes.items():
            if self.members[name].dtype != expected:
                raise ValueError(f"{name} must use dtype {expected}")
        return self


class _XYPosition(Protocol):
    @property
    def x(self) -> float: ...

    @property
    def y(self) -> float: ...


class _EntityPositionPayload(Protocol):
    @property
    def layer_name(self) -> object: ...

    @property
    def layer_name_status(self) -> object: ...

    @property
    def position_bounds_policy(self) -> object: ...

    @property
    def position(self) -> _XYPosition: ...

    @property
    def path_goal(self) -> _XYPosition | None: ...


class MapAsset(StrictModel):
    """Fully validated immutable map values; constructed only after every member succeeds."""

    content_sha256: str
    engine_data_identity: str
    map_identity: str
    pathing: GridSpec
    terrain: GridSpec
    bounds: WorldBounds
    height_values: tuple[float, ...]
    terrain_cell_types: tuple[int, ...]
    ground_passable: tuple[bool, ...]
    amphibious_passable: tuple[bool, ...]
    zone_ids: tuple[int, ...]
    start_positions: tuple[StartPosition, ...]
    waypoints: tuple[WaypointFeature, ...]
    bridges: tuple[BridgeFeature, ...]
    static_objects: tuple[StaticObjectFeature, ...]

    def _require_xy(self, position: _XYPosition, label: str) -> None:
        x = position.x
        y = position.y
        if not self.bounds.minimum.x <= x <= self.bounds.maximum.x or not self.bounds.minimum.y <= y <= self.bounds.maximum.y:
            raise MapAssetValidationError(
                f"{label} ({x}, {y}) is outside pathfinder bounds "
                f"[{self.bounds.minimum.x}, {self.bounds.maximum.x}] x "
                f"[{self.bounds.minimum.y}, {self.bounds.maximum.y}]"
            )

    def require_entity_position(self, payload: _EntityPositionPayload, kind_of_flags: frozenset[str]) -> None:
        """Apply the manifest's explicit layer policy to a telemetry sample and path goal."""
        policy = payload.position_bounds_policy
        if not isinstance(policy, str):
            raise MapAssetValidationError("entity sample has unknown position bounds policy")
        expected_kind_of = {
            "exempt_kindof_aircraft": "AIRCRAFT",
            "exempt_kindof_bridge": "BRIDGE",
            "exempt_kindof_projectile": "PROJECTILE",
        }.get(policy)
        if expected_kind_of is not None:
            if expected_kind_of not in kind_of_flags:
                raise MapAssetValidationError(
                    f"entity sample bounds policy {policy} contradicts game-data KindOf flags"
                )
            return
        if policy in {
            "exempt_locomotor_air_surface",
            "exempt_module_wander_ai",
            "exempt_physics_without_ai_pathing",
        }:
            return
        if policy != "pathfinder_xy_closed":
            raise MapAssetValidationError("entity sample has unknown position bounds policy")
        if payload.layer_name_status not in {"stable", "dynamic_bridge_layer"}:
            return
        self._require_xy(
            payload.position,
            "entity sample position "
            f"for object {getattr(payload, 'object_id', '<unknown>')} "
            f"template {getattr(payload, 'template_name', '<unknown>')} "
            f"layer {payload.layer_name}",
        )
        if payload.path_goal is not None:
            self._require_xy(payload.path_goal, "entity sample path goal")


def _reference_value(reference: object, name: str) -> object:
    if isinstance(reference, Mapping):
        return reference.get(name)
    return getattr(reference, name, None)


def _validate_reference(manifest_path: Path, manifest_bytes: bytes, manifest: MapAssetManifest, reference: object) -> None:
    expected_fields = {
        "type": "map_asset",
        "schema_version": 1,
        "content_sha256": manifest.content_sha256,
        "engine_data_identity": manifest.engine_data_identity,
        "map_identity": manifest.map_identity,
        "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    for field, expected in expected_fields.items():
        if _reference_value(reference, field) != expected:
            raise MapAssetValidationError(f"map asset reference {field} differs from exact asset identity")
    path_value = _reference_value(reference, "path")
    expected_path = f"map-assets-v1/{manifest.content_sha256}/manifest.json"
    if not isinstance(path_value, str) or path_value != expected_path:
        raise MapAssetValidationError("map asset reference path is not the safe content-addressed path")
    if PurePosixPath(path_value).parts != tuple(expected_path.split("/")):
        raise MapAssetValidationError("map asset reference path is not the safe content-addressed path")
    if tuple(manifest_path.parts[-3:]) != ("map-assets-v1", manifest.content_sha256, "manifest.json"):
        raise MapAssetValidationError("map manifest path differs from its content-addressed reference")


def _read_member(asset_dir: Path, name: str, metadata: MemberMetadata) -> bytes:
    path = asset_dir / name
    _require_plain_owned_file(path)
    try:
        compressed = path.read_bytes()
    except OSError as error:
        raise MapAssetValidationError(f"cannot read map asset member {name}: {error}") from error
    if len(compressed) != metadata.compressed_size:
        raise MapAssetValidationError(f"{name} compressed size differs from manifest")
    if hashlib.sha256(compressed).hexdigest() != metadata.compressed_sha256:
        raise MapAssetValidationError(f"{name} compressed sha256 differs from manifest")

    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(compressed, metadata.uncompressed_size + 1)
        flushed = decompressor.flush()
    except zlib.error as error:
        raise MapAssetValidationError(f"{name} has an invalid zlib stream: {error}") from error
    if decompressor.unused_data or decompressor.unconsumed_tail or flushed:
        raise MapAssetValidationError(f"{name} has trailing or overlong compressed data")
    if not decompressor.eof:
        raise MapAssetValidationError(f"{name} contains a truncated zlib stream")
    if len(raw) != metadata.uncompressed_size:
        raise MapAssetValidationError(f"{name} uncompressed size differs from manifest")
    if hashlib.sha256(raw).hexdigest() != metadata.uncompressed_sha256:
        raise MapAssetValidationError(f"{name} uncompressed sha256 differs from manifest")
    return raw


def _validate_bounded_features(manifest: MapAssetManifest) -> None:
    minimum = manifest.coordinate_system.bounds.minimum
    maximum = manifest.coordinate_system.bounds.maximum

    def require(position: Position3, label: str) -> None:
        if not minimum.x <= position.x <= maximum.x or not minimum.y <= position.y <= maximum.y:
            raise MapAssetValidationError(f"{label} is outside pathfinder bounds")

    for start in manifest.features.start_positions:
        require(start.position, f"start position {start.name}")
    for waypoint in manifest.features.waypoints:
        if waypoint.bounds_policy == "pathfinder_xy_closed":
            require(waypoint.position, f"waypoint {waypoint.name}")
    for bridge in manifest.features.bridges:
        require(bridge.from_, f"bridge {bridge.template_name} endpoint")
        require(bridge.to, f"bridge {bridge.template_name} endpoint")
        for corner in bridge.corners:
            require(corner, f"bridge {bridge.template_name} corner")
    for static_object in manifest.features.static_objects:
        require(static_object.position, f"static object {static_object.object_id}")


def load_map_asset(
    manifest_path: Path,
    *,
    expected_reference: object | None = None,
    expected_engine_data_identity: str | None = None,
    expected_map_identity: str | None = None,
) -> MapAsset:
    """Validate an entire map directory atomically, then expose typed immutable values."""
    manifest_path = Path(manifest_path)
    asset_dir = manifest_path.parent
    try:
        if _is_reparse(asset_dir):
            raise MapAssetValidationError("map asset directory is a symlink or reparse point")
        if not asset_dir.is_dir():
            raise MapAssetValidationError("map asset directory does not exist")
        actual_names = {entry.name for entry in asset_dir.iterdir()}
    except FileNotFoundError as error:
        raise MapAssetValidationError("map asset directory does not exist") from error
    except OSError as error:
        raise MapAssetValidationError(f"cannot inspect map asset directory: {error}") from error
    unexpected = sorted(actual_names - ASSET_NAMES)
    missing = sorted(ASSET_NAMES - actual_names)
    if unexpected:
        raise MapAssetValidationError(f"unexpected asset member: {unexpected[0]}")
    if missing:
        raise MapAssetValidationError(f"map asset member is missing: {missing[0]}")

    _require_plain_owned_file(manifest_path)
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest_text = manifest_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MapAssetValidationError(f"map manifest contains invalid UTF-8: {error}") from error
    except OSError as error:
        raise MapAssetValidationError(f"cannot read map manifest: {error}") from error
    try:
        parsed = json.loads(
            manifest_text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise MapAssetValidationError(f"map manifest contains invalid JSON: {error}") from error
    _require_canonical_json(manifest_bytes, parsed)
    schema_errors = sorted(_SCHEMA_VALIDATOR.iter_errors(parsed), key=lambda issue: list(issue.absolute_path))
    if schema_errors:
        schema_issue = schema_errors[0]
        location = ".".join(str(part) for part in schema_issue.absolute_path) or "root"
        raise MapAssetValidationError(f"map manifest schema path {location}: {schema_issue.message}")
    try:
        manifest = MapAssetManifest.model_validate(parsed)
    except ValidationError as error:
        raise MapAssetValidationError(f"map manifest typed validation failed: {error.errors()[0]['msg']}") from error

    if asset_dir.name != manifest.content_sha256 or asset_dir.parent.name != "map-assets-v1":
        raise MapAssetValidationError("map content hash directory does not match manifest")
    field = f'"content_sha256":"{manifest.content_sha256}"'.encode()
    if manifest_bytes.count(field) != 1:
        raise MapAssetValidationError("canonical map manifest has an ambiguous content hash field")
    placeholder = manifest_bytes.replace(field, b'"content_sha256":"' + b"0" * 64 + b'"', 1)
    if hashlib.sha256(placeholder).hexdigest() != manifest.content_sha256:
        raise MapAssetValidationError("map manifest content hash does not bind its canonical descriptor")
    if expected_reference is not None:
        _validate_reference(manifest_path, manifest_bytes, manifest, expected_reference)
    if expected_engine_data_identity is not None and manifest.engine_data_identity != expected_engine_data_identity:
        raise MapAssetValidationError("map asset engine identity differs from telemetry manifest")
    if expected_map_identity is not None and manifest.map_identity != expected_map_identity:
        raise MapAssetValidationError("map asset map identity differs from telemetry manifest")

    raw_members = {name: _read_member(asset_dir, name, manifest.members[name]) for name in sorted(MEMBER_NAMES)}
    count = manifest.grids.pathing.width * manifest.grids.pathing.height
    heights = struct.unpack(f"<{count}f", raw_members["height.f32.zlib"])
    if any(not math.isfinite(value) for value in heights):
        raise MapAssetValidationError("height grid contains a non-finite float32 value")
    terrain = tuple(raw_members["terrain.u8.zlib"])
    if any(value > 6 for value in terrain):
        raise MapAssetValidationError("terrain grid contains an unknown raw cell type")
    ground_raw = tuple(raw_members["pathing-ground.u8.zlib"])
    amphibious_raw = tuple(raw_members["pathing-amphibious.u8.zlib"])
    if any(value not in {0, 1} for value in ground_raw + amphibious_raw):
        raise MapAssetValidationError("pathing grid values must be closed binary flags")
    expected_ground = tuple(1 if cell == 0 else 0 for cell in terrain)
    expected_amphibious = tuple(1 if cell in {0, 1} else 0 for cell in terrain)
    if ground_raw != expected_ground or amphibious_raw != expected_amphibious:
        raise MapAssetValidationError("pathing flags differ from the declared source-grounded cell-type mapping")
    zones = struct.unpack(f"<{count}i", raw_members["zones.i32.zlib"])
    if any(not 0 <= zone <= 16_383 for zone in zones):
        raise MapAssetValidationError("zone grid contains a value outside PathfindCell's 14-bit zone domain")
    _validate_bounded_features(manifest)

    # Construct only after all schema, identity, filesystem, decompression, grid, and feature checks succeed.
    return MapAsset(
        content_sha256=manifest.content_sha256,
        engine_data_identity=manifest.engine_data_identity,
        map_identity=manifest.map_identity,
        pathing=manifest.grids.pathing,
        terrain=manifest.grids.terrain,
        bounds=manifest.coordinate_system.bounds,
        height_values=tuple(heights),
        terrain_cell_types=terrain,
        ground_passable=tuple(bool(value) for value in ground_raw),
        amphibious_passable=tuple(bool(value) for value in amphibious_raw),
        zone_ids=tuple(zones),
        start_positions=tuple(manifest.features.start_positions),
        waypoints=tuple(manifest.features.waypoints),
        bridges=tuple(manifest.features.bridges),
        static_objects=tuple(manifest.features.static_objects),
    )
