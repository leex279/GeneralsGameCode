"""Fail-closed presentation values for fixed replay reports."""

from __future__ import annotations

import json
from typing import cast

from pydantic import BaseModel, ConfigDict

from generals_replay_analyzer.report.model import CanonicalValue, thaw_report_value
from generals_replay_analyzer.web.ports import (
    DerivedAssessmentEvidenceDTO,
    DerivedFeatureEvidenceDTO,
    DerivedLongitudinalEvidenceDTO,
    EvidenceDetailDTO,
    EvidenceLinkDTO,
    InferredAssessmentEvidenceDTO,
    ObservedCommandEvidenceDTO,
    ObservedTelemetryEvidenceDTO,
    ReplayPlayerDisplayDTO,
    ReplayReportDTO,
    ReportClaimDTO,
    ReportSectionDTO,
    TimelineChartDTO,
)


class ReplayReportViewModel(BaseModel):
    """One fixed report paired with its exact frame-based timeline snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report: ReplayReportDTO
    timeline: TimelineChartDTO
    jump_sections: tuple[ReportSectionDTO, ...]
    selected_player: ReplayPlayerDisplayDTO | None
    highlight_claims: tuple[ReportClaimDTO, ...]
    highlight_scope_label: str | None
    duration_label: str
    source_mode_label: str
    ollama_status_label: str


_HIGHLIGHT_PRIORITIES = (
    "cash_change_total",
    "supply_collected_total",
    "tracked_income_total",
    "completed_composition",
    "completed_order_count",
    "queued_order_count",
    "cancelled_order_count",
    "supported_order_action_count",
    "first_build",
)


def _highlight_rank(claim: ReportClaimDTO) -> tuple[int, str]:
    identity = f"{claim.claim_id} {claim.label}".casefold()
    for rank, token in enumerate(_HIGHLIGHT_PRIORITIES):
        if token in identity:
            return rank, claim.claim_id
    return len(_HIGHLIGHT_PRIORITIES), claim.claim_id


# TheSuperHackers @feature Leex 23/08/2026 Lead player reports with bounded evidence-backed match highlights. (#TBD)
def _highlight_claims(report: ReplayReportDTO) -> tuple[ReportClaimDTO, ...]:
    if report.version.replay_player_public_id is None:
        return ()
    candidates = (
        claim
        for section in report.sections
        for claim in section.claims
        if claim.availability != "unavailable"
        and claim.display_value is not None
        and len(claim.display_value) <= 160
        and not claim.display_value.startswith("Oversize ")
        and any(evidence.tier == "derived" for evidence in claim.evidence)
    )
    return tuple(sorted(candidates, key=_highlight_rank)[:8])


def _highlight_scope_label(report: ReplayReportDTO, highlights: tuple[ReportClaimDTO, ...]) -> str | None:
    frame_ends = tuple(claim.frame_window[1] for claim in highlights if claim.frame_window is not None)
    if report.duration_frames is None or not frame_ends:
        return None
    evidence_end = max(frame_ends)
    if evidence_end >= report.duration_frames:
        return None
    return (
        f"Current derived values cover frames 0-{evidence_end} of {report.duration_frames} "
        f"({evidence_end / 30:.1f}s of {report.duration_frames / 30 / 60:.1f}m). "
        "Treat them as early-match signals, not full-match conclusions."
    )


# TheSuperHackers @feature Leex 23/08/2026 Refuse cross-report chart data before rendering evidence links. (#TBD)
def replay_report_view(report: ReplayReportDTO, timeline: TimelineChartDTO) -> ReplayReportViewModel:
    if (
        timeline.query.replay_public_id != report.fixed_report.replay_public_id
        or timeline.query.report_public_id != report.fixed_report.report_public_id
    ):
        raise ValueError("timeline identity does not match the fixed report")
    duration_label = (
        "Duration unavailable"
        if report.duration_frames is None
        else f"{report.duration_frames // 30 // 60}:{report.duration_frames // 30 % 60:02d}"
    )
    jump_sections = tuple(
        section for section in report.sections if section.availability.state != "unavailable" or section.claims
    )
    source_mode_label = {
        "deterministic_only": "Deterministic only",
        "deterministic_with_ollama": "Deterministic with Ollama",
    }[report.source_mode]
    ollama_status_label = (
        "Ollama was not requested"
        if report.ollama.status == "not_requested"
        else f"Ollama {report.ollama.status.replace('_', ' ')}"
    )
    selected_player = next(
        (
            player
            for player in report.players
            if player.replay_player_public_id == report.version.replay_player_public_id
        ),
        None,
    )
    highlight_claims = _highlight_claims(report)
    return ReplayReportViewModel(
        report=report,
        timeline=timeline,
        jump_sections=jump_sections,
        selected_player=selected_player,
        highlight_claims=highlight_claims,
        highlight_scope_label=_highlight_scope_label(report, highlight_claims),
        duration_label=duration_label,
        source_mode_label=source_mode_label,
        ollama_status_label=ollama_status_label,
    )


class EvidenceMetadataDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    value: str


class EvidenceDetailViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    detail: EvidenceDetailDTO
    source_title: str
    metadata: tuple[EvidenceMetadataDTO, ...]
    payload_label: str
    payload_text: str
    citations: tuple[EvidenceLinkDTO, ...]


def _thaw(value: object) -> object:
    return thaw_report_value(cast(CanonicalValue, value))


def _payload(value: object) -> str:
    return json.dumps(_thaw(value), ensure_ascii=False, indent=2, sort_keys=True)


# TheSuperHackers @feature Leex 23/08/2026 Present six typed evidence families without flattening their authority boundary. (#TBD)
def evidence_detail_view(detail: EvidenceDetailDTO) -> EvidenceDetailViewModel:
    source = detail.source
    title: str
    metadata: tuple[EvidenceMetadataDTO, ...]
    payload_label: str
    payload: object
    citations: tuple[EvidenceLinkDTO, ...] = ()
    if type(source) is ObservedCommandEvidenceDTO:
        title = "Observed replay command"
        metadata = (
            EvidenceMetadataDTO(label="Parser run", value=source.parser_run_id),
            EvidenceMetadataDTO(label="Parser version", value=source.parser_version),
            EvidenceMetadataDTO(label="Byte locator", value=f"{source.start_offset}–{source.end_offset}"),
            EvidenceMetadataDTO(label="Frame", value=str(source.frame)),
            EvidenceMetadataDTO(label="Command", value=source.message_name),
        )
        payload_label, payload = "Raw command arguments", source.arguments
    elif type(source) is ObservedTelemetryEvidenceDTO:
        title = "Observed telemetry event"
        metadata = (
            EvidenceMetadataDTO(label="Telemetry run", value=source.telemetry_run_id),
            EvidenceMetadataDTO(label="Engine build", value=source.engine_build),
            EvidenceMetadataDTO(label="Sequence", value=str(source.sequence)),
            EvidenceMetadataDTO(label="Frame", value=str(source.frame)),
            EvidenceMetadataDTO(label="Event", value=source.event_type),
        )
        payload_label, payload = "Raw telemetry payload", source.payload
    elif type(source) is DerivedFeatureEvidenceDTO:
        title = "Derived feature"
        metadata = (
            EvidenceMetadataDTO(label="Feature", value=source.feature_name),
            EvidenceMetadataDTO(label="Extractor", value=f"{source.extractor_name} / {source.extractor_version}"),
            EvidenceMetadataDTO(label="Frame window", value=f"{source.frame_start}–{source.frame_end}"),
            EvidenceMetadataDTO(label="Availability", value=source.availability),
        )
        payload_label, payload, citations = (
            "Formula, value, scope, and details",
            {
                "raw_value": _thaw(source.raw_value),
                "unit": source.unit,
                "scope": _thaw(source.scope),
                "details": _thaw(source.details),
            },
            source.inputs,
        )
    elif type(source) is DerivedAssessmentEvidenceDTO:
        title = "Derived strategy assessment"
        metadata = (
            EvidenceMetadataDTO(label="Strategy", value=source.strategy_label),
            EvidenceMetadataDTO(label="Phase", value=source.phase),
            EvidenceMetadataDTO(label="Rule version", value=source.rule_version),
            EvidenceMetadataDTO(label="Taxonomy", value=source.taxonomy_version),
        )
        payload_label, payload, citations = (
            "Rule result and details",
            {
                "score": source.score,
                "availability": source.availability,
                "unavailable_reason": source.unavailable_reason,
                "details": _thaw(source.details),
            },
            source.citations,
        )
    elif type(source) is DerivedLongitudinalEvidenceDTO:
        title = "Derived longitudinal context"
        metadata = (
            EvidenceMetadataDTO(label="Analyzer", value=f"{source.analyzer_name} / {source.analyzer_version}"),
            EvidenceMetadataDTO(label="Result", value=source.result_name),
            EvidenceMetadataDTO(label="Samples", value=str(source.sample_count)),
            EvidenceMetadataDTO(label="Missing", value=str(source.missing_count)),
        )
        payload_label, payload, citations = "Corpus statistics", source.statistics, source.members
    elif type(source) is InferredAssessmentEvidenceDTO:
        title = "Inferred model assessment"
        metadata = (
            EvidenceMetadataDTO(label="Provider", value=source.provider),
            EvidenceMetadataDTO(label="Model", value=source.model_name),
            EvidenceMetadataDTO(label="Prompt", value=source.prompt_version),
            EvidenceMetadataDTO(label="Confidence", value=f"{source.confidence:.2f}"),
        )
        payload_label, payload, citations = "Validated assessment", source.assessment, source.citations
    else:  # pragma: no cover - EvidenceDetailDTO closes the union.
        raise TypeError("unsupported evidence source")
    return EvidenceDetailViewModel(
        detail=detail,
        source_title=title,
        metadata=metadata,
        payload_label=payload_label,
        payload_text=_payload(payload),
        citations=citations,
    )
