"""Frozen path-free values returned by the published-report query boundary."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal, TypeAlias
from uuid import UUID

from generals_replay_analyzer.report.model import (
    CanonicalValue,
    ReportAvailability,
    ReportDocument,
    ReportEvidenceTier,
    freeze_report_value,
)

EvidenceRole: TypeAlias = Literal["input", "supporting", "contradicting"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _uuid(value: object, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a canonical public UUID")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f"{label} must be a canonical public UUID") from None
    if str(parsed) != value:
        raise ValueError(f"{label} must be a canonical public UUID")
    return value


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be a nonempty built-in string")
    try:
        freeze_report_value(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be path-free semantic text") from exc
    return value


def _payload_text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be a nonempty built-in string")
    try:
        freeze_report_value(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be path-free semantic text") from exc
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class PublishedReportAssetDTO:
    public_id: str
    sha256: str
    kind: Literal["report_structured_json", "report_presentation_bundle"]
    media_type: Literal["application/json"]
    size_bytes: int

    def __post_init__(self) -> None:
        _uuid(self.public_id, "asset public_id")
        _sha256(self.sha256, "asset sha256")
        if self.kind not in ("report_structured_json", "report_presentation_bundle"):
            raise ValueError("unsupported report asset kind")
        if self.media_type != "application/json":
            raise ValueError("report assets must use application/json")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("asset size_bytes must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class PublishedReportDTO:
    document: ReportDocument
    structured_asset: PublishedReportAssetDTO
    presentation_asset: PublishedReportAssetDTO
    html: str
    text: str

    def __post_init__(self) -> None:
        if type(self.document) is not ReportDocument:
            raise TypeError("document must be an accepted ReportDocument")
        if (
            type(self.structured_asset) is not PublishedReportAssetDTO
            or self.structured_asset.kind != "report_structured_json"
            or type(self.presentation_asset) is not PublishedReportAssetDTO
            or self.presentation_asset.kind != "report_presentation_bundle"
        ):
            raise TypeError("published report assets use their exact closed roles")
        _payload_text(self.html, "html")
        _payload_text(self.text, "text")


@dataclass(frozen=True, slots=True)
class PublishedReportGraphDTO:
    schema_version: Literal["replay-report-read-model-v1"]
    output_schema_version: Literal["report-output-v1"]
    replay_public_id: str
    selected_report_public_id: str
    replay_wide: PublishedReportDTO
    player_reports: tuple[PublishedReportDTO, ...]

    def __post_init__(self) -> None:
        if self.schema_version != "replay-report-read-model-v1":
            raise ValueError("unsupported report read-model version")
        if self.output_schema_version != "report-output-v1":
            raise ValueError("unsupported render-report output version")
        _uuid(self.replay_public_id, "replay_public_id")
        _uuid(self.selected_report_public_id, "selected_report_public_id")
        if type(self.replay_wide) is not PublishedReportDTO:
            raise TypeError("replay_wide must be a PublishedReportDTO")
        if self.replay_wide.document.replay_player_public_id is not None:
            raise ValueError("replay-wide report cannot be player scoped")
        if type(self.player_reports) is not tuple or any(
            type(item) is not PublishedReportDTO for item in self.player_reports
        ):
            raise TypeError("player_reports must be an immutable typed tuple")
        ordered = tuple(sorted(self.player_reports, key=lambda item: item.document.replay_player_public_id or ""))
        player_ids = tuple(item.document.replay_player_public_id for item in ordered)
        if None in player_ids or len(set(player_ids)) != len(player_ids):
            raise ValueError("player report subjects must be unique and non-null")
        members = (self.replay_wide, *ordered)
        if any(item.document.replay_public_id != self.replay_public_id for item in members):
            raise ValueError("report graph contains a cross-replay document")
        if self.selected_report_public_id not in {item.document.report_public_id for item in members}:
            raise ValueError("selected report is not a member of the report graph")
        object.__setattr__(self, "player_reports", ordered)

    @property
    def selected(self) -> PublishedReportDTO:
        for item in (self.replay_wide, *self.player_reports):
            if item.document.report_public_id == self.selected_report_public_id:
                return item
        raise RuntimeError("validated selected report disappeared")  # pragma: no cover


@dataclass(frozen=True, slots=True)
class EvidenceLinkDTO:
    public_id: str
    tier: ReportEvidenceTier
    role: EvidenceRole

    def __post_init__(self) -> None:
        _uuid(self.public_id, "evidence public_id")
        if self.tier not in ("observed", "derived", "inferred"):
            raise ValueError("invalid evidence tier")
        if self.role not in ("input", "supporting", "contradicting"):
            raise ValueError("invalid evidence role")


@dataclass(frozen=True, slots=True)
class ObservedCommandEvidenceDTO:
    kind: Literal["replay_command"]
    parser_run_id: str
    parser_version: str
    parser_schema_version: int
    start_offset: int
    end_offset: int
    frame: int
    message_type: int
    message_name: str
    replay_player_public_id: str | None
    arguments: CanonicalValue

    def __post_init__(self) -> None:
        if self.kind != "replay_command":
            raise ValueError("invalid observed command kind")
        _uuid(self.parser_run_id, "parser_run_id")
        _text(self.parser_version, "parser_version")
        if self.replay_player_public_id is not None:
            _uuid(self.replay_player_public_id, "replay_player_public_id")
        if (
            type(self.parser_schema_version) is not int
            or self.parser_schema_version < 0
            or type(self.start_offset) is not int
            or self.start_offset < 0
            or type(self.end_offset) is not int
            or self.end_offset <= self.start_offset
            or type(self.frame) is not int
            or self.frame < 0
            or type(self.message_type) is not int
            or self.message_type < 0
        ):
            raise ValueError("observed command numeric identity is invalid")
        _text(self.message_name, "message_name")
        object.__setattr__(self, "arguments", freeze_report_value(self.arguments))


@dataclass(frozen=True, slots=True)
class ObservedTelemetryEvidenceDTO:
    kind: Literal["telemetry_event"]
    telemetry_run_id: str
    engine_build: str
    telemetry_schema_version: int
    sequence: int
    frame: int
    event_type: str
    payload: CanonicalValue

    def __post_init__(self) -> None:
        if self.kind != "telemetry_event":
            raise ValueError("invalid observed telemetry kind")
        _uuid(self.telemetry_run_id, "telemetry_run_id")
        _text(self.engine_build, "engine_build")
        _text(self.event_type, "event_type")
        if (
            type(self.telemetry_schema_version) is not int
            or self.telemetry_schema_version < 0
            or type(self.sequence) is not int
            or self.sequence < 0
            or type(self.frame) is not int
            or self.frame < 0
        ):
            raise ValueError("observed telemetry numeric identity is invalid")
        object.__setattr__(self, "payload", freeze_report_value(self.payload))


@dataclass(frozen=True, slots=True)
class DerivedFeatureEvidenceDTO:
    kind: Literal["derived_feature"]
    feature_public_id: str
    feature_set_public_id: str
    feature_name: str
    extractor_name: str
    extractor_version: str
    raw_value: CanonicalValue | None
    unit: str | None
    scope: CanonicalValue
    frame_start: int
    frame_end: int
    availability: ReportAvailability
    unavailable_reason: str | None
    details: CanonicalValue
    inputs: tuple[EvidenceLinkDTO, ...]

    def __post_init__(self) -> None:
        if self.kind != "derived_feature":
            raise ValueError("invalid derived feature kind")
        _uuid(self.feature_public_id, "feature_public_id")
        _uuid(self.feature_set_public_id, "feature_set_public_id")
        for value, label in (
            (self.feature_name, "feature_name"),
            (self.extractor_name, "extractor_name"),
            (self.extractor_version, "extractor_version"),
        ):
            _text(value, label)
        if self.unit is not None:
            _text(self.unit, "unit")
        if (
            type(self.frame_start) is not int
            or self.frame_start < 0
            or type(self.frame_end) is not int
            or self.frame_end < self.frame_start
            or self.availability not in ("available", "partial", "unavailable")
        ):
            raise ValueError("derived feature window or availability is invalid")
        raw = freeze_report_value(self.raw_value)
        if (raw is None) != (self.availability == "unavailable"):
            raise ValueError("derived feature nullability must match availability")
        if self.availability == "available" and self.unavailable_reason is not None:
            raise ValueError("available derived feature cannot have a reason")
        if self.availability != "available" and self.unavailable_reason is None:
            raise ValueError("non-available derived feature requires a reason")
        if self.unavailable_reason is not None:
            _text(self.unavailable_reason, "unavailable_reason")
        object.__setattr__(self, "raw_value", raw)
        object.__setattr__(self, "scope", freeze_report_value(self.scope))
        object.__setattr__(self, "details", freeze_report_value(self.details))
        object.__setattr__(self, "inputs", _ordered_links(self.inputs))


@dataclass(frozen=True, slots=True)
class DerivedAssessmentEvidenceDTO:
    kind: Literal["derived_assessment"]
    assessment_public_id: str
    strategy_label: str
    phase: str
    taxonomy_version: str
    rule_version: str
    frame_start: int
    frame_end: int
    score: float | None
    availability: ReportAvailability
    unavailable_reason: str | None
    details: CanonicalValue
    citations: tuple[EvidenceLinkDTO, ...]

    def __post_init__(self) -> None:
        if self.kind != "derived_assessment":
            raise ValueError("invalid derived assessment kind")
        _uuid(self.assessment_public_id, "assessment_public_id")
        for value, label in (
            (self.strategy_label, "strategy_label"),
            (self.phase, "phase"),
            (self.taxonomy_version, "taxonomy_version"),
            (self.rule_version, "rule_version"),
        ):
            _text(value, label)
        if (
            type(self.frame_start) is not int
            or self.frame_start < 0
            or type(self.frame_end) is not int
            or self.frame_end < self.frame_start
            or self.availability not in ("available", "partial", "unavailable")
        ):
            raise ValueError("derived assessment window or availability is invalid")
        if self.score is not None and (
            type(self.score) is not float
            or not math.isfinite(self.score)
            or not 0.0 <= self.score <= 1.0
            or (self.score == 0.0 and math.copysign(1.0, self.score) < 0)
        ):
            raise ValueError("derived assessment score must be a bounded float")
        if self.availability == "available" and self.unavailable_reason is not None:
            raise ValueError("available derived assessment cannot have a reason")
        if self.availability != "available" and self.unavailable_reason is None:
            raise ValueError("non-available derived assessment requires a reason")
        if self.unavailable_reason is not None:
            _text(self.unavailable_reason, "unavailable_reason")
        object.__setattr__(self, "details", freeze_report_value(self.details))
        object.__setattr__(self, "citations", _ordered_links(self.citations))


@dataclass(frozen=True, slots=True)
class DerivedLongitudinalEvidenceDTO:
    kind: Literal["longitudinal_result"]
    result_public_id: str
    longitudinal_run_id: str
    analyzer_name: str
    analyzer_version: str
    result_name: str
    result_kind: str
    sample_count: int
    missing_count: int
    availability: ReportAvailability
    unavailable_reason: str | None
    statistics: CanonicalValue
    members: tuple[EvidenceLinkDTO, ...]

    def __post_init__(self) -> None:
        if self.kind != "longitudinal_result":
            raise ValueError("invalid longitudinal result kind")
        _uuid(self.result_public_id, "result_public_id")
        _uuid(self.longitudinal_run_id, "longitudinal_run_id")
        for value, label in (
            (self.analyzer_name, "analyzer_name"),
            (self.analyzer_version, "analyzer_version"),
            (self.result_name, "result_name"),
            (self.result_kind, "result_kind"),
        ):
            _text(value, label)
        if (
            type(self.sample_count) is not int
            or self.sample_count < 0
            or type(self.missing_count) is not int
            or self.missing_count < 0
            or self.availability not in ("available", "partial", "unavailable")
        ):
            raise ValueError("longitudinal counts or availability are invalid")
        if self.availability == "available" and self.unavailable_reason is not None:
            raise ValueError("available longitudinal evidence cannot have a reason")
        if self.availability != "available" and self.unavailable_reason is None:
            raise ValueError("non-available longitudinal evidence requires a reason")
        if self.unavailable_reason is not None:
            _text(self.unavailable_reason, "unavailable_reason")
        object.__setattr__(self, "statistics", freeze_report_value(self.statistics))
        object.__setattr__(self, "members", _ordered_links(self.members))


@dataclass(frozen=True, slots=True)
class InferredAssessmentEvidenceDTO:
    kind: Literal["inferred_assessment"]
    assessment_public_id: str
    analysis_run_id: str
    assessment_key: str
    strategy_label: str
    phase: str
    frame_start: int
    frame_end: int
    confidence: float
    provider: str
    model_name: str
    model_digest: str
    prompt_version: str
    response_schema_version: str
    assessment: CanonicalValue
    citations: tuple[EvidenceLinkDTO, ...]

    def __post_init__(self) -> None:
        if self.kind != "inferred_assessment":
            raise ValueError("invalid inferred assessment kind")
        _uuid(self.assessment_public_id, "assessment_public_id")
        _uuid(self.analysis_run_id, "analysis_run_id")
        for value, label in (
            (self.assessment_key, "assessment_key"),
            (self.strategy_label, "strategy_label"),
            (self.phase, "phase"),
            (self.provider, "provider"),
            (self.model_name, "model_name"),
            (self.prompt_version, "prompt_version"),
            (self.response_schema_version, "response_schema_version"),
        ):
            _text(value, label)
        _sha256(self.model_digest, "model_digest")
        if (
            type(self.frame_start) is not int
            or self.frame_start < 0
            or type(self.frame_end) is not int
            or self.frame_end < self.frame_start
            or type(self.confidence) is not float
            or not math.isfinite(self.confidence)
            or not 0.0 <= self.confidence <= 1.0
            or (self.confidence == 0.0 and math.copysign(1.0, self.confidence) < 0)
        ):
            raise ValueError("inferred assessment window or confidence is invalid")
        object.__setattr__(self, "assessment", freeze_report_value(self.assessment))
        object.__setattr__(self, "citations", _ordered_links(self.citations))


EvidenceSourceDTO: TypeAlias = (
    ObservedCommandEvidenceDTO
    | ObservedTelemetryEvidenceDTO
    | DerivedFeatureEvidenceDTO
    | DerivedAssessmentEvidenceDTO
    | DerivedLongitudinalEvidenceDTO
    | InferredAssessmentEvidenceDTO
)


@dataclass(frozen=True, slots=True)
class EvidenceDetailDTO:
    schema_version: Literal["replay-evidence-inspector-v1"]
    report_public_id: str
    replay_public_id: str
    evidence_public_id: str
    tier: ReportEvidenceTier
    source_kind: str
    source_schema_version: int
    source: EvidenceSourceDTO

    def __post_init__(self) -> None:
        if self.schema_version != "replay-evidence-inspector-v1":
            raise ValueError("unsupported evidence inspector version")
        _uuid(self.report_public_id, "report_public_id")
        _uuid(self.replay_public_id, "replay_public_id")
        _uuid(self.evidence_public_id, "evidence_public_id")
        if self.tier not in ("observed", "derived", "inferred"):
            raise ValueError("invalid evidence tier")
        _text(self.source_kind, "source_kind")
        if type(self.source_schema_version) is not int or self.source_schema_version < 0:
            raise ValueError("source_schema_version must be nonnegative")
        if type(self.source) is ObservedCommandEvidenceDTO:
            expected = ("observed", "parser_command", self.source.parser_schema_version)
        elif type(self.source) is ObservedTelemetryEvidenceDTO:
            expected = ("observed", "telemetry_event", self.source.telemetry_schema_version)
        elif type(self.source) is DerivedFeatureEvidenceDTO:
            expected = ("derived", "feature", 1)
        elif type(self.source) is DerivedAssessmentEvidenceDTO:
            expected = ("derived", "strategy_rule", 1)
        elif type(self.source) is DerivedLongitudinalEvidenceDTO:
            expected = ("derived", "longitudinal_corpus", 1)
        elif type(self.source) is InferredAssessmentEvidenceDTO:
            expected = ("inferred", "llm", 1)
        else:
            raise TypeError("source must be one exact accepted evidence DTO")
        if (self.tier, self.source_kind, self.source_schema_version) != expected:
            raise ValueError("evidence tier, source kind, and schema version must match the source DTO")


def _ordered_links(values: tuple[EvidenceLinkDTO, ...]) -> tuple[EvidenceLinkDTO, ...]:
    if type(values) is not tuple or any(type(item) is not EvidenceLinkDTO for item in values):
        raise TypeError("evidence links must be an immutable typed tuple")
    identities = tuple((item.role, item.tier, item.public_id) for item in values)
    if len(identities) != len(set(identities)):
        raise ValueError("evidence links must be unique")
    return tuple(sorted(values, key=lambda item: (item.role, item.tier, item.public_id)))
