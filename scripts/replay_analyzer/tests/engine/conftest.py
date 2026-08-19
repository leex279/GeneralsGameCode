"""Shared paths for opt-in modern-engine replay parity tests."""

import os
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Register and apply the engine marker even when pytest starts above the analyzer pyproject."""
    config.addinivalue_line("markers", "engine: launches the modern Zero Hour executable for parser parity")
    engine_directory = Path(__file__).parent
    for item in items:
        if engine_directory in Path(str(item.path)).resolve().parents:
            item.add_marker("engine")


@pytest.fixture(scope="session")
def repository_root() -> Path:
    """Return the checkout root without depending on the pytest invocation directory."""
    return Path(__file__).resolve().parents[4]


@pytest.fixture(scope="session")
def zero_hour_executable(repository_root: Path) -> Path:
    """Require an explicitly built modern Zero Hour executable, or skip for that absent toolchain artifact."""
    override = os.environ.get("GENERALS_REPLAY_ANALYZER_EXE")
    executable = (
        Path(override)
        if override
        else repository_root / "build" / "win32" / "GeneralsMD" / "Release" / "generalszh.exe"
    )
    if not executable.is_file():
        pytest.skip(f"modern Zero Hour executable is absent: {executable}; build target z_generals first")
    return executable.resolve()


@pytest.fixture(scope="session")
def pinned_replay(repository_root: Path) -> Path:
    """Return the checked-in real replay as an absolute path; never copy it into the user profile."""
    replay = (
        repository_root
        / "scripts"
        / "replay_analyzer"
        / "tests"
        / "fixtures"
        / "zero_hour_1_04"
        / "leex279_vs_fox27.rep"
    )
    if not replay.is_file():
        pytest.fail(f"pinned replay fixture is missing: {replay}")
    return replay.resolve()
