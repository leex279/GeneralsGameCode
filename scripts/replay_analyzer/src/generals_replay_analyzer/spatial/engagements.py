"""Conservative engagement clustering over observed combat and validated routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from generals_replay_analyzer.features.evidence import EvidenceRef, evidence_sort_key
from generals_replay_analyzer.spatial.assets import (
    LocomotorSurface,
    Position3,
    SpatialCombatObservation,
    SpatialMapProjection,
    SpatialUnavailable,
    validate_map_projection,
)
from generals_replay_analyzer.spatial.coordinates import (
    GridCell,
    NormalizedXY,
    world_to_grid_cell,
    world_to_map_normalized,
)
from generals_replay_analyzer.spatial.navigation import build_navigation_map, route_between

ENGAGEMENT_ALGORITHM_VERSION = "engagement-cluster-v1"
EngagementQuality: TypeAlias = Literal["complete", "partial", "unavailable"]


@dataclass(frozen=True)
class EngagementCluster:
    cluster_key: str
    members: tuple[SpatialCombatObservation, ...]
    centroid: Position3
    normalized_centroid: NormalizedXY
    zone_id: int | None
    participant_scopes: tuple[str, ...]
    applied_damage_sum: float
    killing_blow_count: int
    first_frame: int
    last_frame: int
    transition_surfaces: tuple[LocomotorSurface, ...]
    evidence: tuple[EvidenceRef, ...]
    algorithm_version: str


@dataclass(frozen=True)
class EngagementOmission:
    evidence: EvidenceRef
    reason: str


@dataclass(frozen=True)
class EngagementClusters:
    clusters: tuple[EngagementCluster, ...]
    omissions: tuple[EngagementOmission, ...]
    omitted_evidence: tuple[EvidenceRef, ...]
    quality: EngagementQuality
    reason: str | None
    algorithm_version: str
    map_content_sha256: str
    map_schema_version: int
    engine_data_identity: str


@dataclass
class _ClusterBuilder:
    members: list[SpatialCombatObservation]
    cells: list[GridCell]
    transition_surfaces: list[LocomotorSurface]


def _observation_key(item: SpatialCombatObservation) -> tuple[int, str, str, str]:
    return (item.frame, item.evidence.source_kind, item.evidence.source_key, item.evidence.public_id)


def _participants(item: SpatialCombatObservation) -> frozenset[str]:
    return frozenset((*item.attacker_scope_keys, *((item.victim_scope_key,) if item.victim_scope_key else ())))


def _transition_surface(
    projection: SpatialMapProjection,
    start: GridCell,
    end: GridCell,
    maximum_distance: int,
) -> LocomotorSurface | None:
    for surface in ("ground", "amphibious"):
        navigation = build_navigation_map(projection, surface)
        route = route_between(navigation, start, end)
        if not isinstance(route, SpatialUnavailable) and route.distance_cells <= maximum_distance:
            return surface
    return None


def _finish_cluster(projection: SpatialMapProjection, builder: _ClusterBuilder) -> EngagementCluster:
    members = tuple(sorted(builder.members, key=_observation_key))
    count = len(members)
    centroid = Position3(
        sum(member.location.x for member in members) / count,
        sum(member.location.y for member in members) / count,
        sum(member.location.z for member in members) / count,
    )
    normalized = world_to_map_normalized(centroid, projection.world_bounds)
    assert isinstance(normalized, NormalizedXY), "validated bounded members produced an invalid centroid"
    zones = {projection.zone_ids[projection.pathing.row_major_index(cell.x, cell.y)] for cell in builder.cells}
    participants = tuple(sorted({scope for member in members for scope in _participants(member)}))
    evidence = tuple(sorted((member.evidence for member in members), key=evidence_sort_key))
    return EngagementCluster(
        cluster_key=members[0].evidence.public_id,
        members=members,
        centroid=centroid,
        normalized_centroid=normalized,
        zone_id=next(iter(zones)) if len(zones) == 1 else None,
        participant_scopes=participants,
        applied_damage_sum=sum(member.applied_amount for member in members),
        killing_blow_count=sum(1 for member in members if member.killing_blow),
        first_frame=members[0].frame,
        last_frame=members[-1].frame,
        transition_surfaces=tuple(builder.transition_surfaces),
        evidence=evidence,
        algorithm_version=ENGAGEMENT_ALGORITHM_VERSION,
    )


# TheSuperHackers @feature Leex 22/08/2026 Cluster combat only through temporal, participant, and validated-route continuity. (#0)
def cluster_engagements(
    projection: SpatialMapProjection,
    observations: tuple[SpatialCombatObservation, ...],
    *,
    gap_frames: int = 90,
    reachable_radius_cells: int = 12,
) -> EngagementClusters:
    if type(gap_frames) is not int or gap_frames <= 0 or type(reachable_radius_cells) is not int or reachable_radius_cells <= 0:
        raise ValueError("engagement bounds must be positive integers")
    checked = validate_map_projection(projection)
    if isinstance(checked, SpatialUnavailable):
        return EngagementClusters(
            (), (), (), "unavailable", checked.reason, ENGAGEMENT_ALGORITHM_VERSION,
            projection.content_sha256, projection.schema_version, projection.engine_data_identity,
        )
    builders: list[_ClusterBuilder] = []
    omitted: list[EngagementOmission] = []
    for observation in sorted(observations, key=_observation_key):
        participants = _participants(observation)
        if not participants:
            omitted.append(EngagementOmission(observation.evidence, "missing_combat_participant"))
            continue
        cell = world_to_grid_cell(observation.location, projection.pathing)
        if isinstance(cell, SpatialUnavailable):
            omitted.append(EngagementOmission(observation.evidence, cell.reason))
            continue
        if not builders:
            builders.append(_ClusterBuilder([observation], [cell], []))
            continue
        current = builders[-1]
        last = current.members[-1]
        surface = None
        if (
            observation.frame - last.frame <= gap_frames
            and participants.intersection({scope for member in current.members for scope in _participants(member)})
        ):
            surface = _transition_surface(projection, current.cells[-1], cell, reachable_radius_cells)
        if surface is None:
            builders.append(_ClusterBuilder([observation], [cell], []))
        else:
            current.members.append(observation)
            current.cells.append(cell)
            current.transition_surfaces.append(surface)
    clusters = tuple(_finish_cluster(projection, builder) for builder in builders)
    stable_omissions = tuple(sorted(omitted, key=lambda item: evidence_sort_key(item.evidence)))
    omitted_evidence = tuple(item.evidence for item in stable_omissions)
    if clusters and stable_omissions:
        quality: EngagementQuality = "partial"
        reason = stable_omissions[0].reason
    elif clusters:
        quality = "complete"
        reason = None
    else:
        quality = "unavailable"
        reason = stable_omissions[0].reason if stable_omissions else "missing_combat_locations"
    return EngagementClusters(
        clusters,
        stable_omissions,
        omitted_evidence,
        quality,
        reason,
        ENGAGEMENT_ALGORITHM_VERSION,
        projection.content_sha256,
        projection.schema_version,
        projection.engine_data_identity,
    )
