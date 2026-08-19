"""Real-engine contract tests for authoritative players and semantic game data."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from uuid import UUID

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from generals_replay_analyzer.telemetry.model import ManifestRecord, PlayersInitializedRecord
from generals_replay_analyzer.telemetry.reader import iter_validated_trace

RUN_ID = "223e4567-e89b-12d3-a456-426614174000"
CATALOG_SCHEMA = Path(__file__).parents[2] / "contracts" / "game-data-catalog-v1.schema.json"


def _runtime_environment(repository_root: Path) -> dict[str, str]:
    """Expose build dependencies while the hardlink resolves retail data beside itself."""
    environment = os.environ.copy()
    dependency_directories = (
        repository_root / "build" / "win32" / "_deps" / "bink-build" / "Release",
        repository_root / "build" / "win32" / "_deps" / "miles-build" / "Release",
    )
    environment["PATH"] = os.pathsep.join([*(str(path.resolve()) for path in dependency_directories), environment["PATH"]])
    return environment


def _run_engine(
    runtime_executable: Path,
    replay: Path,
    trace_path: Path,
    repository_root: Path,
    run_id: str = RUN_ID,
) -> subprocess.CompletedProcess[str]:
    """Launch one bounded real replay into a disposable telemetry destination."""
    return subprocess.run(
        [
            str(runtime_executable),
            "-headless",
            "-noaudio",
            "-replay",
            str(replay),
            "-telemetry",
            str(trace_path),
            "-telemetry-run-id",
            run_id,
            "-telemetry-movement-frames",
            "15",
        ],
        cwd=runtime_executable.parent,
        env=_runtime_environment(repository_root),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _asset_path(trace_path: Path, relative_path: str) -> Path:
    """Resolve a run-owned relative asset while rejecting traversal or absolute references."""
    reference = Path(relative_path)
    assert not reference.is_absolute()
    resolved = (trace_path.parent / reference).resolve()
    resolved.relative_to(trace_path.parent.resolve())
    return resolved


def _assert_stable_ordinals(records: list[dict[str, object]]) -> None:
    """Require deterministic name ordering with explicit stable ordinal positions."""
    names = [str(record["name"]) for record in records]
    assert names == sorted(names)
    assert [record["ordinal"] for record in records] == list(range(len(records)))
    assert len(names) == len(set(names))


def test_replay_emits_one_resolved_player_event_and_a_strict_catalog_asset(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Catch missing, ambiguous, unreferenced, or non-semantic initialized replay evidence."""
    trace_path = (tmp_path / "telemetry.ndjson").resolve()
    completed = _run_engine(zero_hour_runtime_executable, pinned_replay, trace_path, repository_root)

    assert trace_path.is_file(), f"returncode={completed.returncode}\n{completed.stdout[-2000:]}{completed.stderr[-2000:]}"
    records = tuple(iter_validated_trace(trace_path))
    manifests = [record for record in records if isinstance(record, ManifestRecord)]
    player_events = [record for record in records if isinstance(record, PlayersInitializedRecord)]
    assert len(manifests) == 1
    assert len(player_events) == 1
    manifest = manifests[0]
    player_event = player_events[0]
    assert manifest.run_id == UUID(RUN_ID)
    assert manifest.sequence < player_event.sequence

    manifest_reference = manifest.payload.game_data_catalog.model_dump()
    event_reference = player_event.payload.game_data_catalog.model_dump()
    assert event_reference == manifest_reference
    assert manifest_reference["type"] == "game_data_catalog"
    assert manifest_reference["engine_data_identity"] == manifest.payload.engine_build
    assert manifest_reference["path"] == f"game-data-catalog-v1-{manifest_reference['sha256']}.json"

    catalog_path = _asset_path(trace_path, str(manifest_reference["path"]))
    catalog_bytes = catalog_path.read_bytes()
    assert hashlib.sha256(catalog_bytes).hexdigest() == manifest_reference["sha256"]
    assert catalog_bytes.endswith(b"\n")
    catalog = json.loads(catalog_bytes.decode("utf-8"))

    schema = json.loads(CATALOG_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(catalog)
    assert catalog["schema_version"] == 1
    assert catalog["type"] == "game_data_catalog"
    assert catalog["engine_data_identity"] == manifest_reference["engine_data_identity"]
    assert catalog["weapon_scope"] == "referenced_by_thing_templates"
    assert catalog["locomotor_scope"] == "referenced_by_thing_templates"

    players = player_event.payload.players
    assert {player.replay_name for player in players} == {"leex279", "FOX27"}
    assert len({player.player_index for player in players}) == len(players)
    assert sum(player.is_local_player for player in players) == 1
    assert all(player.faction_template_name for player in players)
    assert all(player.color is not None for player in players)
    assert all(player.controller in {"human", "ai"} for player in players)
    assert all(player.start_position_status == "resolved" for player in players)
    assert all(player.start_position is not None for player in players)

    for collection in ("thing_templates", "upgrades", "sciences", "weapons", "locomotors"):
        values = catalog[collection]
        assert isinstance(values, list) and values, f"catalog collection {collection} must not be empty"
        _assert_stable_ordinals(values)

    templates = catalog["thing_templates"]
    assert any(template["kind_of_flags"] for template in templates)
    assert any(template["weapon_names"] for template in templates)
    assert any(template["locomotor_sets"] for template in templates)
    assert any(template["prerequisites"] for template in templates)
    assert any(template["production_capable"] for template in templates)
    assert all("object_id" not in template and "template_id" not in template for template in templates)


def test_catalog_is_byte_deterministic_across_distinct_run_envelopes(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Catch run IDs or output paths leaking into content-addressed engine metadata."""
    traces = [(tmp_path / name / "trace.ndjson").resolve() for name in ("run-a", "run-b")]
    for trace in traces:
        trace.parent.mkdir()
    run_ids = ("323e4567-e89b-12d3-a456-426614174000", "423e4567-e89b-12d3-a456-426614174000")
    references: list[dict[str, object]] = []
    contents: list[bytes] = []
    for trace, run_id in zip(traces, run_ids, strict=True):
        completed = _run_engine(zero_hour_runtime_executable, pinned_replay, trace, repository_root, run_id)
        assert trace.is_file(), completed.stdout[-2000:] + completed.stderr[-2000:]
        manifest = next(record for record in iter_validated_trace(trace) if isinstance(record, ManifestRecord))
        reference = manifest.payload.game_data_catalog.model_dump()
        references.append(reference)
        contents.append(_asset_path(trace, str(reference["path"])).read_bytes())

    assert references[0] == references[1]
    assert contents[0] == contents[1]


def test_catalog_publication_never_overwrites_an_unrelated_collision(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Catch content-addressed cache reuse that trusts a filename without checking its bytes."""
    first_trace = (tmp_path / "first.ndjson").resolve()
    first = _run_engine(zero_hour_runtime_executable, pinned_replay, first_trace, repository_root)
    assert first_trace.is_file(), first.stdout[-2000:] + first.stderr[-2000:]
    manifest = next(record for record in iter_validated_trace(first_trace) if isinstance(record, ManifestRecord))
    catalog_path = _asset_path(first_trace, manifest.payload.game_data_catalog.path)
    collision = b"unrelated caller-owned bytes\n"
    catalog_path.write_bytes(collision)

    second_trace = (tmp_path / "second.ndjson").resolve()
    second = _run_engine(
        zero_hour_runtime_executable,
        pinned_replay,
        second_trace,
        repository_root,
        "523e4567-e89b-12d3-a456-426614174000",
    )

    assert catalog_path.read_bytes() == collision
    assert not second_trace.exists()
    assert "catalog_collision" in second.stderr
    assert not tuple(tmp_path.glob("*.tmp.*"))
