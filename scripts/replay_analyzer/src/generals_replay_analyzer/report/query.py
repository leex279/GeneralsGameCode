"""Read-only selection of completed immutable report and evidence graphs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from typing import Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.analysis_pipeline.codecs import (
    PipelineCodecError,
    PlayerAssessmentSelection,
    decode_assessment_output,
    decode_llm_output,
)
from generals_replay_analyzer.analysis_pipeline.identity_scope import (
    IdentityAnalysisScope,
    IdentityScopeError,
)
from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import (
    AnalysisRun,
    AssessmentEvidence,
    EvidenceItem,
    Feature,
    FeatureEvidence,
    FeatureSet,
    Job,
    JobDependency,
    JobStageResult,
    LongitudinalMember,
    LongitudinalResult,
    LongitudinalRun,
    ManagedAsset,
    ParserRun,
    Player,
    Replay,
    ReplayCommand,
    ReplayPlayer,
    Report,
    StrategyAssessment,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.importing.evidence_identity import (
    parser_command_evidence_identity,
    telemetry_event_evidence_identity,
    validate_observed_evidence_identity,
)
from generals_replay_analyzer.importing.stages import (
    ANALYZE_LLM,
    ANALYZE_LLM_VERSION,
    ASSESS_STRATEGIES,
    ASSESS_STRATEGIES_VERSION,
    DERIVE_FEATURES,
    DERIVE_FEATURES_VERSION,
    IMPORT_OBSERVATIONS,
    IMPORT_OBSERVATIONS_VERSION,
    RENDER_REPORT,
    RENDER_REPORT_VERSION,
    content_key,
    input_digest,
)
from generals_replay_analyzer.llm.evidence_bundle import EvidenceBundle
from generals_replay_analyzer.llm.schema import (
    PROMPT_SHA256,
    PROMPT_VERSION,
    RESPONSE_SCHEMA_SHA256,
    RESPONSE_SCHEMA_VERSION,
    ResponseValidationError,
    validate_response,
)
from generals_replay_analyzer.longitudinal.segments import LongitudinalMemberDTO
from generals_replay_analyzer.report.assembly import DISPLAY_POLICY_VERSION, REPORT_VERSION
from generals_replay_analyzer.report.model import (
    CanonicalValue,
    OllamaReportStatus,
    OllamaStatus,
    ReportAvailability,
    ReportDocument,
    ReportEvidenceRef,
    ReportEvidenceTier,
    ReportLifecycle,
    ReportQualityIssue,
    ReportValue,
    document_to_mapping,
    freeze_report_value,
    thaw_report_value,
)
from generals_replay_analyzer.report.read_model import (
    DerivedAssessmentEvidenceDTO,
    DerivedFeatureEvidenceDTO,
    DerivedLongitudinalEvidenceDTO,
    EvidenceDetailDTO,
    EvidenceLinkDTO,
    EvidenceRole,
    EvidenceSourceDTO,
    InferredAssessmentEvidenceDTO,
    ObservedCommandEvidenceDTO,
    ObservedTelemetryEvidenceDTO,
    PublishedReportAssetDTO,
    PublishedReportDTO,
    PublishedReportGraphDTO,
    ReportPlayerIdentityDTO,
    ReportReplayIdentityDTO,
    ResolvedReportDTO,
    TimelineChartDTO,
    TimelineEvidenceDTO,
    TimelineFamily,
    TimelineFamilyOptionDTO,
    TimelineIntervalDTO,
    TimelineOptionDTO,
    TimelinePointDTO,
    TimelineSeriesDTO,
)
from generals_replay_analyzer.report.render_html import render_html
from generals_replay_analyzer.report.render_json import render_json
from generals_replay_analyzer.report.render_text import render_text
from generals_replay_analyzer.report.resources import (
    ReportResources,
    load_report_resources,
    validate_document,
)
from generals_replay_analyzer.storage import ContentAddressedStore, ContentStorageError

_ASSET_NAMESPACE = uuid5(NAMESPACE_URL, "replay-report-managed-asset-v1")
_REPORT_NAMESPACE = uuid5(NAMESPACE_URL, "replay-report-public-id-v1")
_EVIDENCE_SOURCE_KINDS: dict[str, tuple[str, ...]] = {
    "observed": ("parser_command", "telemetry_event"),
    "derived": ("feature", "strategy_rule", "longitudinal_corpus"),
    "inferred": ("llm",),
}


class ReportGraphError(RuntimeError):
    """Base path-free query failure."""


class ReportGraphNotFoundError(ReportGraphError):
    """The exact public report or evidence identity is not available."""


class ReportGraphAmbiguousError(ReportGraphError):
    """More than one immutable render graph claims the same report."""


class ReportGraphContractError(ReportGraphError):
    """Persisted report, stage, asset, or evidence data is corrupt."""


def _uuid(value: object, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a canonical lowercase UUID")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f"{label} must be a canonical lowercase UUID") from None
    if str(parsed) != value:
        raise ValueError(f"{label} must be a canonical lowercase UUID")
    return value


@dataclass(frozen=True, slots=True)
class FixedReportQuery:
    replay_public_id: str
    report_public_id: str

    def __post_init__(self) -> None:
        _uuid(self.replay_public_id, "replay_public_id")
        _uuid(self.report_public_id, "report_public_id")


@dataclass(frozen=True, slots=True)
class LatestReportQuery:
    replay_public_id: str
    replay_player_public_id: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.replay_public_id, "replay_public_id")
        if self.replay_player_public_id is not None:
            _uuid(self.replay_player_public_id, "replay_player_public_id")


@dataclass(frozen=True, slots=True)
class TimelineChartQuery:
    replay_public_id: str
    report_public_id: str
    replay_player_public_ids: tuple[str, ...] = ()
    families: tuple[TimelineFamily, ...] = ()

    def __post_init__(self) -> None:
        _uuid(self.replay_public_id, "replay_public_id")
        _uuid(self.report_public_id, "report_public_id")
        if type(self.replay_player_public_ids) is not tuple:
            raise TypeError("timeline replay players must be an immutable tuple")
        players = tuple(sorted({_uuid(value, "replay_player_public_id") for value in self.replay_player_public_ids}))
        if type(self.families) is not tuple:
            raise TypeError("timeline families must be an immutable tuple")
        allowed = frozenset({"build_order", "economy", "production", "combat", "activity", "strategy", "quality"})
        if any(type(value) is not str or value not in allowed for value in self.families):
            raise ValueError("timeline family is unsupported")
        object.__setattr__(self, "replay_player_public_ids", players)
        object.__setattr__(self, "families", tuple(sorted(set(self.families))))


@dataclass(frozen=True, slots=True)
class EvidenceQuery:
    report_public_id: str
    evidence_public_id: str
    expected_tier: ReportEvidenceTier

    def __post_init__(self) -> None:
        _uuid(self.report_public_id, "report_public_id")
        _uuid(self.evidence_public_id, "evidence_public_id")
        if self.expected_tier not in ("observed", "derived", "inferred"):
            raise ValueError("expected_tier must be observed, derived, or inferred")


@dataclass(frozen=True, slots=True)
class _ReportEntry:
    replay_player_public_id: str | None
    analysis_run_id: str | None
    structured_asset_public_id: str
    presentation_asset_public_id: str


@dataclass(frozen=True, slots=True)
class _SubjectSelection:
    replay_player_public_id: str | None
    analysis_run_id: str | None
    feature_set_public_ids: tuple[str, ...]
    strategy_cache_key: str
    longitudinal_run_id: str | None


@dataclass(frozen=True, slots=True)
class _ValidatedGraph:
    entries: tuple[_ReportEntry, ...]
    subjects: tuple[_SubjectSelection, ...]


@dataclass(frozen=True, slots=True)
class _SelectedEvidenceClaim:
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SelectedEvidenceBundle:
    claims: tuple[_SelectedEvidenceClaim, ...]


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _mapping(value: object, keys: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys or any(type(key) is not str for key in value):
        raise ReportGraphContractError(f"{label} does not use its exact schema")
    return cast(Mapping[str, object], value)


def _sha256(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _safe_value(value: object) -> CanonicalValue:
    try:
        frozen = freeze_report_value(value)
    except (TypeError, ValueError) as exc:
        raise ReportGraphContractError("evidence contains a forbidden public value") from exc
    nodes = 0
    text_bytes = 0

    def visit(item: object, depth: int) -> None:
        nonlocal nodes, text_bytes
        nodes += 1
        if nodes > 2048 or depth > 12:
            raise ReportGraphContractError("evidence public value exceeds its structural bound")
        if isinstance(item, str):
            text_bytes += len(item.encode("utf-8"))
            if text_bytes > 65536:
                raise ReportGraphContractError("evidence public value exceeds its UTF-8 bound")
        elif isinstance(item, tuple):
            for child in item:
                if isinstance(child, tuple) and len(child) == 2 and isinstance(child[0], str):
                    visit(child[0], depth + 1)
                    visit(child[1], depth + 1)
                else:
                    visit(child, depth + 1)

    visit(frozen, 0)
    return frozen


class ReportQueryService:
    """Validate and return only completed persisted report/evidence graphs."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: AnalyzerSettings,
        store: ContentAddressedStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._store = store or ContentAddressedStore(settings.cache_directory / "reports")

    # TheSuperHackers @feature Leex 23/08/2026 Select only an exact completed report stage graph for read-only consumers. (#TBD)
    def get_report(self, query: FixedReportQuery) -> PublishedReportGraphDTO:
        if type(query) is not FixedReportQuery:
            raise TypeError("query must be a FixedReportQuery")
        graph, _selection = self._read_graph(query)
        return graph

    # TheSuperHackers @feature Leex 23/08/2026 Resolve one latest immutable report subject without assembling or writing. (#TBD)
    def resolve_latest(self, query: LatestReportQuery) -> ResolvedReportDTO:
        if type(query) is not LatestReportQuery:
            raise TypeError("query must be a LatestReportQuery")
        with self._session_factory() as session:
            replay = session.scalar(select(Replay).where(Replay.public_id == query.replay_public_id))
            if replay is None:
                raise ReportGraphNotFoundError("requested replay public ID was not found")
            replay_player_id: int | None = None
            if query.replay_player_public_id is not None:
                players = tuple(
                    session.scalars(
                        select(ReplayPlayer).where(
                            ReplayPlayer.replay_id == replay.id,
                            ReplayPlayer.public_id == query.replay_player_public_id,
                        )
                    )
                )
                if len(players) != 1:
                    raise ReportGraphNotFoundError("requested replay player has no exact report subject")
                replay_player_id = players[0].id
            rows = tuple(
                session.scalars(
                    select(Report).where(
                        Report.replay_id == replay.id,
                        Report.report_version == REPORT_VERSION,
                        Report.replay_player_id.is_(None)
                        if replay_player_id is None
                        else Report.replay_player_id == replay_player_id,
                    )
                )
            )
            if not rows:
                raise ReportGraphNotFoundError("requested report subject has not been generated")
            latest_created_at = max(row.created_at for row in rows)
            latest = tuple(row for row in rows if row.created_at == latest_created_at)
            if len(latest) != 1:
                raise ReportGraphAmbiguousError("latest report subject is ambiguous")
            report_public_id = latest[0].public_id
        graph = self.get_report(FixedReportQuery(query.replay_public_id, report_public_id))
        selected = graph.selected.document
        if selected.replay_player_public_id != query.replay_player_public_id:
            raise ReportGraphContractError("resolved report subject does not match its query")
        return ResolvedReportDTO(
            selected.replay_public_id,
            selected.replay_player_public_id,
            selected.report_public_id,
            "replay-report-v1",
        )

    # TheSuperHackers @feature Leex 23/08/2026 Project report claims onto a fixed frame-canonical timeline without interpolation. (#TBD)
    def timeline_chart(self, query: TimelineChartQuery) -> TimelineChartDTO:
        if type(query) is not TimelineChartQuery:
            raise TypeError("query must be a TimelineChartQuery")
        graph = self.get_report(FixedReportQuery(query.replay_public_id, query.report_public_id))
        selected_document = graph.selected.document
        available_players = (
            ()
            if selected_document.replay_player_public_id is None
            else (TimelineOptionDTO(selected_document.replay_player_public_id, "Selected player"),)
        )
        available_player_ids = {item.public_id for item in available_players}
        if not set(query.replay_player_public_ids).issubset(available_player_ids):
            raise ReportGraphContractError("timeline player selection is outside the report graph")
        selected_player_ids = query.replay_player_public_ids or tuple(item.public_id for item in available_players)
        documents = (selected_document,)
        available_family_values: tuple[TimelineFamily, ...] = (
            "build_order",
            "economy",
            "production",
            "combat",
            "activity",
            "strategy",
            "quality",
        )
        selected_families = query.families or available_family_values
        family_labels = {
            "build_order": "Build order",
            "economy": "Economy",
            "production": "Production",
            "combat": "Combat",
            "activity": "Activity",
            "strategy": "Strategy",
            "quality": "Quality",
        }
        series: list[TimelineSeriesDTO] = []
        for document in documents:
            for value in (
                *document.evidence_availability,
                *document.observed,
                *document.derived,
                *document.inferred,
            ):
                family = self._timeline_family(value.section, value.label)
                if value.frame_window is None or family not in selected_families:
                    continue
                evidence = tuple(TimelineEvidenceDTO(item.public_id, item.tier) for item in value.evidence)
                start, end = value.frame_window
                points: tuple[TimelinePointDTO, ...] = ()
                intervals: tuple[TimelineIntervalDTO, ...] = ()
                kind: Literal["marker", "band"]
                if value.availability == "unavailable":
                    kind = "marker"
                elif start == end:
                    scalar = self._timeline_scalar(value.raw_value)
                    kind = "marker"
                    points = (
                        TimelinePointDTO(
                            start,
                            scalar,
                            value.label,
                            evidence,
                        ),
                    )
                else:
                    kind = "band"
                    intervals = (TimelineIntervalDTO(start, end, value.label, evidence),)
                series.append(
                    TimelineSeriesDTO(
                        value.claim_id,
                        kind,
                        family,
                        document.replay_player_public_id,
                        value.label,
                        value.unit,
                        value.availability,
                        value.unavailable_reason,
                        points,
                        intervals,
                    )
                )
        ordered = tuple(sorted(series, key=lambda item: (item.family, item.player_public_id or "", item.series_id)))
        if not ordered:
            availability: ReportAvailability = "unavailable"
            reason = "timeline_frame_evidence_unavailable"
        elif all(item.availability == "unavailable" for item in ordered):
            availability = "unavailable"
            reason = "timeline_frame_evidence_unavailable"
        elif any(item.availability != "available" for item in ordered):
            availability = "partial"
            reason = "timeline_evidence_partial"
        else:
            availability = "available"
            reason = None
        return TimelineChartDTO(
            "replay-report-timeline-v1",
            query.replay_public_id,
            query.report_public_id,
            "replay-report-v1",
            30,
            "logic-frame-axis-v1",
            "frame-div-30-v1",
            selected_player_ids,
            selected_families,
            available_players,
            tuple(TimelineFamilyOptionDTO(item, family_labels[item]) for item in available_family_values),
            ordered,
            availability,
            reason,
        )

    @staticmethod
    def _timeline_scalar(value: CanonicalValue | None) -> int | float | str | None:
        thawed = thaw_report_value(value)
        if type(thawed) in (int, float, str):
            return cast(int | float | str, thawed)
        return None

    @staticmethod
    def _timeline_family(section: str, label: str) -> TimelineFamily:
        if section == "strategy" or section == "longitudinal":
            return "strategy"
        if section == "availability":
            return "quality"
        normalized = label.casefold()
        if any(token in normalized for token in ("construct", "build", "dozer", "opening")):
            return "build_order"
        if any(token in normalized for token in ("cash", "economy", "income", "resource", "supply")):
            return "economy"
        if any(token in normalized for token in ("production", "upgrade", "unit_composition")):
            return "production"
        if any(token in normalized for token in ("combat", "damage", "attack", "kill", "engagement")):
            return "combat"
        if any(token in normalized for token in ("strategy", "phase", "transition", "timing")):
            return "strategy"
        return "activity"

    # TheSuperHackers @feature Leex 23/08/2026 Resolve bounded evidence only inside one validated immutable report graph. (#TBD)
    def get_evidence(self, query: EvidenceQuery) -> EvidenceDetailDTO:
        if type(query) is not EvidenceQuery:
            raise TypeError("query must be an EvidenceQuery")
        with self._session_factory() as session:
            row = session.scalar(select(Report).where(Report.public_id == query.report_public_id))
            if row is None:
                raise ReportGraphNotFoundError("requested report public ID was not found")
            replay = session.get(Replay, row.replay_id)
            if replay is None:
                raise ReportGraphContractError("report replay identity is unavailable")
            replay_public_id = replay.public_id
        graph, selections = self._read_graph(FixedReportQuery(replay_public_id, query.report_public_id))
        selected = graph.selected
        index = self._document_evidence_index(selected.document)
        tier = index.get(query.evidence_public_id)
        if tier is None:
            raise ReportGraphNotFoundError("evidence is not cited by the exact requested report")
        if tier != query.expected_tier:
            raise ReportGraphContractError("evidence tier does not match the report citation")
        selection = next(
            item for item in selections if item.replay_player_public_id == selected.document.replay_player_public_id
        )
        with self._session_factory() as session:
            evidence = session.scalar(
                select(EvidenceItem).where(
                    EvidenceItem.public_id == query.evidence_public_id,
                    EvidenceItem.replay_id
                    == session.scalar(select(Replay.id).where(Replay.public_id == replay_public_id)),
                )
            )
            if evidence is None:
                raise ReportGraphNotFoundError("report-scoped evidence is unavailable")
            if evidence.tier != tier:
                raise ReportGraphContractError("evidence row tier does not match its report citation")
            source = self._evidence_source(session, evidence, selected.document, selection, index)
            return EvidenceDetailDTO(
                "replay-evidence-inspector-v1",
                selected.document.report_public_id,
                replay_public_id,
                evidence.public_id,
                evidence.tier,
                evidence.source_kind,
                evidence.schema_version,
                source,
            )

    def _read_graph(self, query: FixedReportQuery) -> tuple[PublishedReportGraphDTO, tuple[_SubjectSelection, ...]]:
        resources = load_report_resources()
        with self._session_factory() as session:
            replay = session.scalar(select(Replay).where(Replay.public_id == query.replay_public_id))
            if replay is None:
                raise ReportGraphNotFoundError("requested replay public ID was not found")
            requested = session.scalar(
                select(Report).where(
                    Report.replay_id == replay.id,
                    Report.public_id == query.report_public_id,
                )
            )
            if requested is None:
                raise ReportGraphNotFoundError("requested report does not belong to the exact replay")
            if requested.structured_asset_id is None or requested.rendered_asset_id is None:
                raise ReportGraphContractError("requested report is not fully published")
            structured = session.get(ManagedAsset, requested.structured_asset_id)
            presentation = session.get(ManagedAsset, requested.rendered_asset_id)
            if structured is None or presentation is None:
                raise ReportGraphContractError("requested report asset graph is incomplete")
            matching_jobs: list[Job] = []
            for job in session.scalars(
                select(Job).where(
                    Job.replay_id == replay.id,
                    Job.stage == RENDER_REPORT,
                    Job.component_version == RENDER_REPORT_VERSION,
                    Job.status == "succeeded",
                )
            ):
                if self._mentions_assets(
                    job.output_json,
                    structured.public_id,
                    presentation.public_id,
                ):
                    matching_jobs.append(job)
                    continue
                stage_result = session.scalar(select(JobStageResult).where(JobStageResult.job_id == job.id))
                if stage_result is not None and self._mentions_assets(
                    stage_result.output_json,
                    structured.public_id,
                    presentation.public_id,
                ):
                    matching_jobs.append(job)
            if not matching_jobs:
                raise ReportGraphContractError("published report has no exact successful render stage graph")
            if len(matching_jobs) != 1:
                raise ReportGraphAmbiguousError("multiple render stage graphs claim the exact report")
            validated = self._validate_render_graph(session, replay, matching_jobs[0])
            published: list[PublishedReportDTO] = []
            matched_requested = 0
            for entry in validated.entries:
                report = self._report_for_entry(session, replay, entry)
                item = self._load_report(session, replay, report, entry, resources)
                self._validate_evidence_index(session, replay, item.document)
                published.append(item)
                matched_requested += int(report.id == requested.id)
            if matched_requested != 1:
                raise ReportGraphContractError("requested report membership is not exact")
            replay_wide = tuple(item for item in published if item.document.replay_player_public_id is None)
            players = tuple(item for item in published if item.document.replay_player_public_id is not None)
            if len(replay_wide) != 1:
                raise ReportGraphContractError("render report graph requires one replay-wide report")
            graph = PublishedReportGraphDTO(
                "replay-report-read-model-v1",
                "report-output-v1",
                replay.public_id,
                requested.public_id,
                self._replay_identity(session, replay),
                replay_wide[0],
                players,
            )
            return graph, validated.subjects

    @staticmethod
    def _replay_identity(session: Session, replay: Replay) -> ReportReplayIdentityDTO:
        rows = tuple(
            session.scalars(
                select(ReplayPlayer)
                .where(ReplayPlayer.replay_id == replay.id)
                .order_by(ReplayPlayer.slot_index, ReplayPlayer.public_id)
            )
        )
        players: list[ReportPlayerIdentityDTO] = []
        for row in rows:
            observed = row.observed_json if isinstance(row.observed_json, Mapping) else {}
            faction = observed.get("faction")
            result = observed.get("result")
            players.append(
                ReportPlayerIdentityDTO(
                    row.public_id,
                    row.original_name or f"Player {row.slot_index + 1}",
                    row.slot_index + 1,
                    faction if type(faction) is str else None,
                    result if type(result) is str else None,
                )
            )
        if not players:
            raise ReportGraphContractError("report replay identity has no player subjects")
        names = tuple(item.display_name for item in players)
        joined = " vs ".join(names)
        label = joined if len(names) <= 2 and len(joined) <= 256 else f"{len(names)}-player replay"
        return ReportReplayIdentityDTO(
            label,
            replay.map_name,
            replay.version_string,
            replay.frame_count,
            tuple(players),
        )

    @staticmethod
    def _mentions_assets(value: object, structured_id: str, presentation_id: str) -> bool:
        if not isinstance(value, Mapping):
            return False
        reports = value.get("reports")
        if not isinstance(reports, list):
            return False
        return any(
            isinstance(entry, Mapping)
            and (
                entry.get("structured_asset_public_id") == structured_id
                or entry.get("presentation_asset_public_id") == presentation_id
            )
            for entry in reports
        )

    def _validate_render_graph(self, session: Session, replay: Replay, job: Job) -> _ValidatedGraph:
        result = self._exact_result(session, job)
        if job.output_json != result.output_json:
            raise ReportGraphContractError("render job and immutable stage result disagree")
        input_value = _mapping(
            job.input_json,
            {
                "analysis_plan_version",
                "replay_public_id",
                "replay_sha256",
                "parser_run_id",
                "parser_version",
                "selected_dependency_digest",
                "identity_scope",
                "allow_ollama",
                "report_input_digest",
            },
            "render report input",
        )
        if (
            input_value["analysis_plan_version"] != 1
            or input_value["replay_public_id"] != replay.public_id
            or input_value["replay_sha256"] != replay.sha256
            or type(input_value["allow_ollama"]) is not bool
            or type(input_value["parser_version"]) is not str
            or not input_value["parser_version"]
            or len(input_value["parser_version"]) > 255
            or not _sha256(input_value["selected_dependency_digest"])
            or not _sha256(input_value["report_input_digest"])
        ):
            raise ReportGraphContractError("render report input is not bound to the exact replay graph")
        try:
            _uuid(input_value["parser_run_id"], "render report parser_run_id")
        except ValueError as exc:
            raise ReportGraphContractError("render report parser run identity is invalid") from exc
        parser = session.scalar(
            select(ParserRun).where(
                ParserRun.run_id == input_value["parser_run_id"],
                ParserRun.replay_id == replay.id,
            )
        )
        if parser is None or parser.parser_version != input_value["parser_version"]:
            raise ReportGraphContractError("render report parser version is not authoritative")
        try:
            scope = IdentityAnalysisScope.from_json(input_value["identity_scope"])
        except IdentityScopeError as exc:
            raise ReportGraphContractError("render report identity scope is invalid") from exc
        if scope.to_json() != input_value["identity_scope"]:
            raise ReportGraphContractError("render report identity scope is noncanonical")
        direct = self._dependency(session, replay, job)
        try:
            if input_value["allow_ollama"] is True:
                if direct.stage != ANALYZE_LLM or direct.component_version != ANALYZE_LLM_VERSION:
                    raise ReportGraphContractError("Ollama report graph has the wrong direct dependency")
                llm_result = self._exact_result(session, direct)
                if direct.output_json != llm_result.output_json:
                    raise ReportGraphContractError("LLM job and immutable stage result disagree")
                llm = decode_llm_output(llm_result.output_json)
                assess_job = self._dependency(session, replay, direct)
                if assess_job.stage != ASSESS_STRATEGIES or assess_job.component_version != ASSESS_STRATEGIES_VERSION:
                    raise ReportGraphContractError("LLM report graph requires one assessment dependency")
                assess_result = self._exact_result(session, assess_job)
                assess = decode_assessment_output(assess_result.output_json)
                by_player = {item.replay_player_public_id: item for item in assess}
                if set(by_player) != {item.replay_player_public_id for item in llm}:
                    raise ReportGraphContractError("LLM and assessment subjects disagree")
                subjects = tuple(
                    self._selection(by_player[item.replay_player_public_id], item.analysis_run_id) for item in llm
                )
            else:
                if direct.stage != ASSESS_STRATEGIES or direct.component_version != ASSESS_STRATEGIES_VERSION:
                    raise ReportGraphContractError("deterministic report graph has the wrong direct dependency")
                assess_result = self._exact_result(session, direct)
                if direct.output_json != assess_result.output_json:
                    raise ReportGraphContractError("assessment job and immutable stage result disagree")
                assess = decode_assessment_output(assess_result.output_json)
                assess_job = direct
                subjects = tuple(self._selection(item, None) for item in assess)
        except PipelineCodecError as exc:
            raise ReportGraphContractError("report dependency output is invalid") from exc
        self._validate_job_identity_chain(
            session,
            replay,
            job,
            input_value,
            scope,
            assess_job,
            direct if input_value["allow_ollama"] is True else None,
        )
        self._validate_subject_rows(session, replay, subjects)
        entries = self._report_entries(result.output_json)
        expected = tuple((item.replay_player_public_id, item.analysis_run_id) for item in subjects)
        actual = tuple((item.replay_player_public_id, item.analysis_run_id) for item in entries)
        if actual != expected:
            raise ReportGraphContractError("render report entries disagree with the exact analysis graph")
        return _ValidatedGraph(entries, subjects)

    @staticmethod
    def _dependency(session: Session, replay: Replay, job: Job) -> Job:
        dependencies = tuple(
            session.scalars(
                select(Job)
                .join(JobDependency, Job.id == JobDependency.depends_on_job_id)
                .where(JobDependency.job_id == job.id)
            )
        )
        if len(dependencies) != 1:
            raise ReportGraphContractError("analysis stage requires one exact direct dependency")
        dependency = dependencies[0]
        if dependency.replay_id != replay.id or dependency.status != "succeeded":
            raise ReportGraphContractError("analysis dependency is outside the succeeded replay graph")
        result = ReportQueryService._exact_result(session, dependency)
        if dependency.output_json != result.output_json:
            raise ReportGraphContractError("analysis dependency and immutable stage result disagree")
        return dependency

    def _validate_job_identity_chain(
        self,
        session: Session,
        replay: Replay,
        report: Job,
        report_input: Mapping[str, object],
        scope: IdentityAnalysisScope,
        assess: Job,
        llm: Job | None,
    ) -> None:
        derive = self._dependency(session, replay, assess)
        if derive.stage != DERIVE_FEATURES or derive.component_version != DERIVE_FEATURES_VERSION:
            raise ReportGraphContractError("assessment is not bound to one derive-features stage")
        observation = self._dependency(session, replay, derive)
        if observation.stage != IMPORT_OBSERVATIONS or observation.component_version != IMPORT_OBSERVATIONS_VERSION:
            raise ReportGraphContractError("derive-features is not bound to one observation stage")
        observation_output = observation.output_json
        if (
            not isinstance(observation_output, Mapping)
            or observation_output.get("selected_dependency_digest") != report_input["selected_dependency_digest"]
            or observation_output.get("parser_run_id") != report_input["parser_run_id"]
        ):
            raise ReportGraphContractError("report dependency digest disagrees with observation authority")
        base_input: dict[str, object] = {
            "analysis_plan_version": report_input["analysis_plan_version"],
            "replay_public_id": report_input["replay_public_id"],
            "replay_sha256": report_input["replay_sha256"],
            "parser_run_id": report_input["parser_run_id"],
            "parser_version": report_input["parser_version"],
            "selected_dependency_digest": report_input["selected_dependency_digest"],
            "identity_scope": scope.to_json(),
        }
        base_identity: dict[str, object] = {
            "analysis_plan_version": report_input["analysis_plan_version"],
            "observation_job_key": observation.idempotency_key,
            "selected_dependency_digest": report_input["selected_dependency_digest"],
            "identity_scope": scope.to_json(),
        }
        derive_identity = base_identity
        assess_identity = {**base_identity, "derive_job_key": derive.idempotency_key}
        if (
            derive.input_json != base_input
            or derive.idempotency_key
            != content_key(DERIVE_FEATURES, DERIVE_FEATURES_VERSION, replay.sha256, derive_identity)
            or assess.input_json != {**base_input, "derive_input_digest": input_digest(assess_identity)}
            or assess.idempotency_key
            != content_key(ASSESS_STRATEGIES, ASSESS_STRATEGIES_VERSION, replay.sha256, assess_identity)
        ):
            raise ReportGraphContractError("deterministic analysis stage identity is invalid")
        if llm is not None:
            llm_identity = {
                **base_identity,
                "assess_job_key": assess.idempotency_key,
                "provider_mode": "ollama",
            }
            if llm.input_json != {
                **base_input,
                "allow_ollama": True,
                "assess_input_digest": input_digest(llm_identity),
            } or llm.idempotency_key != content_key(ANALYZE_LLM, ANALYZE_LLM_VERSION, replay.sha256, llm_identity):
                raise ReportGraphContractError("LLM analysis stage identity is invalid")
        report_identity = {
            **base_identity,
            "allow_ollama": llm is not None,
            "assess_job_key": assess.idempotency_key,
            "llm_job_key": llm.idempotency_key if llm is not None else None,
        }
        expected_report_input = {
            **base_input,
            "allow_ollama": llm is not None,
            "report_input_digest": input_digest(report_identity),
        }
        if report.input_json != expected_report_input or report.idempotency_key != content_key(
            RENDER_REPORT, RENDER_REPORT_VERSION, replay.sha256, report_identity
        ):
            raise ReportGraphContractError("render report stage identity is invalid")

    @staticmethod
    def _selection(item: PlayerAssessmentSelection, analysis_run_id: str | None) -> _SubjectSelection:
        return _SubjectSelection(
            item.replay_player_public_id,
            analysis_run_id,
            item.feature_set_public_ids,
            item.strategy_cache_key,
            item.longitudinal_run_id,
        )

    @staticmethod
    def _exact_result(session: Session, job: Job) -> JobStageResult:
        result = session.scalar(select(JobStageResult).where(JobStageResult.job_id == job.id))
        if (
            result is None
            or result.stage != job.stage
            or result.component_version != job.component_version
            or result.idempotency_key != job.idempotency_key
        ):
            raise ReportGraphContractError("job has no exact immutable stage result")
        return result

    @staticmethod
    def _validate_subject_rows(session: Session, replay: Replay, subjects: tuple[_SubjectSelection, ...]) -> None:
        if not subjects or tuple(subjects) != tuple(
            sorted(subjects, key=lambda item: item.replay_player_public_id or "")
        ):
            raise ReportGraphContractError("report subjects must be nonempty and canonical")
        for subject in subjects:
            player_id = None
            if subject.replay_player_public_id is not None:
                player = session.scalar(
                    select(ReplayPlayer).where(
                        ReplayPlayer.public_id == subject.replay_player_public_id,
                        ReplayPlayer.replay_id == replay.id,
                    )
                )
                if player is None:
                    raise ReportGraphContractError("report subject is outside the replay")
                player_id = player.id
            feature_sets = tuple(
                session.scalars(select(FeatureSet).where(FeatureSet.public_id.in_(subject.feature_set_public_ids)))
            )
            if len(feature_sets) != len(subject.feature_set_public_ids) or any(
                row.replay_id != replay.id or row.replay_player_id != player_id or row.status != "succeeded"
                for row in feature_sets
            ):
                raise ReportGraphContractError("report subject feature graph is invalid")
            if subject.analysis_run_id is not None:
                run = session.scalar(select(AnalysisRun).where(AnalysisRun.run_id == subject.analysis_run_id))
                if run is None or run.replay_id != replay.id or run.replay_player_id != player_id:
                    raise ReportGraphContractError("report subject analysis run is cross-run or cross-replay")

    @staticmethod
    def _report_entries(value: object) -> tuple[_ReportEntry, ...]:
        root = _mapping(value, {"schema_version", "reports"}, "render report output")
        if root["schema_version"] != "report-output-v1" or not isinstance(root["reports"], list):
            raise ReportGraphContractError("render report output version is invalid")
        entries: list[_ReportEntry] = []
        for raw in root["reports"]:
            item = _mapping(
                raw,
                {
                    "replay_player_public_id",
                    "analysis_run_id",
                    "structured_asset_public_id",
                    "presentation_asset_public_id",
                },
                "render report entry",
            )
            player = item["replay_player_public_id"]
            analysis = item["analysis_run_id"]
            if player is not None:
                _uuid(player, "replay_player_public_id")
            if analysis is not None:
                _uuid(analysis, "analysis_run_id")
            entries.append(
                _ReportEntry(
                    cast(str | None, player),
                    cast(str | None, analysis),
                    _uuid(item["structured_asset_public_id"], "structured_asset_public_id"),
                    _uuid(item["presentation_asset_public_id"], "presentation_asset_public_id"),
                )
            )
        ordered = tuple(sorted(entries, key=lambda item: item.replay_player_public_id or ""))
        identities = tuple(item.replay_player_public_id for item in ordered)
        if tuple(entries) != ordered or len(identities) != len(set(identities)):
            raise ReportGraphContractError("render report entries must be sorted and unique")
        return ordered

    @staticmethod
    def _report_for_entry(session: Session, replay: Replay, entry: _ReportEntry) -> Report:
        structured_id = session.scalar(
            select(ManagedAsset.id).where(ManagedAsset.public_id == entry.structured_asset_public_id)
        )
        presentation_id = session.scalar(
            select(ManagedAsset.id).where(ManagedAsset.public_id == entry.presentation_asset_public_id)
        )
        if structured_id is None or presentation_id is None:
            raise ReportGraphContractError("render report entry names an unknown managed asset")
        rows = tuple(
            session.scalars(
                select(Report).where(
                    Report.replay_id == replay.id,
                    Report.structured_asset_id == structured_id,
                    Report.rendered_asset_id == presentation_id,
                )
            )
        )
        if len(rows) != 1:
            raise ReportGraphContractError("render report entry does not select one immutable report")
        report = rows[0]
        player_public_id = None
        if report.replay_player_id is not None:
            player = session.get(ReplayPlayer, report.replay_player_id)
            if player is None or player.replay_id != replay.id:
                raise ReportGraphContractError("report player link is invalid")
            player_public_id = player.public_id
        analysis_run_id = None
        if report.analysis_run_id is not None:
            run = session.get(AnalysisRun, report.analysis_run_id)
            if run is None or run.replay_id != replay.id or run.replay_player_id != report.replay_player_id:
                raise ReportGraphContractError("report analysis link is cross-run or cross-replay")
            analysis_run_id = run.run_id
        if (player_public_id, analysis_run_id) != (
            entry.replay_player_public_id,
            entry.analysis_run_id,
        ):
            raise ReportGraphContractError("report row disagrees with its render stage subject")
        return report

    def _load_report(
        self,
        session: Session,
        replay: Replay,
        row: Report,
        entry: _ReportEntry,
        resources: ReportResources,
    ) -> PublishedReportDTO:
        if row.report_version != REPORT_VERSION:
            raise ReportGraphContractError("report row version is unsupported")
        document = self._document(row.report_json)
        if (
            document.report_public_id != row.public_id
            or document.replay_public_id != replay.public_id
            or document.replay_sha256 != replay.sha256
            or document.report_version != row.report_version
            or document.input_digest != row.input_digest
            or document.cache_key != row.cache_key
            or document.replay_player_public_id != entry.replay_player_public_id
            or document.ollama.analysis_run_id != entry.analysis_run_id
            or document.ollama.requested is not (entry.analysis_run_id is not None)
        ):
            raise ReportGraphContractError("report row and canonical document identity drift")
        expected_cache = hashlib.sha256(
            _canonical_bytes(
                {
                    "cache_schema": "replay-report-cache-v1",
                    "report_version": document.report_version,
                    "input_digest": document.input_digest,
                    "include_validated_ollama": document.ollama.requested,
                    "display_policy_version": DISPLAY_POLICY_VERSION,
                    "html_template_sha256": resources.html_template_sha256,
                    "document_schema_sha256": resources.document_schema_sha256,
                }
            )
        ).hexdigest()
        public_name = _canonical_bytes(
            {
                "replay_public_id": document.replay_public_id,
                "replay_player_public_id": document.replay_player_public_id,
                "report_version": document.report_version,
                "input_digest": document.input_digest,
            }
        ).decode("utf-8")
        if document.cache_key != expected_cache or document.report_public_id != str(
            uuid5(_REPORT_NAMESPACE, public_name)
        ):
            raise ReportGraphContractError("report cache or public identity is invalid")
        validate_document(document)
        if row.structured_asset_id is None or row.rendered_asset_id is None:
            raise ReportGraphContractError("published report has missing asset links")
        structured_row = session.get(ManagedAsset, row.structured_asset_id)
        presentation_row = session.get(ManagedAsset, row.rendered_asset_id)
        if structured_row is None or presentation_row is None or structured_row.id == presentation_row.id:
            raise ReportGraphContractError("published report asset links are invalid")
        structured_bytes = render_json(document)
        structured = self._asset(structured_row, "report_structured_json", structured_bytes)
        html = render_html(document)
        text = render_text(document)
        bundle_mapping = {
            "schema_version": "report-presentation-bundle-v1",
            "report_version": REPORT_VERSION,
            "display_policy_version": DISPLAY_POLICY_VERSION,
            "html_template_sha256": resources.html_template_sha256,
            "document_schema_sha256": resources.document_schema_sha256,
            "html": {"sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(), "text": html},
            "text": {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text},
        }
        bundle_bytes = _canonical_bytes(bundle_mapping)
        presentation = self._asset(
            presentation_row,
            "report_presentation_bundle",
            bundle_bytes,
        )
        created_at = row.created_at.replace(tzinfo=UTC) if row.created_at.tzinfo is None else row.created_at.astimezone(UTC)
        return PublishedReportDTO(document, structured, presentation, html, text, created_at)

    def _asset(
        self,
        row: ManagedAsset,
        kind: Literal["report_structured_json", "report_presentation_bundle"],
        expected_bytes: bytes,
    ) -> PublishedReportAssetDTO:
        digest = hashlib.sha256(expected_bytes).hexdigest()
        expected_public_id = str(uuid5(_ASSET_NAMESPACE, f"{kind}:{digest}"))
        try:
            stored = self._store.verify(digest)
            relative_path = stored.path.relative_to(self._settings.data_root).as_posix()
            actual = stored.path.read_bytes()
        except (ContentStorageError, OSError, ValueError) as exc:
            raise ReportGraphContractError("managed report asset content is unavailable") from exc
        if (
            row.public_id != expected_public_id
            or row.sha256 != digest
            or row.kind != kind
            or row.media_type != "application/json"
            or row.size_bytes != len(expected_bytes)
            or row.relative_path != relative_path
            or stored.size != len(expected_bytes)
            or actual != expected_bytes
        ):
            raise ReportGraphContractError("managed report asset identity or bytes drift")
        return PublishedReportAssetDTO(row.public_id, row.sha256, kind, "application/json", row.size_bytes)

    @staticmethod
    def _document(value: object) -> ReportDocument:
        root = _mapping(
            value,
            {
                "schema_version",
                "report_public_id",
                "report_version",
                "input_digest",
                "cache_key",
                "replay_public_id",
                "replay_sha256",
                "replay_player_public_id",
                "lifecycle",
                "evidence_availability",
                "quality_issues",
                "observed",
                "derived",
                "inferred",
                "ollama",
                "warnings",
            },
            "report document",
        )
        try:
            lifecycle_raw = _mapping(
                root["lifecycle"],
                {
                    "lifecycle_state",
                    "parser_completion_status",
                    "telemetry_status",
                    "telemetry_runner_status",
                },
                "report lifecycle",
            )
            ollama_raw = _mapping(
                root["ollama"],
                {
                    "requested",
                    "status",
                    "analysis_run_id",
                    "provider",
                    "model_name",
                    "model_digest",
                    "prompt_version",
                    "response_schema_version",
                    "diagnostic_codes",
                    "validated_prose",
                },
                "report Ollama status",
            )
            document = ReportDocument(
                cast(Literal["replay-report-v1"], root["schema_version"]),
                cast(str, root["report_public_id"]),
                cast(str, root["report_version"]),
                cast(str, root["input_digest"]),
                cast(str, root["cache_key"]),
                cast(str, root["replay_public_id"]),
                cast(str, root["replay_sha256"]),
                cast(str | None, root["replay_player_public_id"]),
                ReportLifecycle(
                    cast(str, lifecycle_raw["lifecycle_state"]),
                    cast(str | None, lifecycle_raw["parser_completion_status"]),
                    cast(str | None, lifecycle_raw["telemetry_status"]),
                    cast(str | None, lifecycle_raw["telemetry_runner_status"]),
                ),
                ReportQueryService._values(root["evidence_availability"]),
                ReportQueryService._issues(root["quality_issues"]),
                ReportQueryService._values(root["observed"]),
                ReportQueryService._values(root["derived"]),
                ReportQueryService._values(root["inferred"]),
                OllamaReportStatus(
                    cast(bool, ollama_raw["requested"]),
                    cast(OllamaStatus, ollama_raw["status"]),
                    cast(str | None, ollama_raw["analysis_run_id"]),
                    cast(str | None, ollama_raw["provider"]),
                    cast(str | None, ollama_raw["model_name"]),
                    cast(str | None, ollama_raw["model_digest"]),
                    cast(str | None, ollama_raw["prompt_version"]),
                    cast(str | None, ollama_raw["response_schema_version"]),
                    tuple(cast(list[str], ollama_raw["diagnostic_codes"])),
                    _safe_value(ollama_raw["validated_prose"]),
                ),
                tuple(cast(list[str], root["warnings"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReportGraphContractError("stored report document is invalid") from exc
        if document_to_mapping(document) != value or render_json(document) != _canonical_bytes(value):
            raise ReportGraphContractError("stored report document is noncanonical")
        return document

    @staticmethod
    def _values(value: object) -> tuple[ReportValue, ...]:
        if not isinstance(value, list):
            raise ReportGraphContractError("report values must be a canonical array")
        output: list[ReportValue] = []
        for raw in value:
            item = _mapping(
                raw,
                {
                    "claim_id",
                    "section",
                    "label",
                    "raw_value",
                    "unit",
                    "availability",
                    "unavailable_reason",
                    "scope",
                    "frame_window",
                    "evidence",
                    "details",
                },
                "report value",
            )
            evidence_raw = item["evidence"]
            if not isinstance(evidence_raw, list):
                raise ReportGraphContractError("report evidence references must be an array")
            evidence = tuple(
                ReportEvidenceRef(
                    cast(str, _mapping(ref, {"public_id", "tier"}, "report evidence")["public_id"]),
                    cast(ReportEvidenceTier, _mapping(ref, {"public_id", "tier"}, "report evidence")["tier"]),
                )
                for ref in evidence_raw
            )
            window_raw = item["frame_window"]
            if window_raw is not None and (not isinstance(window_raw, list) or len(window_raw) != 2):
                raise ReportGraphContractError("report frame window is invalid")
            output.append(
                ReportValue(
                    cast(str, item["claim_id"]),
                    cast(str, item["section"]),
                    cast(str, item["label"]),
                    _safe_value(item["raw_value"]),
                    cast(str | None, item["unit"]),
                    cast(ReportAvailability, item["availability"]),
                    cast(str | None, item["unavailable_reason"]),
                    _safe_value(item["scope"]),
                    None if window_raw is None else (cast(int, window_raw[0]), cast(int, window_raw[1])),
                    evidence,
                    _safe_value(item["details"]),
                )
            )
        return tuple(output)

    @staticmethod
    def _issues(value: object) -> tuple[ReportQualityIssue, ...]:
        if not isinstance(value, list):
            raise ReportGraphContractError("quality issues must be a canonical array")
        return tuple(
            ReportQualityIssue(
                cast(str, item["public_id"]),
                cast(str, item["stage"]),
                cast(str, item["issue_code"]),
                cast(str, item["severity"]),
                _safe_value(item["details"]),
                cast(bool, item["resolved"]),
            )
            for raw in value
            for item in (
                _mapping(
                    raw,
                    {"public_id", "stage", "issue_code", "severity", "details", "resolved"},
                    "quality issue",
                ),
            )
        )

    @staticmethod
    def _document_evidence_index(document: ReportDocument) -> dict[str, ReportEvidenceTier]:
        index: dict[str, ReportEvidenceTier] = {}
        for value in (
            *document.evidence_availability,
            *document.observed,
            *document.derived,
            *document.inferred,
        ):
            for reference in value.evidence:
                prior = index.setdefault(reference.public_id, reference.tier)
                if prior != reference.tier:
                    raise ReportGraphContractError("report evidence citation claims multiple tiers")
        return index

    @staticmethod
    def _source_claim(
        document: ReportDocument,
        claim_id: str,
        evidence: EvidenceItem,
    ) -> ReportValue:
        matches = tuple(
            value
            for value in (
                *document.observed,
                *document.derived,
                *document.inferred,
            )
            if value.claim_id == claim_id
            and any(
                reference.public_id == evidence.public_id and reference.tier == evidence.tier
                for reference in value.evidence
            )
        )
        if len(matches) != 1:
            raise ReportGraphContractError("evidence source does not select one immutable report claim")
        return matches[0]

    def _validate_evidence_index(self, session: Session, replay: Replay, document: ReportDocument) -> None:
        index = self._document_evidence_index(document)
        rows = (
            tuple(session.scalars(select(EvidenceItem).where(EvidenceItem.public_id.in_(tuple(index)))))
            if index
            else ()
        )
        by_public_id = {row.public_id: row for row in rows}
        if set(by_public_id) != set(index):
            raise ReportGraphContractError("report cites unavailable evidence")
        for public_id, tier in index.items():
            row = by_public_id[public_id]
            if (
                row.replay_id != replay.id
                or row.tier != tier
                or row.source_kind not in _EVIDENCE_SOURCE_KINDS[tier]
                or row.schema_version < 1
                or (tier != "observed" and row.schema_version != 1)
            ):
                raise ReportGraphContractError("report evidence source, tier, or version is invalid")

    def _evidence_source(
        self,
        session: Session,
        evidence: EvidenceItem,
        document: ReportDocument,
        selection: _SubjectSelection,
        report_index: Mapping[str, ReportEvidenceTier],
    ) -> EvidenceSourceDTO:
        if evidence.source_kind == "parser_command":
            return self._command_source(session, evidence, document)
        if evidence.source_kind == "telemetry_event":
            return self._telemetry_source(session, evidence, document)
        if evidence.source_kind == "feature":
            return self._feature_source(session, evidence, document, selection, report_index)
        if evidence.source_kind == "strategy_rule":
            return self._rule_source(session, evidence, document, selection, report_index)
        if evidence.source_kind == "longitudinal_corpus":
            return self._longitudinal_source(session, evidence, document, selection, report_index)
        if evidence.source_kind == "llm":
            return self._inferred_source(session, evidence, document, selection, report_index)
        raise ReportGraphContractError("evidence source kind is unsupported")

    @staticmethod
    def _command_source(
        session: Session,
        evidence: EvidenceItem,
        document: ReportDocument,
    ) -> ObservedCommandEvidenceDTO:
        rows = tuple(session.scalars(select(ReplayCommand).where(ReplayCommand.evidence_item_id == evidence.id)))
        if len(rows) != 1:
            raise ReportGraphContractError("parser command evidence is ambiguous")
        row = rows[0]
        run = session.get(ParserRun, row.parser_run_id)
        replay = session.get(Replay, evidence.replay_id)
        player_public_id = None
        if row.replay_player_id is not None:
            player = session.get(ReplayPlayer, row.replay_player_id)
            if player is None or player.replay_id != evidence.replay_id or player.parser_run_id != row.parser_run_id:
                raise ReportGraphContractError("parser command player ownership is invalid")
            player_public_id = player.public_id
        if (
            replay is None
            or run is None
            or run.replay_id != evidence.replay_id
            or run.status != "succeeded"
            or evidence.tier != "observed"
            or evidence.parser_run_id != run.id
            or evidence.telemetry_run_id is not None
            or evidence.schema_version != run.schema_version
            or row.replay_id != evidence.replay_id
        ):
            raise ReportGraphContractError("parser command source or version is invalid")
        try:
            validate_observed_evidence_identity(
                parser_command_evidence_identity(
                    replay.public_id,
                    run.parser_version,
                    row.start_offset,
                ),
                public_id=evidence.public_id,
                source_kind=evidence.source_kind,
                source_key=evidence.source_key,
            )
        except (TypeError, ValueError) as error:
            raise ReportGraphContractError("observed evidence identity is invalid") from error
        claim = ReportQueryService._source_claim(
            document,
            f"observed:parser_command:{evidence.public_id}",
            evidence,
        )
        message_name = row.message_name or f"message_{row.message_type}"
        if (
            claim.section != "timeline"
            or claim.label != message_name
            or claim.raw_value
            != _safe_value(
                {
                    "arguments": row.arguments_json,
                    "message_name": row.message_name,
                    "message_type": row.message_type,
                }
            )
            or claim.availability != "available"
            or claim.frame_window != (row.frame, row.frame)
            or claim.details
            != _safe_value({"source_kind": evidence.source_kind, "schema_version": evidence.schema_version})
        ):
            raise ReportGraphContractError("parser command source drifted from its immutable report claim")
        return ObservedCommandEvidenceDTO(
            "replay_command",
            run.run_id,
            run.parser_version,
            run.schema_version,
            row.start_offset,
            row.end_offset,
            row.frame,
            row.message_type,
            message_name,
            player_public_id,
            _safe_value(row.arguments_json),
        )

    @staticmethod
    def _telemetry_source(
        session: Session,
        evidence: EvidenceItem,
        document: ReportDocument,
    ) -> ObservedTelemetryEvidenceDTO:
        rows = tuple(session.scalars(select(TelemetryEvent).where(TelemetryEvent.evidence_item_id == evidence.id)))
        if len(rows) != 1:
            raise ReportGraphContractError("telemetry event evidence is ambiguous")
        row = rows[0]
        run = session.get(TelemetryRun, row.telemetry_run_id)
        if (
            run is None
            or run.replay_id != evidence.replay_id
            or run.status != "succeeded"
            or evidence.tier != "observed"
            or evidence.telemetry_run_id != run.id
            or evidence.parser_run_id is not None
            or evidence.schema_version != row.schema_version
            or row.schema_version != run.schema_version
        ):
            raise ReportGraphContractError("telemetry event source or version is invalid")
        try:
            validate_observed_evidence_identity(
                telemetry_event_evidence_identity(run.run_id, row.sequence),
                public_id=evidence.public_id,
                source_kind=evidence.source_kind,
                source_key=evidence.source_key,
            )
        except (TypeError, ValueError) as error:
            raise ReportGraphContractError("observed evidence identity is invalid") from error
        claim = ReportQueryService._source_claim(
            document,
            f"observed:telemetry_event:{evidence.public_id}",
            evidence,
        )
        if (
            claim.section != "timeline"
            or claim.label != row.event_type
            or claim.raw_value != _safe_value(row.payload_json)
            or claim.availability != "available"
            or claim.frame_window != (row.frame, row.frame)
            or claim.details
            != _safe_value({"source_kind": evidence.source_kind, "schema_version": evidence.schema_version})
        ):
            raise ReportGraphContractError("telemetry source drifted from its immutable report claim")
        return ObservedTelemetryEvidenceDTO(
            "telemetry_event",
            run.run_id,
            run.engine_build,
            row.schema_version,
            row.sequence,
            row.frame,
            row.event_type,
            _safe_value(row.payload_json),
        )

    def _feature_source(
        self,
        session: Session,
        evidence: EvidenceItem,
        document: ReportDocument,
        selection: _SubjectSelection,
        report_index: Mapping[str, ReportEvidenceTier],
    ) -> DerivedFeatureEvidenceDTO:
        rows = tuple(session.scalars(select(Feature).where(Feature.evidence_item_id == evidence.id)))
        if len(rows) != 1:
            raise ReportGraphContractError("derived feature evidence is ambiguous")
        row = rows[0]
        feature_set = session.get(FeatureSet, row.feature_set_id)
        player_id = self._document_player_id(session, document)
        if (
            feature_set is None
            or feature_set.public_id not in selection.feature_set_public_ids
            or feature_set.replay_id != evidence.replay_id
            or feature_set.replay_player_id != player_id
            or feature_set.status != "succeeded"
            or row.replay_player_id != player_id
            or evidence.tier != "derived"
            or evidence.schema_version != 1
        ):
            raise ReportGraphContractError("derived feature source or version is invalid")
        claim = self._source_claim(document, f"feature:{row.name}:{row.public_id}", evidence)
        raw = self._feature_raw(row)
        if (
            claim.section != "features"
            or claim.label != row.name
            or claim.raw_value != _safe_value(raw)
            or claim.unit != row.unit
            or claim.availability != row.quality
            or claim.unavailable_reason != row.quality_reason
            or claim.scope != _safe_value({"scope_type": row.scope_type, "scope_key": row.scope_key})
            or claim.frame_window != (row.frame_start, row.frame_end)
            or claim.details
            != _safe_value(
                {
                    "extractor": {
                        "name": feature_set.extractor_name,
                        "version": feature_set.extractor_version,
                        "input_digest": feature_set.input_digest,
                    },
                    "feature": row.details_json,
                }
            )
        ):
            raise ReportGraphContractError("derived feature source identity or immutable report claim drifted")
        links = self._feature_links(session, row, evidence.replay_id, report_index)
        return DerivedFeatureEvidenceDTO(
            "derived_feature",
            row.public_id,
            feature_set.public_id,
            row.name,
            feature_set.extractor_name,
            feature_set.extractor_version,
            _safe_value(raw),
            row.unit,
            _safe_value({"scope_type": row.scope_type, "scope_key": row.scope_key}),
            row.frame_start,
            row.frame_end,
            row.quality,
            row.quality_reason,
            _safe_value(row.details_json),
            links,
        )

    @staticmethod
    def _feature_raw(row: Feature) -> object | None:
        return {
            "integer": row.integer_value,
            "real": row.real_value,
            "text": row.text_value,
            "boolean": row.boolean_value,
            "json": row.json_value,
        }[row.value_type]

    def _feature_links(
        self,
        session: Session,
        feature: Feature,
        replay_id: int,
        report_index: Mapping[str, ReportEvidenceTier],
    ) -> tuple[EvidenceLinkDTO, ...]:
        rows = tuple(
            (role, item)
            for role, item in session.execute(
                select(FeatureEvidence.role, EvidenceItem)
                .join(EvidenceItem, EvidenceItem.id == FeatureEvidence.evidence_item_id)
                .where(FeatureEvidence.feature_id == feature.id)
                .order_by(FeatureEvidence.role, EvidenceItem.public_id)
            )
        )
        links = tuple(
            EvidenceLinkDTO(
                item.public_id,
                cast(ReportEvidenceTier, item.tier),
                cast(EvidenceRole, role),
            )
            for role, item in rows
        )
        self._validate_links(rows, replay_id, report_index, allowed_tiers={"observed"})
        return links

    def _rule_source(
        self,
        session: Session,
        evidence: EvidenceItem,
        document: ReportDocument,
        selection: _SubjectSelection,
        report_index: Mapping[str, ReportEvidenceTier],
    ) -> DerivedAssessmentEvidenceDTO:
        rows = tuple(
            session.scalars(select(StrategyAssessment).where(StrategyAssessment.evidence_item_id == evidence.id))
        )
        if len(rows) != 1:
            raise ReportGraphContractError("derived assessment evidence is ambiguous")
        row = rows[0]
        player_id = self._document_player_id(session, document)
        if (
            row.replay_id != evidence.replay_id
            or row.replay_player_id != player_id
            or row.method != "rule"
            or row.analysis_run_id is not None
            or row.taxonomy_version is None
            or row.rule_version is None
            or evidence.tier != "derived"
            or evidence.schema_version != 1
        ):
            raise ReportGraphContractError("derived assessment source or version is invalid")
        player_public_id = document.replay_player_public_id or "replay"
        source_key = (
            f"strategy-rule:{document.replay_public_id}:{player_public_id}:{selection.strategy_cache_key}:"
            f"{row.strategy_label}:{row.phase}:{row.frame_start}:{row.frame_end}"
        )
        if not isinstance(row.details_json, Mapping):
            raise ReportGraphContractError("strategy assessment details are not canonical")
        details = cast(Mapping[str, object], row.details_json)
        reason_value = details.get("reason")
        reason = None if row.quality == "available" else reason_value
        raw = None
        if row.quality != "unavailable":
            raw = {
                "strategy_label": row.strategy_label,
                "phase": row.phase,
                "confidence": row.confidence,
            }
        claim = self._source_claim(
            document,
            f"strategy:{row.strategy_label}:{row.public_id}",
            evidence,
        )
        if (
            evidence.source_key != source_key
            or evidence.public_id != str(uuid5(NAMESPACE_URL, f"evidence:{source_key}"))
            or row.public_id != str(uuid5(NAMESPACE_URL, f"strategy-assessment:{source_key}"))
            or (reason is not None and (type(reason) is not str or not reason))
            or claim.section != "strategy"
            or claim.label != row.strategy_label
            or claim.raw_value != _safe_value(raw)
            or claim.availability != row.quality
            or claim.unavailable_reason != reason
            or claim.frame_window != (row.frame_start, row.frame_end)
            or claim.details
            != _safe_value(
                {
                    "method": row.method,
                    "taxonomy_version": row.taxonomy_version,
                    "rule_version": row.rule_version,
                    "model_version": row.model_version,
                    "assessment": row.details_json,
                }
            )
        ):
            raise ReportGraphContractError("derived assessment source identity or immutable report claim drifted")
        citations = self._assessment_links(
            session,
            row,
            evidence.replay_id,
            report_index,
            allowed_tiers={"observed", "derived"},
        )
        return DerivedAssessmentEvidenceDTO(
            "derived_assessment",
            row.public_id,
            row.strategy_label,
            row.phase,
            row.taxonomy_version,
            row.rule_version,
            row.frame_start,
            row.frame_end,
            row.confidence,
            row.quality,
            reason,
            _safe_value(row.details_json),
            citations,
        )

    def _longitudinal_source(
        self,
        session: Session,
        evidence: EvidenceItem,
        document: ReportDocument,
        selection: _SubjectSelection,
        report_index: Mapping[str, ReportEvidenceTier],
    ) -> DerivedLongitudinalEvidenceDTO:
        rows = tuple(
            session.scalars(select(LongitudinalResult).where(LongitudinalResult.evidence_item_id == evidence.id))
        )
        if len(rows) != 1:
            raise ReportGraphContractError("longitudinal evidence is ambiguous")
        row = rows[0]
        run = session.get(LongitudinalRun, row.longitudinal_run_id)
        if (
            run is None
            or run.run_id != selection.longitudinal_run_id
            or run.status != "succeeded"
            or evidence.tier != "derived"
            or evidence.source_kind != "longitudinal_corpus"
            or evidence.schema_version != 1
        ):
            raise ReportGraphContractError("longitudinal source or version is invalid")
        storage = _mapping(
            row.statistics_json,
            {"storage_schema", "public_statistics", "member_snapshots", "evidence_anchor"},
            "longitudinal result storage",
        )
        public_statistics = storage["public_statistics"]
        snapshots = storage["member_snapshots"]
        anchor = storage["evidence_anchor"]
        if (
            storage["storage_schema"] != "longitudinal-result-storage-v1"
            or not isinstance(public_statistics, Mapping)
            or not isinstance(snapshots, list)
            or not isinstance(anchor, Mapping)
        ):
            raise ReportGraphContractError("longitudinal result storage is outside its accepted schema")
        member_rows = tuple(
            session.execute(
                select(LongitudinalMember, EvidenceItem)
                .join(EvidenceItem, EvidenceItem.id == LongitudinalMember.evidence_item_id)
                .where(LongitudinalMember.longitudinal_result_id == row.id)
                .order_by(EvidenceItem.public_id)
            )
        )
        try:
            member_dtos = tuple(
                LongitudinalMemberDTO.from_mapping(cast(Mapping[str, object], snapshot)) for snapshot in snapshots
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReportGraphContractError("longitudinal member snapshots are invalid") from exc
        if _canonical_bytes(snapshots) != _canonical_bytes([item.as_canonical() for item in member_dtos]):
            raise ReportGraphContractError("longitudinal member snapshots are noncanonical")
        members_by_evidence = {item.public_id: member for member, item in member_rows}
        if len(members_by_evidence) != len(member_rows) or set(members_by_evidence) != {
            item.evidence_public_id for item in member_dtos
        }:
            raise ReportGraphContractError("longitudinal member graph drifted from its stored snapshots")
        current_player = session.get(Player, run.player_id)
        if (
            current_player is None
            or current_player.identity_revision != run.identity_revision
            or current_player.retired_at is not None
        ):
            raise ReportGraphContractError("longitudinal run is outside the current canonical identity revision")
        for snapshot in member_dtos:
            member = members_by_evidence[snapshot.evidence_public_id]
            member_replay = session.get(Replay, member.replay_id)
            member_player = session.get(ReplayPlayer, member.replay_player_id)
            feature_set = session.get(FeatureSet, member.feature_set_id)
            feature = session.get(Feature, member.feature_id) if member.feature_id is not None else None
            member_evidence = session.get(EvidenceItem, member.evidence_item_id)
            direct_rows = (
                ()
                if feature is None
                else tuple(
                    session.scalars(
                        select(EvidenceItem)
                        .join(FeatureEvidence, FeatureEvidence.evidence_item_id == EvidenceItem.id)
                        .where(FeatureEvidence.feature_id == feature.id, FeatureEvidence.role == "input")
                        .order_by(EvidenceItem.source_kind, EvidenceItem.source_key, EvidenceItem.public_id)
                    )
                )
            )
            actual_direct = tuple(
                (item.public_id, item.tier, item.source_kind, item.source_key, item.schema_version)
                for item in direct_rows
            )
            expected_direct = tuple(
                (item.public_id, item.tier, item.source_kind, item.source_key, item.schema_version)
                for item in snapshot.direct_evidence
            )
            if (
                member_replay is None
                or member_player is None
                or feature_set is None
                or feature is None
                or member_evidence is None
                or member.strategy_assessment_id is not None
                or member_player.player_id != run.player_id
                or member_player.replay_id != member_replay.id
                or feature_set.replay_id != member_replay.id
                or feature_set.replay_player_id != member_player.id
                or feature.feature_set_id != feature_set.id
                or feature.replay_player_id != member_player.id
                or feature.evidence_item_id != member_evidence.id
                or member_replay.public_id != snapshot.replay_public_id
                or member_replay.sha256 != snapshot.replay_sha256
                or member_replay.start_time != snapshot.replay_start_time
                or member_replay.version_string != snapshot.replay_version
                or member_replay.lifecycle_state != snapshot.lifecycle_state
                or member_player.public_id != snapshot.replay_player_public_id
                or feature_set.public_id != snapshot.feature_set_public_id
                or feature_set.extractor_name != snapshot.feature_set_extractor_name
                or feature_set.extractor_version != snapshot.feature_set_extractor_version
                or feature_set.input_digest != snapshot.feature_set_input_digest
                or feature_set.settings_json != snapshot.feature_set_settings
                or feature.public_id != snapshot.feature_public_id
                or feature.name != snapshot.feature_name
                or feature.value_type != snapshot.feature_value_type
                or self._feature_raw(feature) != snapshot.raw_value
                or feature.unit != snapshot.unit
                or feature.frame_start != snapshot.frame_start
                or feature.frame_end != snapshot.frame_end
                or feature.quality != snapshot.quality
                or feature.quality_reason != snapshot.reason
                or feature.scope_type != snapshot.feature_scope_type
                or feature.scope_key != snapshot.feature_scope_key
                or feature.details_json != snapshot.feature_details
                or member_evidence.public_id != snapshot.evidence_public_id
                or member_evidence.tier != "derived"
                or member_evidence.source_kind != snapshot.derived_evidence_source_kind
                or member_evidence.source_key != snapshot.derived_evidence_source_key
                or member_evidence.schema_version != snapshot.derived_evidence_schema_version
                or actual_direct != expected_direct
                or any(item.replay_id != member_replay.id or item.tier != "observed" for item in direct_rows)
            ):
                raise ReportGraphContractError("longitudinal member live graph drifted from its snapshot")
        self._validate_links(
            tuple(("input", item) for _member, item in member_rows),
            None,
            report_index,
            allowed_tiers={"derived"},
            require_report_membership=False,
        )
        frame_start = min((item.frame_start for item in member_dtos), default=0)
        frame_end = max((item.frame_end for item in member_dtos), default=0)
        source_key = (
            f"longitudinal:{run.run_id}:{run.cache_key}:{row.result_name}:{row.result_kind}:{frame_start}:{frame_end}"
        )
        claim = self._source_claim(
            document=document,
            claim_id=f"longitudinal:{row.result_name}:{row.public_id}",
            evidence=evidence,
        )
        raw = None if row.quality == "unavailable" else dict(public_statistics)
        anchor_replay = session.scalar(select(Replay).where(Replay.public_id == anchor.get("replay_public_id")))
        anchor_player = session.scalar(
            select(ReplayPlayer).where(ReplayPlayer.public_id == anchor.get("replay_player_public_id"))
        )
        if (
            evidence.source_key != source_key
            or evidence.public_id != str(uuid5(NAMESPACE_URL, source_key))
            or row.public_id
            != str(uuid5(NAMESPACE_URL, f"{run.run_id}:{run.cache_key}:{row.result_kind}:{row.result_name}"))
            or anchor.get("role") != "schema_required_corpus_anchor"
            or anchor_replay is None
            or anchor_player is None
            or anchor_replay.public_id != document.replay_public_id
            or anchor_replay.sha256 != anchor.get("replay_sha256")
            or anchor_player.public_id != document.replay_player_public_id
            or anchor_player.replay_id != anchor_replay.id
            or anchor_player.player_id != run.player_id
            or evidence.replay_id != anchor_replay.id
            or claim.section != "longitudinal"
            or claim.label != row.result_name
            or claim.raw_value != _safe_value(raw)
            or claim.availability != row.quality
            or claim.unavailable_reason != row.quality_reason
            or claim.scope != _safe_value({"scope_type": "player_corpus"})
            or claim.frame_window is not None
            or claim.details
            != _safe_value(
                {
                    "result_kind": row.result_kind,
                    "sample_count": row.sample_count,
                    "missing_count": row.missing_count,
                    "analyzer": {"name": run.analyzer_name, "version": run.analyzer_version},
                }
            )
        ):
            raise ReportGraphContractError("longitudinal source identity or immutable report claim drifted")
        return DerivedLongitudinalEvidenceDTO(
            "longitudinal_result",
            row.public_id,
            run.run_id,
            run.analyzer_name,
            run.analyzer_version,
            row.result_name,
            row.result_kind,
            row.sample_count,
            row.missing_count,
            row.quality,
            row.quality_reason,
            _safe_value(raw),
            tuple(EvidenceLinkDTO(item.public_id, "derived", "input") for _member, item in member_rows),
        )

    def _inferred_source(
        self,
        session: Session,
        evidence: EvidenceItem,
        document: ReportDocument,
        selection: _SubjectSelection,
        report_index: Mapping[str, ReportEvidenceTier],
    ) -> InferredAssessmentEvidenceDTO:
        rows = tuple(
            session.scalars(select(StrategyAssessment).where(StrategyAssessment.evidence_item_id == evidence.id))
        )
        if len(rows) != 1 or selection.analysis_run_id is None:
            raise ReportGraphContractError("inferred assessment evidence is ambiguous")
        row = rows[0]
        run = session.get(AnalysisRun, row.analysis_run_id) if row.analysis_run_id is not None else None
        player_id = self._document_player_id(session, document)
        details = row.details_json if isinstance(row.details_json, Mapping) else {}
        claim_id = details.get("claim_id")
        if (
            run is None
            or run.run_id != selection.analysis_run_id
            or run.replay_id != evidence.replay_id
            or run.replay_player_id != player_id
            or run.status != "succeeded"
            or row.replay_id != evidence.replay_id
            or row.replay_player_id != player_id
            or row.method != "llm"
            or row.model_version != run.model_digest
            or type(claim_id) is not str
            or not claim_id
            or evidence.tier != "inferred"
            or evidence.schema_version != 1
            or evidence.parser_run_id is not None
            or evidence.telemetry_run_id is not None
            or run.prompt_version != PROMPT_VERSION
            or run.prompt_digest != PROMPT_SHA256
            or run.response_schema_version != RESPONSE_SCHEMA_VERSION
            or run.response_schema_digest != RESPONSE_SCHEMA_SHA256
            or run.validated_response_json is None
        ):
            raise ReportGraphContractError("inferred assessment source, run, or version is invalid")
        citations = self._assessment_links(
            session,
            row,
            evidence.replay_id,
            report_index,
            allowed_tiers={"observed", "derived"},
        )
        if any(item.role != "supporting" for item in citations):
            raise ReportGraphContractError("inferred assessment citations must be supporting")
        selected_ids = tuple(sorted(public_id for public_id, tier in report_index.items() if tier != "inferred"))
        bundle = _SelectedEvidenceBundle(tuple(_SelectedEvidenceClaim((public_id,)) for public_id in selected_ids))
        try:
            validated = validate_response(
                cast(Mapping[str, object], run.validated_response_json),
                cast(EvidenceBundle, bundle),
            )
        except ResponseValidationError as exc:
            raise ReportGraphContractError("stored inferred citations fail domain validation") from exc
        claims = cast(list[dict[str, object]], validated.document.as_plain()["strategy_assessments"])
        matches = [item for item in claims if item["claim_id"] == claim_id]
        if len(matches) != 1:
            raise ReportGraphContractError("inferred assessment claim is cross-run or unavailable")
        claim = matches[0]
        window = cast(dict[str, int], claim["window"])
        citation_ids = tuple(item.public_id for item in citations)
        source_key = f"analysis-run:{run.run_id}:{claim_id}"
        report_claim = self._source_claim(
            document,
            f"strategy:{row.strategy_label}:{row.public_id}",
            evidence,
        )
        values_by_evidence: dict[str, ReportValue] = {}
        for value in (*document.observed, *document.derived):
            for reference in value.evidence:
                current = values_by_evidence.get(reference.public_id)
                if (
                    current is None
                    or {"available": 2, "partial": 1, "unavailable": 0}[value.availability]
                    < {"available": 2, "partial": 1, "unavailable": 0}[current.availability]
                ):
                    values_by_evidence[reference.public_id] = value
        try:
            cited_values = tuple(values_by_evidence[public_id] for public_id in citation_ids)
        except KeyError as exc:
            raise ReportGraphContractError("inferred citation is not represented by its report value") from exc
        if not cited_values:
            raise ReportGraphContractError("inferred assessment must cite report-selected evidence")
        minimum = min(
            cited_values,
            key=lambda value: {"available": 2, "partial": 1, "unavailable": 0}[value.availability],
        )
        reasons = tuple(
            sorted({value.unavailable_reason for value in cited_values if value.unavailable_reason is not None})
        )
        expected_details = {
            "assessment": claim["assessment"],
            "claim_id": claim_id,
            "cited_quality_reasons": list(reasons),
            "minimum_cited_quality": {
                "available": "complete",
                "partial": "partial",
                "unavailable": "unavailable",
            }[minimum.availability],
            "schema_version": "llm-strategy-assessment-v1",
        }
        raw = None
        if row.quality != "unavailable":
            raw = {
                "strategy_label": row.strategy_label,
                "phase": row.phase,
                "confidence": row.confidence,
            }
        unavailable_reason = (
            None if row.quality == "available" else (reasons[0] if reasons else "cited_evidence_unavailable")
        )
        if (
            evidence.source_key != source_key
            or evidence.public_id != str(uuid5(NAMESPACE_URL, f"evidence:{source_key}"))
            or row.public_id != str(uuid5(NAMESPACE_URL, f"strategy-assessment:{source_key}"))
            or row.strategy_label != claim["strategy_label"]
            or row.phase != claim["phase"]
            or row.frame_start != window["frame_start"]
            or row.frame_end != window["frame_end"]
            or row.confidence != claim["confidence"]
            or row.quality != minimum.availability
            or citation_ids != tuple(sorted(cast(list[str], claim["evidence_ids"])))
            or details != expected_details
            or report_claim.section != "strategy"
            or report_claim.label != row.strategy_label
            or report_claim.raw_value != _safe_value(raw)
            or report_claim.availability != row.quality
            or report_claim.unavailable_reason != unavailable_reason
            or report_claim.scope
            != _safe_value({"scope_type": "player" if row.replay_player_id is not None else "replay"})
            or report_claim.frame_window != (row.frame_start, row.frame_end)
            or report_claim.details != _safe_value(expected_details)
        ):
            raise ReportGraphContractError("inferred assessment graph or citation identity is invalid")
        return InferredAssessmentEvidenceDTO(
            "inferred_assessment",
            row.public_id,
            run.run_id,
            claim_id,
            row.strategy_label,
            row.phase,
            row.frame_start,
            row.frame_end,
            cast(float, row.confidence),
            run.provider,
            run.model_name,
            run.model_digest,
            run.prompt_version,
            run.response_schema_version,
            _safe_value(claim["assessment"]),
            citations,
        )

    def _assessment_links(
        self,
        session: Session,
        assessment: StrategyAssessment,
        replay_id: int,
        report_index: Mapping[str, ReportEvidenceTier],
        *,
        allowed_tiers: set[str],
    ) -> tuple[EvidenceLinkDTO, ...]:
        rows = tuple(
            (role, item)
            for role, item in session.execute(
                select(AssessmentEvidence.role, EvidenceItem)
                .join(EvidenceItem, EvidenceItem.id == AssessmentEvidence.evidence_item_id)
                .where(AssessmentEvidence.assessment_id == assessment.id)
                .order_by(AssessmentEvidence.role, EvidenceItem.public_id)
            )
        )
        self._validate_links(rows, replay_id, report_index, allowed_tiers=allowed_tiers)
        return tuple(
            EvidenceLinkDTO(
                item.public_id,
                cast(ReportEvidenceTier, item.tier),
                cast(EvidenceRole, role),
            )
            for role, item in rows
        )

    @staticmethod
    def _validate_links(
        rows: tuple[tuple[str, EvidenceItem], ...],
        replay_id: int | None,
        report_index: Mapping[str, ReportEvidenceTier],
        *,
        allowed_tiers: set[str],
        require_report_membership: bool = True,
    ) -> None:
        identities: dict[str, str] = {}
        for role, item in rows:
            prior = identities.setdefault(item.public_id, role)
            if prior != role:
                raise ReportGraphContractError("one citation cannot have conflicting evidence roles")
            if replay_id is not None and item.replay_id != replay_id:
                raise ReportGraphContractError("cross-replay citation is forbidden")
            if item.tier not in allowed_tiers:
                raise ReportGraphContractError("citation tier is not authorized for this source")
            if require_report_membership and report_index.get(item.public_id) != item.tier:
                raise ReportGraphContractError("citation is not selected by the exact report")

    @staticmethod
    def _document_player_id(session: Session, document: ReportDocument) -> int | None:
        if document.replay_player_public_id is None:
            return None
        player = session.scalar(select(ReplayPlayer).where(ReplayPlayer.public_id == document.replay_player_public_id))
        if player is None:
            raise ReportGraphContractError("report player is unavailable")
        return player.id
