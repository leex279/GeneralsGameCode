"""Installed-wheel smoke tests for replay-analyzer package data."""

import json
import os
import shutil
import subprocess
import sys
import textwrap
import zipfile
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
    with zipfile.ZipFile(wheel) as archive:
        packaged_v1_schema = archive.read("generals_replay_analyzer/data/telemetry-v1.schema.json")
        packaged_v2_schema = archive.read("generals_replay_analyzer/data/telemetry-v2.schema.json")
    assert packaged_v1_schema == (PROJECT_ROOT / "contracts" / "telemetry-v1.schema.json").read_bytes()
    assert packaged_v2_schema == (PROJECT_ROOT / "contracts" / "telemetry-v2.schema.json").read_bytes()

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
    telemetry_script = textwrap.dedent(
        """
        import hashlib
        import json
        import tempfile
        from pathlib import Path

        from generals_replay_analyzer.telemetry.reader import iter_validated_trace

        directory = Path(tempfile.mkdtemp(prefix="wheel-telemetry-"))
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
                "exporter_settings": {"movement_sample_frames": 15, "audio_enabled": False},
                "game_data_catalog": reference,
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
        prior = b"".join(
            (json.dumps(record, separators=(",", ":")) + "\\n").encode() for record in [manifest, players]
        )
        complete = {
            **manifest,
            "sequence": 2,
            "event_type": "complete",
            "payload": {
                "final_frame": 0,
                "command_count": 0,
                "event_counts": {"manifest": 1, "players_initialized": 1, "complete": 1},
                "crc_mismatch": False,
                "replay_truncated": False,
                "clean_shutdown": True,
                "writer_error": None,
                "trace_sha256": hashlib.sha256(prior).hexdigest(),
                "map_assets": [],
                "final_cash_balances": [{"player_index": 0, "has_money": False, "balance": None}],
            },
        }
        trace = directory / "trace.ndjson"
        trace.write_bytes(prior + (json.dumps(complete, separators=(",", ":")) + "\\n").encode())
        assert [record.event_type for record in iter_validated_trace(trace)] == [
            "manifest",
            "players_initialized",
            "complete",
        ]
        """
    )
    telemetry = _run([str(environment_python), "-c", telemetry_script], tmp_path, wheel_environment)
    assert telemetry.returncode == 0
