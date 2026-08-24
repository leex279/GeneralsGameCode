"""Source-safety contract for engine-native replay insight providers."""

from pathlib import Path


def test_visibility_sampler_is_modern_only_side_effect_free_and_integer_owned(
    repository_root: Path,
) -> None:
    header = repository_root / "GeneralsMD/Code/GameEngine/Include/Common/ReplayVisibilitySampler.h"
    source = repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayVisibilitySampler.cpp"

    assert header.is_file() and source.is_file()
    header_text = header.read_text(encoding="utf-8")
    source_text = source.read_text(encoding="utf-8")
    assert "defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)" in header_text
    assert "PartitionManager::getShroudStatusForPlayer" in source_text

    forbidden_reads = (
        "getShroudedStatus(",
        "m_everSeenByPlayer",
        "GhostObject",
        "LogicRandom",
        "ClientRandom",
        "RandomValue",
        "rand(",
        "TheDisplay",
        "TheRadar",
        "getThreatValue",
        "getCashValue",
    )
    assert not [token for token in forbidden_reads if token in source_text]
    forbidden_writes = (
        "setShroud",
        "setPosition(",
        "setStatus(",
        "setTeam(",
        "setControllingPlayer(",
    )
    assert not [token for token in forbidden_writes if token in source_text]

    retained_state = source_text.split("struct VisibilityState", maxsplit=1)[1].split("};", maxsplit=1)[0]
    pair_key = source_text.split("struct VisibilityPair", maxsplit=1)[1].split("};", maxsplit=1)[0]
    assert "Object *" not in retained_state
    assert "Object *" not in pair_key
    assert "ObjectID objectId" in pair_key
    assert "Int playerIndex" in pair_key


def test_visibility_sampler_wires_exact_cadence_cap_order_and_lifecycle(repository_root: Path) -> None:
    source = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayVisibilitySampler.cpp"
    ).read_text(encoding="utf-8")
    game_logic = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/GameLogic/System/GameLogic.cpp"
    ).read_text(encoding="utf-8")
    telemetry = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp"
    ).read_text(encoding="utf-8")
    cmake = (repository_root / "GeneralsMD/Code/GameEngine/CMakeLists.txt").read_text(encoding="utf-8")

    assert "SAMPLE_INTERVAL_FRAMES = LOGICFRAMES_PER_SECOND / 2" in source
    assert "MAXIMUM_PAIRS_PER_PASS = 8192" in source
    assert "std::sort(playerIndices.begin(), playerIndices.end())" in source
    assert "std::sort(objectIds.begin(), objectIds.end())" in source
    assert "for (const Int playerIndex : playerIndices)" in source
    assert "for (const ObjectID objectId : objectIds)" in source
    assert '"visibility_sampling_summary"' in source
    assert '"object_visibility_changed"' in source
    assert "object_center_partition_cell" in source
    assert "first_observed_clear" in source
    assert "s_cursor" in source and "s_cycleId" in source

    reset_body = source.split("void ReplayVisibilitySampler::reset()", maxsplit=1)[1].split(
        "void ReplayVisibilitySampler::sampleEndOfFrame()", maxsplit=1
    )[0]
    assert "s_visibilityStates.clear()" in reset_body
    assert "s_cyclePairs.clear()" in reset_body
    assert "s_cursor = 0" in reset_body
    assert "s_cycleId = 0" in reset_body

    assert game_logic.count("ReplayVisibilitySampler::sampleEndOfFrame();") == 1
    end_update = game_logic.split("TheVictoryConditions->UPDATE();", maxsplit=1)[1].split(
        "m_frame++;", maxsplit=1
    )[0]
    assert end_update.index("ReplayMovementSampler::sampleEndOfFrame();") < end_update.index(
        "ReplayVisibilitySampler::sampleEndOfFrame();"
    )
    assert game_logic.count("ReplayVisibilitySampler::reset();") == 1
    assert telemetry.count("ReplayVisibilitySampler::reset();") == 1
    modern_sources = cmake.split("if(NOT IS_VS6_BUILD)", maxsplit=1)[1].split("endif()", maxsplit=1)[0]
    assert "Include/Common/ReplayVisibilitySampler.h" in modern_sources
    assert "Source/Common/ReplayVisibilitySampler.cpp" in modern_sources
