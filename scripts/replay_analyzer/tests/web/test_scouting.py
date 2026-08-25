"""Strategy-first opponent scouting workspace tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    DerivedLongitudinalEvidenceDTO,
    EvidenceDetailDTO,
    EvidenceQueryDTO,
)
from generals_replay_analyzer.web.routes.scouting import router

from .conftest import CountingPortFactory, RecordingBootstrapper
from .test_player_profile_json import PLAYER_ID
from .test_players import EVIDENCE_ID, REPORT_ID, _full_profile, _PlayerPort


def _empty_client() -> TestClient:
    return TestClient(
        create_app(
            object(),
            port_factory=CountingPortFactory(),
            bootstrapper=RecordingBootstrapper(),
        )
    )


def test_scouting_workspace_has_a_first_class_empty_state() -> None:
    """Catch a missing product route or an empty database rendering as a technical failure."""
    with _empty_client() as client:
        response = client.get("/scouting", headers={"host": "localhost", "accept": "text/html"})

    assert response.status_code == 200
    assert 'href="/scouting" aria-current="page"' in response.text
    assert "Opponent scouting" in response.text
    assert "Analyze more replays to build an opponent game plan" in response.text
    assert "No recurring strategy is claimed yet" in response.text


def test_scouting_empty_state_offers_a_direct_import_next_step() -> None:
    """Empty scouting should lead directly to the action that creates evidence."""
    with _empty_client() as client:
        response = client.get("/scouting", headers={"host": "localhost", "accept": "text/html"})

    assert response.status_code == 200
    assert 'href="/imports/dialog"' in response.text
    assert ">Import replay<" in response.text


def _profile_client(port: object) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[application_port] = lambda: port
    return TestClient(app)


def test_selected_opponent_leads_with_recurring_opening_threat_and_counter_plan() -> None:
    """Catch the scouting page falling back to raw analytics tables instead of an actionable plan."""
    profile = _full_profile()
    second_report = profile.version.fixed_reports[0].model_copy(
        update={"report_public_id": "123e4567-e89b-42d3-a456-426614174349"}
    )
    profile = profile.model_copy(
        update={
            "query": profile.query.model_copy(
                update={"report_public_ids": (REPORT_ID, second_report.report_public_id)}
            ),
            "version": profile.version.model_copy(
                update={"fixed_reports": (*profile.version.fixed_reports, second_report)}
            ),
        }
    )
    opening = profile.insights[0].model_copy(
        update={
            "raw_value": 0.70,
            "unit": None,
            "interval": None,
        }
    )
    pressure_timing = profile.insights[1].model_copy(
        update={
            "label": "First pressure timing",
            "raw_value": 450.0,
            "unit": "frames",
            "interval": profile.insights[1].interval.model_copy(update={"lower": 390.0, "upper": 540.0}),
        }
    )
    class ScoutingPort(_PlayerPort):
        def __init__(self) -> None:
            super().__init__(profile.model_copy(update={"insights": (opening, pressure_timing, *profile.insights[2:])}))
            self.evidence_queries: list[EvidenceQueryDTO] = []

        def get_evidence(self, query: EvidenceQueryDTO) -> EvidenceDetailDTO:
            self.evidence_queries.append(query)
            return EvidenceDetailDTO(
                schema_version="web-evidence-inspector-v1",
                query=query,
                replay_public_id=self.profile.replay_history[0].replay_public_id,
                source_kind="longitudinal_corpus",
                source_schema_version=1,
                source=DerivedLongitudinalEvidenceDTO(
                    kind="longitudinal_result",
                    result_public_id=opening.result_public_id,
                    longitudinal_run_id="123e4567-e89b-42d3-a456-426614174390",
                    analyzer_name="Generals Replay Analyzer",
                    analyzer_version="1.0",
                    result_name="recurring_opening",
                    result_kind="pattern",
                    sample_count=10,
                    missing_count=1,
                    availability="available",
                    statistics={
                        "recurring_prefix": [
                            "ChinaPowerPlant",
                            "ChinaBarracks",
                            "ChinaSupplyCenter",
                        ],
                        "share": 0.70,
                        "wilson_interval": [0.52, 0.82],
                        "confidence_level": 0.95,
                    },
                    members=(),
                ),
            )

    port = ScoutingPort()

    with _profile_client(port) as client:
        response = client.get(
            f"/scouting?player={PLAYER_ID}",
            headers={"accept": "text/html"},
        )

    assert response.status_code == 200
    assert port.selections[0].player_public_id == PLAYER_ID
    assert port.profile_queries == [port.profile.query]
    assert port.evidence_queries == [EvidenceQueryDTO(report_public_id=REPORT_ID, evidence_public_id=EVIDENCE_ID, expected_tier="derived")]
    assert "Opponent game plan" in response.text
    assert "Power Plant" in response.text and "Barracks" in response.text and "Supply Center" in response.text
    assert "70%" in response.text
    assert "10 accepted matches" in response.text
    assert "95% confidence" in response.text
    assert "Primary timing 0:15" in response.text
    assert "Threat" in response.text and "Counter-plan" in response.text
    assert "Confirm the opening" in response.text and "Prepare before the timing" in response.text
    assert f'/evidence/derived/{EVIDENCE_ID}?report_id={REPORT_ID}' in response.text


def test_unavailable_recurring_opening_does_not_probe_evidence_across_reports() -> None:
    """Unavailable insights must not enter the bounded evidence lookup loop."""
    profile = _full_profile()
    second_report = profile.version.fixed_reports[0].model_copy(
        update={"report_public_id": "123e4567-e89b-42d3-a456-426614174349"}
    )
    unavailable_opening = profile.insights[0].model_copy(
        update={
            "raw_value": None,
            "sample_count": 0,
            "availability": AvailabilityDTO(state="unavailable", reason_codes=("insufficient_samples",)),
        }
    )
    profile = profile.model_copy(
        update={
            "insights": (unavailable_opening, *profile.insights[1:]),
            "query": profile.query.model_copy(update={"report_public_ids": (REPORT_ID, second_report.report_public_id)}),
            "version": profile.version.model_copy(update={"fixed_reports": (*profile.version.fixed_reports, second_report)}),
        }
    )

    class ScoutingPort(_PlayerPort):
        def __init__(self) -> None:
            super().__init__(profile)
            self.evidence_queries: list[EvidenceQueryDTO] = []

        def get_evidence(self, query: EvidenceQueryDTO) -> EvidenceDetailDTO:
            self.evidence_queries.append(query)
            raise AssertionError("unavailable recurring opening must not request evidence")

    port = ScoutingPort()
    with _profile_client(port) as client:
        response = client.get(f"/scouting?player={PLAYER_ID}", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert port.evidence_queries == []


def test_scouting_query_rejects_unknown_or_noncanonical_player_ids() -> None:
    port = _PlayerPort()
    with _profile_client(port) as client:
        unknown = client.get("/scouting?sql=select", headers={"accept": "text/html"})
        invalid = client.get("/scouting?player=not-a-public-id", headers={"accept": "text/html"})

    assert unknown.status_code == invalid.status_code == 422
    assert port.selections == [] and port.profile_queries == []
