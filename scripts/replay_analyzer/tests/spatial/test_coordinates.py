"""Coordinate conversion and player-centric transform tests."""

import math
from dataclasses import replace

import pytest

from generals_replay_analyzer.spatial.assets import GridSpec, Position3, StartPosition, WorldBounds
from generals_replay_analyzer.spatial.coordinates import (
    GridCell,
    NormalizedXY,
    SpatialUnavailable,
    grid_cell_center,
    map_normalized_to_world,
    player_centric_transform,
    world_to_grid_cell,
    world_to_map_normalized,
)


def test_raw_normalized_round_trip_preserves_unrounded_position() -> None:
    bounds = WorldBounds(Position3(-100.0, 20.0, -5.0), Position3(300.0, 220.0, 50.0))
    raw = Position3(13.25, 111.125, 7.75)

    normalized = world_to_map_normalized(raw, bounds)
    assert normalized == NormalizedXY(0.283125, 0.455625)
    assert map_normalized_to_world(normalized, bounds, raw.z) == raw


def test_world_maximum_normalizes_but_is_excluded_from_grid() -> None:
    bounds = WorldBounds(Position3(0.0, 0.0, 0.0), Position3(20.0, 10.0, 1.0))
    grid = GridSpec(2, 1, 0, 0, 10.0, 10.0, 0.0, 0.0, 20.0, 10.0)
    maximum = Position3(20.0, 10.0, 0.0)

    assert world_to_map_normalized(maximum, bounds) == NormalizedXY(1.0, 1.0)
    result = world_to_grid_cell(maximum, grid)
    assert isinstance(result, SpatialUnavailable)
    assert result.reason == "grid_coordinate_out_of_bounds"


def test_grid_conversion_uses_floor_row_major_cells_without_clamping() -> None:
    grid = GridSpec(3, 2, -2, 4, 5.0, 10.0, -10.0, 40.0, 5.0, 60.0)

    assert world_to_grid_cell(Position3(-0.001, 59.999, 3.0), grid) == GridCell(1, 1)
    assert grid_cell_center(GridCell(1, 1), grid, 3.0) == Position3(-2.5, 55.0, 3.0)
    assert isinstance(world_to_grid_cell(Position3(-10.001, 50.0, 0.0), grid), SpatialUnavailable)


@pytest.mark.parametrize(
    "bounds, point",
    [
        (WorldBounds(Position3(0.0, 0.0, 0.0), Position3(0.0, 10.0, 1.0)), Position3(0.0, 2.0, 0.0)),
        (WorldBounds(Position3(0.0, 0.0, 0.0), Position3(10.0, 10.0, 1.0)), Position3(-0.001, 2.0, 0.0)),
        (WorldBounds(Position3(0.0, 0.0, 0.0), Position3(10.0, 10.0, 1.0)), Position3(10.001, 2.0, 0.0)),
    ],
)
def test_normalization_returns_unavailable_for_degenerate_or_oob_values(bounds, point) -> None:
    result = world_to_map_normalized(point, bounds)
    assert isinstance(result, SpatialUnavailable)


@pytest.mark.parametrize("enemy_xy", [(10.0, 0.0), (0.0, 10.0), (-10.0, 0.0), (0.0, -10.0)])
def test_player_transform_rotates_every_cardinal_enemy_direction_to_positive_x(evidence_ref, enemy_xy) -> None:
    own = StartPosition("own", 10, (0,), Position3(0.0, 0.0, 0.0), evidence_ref(1))
    enemy = StartPosition("enemy", 20, (1,), Position3(*enemy_xy, 0.0), evidence_ref(2))

    transform = player_centric_transform(own, (enemy,))

    assert not isinstance(transform, SpatialUnavailable)
    transformed = transform.apply(enemy.position)
    assert transformed.x == pytest.approx(10.0)
    assert transformed.y == pytest.approx(0.0, abs=1e-12)
    assert transform.inverse(transformed) == pytest.approx(enemy.position)


def test_player_transform_is_mirror_invariant_and_uses_deterministic_tie_order(evidence_ref) -> None:
    own = StartPosition("own", 1, (0,), Position3(0.0, 0.0, 0.0), evidence_ref(1))
    north = StartPosition("z-name", 3, (2,), Position3(0.0, 10.0, 0.0), evidence_ref(3))
    east = StartPosition("a-name", 2, (1,), Position3(10.0, 0.0, 0.0), evidence_ref(2))
    mirrored = replace(east, position=Position3(-10.0, 0.0, 0.0))

    selected = player_centric_transform(own, (north, east))
    mirror_transform = player_centric_transform(own, (mirrored,))

    assert not isinstance(selected, SpatialUnavailable)
    assert selected.enemy_start == east
    assert not isinstance(mirror_transform, SpatialUnavailable)
    assert selected.apply(Position3(4.0, 3.0, 0.0)).x == pytest.approx(
        mirror_transform.apply(Position3(-4.0, -3.0, 0.0)).x
    )


def test_player_transform_canonicalizes_east_facing_zero_angle(evidence_ref) -> None:
    """A signed zero angle would make an otherwise valid map scene fail its canonical boundary."""
    own = StartPosition("own", 1, (0,), Position3(0.0, 0.0, 0.0), evidence_ref(1))
    east = StartPosition("east", 2, (1,), Position3(10.0, 0.0, 0.0), evidence_ref(2))

    transform = player_centric_transform(own, (east,))

    assert not isinstance(transform, SpatialUnavailable)
    assert transform.angle_radians == 0.0
    assert math.copysign(1.0, transform.angle_radians) == 1.0


def test_player_transform_rejects_missing_or_coincident_enemy(evidence_ref) -> None:
    own = StartPosition("own", 1, (0,), Position3(2.0, 3.0, 0.0), evidence_ref(1))
    coincident = StartPosition("enemy", 2, (1,), own.position, evidence_ref(2))

    assert player_centric_transform(own, ()).reason == "unresolved_player_transform"
    assert player_centric_transform(own, (coincident,)).reason == "unresolved_player_transform"


def test_transform_rejects_nonfinite_result_instead_of_snapping(evidence_ref) -> None:
    own = StartPosition("own", 1, (0,), Position3(1e308, 0.0, 0.0), evidence_ref(1))
    enemy = StartPosition("enemy", 2, (1,), Position3(-1e308, 0.0, 0.0), evidence_ref(2))
    result = player_centric_transform(own, (enemy,))
    assert isinstance(result, SpatialUnavailable)
    assert result.reason == "unresolved_player_transform"


def test_reverse_normalization_and_cell_center_reject_invalid_boundaries() -> None:
    degenerate = WorldBounds(Position3(0.0, 0.0, 0.0), Position3(0.0, 10.0, 1.0))
    assert map_normalized_to_world(NormalizedXY(0.5, 0.5), degenerate, 0.0).reason == "invalid_world_bounds"
    with pytest.raises(ValueError, match="unit interval"):
        NormalizedXY(1.0001, 0.5)
    grid = GridSpec(1, 1, 0, 0, 10.0, 10.0, 0.0, 0.0, 10.0, 10.0)
    with pytest.raises(IndexError, match="outside"):
        grid_cell_center(GridCell(1, 0), grid, 0.0)
