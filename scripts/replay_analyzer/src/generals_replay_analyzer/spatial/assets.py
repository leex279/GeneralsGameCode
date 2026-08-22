"""Immutable semantic projections for already validated map and spatial evidence."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal, TypeAlias

from generals_replay_analyzer.features.evidence import EvidenceRef

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_GRID_ELEMENTS = 16 * 1024 * 1024
_MAX_GRID_DIMENSION = 16_384
_MAX_ZONE_ID = 16_383

LocomotorSurface: TypeAlias = Literal["ground", "amphibious"]

_CATEGORY_SOURCES = {
    "static_blocker": "ThingTemplate::isKindOf(KINDOF_OBSTACLE)",
    "supply_source": "ThingTemplate::isKindOf(KINDOF_SUPPLY_SOURCE)",
    "supply_warehouse": "Object::findUpdateModule(SupplyWarehouseDockUpdate)",
    "capturable": "ThingTemplate::isKindOf(KINDOF_CAPTURABLE)",
    "tech_building": "ThingTemplate::isKindOf(KINDOF_TECH_BUILDING)",
    "cash_generator": "ThingTemplate::isKindOf(KINDOF_CASH_GENERATOR)",
    "oil_income": "Object::findUpdateModule(AutoDepositUpdate)+capturable_or_tech_KindOf",
    "bridge": "ThingTemplate::isKindOf(KINDOF_BRIDGE)",
}


def _require_float(value: float, label: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    if value == 0.0 and math.copysign(1.0, value) < 0:
        raise ValueError(f"{label} may not be negative zero")


def _require_nonempty(value: str, label: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a nonempty built-in string")


@dataclass(frozen=True)
class SpatialUnavailable:
    reason: str
    details: tuple[tuple[str, object], ...] = ()

    def __post_init__(self) -> None:
        _require_nonempty(self.reason, "unavailable reason")
        object.__setattr__(self, "details", tuple(sorted(self.details, key=lambda item: item[0])))


SpatialProjectionUnavailable = SpatialUnavailable


@dataclass(frozen=True)
class Position3:
    x: float
    y: float
    z: float

    def __post_init__(self) -> None:
        for value, label in ((self.x, "x"), (self.y, "y"), (self.z, "z")):
            _require_float(value, label)


@dataclass(frozen=True)
class WorldBounds:
    minimum: Position3
    maximum: Position3

    def __post_init__(self) -> None:
        if (
            self.minimum.x > self.maximum.x
            or self.minimum.y > self.maximum.y
            or self.minimum.z > self.maximum.z
        ):
            raise ValueError("world bounds minimum exceeds maximum")


@dataclass(frozen=True)
class GridSpec:
    width: int
    height: int
    index_origin_x: int
    index_origin_y: int
    cell_size_x: float
    cell_size_y: float
    minimum_x: float
    minimum_y: float
    maximum_x: float
    maximum_y: float

    def __post_init__(self) -> None:
        if (
            type(self.width) is not int
            or type(self.height) is not int
            or not 1 <= self.width <= _MAX_GRID_DIMENSION
            or not 1 <= self.height <= _MAX_GRID_DIMENSION
            or self.width * self.height > _MAX_GRID_ELEMENTS
        ):
            raise ValueError("grid dimensions exceed the closed bounds")
        if type(self.index_origin_x) is not int or type(self.index_origin_y) is not int:
            raise ValueError("grid index origins must be integers")
        for value, label in (
            (self.cell_size_x, "cell_size_x"),
            (self.cell_size_y, "cell_size_y"),
            (self.minimum_x, "minimum_x"),
            (self.minimum_y, "minimum_y"),
            (self.maximum_x, "maximum_x"),
            (self.maximum_y, "maximum_y"),
        ):
            _require_float(value, label)
        if self.cell_size_x <= 0 or self.cell_size_y <= 0:
            raise ValueError("grid cell sizes must be positive")
        expected_min_x = self.index_origin_x * self.cell_size_x
        expected_min_y = self.index_origin_y * self.cell_size_y
        expected_max_x = (self.index_origin_x + self.width) * self.cell_size_x
        expected_max_y = (self.index_origin_y + self.height) * self.cell_size_y
        if (
            self.minimum_x != expected_min_x
            or self.minimum_y != expected_min_y
            or self.maximum_x != expected_max_x
            or self.maximum_y != expected_max_y
        ):
            raise ValueError("grid origin, dimensions, cell size, and bounds disagree")

    def row_major_index(self, x: int, y: int) -> int:
        if not 0 <= x < self.width or not 0 <= y < self.height:
            raise IndexError("grid cell is outside the grid")
        return y * self.width + x


@dataclass(frozen=True)
class StartPosition:
    name: str
    waypoint_id: int
    slot_indices: tuple[int, ...]
    position: Position3
    evidence: EvidenceRef

    def __post_init__(self) -> None:
        _require_nonempty(self.name, "start-position name")
        if type(self.waypoint_id) is not int or self.waypoint_id < 0:
            raise ValueError("waypoint_id must be a nonnegative integer")
        slots = tuple(sorted(self.slot_indices))
        if not slots or len(slots) != len(set(slots)) or any(type(slot) is not int or not 0 <= slot <= 7 for slot in slots):
            raise ValueError("slot indices must be unique values from zero through seven")
        if self.evidence.tier != "observed":
            raise ValueError("start-position evidence must be observed")
        object.__setattr__(self, "slot_indices", slots)


@dataclass(frozen=True)
class StaticObjectCategory:
    name: str
    source: str

    def __post_init__(self) -> None:
        if self.name not in _CATEGORY_SOURCES or self.source != _CATEGORY_SOURCES[self.name]:
            raise ValueError("static-object category source is not the closed source-grounded mapping")


@dataclass(frozen=True)
class StaticObjectFeature:
    object_id: int
    template_name: str
    position: Position3
    categories: tuple[StaticObjectCategory, ...]
    evidence: EvidenceRef

    def __post_init__(self) -> None:
        if type(self.object_id) is not int or self.object_id <= 0:
            raise ValueError("static object ID must be positive")
        _require_nonempty(self.template_name, "static object template")
        categories = tuple(sorted(self.categories, key=lambda category: category.name))
        if not categories or len({category.name for category in categories}) != len(categories):
            raise ValueError("static object categories must be nonempty and unique")
        if self.evidence.tier != "observed":
            raise ValueError("static-object evidence must be observed")
        object.__setattr__(self, "categories", categories)


@dataclass(frozen=True)
class SpatialMapProjection:
    schema_version: int
    content_sha256: str
    map_identity: str
    engine_data_identity: str
    pathing: GridSpec
    world_bounds: WorldBounds
    ground_passable: tuple[bool, ...]
    amphibious_passable: tuple[bool, ...]
    zone_ids: tuple[int, ...]
    start_positions: tuple[StartPosition, ...]
    static_objects: tuple[StaticObjectFeature, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "ground_passable", tuple(self.ground_passable))
        object.__setattr__(self, "amphibious_passable", tuple(self.amphibious_passable))
        object.__setattr__(self, "zone_ids", tuple(self.zone_ids))
        object.__setattr__(
            self,
            "start_positions",
            tuple(sorted(self.start_positions, key=lambda start: (start.waypoint_id, start.name))),
        )
        object.__setattr__(self, "static_objects", tuple(sorted(self.static_objects, key=lambda item: item.object_id)))


@dataclass(frozen=True)
class SpatialSample:
    evidence: EvidenceRef
    frame: int
    object_key: str
    owner_scope_key: str | None
    position: Position3
    position_bounds_policy: str
    is_mobile: bool
    is_structure: bool
    is_disabled: bool
    is_engine_moving: bool
    locomotor_surface: LocomotorSurface | None
    path_goal: Position3 | None

    def __post_init__(self) -> None:
        if self.evidence.tier != "observed":
            raise ValueError("spatial sample evidence must be observed")
        if type(self.frame) is not int or self.frame < 0:
            raise ValueError("spatial sample frame must be nonnegative")
        _require_nonempty(self.object_key, "object key")
        if self.owner_scope_key is not None:
            _require_nonempty(self.owner_scope_key, "owner scope key")
        _require_nonempty(self.position_bounds_policy, "position bounds policy")
        if any(type(value) is not bool for value in (
            self.is_mobile,
            self.is_structure,
            self.is_disabled,
            self.is_engine_moving,
        )):
            raise ValueError("spatial sample flags must be exact booleans")
        if self.locomotor_surface not in (None, "ground", "amphibious"):
            raise ValueError("unsupported locomotor surface")


@dataclass(frozen=True)
class SpatialCombatObservation:
    evidence: EvidenceRef
    frame: int
    location: Position3
    attacker_scope_key: str | None
    victim_scope_key: str | None
    applied_amount: float
    killing_blow: bool
    attacker_scope_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.evidence.tier != "observed":
            raise ValueError("combat observation evidence must be observed")
        if type(self.frame) is not int or self.frame < 0:
            raise ValueError("combat observation frame must be nonnegative")
        if type(self.attacker_scope_keys) is not tuple:
            raise ValueError("attacker scope keys must be an immutable tuple")
        attackers = set(self.attacker_scope_keys)
        if self.attacker_scope_key is not None:
            attackers.add(self.attacker_scope_key)
        for scope in (*attackers, self.victim_scope_key):
            if scope is not None:
                _require_nonempty(scope, "participant scope key")
        object.__setattr__(self, "attacker_scope_keys", tuple(sorted(attackers)))
        _require_float(self.applied_amount, "applied amount")
        if self.applied_amount < 0:
            raise ValueError("applied amount must be nonnegative")
        if type(self.killing_blow) is not bool:
            raise ValueError("killing blow must be an exact boolean")


def _in_closed_world(position: Position3, bounds: WorldBounds) -> bool:
    return (
        bounds.minimum.x <= position.x <= bounds.maximum.x
        and bounds.minimum.y <= position.y <= bounds.maximum.y
        and bounds.minimum.z <= position.z <= bounds.maximum.z
    )


# TheSuperHackers @feature Leex 22/08/2026 Reject inconsistent semantic projections without reloading map files. (#0)
def validate_map_projection(projection: SpatialMapProjection) -> SpatialMapProjection | SpatialProjectionUnavailable:
    count = projection.pathing.width * projection.pathing.height
    invalid = (
        type(projection.schema_version) is not int
        or projection.schema_version not in (1, 2)
        or not _SHA256.fullmatch(projection.content_sha256)
        or type(projection.map_identity) is not str
        or not projection.map_identity.strip()
        or type(projection.engine_data_identity) is not str
        or not projection.engine_data_identity.strip()
        or len(projection.ground_passable) != count
        or len(projection.amphibious_passable) != count
        or len(projection.zone_ids) != count
        or any(type(flag) is not bool for flag in projection.ground_passable + projection.amphibious_passable)
        or any(ground and not amphibious for ground, amphibious in zip(
            projection.ground_passable, projection.amphibious_passable, strict=True
        ))
        or any(type(zone) is not int or not 0 <= zone <= _MAX_ZONE_ID for zone in projection.zone_ids)
        or projection.pathing.minimum_x < projection.world_bounds.minimum.x
        or projection.pathing.minimum_y < projection.world_bounds.minimum.y
        or projection.pathing.maximum_x > projection.world_bounds.maximum.x
        or projection.pathing.maximum_y > projection.world_bounds.maximum.y
    )
    starts = projection.start_positions
    objects = projection.static_objects
    all_slots = tuple(slot for start in starts for slot in start.slot_indices)
    if (
        invalid
        or len({(start.waypoint_id, start.name) for start in starts}) != len(starts)
        or len(set(all_slots)) != len(all_slots)
        or len({item.object_id for item in objects}) != len(objects)
        or any(not _in_closed_world(start.position, projection.world_bounds) for start in starts)
        or any(not _in_closed_world(item.position, projection.world_bounds) for item in objects)
    ):
        return SpatialUnavailable("invalid_validated_map_projection")
    return projection
