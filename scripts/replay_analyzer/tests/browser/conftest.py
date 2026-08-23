"""External installed-wheel server and deterministic Chromium fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from .fixture_data import clone_populated_runtime, isolated_runtime_environment, populated_runtime_environment
from .populated_fixture import PopulatedFixtureResult, build_populated_fixture
from .support import ExternalWorkerController, external_artifact_inventory, validate_external_artifact_root

PROJECT_ROOT = Path(__file__).parents[2]
WORKTREE_ROOT = PROJECT_ROOT.parents[1]
REPOSITORY_ROOT = WORKTREE_ROOT.parent.parent if WORKTREE_ROOT.parent.name == ".worktrees" else WORKTREE_ROOT


@dataclass(frozen=True, slots=True)
class WheelEnvironment:
    executable: Path
    module_location: Path
    wheel_sha256: str
    build_root: Path


@dataclass(frozen=True, slots=True)
class InstalledServer:
    origin: str
    executable: Path = field(repr=False)
    environment: dict[str, str] = field(repr=False)
    module_location: Path = field(repr=False)
    wheel_sha256: str
    runtime_root: Path = field(repr=False)
    stdout_log: Path = field(repr=False)
    stderr_log: Path = field(repr=False)


def _run(arguments: list[str], cwd: Path, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, cwd=cwd, env=environment, check=True, text=True, capture_output=True)


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _assert_installed_populated_configuration(
    wheel_environment: WheelEnvironment,
    runtime_root: Path,
    environment: dict[str, str],
) -> None:
    python = wheel_environment.executable.with_name("python.exe")
    script = (
        "import json; "
        "from generals_replay_analyzer.cli import _runtime_configuration; "
        "runtime = _runtime_configuration(); "
        "print(json.dumps({'minimum_sample_size': "
        "runtime.settings.minimum_longitudinal_sample_size, "
        "'settings_revision': runtime.snapshot.revision}, sort_keys=True))"
    )
    result = _run([str(python), "-c", script], runtime_root, environment)
    assert json.loads(result.stdout) == {"minimum_sample_size": 1, "settings_revision": 1}


@pytest.fixture(scope="session")
def browser_type_launch_args() -> dict[str, object]:
    """Keep the release browser off the user's saturated hardware GPU."""
    return {
        "headless": True,
        "args": ["--disable-gpu", "--disable-gpu-compositing", "--disable-gpu-rasterization"],
    }


@pytest.fixture(scope="session")
def wheel_environment(tmp_path_factory: pytest.TempPathFactory) -> WheelEnvironment:
    root = tmp_path_factory.mktemp("replay-analyzer-wheel")
    distribution = root / "dist"
    distribution.mkdir()
    uv = os.environ.get("UV", "uv")
    _run([uv, "build", "--project", str(PROJECT_ROOT), "--wheel", "--out-dir", str(distribution)], root)
    wheel = next(distribution.glob("generals_replay_analyzer-*.whl"))
    environment_root = root / "environment"
    _run([sys.executable, "-m", "venv", "--system-site-packages", str(environment_root)], root)
    python = environment_root / "Scripts" / "python.exe"
    executable = environment_root / "Scripts" / "replay-analyzer.exe"

    # Keep runtime dependencies frozen in the already locked test environment while
    # ensuring the application package itself resolves only from the installed wheel.
    dependency_site = Path(sys.prefix) / "Lib" / "site-packages"
    environment_site = environment_root / "Lib" / "site-packages"
    (environment_site / "task9-runtime-dependencies.pth").write_text(str(dependency_site), encoding="utf-8")
    _run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)], root)

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    result = _run(
        [str(python), "-c", "import generals_replay_analyzer as p; print(p.__file__ or '')"],
        root,
        environment,
    )
    module_location = Path(result.stdout.strip()).resolve()
    assert environment_root.resolve() in module_location.parents
    assert PROJECT_ROOT.resolve() not in module_location.parents
    return WheelEnvironment(
        executable=executable,
        module_location=module_location,
        wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
        build_root=root,
    )


@contextmanager
def _launch_installed_server(
    wheel_environment: WheelEnvironment,
    runtime_root: Path,
    environment: dict[str, str],
) -> Iterator[InstalledServer]:
    port = _reserve_loopback_port()
    origin = f"http://127.0.0.1:{port}"
    stdout_log = runtime_root / "web.stdout.log"
    stderr_log = runtime_root / "web.stderr.log"
    with stdout_log.open("w", encoding="utf-8") as stdout, stderr_log.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            [str(wheel_environment.executable), "web", "--host", "127.0.0.1", "--port", str(port)],
            cwd=runtime_root,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        deadline = time.monotonic() + 20.0
        ready = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"{origin}/health/live", timeout=0.5) as live_response:
                    live = live_response.status == 200
                with urllib.request.urlopen(f"{origin}/health/ready", timeout=0.5) as ready_response:
                    application_ready = ready_response.status == 200
                ready = live and application_ready
                if ready:
                    break
            except (OSError, urllib.error.URLError):
                threading.Event().wait(0.05)
        if not ready:
            process.terminate()
            process.wait(timeout=10)
            pytest.fail("installed-wheel web server failed readiness; inspect external stderr artifact")
        server = InstalledServer(
            origin=origin,
            executable=wheel_environment.executable,
            environment=environment,
            module_location=wheel_environment.module_location,
            wheel_sha256=wheel_environment.wheel_sha256,
            runtime_root=runtime_root,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
        )
        try:
            yield server
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            assert process.poll() is not None


@pytest.fixture(scope="session")
def installed_server(
    wheel_environment: WheelEnvironment,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[InstalledServer]:
    runtime_root = tmp_path_factory.mktemp("replay-analyzer-runtime")
    environment = isolated_runtime_environment(runtime_root)
    with _launch_installed_server(wheel_environment, runtime_root, environment) as server:
        yield server


@pytest.fixture(scope="session")
def populated_fixture_template(tmp_path_factory: pytest.TempPathFactory) -> PopulatedFixtureResult:
    """Compose the accepted production-service fixture once outside every protected tree."""
    runtime_root = tmp_path_factory.mktemp("replay-analyzer-populated-template")
    environment = isolated_runtime_environment(runtime_root)
    return build_populated_fixture(runtime_root, environment, project_root=PROJECT_ROOT)


@pytest.fixture(scope="session")
def populated_server(
    wheel_environment: WheelEnvironment,
    populated_fixture_template: PopulatedFixtureResult,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[InstalledServer]:
    """Launch the installed wheel against one read-only populated runtime clone."""
    runtime_root = tmp_path_factory.mktemp("replay-analyzer-populated-runtime")
    clone_populated_runtime(populated_fixture_template.runtime_root, runtime_root)
    environment = populated_runtime_environment(runtime_root)
    _assert_installed_populated_configuration(wheel_environment, runtime_root, environment)
    with _launch_installed_server(wheel_environment, runtime_root, environment) as server:
        yield server


@pytest.fixture
def mutable_populated_server(
    wheel_environment: WheelEnvironment,
    populated_fixture_template: PopulatedFixtureResult,
    tmp_path: Path,
) -> Iterator[InstalledServer]:
    """Launch a fresh installed-wheel clone for one browser mutation flow."""
    runtime_root = tmp_path / "populated-runtime"
    clone_populated_runtime(populated_fixture_template.runtime_root, runtime_root)
    environment = populated_runtime_environment(runtime_root)
    _assert_installed_populated_configuration(wheel_environment, runtime_root, environment)
    with _launch_installed_server(wheel_environment, runtime_root, environment) as server:
        yield server


@pytest.fixture
def worker_controller(installed_server: InstalledServer) -> Iterator[ExternalWorkerController]:
    """Expose an opt-in installed worker that shares the server's isolated runtime."""
    controller = ExternalWorkerController(
        installed_server.executable,
        installed_server.runtime_root,
        installed_server.environment,
    )
    try:
        yield controller
    finally:
        controller.stop()


@pytest.fixture
def populated_worker_controller(
    mutable_populated_server: InstalledServer,
) -> Iterator[ExternalWorkerController]:
    """Expose an opt-in installed worker for one fresh populated mutation clone."""
    controller = ExternalWorkerController(
        mutable_populated_server.executable,
        mutable_populated_server.runtime_root,
        mutable_populated_server.environment,
    )
    try:
        yield controller
    finally:
        controller.stop()


@pytest.fixture(scope="session")
def browser_artifact_root(
    tmp_path_factory: pytest.TempPathFactory,
    wheel_environment: WheelEnvironment,
) -> Iterator[Path]:
    configured = os.environ.get("REPLAY_ANALYZER_BROWSER_ARTIFACT_ROOT")
    selected = Path(configured) if configured else tmp_path_factory.mktemp("replay-analyzer-artifacts")
    base = validate_external_artifact_root(
        selected,
        WORKTREE_ROOT,
        REPOSITORY_ROOT,
        wheel_environment.build_root,
    )
    run = base / f"task9-{wheel_environment.wheel_sha256[:12]}"
    run.mkdir(parents=True, exist_ok=True)
    manifest_path = run / "run-manifest.json"
    manifest: dict[str, object] = {
        "application_wheel_sha256": wheel_environment.wheel_sha256,
        "artifacts": (),
        "installed_module_location_class": "isolated wheel environment",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    try:
        yield run
    finally:
        manifest["artifacts"] = external_artifact_inventory(run)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
