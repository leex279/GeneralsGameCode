from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _source(repository_root: Path, relative: str) -> str:
    path = repository_root / relative
    assert path.is_file(), f"missing capture implementation: {relative}"
    return path.read_text(encoding="utf-8")


def _vcvars32() -> Path | None:
    candidates = (
        Path(
            r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools"
            r"\VC\Auxiliary\Build\vcvars32.bat"
        ),
        Path(
            r"C:\Program Files\Microsoft Visual Studio\2022\Community"
            r"\VC\Auxiliary\Build\vcvars32.bat"
        ),
        Path(
            r"C:\Program Files\Microsoft Visual Studio\18\Community"
            r"\VC\Auxiliary\Build\vcvars32.bat"
        ),
    )
    return next((path for path in candidates if path.is_file()), None)


def test_capture_flags_are_closed_and_modern_zero_hour_only(repository_root: Path) -> None:
    """Catch missing/dependent flags, loose numeric parsing, or legacy/base-game exposure."""
    command_line = _source(repository_root, "Core/GameEngine/Source/Common/CommandLine.cpp")
    zero_hour_header = _source(
        repository_root, "GeneralsMD/Code/GameEngine/Include/Common/GlobalData.h"
    )
    generals_header = _source(
        repository_root, "Generals/Code/GameEngine/Include/Common/GlobalData.h"
    )

    capture_section = command_line.split(
        "TheSuperHackers @feature Leex 23/08/2026 Validate native replay capture settings",
        maxsplit=1,
    )[1].split("TheSuperHackers @feature Leex 18/08/2026", maxsplit=1)[0]
    assert all(flag in capture_section for flag in ("-recordVideo", "-videoRes", "-videoFps"))
    assert "strtol" in capture_section
    assert "30" in capture_section and "60" in capture_section
    assert "m_headless" in capture_section
    assert "m_simulateReplays.size() != 1" in capture_section
    assert "SIMULATE_REPLAYS_SEQUENTIAL" in capture_section
    assert "#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)" in command_line
    assert "m_recordVideoPath" in zero_hour_header
    assert "m_videoCaptureWidth" in zero_hour_header
    assert "m_videoCaptureHeight" in zero_hour_header
    assert "m_videoCaptureFps" in zero_hour_header
    assert "m_recordVideoPath" not in generals_header


def test_writer_uses_owned_argv_only_win32_child(repository_root: Path) -> None:
    """Catch shell composition, unbounded writes, or output paths escaping their argv item."""
    header = _source(
        repository_root,
        "Core/GameEngineDevice/Include/W3DDevice/GameClient/W3DVideoWriter.h",
    )
    source = _source(
        repository_root,
        "Core/GameEngineDevice/Source/W3DDevice/GameClient/W3DVideoWriter.cpp",
    )
    combined = header + source

    assert "quoteWindowsArgument" in header
    assert "CreateProcessW" in source
    assert "STARTUPINFOEXW" in source
    assert "PROC_THREAD_ATTRIBUTE_HANDLE_LIST" in source
    assert "WriteFile" in source and "bytesWritten" in source
    assert "WaitForSingleObject" in source and "GetExitCodeProcess" in source
    assert "resolvedFfmpegPath.c_str()" in source
    assert "quoteWindowsArgument(m_outputPath)" in source
    assert all(
        forbidden not in combined
        for forbidden in ("_popen", "popen(", "system(", "ShellExecute", "cmd.exe", "powershell")
    )


def test_pixel_and_argv_helpers_execute_exact_contract(
    repository_root: Path, tmp_path: Path
) -> None:
    """Catch padding bytes leaking into RGB24 or Windows quoting changing an argument."""
    header = repository_root / (
        "Core/GameEngineDevice/Include/W3DDevice/GameClient/W3DVideoWriter.h"
    )
    assert header.is_file(), "missing capture writer header"
    vcvars = _vcvars32()
    if os.name != "nt" or vcvars is None:
        pytest.skip("MSVC x86 developer tools are unavailable")

    helper = tmp_path / "capture_contract.cpp"
    helper.write_text(
        r'''
#include "W3DDevice/GameClient/W3DVideoWriter.h"
#include <climits>
#include <string>
#include <vector>

int main()
{
	const unsigned char source[] = {
		1, 2, 3, 99, 4, 5, 6, 88, 0xAA, 0xBB, 0xCC, 0xDD,
		7, 8, 9, 77, 10, 11, 12, 66, 0x11, 0x22, 0x33, 0x44
	};
	const unsigned char expected[] = {3, 2, 1, 6, 5, 4, 9, 8, 7, 12, 11, 10};
	std::vector<unsigned char> converted;
	W3DVideoCaptureFailure failure = W3D_VIDEO_CAPTURE_OK;
	if (!W3DVideoCaptureContract::convertBgraToRgb24(source, 12, 2, 2, converted, failure)) return 1;
	if (converted.size() != sizeof(expected)) return 2;
	for (size_t index = 0; index < sizeof(expected); ++index)
		if (converted[index] != expected[index]) return 3;
	if (W3DVideoCaptureContract::convertBgraToRgb24(source, 7, 2, 2, converted, failure)) return 4;
	if (failure != W3D_VIDEO_CAPTURE_INVALID_PITCH) return 5;
	if (W3DVideoCaptureContract::convertBgraToRgb24(source, INT_MAX, 1, 3, converted, failure)) return 6;
	if (failure != W3D_VIDEO_CAPTURE_INVALID_PITCH) return 7;
	if (W3DVideoCaptureContract::presentationCopiesForFps(30) != 1) return 8;
	if (W3DVideoCaptureContract::presentationCopiesForFps(60) != 2) return 9;
	if (W3DVideoCaptureContract::presentationCopiesForFps(59) != 0) return 10;
	if (W3DVideoCaptureContract::quoteWindowsArgument(L"plain") != L"plain") return 11;
	if (W3DVideoCaptureContract::quoteWindowsArgument(L"C:\\render dir\\match & whoami.mp4")
		!= L"\"C:\\render dir\\match & whoami.mp4\"") return 12;
	if (W3DVideoCaptureContract::quoteWindowsArgument(L"C:\\render dir\\")
		!= L"\"C:\\render dir\\\\\"") return 13;
	if (W3DVideoCaptureContract::quoteWindowsArgument(L"a\"b") != L"\"a\\\"b\"") return 14;
	return 0;
}
''',
        encoding="utf-8",
    )
    executable = tmp_path / "capture_contract.exe"
    include_dir = repository_root / "Core/GameEngineDevice/Include"
    compile_script = tmp_path / "compile.cmd"
    compile_script.write_text(
        f'@call "{vcvars}" >nul\n'
        f'@if errorlevel 1 exit /b %errorlevel%\n'
        f'@cl.exe /nologo /EHsc /std:c++17 /I"{include_dir}" '
        f'"{helper}" /Fe:"{executable}"\n',
        encoding="utf-8",
    )
    compiled = subprocess.run(
        ["cmd.exe", "/d", "/c", str(compile_script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    executed = subprocess.run([executable], cwd=tmp_path, check=False)
    assert executed.returncode == 0


def test_capture_failures_and_sidecar_are_typed(repository_root: Path) -> None:
    """Catch silent D3D, lock/copy/write/pipe/process failures or an ambiguous result."""
    header = _source(
        repository_root,
        "Core/GameEngineDevice/Include/W3DDevice/GameClient/W3DVideoWriter.h",
    )
    source = _source(
        repository_root,
        "Core/GameEngineDevice/Source/W3DDevice/GameClient/W3DVideoWriter.cpp",
    )
    required_failures = (
        "UNSUPPORTED_SURFACE_FORMAT",
        "BACKBUFFER_QUERY_FAILED",
        "SURFACE_COPY_FAILED",
        "SURFACE_LOCK_FAILED",
        "INVALID_PITCH",
        "PIPE_CREATE_FAILED",
        "PROCESS_LAUNCH_FAILED",
        "PIPE_WRITE_FAILED",
        "PIPE_CLOSE_FAILED",
        "PROCESS_WAIT_FAILED",
        "PROCESS_EXIT_FAILED",
        "OUTPUT_MISSING",
    )
    assert all(f"W3D_VIDEO_CAPTURE_{name}" in header for name in required_failures)
    assert '\\"schema_version\\":1' in source
    assert '\\"status\\":\\"success\\"' in source
    assert '\\"status\\":\\"failed\\"' in source
    assert '\\"failure_code\\"' in source
    assert '\\"logic_frames\\"' in source
    assert '\\"presentation_frames\\"' in source
    assert ".capture-result.json" in source
    close_section = source.split("void W3DVideoWriter::close()", maxsplit=1)[1].split(
        "void W3DVideoWriter::fail", maxsplit=1
    )[0]
    assert "if (!m_opened || m_presentationFrames == 0)" in close_section
    assert "W3D_VIDEO_CAPTURE_OUTPUT_MISSING" in close_section
    open_section = source.split("bool W3DVideoWriter::open(", maxsplit=1)[1].split(
        "bool W3DVideoWriter::captureFrame", maxsplit=1
    )[0]
    assert "width != m_actualWidth" in open_section
    assert "fail(W3D_VIDEO_CAPTURE_INVALID_CONFIGURATION" in open_section
    capture_section = source.split("bool W3DVideoWriter::captureFrame(", maxsplit=1)[1].split(
        "bool W3DVideoWriter::writeAll", maxsplit=1
    )[0]
    assert "const HRESULT descriptionResult" in capture_section
    assert "static_cast<DWORD>(descriptionResult)" in capture_section


def test_display_captures_once_per_logic_frame_and_replay_owns_shutdown(
    repository_root: Path,
) -> None:
    """Catch render-loop recapture, a second simulation tick at 60 FPS, or writer-owned exit."""
    display = _source(
        repository_root,
        "GeneralsMD/Code/GameEngineDevice/Source/W3DDevice/GameClient/W3DDisplay.cpp",
    )
    writer = _source(
        repository_root,
        "Core/GameEngineDevice/Source/W3DDevice/GameClient/W3DVideoWriter.cpp",
    )
    capture_hook = display.split(
        "TheSuperHackers @feature Leex 23/08/2026 Capture one rendered replay frame",
        maxsplit=1,
    )[1].split("WW3D::End_Render();", maxsplit=1)[0]

    assert "TheGameLogic->isInReplayGame()" in capture_hook
    assert "TheGameLogic->getFrame()" in capture_hook
    assert "captureFrame" in capture_hook
    assert "close" not in capture_hook
    assert "m_lastLogicFrame" in writer
    assert "presentationCopiesForFps" in writer
    assert "for (int copy = 0; copy < presentationCopies; ++copy)" in writer
    assert "setQuitting" not in writer
    assert "update()" not in writer
    assert "TiVO" not in writer


def test_top_level_build_wiring_preserves_protected_device_cmake(repository_root: Path) -> None:
    """Catch accidental legacy/Base Generals inclusion or editing the protected target file."""
    root_cmake = _source(repository_root, "CMakeLists.txt")
    protected_cmake = _source(
        repository_root, "GeneralsMD/Code/GameEngineDevice/CMakeLists.txt"
    )
    assert "W3DVideoWriter.cpp" in root_cmake
    assert "target_sources(z_gameenginedevice PRIVATE" in root_cmake
    assert "if(RTS_BUILD_ZEROHOUR AND NOT IS_VS6_BUILD)" in root_cmake
    assert "W3DVideoWriter" not in protected_cmake


def test_zero_hour_globaldata_stacktrace_field_has_target_independent_layout(
    repository_root: Path,
) -> None:
    """Keep the modern Zero Hour GlobalData layout independent of target defines."""
    root_cmake = _source(repository_root, "CMakeLists.txt")
    global_data_header = _source(repository_root, "GeneralsMD/Code/GameEngine/Include/Common/GlobalData.h")
    global_data_source = _source(repository_root, "GeneralsMD/Code/GameEngine/Source/Common/GlobalData.cpp")
    layout_guard = "#if !defined(IS_VS6_BUILD) || defined(DEBUG_STACKTRACE)"

    assert "target_compile_definitions(z_gameenginedevice PRIVATE IG_DEBUG_STACKTRACE)" not in root_cmake
    assert "IG_DEBUG_STACKTRACE" not in global_data_header
    assert layout_guard in global_data_header
    assert layout_guard in global_data_source


def test_rendered_analyzer_replay_exits_when_playback_reaches_terminal_state(repository_root: Path) -> None:
    """Rendered capture must not wait indefinitely at the post-replay score screen."""
    source = _source(repository_root, "GeneralsMD/Code/GameEngine/Source/Common/GameEngine.cpp")
    assert "TheGlobalData->m_recordVideoPath.isEmpty()" in source
    assert "TheRecorder->isPlaybackInProgress()" in source
    assert "TheRecorder->sawCRCMismatch()" in source
    assert "TheGameEngine->setQuitting(TRUE)" in source


def test_rendered_capture_records_crc_mismatch_without_focus_dependent_pause(repository_root: Path) -> None:
    source = _source(repository_root, "GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp")
    mismatch = source.split("if (TheGameLogic->getFrame() > 0", maxsplit=1)[1].split("return;", maxsplit=1)[0]
    assert "m_recordVideoPath.isEmpty()" in mismatch
    assert "m_crcInfo.setSawCRCMismatch()" in mismatch
