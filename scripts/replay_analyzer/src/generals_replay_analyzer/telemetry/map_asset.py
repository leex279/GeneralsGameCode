"""Strict, bounded loader for authoritative Zero Hour map-export assets."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import struct
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Protocol, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

FLOAT32_MAX = 3.40282347e38
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_GRID_ELEMENTS = 16 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
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
POSITION_POLICIES = frozenset(
    {
        "pathfinder_xy_closed",
        "exempt_kindof_aircraft",
        "exempt_kindof_bridge",
        "exempt_kindof_projectile",
        "exempt_kindof_parachutable",
        "exempt_locomotor_air_surface",
        "exempt_map_loaded_unclassified_immobile",
    }
)
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


@dataclass(frozen=True)
class _PathIdentity:
    device: int
    inode: int
    mode: int
    link_count: int
    attributes: int


@dataclass(frozen=True)
class _DirectoryGuard:
    trace_directory: Path
    cache_directory: Path
    asset_directory: Path
    trace_identity: _PathIdentity
    cache_identity: _PathIdentity
    asset_identity: _PathIdentity
    trace_resolved: Path
    cache_resolved: Path
    asset_resolved: Path


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key}")
        result[key] = value
    return result


def _schema(version: int) -> dict[str, object]:
    filename = f"map-asset-v{version}.schema.json"
    source = Path(__file__).resolve().parents[3] / "contracts" / filename
    if source.is_file():
        return cast(dict[str, object], json.loads(source.read_text(encoding="utf-8")))
    packaged = resources.files("generals_replay_analyzer").joinpath("data", filename)
    return cast(dict[str, object], json.loads(packaged.read_text(encoding="utf-8")))


_SCHEMA_VALIDATORS = {version: Draft202012Validator(_schema(version)) for version in (1, 2)}


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


def _stat_identity(info: object, label: str) -> _PathIdentity:
    values: dict[str, int] = {}
    for name in ("st_dev", "st_ino", "st_mode", "st_nlink"):
        value = getattr(info, name, None)
        if type(value) is not int:
            raise MapAssetValidationError(f"{label} filesystem identity is unavailable")
        values[name] = value
    if values["st_ino"] == 0:
        raise MapAssetValidationError(f"{label} filesystem identity is unavailable")
    attributes = getattr(info, "st_file_attributes", 0)
    if type(attributes) is not int:
        raise MapAssetValidationError(f"{label} filesystem attributes are unavailable")
    return _PathIdentity(
        device=values["st_dev"],
        inode=values["st_ino"],
        mode=values["st_mode"],
        link_count=values["st_nlink"],
        attributes=attributes,
    )


def _is_reparse_identity(identity: _PathIdentity) -> bool:
    return stat.S_ISLNK(identity.mode) or bool(
        identity.attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _require_plain_owned_file(path: Path) -> tuple[os.stat_result, _PathIdentity]:
    try:
        info = path.lstat()
        identity = _stat_identity(info, f"map asset member {path.name}")
        if _is_reparse_identity(identity):
            raise MapAssetValidationError(f"map asset member is a symlink or reparse point: {path.name}")
    except FileNotFoundError as error:
        raise MapAssetValidationError(f"map asset member is missing: {path.name}") from error
    except OSError as error:
        raise MapAssetValidationError(f"cannot inspect map asset member {path.name}: {error}") from error
    if not stat.S_ISREG(info.st_mode):
        raise MapAssetValidationError(f"map asset member is not a regular file: {path.name}")
    if identity.link_count != 1:
        raise MapAssetValidationError(f"map asset member is a hardlink: {path.name}")
    return info, identity


def _require_plain_directory(path: Path, label: str) -> _PathIdentity:
    try:
        info = path.lstat()
        identity = _stat_identity(info, label)
    except FileNotFoundError as error:
        raise MapAssetValidationError(f"{label} does not exist") from error
    except OSError as error:
        raise MapAssetValidationError(f"cannot inspect {label}: {error}") from error
    if _is_reparse_identity(identity):
        raise MapAssetValidationError(f"{label} is a symlink or reparse point")
    if not stat.S_ISDIR(info.st_mode):
        raise MapAssetValidationError(f"{label} is not a plain directory")
    return identity


def _require_same_directory(path: Path, label: str, expected: _PathIdentity) -> None:
    if _require_plain_directory(path, label) != expected:
        raise MapAssetValidationError(f"{label} identity changed")


def _revalidate_directory_guard(guard: _DirectoryGuard) -> None:
    _require_same_directory(guard.trace_directory, "trace directory", guard.trace_identity)
    _require_same_directory(guard.cache_directory, "cache directory", guard.cache_identity)
    _require_same_directory(guard.asset_directory, "asset directory", guard.asset_identity)
    try:
        trace_resolved = guard.trace_directory.resolve(strict=True)
        cache_resolved = guard.cache_directory.resolve(strict=True)
        asset_resolved = guard.asset_directory.resolve(strict=True)
        common = Path(os.path.commonpath((trace_resolved, asset_resolved)))
    except (OSError, ValueError) as error:
        raise MapAssetValidationError(f"cannot revalidate map asset containment: {error}") from error
    if (
        trace_resolved != guard.trace_resolved
        or cache_resolved != guard.cache_resolved
        or asset_resolved != guard.asset_resolved
        or common != trace_resolved
        or cache_resolved.parent != trace_resolved
        or asset_resolved.parent != cache_resolved
    ):
        raise MapAssetValidationError("map asset directory chain identity or containment changed")


def _validate_contained_asset_path(
    manifest_path: Path, trusted_trace_directory: Path | None
) -> _DirectoryGuard:
    trace_directory = (
        Path(trusted_trace_directory)
        if trusted_trace_directory is not None
        else manifest_path.parent.parent.parent
    )
    trace_identity = _require_plain_directory(trace_directory, "trusted trace directory")
    cache_directory = manifest_path.parent.parent
    if cache_directory.name not in {"map-assets-v1", "map-assets-v2"}:
        raise MapAssetValidationError("map manifest is not below a supported versioned map asset cache")
    cache_identity = _require_plain_directory(cache_directory, "map asset cache directory")
    asset_identity = _require_plain_directory(manifest_path.parent, "map asset directory")
    try:
        trusted_resolved = trace_directory.resolve(strict=True)
        cache_resolved = cache_directory.resolve(strict=True)
        asset_resolved = manifest_path.parent.resolve(strict=True)
        manifest_resolved = manifest_path.resolve(strict=True)
        common = Path(os.path.commonpath((trusted_resolved, manifest_resolved)))
    except (OSError, ValueError) as error:
        raise MapAssetValidationError(f"cannot resolve map asset containment: {error}") from error
    if common != trusted_resolved:
        raise MapAssetValidationError("map manifest escapes the trusted trace directory")
    if (
        cache_resolved.parent != trusted_resolved
        or asset_resolved.parent != cache_resolved
        or manifest_resolved.parent != asset_resolved
        or manifest_resolved.name != "manifest.json"
    ):
        raise MapAssetValidationError("map manifest is not at the canonical map asset cache path")
    guard = _DirectoryGuard(
        trace_directory=trace_directory,
        cache_directory=cache_directory,
        asset_directory=manifest_path.parent,
        trace_identity=trace_identity,
        cache_identity=cache_identity,
        asset_identity=asset_identity,
        trace_resolved=trusted_resolved,
        cache_resolved=cache_resolved,
        asset_resolved=asset_resolved,
    )
    _revalidate_directory_guard(guard)
    return guard


def _read_bounded_file(
    path: Path,
    maximum_size: int,
    label: str,
    directory_guard: _DirectoryGuard,
    *,
    exact_size: int | None = None,
) -> bytes:
    info, preflight_identity = _require_plain_owned_file(path)
    if info.st_size < 1 or info.st_size > maximum_size:
        raise MapAssetValidationError(f"{label} size exceeds its closed bound")
    if exact_size is not None and info.st_size != exact_size:
        raise MapAssetValidationError(f"{label} compressed size differs from manifest")
    data = bytearray()
    try:
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            opened_identity = _stat_identity(opened, f"opened {label}")
            if (
                not stat.S_ISREG(opened_identity.mode)
                or opened_identity.link_count != 1
                or _is_reparse_identity(opened_identity)
            ):
                raise MapAssetValidationError(f"{label} opened handle is not a safe regular file")
            if opened_identity != preflight_identity:
                raise MapAssetValidationError(f"{label} opened identity differs from preflight")
            if opened.st_size != info.st_size:
                raise MapAssetValidationError(f"{label} size changed during validation")
            _revalidate_directory_guard(directory_guard)
            while len(data) < info.st_size:
                chunk = source.read(min(_READ_CHUNK_BYTES, info.st_size - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if source.read(1):
                raise MapAssetValidationError(f"{label} grew during validation")
            _revalidate_directory_guard(directory_guard)
    except OSError as error:
        raise MapAssetValidationError(f"cannot read {label}: {error}") from error
    if len(data) != info.st_size:
        raise MapAssetValidationError(f"{label} size changed during validation")
    return bytes(data)


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
        "ReplayMovementSampler KindOf or catalog-bound current locomotor AIR surface",
        "ReplayMovementSampler KindOf, map-loaded lifecycle KindOf, or catalog-bound current locomotor AIR surface"
    ]

    @model_validator(mode="after")
    def _exact_policy_lists(self) -> EntitySamplePolicy:
        if self.bounded_layer_statuses != ["stable", "dynamic_bridge_layer", "unknown_engine_value"]:
            raise ValueError("bounded layer statuses must equal the closed manifest policy")
        if self.bounded_position_policies != ["pathfinder_xy_closed"]:
            raise ValueError("bounded position policies must equal the closed source-grounded policy")
        v1_exemptions = [
            "exempt_kindof_aircraft",
            "exempt_kindof_bridge",
            "exempt_kindof_projectile",
            "exempt_kindof_parachutable",
            "exempt_locomotor_air_surface",
        ]
        v2_exemptions = [*v1_exemptions, "exempt_map_loaded_unclassified_immobile"]
        expected = v1_exemptions if self.policy_source == (
            "ReplayMovementSampler KindOf or catalog-bound current locomotor AIR surface"
        ) else v2_exemptions
        if self.exempt_position_policies != expected:
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
    storage_order: Literal["row_major_y_then_x_x_fastest"]
    width: Annotated[int, Field(ge=1, le=16384)]

    @model_validator(mode="after")
    def _coherent_extent(self) -> GridSpec:
        if self.width * self.height > MAX_GRID_ELEMENTS:
            raise ValueError("grid dimensions exceed the bounded element limit")
        if self.cell_size.x <= 0 or self.cell_size.y <= 0:
            raise ValueError("grid cell sizes must be positive")
        expected_min_x = self.index_origin.x * self.cell_size.x
        expected_min_y = self.index_origin.y * self.cell_size.y
        if self.bounds.minimum_inclusive.x != expected_min_x or self.bounds.minimum_inclusive.y != expected_min_y:
            raise ValueError("grid index origin, cell size, and minimum bound disagree")
        expected_max_x = (self.index_origin.x + self.width) * self.cell_size.x
        expected_max_y = (self.index_origin.y + self.height) * self.cell_size.y
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
    link_names: Annotated[list[Annotated[str, Field(min_length=1, max_length=1024)]], Field(max_length=8)]
    link_waypoint_ids: Annotated[list[Annotated[int, Field(ge=0)]], Field(max_length=8)] | None = None
    name: Annotated[str, Field(min_length=1, max_length=1024)]
    position: Position3
    waypoint_id: Annotated[int, Field(ge=0)]


class BridgeFeature(StrictModel):
    bounds_policy: Literal["pathfinder_xy_closed"]
    bridge_index: Annotated[int, Field(ge=0)]
    bridge_width: Annotated[float, Field(ge=0)]
    category_source: Literal["TerrainLogic::getFirstBridge"]
    corners: Annotated[list[Position3], Field(min_length=4, max_length=4)]
    from_: Position3 = Field(alias="from")
    layer_id: Annotated[int, Field(ge=0)]
    object_id: Annotated[int, Field(gt=0)] | None
    template_name: Annotated[str, Field(min_length=1, max_length=1024)] | None
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
    source: Literal[
        "ThingTemplate::isKindOf(KINDOF_OBSTACLE)",
        "ThingTemplate::isKindOf(KINDOF_SUPPLY_SOURCE)",
        "Object::findUpdateModule(SupplyWarehouseDockUpdate)",
        "ThingTemplate::isKindOf(KINDOF_CAPTURABLE)",
        "ThingTemplate::isKindOf(KINDOF_TECH_BUILDING)",
        "ThingTemplate::isKindOf(KINDOF_CASH_GENERATOR)",
        "Object::findUpdateModule(AutoDepositUpdate)+capturable_or_tech_KindOf",
        "ThingTemplate::isKindOf(KINDOF_BRIDGE)",
    ]

    @model_validator(mode="after")
    def _source_matches_category(self) -> ObjectCategory:
        exact = {
            "static_blocker": "ThingTemplate::isKindOf(KINDOF_OBSTACLE)",
            "supply_source": "ThingTemplate::isKindOf(KINDOF_SUPPLY_SOURCE)",
            "supply_warehouse": "Object::findUpdateModule(SupplyWarehouseDockUpdate)",
            "capturable": "ThingTemplate::isKindOf(KINDOF_CAPTURABLE)",
            "tech_building": "ThingTemplate::isKindOf(KINDOF_TECH_BUILDING)",
            "cash_generator": "ThingTemplate::isKindOf(KINDOF_CASH_GENERATOR)",
            "oil_income": "Object::findUpdateModule(AutoDepositUpdate)+capturable_or_tech_KindOf",
            "bridge": "ThingTemplate::isKindOf(KINDOF_BRIDGE)",
        }
        if self.source != exact[self.name]:
            raise ValueError("category source does not match its closed engine classification")
        return self


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
    version: Literal[1, 2]
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
    schema_version: Literal[1, 2]
    type: Literal["map_asset"]

    @model_validator(mode="after")
    def _exact_members_and_grid_lengths(self) -> MapAssetManifest:
        if self.producer.version != self.schema_version:
            raise ValueError("map asset producer version must equal schema version")
        if set(self.members) != MEMBER_NAMES:
            raise ValueError("map asset manifest must declare exactly the five canonical members")
        count = self.grids.pathing.width * self.grids.pathing.height
        if self.grids.terrain != self.grids.pathing:
            raise ValueError("v1 requires identical terrain and pathing grid descriptors")
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
        path_bounds = self.grids.pathing.bounds
        world_bounds = self.coordinate_system.bounds
        if (
            world_bounds.minimum.x != path_bounds.minimum_inclusive.x
            or world_bounds.minimum.y != path_bounds.minimum_inclusive.y
            or world_bounds.maximum.x != path_bounds.maximum_exclusive.x
            or world_bounds.maximum.y != path_bounds.maximum_exclusive.y
        ):
            raise ValueError("world XY bounds must exactly equal pathing outer bounds")
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

    @property
    def current_locomotor_template_name(self) -> object: ...


class MapAsset(StrictModel):
    """Fully validated immutable map values; constructed only after every member succeeds."""

    schema_version: Literal[1, 2]
    content_sha256: str
    engine_data_identity: str
    map_identity: str
    declared_position_policies: frozenset[str]
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

    def require_entity_position(
        self,
        payload: _EntityPositionPayload,
        kind_of_flags: frozenset[str],
        catalog_air_locomotors: frozenset[str],
        creation_source: str | None = None,
    ) -> None:
        """Apply the manifest's explicit layer policy to a telemetry sample and path goal."""
        policy = payload.position_bounds_policy
        if not isinstance(policy, str) or policy not in POSITION_POLICIES:
            raise MapAssetValidationError("entity sample has unknown position bounds policy")
        if policy not in self.declared_position_policies:
            raise MapAssetValidationError(
                f"entity sample position policy {policy} is not declared by map asset schema version "
                f"{self.schema_version}"
            )
        expected_kind_of = {
            "exempt_kindof_aircraft": "AIRCRAFT",
            "exempt_kindof_bridge": "BRIDGE",
            "exempt_kindof_projectile": "PROJECTILE",
            "exempt_kindof_parachutable": "PARACHUTABLE",
        }.get(policy)
        if expected_kind_of is not None:
            if expected_kind_of not in kind_of_flags:
                raise MapAssetValidationError(
                    f"entity sample bounds policy {policy} contradicts game-data KindOf flags"
                )
            return
        if policy == "exempt_locomotor_air_surface":
            locomotor_name = payload.current_locomotor_template_name
            if not isinstance(locomotor_name, str) or locomotor_name not in catalog_air_locomotors:
                raise MapAssetValidationError(
                    "entity sample AIR exemption lacks a catalog-bound AIR locomotor for its template/set"
                )
            return
        if policy == "exempt_map_loaded_unclassified_immobile":
            object_id = getattr(payload, "object_id", None)
            classified_static_ids = {entry.object_id for entry in self.static_objects}
            if (
                creation_source != "map_loaded"
                or "IMMOBILE" not in kind_of_flags
                or type(object_id) is not int
                or object_id in classified_static_ids
            ):
                raise MapAssetValidationError(
                    "entity sample map-loaded unclassified immobile exemption lacks lifecycle/catalog/map evidence"
                )
            return
        if policy != "pathfinder_xy_closed":
            raise MapAssetValidationError("entity sample has unknown position bounds policy")
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
        "schema_version": manifest.schema_version,
        "content_sha256": manifest.content_sha256,
        "engine_data_identity": manifest.engine_data_identity,
        "map_identity": manifest.map_identity,
        "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    for field, expected in expected_fields.items():
        if _reference_value(reference, field) != expected:
            raise MapAssetValidationError(f"map asset reference {field} differs from exact asset identity")
    path_value = _reference_value(reference, "path")
    expected_path = f"map-assets-v{manifest.schema_version}/{manifest.content_sha256}/manifest.json"
    if not isinstance(path_value, str) or path_value != expected_path:
        raise MapAssetValidationError("map asset reference path is not the safe content-addressed path")
    if PurePosixPath(path_value).parts != tuple(expected_path.split("/")):
        raise MapAssetValidationError("map asset reference path is not the safe content-addressed path")
    if tuple(manifest_path.parts[-3:]) != (
        f"map-assets-v{manifest.schema_version}",
        manifest.content_sha256,
        "manifest.json",
    ):
        raise MapAssetValidationError("map manifest path differs from its content-addressed reference")


def _read_member(
    asset_dir: Path, name: str, metadata: MemberMetadata, directory_guard: _DirectoryGuard
) -> bytes:
    path = asset_dir / name
    compressed = _read_bounded_file(
        path,
        MAX_MEMBER_BYTES,
        name,
        directory_guard,
        exact_size=metadata.compressed_size,
    )
    if hashlib.sha256(compressed).hexdigest() != metadata.compressed_sha256:
        raise MapAssetValidationError(f"{name} compressed sha256 differs from manifest")

    decompressor = zlib.decompressobj()
    raw = bytearray()
    try:
        cursor = 0
        while cursor < len(compressed):
            chunk = compressed[cursor : cursor + _READ_CHUNK_BYTES]
            cursor += len(chunk)
            pending = chunk
            while pending:
                remaining = metadata.uncompressed_size - len(raw)
                output = decompressor.decompress(pending, remaining + 1)
                raw.extend(output)
                if len(raw) > metadata.uncompressed_size:
                    raise MapAssetValidationError(f"{name} has an overlong zlib output")
                pending = decompressor.unconsumed_tail
                if decompressor.unused_data:
                    raise MapAssetValidationError(f"{name} has trailing compressed data")
                if decompressor.eof:
                    if pending or cursor != len(compressed):
                        raise MapAssetValidationError(f"{name} has trailing compressed data")
                    break
                if pending and remaining == 0:
                    raise MapAssetValidationError(f"{name} has an overlong zlib output")
    except zlib.error as error:
        raise MapAssetValidationError(f"{name} has an invalid zlib stream: {error}") from error
    if decompressor.unused_data or decompressor.unconsumed_tail:
        raise MapAssetValidationError(f"{name} has trailing or overlong compressed data")
    if not decompressor.eof:
        raise MapAssetValidationError(f"{name} contains a truncated zlib stream")
    if len(raw) != metadata.uncompressed_size:
        raise MapAssetValidationError(f"{name} uncompressed size differs from manifest")
    if hashlib.sha256(raw).hexdigest() != metadata.uncompressed_sha256:
        raise MapAssetValidationError(f"{name} uncompressed sha256 differs from manifest")
    return bytes(raw)


def _validate_feature_integrity(manifest: MapAssetManifest) -> None:
    features = manifest.features
    slots = [slot for start in features.start_positions for slot in start.slot_indices]
    if len(slots) != len(set(slots)):
        raise MapAssetValidationError("map feature start slots must be unique")
    waypoint_ids = [waypoint.waypoint_id for waypoint in features.waypoints]
    waypoint_names = [waypoint.name for waypoint in features.waypoints]
    if len(waypoint_ids) != len(set(waypoint_ids)):
        raise MapAssetValidationError("map feature waypoint IDs must be unique")
    if manifest.schema_version == 1:
        if len(waypoint_names) != len(set(waypoint_names)):
            raise MapAssetValidationError("v1 map feature waypoint names must be unique")
        known_names = set(waypoint_names)
        for waypoint in features.waypoints:
            if waypoint.link_waypoint_ids is not None:
                raise MapAssetValidationError("v1 map feature waypoint cannot contain link waypoint IDs")
            if len(waypoint.link_names) != len(set(waypoint.link_names)):
                raise MapAssetValidationError("v1 map feature waypoint links must be unique")
            if any(link not in known_names for link in waypoint.link_names):
                raise MapAssetValidationError("v1 map feature waypoint link targets an unknown unique name")
        return _validate_non_waypoint_feature_integrity(manifest)
    known_waypoints = {waypoint.waypoint_id: waypoint for waypoint in features.waypoints}
    for waypoint in features.waypoints:
        if waypoint.link_waypoint_ids is None:
            raise MapAssetValidationError("v2 map feature waypoint requires link waypoint IDs")
        if len(waypoint.link_names) != len(waypoint.link_waypoint_ids):
            raise MapAssetValidationError("map feature waypoint link names and IDs must have equal length")
        if waypoint.link_waypoint_ids != sorted(set(waypoint.link_waypoint_ids)):
            raise MapAssetValidationError("map feature waypoint link IDs must be uniquely ordered")
        if waypoint.waypoint_id in waypoint.link_waypoint_ids:
            raise MapAssetValidationError("map feature waypoint cannot link to itself")
        for link_name, link_id in zip(waypoint.link_names, waypoint.link_waypoint_ids, strict=True):
            target = known_waypoints.get(link_id)
            if target is None:
                raise MapAssetValidationError("map feature waypoint link targets an unknown ID")
            if link_name != target.name:
                raise MapAssetValidationError("map feature waypoint link name disagrees with its target ID")
    _validate_non_waypoint_feature_integrity(manifest)


def _validate_non_waypoint_feature_integrity(manifest: MapAssetManifest) -> None:
    features = manifest.features
    bridge_indices = [bridge.bridge_index for bridge in features.bridges]
    if bridge_indices != sorted(bridge_indices) or len(bridge_indices) != len(set(bridge_indices)):
        raise MapAssetValidationError("map feature bridge indices must be uniquely ordered")
    bridge_object_ids = [bridge.object_id for bridge in features.bridges if bridge.object_id is not None]
    if len(bridge_object_ids) != len(set(bridge_object_ids)):
        raise MapAssetValidationError("map feature bridge object IDs must be unique")
    static_ids = [entry.object_id for entry in features.static_objects]
    if static_ids != sorted(static_ids) or len(static_ids) != len(set(static_ids)):
        raise MapAssetValidationError("map feature static object IDs must be uniquely ordered")
    for entry in features.static_objects:
        category_names = [category.name for category in entry.categories]
        if len(category_names) != len(set(category_names)):
            raise MapAssetValidationError("map feature static object categories must be unique")
    static_by_id = {entry.object_id: entry for entry in features.static_objects}
    for bridge in features.bridges:
        if bridge.object_id is None or bridge.object_id not in static_by_id:
            continue
        static = static_by_id[bridge.object_id]
        if (
            bridge.template_name is not None
            and bridge.template_name != static.template_name
        ) or "bridge" not in {c.name for c in static.categories}:
            raise MapAssetValidationError("cross-category bridge/static object identity is inconsistent")


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
    trusted_trace_directory: Path | None = None,
) -> MapAsset:
    """Validate an entire map directory atomically, then expose typed immutable values."""
    manifest_path = Path(manifest_path)
    asset_dir = manifest_path.parent
    directory_guard = _validate_contained_asset_path(manifest_path, trusted_trace_directory)
    try:
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
    _revalidate_directory_guard(directory_guard)

    manifest_bytes = _read_bounded_file(
        manifest_path, MAX_MANIFEST_BYTES, "map manifest", directory_guard
    )
    try:
        manifest_text = manifest_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MapAssetValidationError(f"map manifest contains invalid UTF-8: {error}") from error
    try:
        parsed = json.loads(
            manifest_text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise MapAssetValidationError(f"map manifest contains invalid JSON: {error}") from error
    _require_canonical_json(manifest_bytes, parsed)
    version = parsed.get("schema_version") if isinstance(parsed, dict) else None
    if type(version) is not int or version not in _SCHEMA_VALIDATORS:
        raise MapAssetValidationError(f"map manifest has unsupported schema_version: {version!r}")
    schema_errors = sorted(_SCHEMA_VALIDATORS[version].iter_errors(parsed), key=lambda issue: list(issue.absolute_path))
    if schema_errors:
        schema_issue = schema_errors[0]
        location = ".".join(str(part) for part in schema_issue.absolute_path) or "root"
        raise MapAssetValidationError(f"map manifest schema path {location}: {schema_issue.message}")
    try:
        manifest = MapAssetManifest.model_validate(parsed)
    except ValidationError as error:
        raise MapAssetValidationError(f"map manifest typed validation failed: {error.errors()[0]['msg']}") from error

    if asset_dir.name != manifest.content_sha256 or asset_dir.parent.name != f"map-assets-v{manifest.schema_version}":
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

    raw_members = {
        name: _read_member(asset_dir, name, manifest.members[name], directory_guard)
        for name in sorted(MEMBER_NAMES)
    }
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
    _validate_feature_integrity(manifest)

    # Construct only after all schema, identity, filesystem, decompression, grid, and feature checks succeed.
    return MapAsset(
        schema_version=manifest.schema_version,
        content_sha256=manifest.content_sha256,
        engine_data_identity=manifest.engine_data_identity,
        map_identity=manifest.map_identity,
        declared_position_policies=frozenset(
            [*manifest.coordinate_system.entity_sample_policy.bounded_position_policies,
             *manifest.coordinate_system.entity_sample_policy.exempt_position_policies]
        ),
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
