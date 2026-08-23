from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from generals_replay_analyzer.db.models import ParserRun, Replay, ReplayPlayer
from generals_replay_analyzer.web.adapters.report import AnalyticsReportAdapter, _display_value
from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.dependencies import AnalyticsPortFactory
from generals_replay_analyzer.web.ports import (
    EvidenceQueryDTO,
    FixedReportQueryDTO,
    LatestReportQueryDTO,
    TimelineChartQueryDTO,
)

from .conftest import SeededReportDatabase, stable_uuid
from .test_query import _publish_graph


class _Readiness:
    def schema_revision(self) -> str:
        return "0005"


class _Bootstrapper:
    def prepare(self, _settings: object) -> None:
        return None


def test_display_value_preserves_exact_boundary_and_summarizes_oversize_canonical_values() -> None:
    boundary = "x" * 2048
    players_initialized = {
        "engine_player_indices": [0, 1],
        "slots": [
            {"slot_index": index, "resolution_status": "resolved", "replay_name": f"Player {index}"}
            for index in range(256)
        ],
    }
    canonical = json.dumps(
        players_initialized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert _display_value(boundary) == boundary
    assert _display_value("x" * 2049) == (
        "Oversize string (characters=2049,utf8_bytes=2049,"
        f"sha256={hashlib.sha256(json.dumps('x' * 2049, ensure_ascii=False).encode('utf-8')).hexdigest()})"
    )
    summary = _display_value(players_initialized)
    assert summary == (
        "Oversize mapping (entries=2,"
        f"canonical_utf8_bytes={len(canonical)},sha256={hashlib.sha256(canonical).hexdigest()})"
    )
    assert len(summary) <= 2048


def test_display_value_unicode_digest_is_deterministic_and_changes_with_canonical_content() -> None:
    first = ["Generäle 🛰️" * 300]
    second = ["Generäle 🛰️" * 299 + "X"]

    first_summary = _display_value(first)

    assert first_summary == _display_value(first)
    assert first_summary.startswith("Oversize list (items=1,canonical_utf8_bytes=")
    assert first_summary != _display_value(second)


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

    timeline_query = TimelineChartQueryDTO(
        replay_public_id=published.replay_public_id,
        report_public_id=published.player_report_id,
    )
    timeline = adapter.timeline_chart(timeline_query)
    assert timeline.query == timeline_query
    assert timeline.query.report_public_id == report.fixed_report.report_public_id
    assert timeline.timebase_fps == 30


def test_web_report_excludes_a_fabricated_closed_slot_outside_parser_subjects(
    report_database: SeededReportDatabase,
) -> None:
    published = _publish_graph(report_database)
    with report_database.session_factory.begin() as session:  # type: ignore[union-attr]
        session.execute(text("DROP TRIGGER trg_replay_players_succeeded_no_insert"))
        replay = session.scalar(select(Replay).where(Replay.public_id == published.replay_public_id))
        assert replay is not None
        parser = session.scalar(select(ParserRun).where(ParserRun.replay_id == replay.id))
        assert parser is not None
        session.add(
            ReplayPlayer(
                public_id=stable_uuid("web-closed-slot"),
                replay_id=replay.id,
                parser_run_id=parser.id,
                slot_index=7,
                slot_kind="closed",
                original_name="Fabricated Closed Slot",
                normalized_name="fabricated closed slot",
                observed_json={},
            )
        )

    adapter = AnalyticsReportAdapter(published.service)
    report = adapter.get_report(
        FixedReportQueryDTO(
            replay_public_id=published.replay_public_id,
            report_public_id=published.player_report_id,
        )
    )

    assert tuple(player.display_name for player in report.players) == ("Player",)


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
    assert "Replay analysis" in response.text
    assert published.player_report_id in response.text
