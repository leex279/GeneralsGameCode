"""Installed-wheel smoke tests for replay-analyzer package data."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import textwrap
import tomllib
import zipfile
from pathlib import Path

from map_asset_support import write_test_map_asset

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "zero_hour_1_04" / "leex279_vs_fox27.rep"
PROJECT_ROOT = Path(__file__).parents[1]

MIGRATION_RESOURCES = {
    "generals_replay_analyzer/db/migrations/env.py",
    "generals_replay_analyzer/db/migrations/script.py.mako",
    "generals_replay_analyzer/db/migrations/versions/0001_replay_analyzer_v2.py",
    "generals_replay_analyzer/db/migrations/versions/0002_player_identity_audit.py",
    "generals_replay_analyzer/db/migrations/versions/0003_feature_partial_quality.py",
    "generals_replay_analyzer/db/migrations/versions/0004_job_lifecycle.py",
    "generals_replay_analyzer/db/migrations/versions/0005_llm_graph_immutability.py",
}
LLM_RESOURCES = {
    "generals_replay_analyzer/data/strategy-report-v1.txt",
    "generals_replay_analyzer/data/strategy-report-response-v1.schema.json",
    "generals_replay_analyzer/data/strategy-taxonomy-v1.json",
    "generals_replay_analyzer/data/strategy-taxonomy-v1.schema.json",
}
REPORT_RESOURCES = {
    "generals_replay_analyzer/data/replay-report-v1.html",
    "generals_replay_analyzer/data/replay-report-v1.schema.json",
}
DATA_RESOURCES = LLM_RESOURCES | REPORT_RESOURCES | {
    "generals_replay_analyzer/data/zero_hour_1_04_message_types.json",
    "generals_replay_analyzer/data/telemetry-v1.schema.json",
    "generals_replay_analyzer/data/telemetry-v2.schema.json",
    "generals_replay_analyzer/data/game-data-catalog-v1.schema.json",
    "generals_replay_analyzer/data/map-asset-v1.schema.json",
    "generals_replay_analyzer/data/map-asset-v2.schema.json",
    "generals_replay_analyzer/data/zero-hour-combat-types-v1.json",
}
WEB_BOUNDARY_RESOURCES = {
    "generals_replay_analyzer/web/app.py",
    "generals_replay_analyzer/web/resources.py",
    "generals_replay_analyzer/web/routes/dashboard.py",
    "generals_replay_analyzer/web/routes/identity.py",
}
WEB_SHELL_RESOURCES = {
    "generals_replay_analyzer/web/presentation/__init__.py",
    "generals_replay_analyzer/web/presentation/shell.py",
    "generals_replay_analyzer/web/templates/base.html",
    "generals_replay_analyzer/web/templates/dashboard.html",
    "generals_replay_analyzer/web/templates/identity/landing.html",
    "generals_replay_analyzer/web/templates/components/badges.html",
    "generals_replay_analyzer/web/templates/components/empty_state.html",
    "generals_replay_analyzer/web/templates/components/error_panel.html",
    "generals_replay_analyzer/web/templates/components/metric.html",
    "generals_replay_analyzer/web/templates/components/pagination.html",
    "generals_replay_analyzer/web/templates/components/pipeline.html",
    "generals_replay_analyzer/web/templates/components/evidence_drawer.html",
    "generals_replay_analyzer/web/static/css/app.css",
    "generals_replay_analyzer/web/static/js/app.js",
    "generals_replay_analyzer/web/static/vendor/htmx.min.js",
    "generals_replay_analyzer/web/static/vendor/echarts.min.js",
    "generals_replay_analyzer/web/static/vendor/vendor-manifest.json",
    "generals_replay_analyzer/web/static/vendor/THIRD_PARTY_LICENSES.md",
}
WEB_LIBRARY_TEMPLATE_RESOURCES = {
    "generals_replay_analyzer/web/templates/replays/index.html",
    "generals_replay_analyzer/web/templates/replays/_table.html",
    "generals_replay_analyzer/web/templates/imports/dialog.html",
}
WEB_LIBRARY_SOURCE_RESOURCES = {
    "generals_replay_analyzer/web/routes/replays.py",
    "generals_replay_analyzer/web/routes/imports.py",
    "generals_replay_analyzer/web/viewmodels/__init__.py",
    "generals_replay_analyzer/web/viewmodels/replays.py",
}
WEB_LIBRARY_RESOURCES = WEB_LIBRARY_TEMPLATE_RESOURCES | WEB_LIBRARY_SOURCE_RESOURCES
WEB_JOB_TEMPLATE_RESOURCES = {
    "generals_replay_analyzer/web/templates/jobs/index.html",
    "generals_replay_analyzer/web/templates/jobs/_rows.html",
    "generals_replay_analyzer/web/templates/jobs/detail.html",
    "generals_replay_analyzer/web/templates/jobs/_log.html",
}
WEB_JOB_SOURCE_RESOURCES = {
    "generals_replay_analyzer/worker.py",
    "generals_replay_analyzer/watching/__init__.py",
    "generals_replay_analyzer/watching/service.py",
    "generals_replay_analyzer/watching/adapters.py",
    "generals_replay_analyzer/watching/status.py",
    "generals_replay_analyzer/web/adapters/__init__.py",
    "generals_replay_analyzer/web/adapters/analytics.py",
    "generals_replay_analyzer/web/routes/jobs.py",
    "generals_replay_analyzer/web/viewmodels/jobs.py",
}
WEB_JOB_RESOURCES = WEB_JOB_TEMPLATE_RESOURCES | WEB_JOB_SOURCE_RESOURCES
WEB_PACKAGED_TEMPLATE_STATIC_RESOURCES = WEB_SHELL_RESOURCES | WEB_LIBRARY_TEMPLATE_RESOURCES | WEB_JOB_TEMPLATE_RESOURCES


def _source_resource(resource_name: str) -> Path:
    return PROJECT_ROOT / "src" / resource_name


def test_wheel_configuration_explicitly_includes_only_web_templates_and_static_resources() -> None:
    configuration = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = configuration["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    web_force_includes = {
        destination
        for source, destination in force_include.items()
        if source.startswith("src/generals_replay_analyzer/web/")
    }
    assert web_force_includes == WEB_PACKAGED_TEMPLATE_STATIC_RESOURCES - {
        "generals_replay_analyzer/web/presentation/__init__.py",
        "generals_replay_analyzer/web/presentation/shell.py",
    }
    assert all(Path(source).suffix for source in force_include)
    assert not any(
        "*" in source or ".task" in source or "cache" in source or "secret" in source for source in force_include
    )
    assert "src/generals_replay_analyzer/data/**" in configuration["tool"]["hatch"]["build"]["targets"]["wheel"][
        "exclude"
    ]
    assert {
        destination for destination in force_include.values() if destination.startswith("generals_replay_analyzer/data/")
    } == DATA_RESOURCES


def test_wheel_data_resource_allow_list_excludes_temporary_poison_files(tmp_path: Path) -> None:
    """The wheel contains exactly pinned data resources even when source data is poisoned."""
    uv = shutil.which("uv")
    assert uv is not None
    data_directory = PROJECT_ROOT / "src" / "generals_replay_analyzer" / "data"
    poisons = (data_directory / ".cache-secret", data_directory / "unexpected.json")
    for poison in poisons:
        poison.write_bytes(b"not-package-data")
    try:
        distribution_directory = tmp_path / "dist"
        _run([uv, "build", "--wheel", "--out-dir", str(distribution_directory)], PROJECT_ROOT)
        wheel = next(distribution_directory.glob("generals_replay_analyzer-*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            packaged = {
                name for name in archive.namelist() if name.startswith("generals_replay_analyzer/data/")
            }
            assert packaged == DATA_RESOURCES
            assert not any("cache" in name.lower() or "secret" in name.lower() for name in archive.namelist())
    finally:
        for poison in poisons:
            poison.unlink(missing_ok=True)


def test_wheel_web_resource_allow_list_excludes_a_temporary_poison_file(tmp_path: Path) -> None:
    """The wheel must never absorb an unlisted cache or secret placed below web assets."""
    uv = shutil.which("uv")
    assert uv is not None
    poison = PROJECT_ROOT / "src" / "generals_replay_analyzer" / "web" / "static" / "vendor" / ".cache-secret"
    poison.write_bytes(b"not-package-data")
    try:
        distribution_directory = tmp_path / "dist"
        _run([uv, "build", "--wheel", "--out-dir", str(distribution_directory)], PROJECT_ROOT)
        wheel = next(distribution_directory.glob("generals_replay_analyzer-*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            web_resources = {
                name
                for name in archive.namelist()
                if name.startswith(("generals_replay_analyzer/web/templates/", "generals_replay_analyzer/web/static/"))
            }
            assert web_resources == WEB_PACKAGED_TEMPLATE_STATIC_RESOURCES - {
                "generals_replay_analyzer/web/presentation/__init__.py",
                "generals_replay_analyzer/web/presentation/shell.py",
            }
            assert "generals_replay_analyzer/web/static/vendor/.cache-secret" not in archive.namelist()
            assert not any("cache" in name.lower() or "secret" in name.lower() for name in archive.namelist())
    finally:
        poison.unlink(missing_ok=True)


def _run(
    arguments: list[str], working_directory: Path, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one isolated wheel-install command while retaining useful failure output."""
    return subprocess.run(arguments, check=True, cwd=working_directory, text=True, capture_output=True, env=environment)


def test_installed_wheel_contains_and_executes_packaged_migrations(tmp_path: Path) -> None:
    """Reject a wheel whose Alembic baseline depends on checkout files or in-memory SQLite."""
    uv = shutil.which("uv")
    assert uv is not None
    distribution_directory = tmp_path / "dist"
    _run([uv, "build", "--wheel", "--out-dir", str(distribution_directory)], PROJECT_ROOT)
    wheel = next(distribution_directory.glob("generals_replay_analyzer-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert MIGRATION_RESOURCES | LLM_RESOURCES <= set(archive.namelist())
        assert WEB_BOUNDARY_RESOURCES <= set(archive.namelist())
        for resource in MIGRATION_RESOURCES | LLM_RESOURCES:
            assert archive.read(resource) == _source_resource(resource).read_bytes()

    environment_directory = tmp_path / "migration-wheel-environment"
    _run([sys.executable, "-m", "venv", str(environment_directory)], tmp_path)
    environment_python = environment_directory / "Scripts" / "python.exe"
    _run([str(environment_python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)], tmp_path)
    database_path = tmp_path / "wheel-library.sqlite3"
    migration_script = textwrap.dedent(
        """
        import hashlib
        import os
        import sqlite3
        from pathlib import Path

        import generals_replay_analyzer
        from generals_replay_analyzer.db import downgrade_database, upgrade_database
        from generals_replay_analyzer.importing.job_contracts import JobState, WorkerLeaseDTO
        from generals_replay_analyzer.llm import (
            AnalysisOutcome,
            AnalysisRequest,
            DeterministicFallback,
            HttpxOllamaTransport,
            OllamaAnalysisService,
        )
        from generals_replay_analyzer.llm.schema import load_prompt, load_response_schema
        from generals_replay_analyzer.report.resources import load_report_resources
        from generals_replay_analyzer.web.resources import PackagedResourceError, package_resource

        database = Path(os.environ["TEST_DATABASE_PATH"])
        assert "migration-wheel-environment" in str(generals_replay_analyzer.__file__)
        assert tuple(state.value for state in JobState) == ("pending", "running", "succeeded", "failed", "cancelled")
        assert WorkerLeaseDTO.__module__ == "generals_replay_analyzer.importing.job_contracts"
        assert all((AnalysisOutcome, AnalysisRequest, DeterministicFallback, HttpxOllamaTransport, OllamaAnalysisService))
        assert hashlib.sha256(load_prompt().content).hexdigest() == os.environ["TEST_PROMPT_SHA256"]
        assert hashlib.sha256(load_response_schema().content).hexdigest() == os.environ["TEST_RESPONSE_SCHEMA_SHA256"]
        report_resources = load_report_resources()
        assert report_resources.html_template_sha256 == os.environ["TEST_REPORT_TEMPLATE_SHA256"]
        assert report_resources.document_schema_sha256 == os.environ["TEST_REPORT_SCHEMA_SHA256"]
        assert package_resource("db/migrations/env.py").is_file()
        try:
            package_resource("web/templates/not-created-by-task-1.html")
        except PackagedResourceError as error:
            assert str(error) == "packaged resource is unavailable"
        else:
            raise AssertionError("missing packaged resource did not produce a controlled error")
        upgrade_database(database)
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "0005_llm_graph_immutability",
            )
            assert {
                row[1]
                for row in connection.execute("PRAGMA table_info(job_log_snapshots)").fetchall()
            } >= {
                "byte_count",
                "integrity_version",
                "integrity_root_sha256",
                "integrity_chunk_size",
            }
            assert connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'player_identity_operations'"
            ).fetchone() == ("player_identity_operations",)
            first = connection.execute(
                "SELECT type, name, COALESCE(sql, '') FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        downgrade_database(database)
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
            ).fetchall() == []
        upgrade_database(database)
        with sqlite3.connect(database) as connection:
            second = connection.execute(
                "SELECT type, name, COALESCE(sql, '') FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        assert second == first
        """
    )
    environment = os.environ.copy()
    environment["TEST_DATABASE_PATH"] = str(database_path)
    environment["TEST_PROMPT_SHA256"] = hashlib.sha256(
        _source_resource("generals_replay_analyzer/data/strategy-report-v1.txt").read_bytes()
    ).hexdigest()
    environment["TEST_RESPONSE_SCHEMA_SHA256"] = hashlib.sha256(
        _source_resource("generals_replay_analyzer/data/strategy-report-response-v1.schema.json").read_bytes()
    ).hexdigest()
    environment["TEST_REPORT_TEMPLATE_SHA256"] = hashlib.sha256(
        _source_resource("generals_replay_analyzer/data/replay-report-v1.html").read_bytes()
    ).hexdigest()
    environment["TEST_REPORT_SCHEMA_SHA256"] = hashlib.sha256(
        _source_resource("generals_replay_analyzer/data/replay-report-v1.schema.json").read_bytes()
    ).hexdigest()
    environment["PYTHONPATH"] = str(Path(sysconfig.get_paths()["purelib"]))
    result = _run([str(environment_python), "-c", migration_script], tmp_path, environment)
    assert result.returncode == 0


def test_installed_wheel_renders_package_owned_shell_and_local_assets(tmp_path: Path) -> None:
    """Reject a wheel that needs checkout templates, assets, or network-loaded vendors."""
    uv = shutil.which("uv")
    assert uv is not None
    distribution_directory = tmp_path / "dist"
    _run([uv, "build", "--wheel", "--out-dir", str(distribution_directory)], PROJECT_ROOT)
    wheel = next(distribution_directory.glob("generals_replay_analyzer-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert WEB_SHELL_RESOURCES | WEB_LIBRARY_RESOURCES | WEB_JOB_RESOURCES <= set(archive.namelist())
        for resource_name in WEB_SHELL_RESOURCES | WEB_LIBRARY_RESOURCES | WEB_JOB_RESOURCES:
            assert archive.read(resource_name) == _source_resource(resource_name).read_bytes()

    environment_directory = tmp_path / "shell-wheel-environment"
    _run([sys.executable, "-m", "venv", str(environment_directory)], tmp_path)
    environment_python = environment_directory / "Scripts" / "python.exe"
    _run([str(environment_python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)], tmp_path)
    shell_script = textwrap.dedent(
        """
        import hashlib
        import json
        import os

        from fastapi.testclient import TestClient

        import generals_replay_analyzer
        from generals_replay_analyzer.web.app import create_app
        from generals_replay_analyzer.web.ports import (
            AvailabilityDTO,
            DashboardDTO,
            IdentityLandingDTO,
            ImportSubmissionDTO,
            JobPageDTO,
            ReadinessDTO,
            ReplayLibraryPageDTO,
        )
        from generals_replay_analyzer.web.resources import package_resource
        from generals_replay_analyzer.watching import WatchScheduler
        from generals_replay_analyzer.worker import WorkerRuntime

        assert WorkerRuntime.__module__ == "generals_replay_analyzer.worker"
        assert WatchScheduler.__module__ == "generals_replay_analyzer.watching.service"

        class Port:
            def readiness(self):
                return ReadinessDTO(ready=True, schema_revision="wheel")
            def dashboard(self):
                from datetime import UTC, datetime
                return DashboardDTO(generated_at=datetime(2026, 8, 22, 12, 0, tzinfo=UTC), availability=AvailabilityDTO(state="unavailable", reason_codes=("wheel_fixture",)))
            def identity_landing(self):
                from datetime import UTC, datetime
                return IdentityLandingDTO(generated_at=datetime(2026, 8, 22, 12, 0, tzinfo=UTC), availability=AvailabilityDTO(state="unavailable", reason_codes=("wheel_fixture",)))
            def list_replays(self, query):
                return ReplayLibraryPageDTO(query=query, items=(), page=query.page, page_size=query.page_size, total_items=0, availability=AvailabilityDTO(state="unavailable", reason_codes=("wheel_fixture",)))
            def import_roots(self):
                return ()
            def submit_upload(self, command):
                return ImportSubmissionDTO(submission_public_id="123e4567-e89b-42d3-a456-426614174020", availability=AvailabilityDTO(state="unavailable", reason_codes=("wheel_fixture",)), problem_code="opaque_ingress_handoff_pending")
            def submit_root_selection(self, command):
                return ImportSubmissionDTO(submission_public_id="123e4567-e89b-42d3-a456-426614174021", availability=AvailabilityDTO(state="unavailable", reason_codes=("wheel_fixture",)), problem_code="dependency_unavailable")
            def list_jobs(self, query):
                return JobPageDTO(query=query, items=(), availability=AvailabilityDTO(state="unavailable", reason_codes=("wheel_fixture",)))
        class Factory:
            def __enter__(self): return Port()
            def __exit__(self, *args): return None
        class Bootstrapper:
            def prepare(self, settings): return None

        assert "shell-wheel-environment" in str(generals_replay_analyzer.__file__)
        expected_hashes = json.loads(os.environ["WHEEL_WEB_RESOURCE_HASHES"])
        for resource_name, expected_hash in expected_hashes.items():
            resource = package_resource(resource_name.removeprefix("generals_replay_analyzer/"))
            assert hashlib.sha256(resource.read_bytes()).hexdigest() == expected_hash
        manifest = json.loads(package_resource("web/static/vendor/vendor-manifest.json").read_text(encoding="utf-8"))
        with TestClient(create_app(object(), port_factory=lambda: Factory(), bootstrapper=Bootstrapper())) as client:
            for path, heading in (("/", "Replay dashboard"), ("/players", "Player identity")):
                response = client.get(path, headers={"host": "localhost", "accept": "text/html"})
                assert response.status_code == 200
                assert heading in response.text
                assert 'src="/static/vendor/htmx.min.js"' not in response.text
                assert 'src="/static/vendor/echarts.min.js"' not in response.text
            library = client.get("/replays", headers={"host": "localhost", "accept": "text/html"})
            assert library.status_code == 200
            assert "Replay library" in library.text
            assert "wheel_fixture" in library.text
            dialog = client.get("/imports/dialog", headers={"host": "localhost", "accept": "text/html"})
            assert dialog.status_code == 200
            assert "Import replay" in dialog.text
            jobs = client.get("/jobs", headers={"host": "localhost", "accept": "text/html"})
            assert jobs.status_code == 200
            assert "Analysis jobs" in jobs.text
            assert "wheel_fixture" in jobs.text
            stylesheet = client.get("/static/css/app.css", headers={"host": "localhost"})
            assert stylesheet.status_code == 200
            assert stylesheet.headers["content-security-policy"]
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT_ROOT / ".venv" / "Lib" / "site-packages")
    environment["WHEEL_WEB_RESOURCE_HASHES"] = json.dumps(
        {
            resource: hashlib.sha256(_source_resource(resource).read_bytes()).hexdigest()
            for resource in WEB_SHELL_RESOURCES | WEB_LIBRARY_RESOURCES | WEB_JOB_RESOURCES
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    result = _run([str(environment_python), "-c", shell_script], tmp_path, environment)
    assert result.returncode == 0


def test_installed_wheel_loads_catalog_for_symbolic_lookup_and_inspection(tmp_path: Path) -> None:
    """Reject a wheel that works only because a source-checkout contracts directory is nearby."""
    uv = shutil.which("uv")
    assert uv is not None
    distribution_directory = tmp_path / "dist"
    _run([uv, "build", "--wheel", "--out-dir", str(distribution_directory)], PROJECT_ROOT)
    wheel = next(distribution_directory.glob("generals_replay_analyzer-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        packaged_v1_schema = archive.read("generals_replay_analyzer/data/telemetry-v1.schema.json")
        packaged_v2_schema = archive.read("generals_replay_analyzer/data/telemetry-v2.schema.json")
        packaged_map_v1_schema = archive.read("generals_replay_analyzer/data/map-asset-v1.schema.json")
        packaged_map_v2_schema = archive.read("generals_replay_analyzer/data/map-asset-v2.schema.json")
        packaged_combat_types = archive.read("generals_replay_analyzer/data/zero-hour-combat-types-v1.json")
    assert packaged_v1_schema == (PROJECT_ROOT / "contracts" / "telemetry-v1.schema.json").read_bytes()
    assert packaged_v2_schema == (PROJECT_ROOT / "contracts" / "telemetry-v2.schema.json").read_bytes()
    assert packaged_map_v1_schema == (PROJECT_ROOT / "contracts" / "map-asset-v1.schema.json").read_bytes()
    assert packaged_map_v2_schema == (PROJECT_ROOT / "contracts" / "map-asset-v2.schema.json").read_bytes()
    assert packaged_combat_types == (PROJECT_ROOT / "contracts" / "zero-hour-combat-types-v1.json").read_bytes()

    environment_directory = tmp_path / "wheel-environment"
    _run([sys.executable, "-m", "venv", str(environment_directory)], tmp_path)
    environment_python = environment_directory / "Scripts" / "python.exe"
    _run([str(environment_python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)], tmp_path)

    lookup = _run(
        [
            str(environment_python),
            "-c",
            "from generals_replay_analyzer.contracts import message_name_for; print(message_name_for(1001))",
        ],
        tmp_path,
    )
    assert lookup.stdout.strip() == "MSG_CREATE_SELECTED_GROUP"

    executable = environment_directory / "Scripts" / "replay-analyzer.exe"
    export_help = _run([str(executable), "export-telemetry", "--help"], tmp_path)
    assert "--engine ENGINE" in export_help.stdout
    assert "--movement-sample-frames" in export_help.stdout
    analyze_help = _run([str(executable), "analyze", "--help"], tmp_path)
    assert "--execute" in analyze_help.stdout
    assert "--allow-ollama" in analyze_help.stdout
    assert "--json" in analyze_help.stdout
    inspection = _run([str(executable), "inspect", str(FIXTURE_PATH), "--format", "json"], tmp_path)
    output = json.loads(inspection.stdout)
    assert output["command_stream_offset"] == 342
    assert output["completion_status"] == "complete"

    wheel_environment = os.environ.copy()
    wheel_environment["PYTHONPATH"] = str(PROJECT_ROOT / ".venv" / "Lib" / "site-packages")
    telemetry_directory = tmp_path / "wheel-telemetry"
    telemetry_directory.mkdir()
    map_reference = write_test_map_asset(
        telemetry_directory,
        "test",
        "test.map",
        start_positions=[],
    )
    wheel_environment["TEST_TELEMETRY_DIRECTORY"] = str(telemetry_directory)
    wheel_environment["TEST_MAP_REFERENCE"] = json.dumps(map_reference, separators=(",", ":"))
    telemetry_script = textwrap.dedent(
        """
        import hashlib
        import json
        import os
        from pathlib import Path

        from generals_replay_analyzer.telemetry.order_coverage import canonical_order_coverage
        from generals_replay_analyzer.telemetry.reader import iter_validated_trace

        directory = Path(os.environ["TEST_TELEMETRY_DIRECTORY"])
        map_reference = json.loads(os.environ["TEST_MAP_REFERENCE"])
        catalog = {
            "schema_version": 1,
            "type": "game_data_catalog",
            "engine_data_identity": "test",
            "weapon_scope": "referenced_by_thing_templates",
            "locomotor_scope": "referenced_by_thing_templates",
            "thing_templates": [],
            "upgrades": [],
            "sciences": [],
            "weapons": [],
            "locomotors": [],
        }
        catalog_bytes = (json.dumps(catalog, separators=(",", ":")) + "\\n").encode()
        catalog_sha256 = hashlib.sha256(catalog_bytes).hexdigest()
        catalog_name = f"game-data-catalog-v1-{catalog_sha256}.json"
        (directory / catalog_name).write_bytes(catalog_bytes)
        reference = {
            "type": "game_data_catalog",
            "path": catalog_name,
            "sha256": catalog_sha256,
            "engine_data_identity": "test",
        }
        manifest = {
            "schema_version": 2,
            "run_id": "123e4567-e89b-12d3-a456-426614174000",
            "sequence": 0,
            "frame": 0,
            "logic_time_seconds": 0.0,
            "event_type": "manifest",
            "payload": {
                "engine_build": "test",
                "replay_version": "1.04",
                "map_identity": "test.map",
                "initial_seed": 1,
                "exporter_settings": {
                    "movement_sample_frames": 15,
                    "audio_enabled": False,
                    "order_coverage": canonical_order_coverage(),
                },
                "game_data_catalog": reference,
                "map_asset": map_reference,
            },
        }
        slots = [
            {
                "slot_index": index,
                "slot_state": "open",
                "occupied": False,
                "resolution_status": "not_applicable",
                "replay_name": None,
                "player_index": None,
                "team_id": None,
                "faction_template_name": None,
                "color": None,
                "start_position_status": "not_applicable",
                "start_position": None,
                "controller": None,
                "is_human": False,
                "is_header_local_slot": False,
                "is_resolved_local_player": None,
            }
            for index in range(8)
        ]
        players = {
            **manifest,
            "sequence": 1,
            "event_type": "players_initialized",
            "payload": {
                "header_local_slot_index": None,
                "slots": slots,
                "engine_player_indices": [0],
                "game_data_catalog": reference,
            },
        }
        outcome = {
            **manifest,
            "sequence": 2,
            "event_type": "match_outcome",
            "payload": {
                "status": "unknown",
                "source": "unavailable",
                "winner_player_indices": [],
                "loser_player_indices": [],
                "engine_player_indices": [0],
                "terminal_reason": "clean_completion",
                "quit_early": False,
                "replay_header_desync": False,
                "replay_header_disconnected_slots": [],
                "crc_mismatch": False,
                "crc_mismatch_frame": None,
                "clean_shutdown": True,
            },
        }
        prior = b"".join(
            (json.dumps(record, separators=(",", ":")) + "\\n").encode()
            for record in [manifest, players, outcome]
        )
        complete = {
            **manifest,
            "sequence": 3,
            "event_type": "complete",
            "payload": {
                "final_frame": 0,
                "command_count": 0,
                "event_counts": {
                    "manifest": 1,
                    "players_initialized": 1,
                    "match_outcome": 1,
                    "complete": 1,
                },
                "terminal_reason": "clean_completion",
                "crc_mismatch": False,
                "crc_mismatch_frame": None,
                "replay_truncated": False,
                "quit_early": False,
                "replay_header_desync": False,
                "replay_header_disconnected_slots": [],
                "clean_shutdown": True,
                "writer_error": None,
                "trace_sha256": hashlib.sha256(prior).hexdigest(),
                "map_assets": [map_reference],
                "final_cash_balances": [{"player_index": 0, "has_money": False, "balance": None}],
            },
        }
        trace = directory / "trace.ndjson"
        trace.write_bytes(prior + (json.dumps(complete, separators=(",", ":")) + "\\n").encode())
        assert [record.event_type for record in iter_validated_trace(trace)] == [
            "manifest",
            "players_initialized",
            "match_outcome",
            "complete",
        ]
        """
    )
    telemetry = _run([str(environment_python), "-c", telemetry_script], tmp_path, wheel_environment)
    assert telemetry.returncode == 0
