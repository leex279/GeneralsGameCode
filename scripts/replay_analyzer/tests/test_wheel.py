"""Installed-wheel smoke tests for replay-analyzer package data."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "zero_hour_1_04" / "leex279_vs_fox27.rep"
PROJECT_ROOT = Path(__file__).parents[1]


def _run(
    arguments: list[str], working_directory: Path, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one isolated wheel-install command while retaining useful failure output."""
    return subprocess.run(arguments, check=True, cwd=working_directory, text=True, capture_output=True, env=environment)


def test_installed_wheel_loads_catalog_for_symbolic_lookup_and_inspection(tmp_path: Path) -> None:
    """Reject a wheel that works only because a source-checkout contracts directory is nearby."""
    uv = shutil.which("uv")
    assert uv is not None
    distribution_directory = tmp_path / "dist"
    _run([uv, "build", "--wheel", "--out-dir", str(distribution_directory)], PROJECT_ROOT)
    wheel = next(distribution_directory.glob("generals_replay_analyzer-*.whl"))

    environment_directory = tmp_path / "wheel-environment"
    _run([sys.executable, "-m", "venv", str(environment_directory)], tmp_path)
    environment_python = environment_directory / "Scripts" / "python.exe"
    _run([str(environment_python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)], tmp_path)

    lookup = _run(
        [str(environment_python), "-c", "from generals_replay_analyzer.contracts import message_name_for; print(message_name_for(1001))"],
        tmp_path,
    )
    assert lookup.stdout.strip() == "MSG_CREATE_SELECTED_GROUP"

    executable = environment_directory / "Scripts" / "replay-analyzer.exe"
    inspection = _run([str(executable), "inspect", str(FIXTURE_PATH), "--format", "json"], tmp_path)
    output = json.loads(inspection.stdout)
    assert output["command_stream_offset"] == 342
    assert output["completion_status"] == "complete"

    wheel_environment = os.environ.copy()
    wheel_environment["PYTHONPATH"] = str(PROJECT_ROOT / ".venv" / "Lib" / "site-packages")
    telemetry = _run(
        [
            str(environment_python),
            "-c",
            (
                "import hashlib, json, tempfile; from pathlib import Path; "
                "from generals_replay_analyzer.telemetry.reader import iter_validated_trace; "
                "base={'schema_version':1,'run_id':'123e4567-e89b-12d3-a456-426614174000','sequence':0,'frame':0,"
                "'logic_time_seconds':0.0,'event_type':'manifest','payload':{'engine_build':'test','replay_version':'1.04',"
                "'map_identity':'test.map','initial_seed':1,'exporter_settings':{}}}; "
                "body=(json.dumps(base,separators=(',',':'))+'\\n').encode(); "
                "complete={'schema_version':1,'run_id':base['run_id'],'sequence':1,'frame':0,'logic_time_seconds':0.0,"
                "'event_type':'complete','payload':{'final_frame':0,'command_count':0,'event_counts':{'manifest':1,'complete':1},"
                "'crc_mismatch':False,'replay_truncated':False,'clean_shutdown':True,'writer_error':None,"
                "'trace_sha256':hashlib.sha256(body).hexdigest(),'map_assets':[]}}; "
                "trace=Path(tempfile.gettempdir())/'wheel-telemetry.ndjson'; "
                "trace.write_bytes(body+(json.dumps(complete,separators=(',',':'))+'\\n').encode()); "
                "assert [record.event_type for record in iter_validated_trace(trace)] == ['manifest','complete']"
            ),
        ],
        tmp_path,
        wheel_environment,
    )
    assert telemetry.returncode == 0
