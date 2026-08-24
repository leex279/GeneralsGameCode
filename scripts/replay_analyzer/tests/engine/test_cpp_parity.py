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


def test_analyzer_capture_locals_do_not_break_non_analyzer_vc6(repository_root: Path) -> None:
    """Keep passive analyzer bookkeeping absent or referenced when VC6 excludes its consumers."""
    money = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/RTS/Money.cpp"
    ).read_text(encoding="utf-8")
    recorder = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp"
    ).read_text(encoding="utf-8")
    supply_center = (
        repository_root
        / "GeneralsMD/Code/GameEngine/Source/GameLogic/Object/Update/DockUpdate/SupplyCenterDockUpdate.cpp"
    ).read_text(encoding="utf-8")
    analyzer_guarded_capture = (
        "#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)\n"
        "\tconst UnsignedInt replayAnalyzerBefore = m_money;"
    )

    assert money.count(analyzer_guarded_capture) == 3
    assert (
        "#if !defined(RTS_REPLAY_ANALYZER)\n"
        "\t// TheSuperHackers @build Leex 24/08/2026 Reference analyzer-only setup read counts when their consumer is excluded. (#TBD)\n"
        "\t(void)setupStartOffset;"
    ) in recorder
    assert (
        "#if defined(RTS_REPLAY_ANALYZER)\n"
        "\tconst Int commandStartOffset = m_file->seek(0, File::CURRENT);\n"
        "#endif"
    ) in recorder
    assert (
        "#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)\n"
        "\tconst Int deliveredBoxes = supplyTruckAI->getNumberBoxes();\n"
        "#endif"
    ) in supply_center


def test_zero_hour_orientation_constructs_signed_basis_directly(repository_root: Path) -> None:
    """Keep zero-angle orientation bytes independent of compiler cross-product optimization."""
    thing_source = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/Thing/Thing.cpp"
    ).read_text(encoding="utf-8")

    assert (
        "// TheSuperHackers @bugfix Leex 24/08/2026 Build the planar basis directly so modern compilers preserve the retail signed-zero layout. (#TBD)\n"
        "\t\tx.x = u.x;\n"
        "\t\tx.y = u.y;\n"
        "\t\tx.z = 0.0f;\n"
        "\t\ty.x = -u.y;\n"
        "\t\ty.y = u.x;\n"
        "\t\ty.z = 0.0f;"
    ) in thing_source
    assert "y.crossProduct( z, u, y );" not in thing_source
    assert "x.crossProduct( y, z, x );" not in thing_source


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
    assert "Bool readCRC(CRCRecord &record);" in crc_info
    assert "std::list<CRCRecord> m_data;" in crc_info
    assert crc_implementation.index("if (!m_skippedOne)") < crc_implementation.index(
        "m_data.push_back(CRCRecord(val, frame))"
    )
    assert crc_implementation.index("record = m_data.front()") < crc_implementation.index(
        "m_data.pop_front()"
    )
    assert "return FALSE;" in crc_implementation
    assert "record = m_data.front();" in crc_implementation
    assert "return TRUE;" in crc_implementation


@pytest.mark.parametrize(
    "relative_path",
    (
        "GeneralsMD/Code/GameEngine/Source/GameLogic/System/GameLogic.cpp",
        "Generals/Code/GameEngine/Source/GameLogic/System/GameLogic.cpp",
    ),
)
def test_locally_generated_playback_crc_carries_its_snapshot_frame_on_both_dispatch_paths(
    repository_root: Path,
    relative_path: str,
) -> None:
    """Rendered MessageStream latency must not replace the exact CRC calculation frame."""
    source = (repository_root / relative_path).read_text(encoding="utf-8")
    generation = source.split("if (generateForSolo || generateForMP)", maxsplit=1)[1].split(
        "// collect stats", maxsplit=1
    )[0]

    assert "msg->appendBooleanArgument(isPlayback);" in generation
    assert "if (isPlayback)" in generation
    assert "msg->appendIntegerArgument(m_frame);" in generation
    assert generation.index("msg->appendIntegerArgument(m_frame);") < generation.index(
        "GameMessageList *messageList = TheMessageStream;"
    )
    assert "RECORDERMODETYPE_SIMULATION_PLAYBACK" in generation


def test_crc_dispatch_forwards_snapshot_frame_presence_without_changing_legacy_messages(
    repository_root: Path,
) -> None:
    """Only three-argument local playback CRCs may provide a snapshot frame."""
    source = (
        repository_root / "Core/GameEngine/Source/GameLogic/System/GameLogicDispatch.cpp"
    ).read_text(encoding="utf-8")
    dispatch = source.split("else if (TheRecorder && TheRecorder->isPlaybackMode())", maxsplit=1)[1].split(
        "return true;", maxsplit=1
    )[0]

    assert "const Bool fromPlayback" in dispatch
    assert "msg->getArgumentCount() > 2" in dispatch
    assert "const Bool hasSnapshotFrame = fromPlayback" in dispatch
    assert "msg->getArgument(2)->integer" in dispatch
    assert "hasSnapshotFrame" in dispatch.split("TheRecorder->handleCRCMessage", maxsplit=1)[1]


@pytest.mark.parametrize(
    "relative_path",
    (
        "GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp",
        "Generals/Code/GameEngine/Source/Common/Recorder.cpp",
    ),
)
def test_empty_crc_queue_never_publishes_a_fabricated_frame_zero_mismatch(
    repository_root: Path,
    relative_path: str,
) -> None:
    """A missing local snapshot is absence, never the synthetic record (0, 0)."""
    source = (repository_root / relative_path).read_text(encoding="utf-8")
    handler = source.split("void RecorderClass::handleCRCMessage", maxsplit=1)[1].split(
        "Bool RecorderClass::playbackFile", maxsplit=1
    )[0]

    assert "if (!m_crcInfo.readCRC(playbackCRC))" in handler
    assert handler.index("if (!m_crcInfo.readCRC(playbackCRC))") < handler.index(
        "newCRC != playbackCRC.value"
    )
    assert "m_crcInfo.addCRC(newCRC, snapshotFrame)" in handler
    assert "m_crcInfo.addCRC(newCRC, TheGameLogic->getFrame())" not in handler


def test_zero_hour_replay_header_loop_is_legacy_vc6_scoped(repository_root: Path) -> None:
    """MSVC 6 treats repeated for-init declarations in one function as C2374."""
    source = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp"
    ).read_text(encoding="utf-8")
    header_reader = source.split("Bool RecorderClass::readReplayHeader", maxsplit=1)[1].split(
        "Bool RecorderClass::replayMatchesGameVersion", maxsplit=1
    )[0]

    assert "TheSuperHackers @build Leex 23/08/2026" in header_reader
    assert "Int playerDisconnectIndex = 0;" in header_reader
    assert header_reader.count("for (playerDisconnectIndex=0; playerDisconnectIndex<MAX_SLOTS; ++playerDisconnectIndex)") == 2


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
    assert "m_crcInfo.addCRC(newCRC, snapshotFrame)" in handler
    assert "playbackCRC.value" in handler
    assert "const UnsignedInt mismatchFrame = playbackCRC.frame;" in handler
    assert "TheGameLogic->getFrame() - m_crcInfo.GetQueueSize() - 1" not in handler
    assert handler.index('printf("CRC Mismatch in Frame %d\\n", mismatchFrame);') < handler.index(
        "fflush(stdout);"
    )


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
