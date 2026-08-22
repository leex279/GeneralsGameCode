"""Accessible replay-library contracts before the library templates exist."""

from __future__ import annotations

from html.parser import HTMLParser

from fastapi.testclient import TestClient

from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.resources import package_resource

from .conftest import CountingPortFactory, RecordingBootstrapper


class _Parser(HTMLParser):
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


def test_replay_library_accessibility_contract_has_a_dedicated_route() -> None:
    """Removing the route must remove the independently testable library landmark contract."""
    from generals_replay_analyzer.web.routes.replays import router

    assert router.tags == ["replays"]


def test_library_has_one_heading_labelled_filters_caption_and_current_navigation() -> None:
    html = _page("/replays")
    parser = _Parser()
    parser.feed(html)

    labels = {attrs.get("for") for tag, attrs in parser.tags if tag == "label"}
    assert sum(tag == "h1" for tag, _attrs in parser.tags) == 1
    assert any(tag == "form" and attrs.get("aria-label") == "Replay library filters" for tag, attrs in parser.tags)
    assert {"replay-search", "replay-player", "replay-faction", "replay-matchup", "replay-map", "replay-result"} <= labels
    assert any(tag == "caption" for tag, _attrs in parser.tags)
    assert any(tag == "a" and attrs.get("href") == "/imports/dialog" for tag, attrs in parser.tags)
    assert sum(attrs.get("aria-current") == "page" for _tag, attrs in parser.tags) == 1
    assert "Availability: unavailable. Reason: replay_library_adapter_pending" in html


def test_import_dialog_has_labelled_native_controls_and_no_inline_behavior() -> None:
    html = _page("/imports/dialog")
    parser = _Parser()
    parser.feed(html)

    dialog = next(attrs for tag, attrs in parser.tags if tag == "dialog")
    assert dialog["role"] == "dialog"
    assert dialog["aria-labelledby"] == "import-dialog-title"
    assert any(tag == "button" and "autofocus" in attrs and attrs.get("aria-label") == "Close import dialog" for tag, attrs in parser.tags)
    assert any(tag == "input" and attrs.get("id") == "upload-file" and "disabled" in attrs for tag, attrs in parser.tags)
    assert "opaque_ingress_handoff_pending" in html
    assert "<noscript>" in html
    assert "<script" not in html.casefold()
    assert " on" not in html.casefold()
    css = package_resource("web/static/css/app.css").read_text(encoding="utf-8")
    assert ":focus-visible" in css
    assert "prefers-reduced-motion: reduce" in css
