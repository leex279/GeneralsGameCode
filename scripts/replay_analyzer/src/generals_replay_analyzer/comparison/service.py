"""Definition-aligned replay, cohort, opening, strategy, and period comparisons."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db.models import (
    EvidenceItem,
    Feature,
    FeatureSet,
    LongitudinalMember,
    LongitudinalResult,
    LongitudinalRun,
    Map,
    Player,
    Replay,
    ReplayPlayer,
    Report,
    StrategyAssessment,
)

ComparisonKind = Literal["players", "matches", "openings", "strategies", "time_periods"]
ComparisonState = Literal["comparable", "partial", "not_comparable", "unavailable"]
_COMPARISON_NAMESPACE = UUID("78942fb7-97ae-5d0f-9f07-83436e3f5956")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            _canonical_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _mapping(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _canonical_value(value: object) -> object:
    if isinstance(value, datetime):
        if value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("comparison datetime must use UTC")
        return value.isoformat().replace("+00:00", "Z")
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _canonical_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ComparisonDefinition:
    definition_id: str
    definition_version: str
    unit: str | None
    scope_type: str
    window_policy_version: str
    faction_comparability: Literal["same_faction_only", "declared_cross_faction"]
    definition_kind: Literal["feature", "opening", "strategy", "trend", "match_metric"] = "feature"
    taxonomy_version: str | None = None


@dataclass(frozen=True, slots=True)
class DistributionInterval:
    lower: float
    upper: float
    confidence_level: float
    method: str
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class ComparisonValue:
    raw_value: object | None
    unit: str | None
    sample_count: int
    missing_count: int
    availability: Literal["available", "partial", "unavailable"]
    evidence_public_ids: tuple[str, ...]
    interval: DistributionInterval | None = None
    quality_exclusion_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ComparisonInput:
    kind: ComparisonKind
    definition: ComparisonDefinition
    left: ComparisonValue
    right: ComparisonValue
    minimum_sample_size: int
    utc_source_proven: bool
    same_faction: bool = True


@dataclass(frozen=True, slots=True)
class ComparedValue:
    definition: ComparisonDefinition
    left: ComparisonValue
    right: ComparisonValue
    derived_difference: object | None
    difference_evidence_public_ids: tuple[str, ...]
    state: ComparisonState
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LongitudinalSubject:
    subject_kind: Literal["player_cohort", "segment_baseline", "opening", "strategy", "time_period"]
    run_id: str
    player_public_id: str
    identity_revision: int
    result_public_id: str | None = None
    start_inclusive_utc: datetime | None = None
    end_exclusive_utc: datetime | None = None
    taxonomy_version: str | None = None
    rule_version: str | None = None
    analyzer_name: str = "unknown"
    analyzer_version: str = "unknown"
    segment_digest: str = "0" * 64
    quality_policy_digest: str = "0" * 64
    input_digest: str = "0" * 64
    cache_key: str = "0" * 64
    statistics_algorithm_versions: tuple[str, ...] = ("unknown-v1",)
    baseline_public_id: str | None = None
    population_definition_version: str | None = None
    opening_definition_id: str | None = None
    opening_definition_version: str | None = None
    strategy_id: str | None = None


@dataclass(frozen=True, slots=True)
class MatchSubject:
    subject_kind: Literal["match"]
    replay_public_id: str
    replay_player_public_id: str
    report_public_id: str
    report_input_digest: str
    feature_set_public_ids: tuple[str, ...]
    document_schema_version: str = "replay-report-v1"
    report_version: str = "replay-report-v1"
    display_policy_version: str = "replay-comparison-display-v1"


ComparisonSubject = LongitudinalSubject | MatchSubject


@dataclass(frozen=True, slots=True)
class FixedComparisonQuery:
    kind: ComparisonKind
    left: ComparisonSubject
    right: ComparisonSubject
    metric_definitions: tuple[ComparisonDefinition, ...]
    minimum_sample_size: int


@dataclass(frozen=True, slots=True)
class ComparisonFilters:
    faction: str | None = None
    subfaction: str | None = None
    opponent_faction: str | None = None
    opponent_player_public_id: str | None = None
    map_public_id: str | None = None
    start_position: str | None = None
    patch: str | None = None
    date_from_utc: datetime | None = None
    date_to_utc: datetime | None = None
    quality_policy_digest: str | None = None


@dataclass(frozen=True, slots=True)
class ComparisonSelection:
    kind: ComparisonKind
    left_public_id: str | None
    right_public_id: str | None
    baseline_requested: bool
    metric_definition_ids: tuple[str, ...]
    minimum_sample_size: int
    filters: ComparisonFilters = ComparisonFilters()


@dataclass(frozen=True, slots=True)
class ComparisonResolution:
    state: Literal["resolved", "unavailable"]
    fixed_query: FixedComparisonQuery | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ComparisonMetric:
    section: str
    metric_id: str
    label: str
    value_kind: str
    value: ComparedValue


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    comparison_public_id: str
    query_digest: str
    input_digest: str
    query: FixedComparisonQuery
    state: ComparisonState
    reason_codes: tuple[str, ...]
    metrics: tuple[ComparisonMetric, ...]


# TheSuperHackers @feature Leex 23/08/2026 Align exact fixed comparison bindings before exposing any numerical delta. (#TBD)
class ReplayComparisonService:
    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        minimum_sample_size: int = 3,
    ) -> None:
        if minimum_sample_size < 1:
            raise ValueError("minimum sample size must be positive")
        self._session_factory = session_factory
        self._minimum_sample_size = minimum_sample_size

    def resolve(self, selection: ComparisonSelection) -> ComparisonResolution:
        if selection.minimum_sample_size != self._minimum_sample_size:
            return ComparisonResolution("unavailable", None, ("minimum_sample_size_not_accepted",))
        if selection.left_public_id is None or (selection.right_public_id is None and not selection.baseline_requested):
            return ComparisonResolution("unavailable", None, ("comparison_subject_incomplete",))
        if not selection.metric_definition_ids:
            return ComparisonResolution("unavailable", None, ("comparison_metrics_required",))
        if self._session_factory is None:
            return ComparisonResolution("unavailable", None, ("comparison_repository_unavailable",))
        if selection.baseline_requested:
            return ComparisonResolution("unavailable", None, ("segment_baseline_not_materialized",))
        assert selection.right_public_id is not None
        with self._session_factory() as session:
            if selection.kind == "matches":
                subjects: list[MatchSubject] = []
                for public_id in (selection.left_public_id, selection.right_public_id):
                    report = session.scalar(select(Report).where(Report.public_id == public_id))
                    if report is None or report.replay_player_id is None:
                        return ComparisonResolution("unavailable", None, ("fixed_match_not_found",))
                    replay_player = session.get(ReplayPlayer, report.replay_player_id)
                    if replay_player is None:
                        return ComparisonResolution("unavailable", None, ("fixed_match_not_found",))
                    replay = session.get(Replay, report.replay_id)
                    if replay is None:
                        return ComparisonResolution("unavailable", None, ("fixed_match_not_found",))
                    if not self._match_matches_filters(session, replay, replay_player, selection.filters):
                        return ComparisonResolution("unavailable", None, ("comparison_filter_not_materialized",))
                    feature_sets = tuple(
                        session.scalars(
                            select(FeatureSet.public_id)
                            .where(FeatureSet.replay_player_id == replay_player.id, FeatureSet.status == "succeeded")
                            .order_by(FeatureSet.public_id)
                        )
                    )
                    subjects.append(
                        MatchSubject(
                            "match",
                            replay.public_id,
                            replay_player.public_id,
                            report.public_id,
                            report.input_digest,
                            feature_sets,
                            str(_mapping(report.report_json).get("schema_version", "replay-report-v1")),
                            report.report_version,
                            "replay-comparison-display-v1",
                        )
                    )
                definitions = self._match_definitions(session, tuple(subjects), selection.metric_definition_ids)
                if len(definitions) != len(set(selection.metric_definition_ids)):
                    return ComparisonResolution("unavailable", None, ("metric_definition_not_materialized",))
                fixed = FixedComparisonQuery(
                    selection.kind, subjects[0], subjects[1], definitions, selection.minimum_sample_size
                )
                return ComparisonResolution("resolved", fixed, ())
            result_subject = selection.kind in {"openings", "strategies"}
            subjects_long: list[LongitudinalSubject] = []
            for public_id in (selection.left_public_id, selection.right_public_id):
                strategy = None
                if selection.kind == "strategies":
                    strategy = session.scalar(
                        select(StrategyAssessment).where(StrategyAssessment.public_id == public_id)
                    )
                    if (
                        strategy is None
                        or strategy.method != "rule"
                        or strategy.taxonomy_version is None
                        or strategy.rule_version is None
                    ):
                        return ComparisonResolution("unavailable", None, ("rule_strategy_subject_not_found",))
                    member = session.scalar(
                        select(LongitudinalMember).where(LongitudinalMember.strategy_assessment_id == strategy.id)
                    )
                    result = None if member is None else session.get(LongitudinalResult, member.longitudinal_result_id)
                    if result is None:
                        return ComparisonResolution("unavailable", None, ("strategy_longitudinal_binding_not_found",))
                    run = session.get(LongitudinalRun, result.longitudinal_run_id)
                elif result_subject:
                    result = session.scalar(select(LongitudinalResult).where(LongitudinalResult.public_id == public_id))
                    if result is None:
                        return ComparisonResolution("unavailable", None, ("fixed_longitudinal_result_not_found",))
                    run = session.get(LongitudinalRun, result.longitudinal_run_id)
                else:
                    if selection.kind == "time_periods":
                        run = session.scalar(
                            select(LongitudinalRun).where(
                                LongitudinalRun.run_id == public_id, LongitudinalRun.status == "succeeded"
                            )
                        )
                        player = None if run is None else session.get(Player, run.player_id)
                    else:
                        player = session.scalar(select(Player).where(Player.public_id == public_id))
                        candidates = (
                            ()
                            if player is None
                            else tuple(
                                session.scalars(
                                    select(LongitudinalRun)
                                    .where(
                                        LongitudinalRun.player_id == player.id,
                                        LongitudinalRun.identity_revision == player.identity_revision,
                                        LongitudinalRun.status == "succeeded",
                                    )
                                    .order_by(LongitudinalRun.completed_at.desc(), LongitudinalRun.run_id)
                                )
                            )
                        )
                        run = next(
                            (item for item in candidates if self._run_matches_filters(item, selection.filters)),
                            None,
                        )
                        if candidates and run is None:
                            return ComparisonResolution("unavailable", None, ("comparison_filter_not_materialized",))
                    result = None
                if run is None:
                    return ComparisonResolution("unavailable", None, ("fixed_longitudinal_run_not_found",))
                if not self._run_matches_filters(run, selection.filters):
                    return ComparisonResolution("unavailable", None, ("comparison_filter_not_materialized",))
                player = session.get(Player, run.player_id)
                if player is None:
                    return ComparisonResolution("unavailable", None, ("fixed_longitudinal_run_not_found",))
                subject_kind = {
                    "players": "player_cohort",
                    "openings": "opening",
                    "strategies": "strategy",
                    "time_periods": "time_period",
                }[selection.kind]
                start = end = None
                if selection.kind == "time_periods":
                    segment = _mapping(run.segment_key_json)
                    start_value = segment.get("start_inclusive")
                    end_value = segment.get("end_exclusive")
                    if type(start_value) is not int or type(end_value) is not int or start_value >= end_value:
                        return ComparisonResolution("unavailable", None, ("utc_period_binding_not_proven",))
                    start = datetime.fromtimestamp(start_value, UTC)
                    end = datetime.fromtimestamp(end_value, UTC)
                binding = self._longitudinal_binding(
                    run,
                    player.public_id,
                    cast(
                        Literal["player_cohort", "segment_baseline", "opening", "strategy", "time_period"], subject_kind
                    ),
                    None if result is None else result.public_id,
                    start,
                    end,
                )
                if strategy is not None:
                    binding = LongitudinalSubject(
                        **{
                            name: getattr(binding, name)
                            for name in binding.__dataclass_fields__
                            if name not in {"taxonomy_version", "rule_version", "strategy_id"}
                        },
                        taxonomy_version=strategy.taxonomy_version,
                        rule_version=strategy.rule_version,
                        strategy_id=strategy.strategy_label,
                    )
                elif selection.kind == "openings" and result is not None:
                    definition = self._definition_from_run(run, result.result_name)
                    binding = LongitudinalSubject(
                        **{
                            name: getattr(binding, name)
                            for name in binding.__dataclass_fields__
                            if name not in {"opening_definition_id", "opening_definition_version"}
                        },
                        opening_definition_id=result.result_name,
                        opening_definition_version=(
                            "accepted-v1" if definition is None else definition.definition_version
                        ),
                    )
                if (
                    selection.kind in {"openings", "strategies"}
                    and result is not None
                    and set(selection.metric_definition_ids) != {result.result_name}
                ):
                    return ComparisonResolution(
                        "unavailable",
                        None,
                        ("subject_metric_definition_mismatch",),
                    )
                subjects_long.append(binding)
            if selection.kind == "time_periods":
                left_period, right_period = subjects_long
                if (left_period.player_public_id, left_period.identity_revision) != (
                    right_period.player_public_id,
                    right_period.identity_revision,
                ):
                    return ComparisonResolution("unavailable", None, ("time_period_player_revision_mismatch",))
                assert (
                    left_period.start_inclusive_utc
                    and left_period.end_exclusive_utc
                    and right_period.start_inclusive_utc
                    and right_period.end_exclusive_utc
                )
                if not (
                    left_period.end_exclusive_utc <= right_period.start_inclusive_utc
                    or right_period.end_exclusive_utc <= left_period.start_inclusive_utc
                ):
                    return ComparisonResolution("unavailable", None, ("time_periods_overlap",))
            definitions = self._longitudinal_definitions(session, tuple(subjects_long), selection.metric_definition_ids)
            if len(definitions) != len(set(selection.metric_definition_ids)):
                return ComparisonResolution("unavailable", None, ("metric_definition_not_materialized",))
            fixed = FixedComparisonQuery(
                selection.kind, subjects_long[0], subjects_long[1], definitions, selection.minimum_sample_size
            )
            return ComparisonResolution("resolved", fixed, ())

    @staticmethod
    def _run_matches_filters(run: LongitudinalRun, filters: ComparisonFilters) -> bool:
        segment = _mapping(run.segment_key_json)
        exact = (
            ("subject_faction", filters.faction),
            ("subject_subfaction", filters.subfaction),
            ("opponent_faction", filters.opponent_faction),
            ("opponent_player_public_id", filters.opponent_player_public_id),
            ("map_public_id", filters.map_public_id),
            ("replay_patch", filters.patch),
        )
        if any(value is not None and segment.get(key) != value for key, value in exact):
            return False
        if filters.start_position is not None:
            try:
                position = int(filters.start_position)
            except ValueError:
                return False
            if segment.get("start_position") != position:
                return False
        if filters.date_from_utc is not None:
            if filters.date_from_utc.utcoffset() != UTC.utcoffset(filters.date_from_utc):
                return False
            if segment.get("start_inclusive") != int(filters.date_from_utc.timestamp()):
                return False
        if filters.date_to_utc is not None:
            if filters.date_to_utc.utcoffset() != UTC.utcoffset(filters.date_to_utc):
                return False
            if segment.get("end_exclusive") != int(filters.date_to_utc.timestamp()):
                return False
        quality = _mapping(segment.get("quality_policy"))
        return filters.quality_policy_digest is None or _digest(quality) == filters.quality_policy_digest

    @staticmethod
    def _match_matches_filters(
        session: Session,
        replay: Replay,
        replay_player: ReplayPlayer,
        filters: ComparisonFilters,
    ) -> bool:
        if filters.quality_policy_digest is not None:
            return False
        observed = _mapping(replay_player.observed_json)
        if filters.faction is not None and replay_player.faction != filters.faction:
            return False
        if filters.subfaction is not None and observed.get("subfaction") != filters.subfaction:
            return False
        if filters.start_position is not None and str(replay_player.start_position) != filters.start_position:
            return False
        if filters.patch is not None and _mapping(replay.header_json).get("patch_identity") != filters.patch:
            return False
        if filters.date_from_utc is not None and replay.start_time < int(filters.date_from_utc.timestamp()):
            return False
        if filters.date_to_utc is not None and replay.start_time >= int(filters.date_to_utc.timestamp()):
            return False
        if filters.map_public_id is not None:
            map_public_id = session.scalar(select(Map.public_id).where(Map.id == replay.map_id))
            if map_public_id != filters.map_public_id:
                return False
        if filters.opponent_faction is not None or filters.opponent_player_public_id is not None:
            opponents = tuple(
                session.execute(
                    select(ReplayPlayer, Player)
                    .outerjoin(Player, ReplayPlayer.player_id == Player.id)
                    .where(
                        ReplayPlayer.replay_id == replay.id,
                        ReplayPlayer.id != replay_player.id,
                    )
                )
            )
            if filters.opponent_faction is not None and filters.opponent_faction not in {
                item.faction for item, _player in opponents
            }:
                return False
            if filters.opponent_player_public_id is not None and filters.opponent_player_public_id not in {
                player.public_id for _item, player in opponents if player is not None
            }:
                return False
        return True

    @staticmethod
    def _longitudinal_binding(
        run: LongitudinalRun,
        player_public_id: str,
        subject_kind: Literal["player_cohort", "segment_baseline", "opening", "strategy", "time_period"],
        result_public_id: str | None,
        start: datetime | None,
        end: datetime | None,
    ) -> LongitudinalSubject:
        segment = _mapping(run.segment_key_json)
        quality = _mapping(segment.get("quality_policy"))
        settings = _mapping(run.settings_json)
        algorithms = _mapping(settings.get("settings"))
        versions = tuple(
            sorted(
                {
                    str(value)
                    for key, value in algorithms.items()
                    if key.endswith("algorithm_version") and isinstance(value, str)
                }
            )
        ) or ("unknown-v1",)
        return LongitudinalSubject(
            subject_kind,
            run.run_id,
            player_public_id,
            run.identity_revision,
            result_public_id,
            start,
            end,
            analyzer_name=run.analyzer_name,
            analyzer_version=run.analyzer_version,
            segment_digest=_digest(segment),
            quality_policy_digest=_digest(quality),
            input_digest=run.input_digest,
            cache_key=run.cache_key,
            statistics_algorithm_versions=versions,
        )

    @staticmethod
    def _definition_from_run(run: LongitudinalRun, name: str) -> ComparisonDefinition | None:
        values = _mapping(run.settings_json).get("definitions")
        if not isinstance(values, list):
            return None
        for raw in values:
            item = _mapping(raw)
            if item.get("public_name") != name or not isinstance(item.get("definition_version"), str):
                continue
            definition_kind: Literal["feature", "opening", "strategy", "trend", "match_metric"] = "feature"
            if name.startswith("recurring_opening"):
                definition_kind = "opening"
            elif "strategy" in name:
                definition_kind = "strategy"
            elif name.startswith("trend"):
                definition_kind = "trend"
            scopes = item.get("scope_types")
            scope = "player" if not isinstance(scopes, list) or not scopes else str(scopes[0])
            return ComparisonDefinition(
                name,
                cast(str, item["definition_version"]),
                cast(str | None, item.get("unit")),
                scope,
                "inclusive-frame-window-v1",
                "same_faction_only",
                definition_kind,
                cast(str | None, item.get("taxonomy_version")),
            )
        return None

    def _longitudinal_definitions(
        self, session: Session, subjects: tuple[LongitudinalSubject, ...], names: tuple[str, ...]
    ) -> tuple[ComparisonDefinition, ...]:
        result: list[ComparisonDefinition] = []
        runs = [
            session.scalar(select(LongitudinalRun).where(LongitudinalRun.run_id == item.run_id)) for item in subjects
        ]
        for name in sorted(set(names)):
            definitions = [self._definition_from_run(run, name) for run in runs if run is not None]
            if len(definitions) != len(runs) or any(item is None for item in definitions) or len(set(definitions)) != 1:
                continue
            result.append(cast(ComparisonDefinition, definitions[0]))
        return tuple(result)

    @staticmethod
    def _match_definitions(
        session: Session, subjects: tuple[MatchSubject, ...], names: tuple[str, ...]
    ) -> tuple[ComparisonDefinition, ...]:
        result: list[ComparisonDefinition] = []
        for name in sorted(set(names)):
            rows = tuple(
                session.scalars(
                    select(Feature)
                    .join(FeatureSet, Feature.feature_set_id == FeatureSet.id)
                    .where(
                        FeatureSet.public_id.in_(
                            tuple(public_id for subject in subjects for public_id in subject.feature_set_public_ids)
                        ),
                        Feature.name == name,
                    )
                )
            )
            units = {row.unit for row in rows}
            scopes = {row.scope_type for row in rows}
            if len(rows) < 2 or len(units) != 1 or len(scopes) != 1:
                continue
            result.append(
                ComparisonDefinition(
                    name,
                    "accepted-feature-v1",
                    next(iter(units)),
                    next(iter(scopes)),
                    "inclusive-frame-window-v1",
                    "same_faction_only",
                    "match_metric",
                )
            )
        return tuple(result)

    @staticmethod
    def compare_values(value: ComparisonInput) -> ComparedValue:
        reasons: list[str] = []
        if value.kind == "time_periods" and not value.utc_source_proven:
            return ComparedValue(
                value.definition, value.left, value.right, None, (), "unavailable", ("utc_source_unproven",)
            )
        if value.definition.faction_comparability == "same_faction_only" and not value.same_faction:
            reasons.append("faction_mismatch")
        if value.left.unit != value.right.unit or value.left.unit != value.definition.unit:
            reasons.append("unit_mismatch")
        required = 1 if value.kind == "matches" else value.minimum_sample_size
        if value.left.sample_count < required or value.right.sample_count < required:
            reasons.append("minimum_sample_size_not_met")
        if value.left.availability == "unavailable" or value.right.availability == "unavailable":
            reasons.append("subject_value_unavailable")
        if reasons:
            state: ComparisonState = "unavailable" if reasons == ["subject_value_unavailable"] else "not_comparable"
            return ComparedValue(
                value.definition, value.left, value.right, None, (), state, tuple(sorted(set(reasons)))
            )
        difference: object | None = None
        if (
            isinstance(value.left.raw_value, (int, float))
            and not isinstance(value.left.raw_value, bool)
            and isinstance(value.right.raw_value, (int, float))
            and not isinstance(value.right.raw_value, bool)
        ):
            left = float(value.left.raw_value)
            right = float(value.right.raw_value)
            if not math.isfinite(left) or not math.isfinite(right):
                return ComparedValue(
                    value.definition, value.left, value.right, None, (), "not_comparable", ("nonfinite_value",)
                )
            difference = left - right
            if difference == 0.0:
                difference = 0.0
        elif value.left.raw_value != value.right.raw_value:
            difference = {"left": value.left.raw_value, "right": value.right.raw_value}
        evidence = tuple(sorted(set(value.left.evidence_public_ids + value.right.evidence_public_ids)))
        partial = (
            value.left.availability == "partial"
            or value.right.availability == "partial"
            or bool(value.left.quality_exclusion_codes or value.right.quality_exclusion_codes)
        )
        return ComparedValue(
            value.definition, value.left, value.right, difference, evidence, "partial" if partial else "comparable", ()
        )

    def compare(self, query: FixedComparisonQuery) -> ComparisonResult:
        if self._session_factory is None:
            raise RuntimeError("database-backed comparison service is not configured")
        metrics: list[ComparisonMetric] = []
        with self._session_factory() as session:
            self._validate_subjects(session, query)
            for definition in sorted(query.metric_definitions, key=lambda item: item.definition_id):
                left = self._value(session, query.left, definition, query.kind)
                right = self._value(session, query.right, definition, query.kind)
                compared = self.compare_values(
                    ComparisonInput(
                        query.kind,
                        definition,
                        left,
                        right,
                        query.minimum_sample_size,
                        self._utc_source_proven(session, query),
                        self._same_faction(session, query),
                    )
                )
                section = self._section(definition)
                metrics.append(
                    ComparisonMetric(
                        section,
                        definition.definition_id,
                        definition.definition_id.replace("_", " "),
                        self._value_kind(left.raw_value),
                        compared,
                    )
                )
        reason_codes = tuple(sorted({reason for metric in metrics for reason in metric.value.reason_codes}))
        states = {metric.value.state for metric in metrics}
        state: ComparisonState = "comparable"
        if not metrics or states == {"unavailable"}:
            state = "unavailable"
        elif "not_comparable" in states and states <= {"not_comparable", "unavailable"}:
            state = "not_comparable"
        elif states != {"comparable"}:
            state = "partial"
        query_value = self._query_value(query)
        query_digest = _digest(query_value)
        input_digest = _digest({"query": query_value, "metrics": [self._metric_value(item) for item in metrics]})
        return ComparisonResult(
            str(uuid5(_COMPARISON_NAMESPACE, input_digest)),
            query_digest,
            input_digest,
            query,
            state,
            reason_codes,
            tuple(metrics),
        )

    def _validate_subjects(self, session: Session, query: FixedComparisonQuery) -> None:
        if query.minimum_sample_size < 1 or not query.metric_definitions:
            raise ValueError("comparison query is incomplete")
        if query.minimum_sample_size != self._minimum_sample_size:
            raise ValueError("fixed_minimum_sample_size_mismatch")
        definition_ids = tuple(item.definition_id for item in query.metric_definitions)
        if definition_ids != tuple(sorted(set(definition_ids))):
            raise ValueError("fixed_definition_binding_mismatch")
        for subject in (query.left, query.right):
            if isinstance(subject, MatchSubject):
                report = session.scalar(select(Report).where(Report.public_id == subject.report_public_id))
                replay_player = session.scalar(
                    select(ReplayPlayer).where(ReplayPlayer.public_id == subject.replay_player_public_id)
                )
                if (
                    report is None
                    or replay_player is None
                    or report.replay_player_id != replay_player.id
                    or report.replay_id != replay_player.replay_id
                    or report.input_digest != subject.report_input_digest
                    or report.report_version != subject.report_version
                    or _mapping(report.report_json).get("schema_version") != subject.document_schema_version
                    or subject.display_policy_version != "replay-comparison-display-v1"
                ):
                    raise ValueError("fixed_match_binding_mismatch")
                from generals_replay_analyzer.db.models import Replay

                replay = session.get(Replay, report.replay_id)
                if replay is None or replay.public_id != subject.replay_public_id:
                    raise ValueError("fixed_match_binding_mismatch")
                ids = (
                    tuple(
                        session.scalars(
                            select(FeatureSet.public_id)
                            .where(
                                FeatureSet.public_id.in_(subject.feature_set_public_ids),
                                FeatureSet.status == "succeeded",
                                FeatureSet.replay_id == replay.id,
                                FeatureSet.replay_player_id == replay_player.id,
                            )
                            .order_by(FeatureSet.public_id)
                        )
                    )
                    if subject.feature_set_public_ids
                    else ()
                )
                if ids != tuple(sorted(set(subject.feature_set_public_ids))):
                    raise ValueError("fixed_feature_set_binding_mismatch")
            else:
                run = session.scalar(select(LongitudinalRun).where(LongitudinalRun.run_id == subject.run_id))
                player = session.scalar(select(Player).where(Player.public_id == subject.player_public_id))
                if (
                    run is None
                    or player is None
                    or run.player_id != player.id
                    or run.identity_revision != subject.identity_revision
                    or run.status != "succeeded"
                ):
                    raise ValueError("fixed_longitudinal_binding_mismatch")
                expected = self._longitudinal_binding(
                    run,
                    player.public_id,
                    subject.subject_kind,
                    subject.result_public_id,
                    subject.start_inclusive_utc,
                    subject.end_exclusive_utc,
                )
                binding_fields = (
                    "run_id",
                    "player_public_id",
                    "identity_revision",
                    "analyzer_name",
                    "analyzer_version",
                    "segment_digest",
                    "quality_policy_digest",
                    "input_digest",
                    "cache_key",
                    "statistics_algorithm_versions",
                )
                if any(getattr(subject, name) != getattr(expected, name) for name in binding_fields):
                    raise ValueError("fixed_longitudinal_binding_mismatch")
                result = None
                if subject.result_public_id is not None:
                    result = session.scalar(
                        select(LongitudinalResult).where(
                            LongitudinalResult.public_id == subject.result_public_id,
                            LongitudinalResult.longitudinal_run_id == run.id,
                        )
                    )
                if subject.result_public_id is not None and result is None:
                    raise ValueError("fixed_result_binding_mismatch")
                if subject.subject_kind == "time_period":
                    segment = _mapping(run.segment_key_json)
                    start = segment.get("start_inclusive")
                    end = segment.get("end_exclusive")
                    if (
                        type(start) is not int
                        or type(end) is not int
                        or subject.start_inclusive_utc != datetime.fromtimestamp(start, UTC)
                        or subject.end_exclusive_utc != datetime.fromtimestamp(end, UTC)
                    ):
                        raise ValueError("fixed_time_period_binding_mismatch")
                if subject.subject_kind == "opening":
                    definition = None if result is None else self._definition_from_run(run, result.result_name)
                    if (
                        result is None
                        or definition is None
                        or subject.opening_definition_id != result.result_name
                        or subject.opening_definition_version != definition.definition_version
                    ):
                        raise ValueError("fixed_opening_binding_mismatch")
                if subject.subject_kind == "strategy":
                    assessment = (
                        None
                        if result is None
                        else session.scalar(
                            select(StrategyAssessment)
                            .join(
                                LongitudinalMember, LongitudinalMember.strategy_assessment_id == StrategyAssessment.id
                            )
                            .where(LongitudinalMember.longitudinal_result_id == result.id)
                        )
                    )
                    if (
                        assessment is None
                        or assessment.method != "rule"
                        or assessment.strategy_label != subject.strategy_id
                        or assessment.taxonomy_version != subject.taxonomy_version
                        or assessment.rule_version != subject.rule_version
                    ):
                        raise ValueError("fixed_strategy_binding_mismatch")
        canonical = (
            self._match_definitions(session, cast(tuple[MatchSubject, ...], (query.left, query.right)), definition_ids)
            if query.kind == "matches"
            else self._longitudinal_definitions(
                session,
                cast(tuple[LongitudinalSubject, ...], (query.left, query.right)),
                definition_ids,
            )
        )
        if canonical != query.metric_definitions:
            raise ValueError("fixed_definition_binding_mismatch")

    def _value(
        self, session: Session, subject: ComparisonSubject, definition: ComparisonDefinition, kind: ComparisonKind
    ) -> ComparisonValue:
        if isinstance(subject, MatchSubject):
            feature_row = session.execute(
                select(Feature, EvidenceItem)
                .join(FeatureSet, Feature.feature_set_id == FeatureSet.id)
                .join(EvidenceItem, Feature.evidence_item_id == EvidenceItem.id)
                .where(
                    FeatureSet.public_id.in_(subject.feature_set_public_ids),
                    FeatureSet.status == "succeeded",
                    Feature.name == definition.definition_id,
                    Feature.replay_player_id == ReplayPlayer.id,
                    ReplayPlayer.public_id == subject.replay_player_public_id,
                )
                .join(ReplayPlayer, Feature.replay_player_id == ReplayPlayer.id)
                .order_by(Feature.public_id)
            ).first()
            if feature_row is None:
                return ComparisonValue(
                    None, definition.unit, 0, 1, "unavailable", (), quality_exclusion_codes=("metric_not_materialized",)
                )
            feature, evidence = feature_row
            raw = {
                "integer": feature.integer_value,
                "real": feature.real_value,
                "text": feature.text_value,
                "boolean": feature.boolean_value,
                "json": feature.json_value,
            }[feature.value_type]
            return ComparisonValue(
                raw if feature.quality != "unavailable" else None,
                feature.unit,
                1 if feature.quality != "unavailable" else 0,
                0 if feature.quality != "unavailable" else 1,
                cast(Literal["available", "partial", "unavailable"], feature.quality),
                (evidence.public_id,),
                quality_exclusion_codes=() if feature.quality_reason is None else (feature.quality_reason,),
            )
        run = session.scalar(select(LongitudinalRun).where(LongitudinalRun.run_id == subject.run_id))
        assert run is not None
        statement = (
            select(LongitudinalResult, EvidenceItem)
            .join(EvidenceItem, LongitudinalResult.evidence_item_id == EvidenceItem.id)
            .where(LongitudinalResult.longitudinal_run_id == run.id)
        )
        if subject.result_public_id is not None and kind in {"openings", "strategies"}:
            statement = statement.where(
                LongitudinalResult.public_id == subject.result_public_id,
                LongitudinalResult.result_name == definition.definition_id,
            )
        else:
            statement = statement.where(LongitudinalResult.result_name == definition.definition_id)
        result_row = session.execute(statement.order_by(LongitudinalResult.public_id)).first()
        if result_row is None:
            return ComparisonValue(
                None, definition.unit, 0, 1, "unavailable", (), quality_exclusion_codes=("metric_not_materialized",)
            )
        result, evidence = result_row
        storage = _mapping(result.statistics_json)
        statistics = _mapping(storage.get("public_statistics"))
        raw = statistics.get(
            "value",
            statistics.get("median", statistics.get("difference", statistics.get("share", statistics.get("mode")))),
        )
        interval_value = _mapping(statistics.get("interval"))
        interval = None
        if isinstance(interval_value.get("lower"), (int, float)) and isinstance(
            interval_value.get("upper"), (int, float)
        ):
            interval = DistributionInterval(
                float(cast(float, interval_value["lower"])),
                float(cast(float, interval_value["upper"])),
                float(cast(float, interval_value.get("confidence_level", 0.95))),
                str(interval_value.get("method", "accepted")),
                str(interval_value.get("algorithm_version", "accepted-v1")),
            )
        quality = "available" if result.quality == "available" else result.quality
        return ComparisonValue(
            raw if quality != "unavailable" else None,
            definition.unit,
            result.sample_count,
            result.missing_count,
            cast(Literal["available", "partial", "unavailable"], quality),
            (evidence.public_id,),
            interval,
            () if result.quality_reason is None else (result.quality_reason,),
        )

    @staticmethod
    def _utc_source_proven(session: Session, query: FixedComparisonQuery) -> bool:
        if query.kind != "time_periods":
            return True
        for subject in (query.left, query.right):
            assert isinstance(subject, LongitudinalSubject)
            run = session.scalar(select(LongitudinalRun).where(LongitudinalRun.run_id == subject.run_id))
            if run is None:
                return False
            segment = _mapping(run.segment_key_json)
            if segment.get("start_inclusive") is None or segment.get("end_exclusive") is None:
                return False
        return True

    @staticmethod
    def _same_faction(session: Session, query: FixedComparisonQuery) -> bool:
        factions: list[str] = []
        for subject in (query.left, query.right):
            if isinstance(subject, MatchSubject):
                faction = session.scalar(
                    select(ReplayPlayer.faction).where(ReplayPlayer.public_id == subject.replay_player_public_id)
                )
                if faction:
                    factions.append(faction)
                continue
            run = session.scalar(select(LongitudinalRun).where(LongitudinalRun.run_id == subject.run_id))
            if run is None:
                return False
            statement = (
                select(ReplayPlayer.faction)
                .join(LongitudinalMember, LongitudinalMember.replay_player_id == ReplayPlayer.id)
                .join(LongitudinalResult, LongitudinalMember.longitudinal_result_id == LongitudinalResult.id)
                .where(LongitudinalResult.longitudinal_run_id == run.id)
            )
            if subject.result_public_id is not None:
                statement = statement.where(LongitudinalResult.public_id == subject.result_public_id)
            else:
                statement = statement.where(
                    LongitudinalResult.result_name.in_(tuple(item.definition_id for item in query.metric_definitions))
                )
            subject_factions = set(session.scalars(statement)) - {None}
            if len(subject_factions) > 1:
                return False
            factions.extend(cast(set[str], subject_factions))
        return len(set(factions)) <= 1

    @staticmethod
    def _section(definition: ComparisonDefinition) -> str:
        return {"opening": "openings", "strategy": "strategy", "trend": "trend"}.get(
            definition.definition_kind, "overview"
        )

    @staticmethod
    def _value_kind(value: object | None) -> str:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return "scalar"
        return "categorical_share"

    @staticmethod
    def _query_value(query: FixedComparisonQuery) -> object:
        return _canonical_value(query)

    @staticmethod
    def _metric_value(metric: ComparisonMetric) -> object:
        return _canonical_value(metric)
