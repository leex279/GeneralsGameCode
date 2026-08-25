"""Spatial plugin composition and pure feature extraction tests."""

import random
from dataclasses import replace

import pytest

from generals_replay_analyzer.features.base import FeatureScope
from generals_replay_analyzer.features.context import FeatureContext, cache_key, canonical_json, input_digest
from generals_replay_analyzer.features.evidence import EvidenceRef, ObservedEvidence, thaw_canonical
from generals_replay_analyzer.features.registry import BASE_REGISTRY
from generals_replay_analyzer.spatial.features import (
    SPATIAL_FEATURE_NAMES,
    SpatialFeatureExtractor,
    SpatialFeaturePlugin,
)
from generals_replay_analyzer.spatial.statistics import derive_bootstrap_seed

PLAYER_NAMES = (
    "army_route.observed_reachable_distance",
    "army_route.observed_route_segment_count",
    "expansion.completed_structure_positions",
    "expansion.forward_completed_structure_count",
    "movement_density.sample_count_heatmap",
    "movement_density.sample_count_heatmap_bootstrap_interval",
    "resource_control.observed_supply_collected_amount",
    "resource_control.observed_supply_collection_share",
)
REPLAY_NAMES = (
    "engagement.observed_cluster_count",
    "engagement.zone_clusters",
    "map_control.observed_cell_presence_share",
)


def _spatial_settings(*, resamples: int = 32) -> dict[str, object]:
    return {
        "algorithm_schema": "spatial-algorithm-settings-v1",
        "bootstrap_algorithm_version": "pcg64-bootstrap-v1",
        "bootstrap_resamples": resamples,
        "engagement_algorithm_version": "engagement-cluster-v1",
        "engagement_gap_frames": 90,
        "engagement_reachable_radius_cells": 12,
        "heatmap_algorithm_version": "sample-count-heatmap-v1",
        "max_sample_gap_frames": 300,
        "route_algorithm_version": "grid-route-v1",
    }


def _ref(sequence: int) -> EvidenceRef:
    return EvidenceRef(
        f"00000000-0000-4000-8000-{sequence:012d}",
        "observed",
        "telemetry",
        f"telemetry:{sequence:04d}",
        "telemetry-v2",
    )


def _observed(sequence: int, frame: int, event_type: str, facts: object) -> ObservedEvidence:
    return ObservedEvidence(_ref(sequence), frame, event_type, facts)


def _projection() -> dict[str, object]:
    return {
        "amphibious_passable": [True, True, True, True],
        "content_sha256": "d" * 64,
        "engine_data_identity": "fixture-engine",
        "ground_passable": [True, True, True, True],
        "map_identity": "maps/fixture.map",
        "pathing": {
            "bounds": {
                "maximum_exclusive": {"x": 40.0, "y": 10.0},
                "minimum_inclusive": {"x": 0.0, "y": 0.0},
            },
            "cell_size": {"x": 10.0, "y": 10.0},
            "dimension_source": "fixture grid",
            "height": 1,
            "index_origin": {"x": 0, "y": 0},
            "sample_point": "cell_center",
            "storage_order": "row_major_y_then_x_x_fastest",
            "width": 4,
        },
        "schema_version": 2,
        "start_positions": [
            {
                "bounds_policy": "pathfinder_xy_closed",
                "category_source": "GameSlot::getStartPos + TerrainLogic::getWaypointByName",
                "name": "Start Zero",
                "position": {"x": 5.0, "y": 5.0, "z": 0.0},
                "slot_indices": [0],
                "waypoint_id": 10,
            },
            {
                "bounds_policy": "pathfinder_xy_closed",
                "category_source": "GameSlot::getStartPos + TerrainLogic::getWaypointByName",
                "name": "Start One",
                "position": {"x": 35.0, "y": 5.0, "z": 0.0},
                "slot_indices": [1],
                "waypoint_id": 20,
            },
        ],
        "static_objects": [
            {
                "bounds_policy": "pathfinder_xy_closed",
                "categories": [
                    {"name": "supply_source", "source": "ThingTemplate::isKindOf(KINDOF_SUPPLY_SOURCE)"}
                ],
                "creation_source": "map_loaded",
                "object_id": 77,
                "orientation": 0.0,
                "position": {"x": 15.0, "y": 5.0, "z": 0.0},
                "snapshot_scope": "post_map_initialization",
                "template_name": "SupplyDock",
            }
        ],
        "world_bounds": {
            "maximum": {"x": 40.0, "y": 10.0, "z": 5.0},
            "maximum_inclusive": True,
            "minimum": {"x": 0.0, "y": 0.0, "z": -5.0},
            "minimum_inclusive": True,
        },
        "zone_ids": [1, 1, 1, 1],
    }


def _manifest(sequence: int = 1, *, projection: object | None = None) -> ObservedEvidence:
    semantic_projection = _projection() if projection is None else projection
    return _observed(
        sequence,
        0,
        "manifest",
        {
            "engine_build": "fixture-engine",
            "game_data_catalog": {
                "engine_data_identity": "fixture-engine",
                "sha256": "f" * 64,
                "type": "game_data_catalog",
            },
            "map_asset": {
                "content_sha256": "d" * 64,
                "engine_data_identity": "fixture-engine",
                "map_identity": "maps/fixture.map",
                "schema_version": 2,
                "sha256": "e" * 64,
                "type": "map_asset",
            },
            "map_identity": "maps/fixture.map",
            "validated_spatial_projection": semantic_projection,
        },
    )


def _players_initialized(sequence: int, player: str, *, slots: object | None = None) -> ObservedEvidence:
    return _observed(
        sequence,
        1,
        "players_initialized",
        {
            "slots": [
                {
                    "owner_scope_key": player,
                    "player_index": 0,
                    "replay_player_public_id": player,
                    "resolution_status": "resolved",
                    "slot_index": 0,
                },
                {
                    "owner_scope_key": "player:other",
                    "player_index": 1,
                    "replay_player_public_id": "player:other",
                    "resolution_status": "resolved",
                    "slot_index": 1,
                },
            ]
            if slots is None
            else slots
        },
    )


def _slot(
    owner: str,
    *,
    slot_index: object = 0,
    player_index: object = 0,
    resolution_status: object = "resolved",
) -> dict[str, object]:
    return {
        "owner_scope_key": owner,
        "player_index": player_index,
        "replay_player_public_id": owner,
        "resolution_status": resolution_status,
        "slot_index": slot_index,
    }


def _context(
    observed: tuple[ObservedEvidence, ...], *, scope_type: str = "player", settings: object | None = None
) -> FeatureContext:
    replay = "00000000-0000-4000-8000-000000009001"
    player = "00000000-0000-4000-8000-000000009002"
    scope = FeatureScope("player", player, player) if scope_type == "player" else FeatureScope("replay", replay)
    return FeatureContext(
        cache_schema="feature-context-v1",
        replay_public_id=replay,
        replay_sha256="a" * 64,
        replay_player_public_id=player if scope_type == "player" else None,
        scope=scope,
        observation_schema_versions=(("telemetry", "telemetry-v2:fixture"),),
        parser_completion_status="complete",
        telemetry_status="succeeded",
        final_frame=300,
        logic_frames_per_second=30,
        catalog_identity="f" * 64,
        observed=observed,
        settings={"spatial": _spatial_settings()} if settings is None else settings,
    )


def _sample(sequence: int, frame: int, *, object_id: int, owner: str, x: float, structure: bool = False):
    return _observed(
        sequence,
        frame,
        "entity_sample",
        {
            "is_disabled": False,
            "is_engine_moving": not structure,
            "is_mobile": not structure,
            "is_structure": structure,
            "locomotor_surface": None if structure else "ground",
            "object_id": object_id,
            "object_key": f"object:{object_id}",
            "owner_scope_key": owner,
            "path_goal": None,
            "position": {"x": x, "y": 5.0, "z": 0.0},
            "position_bounds_policy": "pathfinder_xy_closed",
        },
    )


def _values(bundle) -> dict[str, object]:
    return {value.name: value for value in bundle.values}


def test_plugin_definitions_are_exact_multi_namespace_and_compose_purely() -> None:
    plugin = SpatialFeaturePlugin()
    expected_specs = {
        "army_route.observed_reachable_distance": ("real", "engine_world_unit", ("player",)),
        "army_route.observed_route_segment_count": ("integer", "count", ("player",)),
        "engagement.observed_cluster_count": ("integer", "count", ("replay",)),
        "engagement.zone_clusters": ("json", "json", ("replay",)),
        "expansion.completed_structure_positions": ("json", "json", ("player",)),
        "expansion.forward_completed_structure_count": ("integer", "count", ("player",)),
        "map_control.observed_cell_presence_share": ("json", "json", ("replay",)),
        "movement_density.sample_count_heatmap": ("json", "json", ("player",)),
        "movement_density.sample_count_heatmap_bootstrap_interval": ("json", "json", ("player",)),
        "resource_control.observed_supply_collected_amount": ("real", "credits", ("player",)),
        "resource_control.observed_supply_collection_share": ("real", "ratio", ("player",)),
    }

    assert plugin.plugin_name == "spatial"
    assert plugin.plugin_version == "spatial-features-v1"
    assert SpatialFeatureExtractor.observation_policy == "replay_wide_telemetry"
    assert plugin.registry_schema == "feature-registry-v1"
    assert plugin.owned_namespaces == (
        "army_route",
        "engagement",
        "expansion",
        "map_control",
        "movement_density",
        "resource_control",
    )
    assert tuple(definition.name for definition in plugin.definitions) == SPATIAL_FEATURE_NAMES
    assert {
        definition.name: (definition.value_type, definition.unit, definition.scope_types)
        for definition in plugin.definitions
    } == expected_specs
    composed = BASE_REGISTRY.with_plugin(plugin)
    assert BASE_REGISTRY.names() == tuple(name for name in BASE_REGISTRY.names())
    assert set(SPATIAL_FEATURE_NAMES).issubset(composed.names())


def test_missing_map_emits_each_scope_feature_as_null_unavailable() -> None:
    extractor = SpatialFeatureExtractor()
    player = extractor.extract(_context(()))
    replay = extractor.extract(_context((), scope_type="replay"))

    assert tuple(value.name for value in player.values) == PLAYER_NAMES
    assert tuple(value.name for value in replay.values) == REPLAY_NAMES
    assert all(value.raw_value is None and value.quality == "unavailable" for value in player.values + replay.values)
    assert {value.quality_reason for value in player.values + replay.values} == {"missing_validated_map_asset"}
    assert all(value.input_evidence == () for value in player.values + replay.values)


def test_player_extractor_uses_only_observed_sources_routes_positions_and_density() -> None:
    player = "00000000-0000-4000-8000-000000009002"
    observations = (
        _manifest(),
        _players_initialized(2, player),
        _observed(
            3,
            10,
            "supply_collected",
            {"amount": 100.0, "replay_player_public_id": player, "source_object_id": 77},
        ),
        _observed(
            4,
            20,
            "supply_collected",
            {"amount": 200.0, "replay_player_public_id": player, "source_object_id": 77},
        ),
        _observed(
            5,
            20,
            "supply_collected",
            {"amount": 300.0, "replay_player_public_id": "player:other", "source_object_id": 77},
        ),
        _observed(
            6,
            30,
            "construction_completed",
            {"object_id": 88, "replay_player_public_id": player, "template_name": "ForwardBarracks"},
        ),
        _sample(7, 30, object_id=88, owner=player, x=15.0, structure=True),
        _sample(8, 40, object_id=99, owner=player, x=5.0),
        _sample(9, 50, object_id=99, owner=player, x=25.0),
    )
    bundle = SpatialFeatureExtractor().extract(_context(observations))
    values = _values(bundle)

    assert values["resource_control.observed_supply_collected_amount"].raw_value == 300.0
    assert values["resource_control.observed_supply_collection_share"].raw_value == 0.5
    share_details = thaw_canonical(values["resource_control.observed_supply_collection_share"].details)
    assert share_details["per_source"] == [
        {
            "denominator": 600.0,
            "denominator_evidence_public_ids": [
                "00000000-0000-4000-8000-000000000003",
                "00000000-0000-4000-8000-000000000004",
                "00000000-0000-4000-8000-000000000005",
            ],
            "player_amount": 300.0,
            "player_evidence_public_ids": [
                "00000000-0000-4000-8000-000000000003",
                "00000000-0000-4000-8000-000000000004",
            ],
            "share": 0.5,
            "source_object_id": 77,
        }
    ]
    positions = thaw_canonical(values["expansion.completed_structure_positions"].raw_value)
    assert positions[0]["object_id"] == 88
    assert positions[0]["map_normalized"] == {"u": 0.375, "v": 0.5}
    assert values["expansion.completed_structure_positions"].quality == "partial"
    assert values["expansion.completed_structure_positions"].quality_reason == "unresolved_player_transform"
    assert _ref(2).public_id in {
        reference.public_id for reference in values["expansion.completed_structure_positions"].input_evidence
    }
    assert values["expansion.forward_completed_structure_count"].raw_value is None
    assert values["expansion.forward_completed_structure_count"].quality_reason == "unresolved_player_transform"
    assert values["army_route.observed_reachable_distance"].raw_value == 20.0
    assert values["army_route.observed_route_segment_count"].raw_value == 1
    heatmap = thaw_canonical(values["movement_density.sample_count_heatmap"].raw_value)
    assert heatmap["sample_count"] == 2
    assert heatmap["cells"] == [
        {"cell_x": 0, "cell_y": 0, "count": 1},
        {"cell_x": 2, "cell_y": 0, "count": 1},
    ]
    bootstrap = values["movement_density.sample_count_heatmap_bootstrap_interval"]
    bootstrap_raw = thaw_canonical(bootstrap.raw_value)
    bootstrap_details = thaw_canonical(bootstrap.details)
    assert bootstrap.quality == "complete"
    assert bootstrap_raw["sample_count"] == 2
    assert bootstrap_details["algorithm_version"] == "pcg64-bootstrap-v1"
    assert bootstrap_details["bit_generator"] == "PCG64"
    assert bootstrap_details["input_evidence_public_ids"] == [
        "00000000-0000-4000-8000-000000000008",
        "00000000-0000-4000-8000-000000000009",
    ]
    assert bootstrap_details["seed_hex"] == derive_bootstrap_seed(input_digest(_context(observations)))
    assert bootstrap_details["numpy_version"]
    assert bootstrap_details["scipy_version"]
    assert all(_manifest().ref.public_id in {ref.public_id for ref in value.input_evidence} for value in bundle.values[:-1])


def test_bootstrap_unavailable_below_two_samples_keeps_exact_eligible_ids_separate_from_omissions() -> None:
    player = "00000000-0000-4000-8000-000000009002"
    observations = (_manifest(), _sample(2, 10, object_id=1, owner=player, x=5.0))

    value = _values(SpatialFeatureExtractor().extract(_context(observations)))[
        "movement_density.sample_count_heatmap_bootstrap_interval"
    ]
    details = thaw_canonical(value.details)

    assert value.quality == "unavailable"
    assert value.quality_reason == "insufficient_resample_observations"
    assert details["input_evidence_public_ids"] == [_ref(2).public_id]
    assert details["omitted_evidence_public_ids"] == []
    assert {reference.public_id for reference in value.input_evidence} == {_ref(1).public_id, _ref(2).public_id}


def test_bootstrap_partial_details_do_not_mix_eligible_and_oob_evidence_ids() -> None:
    player = "00000000-0000-4000-8000-000000009002"
    observations = (
        _manifest(),
        _sample(2, 10, object_id=1, owner=player, x=5.0),
        _sample(3, 20, object_id=1, owner=player, x=15.0),
        _sample(4, 30, object_id=1, owner=player, x=45.0),
    )

    value = _values(SpatialFeatureExtractor().extract(_context(observations)))[
        "movement_density.sample_count_heatmap_bootstrap_interval"
    ]
    details = thaw_canonical(value.details)

    assert value.quality == "partial"
    assert value.quality_reason == "grid_coordinate_out_of_bounds"
    assert details["input_evidence_public_ids"] == [_ref(2).public_id, _ref(3).public_id]
    assert details["omitted_evidence_public_ids"] == [_ref(4).public_id]
    assert {reference.public_id for reference in value.input_evidence} == {
        _ref(1).public_id,
        _ref(2).public_id,
        _ref(3).public_id,
        _ref(4).public_id,
    }


def test_resource_values_preserve_unresolved_source_evidence_as_a_partial_omission() -> None:
    player = "00000000-0000-4000-8000-000000009002"
    observations = (
        _manifest(),
        _observed(
            2,
            10,
            "supply_collected",
            {"amount": 100.0, "replay_player_public_id": player, "source_object_id": 77},
        ),
        _observed(
            3,
            20,
            "supply_collected",
            {"amount": 50.0, "replay_player_public_id": player, "source_object_id": 999},
        ),
    )

    values = _values(SpatialFeatureExtractor().extract(_context(observations)))

    for name in (
        "resource_control.observed_supply_collected_amount",
        "resource_control.observed_supply_collection_share",
    ):
        value = values[name]
        details = thaw_canonical(value.details)
        assert value.quality == "partial"
        assert value.quality_reason == "unresolved_supply_source"
        assert _ref(3).public_id in {reference.public_id for reference in value.input_evidence}
        assert details["omissions"] == [
            {
                "effect": "excluded_from_resolved_supply_amount_and_share",
                "evidence_public_id": _ref(3).public_id,
                "reason": "unresolved_supply_source",
            }
        ]


def test_share_retains_unresolved_peer_denominator_evidence_when_target_has_no_collection() -> None:
    peer_unresolved = _observed(
        2,
        10,
        "supply_collected",
        {"amount": 200.0, "replay_player_public_id": "player:peer", "source_object_id": 999},
    )

    values = _values(SpatialFeatureExtractor().extract(_context((_manifest(), peer_unresolved))))
    amount = values["resource_control.observed_supply_collected_amount"]
    share = values["resource_control.observed_supply_collection_share"]
    amount_details = thaw_canonical(amount.details)
    share_details = thaw_canonical(share.details)

    assert amount.quality_reason == "missing_resolved_supply_source"
    assert {reference.public_id for reference in amount.input_evidence} == {_ref(1).public_id}
    assert amount_details["omissions"] == []
    assert share.quality_reason == "unresolved_supply_source"
    assert {reference.public_id for reference in share.input_evidence} == {
        _ref(1).public_id,
        peer_unresolved.ref.public_id,
    }
    assert share_details["omissions"] == [
        {
            "effect": "excluded_from_resolved_supply_amount_and_share",
            "evidence_public_id": peer_unresolved.ref.public_id,
            "reason": "unresolved_supply_source",
        }
    ]


@pytest.mark.parametrize("reverse", (False, True))
def test_no_owned_share_keeps_target_omission_distinct_from_resolved_peer_denominator(reverse) -> None:
    player = "00000000-0000-4000-8000-000000009002"
    target_unresolved = _observed(
        2,
        10,
        "supply_collected",
        {"amount": 50.0, "replay_player_public_id": player, "source_object_id": 999},
    )
    peer_resolved = _observed(
        3,
        20,
        "supply_collected",
        {"amount": 200.0, "replay_player_public_id": "player:peer", "source_object_id": 77},
    )
    supply = (peer_resolved, target_unresolved) if reverse else (target_unresolved, peer_resolved)

    values = _values(SpatialFeatureExtractor().extract(_context((_manifest(), *supply))))
    amount = values["resource_control.observed_supply_collected_amount"]
    share = values["resource_control.observed_supply_collection_share"]
    amount_details = thaw_canonical(amount.details)
    share_details = thaw_canonical(share.details)

    assert amount.quality_reason == "unresolved_supply_source"
    assert {reference.public_id for reference in amount.input_evidence} == {
        _ref(1).public_id,
        target_unresolved.ref.public_id,
    }
    assert [item["evidence_public_id"] for item in amount_details["omissions"]] == [
        target_unresolved.ref.public_id
    ]
    assert share.quality_reason == "unresolved_supply_source"
    assert {reference.public_id for reference in share.input_evidence} == {
        _ref(1).public_id,
        target_unresolved.ref.public_id,
        peer_resolved.ref.public_id,
    }
    assert share_details["denominator"] == 200.0
    assert [item["evidence_public_id"] for item in share_details["omissions"]] == [
        target_unresolved.ref.public_id
    ]


@pytest.mark.parametrize("reverse", (False, True))
def test_no_owned_all_unresolved_supply_permutations_keep_amount_and_share_omission_sets_distinct(reverse) -> None:
    player = "00000000-0000-4000-8000-000000009002"
    target_unresolved = _observed(
        2,
        10,
        "supply_collected",
        {"amount": 50.0, "replay_player_public_id": player, "source_object_id": 998},
    )
    peer_unresolved = _observed(
        3,
        20,
        "supply_collected",
        {"amount": 200.0, "replay_player_public_id": "player:peer", "source_object_id": 999},
    )
    supply = (peer_unresolved, target_unresolved) if reverse else (target_unresolved, peer_unresolved)

    values = _values(SpatialFeatureExtractor().extract(_context((_manifest(), *supply))))
    amount = values["resource_control.observed_supply_collected_amount"]
    share = values["resource_control.observed_supply_collection_share"]
    amount_details = thaw_canonical(amount.details)
    share_details = thaw_canonical(share.details)

    assert amount.quality_reason == "unresolved_supply_source"
    assert [item["evidence_public_id"] for item in amount_details["omissions"]] == [
        target_unresolved.ref.public_id
    ]
    assert share.quality_reason == "unresolved_supply_source"
    assert [item["evidence_public_id"] for item in share_details["omissions"]] == [
        target_unresolved.ref.public_id,
        peer_unresolved.ref.public_id,
    ]
    assert {reference.public_id for reference in share.input_evidence} == {
        _ref(1).public_id,
        target_unresolved.ref.public_id,
        peer_unresolved.ref.public_id,
    }


def test_expansion_preserves_completion_without_bounded_matching_sample_as_partial() -> None:
    player = "00000000-0000-4000-8000-000000009002"
    observations = (
        _manifest(),
        _players_initialized(2, player),
        _observed(
            3,
            30,
            "construction_completed",
            {"object_id": 88, "replay_player_public_id": player, "template_name": "Barracks"},
        ),
        _sample(4, 30, object_id=88, owner=player, x=15.0, structure=True),
        _observed(
            5,
            40,
            "construction_completed",
            {"object_id": 89, "replay_player_public_id": player, "template_name": "WarFactory"},
        ),
    )

    value = _values(SpatialFeatureExtractor().extract(_context(observations)))[
        "expansion.completed_structure_positions"
    ]
    details = thaw_canonical(value.details)

    assert value.quality == "partial"
    assert _ref(5).public_id in {reference.public_id for reference in value.input_evidence}
    assert details["omissions"] == [
        {
            "effect": "excluded_from_completed_structure_positions_and_forward_count",
            "evidence_public_ids": [_ref(5).public_id],
            "object_id": 89,
            "reason": "missing_bounded_matching_structure_sample",
        }
    ]


def test_density_preserves_position_exempt_evidence_and_states_metric_effect() -> None:
    player = "00000000-0000-4000-8000-000000009002"
    exempt = replace(
        _sample(4, 30, object_id=1, owner=player, x=25.0),
        facts={
            **thaw_canonical(_sample(4, 30, object_id=1, owner=player, x=25.0).facts),
            "position_bounds_policy": "position_exempt",
        },
    )
    observations = (
        _manifest(),
        _sample(2, 10, object_id=1, owner=player, x=5.0),
        _sample(3, 20, object_id=1, owner=player, x=15.0),
        exempt,
    )

    values = _values(SpatialFeatureExtractor().extract(_context(observations)))

    for name in (
        "movement_density.sample_count_heatmap",
        "movement_density.sample_count_heatmap_bootstrap_interval",
    ):
        value = values[name]
        details = thaw_canonical(value.details)
        assert value.quality == "partial"
        assert value.quality_reason == "position_exempt_spatial_sample"
        assert details["omitted_evidence_public_ids"] == [_ref(4).public_id]
        assert details["omission_effect"] == "excluded_from_bounded_sample_count_statistics"


def test_replay_extractor_clusters_damage_and_reports_two_player_presence() -> None:
    observations = (
        _manifest(),
        _sample(2, 10, object_id=1, owner="player:one", x=5.0),
        _sample(3, 11, object_id=2, owner="player:two", x=5.0),
        _observed(
            4,
            20,
            "damage_applied",
            {
                "applied_amount": 25.0,
                "killing_blow": False,
                "location": {"x": 5.0, "y": 5.0, "z": 0.0},
                "source_replay_player_public_ids": ["player:one"],
                "victim_replay_player_public_id": "player:two",
            },
        ),
        _observed(
            5,
            30,
            "damage_applied",
            {
                "applied_amount": 50.0,
                "killing_blow": True,
                "location": {"x": 15.0, "y": 5.0, "z": 0.0},
                "source_replay_player_public_ids": ["player:one"],
                "victim_replay_player_public_id": "player:two",
            },
        ),
    )
    bundle = SpatialFeatureExtractor().extract(_context(observations, scope_type="replay"))
    values = _values(bundle)

    assert values["engagement.observed_cluster_count"].raw_value == 1
    clusters = thaw_canonical(values["engagement.zone_clusters"].raw_value)
    assert clusters[0]["applied_damage_sum"] == 75.0
    assert clusters[0]["killing_blow_count"] == 1
    presence = thaw_canonical(values["map_control.observed_cell_presence_share"].raw_value)
    assert presence[0]["shares"] == [
        {"count": 1, "scope_key": "player:one", "share": 0.5},
        {"count": 1, "scope_key": "player:two", "share": 0.5},
    ]


def test_replay_extractor_keeps_all_proven_damage_attackers_for_cluster_continuity() -> None:
    observations = (
        _manifest(),
        _observed(
            2,
            20,
            "damage_applied",
            {
                "applied_amount": 25.0,
                "killing_blow": False,
                "location": {"x": 5.0, "y": 5.0, "z": 0.0},
                "source_replay_player_public_ids": ["player:one", "player:two"],
                "victim_replay_player_public_id": "player:three",
            },
        ),
        _observed(
            3,
            30,
            "damage_applied",
            {
                "applied_amount": 50.0,
                "killing_blow": False,
                "location": {"x": 15.0, "y": 5.0, "z": 0.0},
                "source_replay_player_public_ids": ["player:two"],
                "victim_replay_player_public_id": "player:four",
            },
        ),
    )

    values = _values(SpatialFeatureExtractor().extract(_context(observations, scope_type="replay")))
    clusters = thaw_canonical(values["engagement.zone_clusters"].raw_value)

    assert values["engagement.observed_cluster_count"].raw_value == 1
    assert clusters[0]["participant_scopes"] == [
        "player:four",
        "player:one",
        "player:three",
        "player:two",
    ]


def test_player_partial_and_unavailable_outputs_retain_exact_omissions() -> None:
    player = "00000000-0000-4000-8000-000000009002"
    observations = (
        _manifest(),
        _observed(
            2,
            10,
            "construction_completed",
            {"object_id": 88, "replay_player_public_id": player, "template_name": "Barracks"},
        ),
        _sample(3, 10, object_id=88, owner=player, x=15.0, structure=True),
        replace(_sample(4, 20, object_id=99, owner=player, x=5.0), facts={
            "is_disabled": False,
            "is_engine_moving": True,
            "is_mobile": True,
            "is_structure": False,
            "locomotor_surface": None,
            "object_id": 99,
            "object_key": "object:99",
            "owner_scope_key": player,
            "path_goal": None,
            "position": {"x": 5.0, "y": 5.0, "z": 0.0},
            "position_bounds_policy": "pathfinder_xy_closed",
        }),
        replace(_sample(5, 30, object_id=99, owner=player, x=25.0), facts={
            "is_disabled": False,
            "is_engine_moving": True,
            "is_mobile": True,
            "is_structure": False,
            "locomotor_surface": None,
            "object_id": 99,
            "object_key": "object:99",
            "owner_scope_key": player,
            "path_goal": None,
            "position": {"x": 25.0, "y": 5.0, "z": 0.0},
            "position_bounds_policy": "pathfinder_xy_closed",
        }),
    )
    values = _values(SpatialFeatureExtractor().extract(_context(observations)))

    assert values["expansion.completed_structure_positions"].quality == "partial"
    assert values["expansion.completed_structure_positions"].quality_reason == "unresolved_player_transform"
    assert values["expansion.forward_completed_structure_count"].quality_reason == "unresolved_player_transform"
    assert values["army_route.observed_reachable_distance"].quality_reason == "unsupported_or_unproven_locomotor_surface"
    assert values["army_route.observed_route_segment_count"].quality_reason == "unsupported_or_unproven_locomotor_surface"


@pytest.mark.parametrize(
    "initializations",
    (
        (),
        (_observed(2, 1, "players_initialized", {}),),
        (_observed(2, 1, "players_initialized", {"slots": "not-a-list"}),),
        (_observed(2, 1, "players_initialized", {"slots": [{"slot_index": 0}]}),),
        (
            _observed(
                2,
                1,
                "players_initialized",
                {
                    "slots": [
                        {
                            "owner_scope_key": "player:wrong",
                            "player_index": 0,
                            "replay_player_public_id": "player:other-wrong",
                            "resolution_status": "resolved",
                            "slot_index": 0,
                        }
                    ]
                },
            ),
        ),
        (
            _players_initialized(2, "00000000-0000-4000-8000-000000009002"),
            _players_initialized(3, "00000000-0000-4000-8000-000000009002"),
        ),
        (
            _players_initialized(
                2,
                "00000000-0000-4000-8000-000000009002",
                slots=[
                    {
                        "owner_scope_key": "00000000-0000-4000-8000-000000009002",
                        "player_index": 0,
                        "replay_player_public_id": "00000000-0000-4000-8000-000000009002",
                        "resolution_status": "resolved",
                        "slot_index": 0,
                    },
                    {
                        "owner_scope_key": "player:other",
                        "player_index": 1,
                        "replay_player_public_id": "player:other",
                        "resolution_status": "resolved",
                        "slot_index": 0,
                    },
                ],
            ),
        ),
        (_players_initialized(2, "player:target", slots=[_slot("player:target", resolution_status="unresolved")]),),
        (_players_initialized(2, "player:target", slots=[_slot("player:target", slot_index=True)]),),
        (_players_initialized(2, "player:target", slots=[_slot("player:target", slot_index=8)]),),
        (_players_initialized(2, "player:target", slots=[_slot("player:target", player_index=True)]),),
        (
            _players_initialized(
                2,
                "player:target",
                slots=[
                    _slot("player:target", slot_index=0, player_index=0),
                    _slot("player:other", slot_index=1, player_index=0),
                ],
            ),
        ),
        (
            _players_initialized(
                2,
                "player:target",
                slots=[
                    _slot("player:target", slot_index=0, player_index=0),
                    _slot("player:target", slot_index=1, player_index=1),
                ],
            ),
        ),
        (_players_initialized(2, "player:other", slots=[_slot("player:other")]),),
    ),
)
def test_player_transform_rejects_missing_duplicate_malformed_or_mismatched_slot_mapping(initializations) -> None:
    player = "00000000-0000-4000-8000-000000009002"
    observations = (
        _manifest(),
        *initializations,
        _observed(
            20,
            30,
            "construction_completed",
            {"object_id": 88, "replay_player_public_id": player, "template_name": "Barracks"},
        ),
        _sample(21, 30, object_id=88, owner=player, x=15.0, structure=True),
    )

    values = _values(SpatialFeatureExtractor().extract(_context(observations)))

    assert values["expansion.completed_structure_positions"].quality == "partial"
    assert values["expansion.completed_structure_positions"].quality_reason == "unresolved_player_transform"
    assert values["expansion.forward_completed_structure_count"].quality == "unavailable"
    assert values["expansion.forward_completed_structure_count"].quality_reason == "unresolved_player_transform"


def test_player_transform_rejects_zero_or_multiple_matching_validated_map_starts() -> None:
    player = "00000000-0000-4000-8000-000000009002"
    empty_projection = _projection()
    empty_projection["start_positions"] = []
    duplicate_projection = _projection()
    starts = duplicate_projection["start_positions"]
    assert isinstance(starts, list)
    duplicate = dict(starts[1])
    duplicate["slot_indices"] = [0]
    duplicate_projection["start_positions"] = [starts[0], duplicate]
    tail = (
        _players_initialized(2, player),
        _observed(
            3,
            30,
            "construction_completed",
            {"object_id": 88, "replay_player_public_id": player, "template_name": "Barracks"},
        ),
        _sample(4, 30, object_id=88, owner=player, x=15.0, structure=True),
    )

    zero = _values(SpatialFeatureExtractor().extract(_context((_manifest(projection=empty_projection), *tail))))
    multiple = _values(SpatialFeatureExtractor().extract(_context((_manifest(projection=duplicate_projection), *tail))))

    assert zero["expansion.completed_structure_positions"].quality_reason == "unresolved_player_transform"
    assert multiple["expansion.completed_structure_positions"].quality == "unavailable"
    assert multiple["expansion.completed_structure_positions"].quality_reason == "invalid_validated_map_projection"


def test_player_transform_ignores_a_nearer_unassigned_map_start_and_requires_proven_opponent_direction() -> None:
    player = "00000000-0000-4000-8000-000000009002"
    projection = _projection()
    starts = projection["start_positions"]
    assert isinstance(starts, list)
    projection["start_positions"] = [
        starts[0],
        {
            **starts[1],
            "name": "Unassigned Nearer",
            "position": {"x": 15.0, "y": 5.0, "z": 0.0},
            "slot_indices": [7],
            "waypoint_id": 15,
        },
        starts[1],
    ]
    observations = (
        _manifest(projection=projection),
        _players_initialized(2, player),
        _observed(
            3,
            30,
            "construction_completed",
            {"object_id": 88, "replay_player_public_id": player, "template_name": "Barracks"},
        ),
        _sample(4, 30, object_id=88, owner=player, x=15.0, structure=True),
    )

    values = _values(SpatialFeatureExtractor().extract(_context(observations)))
    positions = values["expansion.completed_structure_positions"]

    assert positions.quality == "partial"
    assert positions.quality_reason == "unresolved_player_transform"
    assert thaw_canonical(positions.raw_value)[0]["player_centric"] is None
    assert values["expansion.forward_completed_structure_count"].quality == "unavailable"


def test_player_transform_does_not_assume_a_resolved_peer_is_an_opponent_without_relation_evidence() -> None:
    player = "00000000-0000-4000-8000-000000009002"
    observations = (
        _manifest(),
        _players_initialized(2, player),
        _observed(
            3,
            30,
            "construction_completed",
            {"object_id": 88, "replay_player_public_id": player, "template_name": "Barracks"},
        ),
        _sample(4, 30, object_id=88, owner=player, x=15.0, structure=True),
    )

    values = _values(SpatialFeatureExtractor().extract(_context(observations)))

    assert values["expansion.completed_structure_positions"].quality_reason == "unresolved_player_transform"
    assert values["expansion.forward_completed_structure_count"].quality_reason == "unresolved_player_transform"


def test_player_transform_does_not_use_an_unassigned_map_start_when_no_peer_slot_exists() -> None:
    player = "00000000-0000-4000-8000-000000009002"
    observations = (
        _manifest(),
        _players_initialized(2, player, slots=[_slot(player)]),
        _observed(
            3,
            30,
            "construction_completed",
            {"object_id": 88, "replay_player_public_id": player, "template_name": "Barracks"},
        ),
        _sample(4, 30, object_id=88, owner=player, x=15.0, structure=True),
    )

    values = _values(SpatialFeatureExtractor().extract(_context(observations)))

    assert values["expansion.completed_structure_positions"].quality_reason == "unresolved_player_transform"
    assert values["expansion.forward_completed_structure_count"].quality_reason == "unresolved_player_transform"


def test_extraction_is_permutation_stable_and_semantic_changes_invalidate_cache() -> None:
    observations = (_manifest(), _sample(2, 10, object_id=1, owner="player:one", x=5.0))
    baseline = _context(observations, scope_type="replay")
    expected = canonical_json(SpatialFeatureExtractor().extract(baseline))
    shuffler = random.Random(20260822)
    for _ in range(100):
        order = list(observations)
        shuffler.shuffle(order)
        assert canonical_json(SpatialFeatureExtractor().extract(_context(tuple(order), scope_type="replay"))) == expected

    changed_projection = _projection()
    changed_projection["content_sha256"] = "c" * 64
    changed_manifest = _manifest(projection=changed_projection)
    assert cache_key(_context((changed_manifest,), scope_type="replay"), "spatial", "spatial-features-v1") != cache_key(
        baseline, "spatial", "spatial-features-v1"
    )


def test_extractor_never_calls_map_loader(monkeypatch) -> None:
    import generals_replay_analyzer.telemetry.map_asset as loader

    def forbidden(*args, **kwargs):
        raise AssertionError("spatial extractor must not call load_map_asset")

    monkeypatch.setattr(loader, "load_map_asset", forbidden)
    SpatialFeatureExtractor().extract(_context((_manifest(),)))


def test_extractor_rejects_scopes_outside_exact_plugin_definitions() -> None:
    replay = _context((_manifest(),), scope_type="replay")
    team_context = replace(replay, scope=FeatureScope("team", "team:1", team_id=1))

    with pytest.raises(ValueError, match="supports only player and replay scopes"):
        SpatialFeatureExtractor().extract(team_context)
