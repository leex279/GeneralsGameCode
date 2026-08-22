"""Sole ORM adapter for immutable longitudinal analysis graphs."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import NAMESPACE_URL, uuid4, uuid5

import numpy as np
import scipy  # type: ignore[import-untyped]
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

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
    StrategyAssessment,
)
from generals_replay_analyzer.features.context import canonical_json
from generals_replay_analyzer.features.registry import BASE_REGISTRY, FeatureRegistry
from generals_replay_analyzer.identity.audit import identity_cache_digest
from generals_replay_analyzer.longitudinal.patterns import recurring_opening
from generals_replay_analyzer.longitudinal.segments import (
    LongitudinalInput,
    LongitudinalMemberDTO,
    LongitudinalRequest,
    LongitudinalResultDTO,
    LongitudinalRunReceipt,
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
    def __init__(self, session_factory: sessionmaker[Session], *, registry: FeatureRegistry = BASE_REGISTRY) -> None:
        self._session_factory = session_factory
        self._registry = registry

    def analyze(self, request: LongitudinalRequest) -> LongitudinalRunReceipt:
        if request.segment.opponent_player_public_id is not None or request.segment.opponent_faction is not None:
            raise LongitudinalAnalysisError("unsupported_team_opponent_relation")
        if "transition_preferences" in request.pattern_names:
            raise LongitudinalAnalysisError("ambiguous_feature_set_provenance")
        player_id, revision, selected = self._select(request)
        if not selected:
            raise LongitudinalAnalysisError("no_eligible_measurements")
        identity_token = identity_cache_digest(request.player_public_id, revision)
        input_value = LongitudinalInput(
            player_public_id=request.player_public_id,
            identity_revision=revision,
            identity_cache_token=identity_token,
            segment=request.segment,
            settings=request.settings,
            members=tuple(member.dto for member in selected),
        )
        digest = longitudinal_input_digest(input_value)
        cache = _cache_key(digest, request)
        cached = self._load_receipt(cache, request)
        if cached is not None:
            return cached
        evaluated = self._evaluate(request, selected, digest)
        try:
            return self._persist(request, player_id, revision, digest, cache, evaluated)
        except LongitudinalAnalysisError:
            raise
        except Exception as error:
            self._record_failure(request, player_id, revision, digest, cache)
            raise LongitudinalAnalysisError("persistence_failed") from error

    def _select(self, request: LongitudinalRequest) -> tuple[int, int, tuple[_SelectedMember, ...]]:
        source_names = set(request.metric_names)
        if "recurring_opening" in request.pattern_names:
            source_names.add("build.completed_sequence")
        with self._session_factory() as session:
            player = session.scalar(select(Player).where(Player.public_id == request.player_public_id))
            if player is None or player.retired_at is not None:
                raise LongitudinalAnalysisError("canonical_player_not_found")
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
            logical_seen: set[tuple[str, str]] = set()
            for feature, feature_set, replay_player, _parser, replay, evidence, map_row in rows:
                issues = tuple(
                    sorted(
                        session.scalars(
                            select(ReplayQualityIssue.issue_code).where(
                                ReplayQualityIssue.replay_id == replay.id,
                                ReplayQualityIssue.resolved_at.is_(None),
                            )
                        ).all()
                    )
                )
                if not request.segment.quality_policy.includes(
                    lifecycle_state=replay.lifecycle_state,
                    active_issue_codes=issues,
                ):
                    continue
                if not self._matches_segment(request, replay, replay_player, map_row):
                    continue
                if evidence.tier != "derived" or evidence.replay_id != replay.id:
                    continue
                if not self._has_direct_observed_input(session, feature, replay.id):
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
            return player.id, player.identity_revision, tuple(sorted(selected, key=lambda item: item.dto.sort_key()))

    @staticmethod
    def _has_direct_observed_input(session: Session, feature: Feature, replay_id: int) -> bool:
        refs = session.execute(
            select(FeatureEvidence, EvidenceItem)
            .join(EvidenceItem, FeatureEvidence.evidence_item_id == EvidenceItem.id)
            .where(FeatureEvidence.feature_id == feature.id, FeatureEvidence.role == "input")
        ).all()
        return bool(refs) and all(evidence.tier == "observed" and evidence.replay_id == replay_id for _, evidence in refs)

    @staticmethod
    def _matches_segment(request: LongitudinalRequest, replay: Replay, replay_player: ReplayPlayer, map_row: Map | None) -> bool:
        segment = request.segment
        observed = cast(dict[str, object], replay_player.observed_json)
        header = cast(dict[str, object], replay.header_json)
        if segment.subject_faction is not None and replay_player.faction != segment.subject_faction:
            return False
        if segment.subject_subfaction is not None and observed.get("subfaction", header.get("subfaction")) != segment.subject_subfaction:
            return False
        if segment.map_public_id is not None and (map_row is None or map_row.public_id != segment.map_public_id):
            return False
        if segment.start_position is not None and replay_player.start_position != segment.start_position:
            return False
        if segment.replay_version is not None and replay.version_string != segment.replay_version:
            return False
        if segment.replay_patch is not None and header.get("patch_identity") != segment.replay_patch:
            return False
        if segment.start_inclusive is not None:
            if replay.start_time is None:
                return False
            if not segment.start_inclusive <= replay.start_time < cast(int, segment.end_exclusive):
                return False
        return True

    def _evaluate(
        self,
        request: LongitudinalRequest,
        selected: tuple[_SelectedMember, ...],
        input_digest: str,
    ) -> tuple[_EvaluatedResult, ...]:
        results: list[_EvaluatedResult] = []
        for name in request.metric_names:
            members = tuple(item for item in selected if item.dto.feature_name == name)
            try:
                definition = self._registry.definition(name)
            except KeyError:
                results.append(_EvaluatedResult(name, "metric", 0, len(members), "unavailable", "unsupported_metric_definition", {}, members, 0, 0))
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
            if name == "recurring_opening":
                members = tuple(item for item in selected if item.dto.feature_name == "build.completed_sequence")
                result = recurring_opening(
                    tuple(_member_observation(member) for member in members),
                    prefix_length=3,
                    minimum_sample_size=request.settings.minimum_sample_size,
                    confidence_level=request.settings.confidence_level,
                )
                results.append(
                    _EvaluatedResult(
                        name,
                        "pattern",
                        result.sample_count,
                        result.missing_count,
                        result.quality,
                        result.reason,
                        result.statistics,
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
                    receipt = self._load_receipt(cache_key, request)
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
                    settings_json=request.settings.as_canonical(),
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
                        replay_id=evaluated.members[0].replay_id,
                        tier="derived",
                        source_kind="longitudinal",
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
                        statistics_json=dict(evaluated.statistics),
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
                winner = self._load_receipt(cache_key, request)
                if winner is not None:
                    return winner
                raise
            except Exception:
                session.rollback()
                raise
        receipt = self._load_receipt(cache_key, request)
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

    def _load_receipt(self, cache_key: str, request: LongitudinalRequest) -> LongitudinalRunReceipt | None:
        with self._session_factory() as session:
            run = session.scalar(
                select(LongitudinalRun).where(LongitudinalRun.cache_key == cache_key, LongitudinalRun.status == "succeeded")
            )
            if run is None:
                return None
            player = session.get(Player, run.player_id)
            if player is None or player.public_id != request.player_public_id:
                raise LongitudinalAnalysisError("cache_winner_invalid")
            result_rows = session.scalars(
                select(LongitudinalResult)
                .where(LongitudinalResult.longitudinal_run_id == run.id)
                .order_by(LongitudinalResult.result_kind, LongitudinalResult.result_name, LongitudinalResult.public_id)
            ).all()
            result_dtos: list[LongitudinalResultDTO] = []
            for result in result_rows:
                evidence = session.get(EvidenceItem, result.evidence_item_id)
                if evidence is None or evidence.tier != "derived" or evidence.source_kind != "longitudinal":
                    raise LongitudinalAnalysisError("cache_winner_invalid")
                member_rows = session.scalars(
                    select(LongitudinalMember).where(LongitudinalMember.longitudinal_result_id == result.id)
                ).all()
                member_dtos = tuple(
                    sorted(
                        (self._load_member(session, member, request) for member in member_rows),
                        key=LongitudinalMemberDTO.sort_key,
                    )
                )
                result_dtos.append(
                    LongitudinalResultDTO(
                        public_id=result.public_id,
                        result_name=result.result_name,
                        result_kind=result.result_kind,
                        sample_count=result.sample_count,
                        missing_count=result.missing_count,
                        quality="complete" if result.quality == "available" else cast(object, result.quality),  # type: ignore[arg-type]
                        reason=result.quality_reason,
                        statistics=cast(dict[str, object], result.statistics_json),
                        members=member_dtos,
                        evidence_public_id=evidence.public_id,
                    )
                )
            return LongitudinalRunReceipt(
                run_id=run.run_id,
                player_public_id=player.public_id,
                identity_revision=run.identity_revision,
                segment=request.segment,
                settings=request.settings,
                input_digest=run.input_digest,
                cache_key=run.cache_key,
                status="succeeded",
                results=tuple(result_dtos),
            )

    def _load_member(
        self, session: Session, member: LongitudinalMember, request: LongitudinalRequest
    ) -> LongitudinalMemberDTO:
        replay = session.get(Replay, member.replay_id)
        replay_player = session.get(ReplayPlayer, member.replay_player_id)
        feature_set = session.get(FeatureSet, member.feature_set_id)
        evidence = session.get(EvidenceItem, member.evidence_item_id)
        feature = None if member.feature_id is None else session.get(Feature, member.feature_id)
        assessment = None if member.strategy_assessment_id is None else session.get(StrategyAssessment, member.strategy_assessment_id)
        if replay is None or replay_player is None or feature_set is None or evidence is None:
            raise LongitudinalAnalysisError("cache_winner_invalid")
        if feature is None and assessment is None:
            raise LongitudinalAnalysisError("cache_winner_invalid")
        header = cast(dict[str, object], replay.header_json)
        if feature is not None:
            active_issue_codes = tuple(
                sorted(
                    session.scalars(
                        select(ReplayQualityIssue.issue_code).where(
                            ReplayQualityIssue.replay_id == replay.id,
                            ReplayQualityIssue.resolved_at.is_(None),
                        )
                    ).all()
                )
            )
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
            )
            if contract_valid:
                quality, reason, raw_value = _quality_for_member(
                    feature,
                    quality_floor=request.segment.quality_policy.quality_floor,
                    lifecycle_state=replay.lifecycle_state,
                    active_issue_codes=active_issue_codes,
                )
            else:
                quality, reason, raw_value = "unavailable", "unsupported_metric_definition", None
            map_public_id = None
            if replay.map_id is not None:
                map_row = session.get(Map, replay.map_id)
                map_public_id = None if map_row is None else map_row.public_id
            observed = cast(dict[str, object], replay_player.observed_json)
            return LongitudinalMemberDTO(
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
                subject_subfaction=cast(str | None, observed.get("subfaction", header.get("subfaction"))),
                map_public_id=map_public_id,
                start_position=replay_player.start_position,
                lifecycle_state=replay.lifecycle_state,
                active_issue_codes=active_issue_codes,
            )
        raise LongitudinalAnalysisError("ambiguous_feature_set_provenance")
