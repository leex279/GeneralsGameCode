"""Sole ORM adapter for immutable longitudinal analysis graphs."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import NAMESPACE_URL, uuid4, uuid5

import numpy as np
import scipy  # type: ignore[import-untyped]
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import (
    EvidenceItem,
    Feature,
    FeatureEvidence,
    FeatureSet,
    LongitudinalMember,
    LongitudinalResult,
    LongitudinalRun,
    Map,
    ParserRun,
    Player,
    Replay,
    ReplayPlayer,
    ReplayQualityIssue,
)
from generals_replay_analyzer.features.context import canonical_json
from generals_replay_analyzer.features.registry import BASE_REGISTRY, FeatureRegistry
from generals_replay_analyzer.identity.audit import identity_cache_digest
from generals_replay_analyzer.longitudinal.patterns import (
    change_point_candidate,
    consistency,
    map_position_habit,
    opponent_associated_difference,
    personal_baseline_difference,
    recurring_opening,
    timing_band,
    transition_preference,
    trend,
)
from generals_replay_analyzer.longitudinal.segments import (
    LongitudinalDefinitionDTO,
    LongitudinalEvidenceDTO,
    LongitudinalExclusionDTO,
    LongitudinalInput,
    LongitudinalMemberDTO,
    LongitudinalQualityIssueDTO,
    LongitudinalRequest,
    LongitudinalResultDTO,
    LongitudinalRunReceipt,
    LongitudinalSettings,
    SegmentKey,
    longitudinal_input_digest,
)
from generals_replay_analyzer.longitudinal.statistics import (
    LongitudinalObservation,
    StatisticalResult,
    summarize_categorical,
    summarize_continuous,
)

ANALYZER_NAME = "longitudinal-player-analysis"
ANALYZER_VERSION = "longitudinal-player-analysis-v1"
CACHE_SCHEMA = "longitudinal-cache-v1"

_PATTERN_DEFINITIONS: dict[str, tuple[str, str, str]] = {
    "recurring_opening": ("recurring-opening-prefix-wilson-v1", "recurring_opening", "build.completed_sequence"),
    "timing_band.build_first_completed": ("timing-band-median-bootstrap-v1", "timing_band", "build.first_completed_frame"),
    "transition_preferences": ("transition-preference-wilson-v1", "transition_preference", "production.completed_composition"),
    "map_position_habits": ("map-position-habit-wilson-v1", "map_position_habit", "expansion.completed_structure_positions"),
    "personal_baseline.economy_cash_change_total": ("median-difference-bootstrap-v1", "personal_baseline", "economy.cash_change_total"),
    "opponent_associated.economy_cash_change_total": ("median-difference-bootstrap-v1", "opponent_associated", "economy.cash_change_total"),
    "trend.economy_cash_change_total": ("theil-sen-bootstrap-v1", "trend", "economy.cash_change_total"),
    "change_point.economy_cash_change_total": ("median-difference-bootstrap-v1", "change_point", "economy.cash_change_total"),
    "consistency.economy_cash_change_total": ("iqr-over-median-v1", "consistency", "economy.cash_change_total"),
}


class LongitudinalAnalysisError(RuntimeError):
    """Typed failure that never exposes ORM or storage identities."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class _SelectedMember:
    dto: LongitudinalMemberDTO
    replay_id: int
    replay_player_id: int
    feature_set_id: int
    feature_id: int | None
    strategy_assessment_id: int | None
    evidence_item_id: int
    value_type: str


@dataclass(frozen=True)
class _CorpusAnchor:
    replay_id: int
    replay_public_id: str
    replay_sha256: str
    replay_player_id: int
    replay_player_public_id: str
    replay_start_time: int | None


@dataclass(frozen=True)
class _EvaluatedResult:
    name: str
    kind: str
    sample_count: int
    missing_count: int
    quality: str
    reason: str | None
    statistics: Mapping[str, object]
    members: tuple[_SelectedMember, ...]
    frame_start: int
    frame_end: int


def _raw_feature_value(feature: Feature) -> object:
    return {
        "integer": feature.integer_value,
        "real": feature.real_value,
        "text": feature.text_value,
        "boolean": feature.boolean_value,
        "json": feature.json_value,
    }[feature.value_type]


def _canonical_mapping(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _quality_for_member(
    feature: Feature,
    *,
    quality_floor: str,
    lifecycle_state: str,
    active_issue_codes: tuple[str, ...],
) -> tuple[str, str | None, object]:
    if feature.quality == "unavailable":
        return "unavailable", feature.quality_reason or "feature_unavailable", None
    if feature.quality == "partial" and quality_floor == "complete":
        return "unavailable", "below_quality_floor", None
    quality = "complete" if feature.quality == "available" else "partial"
    reason = feature.quality_reason
    if lifecycle_state != "engine_verified" or active_issue_codes:
        quality = "partial"
        reasons = tuple(sorted(active_issue_codes or (lifecycle_state,)))
        reason = "included_quality_condition:" + ",".join(reasons)
    return quality, reason, _raw_feature_value(feature)


def _member_observation(member: _SelectedMember) -> LongitudinalObservation:
    return LongitudinalObservation(
        member_key="|".join(
            (
                member.dto.replay_sha256,
                member.dto.replay_player_public_id,
                member.dto.feature_public_id or member.dto.strategy_assessment_public_id or "",
            )
        ),
        evidence_public_id=member.dto.evidence_public_id,
        raw_value=member.dto.raw_value,
        quality=member.dto.quality,
        reason=member.dto.reason,
    )


def _cache_key(input_digest: str, request: LongitudinalRequest) -> str:
    identity = {
        "cache_schema": CACHE_SCHEMA,
        "input_digest": input_digest,
        "analyzer_name": ANALYZER_NAME,
        "analyzer_version": ANALYZER_VERSION,
        "statistics_algorithm_versions": {
            "bootstrap": request.settings.bootstrap_algorithm_version,
            "trend": request.settings.trend_algorithm_version,
            "change_point": request.settings.change_point_algorithm_version,
            "consistency": request.settings.consistency_algorithm_version,
        },
        "numpy_version": np.__version__,
        "scipy_version": scipy.__version__,
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


# TheSuperHackers @feature Leex 22/08/2026 Persist revision-bound longitudinal evidence without mutating history. (#0)
class LongitudinalAnalysisService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        analyzer_settings: AnalyzerSettings,
        registry: FeatureRegistry = BASE_REGISTRY,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._minimum_sample_size = analyzer_settings.minimum_longitudinal_sample_size

    def analyze(self, request: LongitudinalRequest) -> LongitudinalRunReceipt:
        if request.settings.minimum_sample_size != self._minimum_sample_size:
            raise LongitudinalAnalysisError("minimum_sample_size_mismatch")
        definitions = self._definitions(request)
        player_id, revision, selected, anchors, exclusions = self._select(request, definitions)
        if not anchors:
            raise LongitudinalAnalysisError("no_corpus_anchor")
        identity_token = identity_cache_digest(request.player_public_id, revision)
        input_value = LongitudinalInput(
            player_public_id=request.player_public_id,
            identity_revision=revision,
            identity_cache_token=identity_token,
            segment=request.segment,
            settings=request.settings,
            metric_names=request.metric_names,
            pattern_names=request.pattern_names,
            definitions=definitions,
            members=tuple(member.dto for member in selected),
            exclusions=exclusions,
        )
        digest = longitudinal_input_digest(input_value)
        cache = _cache_key(digest, request)
        cached = self._load_receipt(cache)
        if cached is not None:
            return cached
        evaluated = self._evaluate(request, selected, anchors, digest)
        try:
            return self._persist(request, definitions, exclusions, anchors[0], player_id, revision, digest, cache, evaluated)
        except LongitudinalAnalysisError:
            raise
        except Exception as error:
            self._record_failure(request, player_id, revision, digest, cache)
            raise LongitudinalAnalysisError("persistence_failed") from error

    def _definitions(self, request: LongitudinalRequest) -> tuple[LongitudinalDefinitionDTO, ...]:
        definitions: list[LongitudinalDefinitionDTO] = []
        for name in request.metric_names:
            try:
                feature = self._registry.definition(name)
            except KeyError:
                definitions.append(LongitudinalDefinitionDTO(name, "metric", "unsupported-v1", (), "unsupported"))
            else:
                definitions.append(
                    LongitudinalDefinitionDTO(
                        name,
                        "metric",
                        f"{self._registry.schema_version}:{name}",
                        (name,),
                        "continuous_summary" if feature.value_type in ("integer", "real") else "categorical_summary",
                        feature.value_type,
                        feature.unit,
                        feature.scope_types,
                    )
                )
        for name in request.pattern_names:
            fixed = _PATTERN_DEFINITIONS.get(name)
            if fixed is None:
                definitions.append(LongitudinalDefinitionDTO(name, "pattern", "unsupported-v1", (), "unsupported"))
            else:
                version, algorithm, source = fixed
                definitions.append(LongitudinalDefinitionDTO(name, "pattern", version, (source,), algorithm))
        return tuple(sorted(definitions, key=lambda item: (item.result_kind, item.public_name)))

    def _select(
        self,
        request: LongitudinalRequest,
        definitions: tuple[LongitudinalDefinitionDTO, ...],
    ) -> tuple[int, int, tuple[_SelectedMember, ...], tuple[_CorpusAnchor, ...], tuple[LongitudinalExclusionDTO, ...]]:
        source_names = {source for definition in definitions for source in definition.source_feature_names}
        chronology_requested = any(
            definition.algorithm_kind in {"trend", "change_point"} for definition in definitions
        )
        with self._session_factory() as session:
            player = session.scalar(select(Player).where(Player.public_id == request.player_public_id))
            if player is None or player.retired_at is not None:
                raise LongitudinalAnalysisError("canonical_player_not_found")
            corpus_rows = session.execute(
                select(ReplayPlayer, ParserRun, Replay)
                .join(ParserRun, ReplayPlayer.parser_run_id == ParserRun.id)
                .join(Replay, ReplayPlayer.replay_id == Replay.id)
                .where(ReplayPlayer.player_id == player.id, ParserRun.status == "succeeded", ParserRun.replay_id == Replay.id)
            ).all()
            anchors = tuple(
                sorted(
                    (
                        _CorpusAnchor(replay.id, replay.public_id, replay.sha256, replay_player.id, replay_player.public_id, replay.start_time)
                        for replay_player, _parser, replay in corpus_rows
                    ),
                    key=lambda item: (item.replay_sha256, item.replay_player_public_id),
                )
            )
            rows = session.execute(
                select(Feature, FeatureSet, ReplayPlayer, ParserRun, Replay, EvidenceItem, Map)
                .join(FeatureSet, Feature.feature_set_id == FeatureSet.id)
                .join(ReplayPlayer, FeatureSet.replay_player_id == ReplayPlayer.id)
                .join(ParserRun, ReplayPlayer.parser_run_id == ParserRun.id)
                .join(Replay, FeatureSet.replay_id == Replay.id)
                .join(EvidenceItem, Feature.evidence_item_id == EvidenceItem.id)
                .outerjoin(Map, Replay.map_id == Map.id)
                .where(
                    ReplayPlayer.player_id == player.id,
                    FeatureSet.status == "succeeded",
                    ParserRun.status == "succeeded",
                    ParserRun.replay_id == Replay.id,
                    ReplayPlayer.replay_id == Replay.id,
                    Feature.replay_player_id == ReplayPlayer.id,
                    Feature.name.in_(source_names),
                )
            ).all()
            selected: list[_SelectedMember] = []
            exclusions: list[LongitudinalExclusionDTO] = []
            logical_seen: set[tuple[str, str]] = set()
            for feature, feature_set, replay_player, _parser, replay, evidence, map_row in rows:
                issue_rows = session.scalars(
                    select(ReplayQualityIssue).where(ReplayQualityIssue.replay_id == replay.id)
                ).all()
                issues = tuple(sorted({issue.issue_code for issue in issue_rows if issue.resolved_at is None}))
                if not request.segment.quality_policy.includes(
                    lifecycle_state=replay.lifecycle_state,
                    active_issue_codes=issues,
                ):
                    continue
                if (request.segment.start_inclusive is not None or chronology_requested) and replay.start_time is None:
                    exclusions.append(
                        LongitudinalExclusionDTO(
                            replay.public_id,
                            replay_player.public_id,
                            "missing_replay_start_time",
                            (None, replay.sha256, replay_player.public_id),
                        )
                    )
                if evidence.tier != "derived" or evidence.replay_id != replay.id:
                    continue
                direct_evidence = self._direct_observed_input(session, feature, replay.id)
                if not direct_evidence:
                    continue
                logical = (replay_player.public_id, feature.name)
                if logical in logical_seen:
                    raise LongitudinalAnalysisError("duplicate_logical_measurement")
                logical_seen.add(logical)
                try:
                    definition = self._registry.definition(feature.name)
                except KeyError:
                    definition = None
                contract_valid = (
                    definition is not None
                    and feature.value_type == definition.value_type
                    and feature.unit == definition.unit
                    and feature.scope_type in definition.scope_types
                    and feature.scope_type == "player"
                    and feature.scope_key == replay_player.public_id
                    and feature.frame_start >= 0
                    and feature.frame_end >= feature.frame_start
                )
                if contract_valid:
                    quality, reason, raw_value = _quality_for_member(
                        feature,
                        quality_floor=request.segment.quality_policy.quality_floor,
                        lifecycle_state=replay.lifecycle_state,
                        active_issue_codes=issues,
                    )
                else:
                    quality, reason, raw_value = "unavailable", "unsupported_metric_definition", None
                header = _canonical_mapping(replay.header_json)
                dto = LongitudinalMemberDTO(
                    replay_public_id=replay.public_id,
                    replay_sha256=replay.sha256,
                    replay_player_public_id=replay_player.public_id,
                    feature_set_public_id=feature_set.public_id,
                    feature_public_id=feature.public_id,
                    evidence_public_id=evidence.public_id,
                    feature_name=feature.name,
                    raw_value=raw_value,
                    unit=feature.unit,
                    frame_start=feature.frame_start,
                    frame_end=feature.frame_end,
                    quality=cast(object, quality),  # type: ignore[arg-type]
                    reason=reason,
                    replay_start_time=replay.start_time,
                    replay_version=replay.version_string,
                    replay_patch=cast(str | None, header.get("patch_identity")),
                    subject_faction=replay_player.faction,
                    subject_subfaction=cast(str | None, replay_player.observed_json.get("subfaction", header.get("subfaction"))),
                    map_public_id=None if map_row is None else map_row.public_id,
                    start_position=replay_player.start_position,
                    lifecycle_state=replay.lifecycle_state,
                    active_issue_codes=issues,
                    feature_set_extractor_name=feature_set.extractor_name,
                    feature_set_extractor_version=feature_set.extractor_version,
                    feature_set_input_digest=feature_set.input_digest,
                    feature_set_settings=_canonical_mapping(feature_set.settings_json),
                    feature_value_type=feature.value_type,
                    feature_scope_type=feature.scope_type,
                    feature_scope_key=feature.scope_key,
                    feature_details=_canonical_mapping(feature.details_json),
                    derived_evidence_source_kind=evidence.source_kind,
                    derived_evidence_source_key=evidence.source_key,
                    derived_evidence_schema_version=evidence.schema_version,
                    direct_evidence=direct_evidence,
                    quality_issues=tuple(
                        LongitudinalQualityIssueDTO(
                            issue.public_id,
                            issue.stage,
                            issue.issue_code,
                            issue.severity,
                            issue.resolved_at is not None,
                            None
                            if issue.evidence_item_id is None
                            else cast(EvidenceItem, session.get(EvidenceItem, issue.evidence_item_id)).public_id,
                            _canonical_mapping(issue.details_json),
                        )
                        for issue in issue_rows
                    ),
                )
                selected.append(
                    _SelectedMember(
                        dto,
                        replay.id,
                        replay_player.id,
                        feature_set.id,
                        feature.id,
                        None,
                        evidence.id,
                        feature.value_type,
                    )
                )
            return (
                player.id,
                player.identity_revision,
                tuple(sorted(selected, key=lambda item: item.dto.sort_key())),
                anchors,
                tuple(sorted(set(exclusions), key=lambda item: (item.chronology_key, item.reason))),
            )

    @staticmethod
    def _direct_observed_input(
        session: Session, feature: Feature, replay_id: int
    ) -> tuple[LongitudinalEvidenceDTO, ...]:
        refs = session.execute(
            select(FeatureEvidence, EvidenceItem)
            .join(EvidenceItem, FeatureEvidence.evidence_item_id == EvidenceItem.id)
            .where(FeatureEvidence.feature_id == feature.id, FeatureEvidence.role == "input")
        ).all()
        if not refs or not all(evidence.tier == "observed" and evidence.replay_id == replay_id for _, evidence in refs):
            return ()
        return tuple(
            LongitudinalEvidenceDTO(
                evidence.public_id,
                evidence.tier,
                evidence.source_kind,
                evidence.source_key,
                evidence.schema_version,
            )
            for _, evidence in refs
        )

    @staticmethod
    def _dto_matches_segment(request: LongitudinalRequest, member: LongitudinalMemberDTO) -> bool:
        segment = request.segment
        return not (
            (segment.subject_faction is not None and member.subject_faction != segment.subject_faction)
            or (segment.subject_subfaction is not None and member.subject_subfaction != segment.subject_subfaction)
            or (segment.opponent_faction is not None and member.opponent_faction != segment.opponent_faction)
            or (
                segment.opponent_player_public_id is not None
                and member.opponent_player_public_id != segment.opponent_player_public_id
            )
            or (segment.map_public_id is not None and member.map_public_id != segment.map_public_id)
            or (segment.start_position is not None and member.start_position != segment.start_position)
            or (segment.replay_version is not None and member.replay_version != segment.replay_version)
            or (segment.replay_patch is not None and member.replay_patch != segment.replay_patch)
            or (
                segment.start_inclusive is not None
                and (
                    member.replay_start_time is None
                    or not segment.start_inclusive <= member.replay_start_time < cast(int, segment.end_exclusive)
                )
            )
        )

    def _evaluate(
        self,
        request: LongitudinalRequest,
        selected: tuple[_SelectedMember, ...],
        anchors: tuple[_CorpusAnchor, ...],
        input_digest: str,
    ) -> tuple[_EvaluatedResult, ...]:
        results: list[_EvaluatedResult] = []
        relation_requested = bool(request.segment.opponent_player_public_id or request.segment.opponent_faction)
        relation_available = any(
            item.dto.opponent_player_public_id or item.dto.opponent_faction for item in selected
        )
        if relation_requested and not relation_available:
            return tuple(
                _EvaluatedResult(
                    name,
                    kind,
                    0,
                    len(anchors),
                    "unavailable",
                    "unsupported_team_opponent_relation",
                    {"focal_cohort_count": 0, "reference_cohort_count": 0, "controls_exact": False},
                    (),
                    0,
                    0,
                )
                for kind, names in (("metric", request.metric_names), ("pattern", request.pattern_names))
                for name in names
            )
        focal = tuple(item for item in selected if self._dto_matches_segment(request, item.dto))
        for name in request.metric_names:
            members = tuple(item for item in focal if item.dto.feature_name == name)
            try:
                definition = self._registry.definition(name)
            except KeyError:
                results.append(_EvaluatedResult(name, "metric", 0, len(anchors), "unavailable", "unsupported_metric_definition", {}, (), 0, 0))
                continue
            observations = tuple(_member_observation(member) for member in members)
            if definition.value_type in ("integer", "real"):
                summary = summarize_continuous(
                    name,
                    observations,
                    input_digest=input_digest,
                    minimum_sample_size=request.settings.minimum_sample_size,
                    bootstrap_resamples=request.settings.bootstrap_resamples,
                    confidence_level=request.settings.confidence_level,
                    algorithm_version=request.settings.bootstrap_algorithm_version,
                )
            else:
                summary = summarize_categorical(
                    name,
                    observations,
                    minimum_sample_size=request.settings.minimum_sample_size,
                    confidence_level=request.settings.confidence_level,
                    algorithm_version="wilson-v1",
                )
            results.append(self._from_statistical(summary, members, "metric"))
        for name in request.pattern_names:
            pattern_definition = _PATTERN_DEFINITIONS.get(name)
            if pattern_definition is None:
                results.append(_EvaluatedResult(name, "pattern", 0, len(anchors), "unavailable", "unsupported_pattern_definition", {}, (), 0, 0))
                continue
            _version, algorithm, source_name = pattern_definition
            members = tuple(item for item in focal if item.dto.feature_name == source_name)
            observations = tuple(_member_observation(member) for member in members)
            if algorithm == "recurring_opening":
                result = recurring_opening(
                    observations,
                    prefix_length=3,
                    minimum_sample_size=request.settings.minimum_sample_size,
                    confidence_level=request.settings.confidence_level,
                )
                pattern_result = result
            elif algorithm == "timing_band":
                first = members[0].dto if members else None
                pattern_result = timing_band(
                    source_name,
                    observations,
                    input_digest=input_digest,
                    minimum_sample_size=request.settings.minimum_sample_size,
                    bootstrap_resamples=request.settings.bootstrap_resamples,
                    confidence_level=request.settings.confidence_level,
                    source_unit="frames" if first is None else cast(str, first.unit),
                    source_scope="player" if first is None else first.feature_scope_type,
                    member_windows=tuple((item.dto.frame_start, item.dto.frame_end) for item in members),
                )
            elif algorithm == "transition_preference":
                pattern_result = transition_preference(
                    source_name,
                    observations,
                    minimum_sample_size=request.settings.minimum_sample_size,
                    confidence_level=request.settings.confidence_level,
                    source_kind="feature",
                    assessment_method=None,
                    deterministic_feature_set_provenance=True,
                    registry=self._registry,
                )
            elif algorithm == "map_position_habit":
                pattern_result = map_position_habit(
                    source_name,
                    observations,
                    minimum_sample_size=request.settings.minimum_sample_size,
                    confidence_level=request.settings.confidence_level,
                    map_public_id=request.segment.map_public_id,
                    registry=self._registry,
                )
            elif algorithm == "personal_baseline":
                reference = tuple(item for item in selected if item.dto.feature_name == source_name and item not in focal)
                pattern_result = personal_baseline_difference(
                    observations,
                    tuple(_member_observation(item) for item in reference),
                    input_digest=input_digest,
                    minimum_sample_size=request.settings.minimum_sample_size,
                    bootstrap_resamples=request.settings.bootstrap_resamples,
                    confidence_level=request.settings.confidence_level,
                )
                members = members + reference
            elif algorithm == "opponent_associated":
                pattern_result = opponent_associated_difference(
                    observations,
                    (),
                    opponent_player_public_id=request.segment.opponent_player_public_id,
                    controls_exact=True,
                    input_digest=input_digest,
                    minimum_sample_size=request.settings.minimum_sample_size,
                    bootstrap_resamples=request.settings.bootstrap_resamples,
                    confidence_level=request.settings.confidence_level,
                )
            elif algorithm == "trend":
                chronological = tuple(
                    sorted(
                        (item for item in members if item.dto.replay_start_time is not None),
                        key=lambda item: (cast(int, item.dto.replay_start_time), item.dto.replay_sha256, item.dto.replay_player_public_id),
                    )
                )
                pattern_result = trend(
                    tuple(_member_observation(item) for item in chronological),
                    tuple(cast(int, item.dto.replay_start_time) for item in chronological),
                    input_digest=input_digest,
                    minimum_sample_size=request.settings.minimum_sample_size,
                    bootstrap_resamples=request.settings.bootstrap_resamples,
                    confidence_level=request.settings.confidence_level,
                )
                members = chronological
            elif algorithm == "change_point":
                chronological = tuple(
                    sorted(
                        (item for item in members if item.dto.replay_start_time is not None),
                        key=lambda item: (cast(int, item.dto.replay_start_time), item.dto.replay_sha256, item.dto.replay_player_public_id),
                    )
                )
                pattern_result = change_point_candidate(
                    tuple(_member_observation(item) for item in chronological),
                    tuple(cast(int, item.dto.replay_start_time) for item in chronological),
                    input_digest=input_digest,
                    minimum_sample_size=request.settings.minimum_sample_size,
                    bootstrap_resamples=request.settings.bootstrap_resamples,
                    confidence_level=request.settings.confidence_level,
                )
                members = chronological
            else:
                pattern_result = consistency(observations, minimum_sample_size=request.settings.minimum_sample_size)
            results.append(
                _EvaluatedResult(
                    name,
                    "pattern",
                    pattern_result.sample_count,
                    pattern_result.missing_count,
                    pattern_result.quality,
                    pattern_result.reason,
                    pattern_result.statistics,
                    members,
                    min((item.dto.frame_start for item in members), default=0),
                    max((item.dto.frame_end for item in members), default=0),
                )
            )
        return tuple(sorted(results, key=lambda item: (item.kind, item.name)))

    @staticmethod
    def _from_statistical(
        summary: StatisticalResult,
        members: tuple[_SelectedMember, ...],
        kind: str,
    ) -> _EvaluatedResult:
        return _EvaluatedResult(
            summary.name,
            kind,
            summary.sample_count,
            summary.missing_count,
            summary.quality,
            summary.reason,
            summary.statistics,
            members,
            min((item.dto.frame_start for item in members), default=0),
            max((item.dto.frame_end for item in members), default=0),
        )

    def _persist(
        self,
        request: LongitudinalRequest,
        definitions: tuple[LongitudinalDefinitionDTO, ...],
        exclusions: tuple[LongitudinalExclusionDTO, ...],
        anchor: _CorpusAnchor,
        player_id: int,
        revision: int,
        input_digest: str,
        cache_key: str,
        results: tuple[_EvaluatedResult, ...],
    ) -> LongitudinalRunReceipt:
        with self._session_factory() as session:
            try:
                session.execute(text("BEGIN IMMEDIATE"))
                existing = session.scalar(
                    select(LongitudinalRun).where(LongitudinalRun.cache_key == cache_key, LongitudinalRun.status == "succeeded")
                )
                if existing is not None:
                    session.rollback()
                    receipt = self._load_receipt(cache_key)
                    if receipt is None:
                        raise LongitudinalAnalysisError("cache_winner_invalid")
                    return receipt
                current_revision = session.scalar(select(Player.identity_revision).where(Player.id == player_id))
                if current_revision != revision:
                    raise LongitudinalAnalysisError("identity_revision_changed")
                run_id = str(uuid4())
                run = LongitudinalRun(
                    run_id=run_id,
                    player_id=player_id,
                    identity_revision=revision,
                    analyzer_name=ANALYZER_NAME,
                    analyzer_version=ANALYZER_VERSION,
                    segment_key_json=request.segment.as_canonical(),
                    settings_json={
                        "storage_schema": "longitudinal-run-settings-v1",
                        "player_public_id": request.player_public_id,
                        "settings": request.settings.as_canonical(),
                        "metric_names": list(request.metric_names),
                        "pattern_names": list(request.pattern_names),
                        "definitions": [item.as_canonical() for item in definitions],
                        "exclusions": [asdict(item) for item in exclusions],
                    },
                    input_digest=input_digest,
                    cache_key=cache_key,
                    status="pending",
                    created_at=datetime.now(UTC),
                )
                session.add(run)
                session.flush()
                for evaluated in results:
                    result_public_id = str(uuid5(NAMESPACE_URL, f"{run_id}:{cache_key}:{evaluated.kind}:{evaluated.name}"))
                    result_source_key = (
                        f"longitudinal:{run_id}:{cache_key}:{evaluated.name}:{evaluated.kind}:"
                        f"{evaluated.frame_start}:{evaluated.frame_end}"
                    )
                    evidence = EvidenceItem(
                        public_id=str(uuid5(NAMESPACE_URL, result_source_key)),
                        replay_id=anchor.replay_id,
                        tier="derived",
                        source_kind="longitudinal_corpus",
                        source_key=result_source_key,
                        schema_version=1,
                        created_at=datetime.now(UTC),
                    )
                    session.add(evidence)
                    session.flush()
                    result = LongitudinalResult(
                        public_id=result_public_id,
                        longitudinal_run_id=run.id,
                        evidence_item_id=evidence.id,
                        result_name=evaluated.name,
                        result_kind=evaluated.kind,
                        sample_count=evaluated.sample_count,
                        missing_count=evaluated.missing_count,
                        quality="available" if evaluated.quality == "complete" else evaluated.quality,
                        quality_reason=evaluated.reason,
                        statistics_json={
                            "storage_schema": "longitudinal-result-storage-v1",
                            "public_statistics": dict(evaluated.statistics),
                            "member_snapshots": [member.dto.as_canonical() for member in evaluated.members],
                            "evidence_anchor": {
                                "role": "schema_required_corpus_anchor",
                                "replay_public_id": anchor.replay_public_id,
                                "replay_sha256": anchor.replay_sha256,
                                "replay_player_public_id": anchor.replay_player_public_id,
                            },
                        },
                    )
                    session.add(result)
                    session.flush()
                    for member in evaluated.members:
                        session.add(
                            LongitudinalMember(
                                longitudinal_result_id=result.id,
                                replay_id=member.replay_id,
                                replay_player_id=member.replay_player_id,
                                feature_set_id=member.feature_set_id,
                                feature_id=member.feature_id,
                                strategy_assessment_id=member.strategy_assessment_id,
                                evidence_item_id=member.evidence_item_id,
                            )
                        )
                run.status = "succeeded"
                run.completed_at = datetime.now(UTC)
                session.commit()
            except LongitudinalAnalysisError:
                session.rollback()
                raise
            except IntegrityError:
                session.rollback()
                winner = self._load_receipt(cache_key)
                if winner is not None:
                    return winner
                raise
            except Exception:
                session.rollback()
                raise
        receipt = self._load_receipt(cache_key)
        if receipt is None:
            raise LongitudinalAnalysisError("persistence_failed")
        return receipt

    def _record_failure(
        self,
        request: LongitudinalRequest,
        player_id: int,
        revision: int,
        input_digest: str,
        cache_key: str,
    ) -> None:
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            session.add(
                LongitudinalRun(
                    run_id=str(uuid4()),
                    player_id=player_id,
                    identity_revision=revision,
                    analyzer_name=ANALYZER_NAME,
                    analyzer_version=ANALYZER_VERSION,
                    segment_key_json=request.segment.as_canonical(),
                    settings_json=request.settings.as_canonical(),
                    input_digest=input_digest,
                    cache_key=cache_key,
                    status="failed",
                    created_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                    error_json={"code": "persistence_failed"},
                )
            )
            session.commit()

    def _load_receipt(self, cache_key: str) -> LongitudinalRunReceipt | None:
        with self._session_factory() as session:
            run = session.scalar(
                select(LongitudinalRun).where(LongitudinalRun.cache_key == cache_key, LongitudinalRun.status == "succeeded")
            )
            if run is None:
                return None
            stored_settings = _canonical_mapping(run.settings_json)
            if stored_settings.get("storage_schema") != "longitudinal-run-settings-v1":
                raise LongitudinalAnalysisError("cache_winner_invalid")
            player_public_id = cast(str, stored_settings.get("player_public_id"))
            settings_value = stored_settings.get("settings")
            if not isinstance(settings_value, Mapping):
                raise LongitudinalAnalysisError("cache_winner_invalid")
            settings = LongitudinalSettings.from_mapping(settings_value)
            exclusion_values = stored_settings.get("exclusions")
            if not isinstance(exclusion_values, list):
                raise LongitudinalAnalysisError("cache_winner_invalid")
            exclusions = tuple(
                LongitudinalExclusionDTO.from_mapping(cast(Mapping[str, object], item))
                for item in exclusion_values
            )
            segment_value = _canonical_mapping(run.segment_key_json)
            segment = SegmentKey.from_mapping(segment_value)
            result_rows = session.scalars(
                select(LongitudinalResult)
                .where(LongitudinalResult.longitudinal_run_id == run.id)
                .order_by(LongitudinalResult.result_kind, LongitudinalResult.result_name, LongitudinalResult.public_id)
            ).all()
            result_dtos: list[LongitudinalResultDTO] = []
            for result in result_rows:
                evidence = session.get(EvidenceItem, result.evidence_item_id)
                if evidence is None or evidence.tier != "derived" or evidence.source_kind != "longitudinal_corpus":
                    raise LongitudinalAnalysisError("cache_winner_invalid")
                storage = _canonical_mapping(result.statistics_json)
                if storage.get("storage_schema") != "longitudinal-result-storage-v1":
                    raise LongitudinalAnalysisError("cache_winner_invalid")
                member_values = storage.get("member_snapshots")
                public_statistics = storage.get("public_statistics")
                if not isinstance(member_values, list) or not isinstance(public_statistics, Mapping):
                    raise LongitudinalAnalysisError("cache_winner_invalid")
                member_dtos = tuple(LongitudinalMemberDTO.from_mapping(item) for item in member_values)
                public_statistics_dict = dict(public_statistics)
                anchor = storage.get("evidence_anchor")
                if isinstance(anchor, Mapping):
                    public_statistics_dict.setdefault("evidence_anchor_role", anchor.get("role"))
                result_dtos.append(
                    LongitudinalResultDTO(
                        public_id=result.public_id,
                        result_name=result.result_name,
                        result_kind=result.result_kind,
                        sample_count=result.sample_count,
                        missing_count=result.missing_count,
                        quality="complete" if result.quality == "available" else cast(object, result.quality),  # type: ignore[arg-type]
                        reason=result.quality_reason,
                        statistics=public_statistics_dict,
                        members=member_dtos,
                        evidence_public_id=evidence.public_id,
                    )
                )
            return LongitudinalRunReceipt(
                run_id=run.run_id,
                player_public_id=player_public_id,
                identity_revision=run.identity_revision,
                segment=segment,
                settings=settings,
                input_digest=run.input_digest,
                cache_key=run.cache_key,
                status="succeeded",
                exclusions=exclusions,
                results=tuple(result_dtos),
            )
