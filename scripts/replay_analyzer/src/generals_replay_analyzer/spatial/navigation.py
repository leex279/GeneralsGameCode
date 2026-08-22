"""Deterministic four-neighbor routing over validated semantic pathing grids."""

from __future__ import annotations

import heapq
from collections import OrderedDict
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal, TypeAlias

from generals_replay_analyzer.features.evidence import EvidenceRef, evidence_sort_key
from generals_replay_analyzer.spatial.assets import (
    GridSpec,
    LocomotorSurface,
    SpatialMapProjection,
    SpatialSample,
    SpatialUnavailable,
    validate_map_projection,
)
from generals_replay_analyzer.spatial.coordinates import GridCell, world_to_grid_cell

GRID_ROUTE_VERSION = "grid-route-v1"
_NEIGHBORS = ((0, -1), (1, 0), (0, 1), (-1, 0))
RouteQuality: TypeAlias = Literal["complete", "partial", "unavailable"]


@dataclass(frozen=True)
class NavigationMap:
    content_sha256: str
    schema_version: int
    surface: LocomotorSurface
    algorithm_version: str
    grid: GridSpec
    passable: tuple[bool, ...]
    zone_ids: tuple[int, ...]


@dataclass(frozen=True)
class GridRoute:
    cells: tuple[GridCell, ...]
    distance_cells: int
    distance_world: float
    surface: LocomotorSurface
    algorithm_version: str


@dataclass(frozen=True)
class MovementSegment:
    object_key: str
    start_frame: int
    end_frame: int
    route: GridRoute
    evidence: tuple[EvidenceRef, EvidenceRef]


@dataclass(frozen=True)
class RouteOmission:
    object_key: str
    start_frame: int
    end_frame: int
    evidence: tuple[EvidenceRef, ...]
    reason: str


@dataclass(frozen=True)
class MovementRoutes:
    segments: tuple[MovementSegment, ...]
    omissions: tuple[RouteOmission, ...]
    quality: RouteQuality
    reason: str | None


class NavigationCache:
    """Explicit bounded process-local cache; never part of semantic identity."""

    def __init__(self, max_entries: int) -> None:
        if type(max_entries) is not int or max_entries <= 0:
            raise ValueError("navigation cache maximum must be positive")
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[str, int, str, str], NavigationMap] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: tuple[str, int, str, str]) -> NavigationMap | None:
        return self._entries.get(key)

    def put(self, key: tuple[str, int, str, str], navigation: NavigationMap) -> None:
        self._entries[key] = navigation
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


# TheSuperHackers @feature Leex 22/08/2026 Route only on an explicitly selected validated locomotor surface. (#0)
def build_navigation_map(
    projection: SpatialMapProjection,
    surface: object,
    *,
    cache: NavigationCache | None = None,
) -> NavigationMap | SpatialUnavailable:
    if surface not in ("ground", "amphibious"):
        return SpatialUnavailable("unsupported_or_unproven_locomotor_surface")
    checked = validate_map_projection(projection)
    if isinstance(checked, SpatialUnavailable):
        return checked
    typed_surface: LocomotorSurface = surface
    key = (projection.content_sha256, projection.schema_version, typed_surface, GRID_ROUTE_VERSION)
    cached = cache.get(key) if cache is not None else None
    if cached is not None:
        return cached
    flags = projection.ground_passable if typed_surface == "ground" else projection.amphibious_passable
    navigation = NavigationMap(
        projection.content_sha256,
        projection.schema_version,
        typed_surface,
        GRID_ROUTE_VERSION,
        projection.pathing,
        flags,
        projection.zone_ids,
    )
    if cache is not None:
        cache.put(key, navigation)
    return navigation


def _is_inside(cell: GridCell, grid: GridSpec) -> bool:
    return 0 <= cell.x < grid.width and 0 <= cell.y < grid.height


def _heuristic(left: GridCell, right: GridCell) -> int:
    return abs(left.x - right.x) + abs(left.y - right.y)


def _world_distance(cells: tuple[GridCell, ...], grid: GridSpec) -> float:
    total = 0.0
    for left, right in pairwise(cells):
        total += grid.cell_size_x if left.x != right.x else grid.cell_size_y
    return total


def route_between(
    navigation: NavigationMap | SpatialUnavailable, start: GridCell, end: GridCell
) -> GridRoute | SpatialUnavailable:
    if isinstance(navigation, SpatialUnavailable):
        return navigation
    grid = navigation.grid
    if not _is_inside(start, grid) or not _is_inside(end, grid):
        return SpatialUnavailable("grid_coordinate_out_of_bounds")
    start_index = grid.row_major_index(start.x, start.y)
    end_index = grid.row_major_index(end.x, end.y)
    if not navigation.passable[start_index] or not navigation.passable[end_index]:
        return SpatialUnavailable("unreachable_validated_grid_path")
    start_zone = navigation.zone_ids[start_index]
    end_zone = navigation.zone_ids[end_index]
    if start_zone != 0 and end_zone != 0 and start_zone != end_zone:
        return SpatialUnavailable("unreachable_validated_grid_path")
    validated_zone = start_zone if start_zone != 0 else end_zone

    frontier: list[tuple[int, int, int, int, int, GridCell]] = []
    rank = 0
    heapq.heappush(frontier, (_heuristic(start, end), 0, start.y, start.x, rank, start))
    cost = {start: 0}
    predecessor: dict[GridCell, GridCell] = {}
    closed: set[GridCell] = set()
    while frontier:
        _, current_cost, _, _, _, current = heapq.heappop(frontier)
        if current in closed or current_cost != cost[current]:
            continue
        if current == end:
            break
        closed.add(current)
        for dx, dy in _NEIGHBORS:
            neighbor = GridCell(current.x + dx, current.y + dy)
            if not _is_inside(neighbor, grid):
                continue
            index = grid.row_major_index(neighbor.x, neighbor.y)
            if not navigation.passable[index]:
                continue
            neighbor_zone = navigation.zone_ids[index]
            if neighbor_zone != 0 and neighbor_zone != validated_zone:
                continue
            next_cost = current_cost + 1
            previous_cost = cost.get(neighbor)
            previous_predecessor = predecessor.get(neighbor)
            if previous_cost is not None and (
                next_cost > previous_cost
                or (
                    next_cost == previous_cost
                    and previous_predecessor is not None
                    and (current.y, current.x) >= (previous_predecessor.y, previous_predecessor.x)
                )
            ):
                continue
            cost[neighbor] = next_cost
            predecessor[neighbor] = current
            rank += 1
            heapq.heappush(
                frontier,
                (
                    next_cost + _heuristic(neighbor, end),
                    next_cost,
                    neighbor.y,
                    neighbor.x,
                    rank,
                    neighbor,
                ),
            )
    if end not in cost:
        return SpatialUnavailable("unreachable_validated_grid_path")
    cells = [end]
    while cells[-1] != start:
        cells.append(predecessor[cells[-1]])
    stable_cells = tuple(reversed(cells))
    return GridRoute(
        stable_cells,
        len(stable_cells) - 1,
        _world_distance(stable_cells, grid),
        navigation.surface,
        navigation.algorithm_version,
    )


def _sample_key(sample: SpatialSample) -> tuple[int, str, str, str]:
    return (sample.frame, sample.evidence.source_kind, sample.evidence.source_key, sample.evidence.public_id)


def _eligible_entity(sample: SpatialSample) -> bool:
    return (
        sample.owner_scope_key is not None
        and sample.is_mobile
        and not sample.is_structure
        and not sample.is_disabled
    )


def route_sample_segments(
    projection: SpatialMapProjection,
    samples: tuple[SpatialSample, ...],
    *,
    max_sample_gap_frames: int = 300,
    cache: NavigationCache | None = None,
) -> MovementRoutes:
    if type(max_sample_gap_frames) is not int or max_sample_gap_frames <= 0:
        raise ValueError("maximum sample gap must be a positive integer")
    segments: list[MovementSegment] = []
    omissions: list[RouteOmission] = []
    groups: dict[str, list[SpatialSample]] = {}
    for sample in sorted(samples, key=_sample_key):
        if not _eligible_entity(sample):
            continue
        if sample.position_bounds_policy != "pathfinder_xy_closed":
            omissions.append(
                RouteOmission(
                    sample.object_key,
                    sample.frame,
                    sample.frame,
                    (sample.evidence,),
                    "position_exempt_spatial_sample",
                )
            )
            continue
        groups.setdefault(sample.object_key, []).append(sample)
    for object_key in sorted(groups):
        ordered = sorted(groups[object_key], key=_sample_key)
        for start, end in pairwise(ordered):
            evidence = (start.evidence, end.evidence)
            reason: str | None = None
            if end.frame <= start.frame:
                reason = "non_increasing_sample_frames"
            elif end.frame - start.frame > max_sample_gap_frames:
                reason = "sample_gap_exceeds_policy"
            elif start.locomotor_surface is None or start.locomotor_surface != end.locomotor_surface:
                reason = "unsupported_or_unproven_locomotor_surface"
            if reason is None:
                start_cell = world_to_grid_cell(start.position, projection.pathing)
                end_cell = world_to_grid_cell(end.position, projection.pathing)
                if isinstance(start_cell, SpatialUnavailable) or isinstance(end_cell, SpatialUnavailable):
                    reason = "grid_coordinate_out_of_bounds"
                else:
                    navigation = build_navigation_map(projection, start.locomotor_surface, cache=cache)
                    route = route_between(navigation, start_cell, end_cell)
                    if isinstance(route, SpatialUnavailable):
                        reason = route.reason
                    else:
                        segments.append(MovementSegment(object_key, start.frame, end.frame, route, evidence))
            if reason is not None:
                omissions.append(RouteOmission(object_key, start.frame, end.frame, evidence, reason))
    segments.sort(key=lambda item: (item.object_key, item.start_frame, tuple(map(evidence_sort_key, item.evidence))))
    omissions.sort(key=lambda item: (item.object_key, item.start_frame, tuple(map(evidence_sort_key, item.evidence))))
    if segments and omissions:
        return MovementRoutes(tuple(segments), tuple(omissions), "partial", omissions[0].reason)
    if segments:
        return MovementRoutes(tuple(segments), (), "complete", None)
    reason = omissions[0].reason if omissions else "no_eligible_route_segments"
    return MovementRoutes((), tuple(omissions), "unavailable", reason)
