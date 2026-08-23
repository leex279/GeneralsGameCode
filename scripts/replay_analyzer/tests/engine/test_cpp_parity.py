"""Opt-in integration gate against the modern Zero Hour replay observer."""

import os
import subprocess
from pathlib import Path

import pytest

from generals_replay_analyzer.contracts import default_message_catalog
from generals_replay_analyzer.parity import (
    CppDumpValidationError,
    compare_replay,
    cpp_message_catalog_entries,
    load_cpp_dump,
)
from generals_replay_analyzer.parser import parse_replay


@pytest.mark.parametrize(
    ("header_path", "source_path"),
    (
        (
            "GeneralsMD/Code/GameEngine/Include/Common/Recorder.h",
            "GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp",
        ),
        (
            "Generals/Code/GameEngine/Include/Common/Recorder.h",
            "Generals/Code/GameEngine/Source/Common/Recorder.cpp",
        ),
    ),
)
def test_crc_queue_keeps_atomic_snapshot_frame_value_records(
    repository_root: Path,
    header_path: str,
    source_path: str,
) -> None:
    """Reject value-only CRC queues that lose the deterministic snapshot frame during skip or FIFO reads."""
    header = (repository_root / header_path).read_text(encoding="utf-8")
    source = (repository_root / source_path).read_text(encoding="utf-8")
    crc_info = header.split("class CRCInfo", maxsplit=1)[1].split("\n\t};\n\npublic:", maxsplit=1)[0]
    crc_implementation = source.split("void RecorderClass::CRCInfo::addCRC", maxsplit=1)[1].split(
        "void RecorderClass::logGameStart", maxsplit=1
    )[0]

    assert "struct CRCRecord" in crc_info
    assert "UnsignedInt value;" in crc_info
    assert "UnsignedInt frame;" in crc_info
    assert "void addCRC(UnsignedInt val, UnsignedInt frame);" in crc_info
    assert "CRCRecord readCRC();" in crc_info
    assert "std::list<CRCRecord> m_data;" in crc_info
    assert crc_implementation.index("if (!m_skippedOne)") < crc_implementation.index(
        "m_data.push_back(CRCRecord(val, frame))"
    )
    assert crc_implementation.index("CRCRecord record = m_data.front()") < crc_implementation.index(
        "m_data.pop_front()"
    )
    assert "return record;" in crc_implementation


@pytest.mark.parametrize(
    "relative_path",
    (
        "GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp",
        "Generals/Code/GameEngine/Source/Common/Recorder.cpp",
    ),
)
def test_crc_comparison_uses_human_slot_policy_and_paired_snapshot_frame(
    repository_root: Path,
    relative_path: str,
) -> None:
    """Reject local-IP multiplayer classification and receive-frame queue arithmetic attribution."""
    source = (repository_root / relative_path).read_text(encoding="utf-8")
    setup = source.split("m_gameInfo.setLocalIP", maxsplit=1)[1].split(
        "m_crcInfo = CRCInfo", maxsplit=1
    )[0]
    handler = source.split("void RecorderClass::handleCRCMessage", maxsplit=1)[1].split(
        "Bool RecorderClass::playbackFile", maxsplit=1
    )[0]

    assert "m_gameInfo.isMultiPlayer()" in setup
    assert "getIP() != 0" not in setup
    assert "m_crcInfo.addCRC(newCRC, TheGameLogic->getFrame())" in handler
    assert "playbackCRC.value" in handler
    assert "const UnsignedInt mismatchFrame = playbackCRC.frame;" in handler
    assert "TheGameLogic->getFrame() - m_crcInfo.GetQueueSize() - 1" not in handler


def _runtime_environment(repository_root: Path) -> dict[str, str]:
    """Expose built proprietary dependency DLLs without copying files outside pytest's temporary tree."""
    environment = os.environ.copy()
    dependency_directories = (
        repository_root / "build" / "win32" / "_deps" / "bink-build" / "Release",
        repository_root / "build" / "win32" / "_deps" / "miles-build" / "Release",
    )
    environment["PATH"] = os.pathsep.join([*(str(path.resolve()) for path in dependency_directories), environment["PATH"]])
    return environment


def test_modern_engine_dump_matches_the_pinned_replay_byte_for_byte(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Fail honestly unless a complete real C++ dump matches Python and its packaged message catalog."""
    dump_path = (tmp_path / "cpp.ndjson").resolve()
    base_command = [
        str(zero_hour_runtime_executable),
        "-headless",
        "-noaudio",
        "-replay",
        str(pinned_replay),
    ]
    command = [
        *base_command,
        "-replay-parse-dump",
        str(dump_path),
    ]
    failed_dump_path = (tmp_path / "missing-parent" / "cpp.ndjson").resolve()
    failed_sink_command = [
        *base_command,
        "-replay-parse-dump",
        str(failed_dump_path),
    ]
    try:
        baseline = subprocess.run(
            base_command,
            cwd=zero_hour_runtime_executable.parent,
            env=_runtime_environment(repository_root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        completed = subprocess.run(
            command,
            cwd=zero_hour_runtime_executable.parent,
            env=_runtime_environment(repository_root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        failed_sink = subprocess.run(
            failed_sink_command,
            cwd=zero_hour_runtime_executable.parent,
            env=_runtime_environment(repository_root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"modern Zero Hour timed out before authoritative replay parity completed: {error}")

    if (
        completed.returncode != baseline.returncode
        or completed.stdout != baseline.stdout
        or completed.stderr != baseline.stderr
    ):
        pytest.fail(
            "replay parse dumping changed the engine's replay result or control flow: "
            f"baseline_returncode={baseline.returncode}; dump_returncode={completed.returncode}; "
            f"dump_exists={dump_path.exists()}; "
            f"dump_bytes={dump_path.stat().st_size if dump_path.exists() else 0}; "
            f"baseline_stdout={baseline.stdout[-2000:]!r}; baseline_stderr={baseline.stderr[-2000:]!r}; "
            f"dump_stdout={completed.stdout[-2000:]!r}; dump_stderr={completed.stderr[-2000:]!r}"
        )
    assert failed_sink.returncode == baseline.returncode, (
        "a configured dump path changed replay failure handling after the sink failed to open: "
        f"baseline_returncode={baseline.returncode}; failed_sink_returncode={failed_sink.returncode}; "
        f"failed_sink_stdout={failed_sink.stdout[-2000:]!r}; failed_sink_stderr={failed_sink.stderr[-2000:]!r}"
    )
    assert not failed_dump_path.exists()
    if not dump_path.is_file():
        pytest.fail(f"modern Zero Hour did not create a replay dump: {dump_path}")

    try:
        cpp_dump = load_cpp_dump(dump_path)
    except CppDumpValidationError as error:
        pytest.fail(f"modern Zero Hour produced a non-authoritative replay dump: {error}")

    parsed = parse_replay(pinned_replay)
    mismatch = compare_replay(parsed, cpp_dump)
    assert mismatch is None, str(mismatch)

    packaged_entries = tuple(default_message_catalog().names_by_id.items())
    assert cpp_message_catalog_entries(cpp_dump) == packaged_entries, (
        "packaged message catalog is not the generated C++ message_catalog record"
    )
