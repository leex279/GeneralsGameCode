"""Source contracts for Zero Hour base Generals archive discovery."""

from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "relative_path",
    (
        "Core/GameEngineDevice/Source/Win32Device/Common/Win32BIGFileSystem.cpp",
        "Core/GameEngineDevice/Source/StdDevice/Common/StdBIGFileSystem.cpp",
    ),
)
def test_zero_hour_uses_steam_coinstall_when_generals_registry_path_is_empty(
    repository_root: Path, relative_path: str
) -> None:
    source = (repository_root / relative_path).read_text(encoding="utf-8")

    assert '#define STEAM_GENERALS_ASSET_DIRECTORY "ZH_Generals"' in source
    assert "if (installPath.isEmpty())" in source
    assert "installPath = STEAM_GENERALS_ASSET_DIRECTORY;" in source
    assert 'loadBigFilesFromDirectory(installPath, "*.big")' in source
    assert "TheSuperHackers @bugfix Leex 25/08/2026" in source
