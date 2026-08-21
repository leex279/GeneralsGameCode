"""Installed-wheel smoke tests for replay-analyzer package data."""

import json
import os
import shutil
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

from map_asset_support import write_test_map_asset

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
        [str(environment_python), "-c", "from generals_replay_analyzer.contracts import message_name_for; print(message_name_for(1001))"],
        tmp_path,
    )
    assert lookup.stdout.strip() == "MSG_CREATE_SELECTED_GROUP"

    executable = environment_directory / "Scripts" / "replay-analyzer.exe"
    export_help = _run([str(executable), "export-telemetry", "--help"], tmp_path)
    assert "--engine ENGINE" in export_help.stdout
    assert "--movement-sample-frames" in export_help.stdout
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
