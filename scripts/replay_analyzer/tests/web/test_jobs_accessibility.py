"""Semantic and no-script accessibility checks for durable job pages."""

from __future__ import annotations

from html.parser import HTMLParser

from .test_jobs import JOB_ID, _client, _JobPort, _summary


class _StructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.h1_count = 0
        self.captions = 0
        self.labels: list[str] = []
        self.current_pages = 0
        self._label_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "h1":
            self.h1_count += 1
        if tag == "caption":
            self.captions += 1
        if tag == "label":
            self._label_depth += 1
        if attributes.get("aria-current") == "page":
            self.current_pages += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self._label_depth:
            self._label_depth -= 1


def test_jobs_index_has_one_heading_labelled_filters_caption_and_current_navigation() -> None:
    with _client(_JobPort()) as client:
        response = client.get("/jobs", headers={"host": "localhost"})

    parser = _StructureParser()
    parser.feed(response.text)
    assert parser.h1_count == 1
    assert parser.captions == 1
    assert parser.current_pages == 1
    assert 'aria-label="Analysis job filters"' in response.text
    assert "Skip to main content" in response.text


def test_job_detail_has_distinct_action_names_consequences_and_disabled_ineligible_controls() -> None:
    with _client(_JobPort(_summary(state="succeeded", retryable=False))) as client:
        response = client.get(f"/jobs/{JOB_ID}", headers={"host": "localhost"})

    assert 'aria-label="Retry this analysis job"' in response.text
    assert 'aria-label="Cancel this analysis job"' in response.text
    assert "Retry creates a new eligible attempt" in response.text
    assert "Cancel requests safe settlement" in response.text
    assert response.text.count('aria-disabled="true"') >= 2
    assert "Progress: progress_unavailable" in response.text


def test_cancel_request_and_log_truncation_are_explicit_text() -> None:
    port = _JobPort(_summary(cancel_requested=True))
    log_id = "123e4567-e89b-42d3-a456-426614174023"
    with _client(port) as client:
        detail = client.get(f"/jobs/{JOB_ID}", headers={"host": "localhost"})
        log = client.get(f"/jobs/{JOB_ID}/logs/{log_id}", headers={"host": "localhost"})

    assert "Cancel requested" in detail.text
    assert "truncated" in log.text.casefold()
    assert "<pre>" in log.text
