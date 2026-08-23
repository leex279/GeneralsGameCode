"""Shared, test-only helpers for the browser release gate."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.resources
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, TextIO
from urllib.parse import urlsplit

from playwright.sync_api import Page, Request, Route

EXPECTED_BROWSER_DISTRIBUTIONS: Final = {
    "axe-playwright-python": "0.1.8",
    "playwright": "1.61.0",
    "pytest-playwright": "0.8.0",
}


@dataclass(slots=True)
class ExternalWorkerController:
    """Own one installed-wheel worker process against the server's exact runtime."""

    executable: Path = field(repr=False)
    runtime_root: Path = field(repr=False)
    environment: dict[str, str] = field(repr=False)
    _process: subprocess.Popen[str] | None = field(default=None, init=False, repr=False)
    _stdout: TextIO | None = field(default=None, init=False, repr=False)
    _stderr: TextIO | None = field(default=None, init=False, repr=False)

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        if self.running:
            raise RuntimeError("fixture worker is already running")
        if self._process is not None:
            self.stop()
        self._stdout = (self.runtime_root / "worker.stdout.log").open("w", encoding="utf-8")
        self._stderr = (self.runtime_root / "worker.stderr.log").open("w", encoding="utf-8")
        try:
            self._process = subprocess.Popen(
                [
                    str(self.executable),
                    "worker",
                    "--poll-seconds",
                    "1",
                    "--lease-seconds",
                    "15",
                ],
                cwd=self.runtime_root,
                env=self.environment,
                stdout=self._stdout,
                stderr=self._stderr,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except BaseException:
            self._close_logs()
            raise

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        unexpected_code = process.poll()
        try:
            if unexpected_code is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            else:
                raise AssertionError("installed fixture worker exited unexpectedly; inspect external stderr artifact")
        finally:
            self._process = None
            self._close_logs()

    def _close_logs(self) -> None:
        for stream in (self._stdout, self._stderr):
            if stream is not None:
                stream.close()
        self._stdout = None
        self._stderr = None


def browser_distribution_versions() -> dict[str, str]:
    """Return exact installed versions, failing when a release dependency is absent."""
    return {name: importlib.metadata.version(name) for name in EXPECTED_BROWSER_DISTRIBUTIONS}


def local_axe_script() -> bytes:
    """Resolve the pinned test-only Axe payload through package resources."""
    resource = importlib.resources.files("axe_playwright_python").joinpath("axe.min.js")
    payload = resource.read_bytes()
    if not payload:
        raise AssertionError("bundled axe.min.js is empty")
    return payload


def packaged_web_sources(project_root: Path) -> dict[str, bytes]:
    """Read every accepted template/static resource, recursively and deterministically."""
    web_root = project_root / "src" / "generals_replay_analyzer" / "web"
    resources: dict[str, bytes] = {}
    for relative_root in (Path("templates"), Path("static")):
        for path in sorted((web_root / relative_root).rglob("*")):
            if path.is_file():
                relative = path.relative_to(web_root).as_posix()
                resources[f"generals_replay_analyzer/web/{relative}"] = path.read_bytes()
    return resources


def assert_vendor_manifest(project_root: Path) -> None:
    """Verify every manifest digest against the frozen package-local vendor bytes."""
    vendor_root = project_root / "src" / "generals_replay_analyzer" / "web" / "static" / "vendor"
    manifest = json.loads((vendor_root / "vendor-manifest.json").read_text(encoding="utf-8"))
    for record in manifest["assets"]:
        payload = (vendor_root / record["filename"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == record["sha256"]


def install_same_origin_guard(page: Page, origin: str) -> list[str]:
    """Record failed local transport and abort every request outside the exact origin."""
    failures: list[str] = []

    def guard(route: Route) -> None:
        parsed = urlsplit(route.request.url)
        request_origin = f"{parsed.scheme}://{parsed.netloc}"
        if parsed.scheme in {"http", "https", "ws", "wss"} and request_origin != origin:
            failures.append(route.request.url)
            route.abort("blockedbyclient")
            return
        route.continue_()

    def record_failed_request(request: Request) -> None:
        parsed = urlsplit(request.url)
        if parsed.scheme in {"http", "https", "ws", "wss"}:
            failures.append(request.url)

    page.route("**/*", guard)
    page.on("requestfailed", record_failed_request)
    return failures


def validate_external_artifact_root(base: Path, *protected_roots: Path) -> Path:
    """Reject an artifact root contained by any checkout or release build root."""
    resolved = base.resolve()
    if not protected_roots:
        raise ValueError("at least one protected root is required")
    for protected_root in protected_roots:
        protected = protected_root.resolve()
        if resolved == protected or protected in resolved.parents:
            raise ValueError("browser artifact root must be external to protected release roots")
    return resolved


def external_artifact_inventory(root: Path) -> tuple[dict[str, str | int], ...]:
    """Digest external evidence without recording its absolute storage location."""
    inventory: list[dict[str, str | int]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path == root / "run-manifest.json":
            continue
        inventory.append(
            {
                "logical_name": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
            }
        )
    return tuple(inventory)


def deterministic_context_options(
    viewport: dict[str, int], *, java_script_enabled: bool = True
) -> dict[str, object]:
    """Return the one frozen browser context used by every release flow."""
    return {
        "accept_downloads": False,
        "color_scheme": "light",
        "device_scale_factor": 1,
        "java_script_enabled": java_script_enabled,
        "locale": "en-US",
        "reduced_motion": "reduce",
        "service_workers": "block",
        "timezone_id": "UTC",
        "viewport": viewport,
    }


def install_browser_error_guard(page: Page) -> tuple[list[str], list[str]]:
    """Capture browser errors so every flow can fail closed after assertions."""
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    return console_errors, page_errors


def assert_browser_clean(console_errors: list[str], page_errors: list[str]) -> None:
    assert console_errors == []
    assert page_errors == []
