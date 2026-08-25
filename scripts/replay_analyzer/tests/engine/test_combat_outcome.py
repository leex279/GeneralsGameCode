"""Real-engine contracts for passive combat and authoritative terminal observations."""

import hashlib
import json
import os
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from generals_replay_analyzer.importing.telemetry_import import bridge_v2_damage_victim_template_name
from generals_replay_analyzer.parser import parse_replay
from generals_replay_analyzer.telemetry.reader import iter_validated_trace

RUN_ID = "b23e4567-e89b-12d3-a456-426614174000"


def _environment(repository_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    dependencies = (
        repository_root / "build" / "win32" / "_deps" / "bink-build" / "Release",
        repository_root / "build" / "win32" / "_deps" / "miles-build" / "Release",
    )
    environment["PATH"] = os.pathsep.join([*(str(path.resolve()) for path in dependencies), environment["PATH"]])
    return environment


def _run(executable: Path, replay: Path, trace: Path, repository_root: Path) -> subprocess.CompletedProcess[str]:
    try:
        # Combat/outcome coverage does not need the production-density spatial stream; one sample every two minutes
        # avoids serializing roughly 160,000 unrelated entity_sample records in each focused integration run.
        return subprocess.run(
            [
                str(executable),
                "-headless",
                "-noaudio",
                "-replay",
                str(replay),
                "-telemetry-movement-frames",
                "3600",
                "-telemetry",
                str(trace),
                "-telemetry-run-id",
                RUN_ID,
            ],
            cwd=executable.parent,
            env=_environment(repository_root),
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"modern Zero Hour timed out during combat integration: {error}")


def _write_crc_stripped_derivative(source: Path, destination: Path) -> None:
    parsed = parse_replay(source)
    source_bytes = source.read_bytes()
    pieces = [source_bytes[: parsed.command_stream_offset]]
    pieces.extend(
        source_bytes[command.start_offset : command.end_offset]
        for command in parsed.commands
        if command.message_name != "MSG_LOGIC_CRC"
    )
    destination.write_bytes(b"".join(pieces))


def _load_engine_trace(trace: Path):
    """Apply the one supported producer bridge before strict trace validation."""
    source_trace_sha256, victim_templates = bridge_v2_damage_victim_template_name(trace)
    return tuple(iter_validated_trace(trace)), source_trace_sha256, victim_templates


def test_public_bridge_prepares_the_known_victim_template_extension(tmp_path: Path) -> None:
    """Exercise the narrow producer bridge with a self-contained synthetic trace."""
    trace = tmp_path / "victim-template.ndjson"
    damage = {
        "schema_version": 2,
        "event_type": "damage_applied",
        "sequence": 7,
        "payload": {"victim_template_name": "ChinaTankBattleMaster"},
    }
    damage_line = json.dumps(damage, separators=(",", ":")).encode() + b"\n"
    source_trace_sha256 = hashlib.sha256(damage_line).hexdigest()
    complete = {
        "schema_version": 2,
        "event_type": "complete",
        "sequence": 8,
        "payload": {"trace_sha256": source_trace_sha256},
    }
    trace.write_bytes(damage_line + json.dumps(complete, separators=(",", ":")).encode() + b"\n")

    bridged_source_sha256, victim_templates = bridge_v2_damage_victim_template_name(trace)

    prepared_lines = trace.read_bytes().splitlines(keepends=True)
    prepared_damage = json.loads(prepared_lines[0])
    prepared_complete = json.loads(prepared_lines[1])
    assert bridged_source_sha256 == source_trace_sha256
    assert victim_templates == {7: "ChinaTankBattleMaster"}
    assert "victim_template_name" not in prepared_damage["payload"]
    assert prepared_complete["payload"]["trace_sha256"] == hashlib.sha256(prepared_lines[0]).hexdigest()


def test_natural_replay_reaches_its_authoritative_clean_outcome(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    trace = (tmp_path / "natural-combat.ndjson").resolve()
    completed = _run(zero_hour_runtime_executable, pinned_replay, trace, repository_root)

    assert trace.is_file(), completed.stdout[-2000:] + completed.stderr[-2000:]
    records, _, _ = _load_engine_trace(trace)
    counts = Counter(record.event_type for record in records)
    assert counts["match_outcome"] == 1
    assert counts["damage_applied"] > 0
    assert counts["healing_applied"] > 0
    assert counts["veterancy_changed"] > 0
    outcome = records[-2]
    complete = records[-1]
    assert outcome.event_type == "match_outcome"
    assert outcome.payload.status == "decided"
    assert outcome.payload.winner_player_indices == [2]
    assert outcome.payload.loser_player_indices == [3]
    assert outcome.payload.terminal_reason == "clean_completion"
    assert complete.payload.final_frame == 56004
    assert complete.payload.crc_mismatch is False
    assert complete.payload.crc_mismatch_frame is outcome.payload.crc_mismatch_frame is None
    assert complete.payload.clean_shutdown is True
    assert complete.payload.quit_early == outcome.payload.quit_early
    assert complete.payload.replay_header_desync == outcome.payload.replay_header_desync


def test_crc_stripped_derivative_exercises_combat_mechanics_without_outcome_claims(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    derivative = tmp_path / "mechanics-only-crc-stripped.rep"
    original = pinned_replay.read_bytes()
    _write_crc_stripped_derivative(pinned_replay, derivative)
    trace = (tmp_path / "mechanics-combat.ndjson").resolve()

    completed = _run(zero_hour_runtime_executable, derivative, trace, repository_root)

    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    assert pinned_replay.read_bytes() == original
    records, _, _ = _load_engine_trace(trace)
    counts = Counter(record.event_type for record in records)
    assert counts["damage_applied"] > 0
    # This disposable derivative contains no authoritative healing or veterancy
    # transition, so it cannot provide real-engine evidence for those events.
    assert counts["healing_applied"] == 0
    assert counts["veterancy_changed"] == 0
    for index, record in enumerate(records):
        if record.event_type != "damage_applied" or not record.payload.killing_blow:
            continue
        destruction = next(
            later
            for later in records[index + 1 :]
            if later.event_type == "object_destroyed" and later.payload.object_id == record.payload.victim_object_id
        )
        assert record.sequence < destruction.sequence
    # The derivative is mechanics-only: the test validates writer reachability, not the winner or player strategy.


def test_combat_emission_is_before_on_die_and_terminal_outcome_is_before_complete(repository_root: Path) -> None:
    active_body = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/GameLogic/Object/Body/ActiveBody.cpp"
    ).read_text(encoding="utf-8")
    damage = active_body.split("void ActiveBody::attemptDamage", maxsplit=1)[1].split(
        "Bool ActiveBody::shouldRetaliateAgainstAggressor", maxsplit=1
    )[0]
    healing = active_body.split("void ActiveBody::attemptHealing", maxsplit=1)[1].split(
        "void ActiveBody::setInitialHealth", maxsplit=1
    )[0]
    object_source = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/GameLogic/Object/Object.cpp"
    ).read_text(encoding="utf-8")
    veterancy = object_source.split("void Object::onVeterancyLevelChanged", maxsplit=1)[1].split(
        "void Object::createVeterancyLevelFX", maxsplit=1
    )[0]
    telemetry = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp"
    ).read_text(encoding="utf-8")
    finish = telemetry.split("void ReplayTelemetry::finish(UnsignedInt finalFrame", maxsplit=1)[1].split(
        "void ReplayTelemetry::fail", maxsplit=1
    )[0]

    assert damage.index("ReplayCombat::observeDamage") < damage.index("obj->onDie( damageInfo )")
    assert "ReplayCombat::observeHealing" in healing
    assert "ReplayCombat::observeVeterancy" in veterancy
    assert finish.index("ReplayCombat::emitMatchOutcome") < finish.index('++s_eventCounts["complete"]')


def test_replay_combat_state_is_modern_only_and_retains_no_engine_pointers(repository_root: Path) -> None:
    header = (repository_root / "GeneralsMD/Code/GameEngine/Include/Common/ReplayCombat.h").read_text(encoding="utf-8")
    source = (repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayCombat.cpp").read_text(encoding="utf-8")
    guard = "#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)"

    assert guard in header
    assert guard in source
    state = source.split("struct ReplayCombatState", maxsplit=1)[1].split("};", maxsplit=1)[0]
    assert "Object *" not in state
    assert "Player *" not in state
    reset = source.split("void ReplayCombat::reset", maxsplit=1)[1].split(
        "void ReplayCombat::observeReplayHeader", maxsplit=1
    )[0]
    push = source.split("void ReplayCombat::pushPlayerTransition", maxsplit=1)[1].split(
        "void ReplayCombat::popPlayerTransition", maxsplit=1
    )[0]
    pop = source.split("void ReplayCombat::popPlayerTransition", maxsplit=1)[1].split(
        "#endif", maxsplit=1
    )[0]
    assert "s_state = ReplayCombatState()" in reset
    assert "playerTransitionStack.push_back" in push
    assert "playerTransitionStack.pop_back" in pop


def test_replay_observers_enable_scoped_speed_optimization_only_for_modern_msvc(repository_root: Path) -> None:
    """Keep analyzer observers optimized without changing simulation or legacy compiler flags."""
    common = repository_root / "GeneralsMD/Code/GameEngine"
    optimization = (common / "Include/Common/ReplayAnalyzerOptimization.h").read_text(encoding="utf-8")
    observers = (
        "ReplayCombat.cpp",
        "ReplayEconomy.cpp",
        "ReplayEntityLifecycle.cpp",
        "ReplayMovementSampler.cpp",
        "ReplayPartitionSampler.cpp",
        "ReplayTelemetry.cpp",
        "ReplayVisibilitySampler.cpp",
    )

    assert "#if defined(RTS_REPLAY_ANALYZER) && defined(_MSC_VER) && !defined(IS_VS6_BUILD)" in optimization
    assert '#pragma optimize("gt", on)' in optimization
    assert "TheSuperHackers @performance" in optimization
    for observer in observers:
        source = (common / "Source/Common" / observer).read_text(encoding="utf-8")
        assert '#include "Common/ReplayAnalyzerOptimization.h"' in source


def test_replay_header_and_frozen_player_domain_survive_new_game_reset(repository_root: Path) -> None:
    telemetry = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp"
    ).read_text(encoding="utf-8")
    begin = telemetry.split("void ReplayTelemetry::begin", maxsplit=1)[1].split(
        "void ReplayTelemetry::initialize", maxsplit=1
    )[0]
    initialize = telemetry.split("void ReplayTelemetry::initialize", maxsplit=1)[1].split(
        "void ReplayTelemetry::emit", maxsplit=1
    )[0]
    game_logic = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/GameLogic/System/GameLogic.cpp"
    ).read_text(encoding="utf-8")
    reset = game_logic.split("void GameLogic::reset", maxsplit=1)[1].split(
        "void GameLogic::newGame", maxsplit=1
    )[0]

    assert begin.index("ReplayCombat::reset") < begin.index("ReplayCombat::observeReplayHeader")
    assert initialize.index("ReplayGameDataExport::emitPlayersInitialized") < initialize.index(
        "ReplayCombat::initialize"
    )
    assert "ReplayCombat::reset" not in reset


def test_player_terminal_sources_are_explicit_and_replay_disconnect_metadata_is_not_guessed(repository_root: Path) -> None:
    dispatch = (
        repository_root / "Core/GameEngine/Source/GameLogic/System/GameLogicDispatch.cpp"
    ).read_text(encoding="utf-8")
    surrender = dispatch.split("bool GameLogic::onSelfDestruct", maxsplit=1)[1].split(
        "bool GameLogic::onSetReplayCamera", maxsplit=1
    )[0]
    victory = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/GameLogic/ScriptEngine/VictoryConditions.cpp"
    ).read_text(encoding="utf-8")
    update = victory.split("void VictoryConditions::update", maxsplit=1)[1].split(
        "Player* VictoryConditions::findFirstUndefeatedPlayer", maxsplit=1
    )[0]

    assert "const Bool replayTransferAssets = msg->getArgument(0)->boolean" in surrender
    assert "replayTransferAssets ? REPLAY_PLAYER_SURRENDERED : REPLAY_PLAYER_DISCONNECTED" in surrender
    assert "ReplayPlayerTransitionScope" in update
    assert "REPLAY_PLAYER_DEFEATED" in update
    combat = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayCombat.cpp"
    ).read_text(encoding="utf-8")
    assert "executed_true_self_destruct" in combat
    assert "replay_header_disconnect_plus_executed_false_self_destruct" in combat


def test_team_outcome_excludes_victorious_players_from_losers(repository_root: Path) -> None:
    combat = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayCombat.cpp"
    ).read_text(encoding="utf-8")
    player_loop = combat.split("for (Int index = 0; index < ThePlayerList->getPlayerCount(); ++index)", maxsplit=1)[
        1
    ].split("if (indices.empty())", maxsplit=1)[0]

    assert "const Bool achievedVictory" in player_loop
    assert "else if" in player_loop
    assert "hasBeenDefeated" in player_loop


def test_recorder_carries_explicit_termination_reasons_and_discards_reset_transactions(repository_root: Path) -> None:
    recorder = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp"
    ).read_text(encoding="utf-8")
    reset = recorder.split("void RecorderClass::reset()", maxsplit=1)[1].split(
        "void RecorderClass::update()", maxsplit=1
    )[0]
    stop = recorder.split("void RecorderClass::stopPlayback()", maxsplit=1)[1].split(
        "void RecorderClass::updateRecord", maxsplit=1
    )[0]
    parse_drain = recorder.split("void RecorderClass::completeReplayParseDump()", maxsplit=1)[1].split(
        "Bool RecorderClass::isPlaybackInProgress", maxsplit=1
    )[0]
    telemetry = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp"
    ).read_text(encoding="utf-8")
    configure = telemetry.split("void ReplayTelemetry::configure", maxsplit=1)[1].split(
        "Bool ReplayTelemetry::isEnabled", maxsplit=1
    )[0]

    assert "ReplayTelemetry::discard" in reset
    assert "ReplayTelemetry::discard" in configure
    for reason in (
        "REPLAY_TELEMETRY_TERMINATION_CLEAN_EOF",
        "REPLAY_TELEMETRY_TERMINATION_CRC_MISMATCH",
        "REPLAY_TELEMETRY_TERMINATION_TRUNCATED_INPUT",
        "REPLAY_TELEMETRY_TERMINATION_INTERRUPTED",
    ):
        assert reason in stop
    assert "REPLAY_TELEMETRY_TERMINATION_CRC_MISMATCH" in parse_drain


def test_damage_writer_uses_raw_source_player_mask_not_attacker_current_owner(repository_root: Path) -> None:
    combat = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayCombat.cpp"
    ).read_text(encoding="utf-8")
    damage = combat.split("void ReplayCombat::observeDamage", maxsplit=1)[1].split(
        "void ReplayCombat::observeHealing", maxsplit=1
    )[0]

    assert "damageInfo->in.m_sourcePlayerMask" in damage
    assert "source_player_mask" in damage
    assert "source_player_indices" in damage
    assert "objectPlayerIndex(attacker" not in damage


def test_damage_writer_emits_authoritative_attacker_and_victim_template_identities(
    repository_root: Path,
) -> None:
    combat = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayCombat.cpp"
    ).read_text(encoding="utf-8")
    damage = combat.split("void ReplayCombat::observeDamage", maxsplit=1)[1].split(
        "void ReplayCombat::observeHealing", maxsplit=1
    )[0]

    assert "const ThingTemplate *victimTemplate = victim->getTemplate();" in damage
    assert "damageInfo->in.m_sourceTemplate != nullptr" in damage
    assert "sourceObject->getTemplate()" in damage
    assert r'\"victim_template_name\"' in damage


def test_combat_writer_flushes_creation_before_emitting_object_references(repository_root: Path) -> None:
    combat = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayCombat.cpp"
    ).read_text(encoding="utf-8")
    damage = combat.split("void ReplayCombat::observeDamage", maxsplit=1)[1].split(
        "void ReplayCombat::observeHealing", maxsplit=1
    )[0]
    healing = combat.split("void ReplayCombat::observeHealing", maxsplit=1)[1].split(
        "void ReplayCombat::observeVeterancy", maxsplit=1
    )[0]
    veterancy = combat.split("void ReplayCombat::observeVeterancy", maxsplit=1)[1].split(
        "void ReplayCombat::observePlayerTerminalTransition", maxsplit=1
    )[0]

    assert '#include "Common/ReplayEntityLifecycle.h"' in combat
    assert damage.index("ReplayEntityLifecycle::ensureObjectCreated(victim)") < damage.index(
        'ReplayTelemetry::emit(currentFrame(), "damage_applied"'
    )
    assert healing.index("ReplayEntityLifecycle::ensureObjectCreated(target)") < healing.index(
        'ReplayTelemetry::emit(currentFrame(), "healing_applied"'
    )
    assert veterancy.index("ReplayEntityLifecycle::ensureObjectCreated(object)") < veterancy.index(
        'ReplayTelemetry::emit(currentFrame(), "veterancy_changed"'
    )
