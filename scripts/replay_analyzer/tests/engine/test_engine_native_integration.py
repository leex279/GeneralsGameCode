"""Integration contract for deterministic engine-native replay insight sampling."""

from __future__ import annotations

from pathlib import Path

MODERN_GUARD = "defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)"


def _read(repository_root: Path, relative_path: str) -> str:
    return (repository_root / relative_path).read_text(encoding="utf-8")


def test_all_engine_native_providers_are_in_the_modern_zero_hour_target(
    repository_root: Path,
) -> None:
    cmake = _read(repository_root, "GeneralsMD/Code/GameEngine/CMakeLists.txt")
    modern_sources = cmake.split("if(NOT IS_VS6_BUILD)", maxsplit=1)[1].split(
        "endif()", maxsplit=1
    )[0]

    for provider in (
        "ReplayScoreKeeper",
        "ReplayEconomy",
        "ReplayVisibilitySampler",
        "ReplayPartitionSampler",
    ):
        assert f"Include/Common/{provider}.h" in modern_sources
        assert f"Source/Common/{provider}.cpp" in modern_sources


def test_sampler_lifecycle_and_single_end_of_frame_seam_are_guarded_and_ordered(
    repository_root: Path,
) -> None:
    game_logic = _read(
        repository_root,
        "GeneralsMD/Code/GameEngine/Source/GameLogic/System/GameLogic.cpp",
    )
    telemetry = _read(
        repository_root,
        "GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp",
    )

    guarded_includes = game_logic.split(
        "#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)", maxsplit=1
    )[1].split("#endif", maxsplit=1)[0]
    assert 'include "Common/ReplayVisibilitySampler.h"' in guarded_includes
    assert 'include "Common/ReplayPartitionSampler.h"' in guarded_includes

    reset_body = game_logic.split("void GameLogic::reset()", maxsplit=1)[1].split(
        "m_thingTemplateBuildableOverrides.clear();", maxsplit=1
    )[0]
    assert MODERN_GUARD in reset_body
    assert "ReplayVisibilitySampler::reset();" in reset_body
    assert "ReplayPartitionSampler::reset();" in reset_body

    configure = telemetry.split("void ReplayTelemetry::configure", maxsplit=1)[1].split(
        "Bool ReplayTelemetry::isEnabled", maxsplit=1
    )[0]
    for reset_call in (
        "ReplayScoreKeeper::reset();",
        "ReplayEconomy::reset();",
        "ReplayVisibilitySampler::reset();",
        "ReplayPartitionSampler::reset();",
    ):
        assert reset_call in configure

    initialize = telemetry.split("void ReplayTelemetry::initialize()", maxsplit=1)[1].split(
        "void ReplayTelemetry::emit(", maxsplit=1
    )[0]
    assert initialize.index("ReplayScoreKeeper::initialize();") < initialize.index(
        "ReplayEconomy::initialize();"
    )

    assert game_logic.count("ReplayVisibilitySampler::sampleEndOfFrame();") == 1
    assert game_logic.count("ReplayPartitionSampler::sampleEndOfFrame();") == 1
    update_tail = game_logic.split("ThePartitionManager->UPDATE();", maxsplit=1)[1].split(
        "m_frame++;", maxsplit=1
    )[0]
    movement = update_tail.index("ReplayMovementSampler::sampleEndOfFrame();")
    visibility = update_tail.index("ReplayVisibilitySampler::sampleEndOfFrame();")
    partition = update_tail.index("ReplayPartitionSampler::sampleEndOfFrame();")
    assert movement < visibility < partition

    sampling_guard = "#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)"
    guarded_sampling_block = update_tail[:partition].rsplit(sampling_guard, maxsplit=1)[1]
    sampling_block = guarded_sampling_block[guarded_sampling_block.index(
        "ReplayMovementSampler::sampleEndOfFrame();"
    ) :]
    forbidden_simulation_operations = (
        "LogicRandom",
        "ClientRandom",
        "RandomValue",
        "rand(",
        "->UPDATE(",
        "->set",
        "m_frame =",
        "m_frame++",
    )
    assert not [token for token in forbidden_simulation_operations if token in sampling_block]


def test_terminal_samples_are_deduplicated_and_precede_outcome_and_completion(
    repository_root: Path,
) -> None:
    telemetry = _read(
        repository_root,
        "GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp",
    )
    economy = _read(
        repository_root,
        "GeneralsMD/Code/GameEngine/Source/Common/ReplayEconomy.cpp",
    )
    scorekeeper = _read(
        repository_root,
        "GeneralsMD/Code/GameEngine/Source/Common/ReplayScoreKeeper.cpp",
    )
    partition = _read(
        repository_root,
        "GeneralsMD/Code/GameEngine/Source/Common/ReplayPartitionSampler.cpp",
    )

    finish = telemetry.split("void ReplayTelemetry::finish(", maxsplit=1)[1].split(
        "void ReplayTelemetry::discard()", maxsplit=1
    )[0]
    terminal_partition = "ReplayPartitionSampler::emitTerminalSample(finalFrame);"
    terminal_income = "ReplayEconomy::emitTerminalCashPerMinuteSnapshot(finalFrame);"
    terminal_score = "ReplayScoreKeeper::writeTerminalSnapshot(static_cast<Int>(finalFrame));"
    match_outcome = "ReplayCombat::emitMatchOutcome(finalFrame, reason);"
    completion = 'writeLine(envelope(s_sequence++, finalFrame, "complete"'

    for call in (terminal_partition, terminal_income, terminal_score, match_outcome):
        assert finish.count(call) == 1
    assert (
        finish.index(terminal_partition)
        < finish.index(terminal_income)
        < finish.index(terminal_score)
        < finish.index(match_outcome)
        < finish.index(completion)
    )

    assert "s_state.lastCashPerMinuteFrame == frame" in economy
    assert "s_state.terminalWritten" in scorekeeper
    assert "s_samplerState.s_lastSampleFrame == frame" in partition
