"""Conservative temporal, reachable-route, and participant engagement clustering tests."""

import random
from dataclasses import replace

import pytest

from generals_replay_analyzer.features.context import canonical_json
from generals_replay_analyzer.spatial.assets import Position3, SpatialCombatObservation
from generals_replay_analyzer.spatial.engagements import cluster_engagements


def _combat(evidence_ref, sequence, *, frame, x, attacker="player:1", victim="player:2", killing=False):
    return SpatialCombatObservation(
        evidence=evidence_ref(sequence),
        frame=frame,
        location=Position3(x, 5.0, 0.0),
        attacker_scope_key=attacker,
        victim_scope_key=victim,
        applied_amount=float(sequence * 10),
        killing_blow=killing,
    )


def test_temporal_gap_boundary_joins_at_90_and_splits_at_91(evidence_ref, projection_factory) -> None:
    projection = projection_factory(width=4, height=1)
    observations = (
        _combat(evidence_ref, 1, frame=0, x=5.0),
        _combat(evidence_ref, 2, frame=90, x=15.0, killing=True),
        _combat(evidence_ref, 3, frame=181, x=25.0),
    )

    result = cluster_engagements(projection, observations, gap_frames=90, reachable_radius_cells=12)

    assert tuple(len(cluster.members) for cluster in result.clusters) == (2, 1)
    first = result.clusters[0]
    assert (first.first_frame, first.last_frame) == (0, 90)
    assert first.applied_damage_sum == 30.0
    assert first.killing_blow_count == 1
    assert first.participant_scopes == ("player:1", "player:2")
    assert first.normalized_centroid.u == pytest.approx(0.25)
    assert tuple(ref.public_id for ref in first.evidence) == tuple(sorted(ref.public_id for ref in first.evidence))


def test_reachable_radius_boundary_joins_at_12_and_splits_at_13(evidence_ref, projection_factory) -> None:
    projection = projection_factory(width=14, height=1)
    observations = (
        _combat(evidence_ref, 1, frame=0, x=5.0),
        _combat(evidence_ref, 2, frame=1, x=125.0),
        _combat(evidence_ref, 3, frame=2, x=5.0),
    )

    joined = cluster_engagements(projection, observations[:2], gap_frames=90, reachable_radius_cells=12)
    split = cluster_engagements(projection, (observations[0], replace(observations[1], location=Position3(135.0, 5.0, 0.0))), gap_frames=90, reachable_radius_cells=12)

    assert len(joined.clusters) == 1
    assert len(split.clusters) == 2


def test_participant_discontinuity_and_disconnected_near_cells_start_new_clusters(
    evidence_ref, projection_factory
) -> None:
    continuity = projection_factory(width=2, height=1)
    disconnected = projection_factory(width=2, height=1, zones=(1, 2))
    first = _combat(evidence_ref, 1, frame=0, x=5.0)
    unrelated = _combat(evidence_ref, 2, frame=1, x=15.0, attacker="player:3", victim="player:4")
    related = _combat(evidence_ref, 3, frame=1, x=15.0)

    assert len(cluster_engagements(continuity, (first, unrelated)).clusters) == 2
    assert len(cluster_engagements(disconnected, (first, related)).clusters) == 2


def test_mixed_surface_without_one_common_validated_route_does_not_merge(evidence_ref, projection_factory) -> None:
    projection = projection_factory(
        width=2,
        height=1,
        ground=(False, False),
        amphibious=(False, True),
        zones=(0, 0),
    )
    observations = (_combat(evidence_ref, 1, frame=0, x=5.0), _combat(evidence_ref, 2, frame=1, x=15.0))

    result = cluster_engagements(projection, observations)

    assert len(result.clusters) == 2
    assert all(cluster.transition_surfaces == () for cluster in result.clusters)


def test_unknown_participant_is_omitted_with_partial_quality(evidence_ref, projection_factory) -> None:
    known = _combat(evidence_ref, 1, frame=0, x=5.0)
    unknown = _combat(evidence_ref, 2, frame=1, x=15.0, attacker=None, victim=None)

    result = cluster_engagements(projection_factory(width=2, height=1), (unknown, known))

    assert result.quality == "partial"
    assert result.reason == "missing_combat_participant"
    assert result.omitted_evidence == (unknown.evidence,)
    assert len(result.clusters) == 1


def test_absent_locations_are_explicitly_unavailable(projection_factory) -> None:
    result = cluster_engagements(projection_factory(), ())
    assert result.quality == "unavailable"
    assert result.reason == "missing_combat_locations"
    assert result.clusters == ()


def test_cluster_membership_and_canonical_order_are_insertion_independent(evidence_ref, projection_factory) -> None:
    projection = projection_factory(width=5, height=1)
    observations = (
        _combat(evidence_ref, 4, frame=100, x=45.0),
        _combat(evidence_ref, 2, frame=20, x=15.0),
        _combat(evidence_ref, 1, frame=10, x=5.0),
        _combat(evidence_ref, 3, frame=30, x=25.0),
    )
    expected = None
    shuffler = random.Random(20260822)

    for _ in range(100):
        order = list(observations)
        shuffler.shuffle(order)
        result = cluster_engagements(projection, tuple(order), gap_frames=90, reachable_radius_cells=12)
        encoded = canonical_json(result)
        expected = encoded if expected is None else expected
        assert encoded == expected

    assert tuple(member.evidence.source_key for member in result.clusters[0].members) == (
        "telemetry:0001",
        "telemetry:0002",
        "telemetry:0003",
        "telemetry:0004",
    )


def test_oob_combat_location_has_its_own_explicit_omission_reason(evidence_ref, projection_factory) -> None:
    projection = projection_factory(width=2, height=1)
    bounded = _combat(evidence_ref, 1, frame=0, x=5.0)
    outside = _combat(evidence_ref, 2, frame=1, x=25.0)

    result = cluster_engagements(projection, (outside, bounded))

    assert result.quality == "partial"
    assert result.reason == "grid_coordinate_out_of_bounds"
    assert result.omitted_evidence == (outside.evidence,)


def test_engagements_reject_nonpositive_settings_and_invalid_projection(evidence_ref, projection_factory) -> None:
    projection = projection_factory()
    observation = _combat(evidence_ref, 1, frame=0, x=5.0)
    with pytest.raises(ValueError, match="positive"):
        cluster_engagements(projection, (observation,), gap_frames=0)
    with pytest.raises(ValueError, match="positive"):
        cluster_engagements(projection, (observation,), reachable_radius_cells=0)

    invalid = replace(projection, content_sha256="A" * 64)
    assert cluster_engagements(invalid, (observation,)).reason == "invalid_validated_map_projection"
