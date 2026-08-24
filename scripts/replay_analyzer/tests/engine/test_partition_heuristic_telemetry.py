"""Deterministic contracts for bounded engine partition heuristic samples."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _compile_lattice_probe(
    source: Path,
    executable: Path,
    include_directory: Path,
) -> subprocess.CompletedProcess[str]:
    if os.name == "nt":
        vswhere = Path(
            os.environ.get(
                "ProgramFiles(x86)",
                r"C:\Program Files (x86)",
            )
        ) / "Microsoft Visual Studio/Installer/vswhere.exe"
        if not vswhere.is_file():
            pytest.skip("Visual Studio discovery is unavailable")
        discovered = subprocess.run(
            [str(vswhere), "-latest", "-property", "installationPath"],
            capture_output=True,
            text=True,
            check=False,
        )
        installation = Path(discovered.stdout.strip())
        vcvars = installation / "VC/Auxiliary/Build/vcvarsall.bat"
        if discovered.returncode != 0 or not vcvars.is_file():
            pytest.skip("Visual Studio C++ x86 tools are unavailable")
        batch = source.parent / "compile_lattice_probe.cmd"
        batch.write_text(
            "@echo off\n"
            f'call "{vcvars}" x86 >nul\n'
            "if errorlevel 1 exit /b %errorlevel%\n"
            f'cl.exe /nologo /std:c++17 /EHsc /I"{include_directory}" '
            f'"{source}" /Fe:"{executable}"\n',
            encoding="utf-8",
        )
        return subprocess.run(
            ["cmd.exe", "/d", "/c", str(batch)],
            cwd=source.parent,
            capture_output=True,
            text=True,
            check=False,
        )

    compiler = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("a C++17 compiler is unavailable")
    return subprocess.run(
        [
            compiler,
            "-std=c++17",
            f"-I{include_directory}",
            str(source),
            "-o",
            str(executable),
        ],
        cwd=source.parent,
        capture_output=True,
        text=True,
        check=False,
    )


def test_integer_lattice_covers_tiny_and_large_grids_without_duplicates(
    tmp_path: Path,
    repository_root: Path,
) -> None:
    """Catch fractional selection, missing edges, wrong tie breaks, duplicates, or column-major output."""
    source = tmp_path / "lattice_probe.cpp"
    executable = tmp_path / ("lattice_probe.exe" if os.name == "nt" else "lattice_probe")
    source.write_text(
        r'''
#define RTS_REPLAY_ANALYZER 1
typedef int Int;
typedef unsigned int UnsignedInt;
#include "Common/ReplayPartitionSampler.h"

#include <cstddef>
#include <set>
#include <utility>
#include <vector>

static int requireUniqueAndBounded(
	const std::vector<ReplayPartitionLatticeCell> &cells,
	Int cellCountX,
	Int cellCountY)
{
	std::set<std::pair<Int, Int> > unique;
	for (std::size_t index = 0; index < cells.size(); ++index)
	{
		if (cells[index].cellX < 0 || cells[index].cellX >= cellCountX
			|| cells[index].cellY < 0 || cells[index].cellY >= cellCountY)
		{
			return 1;
		}
		unique.insert(std::make_pair(cells[index].cellX, cells[index].cellY));
	}
	return unique.size() == cells.size() && cells.size() <= 128 ? 0 : 2;
}

int main()
{
	const std::vector<ReplayPartitionLatticeCell> one =
		ReplayPartitionSampler::selectLatticeCells(1, 1);
	if (one.size() != 1 || one[0].cellX != 0 || one[0].cellY != 0)
	{
		return 10;
	}

	const std::vector<ReplayPartitionLatticeCell> tiny =
		ReplayPartitionSampler::selectLatticeCells(2, 3);
	const Int expectedTiny[][2] = {
		{ 0, 0 }, { 1, 0 }, { 0, 1 }, { 1, 1 }, { 0, 2 }, { 1, 2 }
	};
	if (tiny.size() != 6 || requireUniqueAndBounded(tiny, 2, 3) != 0)
	{
		return 11;
	}
	for (std::size_t index = 0; index < tiny.size(); ++index)
	{
		if (tiny[index].cellX != expectedTiny[index][0]
			|| tiny[index].cellY != expectedTiny[index][1])
		{
			return 12;
		}
	}

	const std::vector<ReplayPartitionLatticeCell> wide =
		ReplayPartitionSampler::selectLatticeCells(200, 100);
	if (wide.size() != 128 || requireUniqueAndBounded(wide, 200, 100) != 0
		|| wide.front().cellX != 0 || wide.front().cellY != 0
		|| wide.back().cellX != 199 || wide.back().cellY != 99)
	{
		return 13;
	}
	if (wide[15].cellX != 199 || wide[15].cellY != 0
		|| wide[16].cellX != 0 || wide[16].cellY != 14)
	{
		return 14;
	}

	const std::vector<ReplayPartitionLatticeCell> distorted =
		ReplayPartitionSampler::selectLatticeCells(3, 100);
	if (distorted.size() != 128 || distorted[1].cellX != 2
		|| distorted[2].cellX != 0 || distorted[2].cellY != 1
		|| distorted.back().cellX != 2 || distorted.back().cellY != 99)
	{
		return 15;
	}

	const std::vector<ReplayPartitionLatticeCell> square =
		ReplayPartitionSampler::selectLatticeCells(100, 100);
	if (square.size() != 128 || square[15].cellX != 99 || square[15].cellY != 0
		|| square[16].cellX != 0 || square[16].cellY != 14)
	{
		return 16;
	}

	const std::vector<ReplayPartitionLatticeCell> repeated =
		ReplayPartitionSampler::selectLatticeCells(200, 100);
	if (repeated.size() != wide.size())
	{
		return 17;
	}
	for (std::size_t index = 0; index < wide.size(); ++index)
	{
		if (repeated[index].cellX != wide[index].cellX
			|| repeated[index].cellY != wide[index].cellY)
		{
			return 18;
		}
	}
	return 0;
}
''',
        encoding="utf-8",
    )
    include_directory = repository_root / "GeneralsMD/Code/GameEngine/Include"
    compiled = _compile_lattice_probe(source, executable, include_directory)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr

    completed = subprocess.run(
        [str(executable)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_partition_provider_uses_exact_direct_reads_and_truthful_labels(
    repository_root: Path,
) -> None:
    """Catch debug-normalized values, approximate geometry, unsafe shroud reads, or misleading semantics."""
    header_path = repository_root / "GeneralsMD/Code/GameEngine/Include/Common/ReplayPartitionSampler.h"
    source_path = repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayPartitionSampler.cpp"
    assert header_path.is_file() and source_path.is_file()

    header = header_path.read_text(encoding="utf-8")
    source = source_path.read_text(encoding="utf-8")
    guard = "defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)"
    assert guard in header and guard in source
    assert "TheSuperHackers @feature Leex 23/08/2026" in header
    assert "TheSuperHackers @feature Leex 23/08/2026" in source

    assert "ThePartitionManager->getCellCenterPos" in source
    assert "cell->getShroudStatusForPlayer(playerIndex)" in source
    assert "cell->getThreatValue(playerIndex)" in source
    assert "cell->getCashValue(playerIndex)" in source
    assert '"PartitionCell::getShroudStatusForPlayer"' in source
    assert '"PartitionCell::getThreatValue"' in source
    assert '"PartitionCell::getCashValue"' in source
    # The closed telemetry payload carries provider and semantic labels; the
    # human-facing "Engine AI ... heuristic" labels belong to the map-v2 view.
    assert '"uniform_partition_lattice_v1"' in source
    assert '"engine_ai_owner_contribution_heuristic"' in source

    forbidden = (
        "getShroudedStatus(",
        "m_everSeenByPlayer",
        "GhostObject",
        "LogicRandom",
        "ClientRandom",
        "RandomValue",
        "rand(",
        "getMostValuableLocation",
        "getMostDangerousLocation",
        "debugDisplay",
        "DisplayMax",
        "normalize",
        "setPosition(",
        "setShroud",
        "doThreatAffect",
        "doValueAffect",
    )
    assert not [token for token in forbidden if token in source]

    selection = header.split("selectLatticeCells", maxsplit=1)[1].split("static void reset", maxsplit=1)[0]
    assert not [token for token in ("float", "double", "Real") if token in selection]


def test_partition_provider_bounds_players_cadence_terminal_and_event_order(
    repository_root: Path,
) -> None:
    """Catch unbounded player scans, a missing terminal sample, duplicate terminal frames, or unstable output order."""
    source = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayPartitionSampler.cpp"
    ).read_text(encoding="utf-8")

    assert "SAMPLE_INTERVAL_FRAMES = LOGICFRAMES_PER_SECOND * 10" in source
    header = (
        repository_root / "GeneralsMD/Code/GameEngine/Include/Common/ReplayPartitionSampler.h"
    ).read_text(encoding="utf-8")
    assert "MAXIMUM_SAMPLED_CELLS = 128" in header
    assert "for (Int slotIndex = 0; slotIndex < MAX_SLOTS; ++slotIndex)" in source
    assert "std::sort(playerIndices.begin(), playerIndices.end())" in source
    assert "std::unique(playerIndices.begin(), playerIndices.end())" in source
    assert "for (const Int playerIndex : playerIndices)" in source
    assert "for (const ReplayPartitionLatticeCell &coordinate : coordinates)" in source
    assert 'ReplayTelemetry::emit(frame, "partition_engine_grid_sample"' in source

    periodic = source.split("void ReplayPartitionSampler::sampleEndOfFrame()", maxsplit=1)[1].split(
        "void ReplayPartitionSampler::emitTerminalSample", maxsplit=1
    )[0]
    terminal = source.split("void ReplayPartitionSampler::emitTerminalSample", maxsplit=1)[1]
    assert "frame == 0 || frame % SAMPLE_INTERVAL_FRAMES != 0" in periodic
    assert "emitSamples(frame)" in periodic
    assert "emitSamples(finalFrame)" in terminal
    assert "s_lastSampleFrame == frame" in source
    assert "s_hasSampledFrame" in source

    retained_state = source.split("struct ReplayPartitionSamplerState", maxsplit=1)[1].split(
        "};", maxsplit=1
    )[0]
    assert "Object *" not in retained_state
    assert "Player *" not in retained_state
    assert "PartitionCell *" not in retained_state
