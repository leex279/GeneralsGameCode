"""Deterministic exact and locally seeded spatial statistics."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Literal, TypeAlias, cast

import numpy as np
import scipy  # type: ignore[import-untyped]
from scipy import stats

from generals_replay_analyzer.features.evidence import EvidenceRef, evidence_sort_key
from generals_replay_analyzer.spatial.assets import (
    SpatialMapProjection,
    SpatialSample,
    SpatialUnavailable,
    validate_map_projection,
)
from generals_replay_analyzer.spatial.coordinates import GridCell, world_to_grid_cell

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
PCG64_BOOTSTRAP_VERSION = "pcg64-bootstrap-v1"
NUMPY_VERSION = np.__version__
SCIPY_VERSION = scipy.__version__
_SEED_LABEL = ":spatial-bootstrap-v1"
_HEATMAP_VERSION = "sample-count-heatmap-v1"
StatisticQuality: TypeAlias = Literal["complete", "partial", "unavailable"]


@dataclass(frozen=True)
class SpatialAlgorithmSettings:
    algorithm_schema: str = "spatial-algorithm-settings-v1"
    route_algorithm_version: str = "grid-route-v1"
    engagement_algorithm_version: str = "engagement-cluster-v1"
    heatmap_algorithm_version: str = _HEATMAP_VERSION
    bootstrap_algorithm_version: str = "pcg64-bootstrap-v1"
    max_sample_gap_frames: int = 300
    engagement_gap_frames: int = 90
    engagement_reachable_radius_cells: int = 12
    bootstrap_resamples: int = 1000

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SpatialAlgorithmSettings:
        expected = cls()
        expected_names = {field.name for field in fields(cls)}
        if type(value) is not dict or set(value) != expected_names:
            raise ValueError("spatial settings must contain exactly the closed v1 fields")
        arguments = cast(dict[str, object], value)
        try:
            result = cls(**arguments)  # type: ignore[arg-type]
        except TypeError as error:
            raise ValueError("spatial settings do not match the closed v1 contract") from error
        result.validate()
        if any(
            getattr(result, name) != getattr(expected, name)
            for name in (
                "algorithm_schema",
                "route_algorithm_version",
                "engagement_algorithm_version",
                "heatmap_algorithm_version",
                "bootstrap_algorithm_version",
            )
        ):
            raise ValueError("spatial settings algorithm versions do not match the closed v1 contract")
        return result

    def validate(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (
                self.max_sample_gap_frames,
                self.engagement_gap_frames,
                self.engagement_reachable_radius_cells,
                self.bootstrap_resamples,
            )
        ):
            raise ValueError("spatial settings integer bounds must be positive")

    def canonical_items(self) -> tuple[tuple[str, object], ...]:
        self.validate()
        return tuple(sorted((field.name, getattr(self, field.name)) for field in fields(self)))


@dataclass(frozen=True)
class HeatmapCell:
    cell: GridCell
    count: int
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class SampleCountHeatmap:
    cells: tuple[HeatmapCell, ...]
    sample_count: int
    input_evidence: tuple[EvidenceRef, ...]
    omitted_evidence: tuple[EvidenceRef, ...]
    quality: StatisticQuality
    reason: str | None
    algorithm_version: str
    map_content_sha256: str
    map_schema_version: int
    engine_data_identity: str


@dataclass(frozen=True)
class ScopePresenceShare:
    scope_key: str
    count: int
    share: float
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class CellPresenceShare:
    cell: GridCell
    total_count: int
    shares: tuple[ScopePresenceShare, ...]
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class CellPresenceShares:
    cells: tuple[CellPresenceShare, ...]
    input_evidence: tuple[EvidenceRef, ...]
    omitted_evidence: tuple[EvidenceRef, ...]
    quality: StatisticQuality
    reason: str | None
    algorithm_version: str
    map_content_sha256: str
    map_schema_version: int
    engine_data_identity: str


@dataclass(frozen=True)
class BootstrapCellInterval:
    cell: GridCell
    observed_proportion: float
    lower: float
    upper: float


@dataclass(frozen=True)
class BootstrapHeatmapInterval:
    intervals: tuple[BootstrapCellInterval, ...]
    algorithm_version: str
    requested_algorithm_version: str
    bit_generator: str
    numpy_version: str
    scipy_version: str
    seed_hex: str
    resample_count: int
    statistic_name: str
    input_evidence: tuple[EvidenceRef, ...]
    omitted_evidence: tuple[EvidenceRef, ...]
    quality: StatisticQuality
    reason: str | None
    quantile_policy: str
    map_content_sha256: str
    map_schema_version: int
    engine_data_identity: str


def _sample_sort_key(sample: SpatialSample) -> tuple[int, str, str, str]:
    return (sample.frame, sample.evidence.source_kind, sample.evidence.source_key, sample.evidence.public_id)


def _eligible_entity(sample: SpatialSample, owner_scope_key: str | None) -> bool:
    return (
        sample.owner_scope_key is not None
        and (owner_scope_key is None or sample.owner_scope_key == owner_scope_key)
        and sample.is_mobile
        and not sample.is_structure
        and not sample.is_disabled
    )


def _sorted_evidence(references: list[EvidenceRef]) -> tuple[EvidenceRef, ...]:
    return tuple(sorted(references, key=evidence_sort_key))


# TheSuperHackers @feature Leex 22/08/2026 Count only observed bounded samples without interpolating trajectories. (#0)
def build_sample_count_heatmap(
    projection: SpatialMapProjection,
    samples: tuple[SpatialSample, ...],
    *,
    owner_scope_key: str | None = None,
) -> SampleCountHeatmap:
    checked = validate_map_projection(projection)
    if isinstance(checked, SpatialUnavailable):
        return SampleCountHeatmap(
            (), 0, (), (), "unavailable", checked.reason, _HEATMAP_VERSION,
            projection.content_sha256, projection.schema_version, projection.engine_data_identity,
        )
    cells: dict[GridCell, list[EvidenceRef]] = {}
    omitted: list[tuple[EvidenceRef, str]] = []
    for sample in sorted(samples, key=_sample_sort_key):
        if not _eligible_entity(sample, owner_scope_key):
            continue
        if sample.position_bounds_policy != "pathfinder_xy_closed":
            omitted.append((sample.evidence, "position_exempt_spatial_sample"))
            continue
        cell = world_to_grid_cell(sample.position, projection.pathing)
        if isinstance(cell, SpatialUnavailable):
            omitted.append((sample.evidence, cell.reason))
            continue
        cells.setdefault(cell, []).append(sample.evidence)
    values = tuple(
        HeatmapCell(cell, len(references), _sorted_evidence(references))
        for cell, references in sorted(cells.items(), key=lambda item: (item[0].x, item[0].y))
    )
    evidence = _sorted_evidence([reference for value in values for reference in value.evidence])
    stable_omissions = sorted(omitted, key=lambda item: evidence_sort_key(item[0]))
    omitted_evidence = tuple(item[0] for item in stable_omissions)
    omission_reason = stable_omissions[0][1] if stable_omissions else None
    if values and omitted_evidence:
        quality: StatisticQuality = "partial"
        reason = omission_reason
    elif values:
        quality = "complete"
        reason = None
    else:
        quality = "unavailable"
        reason = cast(str, omission_reason) if omitted_evidence else "missing_bounded_entity_samples"
    return SampleCountHeatmap(
        values,
        len(evidence),
        evidence,
        omitted_evidence,
        quality,
        reason,
        _HEATMAP_VERSION,
        projection.content_sha256,
        projection.schema_version,
        projection.engine_data_identity,
    )


def build_cell_presence_shares(
    projection: SpatialMapProjection, samples: tuple[SpatialSample, ...]
) -> CellPresenceShares:
    checked = validate_map_projection(projection)
    if isinstance(checked, SpatialUnavailable):
        return CellPresenceShares(
            (), (), (), "unavailable", checked.reason, _HEATMAP_VERSION,
            projection.content_sha256, projection.schema_version, projection.engine_data_identity,
        )
    by_cell: dict[GridCell, dict[str, list[EvidenceRef]]] = {}
    omitted: list[tuple[EvidenceRef, str]] = []
    bounded_evidence: list[EvidenceRef] = []
    bounded_scopes: set[str] = set()
    for sample in sorted(samples, key=_sample_sort_key):
        if not _eligible_entity(sample, None):
            continue
        if sample.position_bounds_policy != "pathfinder_xy_closed":
            omitted.append((sample.evidence, "position_exempt_spatial_sample"))
            continue
        cell = world_to_grid_cell(sample.position, projection.pathing)
        if isinstance(cell, SpatialUnavailable):
            omitted.append((sample.evidence, cell.reason))
            continue
        scope_key = cast(str, sample.owner_scope_key)
        bounded_evidence.append(sample.evidence)
        bounded_scopes.add(scope_key)
        by_cell.setdefault(cell, {}).setdefault(scope_key, []).append(sample.evidence)
    output: list[CellPresenceShare] = []
    for cell, scopes in sorted(by_cell.items(), key=lambda item: (item[0].x, item[0].y)):
        if len(scopes) < 2:
            continue
        total = sum(len(references) for references in scopes.values())
        shares = tuple(
            ScopePresenceShare(scope, len(references), len(references) / total, _sorted_evidence(references))
            for scope, references in sorted(scopes.items())
        )
        output.append(
            CellPresenceShare(
                cell,
                total,
                shares,
                _sorted_evidence([reference for share in shares for reference in share.evidence]),
            )
        )
    evidence = _sorted_evidence(bounded_evidence)
    stable_omissions = sorted(omitted, key=lambda item: evidence_sort_key(item[0]))
    omitted_evidence = tuple(item[0] for item in stable_omissions)
    omission_reason = stable_omissions[0][1] if stable_omissions else None
    if len(bounded_scopes) >= 2 and omitted_evidence:
        quality: StatisticQuality = "partial"
        reason = omission_reason
    elif len(bounded_scopes) >= 2:
        quality = "complete"
        reason = None
    else:
        quality = "unavailable"
        reason = cast(str, omission_reason) if omitted_evidence else "missing_bounded_entity_samples"
    return CellPresenceShares(
        tuple(output), evidence, omitted_evidence, quality, reason, _HEATMAP_VERSION,
        projection.content_sha256, projection.schema_version, projection.engine_data_identity,
    )


def derive_bootstrap_seed(input_digest: str) -> str:
    if type(input_digest) is not str or not _SHA256.fullmatch(input_digest):
        raise ValueError("input digest must be lower-case SHA-256")
    return hashlib.sha256(f"{input_digest}{_SEED_LABEL}".encode()).hexdigest()


def _bootstrap_interval(values: list[float]) -> tuple[float, float]:
    ordered = sorted(values)
    percentiles = stats.scoreatpercentile(
        ordered,
        (2.5, 97.5),
        interpolation_method="fraction",
    )
    return float(percentiles[0]), float(percentiles[1])


# TheSuperHackers @feature Leex 22/08/2026 Bootstrap with a locally seeded PCG64 and sorted SciPy quantiles. (#0)
def bootstrap_sample_count_heatmap_interval(
    projection: SpatialMapProjection,
    samples: tuple[SpatialSample, ...],
    *,
    input_digest: str,
    settings: SpatialAlgorithmSettings,
    owner_scope_key: str | None = None,
) -> BootstrapHeatmapInterval | SpatialUnavailable:
    settings.validate()
    checked = validate_map_projection(projection)
    if isinstance(checked, SpatialUnavailable):
        return checked
    eligible: list[tuple[GridCell, SpatialSample]] = []
    omitted: list[tuple[EvidenceRef, str]] = []
    for sample in sorted(samples, key=_sample_sort_key):
        if not _eligible_entity(sample, owner_scope_key):
            continue
        if sample.position_bounds_policy != "pathfinder_xy_closed":
            omitted.append((sample.evidence, "position_exempt_spatial_sample"))
            continue
        cell = world_to_grid_cell(sample.position, projection.pathing)
        if isinstance(cell, SpatialUnavailable):
            omitted.append((sample.evidence, cell.reason))
        else:
            eligible.append((cell, sample))
    if len(eligible) < 2:
        return SpatialUnavailable("insufficient_resample_observations")
    seed_hex = derive_bootstrap_seed(input_digest)
    generator = np.random.Generator(np.random.PCG64(int(seed_hex[:32], 16)))
    occupied = tuple(sorted({cell for cell, _ in eligible}, key=lambda cell: (cell.x, cell.y)))
    distributions: dict[GridCell, list[float]] = {cell: [] for cell in occupied}
    population_size = len(eligible)
    for _ in range(settings.bootstrap_resamples):
        counts = {cell: 0 for cell in occupied}
        sample_indices = generator.integers(0, population_size, size=population_size, endpoint=False)
        for sample_index in sample_indices.tolist():
            cell, _ = eligible[sample_index]
            counts[cell] += 1
        for cell in occupied:
            distributions[cell].append(counts[cell] / population_size)
    observed_counts = {cell: 0 for cell in occupied}
    for cell, _ in eligible:
        observed_counts[cell] += 1
    intervals = []
    for cell in occupied:
        lower, upper = _bootstrap_interval(distributions[cell])
        intervals.append(
            BootstrapCellInterval(
                cell,
                observed_counts[cell] / population_size,
                lower,
                upper,
            )
        )
    stable_omissions = tuple(sorted(omitted, key=lambda item: evidence_sort_key(item[0])))
    return BootstrapHeatmapInterval(
        intervals=tuple(intervals),
        algorithm_version=PCG64_BOOTSTRAP_VERSION,
        requested_algorithm_version=settings.bootstrap_algorithm_version,
        bit_generator="PCG64",
        numpy_version=NUMPY_VERSION,
        scipy_version=SCIPY_VERSION,
        seed_hex=seed_hex,
        resample_count=settings.bootstrap_resamples,
        statistic_name="occupied_cell_sample_proportion",
        input_evidence=_sorted_evidence([sample.evidence for _, sample in eligible]),
        omitted_evidence=tuple(item[0] for item in stable_omissions),
        quality="partial" if omitted else "complete",
        reason=stable_omissions[0][1] if stable_omissions else None,
        quantile_policy="scipy-scoreatpercentile-fraction-v1:p025-p975",
        map_content_sha256=projection.content_sha256,
        map_schema_version=projection.schema_version,
        engine_data_identity=projection.engine_data_identity,
    )
