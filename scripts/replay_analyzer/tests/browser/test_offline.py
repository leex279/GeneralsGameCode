"""Installed-wheel offline and same-origin browser acceptance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from generals_replay_analyzer.watching import WatchedRootRegistry

from .fixture_data import clone_populated_runtime, isolated_runtime_environment, populated_runtime_environment
from .populated_fixture import PopulatedFixtureResult
from .support import (
    ExternalWorkerController,
    assert_browser_clean,
    deterministic_context_options,
    install_browser_error_guard,
    install_same_origin_guard,
)

PRIMARY_CONTENT = (
    ("/", "Replay dashboard"),
    ("/replays", "Replay library"),
    ("/jobs", "Analysis jobs"),
    ("/maps", "Authoritative map scenes"),
    ("/players", "Player Evidence"),
    ("/compare", "Pattern Comparison"),
    ("/settings", "Analyzer settings"),
)
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "tablet": {"width": 1024, "height": 768},
    "mobile": {"width": 390, "height": 844},
}


def _goto_populated_page(
    page: Page,
    origin: str,
    path: str,
    fixture: PopulatedFixtureResult,
) -> None:
    manifest = fixture.manifest
    api_path: str | None = None
    report = next(
        (
            item
            for item in (manifest.replay_report, *manifest.player_reports)
            if item.fixed_url == path
        ),
        None,
    )
    if report is not None:
        api_path = (
            f"/api/replays/{manifest.replay_public_id}/reports/"
            f"{report.report_public_id}/charts/timeline"
        )
    elif path == manifest.map.fixed_url:
        api_path = manifest.map.api_url.split("?", 1)[0]
    elif path == manifest.comparison.fixed_url:
        api_path = manifest.comparison.api_url.split("?", 1)[0]
    else:
        profile = next((item for item in manifest.players if item.profile_url == path), None)
        if profile is not None:
            api_path = profile.profile_api_url.split("?", 1)[0]

    if api_path is None:
        response = page.goto(f"{origin}{path}", wait_until="domcontentloaded", timeout=30_000)
        assert response is not None and response.status == 200
        return
    with page.expect_response(
        lambda response: response.url.startswith(f"{origin}{api_path}"),
        timeout=30_000,
    ) as received:
        response = page.goto(f"{origin}{path}", wait_until="domcontentloaded", timeout=30_000)
    assert response is not None and response.status == 200
    assert received.value.status == 200
    assert received.value.finished() is None
    if report is not None:
        expect(page.locator("#timeline-axis-frame")).to_be_enabled()
    elif path == manifest.map.fixed_url:
        expect(page.locator("#map-chart-status")).to_contain_text("Rendered")
    elif path == manifest.comparison.fixed_url:
        assert manifest.comparison.state == "unavailable"
        expect(page.get_by_text("Status Reasons:", exact=False)).to_contain_text("subject_value_unavailable")
        assert page.locator("[data-comparison-chart] canvas").count() == 0


def test_child_runtime_environment_redirects_profile_and_temporary_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_WATCHED_FOLDERS", "private-profile-value")
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_UNKNOWN", "private-unknown-value")
    root = tmp_path
    environment = isolated_runtime_environment(root)
    for name in ("APPDATA", "HOME", "LOCALAPPDATA", "TEMP", "TMP", "USERPROFILE"):
        assert root.resolve() in Path(environment[name]).resolve().parents
    assert "PYTHONPATH" not in environment
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert {
        name for name in environment if name.upper().startswith("GENERALS_REPLAY_ANALYZER_")
    } == {"GENERALS_REPLAY_ANALYZER_DATA_ROOT"}


def test_child_runtime_environment_can_be_reused_by_server_and_worker(tmp_path: Path) -> None:
    first = isolated_runtime_environment(tmp_path)
    second = isolated_runtime_environment(tmp_path)
    assert second == first


def test_populated_runtime_clone_copies_only_release_inputs(tmp_path: Path) -> None:
    template = tmp_path / "template"
    destination = tmp_path / "runtime"
    (template / "product-data" / "managed-replays").mkdir(parents=True)
    (template / "local-app-data" / "GeneralsReplayAnalyzer" / "configuration").mkdir(parents=True)
    (template / "fixture-input").mkdir()
    (template / "product-data" / "replay-analyzer.sqlite3").write_bytes(b"database")
    (template / "product-data" / "managed-replays" / "fixture.rep").write_bytes(b"replay")
    (template / "local-app-data" / "GeneralsReplayAnalyzer" / "configuration" / "settings.json").write_text(
        "{}", encoding="utf-8"
    )
    (template / "fixture-input" / "leex279_vs_fox27.rep").write_bytes(b"watched replay")
    (template / "populated-browser-fixture.json").write_text("{}", encoding="utf-8")
    (template / "telemetry-source").mkdir()
    (template / "telemetry-source" / "private.ndjson").write_text("private", encoding="utf-8")
    (template / "web.stderr.log").write_text("private log", encoding="utf-8")

    manifest = clone_populated_runtime(template, destination)

    assert manifest == destination / "populated-browser-fixture.json"
    assert (destination / "product-data" / "replay-analyzer.sqlite3").read_bytes() == b"database"
    assert (destination / "product-data" / "managed-replays" / "fixture.rep").read_bytes() == b"replay"
    assert (
        destination / "local-app-data" / "GeneralsReplayAnalyzer" / "configuration" / "settings.json"
    ).is_file()
    assert (destination / "fixture-input" / "leex279_vs_fox27.rep").read_bytes() == b"watched replay"
    assert not (destination / "telemetry-source").exists()
    assert not (destination / "web.stderr.log").exists()

    environment = populated_runtime_environment(destination)
    assert environment["GENERALS_REPLAY_ANALYZER_WATCHED_FOLDERS"] == json.dumps(
        [str((destination / "fixture-input").resolve())]
    )
    installed_configuration = (
        destination
        / "user-profile"
        / "AppData"
        / "Local"
        / "GeneralsReplayAnalyzer"
        / "configuration"
        / "settings.json"
    )
    assert installed_configuration.read_text(encoding="utf-8") == "{}"


def test_populated_runtime_clone_rebinds_the_watched_root_without_changing_its_public_identity(
    tmp_path: Path,
) -> None:
    """Catch isolated runtime paths accidentally changing the durable ingress identity under test."""
    template = tmp_path / "template"
    destination = tmp_path / "runtime"
    (template / "product-data" / "managed-replays").mkdir(parents=True)
    (template / "local-app-data" / "GeneralsReplayAnalyzer").mkdir(parents=True)
    (template / "fixture-input").mkdir()
    (template / "product-data" / "watched-roots-v1.json").write_text(
        json.dumps(
            {
                "roots": [
                    {
                        "label": "Replay folder 1",
                        "path_key_sha256": "a" * 64,
                        "root_public_id": "00000000-0000-4000-8000-000000000011",
                    }
                ],
                "version": 1,
            }
        ),
        encoding="utf-8",
    )
    (template / "populated-browser-fixture.json").write_text("{}", encoding="utf-8")

    clone_populated_runtime(template, destination)
    roots = WatchedRootRegistry(destination / "product-data").reconcile((destination / "fixture-input",))

    assert len(roots) == 1
    assert roots[0].root_public_id == "00000000-0000-4000-8000-000000000011"
    assert roots[0].label == "Replay folder 1"


def test_same_origin_guard_records_failed_local_transport() -> None:
    handlers: dict[str, object] = {}

    class FakePage:
        def route(self, _pattern: str, handler: object) -> None:
            handlers["route"] = handler

        def on(self, event: str, handler: object) -> None:
            handlers[event] = handler

    class FakeRequest:
        url = "http://127.0.0.1:8765/static/js/app.js"

    failures = install_same_origin_guard(FakePage(), "http://127.0.0.1:8765")  # type: ignore[arg-type]
    request_failed = handlers["requestfailed"]
    request_failed(FakeRequest())  # type: ignore[operator]
    assert failures == ["http://127.0.0.1:8765/static/js/app.js"]


def test_external_worker_controller_uses_installed_command_and_settles_owned_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            captured["terminated"] = True
            self.returncode = 0

        def wait(self, timeout: float) -> int:
            captured["wait_timeout"] = timeout
            assert self.returncode is not None
            return self.returncode

        def kill(self) -> None:
            captured["killed"] = True
            self.returncode = 1

    def fake_popen(arguments: list[str], **kwargs: object) -> FakeProcess:
        captured["arguments"] = arguments
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr("browser.support.subprocess.Popen", fake_popen)
    executable = tmp_path / "environment" / "Scripts" / "replay-analyzer.exe"
    environment = {"GENERALS_REPLAY_ANALYZER_DATA_ROOT": str(tmp_path / "product-data")}
    controller = ExternalWorkerController(executable, tmp_path, environment)

    controller.start()
    controller.stop()

    assert captured["arguments"] == [
        str(executable),
        "worker",
        "--poll-seconds",
        "1",
        "--lease-seconds",
        "15",
    ]
    assert captured["cwd"] == tmp_path
    assert captured["env"] is environment
    assert captured["terminated"] is True
    assert captured["wait_timeout"] == 10
    assert (tmp_path / "worker.stdout.log").is_file()
    assert (tmp_path / "worker.stderr.log").is_file()


def test_external_worker_controller_does_not_mask_an_early_worker_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedProcess:
        def poll(self) -> int:
            return 7

    launches = 0

    def fake_popen(_arguments: list[str], **_kwargs: object) -> ExitedProcess:
        nonlocal launches
        launches += 1
        return ExitedProcess()

    monkeypatch.setattr("browser.support.subprocess.Popen", fake_popen)
    controller = ExternalWorkerController(tmp_path / "replay-analyzer.exe", tmp_path, {})
    controller.start()

    with pytest.raises(AssertionError, match="exited unexpectedly"):
        controller.start()

    assert launches == 1


@pytest.mark.browser
def test_installed_wheel_dashboard_smoke(installed_server: object, browser: Browser) -> None:
    origin = installed_server.origin
    context = browser.new_context(**deterministic_context_options(VIEWPORTS["desktop"]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    page.goto(origin, wait_until="domcontentloaded", timeout=30_000)
    expect(page.locator("main")).to_be_visible(timeout=30_000)
    expect(page.get_by_role("heading", name="Replay dashboard", exact=True)).to_be_visible()
    assert page.evaluate("location.origin") == origin
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
@pytest.mark.parametrize(("path", "heading"), PRIMARY_CONTENT)
def test_primary_pages_remain_same_origin_without_browser_cache(
    installed_server: object,
    browser: Browser,
    path: str,
    heading: str,
) -> None:
    origin = installed_server.origin
    context = browser.new_context(**deterministic_context_options(VIEWPORTS["desktop"]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)
    failed_assets: list[str] = []
    page.on(
        "response",
        lambda response: failed_assets.append(response.url)
        if response.request.resource_type in {"script", "stylesheet", "image", "font"} and response.status >= 400
        else None,
    )
    page.goto(f"{origin}{path}", wait_until="domcontentloaded", timeout=30_000)
    expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
    assert rejected == []
    assert failed_assets == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
@pytest.mark.parametrize("viewport_name", tuple(VIEWPORTS))
@pytest.mark.parametrize(
    ("path", "heading"),
    (("/replays", "Replay library"), ("/maps", "Authoritative map scenes"), ("/compare", "Pattern Comparison")),
)
def test_required_index_pages_reflow_without_document_overflow(
    installed_server: object,
    browser: Browser,
    viewport_name: str,
    path: str,
    heading: str,
) -> None:
    origin = installed_server.origin
    context = browser.new_context(**deterministic_context_options(VIEWPORTS[viewport_name]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)
    page.goto(f"{origin}{path}", wait_until="domcontentloaded", timeout=30_000)
    expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
    dimensions = page.evaluate(
        "({scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth})"
    )
    assert dimensions["scrollWidth"] <= dimensions["clientWidth"]
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
@pytest.mark.parametrize(
    ("path", "heading", "essential_selector"),
    (
        ("/replays", "Replay library", 'form[aria-label="Replay library filters"]'),
        ("/jobs", "Analysis jobs", 'form[aria-label="Analysis job filters"]'),
        ("/maps", "Authoritative map scenes", 'form[aria-label="Map scene filters"]'),
        ("/players", "Player Evidence", 'form[aria-label="Player Filters"]'),
        ("/compare", "Pattern Comparison", "fieldset"),
        ("/settings", "Analyzer settings", "table"),
    ),
)
def test_essential_page_content_remains_available_without_javascript(
    installed_server: object,
    browser: Browser,
    path: str,
    heading: str,
    essential_selector: str,
) -> None:
    origin = installed_server.origin
    context = browser.new_context(
        **deterministic_context_options(VIEWPORTS["desktop"], java_script_enabled=False)
    )
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)
    page.goto(f"{origin}{path}", wait_until="domcontentloaded", timeout=30_000)
    expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
    expect(page.locator(essential_selector).first).to_be_visible()
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
def test_populated_fixed_pages_are_same_origin_and_truthful(
    populated_server: object,
    populated_fixture_template: PopulatedFixtureResult,
    browser: Browser,
) -> None:
    origin = populated_server.origin
    manifest = populated_fixture_template.manifest
    routes = (
        (manifest.replay_report.fixed_url, "Timeline data in authoritative replay frames", ("leex279", "FOX27")),
        (manifest.evidence.fixed_url, "Typed immutable source", (manifest.evidence.tier,)),
        (manifest.map.fixed_url, "Observed sample reduction", ("authoritative", "Frame start", "Frame end")),
        (manifest.players[0].profile_url, "Names & Provenance", ("leex279", "Player Patterns")),
        (manifest.players[1].profile_url, "Names & Provenance", ("FOX27", "Player Patterns")),
        (manifest.comparison.fixed_url, "Comparison Evidence", ("economy.cash_change_total", "Sample")),
        (manifest.pending_job.fixed_url, "Job state", ("discover", "pending")),
    )
    context = browser.new_context(**deterministic_context_options(VIEWPORTS["desktop"]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)
    failed_assets: list[str] = []
    page.on(
        "response",
        lambda response: failed_assets.append(response.url)
        if response.request.resource_type in {"script", "stylesheet", "image", "font"}
        and response.status >= 400
        else None,
    )
    for path, semantic_text, expected_values in routes:
        _goto_populated_page(page, origin, path, populated_fixture_template)
        expect(page.get_by_text(semantic_text, exact=False).first).to_be_visible()
        text = page.locator("main").inner_text()
        assert all(value in text for value in expected_values)
    assert rejected == []
    assert failed_assets == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
@pytest.mark.parametrize("viewport_name", tuple(VIEWPORTS))
def test_populated_library_report_map_and_comparison_reflow_offline(
    populated_server: object,
    populated_fixture_template: PopulatedFixtureResult,
    browser: Browser,
    viewport_name: str,
) -> None:
    origin = populated_server.origin
    manifest = populated_fixture_template.manifest
    routes = (
        ("/replays", "Replay library"),
        (manifest.replay_report.fixed_url, "Timeline data in authoritative replay frames"),
        (manifest.map.fixed_url, "Observed sample reduction"),
        (manifest.comparison.fixed_url, "Comparison Evidence"),
    )
    context = browser.new_context(**deterministic_context_options(VIEWPORTS[viewport_name]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)
    for path, semantic_text in routes:
        _goto_populated_page(page, origin, path, populated_fixture_template)
        expect(page.get_by_text(semantic_text, exact=False).first).to_be_visible()
        dimensions = page.evaluate(
            "({scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth})"
        )
        assert dimensions["scrollWidth"] <= dimensions["clientWidth"]
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
@pytest.mark.parametrize("viewport_name", tuple(VIEWPORTS))
def test_populated_wide_evidence_tables_are_keyboard_scroll_regions(
    populated_server: object,
    populated_fixture_template: PopulatedFixtureResult,
    browser: Browser,
    viewport_name: str,
) -> None:
    origin = populated_server.origin
    manifest = populated_fixture_template.manifest
    routes = (
        (manifest.replay_report.fixed_url, "Replay timeline table"),
        (manifest.map.fixed_url, "Map evidence tables"),
    )
    context = browser.new_context(**deterministic_context_options(VIEWPORTS[viewport_name]))
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)
    for path, region_name in routes:
        _goto_populated_page(page, origin, path, populated_fixture_template)
        dimensions = page.evaluate(
            "({scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth})"
        )
        assert dimensions["scrollWidth"] <= dimensions["clientWidth"]

        region = page.get_by_role("region", name=region_name, exact=True)
        expect(region).to_have_attribute("tabindex", "0")
        for _ in range(400):
            page.keyboard.press("Tab")
            if region.evaluate("element => element === document.activeElement"):
                break
        expect(region).to_be_focused()

        scroll = region.evaluate(
            "element => ({left: element.scrollLeft, width: element.clientWidth, content: element.scrollWidth})"
        )
        if scroll["content"] > scroll["width"]:
            for _ in range(12):
                page.keyboard.press("ArrowRight")
            page.wait_for_function(
                "element => element.scrollLeft > 0",
                arg=region.element_handle(),
                timeout=5_000,
            )
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()


@pytest.mark.browser
def test_populated_essential_content_survives_without_javascript(
    populated_server: object,
    populated_fixture_template: PopulatedFixtureResult,
    browser: Browser,
) -> None:
    origin = populated_server.origin
    manifest = populated_fixture_template.manifest
    routes = (
        ("/replays", "leex279"),
        (manifest.replay_report.fixed_url, "Timeline data in authoritative replay frames"),
        (manifest.evidence.fixed_url, "Typed immutable source"),
        (manifest.map.fixed_url, "Observed sample reduction"),
        (manifest.players[0].profile_url, "Player Patterns"),
        (manifest.comparison.fixed_url, "Comparison Evidence"),
        (manifest.pending_job.fixed_url, "Job state"),
        ("/settings", "Editable settings"),
    )
    context = browser.new_context(
        **deterministic_context_options(VIEWPORTS["desktop"], java_script_enabled=False)
    )
    page = context.new_page()
    console_errors, page_errors = install_browser_error_guard(page)
    rejected = install_same_origin_guard(page, origin)
    for path, semantic_text in routes:
        page.goto(f"{origin}{path}", wait_until="domcontentloaded", timeout=30_000)
        expect(page.get_by_text(semantic_text, exact=False).first).to_be_visible()
        assert page.locator("main").inner_text().strip()
    assert rejected == []
    assert_browser_clean(console_errors, page_errors)
    context.close()
