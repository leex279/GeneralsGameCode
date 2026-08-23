from __future__ import annotations

from fastapi.testclient import TestClient

from generals_replay_analyzer.web.adapters.report import AnalyticsReportAdapter
from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.dependencies import AnalyticsPortFactory
from generals_replay_analyzer.web.ports import (
    EvidenceQueryDTO,
    FixedReportQueryDTO,
    LatestReportQueryDTO,
    TimelineChartQueryDTO,
)

from .conftest import SeededReportDatabase
from .test_query import _publish_graph


class _Readiness:
    def schema_revision(self) -> str:
        return "0005"


class _Bootstrapper:
    def prepare(self, _settings: object) -> None:
        return None


def test_production_web_adapter_projects_one_fixed_report_and_its_evidence(
    report_database: SeededReportDatabase,
) -> None:
    published = _publish_graph(report_database)
    adapter = AnalyticsReportAdapter(published.service)

    resolution = adapter.resolve_latest(
        LatestReportQueryDTO(
            replay_public_id=published.replay_public_id,
            replay_player_public_id=published.player_public_id,
        )
    )
    assert resolution.fixed_report == FixedReportQueryDTO(
        replay_public_id=published.replay_public_id,
        report_public_id=published.player_report_id,
    )
    report = adapter.get_report(resolution.fixed_report)
    assert report.fixed_report == resolution.fixed_report
    assert report.generated_at.utcoffset() is not None
    assert tuple(section.key for section in report.sections) == (
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

    cited = next(
        evidence
        for section in report.sections
        for claim in section.claims
        for evidence in claim.evidence
    )
    detail = adapter.get_evidence(
        EvidenceQueryDTO(
            report_public_id=published.player_report_id,
            evidence_public_id=cited.public_id,
            expected_tier=cited.tier,
        )
    )
    assert detail.query.evidence_public_id == cited.public_id

    expected_sources = (
        (published.parser_evidence_id, "observed", "ObservedCommandEvidenceDTO"),
        (published.telemetry_evidence_id, "observed", "ObservedTelemetryEvidenceDTO"),
        (published.feature_evidence_id, "derived", "DerivedFeatureEvidenceDTO"),
        (published.rule_evidence_id, "derived", "DerivedAssessmentEvidenceDTO"),
        (published.longitudinal_evidence_id, "derived", "DerivedLongitudinalEvidenceDTO"),
        (published.inferred_evidence_id, "inferred", "InferredAssessmentEvidenceDTO"),
    )
    for evidence_public_id, tier, expected_type in expected_sources:
        projected = adapter.get_evidence(
            EvidenceQueryDTO(
                report_public_id=published.player_report_id,
                evidence_public_id=evidence_public_id,
                expected_tier=tier,  # type: ignore[arg-type]
            )
        )
        assert type(projected.source).__name__ == expected_type

    timeline = adapter.timeline_chart(
        TimelineChartQueryDTO(
            replay_public_id=published.replay_public_id,
            report_public_id=published.player_report_id,
        )
    )
    assert timeline.query.report_public_id == report.fixed_report.report_public_id
    assert timeline.timebase_fps == 30


def test_production_web_adapter_reports_honest_not_generated_state(
    report_database: SeededReportDatabase,
) -> None:
    published = _publish_graph(report_database)
    adapter = AnalyticsReportAdapter(published.service)

    missing = adapter.resolve_latest(
        LatestReportQueryDTO(replay_public_id="123e4567-e89b-42d3-a456-426614179999")
    )

    assert missing.state == "not_generated"
    assert missing.reason_code == "report_not_generated"


def test_production_request_factory_serves_the_persisted_fixed_report(
    report_database: SeededReportDatabase,
) -> None:
    published = _publish_graph(report_database)
    app = create_app(
        report_database.settings,
        port_factory=AnalyticsPortFactory(report_database.settings, _Readiness()),
        bootstrapper=_Bootstrapper(),
    )

    with TestClient(app) as client:
        response = client.get(
            f"/replays/{published.replay_public_id}/reports/{published.player_report_id}",
            headers={"accept": "text/html", "host": "localhost"},
        )

    assert response.status_code == 200, response.text
    assert "Report analysis" in response.text
    assert published.player_report_id in response.text
