"""Clean-root product journey from the modern engine to every primary Web surface."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from browser.fixture_data import isolated_runtime_environment  # type: ignore[import-not-found]
from browser.populated_fixture import build_populated_fixture  # type: ignore[import-not-found]
from fastapi.testclient import TestClient

from generals_replay_analyzer.config import load_runtime_configuration
from generals_replay_analyzer.engine.config import EngineRunConfig
from generals_replay_analyzer.engine.result import EngineRunStatus
from generals_replay_analyzer.engine.runner import export_telemetry
from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.bootstrap import BootstrapReadinessState, create_production_bootstrapper
from generals_replay_analyzer.web.dependencies import AnalyticsPortFactory


# TheSuperHackers @feature Leex 23/08/2026 Prove one clean-root replay journey reaches every primary product surface. (#TBD)
def test_clean_root_engine_analysis_and_web_journey(
    tmp_path: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    short_token = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:8]
    engine_root = (tmp_path.parents[1] / f"g9-e2e-{short_token}").resolve()
    result = export_telemetry(
        pinned_replay,
        EngineRunConfig(
            executable=zero_hour_runtime_executable,
            timeout_seconds=120,
            data_root=engine_root,
        ),
    )
    assert result.status in {EngineRunStatus.SUCCESS, EngineRunStatus.VALID_CRC_MISMATCH}
    assert result.trace_path is not None

    runtime_root = (tmp_path.parents[1] / f"g9-web-{short_token}").resolve()
    environment = isolated_runtime_environment(runtime_root)
    fixture = build_populated_fixture(
        runtime_root,
        environment,
        project_root=Path(__file__).parents[2],
        telemetry_trace=result.trace_path,
        telemetry_replay_quality=(
            "complete" if result.status is EngineRunStatus.SUCCESS else "partial"
        ),
        telemetry_strategy_analysis_scope=(
            "full" if result.status is EngineRunStatus.SUCCESS else "deterministic_only"
        ),
    )
    runtime_environment = dict(fixture.environment)
    configuration_root = Path(runtime_environment["LOCALAPPDATA"]) / "GeneralsReplayAnalyzer"
    watched_folders = tuple(
        Path(value) for value in json.loads(runtime_environment["GENERALS_REPLAY_ANALYZER_WATCHED_FOLDERS"])
    )
    runtime = load_runtime_configuration(
        configuration_root=configuration_root,
        environment=runtime_environment,
        values={"watched_folders": watched_folders},
    )
    readiness = BootstrapReadinessState()
    app = create_app(
        runtime.settings,
        port_factory=AnalyticsPortFactory(runtime, readiness),
        bootstrapper=create_production_bootstrapper(readiness),
    )
    manifest = fixture.manifest
    html_routes = (
        "/",
        "/replays",
        f"/replays/{manifest.replay_public_id}",
        manifest.replay_report.fixed_url,
        manifest.evidence.fixed_url,
        "/maps",
        manifest.map.fixed_url,
        "/players",
        *(player.profile_url for player in manifest.players),
        "/compare",
        manifest.comparison.fixed_url,
        "/jobs",
        manifest.pending_job.fixed_url,
        "/settings",
    )
    json_routes = (
        manifest.map.api_url,
        *(player.profile_api_url for player in manifest.players),
        manifest.comparison.api_url,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for route in html_routes:
            response = client.get(route)
            assert response.status_code == 200, (route, response.status_code, response.text[:500])
            assert "text/html" in response.headers["content-type"]
        for route in json_routes:
            response = client.get(route)
            assert response.status_code == 200, (route, response.status_code, response.text[:500])
            assert "application/json" in response.headers["content-type"]

    assert tuple(player.display_name for player in manifest.players) == ("leex279", "FOX27")
    assert "3133811" not in {player.display_name for player in manifest.players}
