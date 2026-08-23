"""Real-engine contracts for terminal raw ScoreKeeper observations."""

import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from generals_replay_analyzer.telemetry.model import (
    CompleteRecord,
    MatchOutcomeRecord,
    PlayersInitializedRecord,
    ScoreKeeperSnapshotRecord,
)
from generals_replay_analyzer.telemetry.reader import iter_validated_trace

RUN_ID = "d23e4567-e89b-12d3-a456-426614174000"


def _environment(repository_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    dependencies = (
        repository_root / "build" / "win32" / "_deps" / "bink-build" / "Release",
        repository_root / "build" / "win32" / "_deps" / "miles-build" / "Release",
    )
    environment["PATH"] = os.pathsep.join([*(str(path.resolve()) for path in dependencies), environment["PATH"]])
    return environment


def _run(
    executable: Path,
    replay: Path,
    trace: Path,
    repository_root: Path,
    user_data_root: Path,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [
                str(executable),
                "-headless",
                "-noaudio",
                "-replay",
                str(replay),
                "-telemetry",
                str(trace),
                "-telemetry-run-id",
                RUN_ID,
                "-replay-user-data-root",
                str(user_data_root),
            ],
            cwd=executable.parent,
            env=_environment(repository_root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"modern Zero Hour timed out during ScoreKeeper integration: {error}")


def test_pinned_replay_emits_one_terminal_score_snapshot_for_resolved_slots(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    installed_map = (
        Path.home()
        / "Documents"
        / "Command and Conquer Generals Zero Hour Data"
        / "Maps"
        / "[RANK] Sand Scorpion"
    )
    if not (installed_map / "[RANK] Sand Scorpion.map").is_file():
        pytest.skip(f"pinned replay map is absent: {installed_map}")
    user_data_root = tmp_path / "isolated-user-data"
    staged_map = user_data_root / "Maps" / installed_map.name
    staged_map.mkdir(parents=True)
    for filename in ("[RANK] Sand Scorpion.map", "[RANK] Sand Scorpion.tga", "map.str"):
        shutil.copyfile(installed_map / filename, staged_map / filename)
    trace = (tmp_path / "score.ndjson").resolve()
    completed = _run(zero_hour_runtime_executable, pinned_replay, trace, repository_root, user_data_root)

    assert trace.is_file(), completed.stdout[-2000:] + completed.stderr[-2000:]
    records = tuple(iter_validated_trace(trace))
    counts = Counter(record.event_type for record in records)
    assert counts["scorekeeper_snapshot"] == 1
    players = next(record for record in records if isinstance(record, PlayersInitializedRecord))
    score = next(record for record in records if isinstance(record, ScoreKeeperSnapshotRecord))
    outcome = next(record for record in records if isinstance(record, MatchOutcomeRecord))
    complete = records[-1]
    assert isinstance(complete, CompleteRecord)

    resolved_player_indices = sorted(
        slot.player_index
        for slot in players.payload.slots or []
        if slot.occupied and slot.resolution_status == "resolved" and slot.player_index is not None
    )
    assert [entry.player_index for entry in score.payload.players] == resolved_player_indices
    assert score.payload.source == "Player::getScoreKeeper"
    assert score.payload.player_scope == "resolved_occupied_replay_slots"
    assert score.payload.scoring_enabled is True
    assert score.frame == outcome.frame == complete.payload.final_frame
    assert score.sequence + 1 == outcome.sequence
    assert outcome.sequence + 1 == complete.sequence


def test_scorekeeper_provider_uses_only_raw_signed_getters_and_stable_slot_indices(repository_root: Path) -> None:
    header = (
        repository_root / "GeneralsMD/Code/GameEngine/Include/Common/ReplayScoreKeeper.h"
    ).read_text(encoding="utf-8")
    source = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayScoreKeeper.cpp"
    ).read_text(encoding="utf-8")
    guard = "#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)"

    assert guard in header
    assert guard in source
    assert "calculateScore(" not in source
    for getter in (
        "getTotalMoneyEarned()",
        "getTotalMoneySpent()",
        "getTotalUnitsBuilt()",
        "getTotalUnitsLost()",
        "getTotalUnitsDestroyed()",
        "getTotalBuildingsBuilt()",
        "getTotalBuildingsLost()",
        "getTotalBuildingsDestroyed()",
        "getTotalTechBuildingsCaptured()",
        "getTotalFactionBuildingsCaptured()",
    ):
        assert source.count(getter) == 1
    field_getters = {
        "money_earned": "getTotalMoneyEarned()",
        "money_spent": "getTotalMoneySpent()",
        "units_built": "getTotalUnitsBuilt()",
        "units_lost": "getTotalUnitsLost()",
        "units_destroyed": "getTotalUnitsDestroyed()",
        "buildings_built": "getTotalBuildingsBuilt()",
        "buildings_lost": "getTotalBuildingsLost()",
        "buildings_destroyed": "getTotalBuildingsDestroyed()",
        "tech_buildings_captured": "getTotalTechBuildingsCaptured()",
        "faction_buildings_captured": "getTotalFactionBuildingsCaptured()",
    }
    compact_source = "".join(source.split())
    for field, getter in field_getters.items():
        assert f'\\"{field}\\":"+std::to_string(scoreKeeper->{getter})' in compact_source

    state = source.split("struct ReplayScoreKeeperState", maxsplit=1)[1].split("};", maxsplit=1)[0]
    assert "Object *" not in state
    assert "Player *" not in state
    initialize = source.split("void ReplayScoreKeeper::initialize", maxsplit=1)[1].split(
        "void ReplayScoreKeeper::writeTerminalSnapshot", maxsplit=1
    )[0]
    assert "getConstSlot" in initialize
    assert "isOccupied" in initialize
    assert "getPlayerFromSlotIndex" in initialize
    assert "std::sort" in initialize


def test_scorekeeper_snapshot_is_initialized_after_players_and_written_before_outcome(repository_root: Path) -> None:
    telemetry = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp"
    ).read_text(encoding="utf-8")
    begin = telemetry.split("void ReplayTelemetry::begin", maxsplit=1)[1].split(
        "void ReplayTelemetry::initialize", maxsplit=1
    )[0]
    initialize = telemetry.split("void ReplayTelemetry::initialize", maxsplit=1)[1].split(
        "void ReplayTelemetry::emit", maxsplit=1
    )[0]
    finish = telemetry.split("void ReplayTelemetry::finish(UnsignedInt finalFrame", maxsplit=1)[1].split(
        "void ReplayTelemetry::discard", maxsplit=1
    )[0]

    assert "ReplayScoreKeeper::reset" in begin
    assert initialize.index("ReplayGameDataExport::emitPlayersInitialized") < initialize.index(
        "ReplayScoreKeeper::initialize"
    )
    assert finish.count("ReplayScoreKeeper::writeTerminalSnapshot") == 1
    assert finish.index("ReplayScoreKeeper::writeTerminalSnapshot") < finish.index(
        "ReplayCombat::emitMatchOutcome"
    )
    assert finish.index("ReplayCombat::emitMatchOutcome") < finish.index('++s_eventCounts["complete"]')


def test_scorekeeper_provider_is_wired_only_in_the_modern_zero_hour_target(repository_root: Path) -> None:
    cmake = (repository_root / "GeneralsMD/Code/GameEngine/CMakeLists.txt").read_text(encoding="utf-8")
    modern_block = cmake.split("if(NOT IS_VS6_BUILD)", maxsplit=1)[1].split("endif()", maxsplit=1)[0]

    assert "Include/Common/ReplayScoreKeeper.h" in modern_block
    assert "Source/Common/ReplayScoreKeeper.cpp" in modern_block
