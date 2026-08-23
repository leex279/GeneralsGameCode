"""Typed, report-scoped evidence inspector contracts and routes."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from generals_replay_analyzer.web.ports import (
    DerivedAssessmentEvidenceDTO,
    DerivedFeatureEvidenceDTO,
    DerivedLongitudinalEvidenceDTO,
    EvidenceDetailDTO,
    EvidenceLinkDTO,
    EvidenceQueryDTO,
    InferredAssessmentEvidenceDTO,
    ObservedCommandEvidenceDTO,
    ObservedTelemetryEvidenceDTO,
)

from .test_report import _client, _report, _ReportPort

REPLAY_ID = "123e4567-e89b-42d3-a456-426614174100"
REPORT_ID = "123e4567-e89b-42d3-a456-426614174101"
PLAYER_ID = "123e4567-e89b-42d3-a456-426614174102"
EVIDENCE_ID = "123e4567-e89b-42d3-a456-426614174103"
INPUT_ID = "123e4567-e89b-42d3-a456-426614174106"
RUN_ID = "123e4567-e89b-42d3-a456-426614174107"
MODEL_DIGEST = "b" * 64


def _link(tier: str = "observed") -> EvidenceLinkDTO:
    return EvidenceLinkDTO(public_id=INPUT_ID, tier=tier, role="input")  # type: ignore[arg-type]


@dataclass(frozen=True)
class _Variant:
    tier: str
    source_kind: str
    schema_version: int
    source: object


VARIANTS = (
    _Variant(
        "observed",
        "parser_command",
        1,
        ObservedCommandEvidenceDTO(
            kind="replay_command",
            parser_run_id=RUN_ID,
            parser_version="fixture-parser-v1",
            parser_schema_version=1,
            start_offset=342,
            end_offset=358,
            frame=30,
            message_type=1001,
            message_name="MSG_CREATE_SELECTED_GROUP",
            replay_player_public_id=PLAYER_ID,
            arguments={"group": 1},
        ),
    ),
    _Variant(
        "observed",
        "telemetry_event",
        2,
        ObservedTelemetryEvidenceDTO(
            kind="telemetry_event",
            telemetry_run_id=RUN_ID,
            engine_build="fixture-engine",
            telemetry_schema_version=2,
            sequence=7,
            frame=60,
            event_type="production_started",
            payload={"thing": "ChinaTankBattleMaster"},
        ),
    ),
    _Variant(
        "derived",
        "feature",
        1,
        DerivedFeatureEvidenceDTO(
            kind="derived_feature",
            feature_public_id="123e4567-e89b-42d3-a456-426614174108",
            feature_set_public_id="123e4567-e89b-42d3-a456-426614174109",
            feature_name="first_supply_frame",
            extractor_name="build_order",
            extractor_version="v1",
            raw_value=450,
            unit="frame",
            scope={"scope_type": "player"},
            frame_start=0,
            frame_end=450,
            availability="available",
            unavailable_reason=None,
            details={"formula": "minimum completed supply frame"},
            inputs=(_link(),),
        ),
    ),
    _Variant(
        "derived",
        "strategy_rule",
        1,
        DerivedAssessmentEvidenceDTO(
            kind="derived_assessment",
            assessment_public_id="123e4567-e89b-42d3-a456-426614174110",
            strategy_label="two-supply opening",
            phase="opening",
            taxonomy_version="strategy-taxonomy-v1",
            rule_version="rule-v1",
            frame_start=0,
            frame_end=900,
            score=0.75,
            availability="available",
            unavailable_reason=None,
            details={"formula": "two supplies before frame 900"},
            citations=(_link(),),
        ),
    ),
    _Variant(
        "derived",
        "longitudinal_corpus",
        1,
        DerivedLongitudinalEvidenceDTO(
            kind="longitudinal_result",
            result_public_id="123e4567-e89b-42d3-a456-426614174111",
            longitudinal_run_id=RUN_ID,
            analyzer_name="player-history",
            analyzer_version="v1",
            result_name="opening_frequency",
            result_kind="frequency",
            sample_count=12,
            missing_count=2,
            availability="partial",
            unavailable_reason="two_samples_excluded",
            statistics={"rate": 0.5},
            members=(_link("derived"),),
        ),
    ),
    _Variant(
        "inferred",
        "llm",
        1,
        InferredAssessmentEvidenceDTO(
            kind="inferred_assessment",
            assessment_public_id="123e4567-e89b-42d3-a456-426614174112",
            analysis_run_id=RUN_ID,
            assessment_key="opening-pressure",
            strategy_label="early pressure",
            phase="opening",
            frame_start=0,
            frame_end=900,
            confidence=0.8,
            provider="ollama",
            model_name="fixture-model",
            model_digest=MODEL_DIGEST,
            prompt_version="strategy-report-v1",
            response_schema_version="strategy-response-v1",
            assessment={"summary": "Early pressure supported by cited commands."},
            citations=(_link(),),
        ),
    ),
)


@pytest.mark.parametrize("variant", VARIANTS)
def test_evidence_detail_preserves_each_typed_source_variant(variant: _Variant) -> None:
    """Catch evidence detail flattening an accepted source into generic label/value strings."""
    query = EvidenceQueryDTO(
        report_public_id=REPORT_ID,
        evidence_public_id=EVIDENCE_ID,
        expected_tier=variant.tier,
    )
    detail = EvidenceDetailDTO(
        schema_version="web-evidence-inspector-v1",
        query=query,
        replay_public_id=REPLAY_ID,
        source_kind=variant.source_kind,
        source_schema_version=variant.schema_version,
        source=variant.source,
    )

    round_trip = EvidenceDetailDTO.model_validate(detail.model_dump(mode="python"))
    assert type(round_trip.source) is type(variant.source)
    assert round_trip.query == query


def test_evidence_detail_rejects_tier_or_source_kind_drift() -> None:
    """Catch a route path tier being rebound to a different accepted source family."""
    observed = VARIANTS[0]
    with pytest.raises(ValidationError, match="tier, source kind, and schema version"):
        EvidenceDetailDTO(
            schema_version="web-evidence-inspector-v1",
            query=EvidenceQueryDTO(
                report_public_id=REPORT_ID,
                evidence_public_id=EVIDENCE_ID,
                expected_tier="derived",
            ),
            replay_public_id=REPLAY_ID,
            source_kind=observed.source_kind,
            source_schema_version=observed.schema_version,
            source=observed.source,
        )


def test_typed_evidence_sources_reject_private_locator_payloads() -> None:
    """Catch raw evidence payloads leaking managed filesystem identity."""
    source = VARIANTS[1].source
    assert isinstance(source, ObservedTelemetryEvidenceDTO)
    with pytest.raises(ValidationError, match="absolute paths are not canonical report values"):
        ObservedTelemetryEvidenceDTO(**{**source.model_dump(), "payload": {"trace": "C:\\private\\trace.ndjson"}})


class _EvidencePort(_ReportPort):
    def __init__(self, detail: EvidenceDetailDTO) -> None:
        super().__init__(_report())
        self.detail = detail
        self.evidence_queries: list[EvidenceQueryDTO] = []

    def get_evidence(self, query: EvidenceQueryDTO) -> EvidenceDetailDTO:
        self.evidence_queries.append(query)
        return self.detail


@pytest.mark.parametrize("variant", VARIANTS)
def test_evidence_route_renders_each_typed_source_through_exact_report_scope(variant: _Variant) -> None:
    """Catch the evidence route dropping report scope or one accepted source presentation."""
    query = EvidenceQueryDTO(
        report_public_id=REPORT_ID,
        evidence_public_id=EVIDENCE_ID,
        expected_tier=variant.tier,
    )
    port = _EvidencePort(
        EvidenceDetailDTO(
            schema_version="web-evidence-inspector-v1",
            query=query,
            replay_public_id=REPLAY_ID,
            source_kind=variant.source_kind,
            source_schema_version=variant.schema_version,
            source=variant.source,
        )
    )
    with _client(port) as client:
        response = client.get(
            f"/evidence/{variant.tier}/{EVIDENCE_ID}?report_id={REPORT_ID}",
            headers={"host": "localhost", "accept": "text/html"},
        )

    assert response.status_code == 200
    assert port.evidence_queries == [query]
    assert f"Evidence tier: {variant.tier}" in response.text
    assert variant.source_kind in response.text
    assert f'data-evidence-public-id="{EVIDENCE_ID}"' in response.text


@pytest.mark.parametrize(
    "path",
    [
        f"/evidence/observed/{EVIDENCE_ID}",
        f"/evidence/unknown/{EVIDENCE_ID}?report_id={REPORT_ID}",
        f"/evidence/observed/not-a-uuid?report_id={REPORT_ID}",
        f"/evidence/observed/{EVIDENCE_ID.upper()}?report_id={REPORT_ID}",
        f"/evidence/observed/{EVIDENCE_ID}?report_id={REPORT_ID}&report_id={REPORT_ID}",
    ],
)
def test_evidence_route_rejects_unbound_or_malformed_identity(path: str) -> None:
    """Catch evidence access escaping its exact report, tier, or canonical public ID."""
    variant = VARIANTS[0]
    query = EvidenceQueryDTO(
        report_public_id=REPORT_ID,
        evidence_public_id=EVIDENCE_ID,
        expected_tier="observed",
    )
    port = _EvidencePort(
        EvidenceDetailDTO(
            schema_version="web-evidence-inspector-v1",
            query=query,
            replay_public_id=REPLAY_ID,
            source_kind=variant.source_kind,
            source_schema_version=variant.schema_version,
            source=variant.source,
        )
    )
    with _client(port) as client:
        response = client.get(path, headers={"host": "localhost", "accept": "text/html"})

    assert response.status_code == 422
    assert port.evidence_queries == []


def test_evidence_route_rejects_non_html_content_negotiation() -> None:
    """Catch the typed evidence page being served as a sniffable non-HTML representation."""
    variant = VARIANTS[0]
    query = EvidenceQueryDTO(
        report_public_id=REPORT_ID,
        evidence_public_id=EVIDENCE_ID,
        expected_tier="observed",
    )
    port = _EvidencePort(
        EvidenceDetailDTO(
            schema_version="web-evidence-inspector-v1",
            query=query,
            replay_public_id=REPLAY_ID,
            source_kind=variant.source_kind,
            source_schema_version=variant.schema_version,
            source=variant.source,
        )
    )
    with _client(port) as client:
        response = client.get(
            f"/evidence/observed/{EVIDENCE_ID}?report_id={REPORT_ID}",
            headers={"host": "localhost", "accept": "application/json"},
        )

    assert response.status_code == 406
    assert response.json()["code"] == "not_acceptable"
    assert port.evidence_queries == []
