"""Rendered replay-cast status route acceptance checks."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.ports import AvailabilityDTO, JobDetailDTO, JobSummaryDTO

from .conftest import RecordingBootstrapper

JOB_ID = "123e4567-e89b-42d3-a456-426614174020"


def _summary(state: str, *, retryable: bool | None = None) -> JobSummaryDTO:
    return JobSummaryDTO(
        job_public_id=JOB_ID,
        stage="render_video",
        component_version="video-v1",
        state=state,  # type: ignore[arg-type]
        revision=1,
        attempt_count=1,
        max_attempts=3,
        retryable=state == "failed" if retryable is None else retryable,
        cancel_requested=False,
        created_at_utc=datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
    )


class _VideoPort:
    def __init__(self, state: str, *, retryable: bool | None = None) -> None:
        self.detail = JobDetailDTO(
            summary=_summary(state, retryable=retryable),
            availability=AvailabilityDTO(state="available"),
        )

    def get_job(self, _job_public_id: str) -> JobDetailDTO:
        return self.detail


class _PortFactory:
    def __init__(self, port: _VideoPort) -> None:
        self.port = port

    @contextmanager
    def __call__(self) -> Iterator[_VideoPort]:
        yield self.port


def _client(state: str, *, retryable: bool | None = None) -> TestClient:
    return TestClient(
        create_app(
            object(),
            port_factory=_PortFactory(_VideoPort(state, retryable=retryable)),
            bootstrapper=RecordingBootstrapper(),
        )
    )


def test_video_route_renders_in_progress_state_without_download_controls() -> None:
    with _client("running") as client:
        response = client.get(f"/video/{JOB_ID}", headers={"host": "localhost"})

    assert response.status_code == 200
    assert "Cast production in progress" in response.text
    assert 'role="status"' in response.text
    assert "Downloads unavailable" in response.text
    assert '<meta http-equiv="refresh" content="10">' in response.text
    assert response.text.index('<meta http-equiv="refresh"') < response.text.index("</head>")
    assert "This page refreshes while the worker produces the cast" in response.text
    assert f'href="/jobs/{JOB_ID}"' in response.text
    assert response.request.url.path == f"/video/{JOB_ID}"
    assert 'href="/video/' not in response.text.replace(f'href="/video/{JOB_ID}"', "")


@pytest.mark.parametrize(
    ("state", "refreshes"),
    (("pending", True), ("running", True), ("succeeded", False), ("failed", False), ("cancelled", False)),
)
def test_video_route_refresh_policy_covers_every_job_state(state: str, refreshes: bool) -> None:
    with _client(state) as client:
        response = client.get(f"/video/{JOB_ID}", headers={"host": "localhost"})

    assert response.status_code == 200
    assert ('<meta http-equiv="refresh" content="10">' in response.text) is refreshes
    assert ("This page refreshes while the worker produces the cast" in response.text) is refreshes
    assert f'href="/jobs/{JOB_ID}"' in response.text


def test_video_route_renders_failed_state_with_actionable_semantic_status() -> None:
    with _client("failed") as client:
        response = client.get(f"/video/{JOB_ID}", headers={"host": "localhost"})

    assert response.status_code == 200
    assert "Cast production failed" in response.text
    assert "This job can be retried" in response.text
    assert "No progress counter was recorded before this job ended" in response.text
    assert "Progress will appear" not in response.text


def test_video_route_does_not_offer_retry_for_terminal_failure() -> None:
    with _client("failed", retryable=False) as client:
        response = client.get(f"/video/{JOB_ID}", headers={"host": "localhost"})

    assert response.status_code == 200
    assert "Review the durable job for the recorded failure details" in response.text
    assert "can be retried" not in response.text


def test_video_route_exposes_verified_download_labels_without_private_paths() -> None:
    with _client("succeeded") as client:
        response = client.get(f"/video/{JOB_ID}", headers={"host": "localhost"})

    assert response.status_code == 200
    assert "Verified cast ready" in response.text
    assert 'aria-labelledby="cast-downloads"' in response.text
    assert 'aria-label="Download verified replay cast MP4"' in response.text
    assert 'aria-label="Download verified replay cast manifest"' in response.text
    assert "C:\\" not in response.text
    assert "/srv/" not in response.text
    assert '<meta http-equiv="refresh"' not in response.text
    assert "Media verification completed" in response.text
    assert "Progress will appear" not in response.text


def test_video_route_rejects_noncanonical_public_ids() -> None:
    with _client("running") as client:
        response = client.get("/video/123E4567-E89B-42D3-A456-426614174020", headers={"host": "localhost"})

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_video_id"
