"""Real-engine and explicitly synthetic-derivative entity lifecycle integration tests."""

import hashlib
import json
import os
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from generals_replay_analyzer.parser import parse_replay
from generals_replay_analyzer.telemetry.model import (
    CompleteRecord,
    ObjectCreatedRecord,
    PlayersInitializedRecord,
    TelemetryRecord,
)
from generals_replay_analyzer.telemetry.reader import iter_validated_trace

RUN_IDS = (
    "123e4567-e89b-12d3-a456-426614174001",
    "123e4567-e89b-12d3-a456-426614174002",
)
LIFECYCLE_EVENTS = {
    "object_created",
    "construction_started",
    "construction_completed",
    "owner_changed",
    "sold",
    "object_destroyed",
}
REFERENCE_FIELDS: dict[str, tuple[str, ...]] = {
    "construction_started": ("object_id", "producer_object_id", "builder_object_id"),
    "construction_completed": ("object_id", "producer_object_id", "builder_object_id"),
    "owner_changed": ("object_id",),
    "sold": ("object_id",),
    "object_destroyed": ("object_id",),
    "production_queued": ("producer_object_id",),
    "production_cancelled": ("producer_object_id",),
    "production_completed": ("producer_object_id",),
    "upgrade_queued": ("producer_object_id",),
    "upgrade_cancelled": ("producer_object_id",),
    "upgrade_completed": ("producer_object_id",),
    "science_purchased": ("source_object_id",),
    "special_power_used": ("source_object_id", "target_object_id"),
    "supply_collected": ("collector_object_id", "source_object_id", "dropoff_object_id"),
    "damage_applied": ("victim_object_id", "attacker_object_id"),
    "healing_applied": ("target_object_id",),
    "veterancy_changed": ("object_id",),
    "entity_state_changed": ("object_id",),
    "entity_sample": ("object_id",),
}


def _runtime_environment(repository_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    dependencies = (
        repository_root / "build" / "win32" / "_deps" / "bink-build" / "Release",
        repository_root / "build" / "win32" / "_deps" / "miles-build" / "Release",
    )
    environment["PATH"] = os.pathsep.join([*(str(path.resolve()) for path in dependencies), environment["PATH"]])
    return environment


def _run_engine(command: list[str], game_directory: Path, repository_root: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=game_directory,
            env=_runtime_environment(repository_root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"modern Zero Hour timed out during entity lifecycle integration: {error}")


def _command(executable: Path, replay: Path, trace: Path, run_id: str) -> list[str]:
    return [
        str(executable),
        "-headless",
        "-noaudio",
        "-replay",
        str(replay),
        "-telemetry",
        str(trace),
        "-telemetry-run-id",
        run_id,
    ]


def _write_crc_stripped_derivative(source: Path, destination: Path) -> int:
    """Create disposable synthetic mechanics coverage; this is not full-match evidence."""
    parsed = parse_replay(source)
    source_bytes = source.read_bytes()
    pieces = [source_bytes[: parsed.command_stream_offset]]
    final_frame = 0
    for command in parsed.commands:
        if command.message_name == "MSG_LOGIC_CRC":
            continue
        pieces.append(source_bytes[command.start_offset : command.end_offset])
        final_frame = command.frame
    destination.write_bytes(b"".join(pieces))
    return final_frame


def _referenced_ids(event_type: str, payload: Mapping[str, object]) -> Iterator[int]:
    for field in REFERENCE_FIELDS.get(event_type, ()):
        value = payload.get(field)
        if isinstance(value, int):
            yield value
    if event_type == "object_created":
        context = payload.get("creation_context")
        if isinstance(context, Mapping):
            producer = context.get("producer_object_id")
            if isinstance(producer, int):
                yield producer
    elif event_type == "order_issued":
        selected = payload.get("selected_object_ids")
        if isinstance(selected, list):
            yield from (value for value in selected if isinstance(value, int))
        target = payload.get("target_object_id")
        if isinstance(target, int):
            yield target


def _assert_creation_precedes_every_reference(records: tuple[TelemetryRecord, ...]) -> None:
    created: set[int] = set()
    for record in records:
        event_type = record.event_type
        payload = record.payload.model_dump()
        if event_type == "object_created":
            object_id = payload["object_id"]
            assert object_id not in created
            created.add(object_id)
            continue
        for object_id in _referenced_ids(event_type, payload):
            assert object_id in created, (event_type, object_id)


def _normalized_bytes(trace: Path) -> bytes:
    normalized: list[bytes] = []
    for line in trace.read_bytes().splitlines():
        record = json.loads(line)
        record["run_id"] = "<run-id>"
        if record["event_type"] == "complete":
            record["payload"]["trace_sha256"] = "<trace-sha256>"
        normalized.append(json.dumps(record, separators=(",", ":")).encode() + b"\n")
    return b"".join(normalized)


def _normalized_lifecycle_source_bytes(trace: Path) -> bytes:
    result: list[bytes] = []
    for line in trace.read_bytes().splitlines(keepends=True):
        record = json.loads(line)
        if record["event_type"] in LIFECYCLE_EVENTS:
            result.append(line.replace(str(record["run_id"]).encode(), b"<run-id>"))
    return b"".join(result)


def test_natural_pinned_replay_emits_unique_ordered_preinitialized_map_and_starting_entities(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Treat only the natural CRC-stopping replay as authoritative real replay evidence."""
    trace = (tmp_path / "natural-lifecycle.ndjson").resolve()
    completed = _run_engine(
        _command(zero_hour_runtime_executable, pinned_replay, trace, RUN_IDS[0]),
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert trace.is_file(), completed.stdout[-2000:] + completed.stderr[-2000:]
    records = tuple(iter_validated_trace(trace))
    players_index, players_record = next(
        (index, record) for index, record in enumerate(records) if isinstance(record, PlayersInitializedRecord)
    )
    creations = [record for record in records if isinstance(record, ObjectCreatedRecord)]
    assert creations
    assert all(records.index(record) > players_index for record in creations)
    assert len({record.payload.object_id for record in creations}) == len(creations)
    assert {record.payload.creation_source for record in creations} >= {"map_loaded", "starting_object"}
    occupied_players = {
        slot.player_index
        for slot in players_record.payload.slots or []
        if slot.occupied and slot.player_index is not None
    }
    assert any(
        record.payload.creation_source == "map_loaded"
        and (
            record.payload.owner_player_index is None
            or record.payload.owner_player_index not in occupied_players
        )
        for record in creations
    )
    assert any(record.payload.position_status == "placed" and record.payload.position is not None for record in creations)
    assert any(record.event_type == "construction_started" for record in records)
    _assert_creation_precedes_every_reference(records)


def test_natural_pinned_replay_lifecycle_and_non_lifecycle_streams_are_byte_deterministic(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    traces = [(tmp_path / f"deterministic-{index}.ndjson").resolve() for index in range(2)]
    for trace, run_id in zip(traces, RUN_IDS, strict=True):
        completed = _run_engine(
            _command(zero_hour_runtime_executable, pinned_replay, trace, run_id),
            zero_hour_runtime_executable.parent,
            repository_root,
        )
        assert trace.is_file(), completed.stdout[-2000:] + completed.stderr[-2000:]
        assert tuple(iter_validated_trace(trace))

    assert _normalized_lifecycle_source_bytes(traces[0]) == _normalized_lifecycle_source_bytes(traces[1])
    assert _normalized_bytes(traces[0]) == _normalized_bytes(traces[1])
    first_catalogs = tuple(sorted((path.name, hashlib.sha256(path.read_bytes()).hexdigest()) for path in tmp_path.glob("game-data-catalog-v1-*.json")))
    assert len(first_catalogs) == 1


def test_crc_stripped_derivative_exercises_synthetic_lifecycle_mechanics_only(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Use altered bytes only for mechanics reachability, never replay strategy or full-match evidence."""
    derivative = tmp_path / "synthetic-crc-stripped.rep"
    original_bytes = pinned_replay.read_bytes()
    terminal_command_frame = _write_crc_stripped_derivative(pinned_replay, derivative)
    trace = (tmp_path / "synthetic-mechanics.ndjson").resolve()

    completed = _run_engine(
        _command(zero_hour_runtime_executable, derivative, trace, RUN_IDS[0]),
        zero_hour_runtime_executable.parent,
        repository_root,
    )

    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    assert pinned_replay.read_bytes() == original_bytes
    records = tuple(iter_validated_trace(trace))
    complete = records[-1]
    assert isinstance(complete, CompleteRecord)
    assert complete.payload.final_frame == terminal_command_frame + 1
    observed = {record.event_type for record in records}
    assert {"construction_started", "construction_completed", "sold", "object_destroyed"} <= observed
    _assert_creation_precedes_every_reference(records)


def test_lifecycle_helper_is_modern_only_and_never_buffers_object_pointers(repository_root: Path) -> None:
    header = (
        repository_root / "GeneralsMD/Code/GameEngine/Include/Common/ReplayEntityLifecycle.h"
    ).read_text(encoding="utf-8")
    source = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayEntityLifecycle.cpp"
    ).read_text(encoding="utf-8")

    guard = "#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)"
    assert guard in header
    assert guard in source
    creation_entry = source.split("struct CreationEntry", maxsplit=1)[1].split("};", maxsplit=1)[0]
    assert "Object *" not in creation_entry
    assert "Object*" not in creation_entry


def test_every_lifecycle_instrumentation_seam_has_the_required_feature_comment(repository_root: Path) -> None:
    required_comment = "// TheSuperHackers @feature Leex 20/08/2026"
    paths = (
        "GeneralsMD/Code/GameEngine/Source/GameLogic/System/GameLogic.cpp",
        "GeneralsMD/Code/GameEngine/Source/GameLogic/Object/Object.cpp",
        "GeneralsMD/Code/GameEngine/Source/GameLogic/Object/Update/ProductionUpdate.cpp",
        "GeneralsMD/Code/GameEngine/Source/Common/Thing/Thing.cpp",
        "GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp",
    )
    call_names = (
        "ReplayEntityLifecycle::reset",
        "ReplayEntityLifecycle::observeRegistered",
        "ReplayEntityLifecycle::observeTransform",
        "ReplayEntityLifecycle::observePositionSet",
        "ReplayEntityLifecycle::observeTeamChanged",
        "ReplayEntityLifecycle::observeStatusChanged",
        "ReplayEntityLifecycle::observeDestroyed",
        "ReplayEntityLifecycle::initialize",
        "ReplayEntityLifecycle::flushPendingCreations",
        "ReplayEntityCreationScope creationScope",
        "creationScope.observeReturned",
    )
    for relative_path in paths:
        lines = (repository_root / relative_path).read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if any(call_name in line for call_name in call_names):
                assert required_comment in "\n".join(lines[max(0, index - 2) : index])


def test_first_explicit_position_set_freezes_creation_pose_before_later_transform_updates(
    repository_root: Path,
) -> None:
    thing_source = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/Thing/Thing.cpp"
    ).read_text(encoding="utf-8")
    lifecycle_source = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayEntityLifecycle.cpp"
    ).read_text(encoding="utf-8")
    set_position = thing_source.split("void Thing::setPosition( const Coord3D *pos )", maxsplit=1)[1].split(
        "void Thing::setOrientation", maxsplit=1
    )[0]
    assert "void ReplayEntityLifecycle::observePositionSet" in lifecycle_source
    observe_position = lifecycle_source.split(
        "void ReplayEntityLifecycle::observePositionSet", maxsplit=1
    )[1].split("void ReplayEntityLifecycle::", maxsplit=1)[0]
    finalize_creation = lifecycle_source.split("void finalizeCreation", maxsplit=1)[1].split(
        "CreationMap::iterator findCreation", maxsplit=1
    )[0]
    initialize = lifecycle_source.split("void ReplayEntityLifecycle::initialize", maxsplit=1)[1].split(
        "void ReplayEntityLifecycle::flushPendingCreations", maxsplit=1
    )[0]

    assert "ReplayEntityLifecycle::observePositionSet(AsObject(this))" in set_position
    assert set_position.index("ReplayEntityLifecycle::observePositionSet") > set_position.index("m_cachedPos = *pos")
    assert "if (!it->second.hasPosition)" in observe_position
    assert "it->second.orientation = object->getOrientation()" in observe_position
    assert "entry.orientation = object->getOrientation()" not in finalize_creation
    assert initialize.index("finalizeThrough(~0ULL)") < initialize.index("flushEvents()")


def test_direct_creation_source_marks_only_returned_root_after_nested_object_creation(
    repository_root: Path,
) -> None:
    game_logic = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/GameLogic/System/GameLogic.cpp"
    ).read_text(encoding="utf-8")
    lifecycle_header = (
        repository_root / "GeneralsMD/Code/GameEngine/Include/Common/ReplayEntityLifecycle.h"
    ).read_text(encoding="utf-8")
    lifecycle_source = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayEntityLifecycle.cpp"
    ).read_text(encoding="utf-8")
    nested_containment = (
        repository_root
        / "GeneralsMD/Code/GameEngine/Source/GameLogic/Object/Contain/OverlordContain.cpp"
    ).read_text(encoding="utf-8")
    starting_call = game_logic.split("static Object * placeObjectAtPosition", maxsplit=1)[1].split(
        "static void placeNetworkBuildingsForPlayer", maxsplit=1
    )[0]

    assert "void observeReturned(const Object *object)" in lifecycle_header
    assert "s_creationSource" not in lifecycle_source
    assert "s_directCreationDepth != 0" in lifecycle_source
    assert starting_call.index("creationScope.observeReturned(obj)") > starting_call.index(
        "TheThingFactory->newObject"
    )
    assert "onObjectCreated()" in nested_containment
    assert "createPayload()" in nested_containment
    assert "TheThingFactory->newObject" in nested_containment
    assert "observeReturned" not in nested_containment


def test_initial_construction_transition_uses_registration_identity_snapshot(repository_root: Path) -> None:
    lifecycle_source = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayEntityLifecycle.cpp"
    ).read_text(encoding="utf-8")
    registration = lifecycle_source.split("void ReplayEntityLifecycle::observeRegistered", maxsplit=1)[1].split(
        "void ReplayEntityLifecycle::", maxsplit=1
    )[0]
    finalize_creation = lifecycle_source.split("void finalizeCreation", maxsplit=1)[1].split(
        "CreationMap::iterator findCreation", maxsplit=1
    )[0]
    owner_change = lifecycle_source.split("void ReplayEntityLifecycle::observeTeamChanged", maxsplit=1)[1].split(
        "void ReplayEntityLifecycle::observeStatusChanged", maxsplit=1
    )[0]

    assert "entry.initialIdentity = identityForTeam(object->getTeam())" in registration
    assert "initialConstructionPayload(entry" in finalize_creation
    assert "constructionPayload(object" not in finalize_creation
    assert owner_change.index("ensureObjectCreated(object)") < owner_change.index("identityForTeam(newTeam)")
