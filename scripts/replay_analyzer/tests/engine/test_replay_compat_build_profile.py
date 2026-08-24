"""Build-profile contracts for the opt-in retail replay compatibility engine."""

from __future__ import annotations

import json
from pathlib import Path


def _preset_by_name(document: dict[str, object], group: str, name: str) -> dict[str, object]:
    presets = document[group]
    assert isinstance(presets, list)
    return next(preset for preset in presets if isinstance(preset, dict) and preset.get("name") == name)


def test_replay_analyzer_profile_is_explicit_and_uses_an_isolated_build_directory(
    repository_root: Path,
) -> None:
    presets = json.loads((repository_root / "CMakePresets.json").read_text(encoding="utf-8"))

    default = _preset_by_name(presets, "configurePresets", "default")
    ordinary = _preset_by_name(presets, "configurePresets", "win32")
    replay_analyzer = _preset_by_name(presets, "configurePresets", "win32-replay-analyzer")
    assert default["binaryDir"] == "${sourceDir}/build/${presetName}"
    assert ordinary.get("cacheVariables", {}).get("RTS_BUILD_OPTION_GENERALS_ONLINE_REPLAY_COMPAT") is None
    assert replay_analyzer["inherits"] == "win32"
    assert replay_analyzer["cacheVariables"] == {
        "RTS_BUILD_OPTION_GENERALS_ONLINE_REPLAY_COMPAT": "ON"
    }

    build = _preset_by_name(presets, "buildPresets", "win32-replay-analyzer")
    assert build["configurePreset"] == "win32-replay-analyzer"
    assert build["configuration"] == "Release"

    workflow = _preset_by_name(presets, "workflowPresets", "win32-replay-analyzer")
    assert workflow["steps"] == [
        {"type": "configure", "name": "win32-replay-analyzer"},
        {"type": "build", "name": "win32-replay-analyzer"},
    ]


def test_replay_compatibility_option_defaults_off_and_gates_zero_hour_definitions(
    repository_root: Path,
) -> None:
    cmake = (repository_root / "GeneralsMD/Code/CMakeLists.txt").read_text(encoding="utf-8")

    option = (
        'option(RTS_BUILD_OPTION_GENERALS_ONLINE_REPLAY_COMPAT '
        '"Build modern Zero Hour with the opt-in Generals Online deterministic replay profile" OFF)'
    )
    guard = "if(RTS_BUILD_OPTION_GENERALS_ONLINE_REPLAY_COMPAT AND NOT IS_VS6_BUILD)"
    profile = "GENERALS_ONLINE_HIGH_FPS_SERVER=1"
    assert option in cmake
    assert guard in cmake
    assert cmake.index(guard) < cmake.index(profile)
