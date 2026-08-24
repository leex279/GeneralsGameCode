"""Deterministic populated runtime fixture contract for installed browser tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from generals_replay_analyzer.config import load_runtime_configuration
from generals_replay_analyzer.db import create_database_engine, create_session_factory
from generals_replay_analyzer.db.models import Job, TelemetryRun
from generals_replay_analyzer.video.resolver import VideoRequestResolver

from .populated_fixture import (
    ComparisonBinding,
    EvidenceBinding,
    JobBinding,
    MapBinding,
    PlayerBinding,
    PopulatedFixtureManifest,
    PopulatedFixtureResult,
    ReportBinding,
    build_populated_fixture,
)


def _environment(runtime_root: Path) -> dict[str, str]:
    return {
        "GENERALS_REPLAY_ANALYZER_DATA_ROOT": str(runtime_root / "product-data"),
        "LOCALAPPDATA": str(runtime_root / "local-app-data"),
        "APPDATA": str(runtime_root / "roaming-app-data"),
        "USERPROFILE": str(runtime_root / "user-profile"),
    }


def test_manifest_contract_writes_exact_canonical_dynamic_identity_document(tmp_path: Path) -> None:
    manifest = PopulatedFixtureManifest(
        schema_version="populated-browser-fixture-v2",
        fixed_now_utc="2026-08-23T12:00:00Z",
        input_replay_sha256="a" * 64,
        telemetry_trace_sha256="b" * 64,
        map_member_sha256s=("c" * 64,),
        schema_head="0005",
        replay_public_id="00000000-0000-4000-8000-000000000001",
        import_root_public_id="00000000-0000-4000-8000-000000000011",
        import_relative_path="leex279_vs_fox27.rep",
        replay_report=ReportBinding(
            "00000000-0000-4000-8000-000000000002",
            "v1",
            None,
            None,
            "/replays/00000000-0000-4000-8000-000000000001/reports/00000000-0000-4000-8000-000000000002",
        ),
        player_reports=(
            ReportBinding(
                "00000000-0000-4000-8000-000000000006",
                "v1",
                "00000000-0000-4000-8000-000000000004",
                "00000000-0000-4000-8000-000000000014",
                "/reports/6",
            ),
            ReportBinding(
                "00000000-0000-4000-8000-000000000007",
                "v1",
                "00000000-0000-4000-8000-000000000005",
                "00000000-0000-4000-8000-000000000015",
                "/reports/7",
            ),
        ),
        evidence=EvidenceBinding(
            "00000000-0000-4000-8000-000000000008",
            "observed",
            "/evidence/observed/00000000-0000-4000-8000-000000000008?report_id=00000000-0000-4000-8000-000000000002",
        ),
        map=MapBinding(
            "00000000-0000-4000-8000-000000000003",
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
            0,
            20,
            "replay-map-scene-v2",
            "/map",
            "/api/map",
        ),
        players=(
            PlayerBinding(
                "00000000-0000-4000-8000-000000000004",
                "leex279",
                0,
                "00000000-0000-4000-8000-000000000014",
                ("00000000-0000-4000-8000-000000000024",),
                "{}",
                "/players/00000000-0000-4000-8000-000000000004",
                "/api/players/00000000-0000-4000-8000-000000000004/profile",
                "/players/00000000-0000-4000-8000-000000000004/identity",
            ),
            PlayerBinding(
                "00000000-0000-4000-8000-000000000005",
                "FOX27",
                0,
                "00000000-0000-4000-8000-000000000015",
                ("00000000-0000-4000-8000-000000000025",),
                "{}",
                "/players/00000000-0000-4000-8000-000000000005",
                "/api/players/00000000-0000-4000-8000-000000000005/profile",
                "/players/00000000-0000-4000-8000-000000000005/identity",
            ),
        ),
        comparison=ComparisonBinding(
            "00000000-0000-4000-8000-000000000009",
            "available",
            "economy.cash_change_total",
            "d" * 64,
            "e" * 64,
            "{}",
            "/compare/result",
            "/api/comparisons",
        ),
        pending_job=JobBinding(
            "00000000-0000-4000-8000-000000000010",
            "discover",
            "pending",
            0,
            "/jobs/00000000-0000-4000-8000-000000000010",
        ),
        settings_revision=0,
        ollama_state="not_requested",
    )

    path = manifest.write(tmp_path)

    assert path == tmp_path / "populated-browser-fixture.json"
    document = json.loads(path.read_bytes())
    assert document["schema_version"] == "populated-browser-fixture-v2"
    assert document["payload_sha256"] == manifest.fixture_digest
    assert document["payload"]["players"][0]["display_name"] == "leex279"
    assert document["payload"]["comparison"]["metric_definition_id"] == "economy.cash_change_total"


@pytest.mark.parametrize(
    "query_timings",
    (
        (("get_report:replay", 30.0),),
        tuple((f"query:{index}", 10.0) for index in range(13)),
    ),
)
def test_result_rejects_query_timings_outside_the_acceptance_budget(
    tmp_path: Path,
    query_timings: tuple[tuple[str, float], ...],
) -> None:
    with pytest.raises(ValueError, match="query timing budget"):
        PopulatedFixtureResult(
            tmp_path / "populated-browser-fixture.json",
            tmp_path,
            (),
            None,  # type: ignore[arg-type]
            query_timings,
        )


def test_result_rejects_an_incomplete_query_timing_contract(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="query timing budget"):
        PopulatedFixtureResult(
            tmp_path / "populated-browser-fixture.json",
            tmp_path,
            (),
            None,  # type: ignore[arg-type]
            (),
        )


def test_builder_composes_pinned_replay_through_production_services_and_queries(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[2]
    runtime_root = tmp_path / "external-runtime"

    result = build_populated_fixture(runtime_root, _environment(runtime_root), project_root=project_root)
    manifest = result.manifest

    assert manifest.schema_version == "populated-browser-fixture-v2"
    assert tuple(player.display_name for player in manifest.players) == ("leex279", "FOX27")
    assert manifest.comparison.metric_definition_id == "economy.cash_change_total"
    assert manifest.pending_job.status == "pending"
    assert manifest.ollama_state == "not_requested"
    assert (runtime_root / "product-data" / "replay-analyzer.sqlite3").is_file()
    assert result.manifest_path == runtime_root / "populated-browser-fixture.json"
    assert result.runtime_root == runtime_root.resolve()
    result_environment = dict(result.environment)
    assert json.loads(result_environment["GENERALS_REPLAY_ANALYZER_WATCHED_FOLDERS"]) == [
        str((runtime_root / "fixture-input").resolve())
    ]
    assert json.loads(result.manifest_path.read_bytes())["payload_sha256"] == result.fixture_digest
    assert str(runtime_root.resolve()) not in result.manifest_path.read_text(encoding="utf-8")
    assert len(result.query_timings) == 15
    assert all(duration < 30.0 for _label, duration in result.query_timings)
    assert sum(duration for _label, duration in result.query_timings) < 120.0
    engine = create_database_engine(runtime_root / "product-data" / "replay-analyzer.sqlite3")
    try:
        factory = create_session_factory(engine)
        result_environment = dict(result.environment)
        settings = load_runtime_configuration(
            environment=result_environment,
            values={
                "watched_folders": tuple(
                    Path(value)
                    for value in json.loads(result_environment["GENERALS_REPLAY_ANALYZER_WATCHED_FOLDERS"])
                )
            },
        ).settings
        resolved_video = VideoRequestResolver(factory, settings).resolve(
            {
                "diagnostic_preview": True,
                "evidence_horizon": "partial",
                "logic_frames_per_second": 30,
                "replay_public_id": manifest.replay_public_id,
                "replay_sha256": manifest.input_replay_sha256,
                "report_public_id": manifest.replay_report.report_public_id,
            }
        )
        assert resolved_video.replay_path == (
            settings.data_root
            / "replays"
            / manifest.input_replay_sha256[:2]
            / manifest.input_replay_sha256
        )
        with factory() as session:
            pending = session.scalar(
                select(Job).where(Job.public_id == manifest.pending_job.job_public_id)
            )
            assert pending is not None and isinstance(pending.input_json, dict)
            assert pending.stage == "discover"
            assert pending.input_json["request_telemetry"] is False
            completed_discoveries = tuple(
                session.scalars(
                    select(Job).where(
                        Job.stage == "discover",
                        Job.public_id != pending.public_id,
                    )
                )
            )
            assert any(
                isinstance(job.input_json, dict)
                and job.input_json.get("request_telemetry") is True
                for job in completed_discoveries
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(TelemetryRun)
                    .where(TelemetryRun.status == "succeeded")
                )
                == 1
            )
    finally:
        engine.dispose()


def test_builder_rejects_an_environment_root_outside_its_runtime_root(tmp_path: Path) -> None:
    runtime_root = tmp_path / "external-runtime"
    environment = _environment(runtime_root)
    environment["GENERALS_REPLAY_ANALYZER_DATA_ROOT"] = str(tmp_path / "sibling-data")

    try:
        build_populated_fixture(runtime_root, environment, project_root=Path(__file__).parents[2])
    except ValueError as error:
        assert "inside runtime_root" in str(error)
    else:
        raise AssertionError("builder accepted a data root outside its runtime root")


def test_fresh_process_loads_the_exact_json_encoded_watched_root(tmp_path: Path) -> None:
    runtime_root = tmp_path / "external-runtime"
    source_root = runtime_root / "fixture-input"
    source_root.mkdir(parents=True)
    environment = {
        **os.environ,
        **_environment(runtime_root),
        "GENERALS_REPLAY_ANALYZER_WATCHED_FOLDERS": json.dumps([str(source_root)]),
    }
    command = [
        sys.executable,
        "-c",
        (
            "import json; "
            "from generals_replay_analyzer.cli import _runtime_configuration; "
            "from generals_replay_analyzer.watching import WatchedRootRegistry; "
            "runtime = _runtime_configuration(); "
            "roots = WatchedRootRegistry(runtime.settings.data_root).reconcile("
            "runtime.settings.watched_folders); "
            "print(json.dumps({'root_public_id': roots[0].root_public_id, "
            "'available': roots[0].available, 'count': len(roots)}))"
        ),
    ]

    first = subprocess.run(
        command,
        cwd=Path(__file__).parents[2],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    second = subprocess.run(
        command,
        cwd=Path(__file__).parents[2],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    first_payload = json.loads(first.stdout)
    assert first_payload == json.loads(second.stdout)
    assert first_payload["count"] == 1
    assert first_payload["available"] is True
    assert str(source_root.resolve()) not in first.stdout
