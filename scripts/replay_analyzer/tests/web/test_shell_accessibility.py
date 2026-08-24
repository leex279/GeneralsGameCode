"""Static semantic and reduced-motion contracts for the package shell."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Self

from fastapi.testclient import TestClient

from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.resources import package_resource

from .conftest import CountingPortFactory, RecordingBootstrapper


class LandmarkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _page(path: str) -> str:
    app = create_app(object(), port_factory=CountingPortFactory(), bootstrapper=RecordingBootstrapper())
    with TestClient(app) as client:
        response = client.get(path, headers={"host": "localhost"})
    assert response.status_code == 200
    return response.text


def test_shell_has_keyboard_landmarks_unique_heading_and_disabled_non_actions() -> None:
    html = _page("/")
    parser = LandmarkParser()
    parser.feed(html)

    assert any(tag == "header" for tag, _attrs in parser.tags)
    assert ("main", {"id": "main-content", "tabindex": "-1"}) in parser.tags
    assert any(tag == "footer" for tag, _attrs in parser.tags)
    assert any(tag == "nav" and attrs.get("aria-label") == "Primary navigation" for tag, attrs in parser.tags)
    assert sum(tag == "h1" for tag, _attrs in parser.tags) == 1
    assert any(tag == "a" and attrs.get("href") == "#main-content" for tag, attrs in parser.tags)
    assert sum(attrs.get("aria-current") == "page" for _tag, attrs in parser.tags) == 1
    assert any(attrs.get("aria-disabled") == "true" for _tag, attrs in parser.tags)
    assert any(tag == "dialog" and attrs.get("aria-labelledby") == "command-palette-title" for tag, attrs in parser.tags)
    assert any(tag == "button" and "data-command-palette-close" in attrs for tag, attrs in parser.tags)


def test_shell_exposes_textual_unavailability_with_non_color_css_support() -> None:
    html = _page("/")
    css = package_resource("web/static/css/app.css").read_text(encoding="utf-8")

    assert "Availability: unavailable" in html
    assert "analytics_adapter_pending" in html
    assert ":focus-visible" in css
    assert "prefers-reduced-motion: reduce" in css


def test_partial_availability_without_a_port_reason_still_has_an_honest_stable_label() -> None:
    app = create_app(
        object(),
        port_factory=lambda: _PartialAvailabilityScope(),
        bootstrapper=RecordingBootstrapper(),
    )

    with TestClient(app) as client:
        response = client.get("/", headers={"host": "localhost"})

    assert "Availability: partial" in response.text
    assert "availability_reason_not_supplied" in response.text


def test_empty_state_is_reserved_for_unavailable_availability() -> None:
    app = create_app(
        object(),
        port_factory=lambda: _PartialAvailabilityScope(),
        bootstrapper=RecordingBootstrapper(),
    )

    with TestClient(app) as client:
        response = client.get("/", headers={"host": "localhost"})

    assert "Availability: partial" in response.text
    assert "Analysis data unavailable" not in response.text
    assert "Analysis is partially available" in response.text


class _PartialAvailabilityScope:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        return None

    def readiness(self) -> object:
        return None

    def dashboard(self) -> object:
        from datetime import UTC, datetime

        from generals_replay_analyzer.web.ports import AvailabilityDTO, DashboardDTO

        return DashboardDTO(
            generated_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
            availability=AvailabilityDTO(state="partial"),
        )

    def identity_landing(self) -> object:
        return self.dashboard()
