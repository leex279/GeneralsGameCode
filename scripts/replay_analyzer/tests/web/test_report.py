"""Replay report routes and immutable fake-port contracts."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    FixedReportQueryDTO,
    LatestReportQueryDTO,
    OllamaReportStatusDTO,
    PipelineStateDTO,
    QualityIssueDTO,
    ReplayPlayerDisplayDTO,
    ReplayReportDTO,
    ReportClaimDTO,
    ReportEvidenceReferenceDTO,
    ReportLifecycleDTO,
    ReportQueryPort,
    ReportResolutionDTO,
    ReportSectionDTO,
    ReportVersionDTO,
    TerminalQualityDTO,
    TimelineChartDTO,
    TimelineChartQueryDTO,
)

from .conftest import RecordingBootstrapper

REPLAY_ID = "123e4567-e89b-42d3-a456-426614174100"
REPORT_ID = "123e4567-e89b-42d3-a456-426614174101"
PLAYER_ID = "123e4567-e89b-42d3-a456-426614174102"
EVIDENCE_ID = "123e4567-e89b-42d3-a456-426614174103"
OPPONENT_PLAYER_ID = "123e4567-e89b-42d3-a456-426614174105"
OPPONENT_REPORT_ID = "123e4567-e89b-42d3-a456-426614174106"
SHA256 = "a" * 64
SECTION_KEYS = (
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


def test_report_resolution_contract_keeps_latest_selection_separate_from_fixed_identity() -> None:
    """Catch a latest-report lookup becoming an ambiguous or mutable report read."""
    latest = LatestReportQueryDTO(replay_public_id=REPLAY_ID, replay_player_public_id=PLAYER_ID)
    fixed = FixedReportQueryDTO(replay_public_id=REPLAY_ID, report_public_id=REPORT_ID)
    version = ReportVersionDTO(
        report_public_id=REPORT_ID,
        report_version="replay-report-v1",
        replay_player_public_id=PLAYER_ID,
    )

    resolution = ReportResolutionDTO(
        state="available",
        fixed_report=fixed,
        version=version,
    )

    assert latest.model_dump(mode="json") == {
        "replay_public_id": REPLAY_ID,
        "replay_player_public_id": PLAYER_ID,
    }
    assert resolution.fixed_report == fixed
    assert resolution.version == version
    assert resolution.reason_code is None
    assert resolution.pipeline is None

    with pytest.raises(ValidationError, match="available report resolution requires"):
        ReportResolutionDTO(state="available")

    active = ReportResolutionDTO(
        state="pipeline_active",
        reason_code="report_pipeline_running",
        pipeline=PipelineStateDTO(
            stage="render_report",
            state="running",
            attempt=1,
            progress=0.5,
            replay_public_id=REPLAY_ID,
        ),
    )
    assert active.fixed_report is None and active.version is None


def _claim(section: str) -> ReportClaimDTO:
    return ReportClaimDTO(
        claim_id=f"{section}:fixture",
        section=section,
        label=f"{section.replace('_', ' ').title()} fixture",
        raw_value={"count": 10},
        display_value="10",
        unit="events",
        availability="available",
        unavailable_reason=None,
        scope={"scope_type": "replay"},
        frame_window=(0, 30),
        confidence=None,
        evidence=(ReportEvidenceReferenceDTO(public_id=EVIDENCE_ID, tier="observed"),),
        details={"definition_version": "fixture-v1"},
    )


def _report(*, sections: tuple[ReportSectionDTO, ...] | None = None) -> ReplayReportDTO:
    fixed = FixedReportQueryDTO(replay_public_id=REPLAY_ID, report_public_id=REPORT_ID)
    version = ReportVersionDTO(
        report_public_id=REPORT_ID,
        report_version="replay-report-v1",
        replay_player_public_id=PLAYER_ID,
    )
    return ReplayReportDTO(
        schema_version="web-replay-report-v1",
        generated_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
        fixed_report=fixed,
        version=version,
        availability=AvailabilityDTO(state="available"),
        replay_label="Tournament Desert fixture.rep",
        replay_sha256=SHA256,
        players=(
            ReplayPlayerDisplayDTO(display_name="Leex279", slot=1, faction="China", result="won"),
            ReplayPlayerDisplayDTO(display_name="Fox27", slot=2, faction="GLA", result="lost"),
        ),
        result="Leex279 won",
        map_name="Tournament Desert",
        patch="1.04",
        duration_frames=3_600,
        source_mode="deterministic_with_ollama",
        lifecycle=ReportLifecycleDTO(
            lifecycle_state="engine_verified",
            parser_completion_status="complete",
            telemetry_status="complete",
            telemetry_runner_status="succeeded",
        ),
        terminal_quality=TerminalQualityDTO(lifecycle="engine_verified"),
        sections=sections
        or tuple(
            ReportSectionDTO(
                key=key,
                title=key.replace("_", " ").title(),
                availability=AvailabilityDTO(state="available"),
                claims=(_claim(key),),
            )
            for key in reversed(SECTION_KEYS)
        ),
        ollama=OllamaReportStatusDTO(
            requested=True,
            status="succeeded",
            analysis_run_id="123e4567-e89b-42d3-a456-426614174104",
            provider="ollama",
            model_name="fixture-model",
            model_digest="b" * 64,
            prompt_version="strategy-report-v1",
            response_schema_version="strategy-response-v1",
            diagnostic_codes=(),
            validated_prose={"summary": "Validated local interpretation."},
        ),
        warnings=("Fixture warning",),
    )


def _replace_report(report: ReplayReportDTO, **updates: object) -> ReplayReportDTO:
    return ReplayReportDTO.model_validate({**report.model_dump(), **updates})


def _unavailable_section(key: str, reason: str) -> ReportSectionDTO:
    return ReportSectionDTO(
        key=key,  # type: ignore[arg-type]
        title=key.replace("_", " ").title(),
        availability=AvailabilityDTO(state="unavailable", reason_codes=(reason,)),
        claims=(),
    )


def test_report_dto_preserves_raw_semantics_and_all_eleven_canonical_sections() -> None:
    """Catch report presentation dropping authority fields or an analysis section."""
    report = _report()

    assert tuple(section.key for section in report.sections) == SECTION_KEYS
    overview = report.sections[0].claims[0]
    assert overview.raw_value == (("count", 10),)
    assert overview.display_value == "10"
    assert overview.scope == (("scope_type", "replay"),)
    assert overview.details == (("definition_version", "fixture-v1"),)

    with pytest.raises(ValidationError, match="exactly one of every report section"):
        _report(sections=report.sections[:-1])


def test_report_claim_rejects_placeholder_values_and_unsafe_canonical_data() -> None:
    """Catch unavailable claims gaining substitute values or leaking source locators."""
    with pytest.raises(ValidationError, match="unavailable report claim cannot expose a value"):
        _claim("overview").model_copy(
            update={"availability": "unavailable", "unavailable_reason": "telemetry_missing"}
        ).model_validate(
            {
                **_claim("overview").model_dump(),
                "availability": "unavailable",
                "unavailable_reason": "telemetry_missing",
            }
        )


def test_report_page_leads_with_player_reports_and_evidence_backed_highlights() -> None:
    """Catch strategy, coaching, and build order being buried below the evidence ledger."""
    report = _report()
    economy = ReportSectionDTO(
        key="economy",
        title="Economy",
        availability=AvailabilityDTO(state="available"),
        claims=(
            ReportClaimDTO(
                **{
                    **_claim("economy").model_dump(),
                        "claim_id": "feature:economy.supply_collection_rate:fixture",
                        "label": "economy.supply_collection_rate",
                        "raw_value": 1_350.0,
                        "display_value": "1350",
                        "unit": "credits_per_minute",
                        "evidence": (ReportEvidenceReferenceDTO(public_id=EVIDENCE_ID, tier="derived"),),
                }
            ),
        ),
    )
    build_order = ReportSectionDTO(
        key="opening_build_order",
        title="Opening build order",
        availability=AvailabilityDTO(state="available"),
        claims=(
            ReportClaimDTO(
                **{
                    **_claim("opening_build_order").model_dump(),
                    "claim_id": "feature:build.completed_sequence:fixture",
                    "label": "build.completed_sequence",
                    "raw_value": [{"frame": 45, "template_name": "AmericaPowerPlant"}],
                    "display_value": "1 completed structure",
                    "unit": "json",
                    "evidence": (ReportEvidenceReferenceDTO(public_id=EVIDENCE_ID, tier="derived"),),
                }
            ),
        ),
    )
    strategy = ReportSectionDTO(
        key="strategy_phases",
        title="Strategy phases",
        availability=AvailabilityDTO(state="available"),
        claims=(
            ReportClaimDTO(
                **{
                    **_claim("strategy_phases").model_dump(),
                    "claim_id": "strategy:usa_humvee_pressure:fixture",
                    "label": "usa_humvee_pressure",
                    "raw_value": {
                        "confidence": 1.0,
                        "phase": "early",
                        "strategy_label": "usa_humvee_pressure",
                    },
                    "display_value": "Humvee pressure",
                    "unit": None,
                    "evidence": (ReportEvidenceReferenceDTO(public_id=EVIDENCE_ID, tier="derived"),),
                }
            ),
        ),
    )
    player_report = _replace_report(
        report,
        players=(
            ReplayPlayerDisplayDTO(
                replay_player_public_id=PLAYER_ID,
                report_public_id=REPORT_ID,
                display_name="Leex279",
                slot=1,
                faction="China",
                result="won",
            ),
            ReplayPlayerDisplayDTO(
                replay_player_public_id=OPPONENT_PLAYER_ID,
                report_public_id=OPPONENT_REPORT_ID,
                display_name="Fox27",
                slot=2,
                faction="GLA",
                result="lost",
            ),
        ),
        sections=tuple(
            economy
            if section.key == "economy"
            else build_order
            if section.key == "opening_build_order"
            else strategy
            if section.key == "strategy_phases"
            else section
            for section in report.sections
        ),
    )

    with _client(_ReportPort(player_report)) as client:
        response = client.get(
            f"/replays/{REPLAY_ID}/reports/{REPORT_ID}",
            headers={"host": "localhost", "accept": "text/html"},
        )

    assert response.status_code == 200
    for first, second in (
        ('id="what-happened"', 'id="strategy"'),
        ('id="strategy"', 'id="opening-intelligence"'),
        ('id="opening-intelligence"', 'id="worth-reviewing"'),
        ('id="worth-reviewing"', 'id="report-timeline"'),
        ('id="report-timeline"', 'id="build-order"'),
        ('id="build-order"', 'id="technical-evidence"'),
    ):
        assert response.text.index(first) < response.text.index(second)
    assert "Humvee pressure" in response.text
    assert "Supply income" in response.text
    assert "1,350 supplies/min" in response.text
    assert "Power Plant" in response.text
    assert "Opening intelligence" in response.text
    assert "Power Plant completed" in response.text
    assert 'aria-label="Review derived evidence for Power Plant completed"' in response.text
    assert "No verified scouting clear inside the verified horizon." in response.text
    assert "Build order" in response.text
    assert "Army and attacks" in response.text
    assert '<details id="technical-evidence" class="workspace-panel technical-evidence">' in response.text
    assert f'/replays/{REPLAY_ID}/reports/{OPPONENT_REPORT_ID}' in response.text
    assert f'/replays/{REPLAY_ID}/reports/{REPORT_ID}/map' in response.text

    with pytest.raises(ValidationError, match="absolute paths are not canonical report values"):
        ReportClaimDTO(
            **{
                **_claim("overview").model_dump(),
                "details": {"source": "C:\\private\\trace.ndjson"},
            }
        )


def test_report_surfaces_timestamped_tactical_review_moments_before_the_raw_timeline() -> None:
    """Catch scouting and engagement evidence being buried in metric prose or technical rows."""
    report = _report()

    def event_claim(section: str, label: str, raw_value: object) -> ReportClaimDTO:
        return ReportClaimDTO(
            claim_id=f"feature:{label}:fixture",
            section=section,  # type: ignore[arg-type]
            label=label,
            raw_value=raw_value,
            display_value="fixture events",
            unit="json",
            availability="available",
            unavailable_reason=None,
            scope={"scope_type": "player", "public_id": PLAYER_ID},
            frame_window=(0, 3_600),
            confidence=None,
            evidence=(ReportEvidenceReferenceDTO(public_id=EVIDENCE_ID, tier="observed"),),
            details={"definition_version": "fixture-v1"},
        )

    replacements = {
        "activity": (
            event_claim(
                "activity",
                "scouting.first_observed_clear_timing",
                [{"frame": 150, "object_id": 42, "template_name": "ChinaWarFactory"}],
            ),
        ),
        "combat_engagements": (
            event_claim(
                "combat_engagements",
                "combat.turning_point_timing",
                [
                    {
                        "frame": 300,
                        "attacker_template_name": "AmericaVehicleHumvee",
                        "victim_template_name": "ChinaWarFactory",
                        "criterion": "engagement-swing-v1",
                    }
                ],
            ),
            event_claim(
                "combat_engagements",
                "combat.observed_kill_timing",
                [{"frame": 300, "victim_template_name": "ChinaWarFactory"}],
            ),
        ),
    }
    report = _replace_report(
        report,
        sections=tuple(
            section.model_copy(update={"claims": replacements[section.key]})
            if section.key in replacements
            else section
            for section in report.sections
        ),
    )

    with _client(_ReportPort(report)) as client:
        response = client.get(
            f"/replays/{REPLAY_ID}/reports/{REPORT_ID}",
            headers={"host": "localhost", "accept": "text/html"},
        )

    assert response.status_code == 200
    assert response.text.index('id="key-moments"') < response.text.index('id="report-timeline"')
    assert "War Factory first observed" in response.text
    assert "Frame 150" in response.text
    assert "Review what changed after this information became visible." in response.text
    assert "Humvee over War Factory" in response.text
    assert "War Factory destroyed" not in response.text
    assert f'/evidence/observed/{EVIDENCE_ID}?report_id={REPORT_ID}' in response.text
    assert (
        f'/replays/{REPLAY_ID}/reports/{REPORT_ID}/map?frame_start=0&amp;frame_end=600'
        in response.text
    )
    assert "Open on battlefield" in response.text


class _ReportPort:
    def __init__(self, report: ReplayReportDTO) -> None:
        self.report = report
        self.report_queries: list[FixedReportQueryDTO] = []
        self.timeline_queries: list[TimelineChartQueryDTO] = []

    def resolve_latest(self, query: LatestReportQueryDTO) -> ReportResolutionDTO:
        return ReportResolutionDTO(
            state="available",
            fixed_report=self.report.fixed_report,
            version=self.report.version,
        )

    def get_report(self, query: FixedReportQueryDTO) -> ReplayReportDTO:
        self.report_queries.append(query)
        return self.report

    def timeline_chart(self, query: TimelineChartQueryDTO) -> TimelineChartDTO:
        self.timeline_queries.append(query)
        return TimelineChartDTO(
            schema_version="web-report-timeline-v1",
            query=query,
            availability=AvailabilityDTO(state="unavailable", reason_codes=("timeline_fixture_unavailable",)),
            timebase_fps=30,
            available_players=(),
            available_families=(),
            series=(),
        )

    def get_evidence(self, query: object) -> object:
        raise AssertionError(f"unexpected evidence query: {query!r}")


class _Factory:
    def __init__(self, port: _ReportPort) -> None:
        self.port = port

    @contextmanager
    def __call__(self) -> Iterator[_ReportPort]:
        yield self.port


def _client(port: _ReportPort) -> TestClient:
    assert isinstance(port, ReportQueryPort)
    return TestClient(create_app(object(), port_factory=_Factory(port), bootstrapper=RecordingBootstrapper()))  # type: ignore[arg-type]


def test_report_route_returns_honest_service_unavailable_without_report_port() -> None:
    """Catch runtime wiring gaps becoming fabricated preview data or an unhandled exception."""
    app = create_app(
        object(),
        port_factory=_Factory(object()),  # type: ignore[arg-type]
        bootstrapper=RecordingBootstrapper(),
    )
    with TestClient(app) as client:
        response = client.get(
            f"/replays/{REPLAY_ID}/reports/{REPORT_ID}",
            headers={"host": "localhost", "accept": "text/html"},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "report_adapter_pending"


def test_latest_report_route_resolves_once_and_redirects_to_fixed_version() -> None:
    """Catch a latest link rendering mutable content instead of freezing the report ID in the URL."""
    port = _ReportPort(_report())
    with _client(port) as client:
        response = client.get(
            f"/replays/{REPLAY_ID}?replay_player_id={PLAYER_ID}",
            headers={"host": "localhost"},
            follow_redirects=False,
        )

    assert response.status_code == 307
    assert response.headers["location"] == f"/replays/{REPLAY_ID}/reports/{REPORT_ID}"


class _ResolutionPort(_ReportPort):
    def __init__(self, resolution: ReportResolutionDTO) -> None:
        super().__init__(_report())
        self.resolution = resolution

    def resolve_latest(self, query: LatestReportQueryDTO) -> ReportResolutionDTO:
        return self.resolution


@pytest.mark.parametrize(
    "resolution",
    [
        ReportResolutionDTO(state="not_generated", reason_code="report_not_generated"),
        ReportResolutionDTO(
            state="pipeline_active",
            reason_code="report_pipeline_running",
            pipeline=PipelineStateDTO(
                stage="render_report",
                state="running",
                attempt=1,
                replay_public_id=REPLAY_ID,
            ),
        ),
        ReportResolutionDTO(
            state="pipeline_failed",
            reason_code="report_pipeline_failed",
            pipeline=PipelineStateDTO(
                stage="render_report",
                state="failed",
                attempt=2,
                replay_public_id=REPLAY_ID,
            ),
        ),
    ],
)
def test_latest_report_route_renders_each_honest_unavailable_resolution(
    resolution: ReportResolutionDTO,
) -> None:
    """Catch unresolved latest navigation inventing a report or hiding pipeline state."""
    with _client(_ResolutionPort(resolution)) as client:
        response = client.get(f"/replays/{REPLAY_ID}", headers={"host": "localhost", "accept": "text/html"})

    assert response.status_code == 200
    assert "Replay report unavailable" in response.text
    assert resolution.reason_code in response.text
    if resolution.pipeline is not None:
        assert resolution.pipeline.stage in response.text
        assert resolution.pipeline.state in response.text


@pytest.mark.parametrize(
    "resolution",
    [
        ReportResolutionDTO(
            state="available",
            fixed_report=FixedReportQueryDTO(
                replay_public_id="123e4567-e89b-42d3-a456-426614174198",
                report_public_id=REPORT_ID,
            ),
            version=ReportVersionDTO(
                report_public_id=REPORT_ID,
                report_version="replay-report-v1",
                replay_player_public_id=PLAYER_ID,
            ),
        ),
        ReportResolutionDTO(
            state="available",
            fixed_report=FixedReportQueryDTO(replay_public_id=REPLAY_ID, report_public_id=REPORT_ID),
            version=ReportVersionDTO(
                report_public_id=REPORT_ID,
                report_version="replay-report-v1",
                replay_player_public_id=None,
            ),
        ),
    ],
)
def test_latest_report_route_rejects_cross_scope_port_resolution(resolution: ReportResolutionDTO) -> None:
    """Catch a faulty port redirecting latest navigation across replay or player scope."""
    with _client(_ResolutionPort(resolution)) as client:
        response = client.get(
            f"/replays/{REPLAY_ID}?replay_player_id={PLAYER_ID}",
            headers={"host": "localhost", "accept": "text/html"},
            follow_redirects=False,
        )

    assert response.status_code == 409
    assert response.json()["code"] == "report_identity_mismatch"


def test_fixed_report_route_keeps_all_sections_inside_collapsed_provenance_and_uses_exact_query() -> None:
    """Catch fixed navigation dropping raw sections or exposing them before player analysis."""
    port = _ReportPort(_report())
    with _client(port) as client:
        response = client.get(
            f"/replays/{REPLAY_ID}/reports/{REPORT_ID}",
            headers={"host": "localhost", "accept": "text/html"},
        )

    assert response.status_code == 200
    assert port.report_queries == [FixedReportQueryDTO(replay_public_id=REPLAY_ID, report_public_id=REPORT_ID)]
    assert port.timeline_queries == [
        TimelineChartQueryDTO(
            replay_public_id=REPLAY_ID,
            report_public_id=REPORT_ID,
            families=("strategy",),
        )
    ]
    assert response.text.index("What happened") < response.text.index("Technical evidence and provenance")
    for key in SECTION_KEYS:
        assert f'id="section-{key}"' in response.text
    assert 'src="/static/vendor/echarts.min.js"' in response.text
    assert 'src="/static/js/report.js"' in response.text


def test_fixed_report_bounds_inline_evidence_links_and_reports_the_full_count() -> None:
    """Catch high-cardinality derived evidence expanding one report page into tens of megabytes."""
    evidence = tuple(
        ReportEvidenceReferenceDTO(
            public_id=f"123e4567-e89b-42d3-a456-42661417{index:04d}",
            tier="observed",
        )
        for index in range(7)
    )
    report = _report()
    first = report.sections[0]
    sections = (
        first.model_copy(
            update={
                "claims": (
                    first.claims[0].model_copy(update={"evidence": evidence}),
                )
            }
        ),
        *report.sections[1:],
    )

    with _client(_ReportPort(_replace_report(report, sections=sections))) as client:
        response = client.get(
            f"/replays/{REPLAY_ID}/reports/{REPORT_ID}",
            headers={"host": "localhost", "accept": "text/html"},
        )

    assert response.status_code == 200
    assert 'data-evidence-public-id="123e4567-e89b-42d3-a456-426614170004"' in response.text
    assert 'data-evidence-public-id="123e4567-e89b-42d3-a456-426614170005"' not in response.text
    assert "Showing 5 of 7 direct evidence references" in response.text


def test_fixed_report_route_fails_closed_on_cross_report_port_data() -> None:
    """Catch a faulty adapter binding another immutable report to the requested URL."""
    other_report_id = "123e4567-e89b-42d3-a456-426614174199"
    report = _replace_report(
        _report(),
        fixed_report={"replay_public_id": REPLAY_ID, "report_public_id": other_report_id},
        version={
            "report_public_id": other_report_id,
            "report_version": "replay-report-v1",
            "replay_player_public_id": PLAYER_ID,
        },
    )
    with _client(_ReportPort(report)) as client:
        response = client.get(
            f"/replays/{REPLAY_ID}/reports/{REPORT_ID}",
            headers={"host": "localhost", "accept": "text/html"},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "report_identity_mismatch"


class _CoordinatedCrossScopePort(_ReportPort):
    def timeline_chart(self, _query: TimelineChartQueryDTO) -> TimelineChartDTO:
        return TimelineChartDTO(
            schema_version="web-report-timeline-v1",
            query=TimelineChartQueryDTO(
                replay_public_id=self.report.fixed_report.replay_public_id,
                report_public_id=self.report.fixed_report.report_public_id,
            ),
            availability=AvailabilityDTO(state="unavailable", reason_codes=("timeline_unavailable",)),
            timebase_fps=30,
            available_players=(),
            available_families=(),
            series=(),
        )


def test_fixed_report_route_rejects_coordinated_cross_scope_port_data() -> None:
    """Catch report and timeline values colluding on an identity different from the URL."""
    other_replay_id = "123e4567-e89b-42d3-a456-426614174198"
    other_report_id = "123e4567-e89b-42d3-a456-426614174199"
    report = _replace_report(
        _report(),
        fixed_report={"replay_public_id": other_replay_id, "report_public_id": other_report_id},
        version={
            "report_public_id": other_report_id,
            "report_version": "replay-report-v1",
            "replay_player_public_id": PLAYER_ID,
        },
    )
    with _client(_CoordinatedCrossScopePort(report)) as client:
        response = client.get(
            f"/replays/{REPLAY_ID}/reports/{REPORT_ID}",
            headers={"host": "localhost", "accept": "text/html"},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "report_identity_mismatch"


def test_deterministic_only_report_labels_ollama_as_not_requested() -> None:
    """Catch a valid deterministic report being mistaken for failed model analysis."""
    report = _report()
    sections = tuple(
        _unavailable_section(section.key, "ollama_not_requested") if section.key == "llm_interpretation" else section
        for section in report.sections
    )
    report = _replace_report(
        report,
        source_mode="deterministic_only",
        sections=sections,
        ollama=OllamaReportStatusDTO(requested=False, status="not_requested").model_dump(),
        warnings=(),
    )

    with _client(_ReportPort(report)) as client:
        response = client.get(
            f"/replays/{REPLAY_ID}/reports/{REPORT_ID}",
            headers={"host": "localhost", "accept": "text/html"},
        )

    assert response.status_code == 200
    assert "Deterministic only" in response.text
    assert "Ollama was not requested" in response.text
    assert "ollama_not_requested" in response.text


def test_telemetry_missing_report_keeps_unavailable_sections_explicit() -> None:
    """Catch absent telemetry being rendered as zero values or blank analysis cards."""
    report = _report()
    telemetry_sections = {"economy", "production_composition", "combat_engagements", "activity"}
    sections = tuple(
        _unavailable_section(section.key, "telemetry_missing") if section.key in telemetry_sections else section
        for section in report.sections
    )
    report = _replace_report(
        report,
        lifecycle={
            "lifecycle_state": "parser_complete",
            "parser_completion_status": "complete",
            "telemetry_status": "missing",
            "telemetry_runner_status": None,
        },
        sections=sections,
        warnings=("Telemetry was not captured; telemetry-backed sections remain unavailable.",),
    )

    with _client(_ReportPort(report)) as client:
        response = client.get(
            f"/replays/{REPLAY_ID}/reports/{REPORT_ID}",
            headers={"host": "localhost", "accept": "text/html"},
        )

    assert response.status_code == 200
    assert "Telemetry: missing" in response.text
    assert "Telemetry was not captured" in response.text
    assert response.text.count("telemetry_missing") == len(telemetry_sections)


def test_ollama_failure_is_diagnostic_and_does_not_replace_deterministic_sections() -> None:
    """Catch optional prose failure hiding or invalidating deterministic report findings."""
    report = _report()
    sections = tuple(
        _unavailable_section(section.key, "ollama_generation_failed")
        if section.key == "llm_interpretation"
        else section
        for section in report.sections
    )
    report = _replace_report(
        report,
        source_mode="deterministic_only",
        sections=sections,
        ollama={"requested": True, "status": "failed", "diagnostic_codes": ("ollama_timeout",)},
        warnings=("Optional Ollama interpretation failed; deterministic analysis remains valid.",),
    )

    with _client(_ReportPort(report)) as client:
        response = client.get(
            f"/replays/{REPLAY_ID}/reports/{REPORT_ID}",
            headers={"host": "localhost", "accept": "text/html"},
        )

    assert response.status_code == 200
    assert "Ollama failed" in response.text
    assert "ollama_timeout" in response.text
    assert "Overview fixture" in response.text
    assert "ollama_generation_failed" in response.text


@pytest.mark.parametrize(
    ("lifecycle", "issue_code", "issue_message"),
    [
        ("truncated", "replay_truncated", "The replay ended before a complete parser terminator."),
        ("desynced", "crc_mismatch", "Engine verification detected a CRC mismatch."),
    ],
)
def test_terminal_quality_failures_are_prominent_before_analysis(
    lifecycle: str,
    issue_code: str,
    issue_message: str,
) -> None:
    """Catch terminal replay defects being buried below metrics or model prose."""
    report = _report()
    parser_status = "truncated" if lifecycle == "truncated" else "complete"
    report = _replace_report(
        report,
        lifecycle={
            **report.lifecycle.model_dump(),
            "lifecycle_state": lifecycle,
            "parser_completion_status": parser_status,
        },
        terminal_quality=TerminalQualityDTO(
            lifecycle=lifecycle,
            issues=(QualityIssueDTO(code=issue_code, message=issue_message),),
        ).model_dump(),
    )

    with _client(_ReportPort(report)) as client:
        response = client.get(
            f"/replays/{REPLAY_ID}/reports/{REPORT_ID}",
            headers={"host": "localhost", "accept": "text/html"},
        )

    assert response.status_code == 200
    assert response.text.index(issue_message) < response.text.index("Technical evidence and provenance")
    assert response.text.index(issue_code) > response.text.index("Technical evidence and provenance")
    assert issue_message in response.text
    assert "Opening-only analysis:" in response.text
    assert "Recorded command markers can continue beyond this boundary" in response.text
    assert "Recorded result:" not in response.text
    assert "<em>won</em>" not in response.text
