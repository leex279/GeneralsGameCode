from __future__ import annotations

import os
import subprocess
from pathlib import Path

from generals_replay_analyzer.longitudinal import LongitudinalAnalysisService, LongitudinalRequest, SegmentKey


def test_installed_wheel_exposes_public_longitudinal_surface_without_checkout_fallback(tmp_path: Path) -> None:
    project = Path(__file__).parents[2]
    dist = tmp_path / "dist"
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(dist)], cwd=project, check=True)
    wheel = next(dist.glob("*.whl"))
    environment = tmp_path / "environment"
    subprocess.run(["uv", "venv", "--python", "3.12", str(environment)], check=True)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(["uv", "pip", "install", "--python", str(python), "--offline", "--no-deps", str(wheel)], check=True)
    dependency_root = str(Path(__import__("sqlalchemy").__file__).parent.parent)
    code = """
import generals_replay_analyzer.longitudinal as package
from pathlib import Path
assert package.LongitudinalAnalysisService
assert package.LongitudinalRequest
assert package.SegmentKey
module_path = Path(package.__file__).resolve()
assert 'site-packages' in module_path.parts
assert not (module_path.parent / 'data').exists()
print(module_path)
"""
    child_environment = dict(os.environ)
    child_environment["PYTHONPATH"] = dependency_root
    result = subprocess.run(
        [str(python), "-c", code],
        cwd=tmp_path,
        env=child_environment,
        text=True,
        capture_output=True,
        check=True,
    )
    assert str(project.resolve()) not in result.stdout


def test_imported_public_request_is_orm_free() -> None:
    request = LongitudinalRequest(
        player_public_id="00000000-0000-4000-8000-000000000001",
        segment=SegmentKey(),
        metric_names=("economy.cash_change_total",),
        pattern_names=(),
        settings=__import__("generals_replay_analyzer.longitudinal", fromlist=["LongitudinalSettings"]).LongitudinalSettings(
            minimum_sample_size=2,
            bootstrap_resamples=10,
            confidence_level=0.95,
            enabled_metrics=("economy.cash_change_total",),
        ),
    )
    assert not hasattr(request, "_sa_instance_state")
    assert LongitudinalAnalysisService
