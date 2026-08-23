"""Durable job routes and immutable web boundary tests."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    CancelJobCommandDTO,
    JobDetailDTO,
    JobLogChunkDTO,
    JobLogQueryDTO,
    JobLogReferenceDTO,
    JobMutationDTO,
    JobPageDTO,
    JobQueryDTO,
    JobSummaryDTO,
    RetryJobCommandDTO,
)

from .conftest import RecordingBootstrapper

JOB_ID = "123e4567-e89b-42d3-a456-426614174020"
REPLAY_ID = "123e4567-e89b-42d3-a456-426614174021"


def _summary(**changes: object) -> JobSummaryDTO:
    values: dict[str, object] = {
        "job_public_id": JOB_ID,
        "replay_public_id": REPLAY_ID,
        "stage": "parse",
        "component_version": "parse-v1",
        "state": "running",
        "revision": 3,
        "attempt_count": 1,
        "max_attempts": 3,
        "retryable": True,
        "cancel_requested": False,
        "created_at_utc": datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        "started_at_utc": datetime(2026, 8, 22, 12, 1, tzinfo=UTC),
        "completed_at_utc": None,
        "progress": None,
        "error": None,
    }
    values.update(changes)
    return JobSummaryDTO.model_validate(values)


def test_job_query_uses_accepted_cursor_contract_and_frozen_values() -> None:
    query = JobQueryDTO(states=("pending", "running"), replay_public_id=REPLAY_ID, stage="parse", limit=25)
    page = JobPageDTO(
        query=query,
        items=(_summary(),),
        next_job_public_id=JOB_ID,
        watched_folders=(),
        poll_after_seconds=2,
        availability=AvailabilityDTO(state="available"),
    )

    assert query.limit == 25
    assert page.next_job_public_id == JOB_ID
    assert not hasattr(query, "page")
    assert not hasattr(page, "total_items")
    with pytest.raises(ValidationError):
        JobQueryDTO.model_validate({"page": 1})


def test_job_dtos_reject_private_lifecycle_and_process_fields() -> None:
    for private_field in ("lease_token", "lease_owner", "worker_public_id", "process_id", "input_json", "path"):
        values = _summary().model_dump()
        values[private_field] = "private"
        with pytest.raises(ValidationError):
            JobSummaryDTO.model_validate(values)


@pytest.mark.parametrize("state", ["pending", "running", "succeeded", "failed", "cancelled"])
def test_job_state_vocabulary_matches_the_accepted_lifecycle(state: str) -> None:
    assert _summary(state=state).state == state


class _TokenValidator:
    def accepts(self, token: str | None) -> bool:
        return token == "accepted-token"


class _JobPort:
    def __init__(self, summary: JobSummaryDTO | None = None) -> None:
        self.summary = summary or _summary()
        self.queries: list[JobQueryDTO] = []
        self.retries: list[RetryJobCommandDTO] = []
        self.cancellations: list[CancelJobCommandDTO] = []
        self.log_queries: list[JobLogQueryDTO] = []
        self.detail_reads = 0

    def list_jobs(self, query: JobQueryDTO) -> JobPageDTO:
        self.queries.append(query)
        return JobPageDTO(
            query=query,
            items=(self.summary,),
            next_job_public_id=None,
            watched_folders=(),
            poll_after_seconds=2 if self.summary.state in {"pending", "running"} else None,
            availability=AvailabilityDTO(state="available"),
        )

    def _detail(self) -> JobDetailDTO:
        return JobDetailDTO(
            summary=self.summary,
            dependency_job_public_ids=("123e4567-e89b-42d3-a456-426614174022",),
            logs=(
                JobLogReferenceDTO(
                    log_public_id="123e4567-e89b-42d3-a456-426614174023",
                    label="stdout",
                    byte_count=70_000,
                    created_at_utc=datetime(2026, 8, 22, 12, 2, tzinfo=UTC),
                ),
            ),
            availability=AvailabilityDTO(state="available"),
        )

    def get_job(self, _job_public_id: str) -> JobDetailDTO:
        self.detail_reads += 1
        return self._detail()

    def retry_job(self, command: RetryJobCommandDTO) -> JobMutationDTO:
        self.retries.append(command)
        self.summary = self.summary.model_copy(update={"state": "pending", "revision": command.expected_revision + 1})
        return JobMutationDTO(detail=self._detail(), result_code="retried")

    def cancel_job(self, command: CancelJobCommandDTO) -> JobMutationDTO:
        self.cancellations.append(command)
        if self.summary.state == "pending":
            self.summary = self.summary.model_copy(update={"state": "cancelled", "revision": command.expected_revision + 1})
            result = "cancelled"
        else:
            self.summary = self.summary.model_copy(update={"cancel_requested": True, "revision": command.expected_revision + 1})
            result = "cancel_requested"
        return JobMutationDTO(detail=self._detail(), result_code=result)

    def read_job_log(self, query: JobLogQueryDTO) -> JobLogChunkDTO:
        self.log_queries.append(query)
        return JobLogChunkDTO(state="available", text="safe <script>alert(1)</script>", next_offset=65_536)


class _JobPortFactory:
    def __init__(self, port: _JobPort) -> None:
        self.port = port
        self.created = 0
        self.closed = 0

    @contextmanager
    def __call__(self) -> Iterator[_JobPort]:
        self.created += 1
        try:
            yield self.port
        finally:
            self.closed += 1


def _client(port: _JobPort) -> TestClient:
    return TestClient(
        create_app(
            object(),
            port_factory=_JobPortFactory(port),
            bootstrapper=RecordingBootstrapper(),
            csrf_validator=_TokenValidator(),
        )
    )


def test_jobs_list_maps_accepted_cursor_filters_and_active_polling() -> None:
    port = _JobPort()
    with _client(port) as client:
        response = client.get(
            f"/jobs?state=pending&state=running&stage=parse&replay_public_id={REPLAY_ID}&limit=25",
            headers={"host": "localhost", "hx-request": "true"},
        )

    assert response.status_code == 200
    assert port.queries == [
        JobQueryDTO(states=("pending", "running"), stage="parse", replay_public_id=REPLAY_ID, limit=25)
    ]
    assert "<!doctype" not in response.text.casefold()
    assert 'hx-get="/jobs' in response.text
    assert "running" in response.text


def test_jobs_list_canonicalizes_equivalent_state_filter_permutations_and_duplicates() -> None:
    port = _JobPort()
    with _client(port) as client:
        response = client.get(
            "/jobs?state=running&state=pending&state=running&limit=20",
            headers={"host": "localhost", "hx-request": "true"},
        )

    assert response.status_code == 200
    assert port.queries == [JobQueryDTO(states=("pending", "running"), limit=20)]
    assert 'hx-get="/jobs?state=pending&amp;state=running&amp;limit=20"' in response.text


def test_terminal_job_page_does_not_poll_and_detail_keeps_states_distinct() -> None:
    port = _JobPort(_summary(state="failed", cancel_requested=False))
    with _client(port) as client:
        listing = client.get("/jobs", headers={"host": "localhost"})
        detail = client.get(f"/jobs/{JOB_ID}", headers={"host": "localhost"})

    assert listing.status_code == detail.status_code == 200
    assert "hx-trigger" not in listing.text
    assert '<script src="/static/vendor/htmx.min.js" defer></script>' in listing.text
    assert "State: failed" in detail.text
    assert "Progress: progress_unavailable" in detail.text
    assert "Dependency jobs" in detail.text
    assert "stdout" in detail.text


def test_retry_and_cancel_map_only_revisioned_public_commands() -> None:
    port = _JobPort()
    headers = {
        "host": "localhost",
        "origin": "http://localhost",
        "x-csrf-token": "accepted-token",
    }
    with _client(port) as client:
        retried = client.post(f"/jobs/{JOB_ID}/retry", data={"expected_revision": "3"}, headers=headers)
        cancelled = client.post(f"/jobs/{JOB_ID}/cancel", data={"expected_revision": "4"}, headers=headers)

    assert retried.status_code == cancelled.status_code == 200
    assert port.retries == [RetryJobCommandDTO(job_public_id=JOB_ID, expected_revision=3)]
    assert port.cancellations == [CancelJobCommandDTO(job_public_id=JOB_ID, expected_revision=4)]
    assert "cancelled" in cancelled.text
    assert port.detail_reads == 0


def test_job_log_read_is_bounded_opaque_and_html_escaped() -> None:
    port = _JobPort()
    log_id = "123e4567-e89b-42d3-a456-426614174023"
    with _client(port) as client:
        response = client.get(f"/jobs/{JOB_ID}/logs/{log_id}?offset=0", headers={"host": "localhost"})

    assert response.status_code == 200
    assert port.log_queries == [JobLogQueryDTO(job_public_id=JOB_ID, log_public_id=log_id, offset=0, limit=65_536)]
    assert "&lt;script&gt;" in response.text and "<script>" not in response.text
    assert "log continues" in response.text.casefold()


def test_job_mutations_are_rejected_by_security_before_port_invocation() -> None:
    port = _JobPort()
    with _client(port) as client:
        responses = (
            client.post(f"/jobs/{JOB_ID}/retry", data={"expected_revision": "3"}, headers={"host": "example.test"}),
            client.post(
                f"/jobs/{JOB_ID}/cancel",
                data={"expected_revision": "3"},
                headers={"host": "localhost", "origin": "http://example.test", "x-csrf-token": "accepted-token"},
            ),
            client.post(
                f"/jobs/{JOB_ID}/cancel",
                data={"expected_revision": "3"},
                headers={"host": "localhost", "origin": "http://localhost", "x-csrf-token": "wrong"},
            ),
        )

    assert [response.status_code for response in responses] == [403, 403, 403]
    assert port.retries == [] and port.cancellations == []


def test_rendered_job_action_uses_one_time_native_csrf_without_javascript() -> None:
    port = _JobPort(_summary(state="failed", retryable=True))
    headers = {"host": "localhost", "origin": "http://localhost"}
    with _client(port) as client:
        detail = client.get(f"/jobs/{JOB_ID}", headers={"host": "localhost"})
        token = re.search(r'name="_csrf" value="([^"]+)"', detail.text)
        assert token is not None
        accepted = client.post(
            f"/jobs/{JOB_ID}/retry",
            data={"expected_revision": "3", "_csrf": token.group(1)},
            headers=headers,
        )
        replayed = client.post(
            f"/jobs/{JOB_ID}/retry",
            data={"expected_revision": "4", "_csrf": token.group(1)},
            headers=headers,
        )

    assert accepted.status_code == 200
    assert replayed.status_code == 403
    assert len(port.retries) == 1


def test_rendered_job_tokens_are_distinct_and_bound_to_the_exact_post_action() -> None:
    port = _JobPort(_summary(state="failed", retryable=True))
    headers = {"host": "localhost", "origin": "http://localhost"}
    with _client(port) as client:
        detail = client.get(f"/jobs/{JOB_ID}", headers={"host": "localhost"})
        retry = re.search(rf'action="/jobs/{JOB_ID}/retry".*?name="_csrf" value="([^"]+)"', detail.text)
        cancel = re.search(rf'action="/jobs/{JOB_ID}/cancel".*?name="_csrf" value="([^"]+)"', detail.text)
        assert retry is not None and cancel is not None
        assert retry.group(1) != cancel.group(1)

        crossed = client.post(
            f"/jobs/{JOB_ID}/cancel",
            data={"expected_revision": "3", "_csrf": retry.group(1)},
            headers=headers,
        )
        accepted = client.post(
            f"/jobs/{JOB_ID}/retry",
            data={"expected_revision": "3", "_csrf": retry.group(1)},
            headers=headers,
        )

    assert crossed.status_code == 403
    assert accepted.status_code == 200
    assert port.cancellations == []
    assert len(port.retries) == 1


def test_wrong_job_form_token_is_rejected_before_opening_the_port_scope() -> None:
    port = _JobPort(_summary(state="failed", retryable=True))
    factory = _JobPortFactory(port)
    app = create_app(
        object(),
        port_factory=factory,
        bootstrapper=RecordingBootstrapper(),
        csrf_validator=_TokenValidator(),
    )
    with TestClient(app) as client:
        detail = client.get(f"/jobs/{JOB_ID}", headers={"host": "localhost"})
        scopes_before_post = factory.created
        response = client.post(
            f"/jobs/{JOB_ID}/retry",
            data={"expected_revision": "3", "_csrf": "wrong-token"},
            headers={"host": "localhost", "origin": "http://localhost"},
        )

    assert detail.status_code == 200
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_rejected"
    assert factory.created == scopes_before_post
    assert factory.closed == factory.created


def test_job_forms_reuse_the_valid_session_cookie_across_tabs() -> None:
    port = _JobPort(_summary(state="failed", retryable=True))
    with _client(port) as client:
        first = client.get(f"/jobs/{JOB_ID}", headers={"host": "localhost"})
        first_token = re.search(
            rf'action="/jobs/{JOB_ID}/retry".*?name="_csrf" value="([^"]+)"', first.text
        )
        first_cookie = client.cookies.get("_csrf")
        second = client.get(f"/jobs/{JOB_ID}", headers={"host": "localhost"})
        second_token = re.search(
            rf'action="/jobs/{JOB_ID}/retry".*?name="_csrf" value="([^"]+)"', second.text
        )
        second_cookie = client.cookies.get("_csrf")
        assert first_token is not None and second_token is not None
        response = client.post(
            f"/jobs/{JOB_ID}/retry",
            data={"expected_revision": "3", "_csrf": first_token.group(1)},
            headers={"host": "localhost", "origin": "http://localhost"},
        )

    assert first_cookie == second_cookie
    assert first_token.group(1) != second_token.group(1)
    assert response.status_code == 200
