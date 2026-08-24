"""Safe orchestration for native evidence-backed commented replay casts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import Field

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.report.read_model import PublishedReportGraphDTO
from generals_replay_analyzer.spatial.query import MapSceneReadModel
from generals_replay_analyzer.video.camera import CameraPlanService
from generals_replay_analyzer.video.commentary import CommentaryPlanService
from generals_replay_analyzer.video.contracts import (
    CameraPlanAuthorityV1,
    CameraPlanV1,
    CommentaryPlanV1,
    PublicId,
    Sha256,
    ValidatedCommentaryEnrichmentV1,
    VideoContract,
    VideoSettingsV1,
)
from generals_replay_analyzer.video.manifest import ArtifactHashV1, VideoManifestPublisher, VideoManifestV1
from generals_replay_analyzer.video.process import (
    VideoProcessCancelled,
    VideoProcessResult,
    VideoProcessRunner,
    VideoProcessSpec,
    VideoStage,
)
from generals_replay_analyzer.video.subtitles import render_webvtt
from generals_replay_analyzer.video.verify import VerificationLandmarkV1, VerifiedMediaV1
from generals_replay_analyzer.video.voice import (
    NarrationScheduler,
    VoiceClipV1,
    VoiceProvider,
    render_narration_wav,
)

_STAGES: tuple[VideoStage, ...] = (
    "camera_plan",
    "commentary_plan",
    "voice",
    "engine_capture",
    "mux",
    "verify",
)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_HASH_CHUNK_SIZE = 1024 * 1024


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


class CameraPlanner(Protocol):
    def create(
        self,
        authority: CameraPlanAuthorityV1,
        report: PublishedReportGraphDTO,
        scene: MapSceneReadModel,
    ) -> CameraPlanV1: ...


class CommentaryPlanner(Protocol):
    def create(
        self,
        report: PublishedReportGraphDTO,
        camera: CameraPlanV1,
        *,
        enrichment: ValidatedCommentaryEnrichmentV1 | None = None,
    ) -> CommentaryPlanV1: ...


class ProcessRunner(Protocol):
    def run(self, spec: VideoProcessSpec) -> VideoProcessResult: ...


class MediaVerifier(Protocol):
    def verify(
        self,
        final_video: Path,
        narration_wav: Path,
        subtitles: Path | None,
        *,
        settings: VideoSettingsV1,
        final_frame: int,
        landmarks: tuple[VerificationLandmarkV1, ...],
    ) -> VerifiedMediaV1: ...


class ManifestPublisher(Protocol):
    def publish(self, manifest: VideoManifestV1, destination: Path) -> Path: ...


class VideoRenderError(RuntimeError):
    """A render input or output violated its immutable production contract."""


class VideoRenderCancelled(VideoRenderError):
    """Cancellation won before the next render stage could safely launch."""


# TheSuperHackers @feature Leex 24/08/2026 Freeze accepted evidence inputs without exposing executable configuration to Web requests. (#TBD)
@dataclass(frozen=True, slots=True)
class VideoRenderRequest:
    authority: CameraPlanAuthorityV1
    report: PublishedReportGraphDTO
    scene: MapSceneReadModel
    replay_path: Path
    enrichment: ValidatedCommentaryEnrichmentV1 | None = None
    cancellation: CancellationSignal | None = None
    timeout_seconds: int = 14_400

    def __post_init__(self) -> None:
        if type(self.authority) is not CameraPlanAuthorityV1:
            raise TypeError("video render authority must use camera-plan authority v1")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 86_400:
            raise ValueError("video render timeout must be between 1 and 86400 seconds")
        object.__setattr__(self, "replay_path", self.replay_path.resolve())


# TheSuperHackers @feature Leex 24/08/2026 Return content identities alongside private worker paths for safe durable-job adaptation. (#TBD)
class VideoRenderResult(VideoContract):
    run_public_id: PublicId
    run_directory: Path
    stages: tuple[VideoStage, ...] = Field(min_length=6, max_length=6)
    camera_plan_path: Path
    commentary_plan_path: Path
    narration_path: Path
    subtitles_path: Path
    gameplay_video_path: Path
    final_video_path: Path
    final_video_sha256: Sha256
    manifest_public_id: PublicId
    manifest_path: Path
    manifest_sha256: Sha256


class NativeCaptureResultV1(VideoContract):
    """Closed result written by the in-engine D3D capture boundary."""

    schema_version: Literal[1]
    status: Literal["success"]
    failure_code: Literal["ok"]
    failure_detail: int
    requested_width: int = Field(ge=1)
    requested_height: int = Field(ge=1)
    actual_width: int = Field(ge=1)
    actual_height: int = Field(ge=1)
    fps: Literal[30, 60]
    logic_frames: int = Field(ge=1)
    presentation_frames: int = Field(ge=1)
    process_exit_code: Literal[0]


RenderManifestInput = VideoManifestV1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(status: os.stat_result) -> bool:
    attributes = getattr(status, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _require_ordinary_file(path: Path, label: str) -> Path:
    target = path.resolve()
    try:
        status = path.lstat()
    except OSError as error:
        raise VideoRenderError(f"{label} is not an existing ordinary file") from error
    if stat.S_ISLNK(status.st_mode) or _is_reparse(status) or not target.is_file():
        raise VideoRenderError(f"{label} must be an ordinary non-reparse file")
    return target


def _require_ordinary_directory(path: Path, label: str) -> Path:
    target = path.resolve()
    try:
        status = path.lstat()
    except OSError as error:
        raise VideoRenderError(f"{label} is not an existing ordinary directory") from error
    if stat.S_ISLNK(status.st_mode) or _is_reparse(status) or not target.is_dir():
        raise VideoRenderError(f"{label} must be an ordinary non-reparse directory")
    return target


def _create_owned_directory(path: Path) -> Path:
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    _require_ordinary_directory(cursor, "video run ancestor")
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        _require_ordinary_directory(directory, "video run directory")
    return _require_ordinary_directory(path, "video run directory")


def _write_exclusive(path: Path, payload: bytes) -> Path:
    try:
        with path.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError as error:
        raise VideoRenderError(f"exclusive render artifact already exists: {path.name}") from error
    return path.resolve()


def _copy_exclusive(source: Path, destination: Path) -> Path:
    try:
        with source.open("rb") as input_handle, destination.open("xb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle, _HASH_CHUNK_SIZE)
            output_handle.flush()
            os.fsync(output_handle.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination.resolve()


def _camera_script(plan: CameraPlanV1) -> bytes:
    lines = []
    for segment in plan.segments:
        lines.append(
            ",".join(
                (
                    str(segment.start_frame),
                    str(segment.end_frame),
                    str(segment.target_x),
                    str(segment.target_y),
                    str(segment.target_z),
                    str(segment.zoom),
                    str(segment.pitch),
                    str(segment.yaw),
                    segment.transition,
                    segment.segment_id,
                )
            )
        )
    return ("\n".join(lines) + "\n").encode("ascii")


def _ffmpeg_filter_path(path: Path) -> str:
    value = str(path)
    if "\0" in value:
        raise VideoRenderError("subtitle path contains a NUL byte")
    for character in ("\\", ":", "'", ",", ";", "[", "]"):
        value = value.replace(character, f"\\{character}")
    return value


def _load_capture_result(path: Path, settings: VideoSettingsV1, final_frame: int) -> NativeCaptureResultV1:
    sidecar = _require_ordinary_file(path, "native capture result")
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        result = NativeCaptureResultV1.model_validate(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise VideoRenderError("native capture result is not a valid successful closed record") from error
    expected_logic_frames = final_frame + 1
    expected_presentation_frames = expected_logic_frames * (settings.fps // 30)
    if (
        (result.requested_width, result.requested_height) != (settings.width, settings.height)
        or (result.actual_width, result.actual_height) != (settings.width, settings.height)
        or result.fps != settings.fps
        or result.logic_frames != expected_logic_frames
        or result.presentation_frames != expected_presentation_frames
    ):
        raise VideoRenderError("native capture result differs from the fixed render contract")
    return result


def _landmarks(camera: CameraPlanV1) -> tuple[VerificationLandmarkV1, ...]:
    by_frame: dict[int, set[str]] = {}
    for segment in camera.segments:
        by_frame.setdefault(segment.start_frame, set()).update(
            citation.evidence_public_id for citation in segment.evidence
        )
    return tuple(
        VerificationLandmarkV1(frame=frame, evidence_public_ids=tuple(sorted(evidence)))
        for frame, evidence in sorted(by_frame.items())
    )


# TheSuperHackers @feature Leex 24/08/2026 Orchestrate immutable native capture, exact-duration mux, verification, and publication. (#TBD)
class VideoRenderService:
    def __init__(
        self,
        *,
        settings: AnalyzerSettings,
        camera_planner: CameraPlanner | None = None,
        commentary_planner: CommentaryPlanner | None = None,
        voice_provider: VoiceProvider,
        process_runner: ProcessRunner | None = None,
        media_verifier: MediaVerifier,
        manifest_publisher: ManifestPublisher | None = None,
        narration_scheduler: NarrationScheduler | None = None,
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self.settings = settings
        self._camera_planner = camera_planner if camera_planner is not None else CameraPlanService()
        self._commentary_planner = commentary_planner if commentary_planner is not None else CommentaryPlanService()
        self._voice_provider = voice_provider
        self._process_runner = process_runner if process_runner is not None else VideoProcessRunner()
        self._media_verifier = media_verifier
        self._manifest_publisher = (
            manifest_publisher if manifest_publisher is not None else VideoManifestPublisher()
        )
        self._narration_scheduler = narration_scheduler if narration_scheduler is not None else NarrationScheduler()
        self._uuid_factory = uuid_factory

    def render(self, request: VideoRenderRequest) -> VideoRenderResult:
        if type(request) is not VideoRenderRequest:
            raise TypeError("render requires a fixed VideoRenderRequest")
        self._check_cancelled(request)
        settings, engine, ffmpeg, ffprobe = self._fixed_settings()
        replay = _require_ordinary_file(request.replay_path, "replay")
        if _sha256(replay) != request.authority.replay_sha256:
            raise VideoRenderError("replay hash differs from accepted camera authority")
        run_id = str(self._uuid_factory())
        run_directory = self._create_run_directory(run_id)
        frozen_replay = _copy_exclusive(replay, run_directory / "replay.rep")
        if _sha256(frozen_replay) != request.authority.replay_sha256:
            raise VideoRenderError("frozen replay hash differs from accepted camera authority")
        immutable = self._snapshot((frozen_replay, engine, ffmpeg, ffprobe))

        camera = self._camera_planner.create(request.authority, request.report, request.scene)
        if type(camera) is not CameraPlanV1 or camera.authority != request.authority:
            raise VideoRenderError("camera plan authority differs from the fixed render request")
        camera_plan_path = _write_exclusive(run_directory / "camera-plan-v1.json", camera.canonical_json().encode("utf-8"))
        camera_script_path = _write_exclusive(run_directory / "camera-plan-v1.txt", _camera_script(camera))
        self._check_cancelled(request)
        immutable.update(self._snapshot((camera_plan_path, camera_script_path)))

        commentary = self._commentary_planner.create(
            request.report,
            camera,
            enrichment=request.enrichment,
        )
        if (
            type(commentary) is not CommentaryPlanV1
            or commentary.replay_public_id != request.authority.replay_public_id
            or commentary.report_public_id != request.authority.report_public_id
            or commentary.evidence_horizon != request.authority.evidence_horizon
        ):
            raise VideoRenderError("commentary plan authority differs from the fixed render request")
        commentary_plan_path = _write_exclusive(
            run_directory / "commentary-plan-v1.json",
            commentary.canonical_json().encode("utf-8"),
        )
        self._check_cancelled(request)
        immutable.update(self._snapshot((commentary_plan_path,)))

        clips = self._render_voice(request, commentary, run_directory, immutable)
        schedule = self._narration_scheduler.schedule(
            commentary,
            clips,
            request.authority.evidence_horizon.frame_end,
        )
        narration_path = render_narration_wav(schedule, run_directory / "narration.wav")
        subtitles_path = render_webvtt(schedule, run_directory / "subtitles.vtt")
        immutable.update(self._snapshot((narration_path, subtitles_path)))
        self._assert_immutable(immutable)
        self._check_cancelled(request)

        gameplay_path = run_directory / "gameplay.mp4"
        engine_result = self._run_process(
            VideoProcessSpec(
                stage="engine_capture",
                run_id=run_id,
                argv=(
                    str(engine),
                    "-replay",
                    str(frozen_replay),
                    "-autocamera",
                    str(camera_script_path),
                    "-recordVideo",
                    str(gameplay_path),
                    "-videoRes",
                    f"{settings.width}x{settings.height}",
                    "-videoFps",
                    str(settings.fps),
                ),
                cwd=engine.parent,
                stdout_path=run_directory / "engine-capture.stdout.log",
                stderr_path=run_directory / "engine-capture.stderr.log",
                timeout_seconds=request.timeout_seconds,
                cancellation=request.cancellation,
            ),
            immutable,
        )
        del engine_result
        gameplay_path = _require_ordinary_file(gameplay_path, "native gameplay capture")
        capture_result_path = _require_ordinary_file(
            gameplay_path.with_name(f"{gameplay_path.name}.capture-result.json"),
            "native capture result",
        )
        _load_capture_result(capture_result_path, settings, request.authority.evidence_horizon.frame_end)
        immutable.update(self._snapshot((gameplay_path, capture_result_path)))
        self._check_cancelled(request)

        mux_candidate = run_directory / "mux-candidate.mp4"
        mux_result = self._run_process(
            VideoProcessSpec(
                stage="mux",
                run_id=run_id,
                argv=self._mux_argv(
                    ffmpeg,
                    gameplay_path,
                    narration_path,
                    subtitles_path,
                    mux_candidate,
                    settings,
                    request.authority.evidence_horizon.frame_end,
                ),
                cwd=ffmpeg.parent,
                stdout_path=run_directory / "mux.stdout.log",
                stderr_path=run_directory / "mux.stderr.log",
                timeout_seconds=request.timeout_seconds,
                cancellation=request.cancellation,
            ),
            immutable,
        )
        del mux_result
        mux_candidate = _require_ordinary_file(mux_candidate, "mux candidate")
        immutable.update(self._snapshot((mux_candidate,)))
        self._check_cancelled(request)

        self._assert_immutable(immutable)
        verification = self._media_verifier.verify(
            mux_candidate,
            narration_path,
            subtitles_path if settings.subtitle_mode == "track" else None,
            settings=settings,
            final_frame=request.authority.evidence_horizon.frame_end,
            landmarks=_landmarks(camera),
        )
        self._assert_immutable(immutable)
        if not verification.passed:
            raise VideoRenderError("media verification did not pass")
        self._check_cancelled(request)

        final_sha256 = verification.final_video_sha256
        if final_sha256 != _sha256(mux_candidate):
            raise VideoRenderError("verification hash differs from the immutable mux candidate")
        if verification.narration_sha256 != immutable[narration_path.resolve()]:
            raise VideoRenderError("verification narration hash differs from the immutable narration")
        expected_subtitle_hash = immutable[subtitles_path.resolve()] if settings.subtitle_mode == "track" else None
        if verification.subtitle_sha256 != expected_subtitle_hash:
            raise VideoRenderError("verification subtitle hash differs from the immutable subtitle contract")
        final_path = run_directory / f"final-{final_sha256}.mp4"
        manifest_path = run_directory / "video-manifest-v1.json"
        try:
            final_path = _copy_exclusive(mux_candidate, final_path)
            immutable.update(self._snapshot((final_path,)))
            manifest = VideoManifestV1(
                render_public_id=run_id,
                verification_passed=True,
                artifacts=self._manifest_artifacts(
                    immutable,
                    replay=frozen_replay,
                    engine=engine,
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    camera_plan=camera_plan_path,
                    camera_script=camera_script_path,
                    commentary_plan=commentary_plan_path,
                    voice_clips=clips,
                    narration=narration_path,
                    subtitles=subtitles_path,
                    gameplay=gameplay_path,
                    capture_result=capture_result_path,
                    final=final_path,
                ),
            )
            manifest_path = self._manifest_publisher.publish(manifest, manifest_path)
            manifest_sha256 = _sha256(_require_ordinary_file(manifest_path, "published video manifest"))
            self._assert_immutable(immutable)
            self._check_cancelled(request)
        except BaseException:
            final_path.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            raise

        return VideoRenderResult(
            run_public_id=run_id,
            run_directory=run_directory,
            stages=_STAGES,
            camera_plan_path=camera_plan_path,
            commentary_plan_path=commentary_plan_path,
            narration_path=narration_path,
            subtitles_path=subtitles_path,
            gameplay_video_path=gameplay_path,
            final_video_path=final_path,
            final_video_sha256=final_sha256,
            manifest_public_id=run_id,
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
        )

    def _fixed_settings(self) -> tuple[VideoSettingsV1, Path, Path, Path]:
        if self.settings.engine_executable is None:
            raise VideoRenderError("engine executable is not configured")
        if self.settings.ffmpeg_executable is None or self.settings.ffprobe_executable is None:
            raise VideoRenderError("FFmpeg and ffprobe executables must be configured")
        settings = VideoSettingsV1(
            width=self.settings.video_width,
            height=self.settings.video_height,
            fps=self.settings.video_fps,
            subtitle_mode=self.settings.video_subtitle_mode,
        )
        return (
            settings,
            _require_ordinary_file(self.settings.engine_executable, "engine executable"),
            _require_ordinary_file(self.settings.ffmpeg_executable, "FFmpeg executable"),
            _require_ordinary_file(self.settings.ffprobe_executable, "ffprobe executable"),
        )

    def _create_run_directory(self, run_id: str) -> Path:
        parent = _create_owned_directory(self.settings.video_run_directory)
        run_directory = parent / run_id
        try:
            run_directory.mkdir()
        except FileExistsError as error:
            raise VideoRenderError("video render identity already exists and cannot be reused") from error
        return _require_ordinary_directory(run_directory, "video render directory")

    def _render_voice(
        self,
        request: VideoRenderRequest,
        plan: CommentaryPlanV1,
        run_directory: Path,
        immutable: dict[Path, str],
    ) -> tuple[VoiceClipV1, ...]:
        clip_directory = run_directory / "voice-clips"
        clip_directory.mkdir()
        clips: list[VoiceClipV1] = []
        for event in plan.events:
            self._assert_immutable(immutable)
            self._check_cancelled(request)
            destination = clip_directory / f"{event.event_id}.wav"
            if destination.exists():
                raise VideoRenderError("voice clip destination already exists")
            clip = self._voice_provider.render(event, destination)
            self._assert_immutable(immutable)
            source = _require_ordinary_file(clip.source_path, "voice clip")
            if source != destination.resolve():
                raise VideoRenderError("voice provider returned a clip outside its exclusive destination")
            immutable.update(self._snapshot((source,)))
            clips.append(clip)
        return tuple(clips)

    def _run_process(
        self,
        spec: VideoProcessSpec,
        immutable: dict[Path, str],
    ) -> VideoProcessResult:
        self._assert_immutable(immutable)
        try:
            result = self._process_runner.run(spec)
        except VideoProcessCancelled as error:
            raise VideoRenderCancelled("video render cancelled while settling an active child tree") from error
        self._assert_immutable(immutable)
        return result

    @staticmethod
    def _mux_argv(
        ffmpeg: Path,
        gameplay: Path,
        narration: Path,
        subtitles: Path,
        destination: Path,
        settings: VideoSettingsV1,
        final_frame: int,
    ) -> tuple[str, ...]:
        duration = f"{(final_frame + 1) / 30.0:.9f}"
        common = (
            str(ffmpeg),
            "-nostdin",
            "-n",
            "-i",
            str(gameplay),
            "-i",
            str(narration),
        )
        if settings.subtitle_mode == "track":
            return common + (
                "-i",
                str(subtitles),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-map",
                "2:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-ac",
                "1",
                "-c:s",
                "mov_text",
                "-t",
                duration,
                "-r",
                str(settings.fps),
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(destination),
            )
        return common + (
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            f"subtitles=filename='{_ffmpeg_filter_path(subtitles)}'",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-ac",
            "1",
            "-t",
            duration,
            "-r",
            str(settings.fps),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(destination),
        )

    @staticmethod
    def _snapshot(paths: tuple[Path, ...]) -> dict[Path, str]:
        return {path.resolve(): _sha256(_require_ordinary_file(path, "immutable render input")) for path in paths}

    @staticmethod
    def _assert_immutable(snapshot: dict[Path, str]) -> None:
        for path, expected in snapshot.items():
            try:
                actual = _sha256(_require_ordinary_file(path, "immutable render input"))
            except (OSError, VideoRenderError) as error:
                raise VideoRenderError(f"immutable input changed during rendering: {path.name}") from error
            if actual != expected:
                raise VideoRenderError(f"immutable input changed during rendering: {path.name}")

    @staticmethod
    def _check_cancelled(request: VideoRenderRequest) -> None:
        if request.cancellation is not None and request.cancellation.is_set():
            raise VideoRenderCancelled("video render cancelled before the next stage")

    @staticmethod
    def _manifest_artifacts(
        immutable: dict[Path, str],
        *,
        replay: Path,
        engine: Path,
        ffmpeg: Path,
        ffprobe: Path,
        camera_plan: Path,
        camera_script: Path,
        commentary_plan: Path,
        voice_clips: tuple[VoiceClipV1, ...],
        narration: Path,
        subtitles: Path,
        gameplay: Path,
        capture_result: Path,
        final: Path,
    ) -> tuple[ArtifactHashV1, ...]:
        paths = {
            "replay": replay,
            "engine_executable": engine,
            "ffmpeg_executable": ffmpeg,
            "ffprobe_executable": ffprobe,
            "camera_plan": camera_plan,
            "camera_script": camera_script,
            "commentary_plan": commentary_plan,
            "narration": narration,
            "subtitles": subtitles,
            "gameplay_video": gameplay,
            "native_capture_result": capture_result,
            "final_video": final,
        }
        paths.update(
            {f"voice_clip_{index:04d}": clip.source_path for index, clip in enumerate(voice_clips)}
        )
        return tuple(
            ArtifactHashV1(name=name, path=path, sha256=immutable[path.resolve()])
            for name, path in sorted(paths.items())
        )
