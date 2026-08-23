"""Production projection from immutable Analytics reports to public Web DTOs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping

from generals_replay_analyzer.report.model import ReportAvailability, ReportQualityIssue, ReportValue, thaw_report_value
from generals_replay_analyzer.report.query import (
    EvidenceQuery,
    FixedReportQuery,
    LatestReportQuery,
    ReportGraphAmbiguousError,
    ReportGraphContractError,
    ReportGraphNotFoundError,
    ReportQueryService,
    TimelineChartQuery,
)
from generals_replay_analyzer.report.read_model import (
    DerivedAssessmentEvidenceDTO as AnalyticsDerivedAssessmentEvidenceDTO,
)
from generals_replay_analyzer.report.read_model import (
    DerivedFeatureEvidenceDTO as AnalyticsDerivedFeatureEvidenceDTO,
)
from generals_replay_analyzer.report.read_model import (
    DerivedLongitudinalEvidenceDTO as AnalyticsDerivedLongitudinalEvidenceDTO,
)
from generals_replay_analyzer.report.read_model import (
    EvidenceDetailDTO as AnalyticsEvidenceDetailDTO,
)
from generals_replay_analyzer.report.read_model import (
    EvidenceLinkDTO as AnalyticsEvidenceLinkDTO,
)
from generals_replay_analyzer.report.read_model import (
    InferredAssessmentEvidenceDTO as AnalyticsInferredAssessmentEvidenceDTO,
)
from generals_replay_analyzer.report.read_model import (
    ObservedCommandEvidenceDTO as AnalyticsObservedCommandEvidenceDTO,
)
from generals_replay_analyzer.report.read_model import (
    ObservedTelemetryEvidenceDTO as AnalyticsObservedTelemetryEvidenceDTO,
)
from generals_replay_analyzer.report.read_model import (
    TimelineChartDTO as AnalyticsTimelineChartDTO,
)
from generals_replay_analyzer.web.errors import PublicProblem
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    DerivedAssessmentEvidenceDTO,
    DerivedFeatureEvidenceDTO,
    DerivedLongitudinalEvidenceDTO,
    EvidenceDetailDTO,
    EvidenceLinkDTO,
    EvidenceQueryDTO,
    EvidenceSourceDTO,
    FixedReportQueryDTO,
    InferredAssessmentEvidenceDTO,
    LatestReportQueryDTO,
    ObservedCommandEvidenceDTO,
    ObservedTelemetryEvidenceDTO,
    OllamaReportStatusDTO,
    QualityIssueDTO,
    ReplayPlayerDisplayDTO,
    ReplayReportDTO,
    ReportClaimDTO,
    ReportEvidenceReferenceDTO,
    ReportLifecycleDTO,
    ReportResolutionDTO,
    ReportSectionDTO,
    ReportSectionKey,
    ReportVersionDTO,
    TerminalQualityDTO,
    TimelineChartDTO,
    TimelineChartQueryDTO,
    TimelineFamilyOptionDTO,
    TimelineIntervalDTO,
    TimelinePlayerOptionDTO,
    TimelinePointDTO,
    TimelineSeriesDTO,
)

_SECTION_ORDER: tuple[ReportSectionKey, ...] = (
    "overview",
    "players_results",
    "opening_build_order",
    "economy",
    "production_composition",
    "combat_engagements",
    "activity",
    "strategy_phases",
    "spatial_analysis",
    "longitudinal_context",
    "llm_interpretation",
)
_SECTION_TITLES: dict[ReportSectionKey, str] = {
    "overview": "Overview",
    "players_results": "Players and results",
    "opening_build_order": "Opening build order",
    "economy": "Economy",
    "production_composition": "Production and composition",
    "combat_engagements": "Combat and engagements",
    "activity": "Activity",
    "strategy_phases": "Strategy phases",
    "spatial_analysis": "Spatial analysis",
    "longitudinal_context": "Longitudinal context",
    "llm_interpretation": "Ollama interpretation",
}


def _availability(
    state: ReportAvailability,
    reason: str | None,
    evidence: Iterable[str] = (),
) -> AvailabilityDTO:
    return AvailabilityDTO(
        state=state,
        reason_codes=() if reason is None else (reason,),
        evidence_references=tuple(sorted(set(evidence))),
    )


def _display_value(value: object) -> str:
    if type(value) is bool:
        rendered = "Yes" if value else "No"
    elif type(value) is int:
        rendered = str(value)
    elif type(value) is float:
        rendered = f"{value:.3f}".rstrip("0").rstrip(".")
    elif type(value) is str:
        rendered = value
    else:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(rendered) <= 2048:
        return rendered
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    if type(value) is str:
        return f"Oversize string (characters={len(value)},utf8_bytes={len(value.encode('utf-8'))},sha256={digest})"
    if isinstance(value, Mapping):
        return f"Oversize mapping (entries={len(value)},canonical_utf8_bytes={len(canonical)},sha256={digest})"
    if isinstance(value, (list, tuple)):
        return f"Oversize list (items={len(value)},canonical_utf8_bytes={len(canonical)},sha256={digest})"
    return f"Oversize value (canonical_utf8_bytes={len(canonical)},sha256={digest})"


def _report_section(value: ReportValue, tier: str) -> ReportSectionKey:
    if tier == "inferred":
        return "llm_interpretation"
    if value.section == "availability":
        return "overview"
    if value.section == "strategy":
        return "strategy_phases"
    if value.section == "longitudinal":
        return "longitudinal_context"
    normalized = f"{value.section} {value.label}".casefold()
    if any(token in normalized for token in ("position", "movement", "spatial", "route", "map control", "density")):
        return "spatial_analysis"
    if any(token in normalized for token in ("construct", "build", "dozer", "opening")):
        return "opening_build_order"
    if any(token in normalized for token in ("cash", "economy", "income", "resource", "supply")):
        return "economy"
    if any(token in normalized for token in ("production", "upgrade", "composition", "unit")):
        return "production_composition"
    if any(token in normalized for token in ("damage", "combat", "engagement", "attack", "kill")):
        return "combat_engagements"
    return "activity"


def _claim(value: ReportValue, tier: str) -> ReportClaimDTO:
    raw = None if value.raw_value is None else thaw_report_value(value.raw_value)
    return ReportClaimDTO(
        claim_id=value.claim_id,
        section=_report_section(value, tier),
        label=value.label,
        raw_value=raw,
        display_value=None if raw is None else _display_value(raw),
        unit=value.unit,
        availability=value.availability,
        unavailable_reason=value.unavailable_reason,
        scope=thaw_report_value(value.scope),
        frame_window=value.frame_window,
        confidence=None,
        evidence=tuple(ReportEvidenceReferenceDTO(public_id=item.public_id, tier=item.tier) for item in value.evidence),
        details=thaw_report_value(value.details),
    )


def _section_availability(claims: tuple[ReportClaimDTO, ...]) -> AvailabilityDTO:
    evidence = tuple(item.public_id for claim in claims for item in claim.evidence)
    if not claims:
        return _availability("unavailable", "section_not_available")
    if all(claim.availability == "unavailable" for claim in claims):
        reasons = tuple(sorted({claim.unavailable_reason for claim in claims if claim.unavailable_reason is not None}))
        return AvailabilityDTO(
            state="unavailable",
            reason_codes=reasons or ("section_not_available",),
            evidence_references=tuple(sorted(set(evidence))),
        )
    if all(claim.availability == "available" for claim in claims):
        return _availability("available", None, evidence)
    return _availability("partial", "section_evidence_partial", evidence)


def _quality_issue(value: ReportQualityIssue) -> QualityIssueDTO:
    details = thaw_report_value(value.details)
    raw_boundary = details.get("crc_mismatch_frame") if isinstance(details, dict) else None
    frame_end = raw_boundary if type(raw_boundary) is int else None
    message = (
        f"CRC mismatch at frame {frame_end}"
        if value.issue_code == "crc_mismatch" and frame_end is not None
        else value.issue_code.replace("_", " ")
    )
    return QualityIssueDTO(code=value.issue_code, message=message, frame_end=frame_end)


def _web_link(value: AnalyticsEvidenceLinkDTO) -> EvidenceLinkDTO:
    return EvidenceLinkDTO(public_id=value.public_id, tier=value.tier, role=value.role)


# TheSuperHackers @feature Leex 23/08/2026 Project immutable report graphs into path-free Web contracts without reinterpreting evidence. (#TBD)
class AnalyticsReportAdapter:
    """Read-only production adapter for fixed reports, charts, and evidence."""

    def __init__(self, service: ReportQueryService) -> None:
        if type(service) is not ReportQueryService:
            raise TypeError("service must be a ReportQueryService")
        self._service = service

    def resolve_latest(self, query: LatestReportQueryDTO) -> ReportResolutionDTO:
        try:
            resolved = self._service.resolve_latest(
                LatestReportQuery(query.replay_public_id, query.replay_player_public_id)
            )
        except ReportGraphNotFoundError:
            return ReportResolutionDTO(state="not_generated", reason_code="report_not_generated")
        except (ReportGraphAmbiguousError, ReportGraphContractError) as error:
            raise PublicProblem(
                status=503,
                code="report_resolution_unavailable",
                detail="Replay report resolution is temporarily unavailable",
            ) from error
        fixed = FixedReportQueryDTO(
            replay_public_id=resolved.replay_public_id,
            report_public_id=resolved.report_public_id,
        )
        return ReportResolutionDTO(
            state="available",
            fixed_report=fixed,
            version=ReportVersionDTO(
                report_public_id=resolved.report_public_id,
                report_version=resolved.report_version,
                replay_player_public_id=resolved.replay_player_public_id,
            ),
        )

    def get_report(self, query: FixedReportQueryDTO) -> ReplayReportDTO:
        try:
            graph = self._service.get_report(FixedReportQuery(query.replay_public_id, query.report_public_id))
        except ReportGraphNotFoundError as error:
            raise PublicProblem(status=404, code="report_not_found", detail="Replay report was not found") from error
        except (ReportGraphAmbiguousError, ReportGraphContractError) as error:
            raise PublicProblem(
                status=503,
                code="report_graph_unavailable",
                detail="Replay report is temporarily unavailable",
            ) from error
        selected = graph.selected
        document = selected.document
        claims_by_section: dict[ReportSectionKey, list[ReportClaimDTO]] = {key: [] for key in _SECTION_ORDER}
        for tier, values in (
            ("observed", (*document.evidence_availability, *document.observed)),
            ("derived", document.derived),
            ("inferred", document.inferred),
        ):
            for value in values:
                claim = _claim(value, tier)
                claims_by_section[claim.section].append(claim)
        sections = tuple(
            ReportSectionDTO(
                key=key,
                title=_SECTION_TITLES[key],
                availability=_section_availability(tuple(claims_by_section[key])),
                claims=tuple(sorted(claims_by_section[key], key=lambda item: item.claim_id)),
            )
            for key in _SECTION_ORDER
        )
        available_claims = tuple(claim for section in sections for claim in section.claims)
        overall = _section_availability(available_claims)
        report_ids_by_player = {
            item.document.replay_player_public_id: item.document.report_public_id for item in graph.player_reports
        }
        players = tuple(
            ReplayPlayerDisplayDTO(
                replay_player_public_id=item.public_id,
                report_public_id=report_ids_by_player.get(item.public_id),
                display_name=item.display_name,
                slot=item.slot,
                faction=item.faction,
                result=item.result,
            )
            for item in graph.identity.players
        )
        selected_player = next(
            (item for item in graph.identity.players if item.public_id == document.replay_player_public_id),
            None,
        )
        quality_issues = tuple(_quality_issue(item) for item in document.quality_issues)
        ollama = document.ollama
        return ReplayReportDTO(
            schema_version="web-replay-report-v1",
            generated_at=selected.created_at_utc,
            fixed_report=query,
            version=ReportVersionDTO(
                report_public_id=document.report_public_id,
                report_version=document.report_version,
                replay_player_public_id=document.replay_player_public_id,
            ),
            availability=overall,
            replay_label=graph.identity.label,
            replay_sha256=document.replay_sha256,
            players=players,
            result=None if selected_player is None else selected_player.result,
            map_name=graph.identity.map_name,
            patch=graph.identity.patch,
            duration_frames=graph.identity.duration_frames,
            source_mode="deterministic_with_ollama" if ollama.requested else "deterministic_only",
            lifecycle=ReportLifecycleDTO(
                lifecycle_state=document.lifecycle.lifecycle_state,
                parser_completion_status=document.lifecycle.parser_completion_status,
                telemetry_status=document.lifecycle.telemetry_status,
                telemetry_runner_status=document.lifecycle.telemetry_runner_status,
            ),
            terminal_quality=TerminalQualityDTO(
                lifecycle=document.lifecycle.lifecycle_state,
                issues=quality_issues,
                engine_run_status=document.lifecycle.telemetry_runner_status,
                strategy_analysis_scope="replay" if document.replay_player_public_id is None else "player",
            ),
            sections=sections,
            ollama=OllamaReportStatusDTO(
                requested=ollama.requested,
                status=ollama.status,
                analysis_run_id=ollama.analysis_run_id,
                provider=ollama.provider,
                model_name=ollama.model_name,
                model_digest=ollama.model_digest,
                prompt_version=ollama.prompt_version,
                response_schema_version=ollama.response_schema_version,
                diagnostic_codes=ollama.diagnostic_codes,
                validated_prose=None if ollama.validated_prose is None else thaw_report_value(ollama.validated_prose),
            ),
            warnings=document.warnings,
        )

    def timeline_chart(self, query: TimelineChartQueryDTO) -> TimelineChartDTO:
        try:
            chart = self._service.timeline_chart(
                TimelineChartQuery(query.replay_public_id, query.report_public_id, query.players, query.families)
            )
        except ReportGraphNotFoundError as error:
            raise PublicProblem(status=404, code="report_not_found", detail="Replay report was not found") from error
        except (ReportGraphAmbiguousError, ReportGraphContractError) as error:
            raise PublicProblem(
                status=503,
                code="report_timeline_unavailable",
                detail="Replay timeline is temporarily unavailable",
            ) from error
        return self._timeline(chart, query)

    @staticmethod
    # TheSuperHackers @fix Leex 23/08/2026 Keep expanded timeline defaults separate from the immutable public request identity. (#TBD)
    def _timeline(chart: AnalyticsTimelineChartDTO, query: TimelineChartQueryDTO) -> TimelineChartDTO:
        series = tuple(
            TimelineSeriesDTO(
                series_id=item.series_id,
                label=item.label,
                kind=item.kind,
                player_public_id=item.player_public_id,
                event_family=item.family,
                unit=item.unit,
                availability=_availability(
                    item.availability,
                    item.unavailable_reason,
                    (
                        *(reference.public_id for point in item.points for reference in point.evidence),
                        *(reference.public_id for interval in item.intervals for reference in interval.evidence),
                    ),
                ),
                points=tuple(
                    TimelinePointDTO(
                        frame=point.frame,
                        value=point.value,
                        label=point.label or item.label,
                        evidence=tuple(
                            ReportEvidenceReferenceDTO(public_id=value.public_id, tier=value.tier)
                            for value in point.evidence
                        ),
                    )
                    for point in item.points
                ),
                intervals=tuple(
                    TimelineIntervalDTO(
                        frame_start=interval.frame_start,
                        frame_end=interval.frame_end,
                        label=interval.label,
                        evidence=tuple(
                            ReportEvidenceReferenceDTO(public_id=value.public_id, tier=value.tier)
                            for value in interval.evidence
                        ),
                    )
                    for interval in item.intervals
                ),
            )
            for item in chart.series
        )
        return TimelineChartDTO(
            schema_version="web-report-timeline-v1",
            query=query,
            availability=_availability(chart.availability, chart.unavailable_reason),
            timebase_fps=chart.frames_per_second,
            available_players=tuple(
                TimelinePlayerOptionDTO(public_id=item.public_id, label=item.label) for item in chart.available_players
            ),
            available_families=tuple(
                TimelineFamilyOptionDTO(value=item.family, label=item.label) for item in chart.available_families
            ),
            series=series,
        )

    def get_evidence(self, query: EvidenceQueryDTO) -> EvidenceDetailDTO:
        try:
            detail = self._service.get_evidence(
                EvidenceQuery(query.report_public_id, query.evidence_public_id, query.expected_tier)
            )
        except ReportGraphNotFoundError as error:
            raise PublicProblem(
                status=404, code="evidence_not_found", detail="Report evidence was not found"
            ) from error
        except (ReportGraphAmbiguousError, ReportGraphContractError) as error:
            raise PublicProblem(
                status=503,
                code="evidence_graph_unavailable",
                detail="Report evidence is temporarily unavailable",
            ) from error
        return self._evidence(query, detail)

    @staticmethod
    def _evidence(query: EvidenceQueryDTO, detail: AnalyticsEvidenceDetailDTO) -> EvidenceDetailDTO:
        source = detail.source
        mapped: EvidenceSourceDTO
        if type(source) is AnalyticsObservedCommandEvidenceDTO:
            mapped = ObservedCommandEvidenceDTO(
                kind=source.kind,
                parser_run_id=source.parser_run_id,
                parser_version=source.parser_version,
                parser_schema_version=source.parser_schema_version,
                start_offset=source.start_offset,
                end_offset=source.end_offset,
                frame=source.frame,
                message_type=source.message_type,
                message_name=source.message_name,
                replay_player_public_id=source.replay_player_public_id,
                arguments=thaw_report_value(source.arguments),
            )
        elif type(source) is AnalyticsObservedTelemetryEvidenceDTO:
            mapped = ObservedTelemetryEvidenceDTO(
                kind=source.kind,
                telemetry_run_id=source.telemetry_run_id,
                engine_build=source.engine_build,
                telemetry_schema_version=source.telemetry_schema_version,
                sequence=source.sequence,
                frame=source.frame,
                event_type=source.event_type,
                payload=thaw_report_value(source.payload),
            )
        elif type(source) is AnalyticsDerivedFeatureEvidenceDTO:
            mapped = DerivedFeatureEvidenceDTO(
                kind=source.kind,
                feature_public_id=source.feature_public_id,
                feature_set_public_id=source.feature_set_public_id,
                feature_name=source.feature_name,
                extractor_name=source.extractor_name,
                extractor_version=source.extractor_version,
                raw_value=None if source.raw_value is None else thaw_report_value(source.raw_value),
                unit=source.unit,
                scope=thaw_report_value(source.scope),
                frame_start=source.frame_start,
                frame_end=source.frame_end,
                availability=source.availability,
                unavailable_reason=source.unavailable_reason,
                details=thaw_report_value(source.details),
                inputs=tuple(_web_link(item) for item in source.inputs),
            )
        elif type(source) is AnalyticsDerivedAssessmentEvidenceDTO:
            mapped = DerivedAssessmentEvidenceDTO(
                kind=source.kind,
                assessment_public_id=source.assessment_public_id,
                strategy_label=source.strategy_label,
                phase=source.phase,
                taxonomy_version=source.taxonomy_version,
                rule_version=source.rule_version,
                frame_start=source.frame_start,
                frame_end=source.frame_end,
                score=source.score,
                availability=source.availability,
                unavailable_reason=source.unavailable_reason,
                details=thaw_report_value(source.details),
                citations=tuple(_web_link(item) for item in source.citations),
            )
        elif type(source) is AnalyticsDerivedLongitudinalEvidenceDTO:
            mapped = DerivedLongitudinalEvidenceDTO(
                kind=source.kind,
                result_public_id=source.result_public_id,
                longitudinal_run_id=source.longitudinal_run_id,
                analyzer_name=source.analyzer_name,
                analyzer_version=source.analyzer_version,
                result_name=source.result_name,
                result_kind=source.result_kind,
                sample_count=source.sample_count,
                missing_count=source.missing_count,
                availability=source.availability,
                unavailable_reason=source.unavailable_reason,
                statistics=(None if source.availability == "unavailable" else thaw_report_value(source.statistics)),
                members=tuple(_web_link(item) for item in source.members),
            )
        elif type(source) is AnalyticsInferredAssessmentEvidenceDTO:
            mapped = InferredAssessmentEvidenceDTO(
                kind=source.kind,
                assessment_public_id=source.assessment_public_id,
                analysis_run_id=source.analysis_run_id,
                assessment_key=source.assessment_key,
                strategy_label=source.strategy_label,
                phase=source.phase,
                frame_start=source.frame_start,
                frame_end=source.frame_end,
                confidence=source.confidence,
                provider=source.provider,
                model_name=source.model_name,
                model_digest=source.model_digest,
                prompt_version=source.prompt_version,
                response_schema_version=source.response_schema_version,
                assessment=thaw_report_value(source.assessment),
                citations=tuple(_web_link(item) for item in source.citations),
            )
        else:  # pragma: no cover - the Analytics DTO closes the union.
            raise TypeError("unsupported Analytics evidence source")
        return EvidenceDetailDTO(
            schema_version="web-evidence-inspector-v1",
            query=query,
            replay_public_id=detail.replay_public_id,
            source_kind=detail.source_kind,
            source_schema_version=detail.source_schema_version,
            source=mapped,
        )
