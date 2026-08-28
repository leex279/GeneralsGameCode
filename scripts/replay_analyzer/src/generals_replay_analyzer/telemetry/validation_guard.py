"""Race-proof private process-tree ownership for telemetry validation.

Linux uses a scoped child subreaper and Windows uses the existing suspended-launch
kill-on-close Job Object. Other POSIX platforms are rejected by the importer before
this module starts because they provide neither ownership primitive here.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import FrameType
from typing import BinaryIO, NoReturn, cast

_SIGKILL = int(getattr(signal, "SIGKILL", 9))


class _GuardianTermination(BaseException):
    def __init__(self, signal_number: int) -> None:
        super().__init__(signal_number)
        self.signal_number = signal_number


def _request_termination(signal_number: int, _frame: FrameType | None) -> NoReturn:
    raise _GuardianTermination(signal_number)


def _linux_child_process_ids(root_process_id: int) -> tuple[int, ...]:
    descendants: list[int] = []
    pending = [root_process_id]
    visited = {root_process_id}
    while pending:
        parent_id = pending.pop()
        try:
            children = Path(f"/proc/{parent_id}/task/{parent_id}/children").read_text(
                encoding="ascii"
            )
        except OSError:
            continue
        for value in children.split():
            try:
                child_id = int(value)
            except ValueError:
                continue
            if child_id in visited:
                continue
            visited.add(child_id)
            descendants.append(child_id)
            pending.append(child_id)
    return tuple(descendants)


def _enable_linux_child_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        error_number = ctypes.get_errno()
        raise OSError(error_number, "telemetry validator guardian could not become a child subreaper")


def _kill_and_reap_linux_children(root_process_id: int) -> None:
    while True:
        # TheSuperHackers @bugfix Leex 26/08/2026 Rescan after each reap so children orphaned during cleanup cannot outlive this private subreaper. (#TBD)
        process_ids = _linux_child_process_ids(root_process_id)
        for process_id in reversed(process_ids):
            try:
                os.kill(process_id, _SIGKILL)
            except ProcessLookupError:
                continue
        while True:
            try:
                os.waitpid(-1, 0)
                break
            except ChildProcessError:
                return


def _run_linux(command: list[str]) -> int:
    # TheSuperHackers @bugfix Leex 26/08/2026 Keep a scoped subreaper alive until every validator descendant is killed and reaped. (#TBD)
    _enable_linux_child_subreaper()
    previous_handler = signal.signal(signal.SIGTERM, _request_termination)
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, close_fds=True)
        try:
            return process.wait()
        except _GuardianTermination as interruption:
            return 128 + interruption.signal_number
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        _kill_and_reap_linux_children(os.getpid())
        signal.signal(signal.SIGTERM, previous_handler)


def _run_windows(command: list[str]) -> int:
    from ..engine.runner import ProcessLaunchRequest, _windows_process_launcher

    # TheSuperHackers @bugfix Leex 26/08/2026 Assign validation suspended to a kill-on-close Job before descendants can race its leader. (#TBD)
    working_directory = Path.cwd()
    execution = _windows_process_launcher(
        ProcessLaunchRequest(
            run_id="telemetry-validation-guard",
            run_dir=working_directory,
            argv=tuple(command),
            cwd=working_directory,
            stdout_path=working_directory,
            stderr_path=working_directory,
            stdout_handle=cast(BinaryIO, sys.stdout.buffer),
            stderr_handle=cast(BinaryIO, sys.stderr.buffer),
            timeout_seconds=24 * 60 * 60,
        )
    )
    return execution.exit_code


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        return 2
    if sys.platform == "win32":
        return _run_windows(command)
    if sys.platform.startswith("linux"):
        return _run_linux(command)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
