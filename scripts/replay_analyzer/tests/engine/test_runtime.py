"""Native security contracts for runtime executable binding."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from generals_replay_analyzer.engine import runtime as runtime_module
from generals_replay_analyzer.engine.config import EngineRunConfigurationError
from generals_replay_analyzer.engine.runtime import bind_runtime_executable

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows share-mode contracts require NTFS handles")


def _binding_inputs(tmp_path: Path) -> tuple[Path, Path]:
    source = (tmp_path / "build" / "generalszh.exe").resolve()
    runtime = (tmp_path / "installed Zero Hour").resolve()
    source.parent.mkdir()
    runtime.mkdir()
    source.write_bytes(b"instrumented-engine-build")
    return source, runtime


def test_staged_binding_denies_writes_through_every_hardlink(tmp_path: Path) -> None:
    """Catch a writer changing the selected executable after binding validation."""
    source, runtime = _binding_inputs(tmp_path)

    with bind_runtime_executable(source, runtime) as binding:
        for candidate in (source, binding.launch_executable):
            with pytest.raises(PermissionError), candidate.open("r+b") as output:
                output.write(b"attacker")
        assert source.read_bytes() == b"instrumented-engine-build"
        assert os.path.samefile(source, binding.launch_executable)


def test_staged_binding_denies_unlink_rename_and_replacement(tmp_path: Path) -> None:
    """Catch pathname replacement while the validated executable is launchable."""
    source, runtime = _binding_inputs(tmp_path)
    with bind_runtime_executable(source, runtime) as binding:
        staged = binding.launch_executable
        for index, candidate in enumerate((source, staged)):
            renamed = candidate.with_name(f"renamed-{index}.exe")
            replacement = runtime / f"attacker-{index}.exe"
            replacement.write_bytes(b"attacker-replacement")
            with pytest.raises(PermissionError):
                candidate.unlink()
            with pytest.raises(PermissionError):
                candidate.rename(renamed)
            with pytest.raises(PermissionError):
                os.replace(replacement, candidate)
            assert replacement.read_bytes() == b"attacker-replacement"
        assert staged.exists()
        assert os.path.samefile(source, staged)


def test_staged_binding_cleanup_uses_its_owned_handle_not_path_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch cleanup reverting to a check-then-unlink pathname race."""
    source, runtime = _binding_inputs(tmp_path)
    staged: Path | None = None
    original_unlink = Path.unlink

    def reject_staged_path_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if staged is not None and path == staged:
            raise AssertionError("cleanup used pathname unlink")
        original_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", reject_staged_path_unlink)
    with bind_runtime_executable(source, runtime) as binding:
        staged = binding.launch_executable

    assert staged is not None and not staged.exists()
    assert source.read_bytes() == b"instrumented-engine-build"


@pytest.mark.parametrize("interruption", [RuntimeError("launch failed"), KeyboardInterrupt("cancelled")])
def test_staged_binding_cleans_up_after_launch_exception_or_cancellation(
    tmp_path: Path,
    interruption: BaseException,
) -> None:
    """Catch launch failures and cancellation leaking a locked staged executable."""
    source, runtime = _binding_inputs(tmp_path)
    staged: Path | None = None

    with (
        pytest.raises(type(interruption), match=str(interruption)),
        bind_runtime_executable(source, runtime) as binding,
    ):
        staged = binding.launch_executable
        raise interruption

    assert staged is not None and not staged.exists()
    assert source.read_bytes() == b"instrumented-engine-build"


def test_same_directory_binding_locks_without_staging_or_deleting_source(tmp_path: Path) -> None:
    """Catch the no-staging branch yielding an unprotected configured executable."""
    runtime = (tmp_path / "installed Zero Hour").resolve()
    runtime.mkdir()
    source = runtime / "generalszh.exe"
    source.write_bytes(b"installed-instrumented-engine")

    with bind_runtime_executable(source, runtime) as binding:
        assert binding.staged is False
        assert binding.launch_executable == source
        with pytest.raises(PermissionError):
            source.write_bytes(b"attacker")
        with pytest.raises(PermissionError):
            source.unlink()

    assert source.read_bytes() == b"installed-instrumented-engine"


def test_create_process_can_launch_an_executable_while_binding_lock_is_held(tmp_path: Path) -> None:
    """Catch a share mode that blocks the Windows image loader together with attackers."""
    system_root = Path(os.environ["SystemRoot"])
    system_command = (system_root / "System32" / "cmd.exe").resolve()
    source = (tmp_path / "build" / "launchable.exe").resolve()
    runtime = (tmp_path / "installed Zero Hour").resolve()
    source.parent.mkdir()
    runtime.mkdir()
    shutil.copyfile(system_command, source)

    with bind_runtime_executable(source, runtime) as binding:
        completed = subprocess.run(
            [binding.launch_executable, "/d", "/c", "exit", "0"],
            cwd=runtime,
            check=False,
            capture_output=True,
            timeout=10,
        )

    assert completed.returncode == 0


def test_cross_volume_binding_fails_closed_before_creating_a_destination(
    tmp_path: Path,
) -> None:
    """Catch hardlink staging proceeding when source and runtime volume identities differ."""
    _source, runtime = _binding_inputs(tmp_path)
    source_identity = runtime_module._WindowsFileIdentity(11, 101, 0, 12)
    runtime_identity = runtime_module._WindowsFileIdentity(22, 202, 0x10, 0)

    with pytest.raises(EngineRunConfigurationError, match="different volumes"):
        runtime_module._win_require_same_volume(source_identity, runtime_identity)
    assert list(runtime.glob("generalszh_replay_analyzer_*.exe")) == []


def test_platform_without_mandatory_share_locks_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch POSIX advisory-lock semantics being presented as an immutable binding."""
    source, runtime = _binding_inputs(tmp_path)
    monkeypatch.setattr(runtime_module.os, "name", "posix")

    with (
        pytest.raises(EngineRunConfigurationError, match="unavailable on this platform"),
        bind_runtime_executable(source, runtime),
    ):
        pytest.fail("insecure platform binding unexpectedly yielded")
