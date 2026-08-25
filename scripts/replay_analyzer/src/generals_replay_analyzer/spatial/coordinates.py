"""Non-mutating world, grid, normalized, and player-centric coordinate transforms."""

from __future__ import annotations

import math
from dataclasses import dataclass

from generals_replay_analyzer.spatial.assets import GridSpec, Position3, SpatialUnavailable, StartPosition, WorldBounds


@dataclass(frozen=True)
class NormalizedXY:
    u: float
    v: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.u) or not math.isfinite(self.v) or not 0.0 <= self.u <= 1.0 or not 0.0 <= self.v <= 1.0:
            raise ValueError("normalized coordinates must be finite values in the closed unit interval")


@dataclass(frozen=True, order=True)
class GridCell:
    x: int
    y: int

    def __post_init__(self) -> None:
        if type(self.x) is not int or type(self.y) is not int:
            raise ValueError("grid cell coordinates must be integers")


@dataclass(frozen=True)
class PlayerTransform:
    own_start: StartPosition
    enemy_start: StartPosition
    angle_radians: float
    cosine: float
    sine: float
    transform_version: str = "player-centric-v1"

    def apply(self, position: Position3) -> Position3:
        dx = position.x - self.own_start.position.x
        dy = position.y - self.own_start.position.y
        return Position3(
            dx * self.cosine - dy * self.sine,
            dx * self.sine + dy * self.cosine,
            position.z - self.own_start.position.z,
        )

    def inverse(self, position: Position3) -> Position3:
        return Position3(
            position.x * self.cosine + position.y * self.sine + self.own_start.position.x,
            -position.x * self.sine + position.y * self.cosine + self.own_start.position.y,
            position.z + self.own_start.position.z,
        )


def world_to_map_normalized(position: Position3, bounds: WorldBounds) -> NormalizedXY | SpatialUnavailable:
    width = bounds.maximum.x - bounds.minimum.x
    height = bounds.maximum.y - bounds.minimum.y
    if width <= 0 or height <= 0:
        return SpatialUnavailable("invalid_world_bounds")
    if not (
        bounds.minimum.x <= position.x <= bounds.maximum.x
        and bounds.minimum.y <= position.y <= bounds.maximum.y
    ):
        return SpatialUnavailable("map_coordinate_out_of_bounds")
    return NormalizedXY((position.x - bounds.minimum.x) / width, (position.y - bounds.minimum.y) / height)


def map_normalized_to_world(
    point: NormalizedXY | SpatialUnavailable, bounds: WorldBounds, z: float
) -> Position3 | SpatialUnavailable:
    width = bounds.maximum.x - bounds.minimum.x
    height = bounds.maximum.y - bounds.minimum.y
    if isinstance(point, SpatialUnavailable):
        return point
    if width <= 0 or height <= 0:
        return SpatialUnavailable("invalid_world_bounds")
    try:
        return Position3(bounds.minimum.x + point.u * width, bounds.minimum.y + point.v * height, z)
    except ValueError:
        return SpatialUnavailable("map_coordinate_out_of_bounds")


def world_to_grid_cell(position: Position3, grid: GridSpec) -> GridCell | SpatialUnavailable:
    if not (
        grid.minimum_x <= position.x < grid.maximum_x
        and grid.minimum_y <= position.y < grid.maximum_y
    ):
        return SpatialUnavailable("grid_coordinate_out_of_bounds")
    x = math.floor((position.x - grid.minimum_x) / grid.cell_size_x)
    y = math.floor((position.y - grid.minimum_y) / grid.cell_size_y)
    if not 0 <= x < grid.width or not 0 <= y < grid.height:
        return SpatialUnavailable("grid_coordinate_out_of_bounds")
    return GridCell(x, y)


def grid_cell_center(cell: GridCell, grid: GridSpec, z: float) -> Position3:
    grid.row_major_index(cell.x, cell.y)
    return Position3(
        grid.minimum_x + (cell.x + 0.5) * grid.cell_size_x,
        grid.minimum_y + (cell.y + 0.5) * grid.cell_size_y,
        z,
    )


# TheSuperHackers @feature Leex 22/08/2026 Define a versioned affine view without modifying raw map coordinates. (#0)
def player_centric_transform(
    own_start: StartPosition, enemy_starts: tuple[StartPosition, ...]
) -> PlayerTransform | SpatialUnavailable:
    candidates: list[tuple[float, int, str, StartPosition, float, float]] = []
    for enemy in enemy_starts:
        dx = enemy.position.x - own_start.position.x
        dy = enemy.position.y - own_start.position.y
        squared_distance = dx * dx + dy * dy
        if math.isfinite(squared_distance):
            candidates.append((squared_distance, enemy.waypoint_id, enemy.name, enemy, dx, dy))
    if not candidates:
        return SpatialUnavailable("unresolved_player_transform")
    squared_distance, _, _, enemy, dx, dy = min(candidates, key=lambda item: item[:3])
    if squared_distance <= 0:
        return SpatialUnavailable("unresolved_player_transform")
    angle = -math.atan2(dy, dx)
    # TheSuperHackers @bugfix Leex 25/08/2026 Normalize derived signed zero angles before immutable scene canonicalization. (#TBD)
    if angle == 0.0:
        angle = 0.0
    cosine = math.cos(angle)
    sine = math.sin(angle)
    if not all(math.isfinite(value) for value in (angle, cosine, sine)):
        return SpatialUnavailable("unresolved_player_transform")
    return PlayerTransform(own_start, enemy, angle, cosine, sine)


__all__ = [
    "GridCell",
    "NormalizedXY",
    "PlayerTransform",
    "SpatialUnavailable",
    "grid_cell_center",
    "map_normalized_to_world",
    "player_centric_transform",
    "world_to_grid_cell",
    "world_to_map_normalized",
]
