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
