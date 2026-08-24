"""No-shell video stage process boundary tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from generals_replay_analyzer.engine.runner import ProcessExecution, ProcessLaunchRequest
from generals_replay_analyzer.video.process import (
    VideoProcessCancelled,
    VideoProcessError,
    VideoProcessRunner,
    VideoProcessSpec,
)

RUN_ID = "10000000-0000-4000-8000-000000000001"


def _spec(tmp_path: Path, *arguments: str) -> VideoProcessSpec:
    executable = (tmp_path / "tools" / "ffmpeg.exe").resolve()
    executable.parent.mkdir()
    executable.write_bytes(b"binary")
    run_dir = (tmp_path / "video-runs" / RUN_ID).resolve()
    run_dir.mkdir(parents=True)
    return VideoProcessSpec(
        stage="mux",
        run_id=RUN_ID,
        argv=(str(executable), *arguments),
        cwd=executable.parent,
        stdout_path=run_dir / "mux.stdout.log",
        stderr_path=run_dir / "mux.stderr.log",
        timeout_seconds=30,
    )


def test_runner_preserves_malicious_filenames_as_one_argv_item_and_never_uses_shell(tmp_path: Path) -> None:
    malicious = str((tmp_path / "replay & whoami; $(touch nope).mp4").resolve())
    observed: list[ProcessLaunchRequest] = []

    def launcher(request: ProcessLaunchRequest) -> ProcessExecution:
        observed.append(request)
        request.stdout_handle.write(b"ok")
        request.stderr_handle.write(b"diagnostic")
        return ProcessExecution(0, False, 0.25, False, None)

    result = VideoProcessRunner(launcher=launcher).run(_spec(tmp_path, "-i", malicious))

    assert observed[0].argv[-1] == malicious
    assert observed[0].shell is False
    assert result.exit_code == 0
    assert result.stdout_path.read_bytes() == b"ok"
    assert result.stderr_path.read_bytes() == b"diagnostic"


@pytest.mark.parametrize(
    ("execution", "diagnostic"),
    [
        (ProcessExecution(7, False, 0.5, False, None), "exit code 7"),
        (ProcessExecution(-1, True, 30.0, True, "job_object"), "timed out"),
    ],
)
def test_runner_raises_typed_failure_for_nonzero_or_timeout(
    tmp_path: Path,
    execution: ProcessExecution,
    diagnostic: str,
) -> None:
    def launcher(request: ProcessLaunchRequest) -> ProcessExecution:
        request.stderr_handle.write(b"bounded failure")
        return execution

    with pytest.raises(VideoProcessError, match=diagnostic) as raised:
        VideoProcessRunner(launcher=launcher).run(_spec(tmp_path, "-version"))

    assert raised.value.result is not None
    assert raised.value.result.stderr_path.read_bytes() == b"bounded failure"


def test_runner_rejects_output_collisions_before_launch(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "-version")
    spec.stdout_path.write_text("caller owned", encoding="utf-8")
    called = False

    def launcher(request: ProcessLaunchRequest) -> ProcessExecution:
        nonlocal called
        called = True
        return ProcessExecution(0, False, 0.1, False, None)

    with pytest.raises(VideoProcessError, match="exclusive process logs"):
        VideoProcessRunner(launcher=launcher).run(spec)
    assert called is False
    assert spec.stdout_path.read_text(encoding="utf-8") == "caller owned"


def test_runner_propagates_cancellation_to_the_hardened_child_tree_launcher(tmp_path: Path) -> None:
    cancellation = _Cancellation()
    spec = _spec(tmp_path, "-version").model_copy(update={"cancellation": cancellation})
    observed: list[ProcessLaunchRequest] = []

    def launcher(request: ProcessLaunchRequest) -> ProcessExecution:
        observed.append(request)
        return ProcessExecution(1, False, 0.1, True, "windows_job_object_cancellation", True)

    with pytest.raises(VideoProcessCancelled) as raised:
        VideoProcessRunner(launcher=launcher).run(spec)

    assert observed[0].cancellation is cancellation
    assert raised.value.result is not None
    assert raised.value.result.process_tree_terminated is True


class _Cancellation:
    def is_set(self) -> bool:
        return True


def test_process_spec_rejects_shell_strings_nul_and_relative_executables(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "-version")
    for argv in (("ffmpeg.exe", "-version"), (spec.argv[0], "bad\0argument"), (spec.argv[0],)):
        values = spec.model_dump()
        values["argv"] = argv
        if len(argv) == 1:
            values["argv"] = ()
        with pytest.raises(ValueError):
            VideoProcessSpec.model_validate(values)

