"""Installed-wheel proof for the spatial package and numeric dependencies."""

from __future__ import annotations

import os
import shutil
import site
import subprocess
import sys
import textwrap
import zipfile
from email import message_from_bytes
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]


def _run(
    arguments: list[str], working_directory: Path, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, check=True, cwd=working_directory, text=True, capture_output=True, env=environment)


def test_installed_wheel_declares_and_imports_spatial_numeric_dependencies(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    distribution_directory = tmp_path / "dist"
    _run([uv, "build", "--wheel", "--out-dir", str(distribution_directory)], PROJECT_ROOT)
    wheel = next(distribution_directory.glob("generals_replay_analyzer-*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = message_from_bytes(archive.read(metadata_name))
        requirements = set(metadata.get_all("Requires-Dist", []))
        assert "numpy<2.5,>=2.2" in requirements
        assert "scipy<2,>=1.15" in requirements
        assert "generals_replay_analyzer/spatial/features.py" in names
        assert "generals_replay_analyzer/spatial/statistics.py" in names

    environment_directory = tmp_path / "spatial-wheel-environment"
    _run([sys.executable, "-m", "venv", str(environment_directory)], tmp_path)
    environment_python = environment_directory / "Scripts" / "python.exe"
    _run([str(environment_python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)], tmp_path)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(site.getsitepackages())
    script = textwrap.dedent(
        """
        import generals_replay_analyzer
        import numpy
        import scipy

        from generals_replay_analyzer.spatial.features import SPATIAL_REGISTRY, SpatialFeatureExtractor
        from generals_replay_analyzer.spatial.statistics import NUMPY_VERSION, SCIPY_VERSION

        assert "spatial-wheel-environment" in str(generals_replay_analyzer.__file__)
        assert SpatialFeatureExtractor.observation_policy == "replay_wide_telemetry"
        assert NUMPY_VERSION == numpy.__version__
        assert SCIPY_VERSION == scipy.__version__
        assert "movement_density.sample_count_heatmap_bootstrap_interval" in SPATIAL_REGISTRY.names()
        """
    )
    result = _run([str(environment_python), "-c", script], tmp_path, environment)
    assert result.returncode == 0
