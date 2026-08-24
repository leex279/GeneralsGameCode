"""Contract tests for the package-owned dashboard shell."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.ports import AvailabilityDTO, DashboardDTO, PipelineStateDTO
from generals_replay_analyzer.web.presentation.shell import (
    NavigationItemDTO,
    ShellContextDTO,
    dashboard_shell,
    identity_shell,
)

from .conftest import CountingPortFactory, FakeWebApplicationPort, RecordingBootstrapper


def _client() -> TestClient:
    return TestClient(create_app(object(), port_factory=CountingPortFactory(), bootstrapper=RecordingBootstrapper()))


def test_fake_snapshots_render_package_shell_for_each_reserved_page() -> None:
    with _client() as client:
        dashboard = client.get("/", headers={"host": "localhost"})
        players = client.get("/players", headers={"host": "localhost"})

    assert dashboard.status_code == players.status_code == 200
    assert "<h1>Replay dashboard</h1>" in dashboard.text
    assert "<h1>Player Evidence</h1>" in players.text
    for response, active in ((dashboard, 'href="/" aria-current="page"'), (players, 'href="/players" aria-current="page"')):
        assert 'href="/">Dashboard</a>' in response.text
        assert 'href="/players">Players</a>' in response.text
        assert 'href="/scouting">Scouting</a>' in response.text
        assert active in response.text
        assert 'href="/maps">Maps</a>' in response.text
        assert 'href="/compare">Compare</a>' in response.text
        assert 'href="/settings">Settings</a>' in response.text
        assert "Upcoming areas" not in response.text


def test_shell_mappers_keep_availability_pipeline_and_quality_separate() -> None:
    snapshot = DashboardDTO(
        generated_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        availability=AvailabilityDTO(state="partial", reason_codes=("terminal_evidence_pending",)),
        pipeline_states=(
            PipelineStateDTO(stage="report", state="queued", attempt=2),
            PipelineStateDTO(stage="features", state="running", attempt=1, progress=0.5),
        ),
    )

    shell = dashboard_shell(snapshot)
    identity = identity_shell(FakeWebApplicationPort().identity_landing())

    assert shell.availability is snapshot.availability
    assert shell.pipeline is snapshot.pipeline_states[1]
    assert shell.terminal_quality is None
    assert shell.availability.reason_codes == ("terminal_evidence_pending",)
    assert identity.pipeline is None
    assert identity.terminal_quality is None


def test_unavailable_snapshot_is_never_presented_as_a_completed_report_or_zero_metric() -> None:
    shell = dashboard_shell(FakeWebApplicationPort().dashboard())

    assert shell.availability.state == "unavailable"
    assert shell.availability.reason_codes == ("analytics_adapter_pending",)
    assert shell.pipeline is None


def test_shell_view_dtos_reject_private_filesystem_locators() -> None:
    with pytest.raises(ValidationError, match="filesystem paths"):
        NavigationItemDTO(label=r"C:\private", href="/", active=True, availability="available")
    with pytest.raises(ValidationError, match="filesystem paths"):
        ShellContextDTO(
            page_title="file:///srv/private/report",
            current_path="/",
            navigation=(),
            pipeline=None,
            availability=AvailabilityDTO(state="unavailable"),
            terminal_quality=None,
        )
    with pytest.raises(ValidationError, match="filesystem paths"):
        ShellContextDTO(
            page_title="Replay dashboard",
            current_path="/",
            navigation=(),
            pipeline=None,
            availability=AvailabilityDTO(state="unavailable"),
            terminal_quality=None,
            correlation_id="/srv/private/correlation",
        )


@pytest.mark.parametrize("accept", ["application/json", "application/xml", "text/html;q=0", "*/*;q=0"])
def test_reserved_pages_reject_non_html_acceptance_with_controlled_problem(accept: str) -> None:
    with _client() as client:
        response = client.get("/", headers={"host": "localhost", "accept": accept})

    assert response.status_code == 406
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json()["code"] == "not_acceptable"


@pytest.mark.parametrize("accept", ["text/html", "text/html;q=0.2", "*/*"])
def test_reserved_pages_render_html_for_an_accepted_representation(accept: str) -> None:
    with _client() as client:
        response = client.get("/players", headers={"host": "localhost", "accept": accept})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize(
    "accept",
    [
        "text/*",
        "TEXT/HTML ; Q=1",
        "text/html;q=0, text/*;q=1",
        "text/html;q=0, */*;q=1",
        "*/*;q=1, text/html;q=0",
        "text/html;q=1, text/html;q=0",
        "text/html;q=0.5, text/html;q=0.5",
        "text/html;q=bogus",
        "text/html;q=NaN",
        "text/html;q=Infinity",
        "text/html;q=2",
        "text/html;q=-0.1",
        "text/html;q=0.0001",
        "text/html;q=1.001",
        "text/html;q=1.0000",
        "text/html;q",
        "text/html;Q",
        "text/*;q",
        "*/*;q",
        "text/html;q=0.5;q",
        "text/html;q=0.5;q=0.5",
        "text/html;charset=utf-8",
        "text/html;level=1",
        "text/*;ext=enabled",
        "*/*;foo=bar",
        "text/html;q=0.5;charset=utf-8",
        "text/html;charset=utf-8;q=0.5",
        "text/html;foo",
        "text/html;foo=",
        "application/json, application/xml",
    ],
)
def test_accept_parser_applies_closed_rfc_subset_and_specific_precedence(accept: str) -> None:
    with _client() as client:
        response = client.get("/", headers={"host": "localhost", "accept": accept})

    should_allow = accept in {"text/*", "TEXT/HTML ; Q=1", "text/html;q=0.5, text/html;q=0.5"}
    assert response.status_code == (200 if should_allow else 406)
