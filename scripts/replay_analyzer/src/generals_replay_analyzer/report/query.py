"""Read-only selection of completed immutable report and evidence graphs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, replace
from datetime import UTC
from typing import Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select
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
    CombatEvent,
    EconomyEvent,
    Entity,
    EntitySample,
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
    ProductionEvent,
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
from generals_replay_analyzer.importing.jobs import StageFailure
from generals_replay_analyzer.importing.service import _dependency_identity, _dependency_output
from generals_replay_analyzer.importing.stages import (
    ANALYZE_LLM,
    ANALYZE_LLM_VERSION,
    ASSESS_STRATEGIES,
    ASSESS_STRATEGIES_VERSION,
    DERIVE_FEATURES,
    DERIVE_FEATURES_VERSION,
    IMPORT_OBSERVATIONS,
    IMPORT_OBSERVATIONS_VERSION,
    PARSE,
    PARSE_VERSION,
    RENDER_REPORT,
    RENDER_REPORT_VERSION,
    TELEMETRY,
    TELEMETRY_VERSION,
    content_key,
    input_digest,
)
from generals_replay_analyzer.importing.telemetry_import import (
    _attempt_from_dependency,
    _attempt_from_failed_dependency,
    _attempt_settings,
    _contains_pathlike_text,
    _failed_dependency_error,
    _succeeded_dependency_output,
    _validated_parser_dependency,
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
from generals_replay_analyzer.report.render_html import _render_html_validated
from generals_replay_analyzer.report.render_json import _render_json_validated
from generals_replay_analyzer.report.render_text import _render_text_validated
from generals_replay_analyzer.report.resources import (
    ReportResources,
    load_report_resources,
    validate_document,
)
from generals_replay_analyzer.storage import ContentAddressedStore, ContentStorageError

_ASSET_NAMESPACE = uuid5(NAMESPACE_URL, "replay-report-managed-asset-v1")
_REPORT_NAMESPACE = uuid5(NAMESPACE_URL, "replay-report-public-id-v1")
# TheSuperHackers @bugfix Leex 25/08/2026 Bound complete full-match chart series without truncating their evidence. (#TBD)
_PUBLIC_VALUE_NODE_LIMIT = 16_384
_EVIDENCE_SOURCE_KINDS: dict[str, tuple[str, ...]] = {
    "observed": ("parser_command", "telemetry_event"),
    "derived": ("feature", "strategy_rule", "longitudinal_corpus"),
    "inferred": ("llm",),
}
_SUPPORTED_ANALYSIS_STAGE_VERSIONS = {
    DERIVE_FEATURES: frozenset({"1", DERIVE_FEATURES_VERSION}),
    ASSESS_STRATEGIES: frozenset({"1", ASSESS_STRATEGIES_VERSION}),
}


# TheSuperHackers @fix Leex 25/08/2026 Keep immutable reports readable across closed assessment-stage upgrades. (#TBD)
def _supported_analysis_stage_version(stage: str, version: str) -> bool:
    return version in _SUPPORTED_ANALYSIS_STAGE_VERSIONS.get(stage, frozenset())


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
    parser_run_id: str
    telemetry_run_id: str | None


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


def _project_verified_player_identities(
    rows: tuple[ReplayPlayer, ...],
    initialization: Mapping[str, object],
    outcome: Mapping[str, object] | None,
    complete: Mapping[str, object] | None,
) -> dict[str, tuple[str | None, str | None]]:
    """Project engine player facts without mutating immutable parser observations."""

    by_slot = {row.slot_index: row for row in rows}
    if len(by_slot) != len(rows):
        raise ReportGraphContractError("report player slots are ambiguous")
    raw_slots = initialization.get("slots")
    if type(raw_slots) is not list:
        raise ReportGraphContractError("telemetry player initialization slots are invalid")
    by_player_index: dict[int, ReplayPlayer] = {}
    factions: dict[str, str | None] = {}
    resolved_slots: set[int] = set()
    for raw_slot in raw_slots:
        if not isinstance(raw_slot, Mapping) or raw_slot.get("resolution_status") != "resolved":
            continue
        slot_index = raw_slot.get("slot_index")
        player_index = raw_slot.get("player_index")
        if type(slot_index) is not int or type(player_index) is not int:
            raise ReportGraphContractError("telemetry player initialization identity is invalid")
        row = by_slot.get(slot_index)
        if row is None:
            continue
        if slot_index in resolved_slots or player_index in by_player_index:
            raise ReportGraphContractError("telemetry player initialization identity is ambiguous")
        resolved_slots.add(slot_index)
        by_player_index[player_index] = row
        faction = raw_slot.get("faction_template_name")
        factions[row.public_id] = faction if type(faction) is str and faction else None

    results: dict[str, str] = {}
    clean_terminal = bool(
        outcome is not None
        and complete is not None
        and outcome.get("status") == "decided"
        and outcome.get("source") == "victory_conditions"
        and outcome.get("terminal_reason") == "clean_completion"
        and outcome.get("crc_mismatch") is False
        and outcome.get("clean_shutdown") is True
        and complete.get("terminal_reason") == "clean_completion"
        and complete.get("crc_mismatch") is False
        and complete.get("replay_truncated") is False
        and complete.get("clean_shutdown") is True
    )
    if clean_terminal:
        assert outcome is not None
        winners = outcome.get("winner_player_indices")
        losers = outcome.get("loser_player_indices")
        if (
            type(winners) is not list
            or type(losers) is not list
            or any(type(value) is not int for value in (*winners, *losers))
            or set(winners) & set(losers)
        ):
            raise ReportGraphContractError("telemetry match outcome identity is invalid")
        for player_index in cast(list[int], winners):
            row = by_player_index.get(player_index)
            if row is not None:
                results[row.public_id] = "won"
        for player_index in cast(list[int], losers):
            row = by_player_index.get(player_index)
            if row is not None:
                results[row.public_id] = "lost"
    return {
        row.public_id: (factions.get(row.public_id), results.get(row.public_id))
        for row in rows
        if row.public_id in factions or row.public_id in results
    }


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
        if nodes > _PUBLIC_VALUE_NODE_LIMIT or depth > 12:
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
        report_graph_cache: MutableMapping[tuple[str, str], PublishedReportGraphDTO] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._store = store or ContentAddressedStore(settings.cache_directory / "reports")
        # TheSuperHackers @performance Leex 25/08/2026 Cache only fully validated immutable report graphs per service instance. (#TBD)
        self._report_graph_cache = report_graph_cache if report_graph_cache is not None else {}

    # TheSuperHackers @feature Leex 23/08/2026 Select only an exact completed report stage graph for read-only consumers. (#TBD)
    def get_report(self, query: FixedReportQuery) -> PublishedReportGraphDTO:
        if type(query) is not FixedReportQuery:
            raise TypeError("query must be a FixedReportQuery")
        cache_key = (query.replay_public_id, query.report_public_id)
        cached = self._report_graph_cache.get(cache_key)
        if cached is not None:
            return cached
        graph, _selection = self._read_graph(query)
        self._report_graph_cache[cache_key] = graph
        return graph

    # TheSuperHackers @performance Leex 25/08/2026 Invalidate report graph cache entries when immutable data is refreshed. (#TBD)
    def invalidate_report_cache(
        self,
        replay_public_id: str | None = None,
        report_public_id: str | None = None,
    ) -> None:
        """Drop one fixed report cache entry, or all entries when unqualified."""
        if replay_public_id is None and report_public_id is None:
            self._report_graph_cache.clear()
            return
        if replay_public_id is None or report_public_id is None:
            raise ValueError("replay and report public IDs must be supplied together")
        self._report_graph_cache.pop((replay_public_id, report_public_id), None)

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
        frames_per_second, seconds_display_policy_version = self._timeline_timebase(query.replay_public_id)
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
            for issue in document.quality_issues:
                details = thaw_report_value(issue.details)
                frame = details.get("crc_mismatch_frame") if isinstance(details, Mapping) else None
                if issue.issue_code != "crc_mismatch" or type(frame) is not int or frame < 0:
                    continue
                if "quality" not in selected_families:
                    continue
                label = f"CRC mismatch at frame {frame}"
                # TheSuperHackers @fix Leex 25/08/2026 Preserve exact terminal CRC evidence on replay-wide timelines without inventing gameplay claims. (#TBD)
                series.append(
                    TimelineSeriesDTO(
                        f"quality:{issue.public_id}",
                        "marker",
                        "quality",
                        None,
                        label,
                        None,
                        "partial",
                        "crc_mismatch",
                        (TimelinePointDTO(frame, None, label, ()),),
                        (),
                    )
                )
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
                elif value.label in ("production.science_purchase_timing", "production.special_power_timing"):
                    # TheSuperHackers @feature Leex 24/08/2026 Expand observed power timing rows into frame markers instead of one full-match band. (#TBD)
                    thawed_timing = thaw_report_value(value.raw_value) if value.raw_value is not None else ()
                    raw_timing = thawed_timing if isinstance(thawed_timing, (list, tuple)) else ()
                    timing_points = tuple(
                        TimelinePointDTO(
                            cast(int, item["frame"]),
                            None,
                            f"{value.label}: {cast(str, item['item_name'])}",
                            evidence,
                        )
                        for item in raw_timing
                        if isinstance(item, Mapping)
                        and type(item.get("frame")) is int
                        and type(item.get("item_name")) is str
                    )
                    kind = "marker"
                    points = timing_points
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
            frames_per_second,
            "logic-frame-axis-v1",
            seconds_display_policy_version,
            selected_player_ids,
            selected_families,
            available_players,
            tuple(TimelineFamilyOptionDTO(item, family_labels[item]) for item in available_family_values),
            ordered,
            availability,
            reason,
        )

    # TheSuperHackers @feature Leex 24/08/2026 Resolve timeline seconds only from persisted engine clock authority. (#TBD)
    def _timeline_timebase(self, replay_public_id: str) -> tuple[Literal[30, 60] | None, Literal["frame-div-authoritative-logic-fps-v2", "frame-div-30-historical-v1", "frame-only-authority-unavailable-v2"]]:
        """Resolve timeline seconds only from persisted replay/telemetry clock authority."""
        with self._session_factory() as session:
            replay = session.scalar(select(Replay).where(Replay.public_id == replay_public_id))
            if replay is None:
                raise ReportGraphNotFoundError("requested replay public ID was not found")
            header = replay.header_json if isinstance(replay.header_json, Mapping) else {}
            timebase = header.get("timebase")
            if isinstance(timebase, Mapping):
                fps = timebase.get("logic_frames_per_second")
                if timebase.get("source") == "engine_manifest" and fps in (30, 60):
                    return cast(Literal[30, 60], fps), "frame-div-authoritative-logic-fps-v2"
            telemetry_runs = tuple(session.scalars(select(TelemetryRun).where(TelemetryRun.replay_id == replay.id, TelemetryRun.status == "succeeded").order_by(TelemetryRun.run_id)))
        for telemetry in telemetry_runs:
            settings = telemetry.settings_json if isinstance(telemetry.settings_json, Mapping) else {}
            fps = settings.get("logic_frames_per_second")
            if settings.get("logic_timebase_source") == "engine_manifest" and fps in (30, 60):
                return cast(Literal[30, 60], fps), "frame-div-authoritative-logic-fps-v2"
        # TheSuperHackers @info Leex 24/08/2026 Retain the explicit legacy V1 30 Hz display contract only when no V2 engine-manifest clock exists. (#TBD)
        if any(item.schema_version < 2 for item in telemetry_runs):
            return 30, "frame-div-30-historical-v1"
        return None, "frame-only-authority-unavailable-v2"

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
            # TheSuperHackers @bugfix Leex 23/08/2026 Accept only fully validated equivalent report claimants. (#TBD)
            validated_claims = tuple(
                self._validate_render_graph(session, replay, job)
                for job in sorted(matching_jobs, key=lambda item: item.public_id)
            )
            validated = validated_claims[0]
            if any(candidate != validated for candidate in validated_claims[1:]):
                raise ReportGraphAmbiguousError("multiple render stage graphs claim the exact report")
            published: list[PublishedReportDTO] = []
            matched_requested = 0
            for entry in validated.entries:
                report = self._report_for_entry(session, replay, entry)
                item = self._load_report(session, replay, report, entry, resources)
                self._validate_evidence_index(
                    session,
                    replay,
                    item.document,
                    validated.parser_run_id,
                    validated.telemetry_run_id,
                )
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
                self._replay_identity(session, replay, validated.subjects, validated.telemetry_run_id),
                replay_wide[0],
                players,
            )
            return graph, validated.subjects

    @staticmethod
    def _replay_identity(
        session: Session,
        replay: Replay,
        subjects: tuple[_SubjectSelection, ...],
        telemetry_run_id: str | None,
    ) -> ReportReplayIdentityDTO:
        subject_ids = tuple(
            sorted(
                item.replay_player_public_id
                for item in subjects
                if item.replay_player_public_id is not None
            )
        )
        rows = tuple(
            session.scalars(
                select(ReplayPlayer)
                .where(
                    ReplayPlayer.replay_id == replay.id,
                    ReplayPlayer.public_id.in_(subject_ids),
                    ReplayPlayer.slot_kind.in_(("human", "ai")),
                )
                .order_by(ReplayPlayer.slot_index, ReplayPlayer.public_id)
            )
        )
        parser_ids = {row.parser_run_id for row in rows}
        parser = None if len(parser_ids) != 1 else session.get(ParserRun, next(iter(parser_ids)))
        if (
            len(rows) != len(subject_ids)
            or parser is None
            or parser.replay_id != replay.id
            or parser.status != "succeeded"
            or parser.completion_status != "complete"
        ):
            raise ReportGraphContractError("report identity is outside one occupied parser authority")
        verified: dict[str, tuple[str | None, str | None]] = {}
        if telemetry_run_id is not None:
            telemetry = session.scalar(
                select(TelemetryRun).where(
                    TelemetryRun.replay_id == replay.id,
                    TelemetryRun.run_id == telemetry_run_id,
                    TelemetryRun.status == "succeeded",
                )
            )
            if telemetry is None:
                raise ReportGraphContractError("report identity telemetry authority is unavailable")
            identity_events = tuple(
                session.scalars(
                    select(TelemetryEvent)
                    .where(
                        TelemetryEvent.telemetry_run_id == telemetry.id,
                        TelemetryEvent.event_type.in_(("players_initialized", "match_outcome", "complete")),
                    )
                    .order_by(TelemetryEvent.sequence)
                )
            )
            initialization = tuple(item for item in identity_events if item.event_type == "players_initialized")
            outcomes = tuple(item for item in identity_events if item.event_type == "match_outcome")
            completions = tuple(item for item in identity_events if item.event_type == "complete")
            if len(initialization) > 1 or len(outcomes) > 1 or len(completions) > 1:
                raise ReportGraphContractError("report identity telemetry evidence is ambiguous")
            if initialization:
                # TheSuperHackers @bugfix Leex 25/08/2026 Present verified engine factions and clean outcomes without rewriting immutable parser evidence. (#TBD)
                verified = _project_verified_player_identities(
                    rows,
                    cast(Mapping[str, object], initialization[0].payload_json),
                    None if not outcomes else cast(Mapping[str, object], outcomes[0].payload_json),
                    None if not completions else cast(Mapping[str, object], completions[0].payload_json),
                )
        players: list[ReportPlayerIdentityDTO] = []
        for row in rows:
            observed = row.observed_json if isinstance(row.observed_json, Mapping) else {}
            faction = observed.get("faction")
            result = observed.get("result")
            verified_faction, verified_result = verified.get(row.public_id, (None, None))
            players.append(
                ReportPlayerIdentityDTO(
                    row.public_id,
                    row.original_name or f"Player {row.slot_index + 1}",
                    row.slot_index + 1,
                    verified_faction if verified_faction is not None else faction if type(faction) is str else None,
                    verified_result if verified_result is not None else result if type(result) is str else None,
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
        if isinstance(job.input_json, Mapping) and set(job.input_json) == {
            "replay_public_id",
            "replay_sha256",
        }:
            return self._validate_legacy_render_graph(session, replay, job)
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
                if assess_job.stage != ASSESS_STRATEGIES or not _supported_analysis_stage_version(
                    ASSESS_STRATEGIES, assess_job.component_version
                ):
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
                if direct.stage != ASSESS_STRATEGIES or not _supported_analysis_stage_version(
                    ASSESS_STRATEGIES, direct.component_version
                ):
                    raise ReportGraphContractError("deterministic report graph has the wrong direct dependency")
                assess_result = self._exact_result(session, direct)
                if direct.output_json != assess_result.output_json:
                    raise ReportGraphContractError("assessment job and immutable stage result disagree")
                assess = decode_assessment_output(assess_result.output_json)
                assess_job = direct
                subjects = tuple(self._selection(item, None) for item in assess)
        except PipelineCodecError as exc:
            raise ReportGraphContractError("report dependency output is invalid") from exc
        telemetry_run_id = self._validate_job_identity_chain(
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
        return _ValidatedGraph(
            entries,
            subjects,
            cast(str, input_value["parser_run_id"]),
            telemetry_run_id,
        )

    def _validate_legacy_render_graph(self, session: Session, replay: Replay, job: Job) -> _ValidatedGraph:
        result = self._exact_result(session, job)
        if job.output_json != result.output_json:
            raise ReportGraphContractError("render job and immutable stage result disagree")
        replay_input = {
            "replay_public_id": replay.public_id,
            "replay_sha256": replay.sha256,
        }
        if job.input_json != replay_input:
            raise ReportGraphContractError("legacy render report input is not bound to the exact replay")
        assess = self._dependency(session, replay, job)
        if assess.stage != ASSESS_STRATEGIES or not _supported_analysis_stage_version(
            ASSESS_STRATEGIES, assess.component_version
        ):
            raise ReportGraphContractError("legacy report graph requires one assessment dependency")
        derive = self._legacy_dependency_before_parent_completion(session, replay, assess)
        if derive.stage != DERIVE_FEATURES or not _supported_analysis_stage_version(
            DERIVE_FEATURES, derive.component_version
        ):
            raise ReportGraphContractError("legacy assessment is not bound to one derive-features stage")
        observation = self._dependency(session, replay, derive)
        if observation.stage != IMPORT_OBSERVATIONS or observation.component_version != IMPORT_OBSERVATIONS_VERSION:
            raise ReportGraphContractError("legacy derive-features is not bound to one observation stage")
        for stage_job in (observation, derive, assess):
            stage_result = self._exact_result(session, stage_job)
            if stage_job.output_json != stage_result.output_json:
                raise ReportGraphContractError("legacy analysis job and immutable stage result disagree")
        if derive.input_json != replay_input or assess.input_json != replay_input:
            raise ReportGraphContractError("legacy analysis input is not bound to the exact replay")
        branch_recipe, telemetry_run_id = self._validate_observation_authority(
            session, replay, observation
        )
        identities = (
            (
                derive,
                DERIVE_FEATURES,
                derive.component_version,
                {"derive_features_version": derive.component_version, "observations": dict(branch_recipe)},
            ),
            (
                assess,
                ASSESS_STRATEGIES,
                assess.component_version,
                {"assess_strategies_version": assess.component_version, "observations": dict(branch_recipe)},
            ),
            (
                job,
                RENDER_REPORT,
                RENDER_REPORT_VERSION,
                {"render_report_version": RENDER_REPORT_VERSION, "observations": dict(branch_recipe)},
            ),
        )
        if any(
            stage_job.idempotency_key != content_key(stage, version, replay.sha256, identity)
            for stage_job, stage, version, identity in identities
        ):
            raise ReportGraphContractError("legacy analysis stage identity is invalid")
        try:
            assess_output = decode_assessment_output(assess.output_json)
        except PipelineCodecError as exc:
            raise ReportGraphContractError("legacy assessment output is invalid") from exc
        successful_llm = tuple(
            session.scalars(
                select(Job)
                .join(JobDependency, Job.id == JobDependency.job_id)
                .where(
                    JobDependency.depends_on_job_id == assess.id,
                    Job.stage == ANALYZE_LLM,
                    Job.component_version == ANALYZE_LLM_VERSION,
                    Job.status == "succeeded",
                )
                .order_by(Job.public_id)
            )
        )
        if len(successful_llm) > 1:
            raise ReportGraphContractError("legacy LLM analysis graph is ambiguous")
        if successful_llm:
            llm = successful_llm[0]
            if (
                llm.input_json != replay_input
                or llm.idempotency_key
                != content_key(
                    ANALYZE_LLM,
                    ANALYZE_LLM_VERSION,
                    replay.sha256,
                    {"analyze_llm_version": ANALYZE_LLM_VERSION, "observations": dict(branch_recipe)},
                )
            ):
                raise ReportGraphContractError("legacy LLM stage identity is invalid")
            llm_result = self._exact_result(session, llm)
            if llm.output_json != llm_result.output_json:
                raise ReportGraphContractError("legacy LLM job and immutable stage result disagree")
            try:
                llm_output = decode_llm_output(llm.output_json)
            except PipelineCodecError as exc:
                raise ReportGraphContractError("legacy LLM output is invalid") from exc
            by_player = {item.replay_player_public_id: item for item in assess_output}
            if set(by_player) != {item.replay_player_public_id for item in llm_output}:
                raise ReportGraphContractError("legacy LLM and assessment subjects disagree")
            subjects = tuple(
                self._selection(by_player[item.replay_player_public_id], item.analysis_run_id)
                for item in llm_output
            )
        else:
            subjects = tuple(self._selection(item, None) for item in assess_output)
        self._validate_subject_rows(session, replay, subjects)
        entries = self._report_entries(result.output_json)
        expected = tuple((item.replay_player_public_id, item.analysis_run_id) for item in subjects)
        actual = tuple((item.replay_player_public_id, item.analysis_run_id) for item in entries)
        if actual != expected:
            raise ReportGraphContractError("legacy render report entries disagree with the exact analysis graph")
        return _ValidatedGraph(
            entries,
            subjects,
            cast(str, cast(Mapping[str, object], observation.output_json)["parser_run_id"]),
            telemetry_run_id,
        )

    def _validate_observation_authority(
        self, session: Session, replay: Replay, observation: Job
    ) -> tuple[Mapping[str, object], str | None]:
        input_json = observation.input_json
        output_json = observation.output_json
        if not isinstance(input_json, Mapping) or not isinstance(output_json, Mapping):
            raise ReportGraphContractError("legacy observation identity is invalid")
        if set(output_json) != {
            "idempotency_key",
            "parser_run_id",
            "parser_command_count",
            "telemetry_run_id",
            "telemetry_event_count",
        }:
            raise ReportGraphContractError("legacy observation output does not use the production schema")
        parser_command_count = output_json.get("parser_command_count")
        telemetry_event_count = output_json.get("telemetry_event_count")
        if (
            output_json.get("idempotency_key") != observation.idempotency_key
            or type(parser_command_count) is not int
            or parser_command_count < 0
            or type(telemetry_event_count) is not int
            or telemetry_event_count < 0
        ):
            raise ReportGraphContractError("legacy observation output identity is invalid")
        branch_recipe = input_json.get("branch_recipe")
        selected_digest = input_json.get("selected_dependency_digest")
        parser_run_id = output_json.get("parser_run_id")
        observation_telemetry_run_id = output_json.get("telemetry_run_id")
        if "telemetry_run_id" not in output_json:
            raise ReportGraphContractError("legacy observation telemetry identity is missing")
        if observation_telemetry_run_id is not None:
            try:
                observation_telemetry_run_id = _uuid(
                    observation_telemetry_run_id,
                    "legacy telemetry_run_id",
                )
            except ValueError as exc:
                raise ReportGraphContractError("legacy telemetry run identity is invalid") from exc
        if (
            not isinstance(branch_recipe, Mapping)
            or set(branch_recipe) != {"import_observations_version", "import_mode", "parse", "telemetry"}
            or branch_recipe.get("import_observations_version") != IMPORT_OBSERVATIONS_VERSION
            or branch_recipe.get("import_mode") not in {"copy", "reference"}
        ):
            raise ReportGraphContractError("legacy observation branch recipe is invalid")
        import_mode = branch_recipe["import_mode"]
        parse_identity = branch_recipe.get("parse")
        telemetry_identity = branch_recipe.get("telemetry")
        if (
            not isinstance(parse_identity, Mapping)
            or set(parse_identity) != {"import_mode", "parse_version", "parser_version"}
            or parse_identity.get("import_mode") != import_mode
            or parse_identity.get("parse_version") != PARSE_VERSION
            or type(parse_identity.get("parser_version")) is not str
            or not parse_identity.get("parser_version")
            or (
                telemetry_identity is not None
                and (
                    not isinstance(telemetry_identity, Mapping)
                    or set(telemetry_identity) != {"acquirer_version", "import_mode", "telemetry_version"}
                    or telemetry_identity.get("import_mode") != import_mode
                    or telemetry_identity.get("telemetry_version") != TELEMETRY_VERSION
                    or type(telemetry_identity.get("acquirer_version")) is not str
                    or not telemetry_identity.get("acquirer_version")
                )
            )
        ):
            raise ReportGraphContractError("legacy observation branch recipe is invalid")
        provisional_key = content_key(
            IMPORT_OBSERVATIONS,
            IMPORT_OBSERVATIONS_VERSION,
            replay.sha256,
            dict(branch_recipe),
        )
        if (
            input_json.get("replay_public_id") != replay.public_id
            or input_json.get("replay_sha256") != replay.sha256
            or input_json.get("dependency_identity_bound") is not True
            or input_json.get("provisional_idempotency_key") != provisional_key
            or not _sha256(selected_digest)
        ):
            raise ReportGraphContractError("legacy observation identity is invalid")
        try:
            parser_run_id = _uuid(parser_run_id, "legacy parser_run_id")
        except ValueError as exc:
            raise ReportGraphContractError("legacy parser run identity is invalid") from exc
        parser = session.scalar(select(ParserRun).where(ParserRun.run_id == parser_run_id))
        if (
            parser is None
            or parser.replay_id != replay.id
            or parser.parser_version != parse_identity["parser_version"]
            or parser.input_sha256 != replay.sha256
            or parser.status != "succeeded"
            or parser.completion_status != "complete"
            or parser.completed_at is None
            or parser.result_sha256 is None
        ):
            raise ReportGraphContractError("legacy parser run is not authoritative")
        dependencies = list(
            session.scalars(
                select(Job)
                .join(JobDependency, Job.id == JobDependency.depends_on_job_id)
                .where(JobDependency.job_id == observation.id)
            )
        )
        dependencies.sort(key=lambda item: (0 if item.stage == PARSE else 1, item.component_version, item.public_id))
        expected_stages = {PARSE, TELEMETRY} if telemetry_identity is not None else {PARSE}
        if (
            {dependency.stage for dependency in dependencies} != expected_stages
            or len(dependencies) != len(expected_stages)
            or any(
                dependency.replay_id != replay.id
                or dependency.component_version
                != (PARSE_VERSION if dependency.stage == PARSE else TELEMETRY_VERSION)
                for dependency in dependencies
            )
        ):
            raise ReportGraphContractError("legacy observation dependencies do not match the selected branch")
        selected = {dependency.stage: dependency for dependency in dependencies}
        parse_job = selected[PARSE]
        expected_stage_input = {
            "replay_public_id": replay.public_id,
            "replay_sha256": replay.sha256,
            "import_mode": import_mode,
        }
        try:
            parse_dependency = _dependency_output(parse_job)
            parse_output = _succeeded_dependency_output(parse_dependency)
            parser_version = _validated_parser_dependency(parse_output, replay.sha256)
        except (TypeError, ValueError, StageFailure) as exc:
            raise ReportGraphContractError("legacy selected parser output is invalid") from exc
        warnings = parser.warnings_json
        if not isinstance(warnings, list) or any(
            not isinstance(item, Mapping) or not isinstance(item.get("code"), str)
            for item in warnings
        ):
            raise ReportGraphContractError("legacy parser run warnings are invalid")
        expected_warning_codes = tuple(sorted({cast(str, item["code"]) for item in warnings}))
        command_count = int(
            session.scalar(
                select(func.count()).select_from(ReplayCommand).where(ReplayCommand.parser_run_id == parser.id)
            )
            or 0
        )
        if parser_command_count != command_count:
            raise ReportGraphContractError(
                "legacy observation parser command count is not authoritative"
            )
        if (
            parse_job.status != "succeeded"
            or parse_job.output_json != self._exact_result(session, parse_job).output_json
            or parser_version != parser.parser_version
            or parse_output.get("completion_status") != parser.completion_status
            or parse_output.get("command_count") != command_count
            or parse_output.get("warning_codes") != expected_warning_codes
            or parse_output.get("command_stream_offset") != parser.command_stream_offset
            or parse_output.get("end_offset") != parser.end_offset
            or parse_job.idempotency_key
            != content_key(PARSE, PARSE_VERSION, replay.sha256, dict(parse_identity))
            or parse_job.input_json != expected_stage_input
        ):
            raise ReportGraphContractError("legacy selected parser does not match its branch recipe")
        telemetry_job = selected.get(TELEMETRY)
        if telemetry_identity is not None:
            assert telemetry_job is not None
            telemetry_parents = set(
                session.scalars(
                    select(JobDependency.depends_on_job_id).where(JobDependency.job_id == telemetry_job.id)
                )
            )
            if (
                telemetry_job.idempotency_key
                != content_key(TELEMETRY, TELEMETRY_VERSION, replay.sha256, dict(telemetry_identity))
                or telemetry_job.input_json != expected_stage_input
                or telemetry_parents != {parse_job.id}
            ):
                raise ReportGraphContractError("legacy selected telemetry does not match its parser branch")
            try:
                telemetry_dependency = _dependency_output(telemetry_job)
                attempt = None
                expected_telemetry_status = None
                if telemetry_dependency.status == "succeeded":
                    telemetry_output = _succeeded_dependency_output(telemetry_dependency)
                    if telemetry_job.output_json != self._exact_result(session, telemetry_job).output_json:
                        raise ReportGraphContractError(
                            "legacy telemetry job and immutable stage result disagree"
                        )
                    attempt = _attempt_from_dependency(telemetry_output)
                    expected_telemetry_status = "succeeded"
                else:
                    if telemetry_job.output_json is not None or session.scalar(
                        select(func.count()).select_from(JobStageResult).where(
                            JobStageResult.job_id == telemetry_job.id
                        )
                    ):
                        raise ReportGraphContractError(
                            "legacy failed telemetry claims a successful output"
                        )
                    failure_code, _failure_message, failure_details = _failed_dependency_error(
                        telemetry_dependency
                    )
                    if failure_code == "dependency_failed":
                        if output_json.get("telemetry_run_id") is not None:
                            raise ReportGraphContractError(
                                "legacy dependency-failed telemetry cannot claim a run"
                            )
                    else:
                        attempt = _attempt_from_failed_dependency(
                            telemetry_dependency,
                            failure_code,
                            failure_details,
                        )
                        expected_telemetry_status = "failed"
            except ReportGraphContractError:
                raise
            except (TypeError, ValueError, StageFailure) as exc:
                raise ReportGraphContractError("legacy selected telemetry output is invalid") from exc
            if attempt is not None:
                attempt = replace(attempt, parser_run_id=parser.run_id)
                telemetry = session.scalar(
                    select(TelemetryRun).where(TelemetryRun.run_id == attempt.run_id)
                )
                expected_telemetry_settings = _attempt_settings(
                    attempt,
                    observation.idempotency_key,
                )
                if telemetry is not None and expected_telemetry_status == "succeeded":
                    actual_settings = telemetry.settings_json if isinstance(telemetry.settings_json, dict) else {}
                    if actual_settings != expected_telemetry_settings:
                        authoritative_fps = actual_settings.get("logic_frames_per_second")
                        authoritative_source = actual_settings.get("logic_timebase_source")
                        valid_timebase = (
                            telemetry.schema_version == 1
                            and authoritative_fps == 30
                            and authoritative_source == "historical_v1_contract"
                        ) or (
                            telemetry.schema_version >= 2
                            and authoritative_fps in (30, 60)
                            and authoritative_source == "engine_manifest"
                        )
                        if not valid_timebase:
                            raise ReportGraphContractError("legacy telemetry logic timebase is not authoritative")
                        # TheSuperHackers @fix Leex 24/08/2026 Accept only the closed persisted replay-clock extension on legacy report branches. (#TBD)
                        expected_telemetry_settings.update(
                            {
                                "logic_frames_per_second": authoritative_fps,
                                "logic_timebase_source": authoritative_source,
                            }
                        )
                manifest_engine_build_is_authoritative = (
                    telemetry is not None
                    and expected_telemetry_status == "succeeded"
                    and attempt.engine_build is None
                    and type(telemetry.engine_build) is str
                    and bool(telemetry.engine_build)
                    and telemetry.engine_build.isprintable()
                    and "\x00" not in telemetry.engine_build
                    and not _contains_pathlike_text(telemetry.engine_build)
                )
                if (
                    telemetry is None
                    or telemetry.replay_id != replay.id
                    or telemetry.status != expected_telemetry_status
                    or telemetry.completed_at is None
                    or telemetry.runner_status != attempt.runner_status
                    or telemetry.strategy_analysis_scope != attempt.strategy_analysis_scope
                    or telemetry.process_exit_code != attempt.process_exit_code
                    # TheSuperHackers @bugfix Leex 25/08/2026 Accept a validated manifest build when acquisition could not predeclare it. (#TBD)
                    or (
                        not manifest_engine_build_is_authoritative
                        and telemetry.engine_build != (attempt.engine_build or "unavailable")
                    )
                    or telemetry.engine_executable_sha256 != attempt.engine_executable_sha256
                    or telemetry.diagnostics_json != [dict(item) for item in attempt.diagnostics]
                    or telemetry.settings_json != expected_telemetry_settings
                    or output_json.get("telemetry_run_id") != telemetry.run_id
                ):
                    raise ReportGraphContractError("legacy telemetry run is not authoritative")
                actual_telemetry_event_count = int(
                    session.scalar(
                        select(func.count()).select_from(TelemetryEvent).where(
                            TelemetryEvent.telemetry_run_id == telemetry.id
                        )
                    )
                    or 0
                )
                if expected_telemetry_status == "failed":
                    child_count = actual_telemetry_event_count + sum(
                        int(session.scalar(statement) or 0)
                        for statement in (
                            select(func.count()).select_from(Entity).where(Entity.telemetry_run_id == telemetry.id),
                            select(func.count()).select_from(EntitySample).where(
                                EntitySample.telemetry_run_id == telemetry.id
                            ),
                            select(func.count()).select_from(ProductionEvent).where(
                                ProductionEvent.telemetry_run_id == telemetry.id
                            ),
                            select(func.count()).select_from(EconomyEvent).where(
                                EconomyEvent.telemetry_run_id == telemetry.id
                            ),
                            select(func.count()).select_from(CombatEvent).where(
                                CombatEvent.telemetry_run_id == telemetry.id
                            ),
                            select(func.count()).select_from(EvidenceItem).where(
                                EvidenceItem.telemetry_run_id == telemetry.id
                            ),
                        )
                    )
                    if child_count:
                        raise ReportGraphContractError(
                            "legacy failed telemetry cannot claim materialized observations"
                        )
                if telemetry_event_count != actual_telemetry_event_count:
                    raise ReportGraphContractError(
                        "legacy observation telemetry event count is not authoritative"
                    )
            elif telemetry_event_count != 0:
                raise ReportGraphContractError(
                    "legacy dependency-failed telemetry cannot claim materialized events"
                )
        elif output_json.get("telemetry_run_id") is not None:
            raise ReportGraphContractError("legacy parser-only branch cannot claim telemetry")
        elif telemetry_event_count != 0:
            raise ReportGraphContractError("legacy parser-only branch cannot claim telemetry events")
        if output_json.get("parser_run_id") != parser.run_id:
            raise ReportGraphContractError("legacy observation output disagrees with parser authority")
        try:
            dependency_identities = [_dependency_identity(dependency) for dependency in dependencies]
        except (TypeError, ValueError, StageFailure) as exc:
            raise ReportGraphContractError("legacy observation dependency identity is invalid") from exc
        selected_identity = {
            "replay_sha256": replay.sha256,
            "branch_recipe": dict(branch_recipe),
            "dependencies": dependency_identities,
        }
        if (
            selected_digest != input_digest(selected_identity)
            or observation.idempotency_key
            != content_key(
                IMPORT_OBSERVATIONS,
                IMPORT_OBSERVATIONS_VERSION,
                replay.sha256,
                selected_identity,
            )
        ):
            raise ReportGraphContractError("legacy observation materialization identity is invalid")
        return branch_recipe, observation_telemetry_run_id

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

    @staticmethod
    def _legacy_dependency_before_parent_completion(session: Session, replay: Replay, job: Job) -> Job:
        # TheSuperHackers @bugfix Leex 26/08/2026 Preserve completed legacy reports when a newer dependency edge is appended after their assessment finished. (#TBD)
        if job.completed_at is None:
            raise ReportGraphContractError("legacy analysis stage has no completion boundary")
        dependencies = tuple(
            session.scalars(
                select(Job)
                .join(JobDependency, Job.id == JobDependency.depends_on_job_id)
                .where(
                    JobDependency.job_id == job.id,
                    JobDependency.created_at <= job.completed_at,
                    Job.replay_id == replay.id,
                    Job.status == "succeeded",
                    Job.created_at <= job.completed_at,
                    Job.completed_at.is_not(None),
                    Job.completed_at <= job.completed_at,
                )
            )
        )
        if len(dependencies) != 1:
            raise ReportGraphContractError("legacy analysis stage requires one exact historical direct dependency")
        dependency = dependencies[0]
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
    ) -> str | None:
        derive = self._dependency(session, replay, assess)
        if derive.stage != DERIVE_FEATURES or not _supported_analysis_stage_version(
            DERIVE_FEATURES, derive.component_version
        ):
            raise ReportGraphContractError("assessment is not bound to one derive-features stage")
        observation = self._dependency(session, replay, derive)
        if observation.stage != IMPORT_OBSERVATIONS or observation.component_version != IMPORT_OBSERVATIONS_VERSION:
            raise ReportGraphContractError("derive-features is not bound to one observation stage")
        observation_input = observation.input_json
        observation_output = observation.output_json
        if not isinstance(observation_input, Mapping) or not isinstance(observation_output, Mapping):
            raise ReportGraphContractError("report dependency digest disagrees with observation authority")
        _branch_recipe, telemetry_run_id = self._validate_observation_authority(
            session, replay, observation
        )
        if (
            observation_input.get("selected_dependency_digest") != report_input["selected_dependency_digest"]
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
            != content_key(DERIVE_FEATURES, derive.component_version, replay.sha256, derive_identity)
            or assess.input_json != {**base_input, "derive_input_digest": input_digest(assess_identity)}
            or not _supported_analysis_stage_version(ASSESS_STRATEGIES, assess.component_version)
            or assess.idempotency_key
            != content_key(ASSESS_STRATEGIES, assess.component_version, replay.sha256, assess_identity)
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
        return telemetry_run_id

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
        # TheSuperHackers @performance Leex 23/08/2026 Validate each immutable report once before deterministic trusted rendering. (#TBD)
        structured_bytes = _render_json_validated(document)
        structured = self._asset(structured_row, "report_structured_json", structured_bytes)
        html = _render_html_validated(document)
        text = _render_text_validated(document)
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
        if document_to_mapping(document) != value or _canonical_bytes(document_to_mapping(document)) != _canonical_bytes(
            value
        ):
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

    def _validate_evidence_index(
        self,
        session: Session,
        replay: Replay,
        document: ReportDocument,
        selected_parser_run_id: str,
        selected_telemetry_run_id: str | None,
    ) -> None:
        index = self._document_evidence_index(document)
        rows: tuple[EvidenceItem, ...] = ()
        if index:
            # TheSuperHackers @bugfix Leex 25/08/2026 Bind large report evidence sets once through SQLite JSON instead of exceeding SQL variables. (#TBD)
            requested = func.json_each(
                json.dumps(tuple(index), separators=(",", ":"), ensure_ascii=True)
            ).table_valued("value")
            rows = tuple(
                session.scalars(
                    select(EvidenceItem).join(
                        requested,
                        EvidenceItem.public_id == requested.c.value,
                    )
                )
            )
        by_public_id = {row.public_id: row for row in rows}
        if set(by_public_id) != set(index):
            raise ReportGraphContractError("report cites unavailable evidence")
        parser = session.scalar(
            select(ParserRun).where(
                ParserRun.replay_id == replay.id,
                ParserRun.run_id == selected_parser_run_id,
                ParserRun.status == "succeeded",
                ParserRun.completion_status == "complete",
            )
        )
        if parser is None:
            raise ReportGraphContractError("report evidence has no authoritative parser")
        telemetry = (
            None
            if selected_telemetry_run_id is None
            else session.scalar(
                select(TelemetryRun).where(TelemetryRun.run_id == selected_telemetry_run_id)
            )
        )
        for public_id, tier in index.items():
            row = by_public_id[public_id]
            if (
                row.replay_id != replay.id
                or row.tier != tier
                or row.source_kind not in _EVIDENCE_SOURCE_KINDS[tier]
                or row.schema_version < 1
                or (tier != "observed" and row.schema_version != 1)
                or (row.parser_run_id is not None and row.parser_run_id != parser.id)
                or (
                    row.telemetry_run_id is not None
                    and (
                        telemetry is None
                        or row.telemetry_run_id != telemetry.id
                        or telemetry.replay_id != replay.id
                        or telemetry.status != "succeeded"
                        or not isinstance(telemetry.settings_json, Mapping)
                        or telemetry.settings_json.get("parser_run_id") != parser.run_id
                    )
                )
            ):
                raise ReportGraphContractError(
                    "report evidence source, tier, version, parser, or telemetry authority is invalid"
                )

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
