"""Application-factory and immutable route-boundary contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from generals_replay_analyzer.web import ports as web_ports
from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    DashboardDTO,
    DiagnosticDTO,
    PipelineStateDTO,
    QualityIssueDTO,
    TerminalQualityDTO,
)

from .conftest import CountingPortFactory, RecordingBootstrapper


def _app(port_factory: CountingPortFactory, bootstrapper: RecordingBootstrapper) -> Any:
    return create_app(object(), port_factory=port_factory, bootstrapper=bootstrapper)


def test_lifespan_prepares_once_and_liveness_never_opens_application_scope(
    port_factory: CountingPortFactory,
    bootstrapper: RecordingBootstrapper,
) -> None:
    app = _app(port_factory, bootstrapper)

    with TestClient(app) as client:
        response = client.get("/health/live", headers={"host": "localhost"})

    assert response.status_code == 200
    assert response.json() == {"status": "live"}
    assert len(bootstrapper.settings) == 1
    assert port_factory.created == port_factory.closed == 0


def test_readiness_uses_one_closed_request_scope_and_returns_accepted_schema(
    port_factory: CountingPortFactory,
    bootstrapper: RecordingBootstrapper,
) -> None:
    with TestClient(_app(port_factory, bootstrapper)) as client:
        response = client.get("/health/ready", headers={"host": "127.0.0.1"})

    assert response.status_code == 200
    assert response.json() == {"ready": True, "schema_revision": "accepted-head", "diagnostics": []}
    assert port_factory.created == port_factory.closed == 1


def test_unready_dependency_returns_sanitized_problem_with_stable_diagnostics(
    bootstrapper: RecordingBootstrapper,
) -> None:
    port_factory = CountingPortFactory(ready=False)

    with TestClient(_app(port_factory, bootstrapper)) as client:
        response = client.get("/health/ready", headers={"host": "[::1]"})

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "about:blank",
        "title": "Service Unavailable",
        "status": 503,
        "code": "not_ready",
        "detail": "Replay Analyzer is not ready",
        "diagnostics": [{"code": "schema_unavailable", "message": "Schema unavailable"}],
    }
    assert port_factory.created == port_factory.closed == 1


@pytest.mark.parametrize(
    ("path", "expected_heading", "expected_reason"),
    [
        ("/", "Replay dashboard", "analytics_adapter_pending"),
        ("/players", "Player Evidence", "player_history_adapter_pending"),
    ],
)
def test_reserved_routes_render_fake_port_snapshots_and_close_each_scope(
    path: str,
    expected_heading: str,
    expected_reason: str,
    port_factory: CountingPortFactory,
    bootstrapper: RecordingBootstrapper,
) -> None:
    with TestClient(_app(port_factory, bootstrapper)) as client:
        response = client.get(path, headers={"host": "localhost"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert f"<h1>{expected_heading}</h1>" in response.text
    assert "unavailable" in response.text
    assert expected_reason in response.text
    assert port_factory.created == port_factory.closed == 1


def test_public_dtos_are_frozen_forbid_extra_and_keep_states_distinct() -> None:
    availability = AvailabilityDTO(state="partial", reason_codes=("missing_terminal_evidence",))
    snapshot = DashboardDTO(
        generated_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        availability=availability,
        pipeline_states=(
            PipelineStateDTO(
                stage="features",
                state="running",
                attempt=2,
                progress=0.5,
                job_public_id="123e4567-e89b-42d3-a456-426614174000",
            ),
        ),
    )

    assert snapshot.availability.state == "partial"
    assert snapshot.pipeline_states[0].state == "running"
    with pytest.raises(ValidationError):
        AvailabilityDTO.model_validate({"state": "available", "status": "complete"})
    with pytest.raises(ValidationError):
        DashboardDTO(
            generated_at=datetime(2026, 8, 21, 12, 0),  # noqa: DTZ001 - deliberately naive invalid input
            availability=availability,
        )
    with pytest.raises(ValidationError):
        snapshot.availability = AvailabilityDTO(state="available")  # type: ignore[misc]


def test_reserved_pages_publish_distinct_semantic_names_and_non_color_availability_vocabulary(
    port_factory: CountingPortFactory,
    bootstrapper: RecordingBootstrapper,
) -> None:
    schema = _app(port_factory, bootstrapper).openapi()

    assert schema["paths"]["/"]["get"]["summary"] == "Dashboard"
    assert schema["paths"]["/players"]["get"]["summary"] == "Player directory"


def test_public_dtos_reject_absolute_paths_internal_ids_and_noncanonical_public_ids() -> None:
    with pytest.raises(ValidationError, match="absolute filesystem paths"):
        DiagnosticDTO(code="database_unavailable", message=r"Unavailable at C:\private\library.sqlite3")
    with pytest.raises(ValidationError):
        AvailabilityDTO.model_validate({"state": "available", "id": 42})
    with pytest.raises(ValidationError, match="lowercase hyphenated UUID"):
        PipelineStateDTO(
            stage="features",
            state="running",
            attempt=1,
            job_public_id="123E4567-E89B-42D3-A456-426614174000",
        )


@pytest.mark.parametrize(
    "message",
    [
        "Unavailable at '/srv/replays/library.sqlite3'.",
        "Database path:/srv/replays/library.sqlite3 is unavailable.",
        "Database path=[/srv/replays/library.sqlite3], retry later.",
        "Database path=//srv/share/library.sqlite3 is unavailable.",
        "Database path,//srv/share/library.sqlite3 is unavailable.",
        r"Unavailable at (C:\private\library.sqlite3), retry later.",
        r"Unavailable at \\server\share\library.sqlite3.",
        r"Unavailable at \\?\C:\private\library.sqlite3.",
        "Unavailable at ../private/library.sqlite3.",
        r"Unavailable at ..\private\library.sqlite3.",
    ],
)
def test_public_dtos_reject_punctuation_adjacent_and_traversal_paths(message: str) -> None:
    with pytest.raises(ValidationError, match="filesystem paths"):
        DiagnosticDTO(code="database_unavailable", message=message)


@pytest.mark.parametrize(
    "message",
    [
        "Traversal docs/../private/library.sqlite3 is unavailable.",
        r"Traversal docs\..\private\library.sqlite3 is unavailable.",
        "Traversal (docs/../private/library.sqlite3), retry later.",
        r"Traversal [docs\..\private\library.sqlite3], retry later.",
        "Database file:///srv/private/library.sqlite3 is unavailable.",
        r"Database sqlite:///C:\private\library.sqlite3 is unavailable.",
        "endpoint=http://operator:secret-token@127.0.0.1:11434",
        "endpoint=http://127.0.0.1:11434?token=secret-token",
        "endpoint=https://localhost:8765/#secret-token",
        "endpoint=http://127.0.0.1:11434//srv/private/library.sqlite3",
    ],
)
def test_public_dtos_reject_embedded_traversal_and_unsafe_uri_tokens(message: str) -> None:
    with pytest.raises(ValidationError, match="filesystem paths"):
        DiagnosticDTO(code="database_unavailable", message=message)


@pytest.mark.parametrize("component", ["?database={locator}", "#database={locator}"])
@pytest.mark.parametrize(
    "locator",
    [
        "/srv/private/library.sqlite3",
        r"C:\private\library.sqlite3",
        "%2Fsrv%2Fprivate%2Flibrary.sqlite3",
        "%252Fsrv%252Fprivate%252Flibrary.sqlite3",
        r"C:%5Cprivate%5Clibrary.sqlite3",
        r"C:%255Cprivate%255Clibrary.sqlite3",
        "%2F%2Fserver%2Fshare%2Flibrary.sqlite3",
        "%252F%252Fserver%252Fshare%252Flibrary.sqlite3",
        "%5C%5Cserver%5Cshare%5Clibrary.sqlite3",
        "%255C%255Cserver%255Cshare%255Clibrary.sqlite3",
        "%5C%5C%3F%5CC:%5Cprivate%5Clibrary.sqlite3",
        "%255C%255C%253F%255CC:%255Cprivate%255Clibrary.sqlite3",
        "docs%2F..%2Fprivate%2Flibrary.sqlite3",
        "docs%252F..%252Fprivate%252Flibrary.sqlite3",
        "docs%5C..%5Cprivate%5Clibrary.sqlite3",
        "docs%255C..%255Cprivate%255Clibrary.sqlite3",
        "%2525252Fsrv%2525252Fprivate%2525252Flibrary.sqlite3",
        "%2Gsrv%2Fprivate%2Flibrary.sqlite3",
    ],
)
def test_public_dtos_reject_local_locators_in_every_uri_data_component(component: str, locator: str) -> None:
    message = f"endpoint=https://localhost:8765/{component.format(locator=locator)}"

    with pytest.raises(ValidationError, match="filesystem paths"):
        DiagnosticDTO(code="database_unavailable", message=message)


@pytest.mark.parametrize(
    "component",
    [
        "?%2Fsrv%2Fprivate=value",
        r"?C:%5Cprivate=value",
        "#%252Fprivate=value",
        r"#C:%255Cprivate=value",
    ],
)
def test_public_dtos_reject_local_locators_in_uri_keys(component: str) -> None:
    message = f"endpoint=https://localhost:8765/{component}"

    with pytest.raises(ValidationError, match="filesystem paths"):
        DiagnosticDTO(code="database_unavailable", message=message)


@pytest.mark.parametrize(
    "message",
    [
        "Ordinary prose may mention a 1/2 ratio.",
        "Ordinary prose: use / as the separator.",
        "The evidence group is analysis/features and drive C: is named.",
        "The local capability endpoint is http://127.0.0.1:11434.",
        "endpoint=http://127.0.0.1:11434",
        "endpoint=https://localhost:8765",
        "endpoint=https://localhost:8765/?format=json#status=ready",
        "endpoint=https://localhost:8765/?ratio=1/2",
        "endpoint=https://localhost:8765/?note=use%20%2F%20as%20the%20separator",
        "endpoint=https://localhost:8765/#database=available",
        "The local capability endpoint is (http://[::1]:11434).",
    ],
)
def test_public_dto_path_guard_does_not_reject_ordinary_prose(message: str) -> None:
    assert DiagnosticDTO(code="safe_message", message=message).message == message


def test_public_dto_accepts_structured_web_route_and_percent_literal_metadata() -> None:
    message = "endpoint=https://localhost:8765/health?progress=100%25#status=ready"

    assert DiagnosticDTO(code="safe_message", message=message).message == message


@pytest.mark.parametrize(
    ("message", "accepted"),
    [
        ("endpoint=https://localhost:8765/health?format=json#status=ready", True),
        ("endpoint=https://localhost:8765/?progress=100%25#status=ready", True),
        ("endpoint=https://localhost:8765/srv/private/library.sqlite3", False),
        ("endpoint=https://localhost:8765/health/../private", False),
        ("endpoint=https://localhost:8765/health/%2E%2E/private", False),
        (r"endpoint=https://localhost:8765/C:%5Cprivate%5Clibrary.sqlite3", False),
        ("endpoint=https://operator%40localhost:8765/health", False),
        ("endpoint=https://localhost:8765/?database=%252Gprivate", False),
    ],
)
def test_http_endpoint_classifier_distinguishes_routes_from_local_locators(message: str, accepted: bool) -> None:
    if accepted:
        assert DiagnosticDTO(code="safe_message", message=message).message == message
        return
    with pytest.raises(ValidationError, match="filesystem paths"):
        DiagnosticDTO(code="database_unavailable", message=message)


def test_diagnostic_message_length_cap_accepts_boundary_and_rejects_over_limit() -> None:
    limit = web_ports.MAX_DIAGNOSTIC_MESSAGE_LENGTH

    assert DiagnosticDTO(code="safe_message", message="x" * limit).message == "x" * limit
    with pytest.raises(ValidationError, match="at most"):
        DiagnosticDTO(code="safe_message", message="x" * (limit + 1))


def test_public_candidate_scanner_has_linear_bound_for_near_limit_boundary_input() -> None:
    unit = "endpoint=https://localhost:8765/health,"
    repeats = web_ports.MAX_DIAGNOSTIC_MESSAGE_LENGTH // len(unit)
    message = (unit * repeats).ljust(web_ports.MAX_DIAGNOSTIC_MESSAGE_LENGTH, "x")

    candidates = tuple(web_ports.iter_diagnostic_candidates(message))

    assert len(message) == web_ports.MAX_DIAGNOSTIC_MESSAGE_LENGTH
    assert len(candidates) == repeats
    assert len(candidates) <= len(message) // len("https://")
    assert DiagnosticDTO(code="safe_message", message=message).message == message


def test_terminal_quality_does_not_flatten_pipeline_or_availability_state() -> None:
    quality = TerminalQualityDTO(
        lifecycle="engine_verified",
        issues=(QualityIssueDTO(code="outcome_unknown", message="Outcome evidence is unavailable"),),
        engine_run_status="valid_crc_mismatch",
        strategy_analysis_scope="observed_boundary_only",
    )

    assert quality.lifecycle == "engine_verified"
    assert quality.engine_run_status == "valid_crc_mismatch"
    assert quality.strategy_analysis_scope == "observed_boundary_only"
    assert quality.issues[0].code == "outcome_unknown"
    assert not hasattr(quality, "status")
