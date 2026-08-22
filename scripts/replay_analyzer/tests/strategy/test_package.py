"""Installed-wheel proof for the package-local strategy taxonomy resources."""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path


def _run(command: list[str], *, cwd: Path, dependency_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    if dependency_path is not None:
        environment["PYTHONPATH"] = str(dependency_path)
    environment["UV_CACHE_DIR"] = str(Path(__file__).resolve().parents[2] / ".uv-cache")
    result = subprocess.run(command, cwd=cwd, env=environment, check=False, capture_output=True, text=True)
    assert result.returncode == 0, f"command failed: {command!r}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result


def test_wheel_contains_exact_taxonomy_bytes_and_loads_outside_the_checkout(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[2]
    source_data = project / "src" / "generals_replay_analyzer" / "data"
    distribution = tmp_path / "dist"
    distribution.mkdir()
    _run(["uv", "build", "--wheel", "--out-dir", str(distribution)], cwd=project)
    wheel = next(distribution.glob("generals_replay_analyzer-*.whl"))
    expected = (
        "strategy-taxonomy-v1.json",
        "strategy-taxonomy-v1.schema.json",
    )
    with zipfile.ZipFile(wheel) as archive:
        for name in expected:
            archive_name = f"generals_replay_analyzer/data/{name}"
            assert archive.read(archive_name) == (source_data / name).read_bytes()

    environment = tmp_path / "isolated"
    _run(["uv", "venv", "--python", sys.executable, str(environment)], cwd=tmp_path)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run(["uv", "pip", "install", "--offline", "--no-deps", "--python", str(python), str(wheel)], cwd=tmp_path)
    proof = _run(
        [
            str(python),
            "-s",
            "-c",
            (
                "import pathlib;"
                "from generals_replay_analyzer.features.registry import BASE_REGISTRY;"
                "import generals_replay_analyzer.strategy.taxonomy as module;"
                f"assert pathlib.Path(module.__file__).is_relative_to(pathlib.Path({str(environment)!r}));"
                "default_taxonomy=module.default_taxonomy;"
                "t=default_taxonomy(BASE_REGISTRY);"
                "assert t.schema_version=='strategy-taxonomy-v1';"
                "assert [s.strategy_id for s in t.strategies]==['unknown_or_mixed'];"
                "print(t.content_sha256)"
            ),
        ],
        cwd=tmp_path,
        dependency_path=Path(sysconfig.get_paths()["purelib"]),
    )
    assert len(proof.stdout.strip()) == 64
    assert str(project.resolve()).casefold() not in proof.stdout.casefold()
