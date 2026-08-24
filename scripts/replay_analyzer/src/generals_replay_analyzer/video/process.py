"""Typed argv-only process boundary for video production stages."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator

from generals_replay_analyzer.engine.runner import (
    ProcessExecution,
    ProcessLauncher,
    ProcessLaunchRequest,
    default_process_launcher,
)
from generals_replay_analyzer.video.contracts import PublicId, VideoContract

VideoStage = Literal["camera_plan", "commentary_plan", "voice", "engine_capture", "mux", "verify"]


class VideoProcessSpec(VideoContract):
    stage: VideoStage
    run_id: PublicId
    argv: tuple[str, ...] = Field(min_length=2, max_length=256)
    cwd: Path
    stdout_path: Path
    stderr_path: Path
    timeout_seconds: int = Field(ge=1, le=86_400)
    cancellation: Any | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _require_safe_argv_and_paths(self) -> Self:
        if any(not argument or "\0" in argument for argument in self.argv):
            raise ValueError("process argv must contain non-empty NUL-free items")
        executable = Path(self.argv[0])
        if not executable.is_absolute():
            raise ValueError("process executable must be an absolute configured path")
        if not self.cwd.is_absolute() or not self.stdout_path.is_absolute() or not self.stderr_path.is_absolute():
            raise ValueError("process working and log paths must be absolute")
        if self.stdout_path == self.stderr_path:
            raise ValueError("stdout and stderr paths must differ")
        return self


class VideoProcessResult(VideoContract):
    stage: VideoStage
    exit_code: int
    timed_out: bool
    duration_seconds: float = Field(ge=0.0)
    process_tree_terminated: bool
    termination_method: str | None
    cancelled: bool = False
    stdout_path: Path
    stderr_path: Path


class VideoProcessError(RuntimeError):
    def __init__(self, message: str, result: VideoProcessResult | None = None) -> None:
        super().__init__(message)
        self.result = result


class VideoProcessCancelled(VideoProcessError):
    """An active child tree was settled because its durable job was cancelled."""


# TheSuperHackers @feature Leex 24/08/2026 Reuse the hardened engine process-tree launcher for every video subprocess. (#TBD)
class VideoProcessRunner:
    def __init__(self, *, launcher: ProcessLauncher = default_process_launcher) -> None:
        self._launcher = launcher

    def run(self, spec: VideoProcessSpec) -> VideoProcessResult:
        if type(spec) is not VideoProcessSpec:
            raise TypeError("video process runner requires a validated process specification")
        try:
            spec.stdout_path.parent.mkdir(parents=True, exist_ok=True)
            with spec.stdout_path.open("xb") as stdout_handle, spec.stderr_path.open("xb") as stderr_handle:
                execution = self._launcher(
                    ProcessLaunchRequest(
                        run_id=spec.run_id,
                        run_dir=spec.stdout_path.parent,
                        argv=spec.argv,
                        cwd=spec.cwd,
                        stdout_path=spec.stdout_path,
                        stderr_path=spec.stderr_path,
                        stdout_handle=stdout_handle,
                        stderr_handle=stderr_handle,
                        timeout_seconds=spec.timeout_seconds,
                        cancellation=cast(Any, spec.cancellation),
                    )
                )
        except FileExistsError as error:
            raise VideoProcessError("exclusive process logs already exist") from error
        except OSError as error:
            raise VideoProcessError(f"video process could not launch: {error}") from error
        result = self._result(spec, execution)
        if result.cancelled:
            raise VideoProcessCancelled(f"video stage {spec.stage} was cancelled", result)
        if result.timed_out:
            raise VideoProcessError(f"video stage {spec.stage} timed out", result)
        if result.exit_code != 0:
            raise VideoProcessError(f"video stage {spec.stage} failed with exit code {result.exit_code}", result)
        return result

    @staticmethod
    def _result(spec: VideoProcessSpec, execution: ProcessExecution) -> VideoProcessResult:
        return VideoProcessResult(
            stage=spec.stage,
            exit_code=execution.exit_code,
            timed_out=execution.timed_out,
            duration_seconds=execution.duration_seconds,
            process_tree_terminated=execution.process_tree_terminated,
            termination_method=execution.termination_method,
            cancelled=execution.cancelled,
            stdout_path=spec.stdout_path,
            stderr_path=spec.stderr_path,
        )

