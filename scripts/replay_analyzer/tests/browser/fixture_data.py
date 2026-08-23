"""Deterministic external runtime configuration for installed-wheel browser tests."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from generals_replay_analyzer.watching.roots import _normalized_path_key

POPULATED_FIXTURE_MANIFEST = "populated-browser-fixture.json"


def clone_populated_runtime(template_root: Path, runtime_root: Path) -> Path:
    """Copy only closed fixture data needed by an installed server or worker."""
    template = template_root.resolve()
    runtime = runtime_root.resolve()
    if template == runtime or template in runtime.parents or runtime in template.parents:
        raise ValueError("fixture template and runtime roots must be independent")
    if runtime.exists() and any(runtime.iterdir()):
        raise ValueError("fresh fixture runtime root must be empty")

    data = template / "product-data"
    local_app_data = template / "local-app-data"
    fixture_input = template / "fixture-input"
    manifest = template / POPULATED_FIXTURE_MANIFEST
    if (
        not data.is_dir()
        or not local_app_data.is_dir()
        or not fixture_input.is_dir()
        or not manifest.is_file()
    ):
        raise FileNotFoundError("populated fixture template is incomplete")

    runtime.mkdir(parents=True, exist_ok=True)
    shutil.copytree(data, runtime / data.name)
    shutil.copytree(local_app_data, runtime / local_app_data.name)
    shutil.copytree(fixture_input, runtime / fixture_input.name)
    destination_manifest = runtime / manifest.name
    shutil.copy2(manifest, destination_manifest)
    registry_path = runtime / data.name / "watched-roots-v1.json"
    if registry_path.is_file():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        roots = registry.get("roots") if isinstance(registry, dict) else None
        if not isinstance(roots, list) or len(roots) != 1 or not isinstance(roots[0], dict):
            raise ValueError("populated fixture watched-root registry must contain exactly one root")
        # TheSuperHackers @fix Leex 23/08/2026 Preserve the durable root identity while rebinding an isolated clone path. (#TBD)
        roots[0]["path_key_sha256"] = _normalized_path_key(runtime / fixture_input.name)
        registry_path.write_text(
            json.dumps(registry, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return destination_manifest


def isolated_runtime_environment(runtime_root: Path) -> dict[str, str]:
    """Return a child-process environment rooted wholly outside the checkout."""
    data_root = runtime_root / "product-data"
    local_app_data = runtime_root / "local-app-data"
    roaming_app_data = runtime_root / "roaming-app-data"
    user_profile = runtime_root / "user-profile"
    temporary = runtime_root / "temporary"
    for directory in (data_root, local_app_data, roaming_app_data, user_profile, temporary):
        directory.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    for name in tuple(environment):
        if name.upper().startswith("GENERALS_REPLAY_ANALYZER_"):
            environment.pop(name)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["APPDATA"] = str(roaming_app_data)
    environment["HOME"] = str(user_profile)
    environment["LOCALAPPDATA"] = str(local_app_data)
    environment["TEMP"] = str(temporary)
    environment["TMP"] = str(temporary)
    environment["USERPROFILE"] = str(user_profile)
    environment["GENERALS_REPLAY_ANALYZER_DATA_ROOT"] = str(data_root)
    for proxy_name in (
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    ):
        environment.pop(proxy_name, None)
    return environment


def populated_runtime_environment(runtime_root: Path) -> dict[str, str]:
    """Bind a cloned populated runtime to its copied watched replay root."""
    environment = isolated_runtime_environment(runtime_root)
    fixture_input = (runtime_root / "fixture-input").resolve()
    if not fixture_input.is_dir():
        raise FileNotFoundError("populated fixture watched root is unavailable")
    configuration_source = runtime_root / "local-app-data" / "GeneralsReplayAnalyzer"
    configuration_destination = (
        runtime_root / "user-profile" / "AppData" / "Local" / "GeneralsReplayAnalyzer"
    )
    if not configuration_source.is_dir():
        raise FileNotFoundError("populated fixture configuration is unavailable")
    if configuration_destination.exists():
        raise ValueError("installed-process configuration root must start absent")
    shutil.copytree(configuration_source, configuration_destination)
    environment["GENERALS_REPLAY_ANALYZER_WATCHED_FOLDERS"] = json.dumps([str(fixture_input)])
    return environment
