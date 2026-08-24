"""End-to-end orchestration tests for native commented replay rendering."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import wave
from collections.abc import Callable
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.report.read_model import PublishedReportGraphDTO
from generals_replay_analyzer.spatial.query import MapSceneReadModel
from generals_replay_analyzer.video.contracts import (
    CameraPlanAuthorityV1,
    CameraPlanV1,
    CameraSegmentV1,
    CommentaryEventV1,
    CommentaryPlanV1,
    EvidenceCitationV1,
    EvidenceHorizonV1,
    VideoSettingsV1,
)
from generals_replay_analyzer.video.process import VideoProcessError, VideoProcessResult, VideoProcessSpec
from generals_replay_analyzer.video.render import (
    MediaVerifier,
    RenderManifestInput,
    VideoRenderCancelled,
    VideoRenderError,
    VideoRenderRequest,
    VideoRenderService,
)
from generals_replay_analyzer.video.verify import ObservedVideoV1, VerificationLandmarkV1, VerifiedMediaV1
from generals_replay_analyzer.video.voice import VoiceClipV1

RUN_ID = UUID("10000000-0000-4000-8000-000000000001")
REPLAY_ID = "20000000-0000-4000-8000-000000000002"
REPORT_ID = "30000000-0000-4000-8000-000000000003"
TELEMETRY_ID = "40000000-0000-4000-8000-000000000004"
MAP_ID = "50000000-0000-4000-8000-000000000005"
EVIDENCE_ID = "60000000-0000-4000-8000-000000000006"
SEGMENT_ID = "70000000-0000-4000-8000-000000000007"
EVENT_ID = "80000000-0000-4000-8000-000000000008"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_media_verifier_protocol_requires_logic_timebase() -> None:
    assert "logic_frames_per_second" in inspect.signature(MediaVerifier.verify).parameters


def _authority(replay_sha256: str) -> CameraPlanAuthorityV1:
    return CameraPlanAuthorityV1(
        replay_public_id=REPLAY_ID,
        replay_sha256=replay_sha256,
        report_public_id=REPORT_ID,
        telemetry_run_public_id=TELEMETRY_ID,
        telemetry_trace_sha256="1" * 64,
        map_public_id=MAP_ID,
        map_content_sha256="2" * 64,
        evidence_horizon=EvidenceHorizonV1(frame_end=59),
        logic_frames_per_second=30,
    )


def _camera(authority: CameraPlanAuthorityV1) -> CameraPlanV1:
    citation = EvidenceCitationV1(
        evidence_public_id=EVIDENCE_ID,
        tier="observed",
        frame_start=0,
        frame_end=59,
    )
    return CameraPlanV1(
        authority=authority,
        segments=(
            CameraSegmentV1(
                segment_id=SEGMENT_ID,
                start_frame=0,
                end_frame=59,
                target_x=123.5,
                target_y=456.25,
                target_z=10.0,
                zoom=1.5,
                pitch=-45.0,
                yaw=90.0,
                transition="cut",
                transition_frames=0,
                focus_kind="engagement",
                label="Opening contact",
                evidence=(citation,),
            ),
        ),
    )


def _commentary(authority: CameraPlanAuthorityV1) -> CommentaryPlanV1:
    return CommentaryPlanV1(
        logic_hz=authority.logic_frames_per_second,
        replay_public_id=REPLAY_ID,
        report_public_id=REPORT_ID,
        evidence_horizon=authority.evidence_horizon,
        events=(
            CommentaryEventV1(
                event_id=EVENT_ID,
                start_frame=0,
                latest_end_frame=59,
                text="The opening engagement starts now.",
                subtitle_text="The opening engagement starts now.",
                role="play_by_play",
                evidence=(
                    EvidenceCitationV1(
                        evidence_public_id=EVIDENCE_ID,
                        tier="observed",
                        frame_start=0,
                        frame_end=59,
                    ),
                ),
                confidence_tier="observed",
                camera_segment_id=SEGMENT_ID,
            ),
        ),
    )


class _CameraPlanner:
    def __init__(self, plan: CameraPlanV1, stages: list[str], *, cancellation: _Cancellation | None = None) -> None:
        self.plan = plan
        self.stages = stages
        self.cancellation = cancellation

    def create(
        self,
        authority: CameraPlanAuthorityV1,
        report: PublishedReportGraphDTO,
        scene: MapSceneReadModel,
    ) -> CameraPlanV1:
        del authority, report, scene
        self.stages.append("camera_plan")
        if self.cancellation is not None:
            self.cancellation.cancelled = True
        return self.plan


class _CommentaryPlanner:
    def __init__(self, plan: CommentaryPlanV1, stages: list[str]) -> None:
        self.plan = plan
        self.stages = stages

    def create(
        self,
        report: PublishedReportGraphDTO,
        camera: CameraPlanV1,
        *,
        enrichment: object | None = None,
    ) -> CommentaryPlanV1:
        del report, camera, enrichment
        self.stages.append("commentary_plan")
        return self.plan


class _VoiceProvider:
    def __init__(self, stages: list[str], *, mutate: Path | None = None) -> None:
        self.stages = stages
        self.mutate = mutate

    def render(self, event: CommentaryEventV1, destination: Path) -> VoiceClipV1:
        self.stages.append("voice")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as destination_handle, wave.open(destination_handle, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(30_000)
            output.writeframes(b"\x10\0" * 5_000)
        if self.mutate is not None:
            self.mutate.write_bytes(b"changed")
        return VoiceClipV1.from_wav(event, destination, provider_name="test", voice_name="caster")


class _ProcessRunner:
    def __init__(
        self,
        stages: list[str],
        *,
        fail_stage: str | None = None,
        mutate_after_stage: tuple[str, Path] | None = None,
        cancellation: _Cancellation | None = None,
        capture_result: dict[str, object] | None = None,
        inspect_engine_capture: Callable[[VideoProcessSpec], None] | None = None,
        engine_exit_code: int = 0,
    ) -> None:
        self.stages = stages
        self.specs: list[VideoProcessSpec] = []
        self.fail_stage = fail_stage
        self.mutate_after_stage = mutate_after_stage
        self.cancellation = cancellation
        self.capture_result = capture_result
        self.inspect_engine_capture = inspect_engine_capture
        self.engine_exit_code = engine_exit_code
        self.mutation_denied = False

    def run(self, spec: VideoProcessSpec) -> VideoProcessResult:
        self.specs.append(spec)
        self.stages.append(spec.stage)
        if spec.stage == "engine_capture" and self.inspect_engine_capture is not None:
            self.inspect_engine_capture(spec)
        if spec.stage == self.fail_stage:
            raise VideoProcessError(f"{spec.stage} launch failed")
        output = (
            Path(spec.argv[spec.argv.index("-recordVideo") + 1])
            if spec.stage == "engine_capture"
            else Path(spec.argv[-1])
        )
        output.write_bytes(b"gameplay" if spec.stage == "engine_capture" else b"muxed-final")
        if spec.stage == "engine_capture":
            payload: dict[str, object] = {
                "schema_version": 1,
                "status": "success",
                "failure_code": "ok",
                "failure_detail": 0,
                "requested_width": 640,
                "requested_height": 360,
                "actual_width": 640,
                "actual_height": 360,
                "fps": 30,
                "logic_frames": 60,
                "presentation_frames": 60,
                "process_exit_code": 0,
            }
            if self.capture_result is not None:
                payload.update(self.capture_result)
            (output.parent / f"{output.name}.capture-result.json").write_text(json.dumps(payload), encoding="utf-8")
        spec.stdout_path.write_bytes(b"")
        spec.stderr_path.write_bytes(b"")
        if self.mutate_after_stage is not None and self.mutate_after_stage[0] == spec.stage:
            try:
                self.mutate_after_stage[1].write_bytes(b"changed executable")
            except PermissionError:
                self.mutation_denied = True
        if self.cancellation is not None and spec.stage == "engine_capture":
            self.cancellation.cancelled = True
        result = VideoProcessResult(
            stage=spec.stage,
            exit_code=self.engine_exit_code if spec.stage == "engine_capture" else 0,
            timed_out=False,
            duration_seconds=0.1,
            process_tree_terminated=False,
            termination_method=None,
            stdout_path=spec.stdout_path,
            stderr_path=spec.stderr_path,
        )
        if result.exit_code != 0:
            raise VideoProcessError(f"{spec.stage} failed with exit code {result.exit_code}", result)
        return result


class _Verifier:
    def __init__(self, stages: list[str], *, mutate: Path | None = None, wrong_auxiliary_hash: bool = False) -> None:
        self.stages = stages
        self.mutate = mutate
        self.calls: list[tuple[Path, Path, Path | None, int]] = []
        self.wrong_auxiliary_hash = wrong_auxiliary_hash

    def verify(
        self,
        final_video: Path,
        narration_wav: Path,
        subtitles: Path | None,
        *,
        settings: VideoSettingsV1,
        final_frame: int,
        logic_frames_per_second: int,
        landmarks: tuple[VerificationLandmarkV1, ...],
    ) -> VerifiedMediaV1:
        self.stages.append("verify")
        self.calls.append((final_video, narration_wav, subtitles, final_frame))
        if self.mutate is not None:
            self.mutate.write_bytes(b"changed executable")
        return VerifiedMediaV1(
            final_video_sha256=_sha256(final_video),
            narration_sha256="f" * 64 if self.wrong_auxiliary_hash else _sha256(narration_wav),
            subtitle_sha256=(
                "e" * 64
                if self.wrong_auxiliary_hash and subtitles is not None
                else _sha256(subtitles)
                if subtitles is not None
                else None
            ),
            expected_duration_seconds=(final_frame + 1) / logic_frames_per_second,
            observed=ObservedVideoV1(
                codec_name="h264",
                pixel_format="yuv420p",
                width=settings.width,
                height=settings.height,
                fps_numerator=settings.fps,
                fps_denominator=1,
                frame_count=(final_frame + 1) * settings.fps // logic_frames_per_second,
                duration_seconds=(final_frame + 1) / logic_frames_per_second,
                audio_codec_name="aac",
                audio_sample_rate=30_000,
                audio_channels=1,
                subtitle_codec_name="mov_text" if subtitles is not None else None,
            ),
            landmarks=landmarks,
        )


class _ManifestPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.manifests: list[RenderManifestInput] = []

    def publish(self, manifest: RenderManifestInput, destination: Path) -> Path:
        self.manifests.append(manifest)
        if self.fail:
            raise OSError("manifest publication failed")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8", newline="\n") as output:
            output.write("{}")
        return destination.resolve()


class _Cancellation:
    cancelled = False

    def is_set(self) -> bool:
        return self.cancelled


def _settings(tmp_path: Path) -> tuple[AnalyzerSettings, Path, Path, Path]:
    tools = tmp_path / "tools with spaces & metacharacters"
    tools.mkdir()
    engine_runtime = tmp_path / "installed Zero Hour"
    engine_runtime.mkdir()
    engine = tools / "generalszh & safe.exe"
    ffmpeg = tools / "ffmpeg;safe.exe"
    ffprobe = tools / "ffprobe $(safe).exe"
    for path, content in ((engine, b"engine"), (ffmpeg, b"ffmpeg"), (ffprobe, b"ffprobe")):
        path.write_bytes(content)
    settings = AnalyzerSettings._for_testing_with_repository_outputs(
        data_root=tmp_path / "product data & safe",
        engine_executable=engine,
        engine_runtime_directory=engine_runtime,
        ffmpeg_executable=ffmpeg,
        ffprobe_executable=ffprobe,
        video_width=640,
        video_height=360,
        video_fps=30,
        video_subtitle_mode="track",
    )
    return settings, engine.resolve(), ffmpeg.resolve(), ffprobe.resolve()


def _request(
    tmp_path: Path,
    *,
    cancellation: _Cancellation | None = None,
    diagnostic_preview: bool = False,
) -> tuple[VideoRenderRequest, CameraPlanV1, CommentaryPlanV1]:
    replay = (tmp_path / "Replay & whoami; $(touch nope).rep").resolve()
    replay.write_bytes(b"retail replay")
    authority = _authority(_sha256(replay))
    return (
        VideoRenderRequest(
            authority=authority,
            report=cast(PublishedReportGraphDTO, object()),
            scene=cast(MapSceneReadModel, object()),
            replay_path=replay,
            diagnostic_preview=diagnostic_preview,
            cancellation=cancellation,
        ),
        _camera(authority),
        _commentary(authority),
    )


def _service(
    tmp_path: Path,
    request: VideoRenderRequest,
    camera: CameraPlanV1,
    commentary: CommentaryPlanV1,
    *,
    cancellation_camera: _Cancellation | None = None,
    voice_mutate: Path | None = None,
    process: _ProcessRunner | None = None,
    verifier: _Verifier | None = None,
    publisher: _ManifestPublisher | None = None,
) -> tuple[VideoRenderService, list[str], _ProcessRunner, _Verifier, _ManifestPublisher]:
    settings, _, _, _ = _settings(tmp_path)
    stages: list[str] = []
    process = process or _ProcessRunner(stages)
    verifier = verifier or _Verifier(stages)
    publisher = publisher or _ManifestPublisher()
    service = VideoRenderService(
        settings=settings,
        camera_planner=_CameraPlanner(camera, stages, cancellation=cancellation_camera),
        commentary_planner=_CommentaryPlanner(commentary, stages),
        voice_provider=_VoiceProvider(stages, mutate=voice_mutate),
        process_runner=process,
        media_verifier=verifier,
        manifest_publisher=publisher,
        uuid_factory=lambda: RUN_ID,
    )
    return service, stages, process, verifier, publisher


def test_render_runs_closed_stage_order_with_safe_argv_exact_duration_and_verified_publication(tmp_path: Path) -> None:
    request, camera, commentary = _request(tmp_path)
    service, stages, process, verifier, publisher = _service(tmp_path, request, camera, commentary)

    result = service.render(request)

    assert stages == ["camera_plan", "commentary_plan", "voice", "engine_capture", "mux", "verify"]
    assert result.stages == ("camera_plan", "commentary_plan", "voice", "engine_capture", "mux", "verify")
    assert result.run_public_id == str(RUN_ID)
    assert result.run_directory == result.final_video_path.parent
    assert result.final_video_path.name == f"final-{result.final_video_sha256}.mp4"
    assert result.final_video_path.read_bytes() == b"muxed-final"
    assert result.manifest_path.is_file()
    assert result.manifest_public_id == result.run_public_id
    assert result.manifest_sha256 == _sha256(result.manifest_path)
    assert verifier.calls[0][3] == 59
    engine_spec, mux_spec = process.specs
    assert engine_spec.cwd == (tmp_path / "installed Zero Hour").resolve()
    frozen_replay = Path(engine_spec.argv[engine_spec.argv.index("-replay") + 1])
    assert frozen_replay.parent == result.run_directory
    assert frozen_replay.name == "replay.rep"
    assert frozen_replay.read_bytes() == request.replay_path.read_bytes()
    assert engine_spec.argv[engine_spec.argv.index("-videoRes") + 1] == "640x360"
    assert engine_spec.argv[engine_spec.argv.index("-xres") + 1] == "640"
    assert engine_spec.argv[engine_spec.argv.index("-yres") + 1] == "360"
    assert engine_spec.argv[engine_spec.argv.index("-videoFps") + 1] == "30"
    assert mux_spec.argv[mux_spec.argv.index("-t") + 1] == "2.000000000"
    assert all(spec.argv[0] == str(spec.argv[0]) and spec.argv for spec in process.specs)
    manifest = publisher.manifests[0]
    assert manifest.verification_passed is True
    assert {artifact.name for artifact in manifest.artifacts} >= {
        "replay",
        "engine_executable",
        "ffmpeg_executable",
        "ffprobe_executable",
        "camera_plan",
        "camera_script",
        "commentary_plan",
        "voice_clip_0000",
        "narration",
        "subtitles",
        "gameplay_video",
        "native_capture_result",
        "final_video",
    }
    assert all(
        artifact.path is not None and _sha256(artifact.path) == artifact.sha256 for artifact in manifest.artifacts
    )


def test_explicit_diagnostic_preview_accepts_replay_validator_exit_one_only_with_success_sidecar(
    tmp_path: Path,
) -> None:
    request, camera, commentary = _request(tmp_path, diagnostic_preview=True)
    stages: list[str] = []
    process = _ProcessRunner(stages, engine_exit_code=1)
    service, _, _, _, publisher = _service(tmp_path, request, camera, commentary, process=process)

    result = service.render(request)

    assert result.final_video_path.is_file()
    assert [spec.stage for spec in process.specs] == ["engine_capture", "mux"]
    assert publisher.manifests


@pytest.mark.parametrize(("diagnostic_preview", "exit_code"), ((False, 1), (True, 2)))
def test_capture_validator_failure_is_not_accepted_outside_the_explicit_partial_contract(
    tmp_path: Path,
    diagnostic_preview: bool,
    exit_code: int,
) -> None:
    request, camera, commentary = _request(tmp_path, diagnostic_preview=diagnostic_preview)
    stages: list[str] = []
    process = _ProcessRunner(stages, engine_exit_code=exit_code)
    service, _, _, _, publisher = _service(tmp_path, request, camera, commentary, process=process)

    with pytest.raises(VideoProcessError, match=f"exit code {exit_code}"):
        service.render(request)

    assert [spec.stage for spec in process.specs] == ["engine_capture"]
    assert publisher.manifests == []


def test_render_stages_engine_capture_beside_runtime_and_removes_it_after_the_process_settles(tmp_path: Path) -> None:
    request, camera, commentary = _request(tmp_path)
    staged: list[Path] = []
    configured = (tmp_path / "tools with spaces & metacharacters" / "generalszh & safe.exe").resolve()
    runtime = (tmp_path / "installed Zero Hour").resolve()

    def inspect(spec: VideoProcessSpec) -> None:
        launch = Path(spec.argv[0])
        staged.append(launch)
        assert launch.parent == runtime
        assert launch.exists()
        assert os.path.samefile(launch, configured)

    stages: list[str] = []
    process = _ProcessRunner(stages, inspect_engine_capture=inspect)
    service, _, _, _, publisher = _service(tmp_path, request, camera, commentary, process=process)
    service.render(request)

    assert len(staged) == 1
    assert not staged[0].exists()
    assert any(artifact.name == "engine_executable" for artifact in publisher.manifests[0].artifacts)


def test_render_removes_staged_engine_after_engine_capture_launch_failure(tmp_path: Path) -> None:
    request, camera, commentary = _request(tmp_path)
    staged: list[Path] = []

    def inspect(spec: VideoProcessSpec) -> None:
        staged.append(Path(spec.argv[0]))
        assert staged[-1].exists()

    stages: list[str] = []
    process = _ProcessRunner(stages, fail_stage="engine_capture", inspect_engine_capture=inspect)
    service, _, _, _, _ = _service(tmp_path, request, camera, commentary, process=process)
    with pytest.raises(VideoProcessError, match="engine_capture"):
        service.render(request)
    assert len(staged) == 1 and not staged[0].exists()


def test_render_detects_replay_changes_and_denies_executable_mutation_during_capture(tmp_path: Path) -> None:
    request, camera, commentary = _request(tmp_path)
    service, _, _, _, publisher = _service(
        tmp_path,
        request,
        camera,
        commentary,
        voice_mutate=request.replay_path,
    )
    result = service.render(request)
    assert result.final_video_path.is_file()
    assert result.gameplay_video_path.parent.joinpath("replay.rep").read_bytes() == b"retail replay"

    second_root = tmp_path / "second"
    second_root.mkdir()
    request, camera, commentary = _request(second_root)
    settings, engine, _, _ = _settings(second_root)
    stages: list[str] = []
    process = _ProcessRunner(stages, mutate_after_stage=("engine_capture", engine))
    publisher = _ManifestPublisher()
    service = VideoRenderService(
        settings=settings,
        camera_planner=_CameraPlanner(camera, stages),
        commentary_planner=_CommentaryPlanner(commentary, stages),
        voice_provider=_VoiceProvider(stages),
        process_runner=process,
        media_verifier=_Verifier(stages),
        manifest_publisher=publisher,
        uuid_factory=lambda: RUN_ID,
    )
    result = service.render(request)
    assert result.final_video_path.is_file()
    assert process.mutation_denied is True
    assert engine.read_bytes() == b"engine"
    assert len(publisher.manifests) == 1


def test_render_rejects_an_injected_camera_plan_for_a_different_authority(tmp_path: Path) -> None:
    request, _, commentary = _request(tmp_path)
    mismatched_values = request.authority.model_dump()
    mismatched_values["telemetry_trace_sha256"] = "f" * 64
    mismatched_camera = _camera(CameraPlanAuthorityV1.model_validate(mismatched_values))
    service, _, process, _, publisher = _service(tmp_path, request, mismatched_camera, commentary)

    with pytest.raises(VideoRenderError, match="camera plan authority"):
        service.render(request)

    assert process.specs == []
    assert publisher.manifests == []


def test_render_rejects_verifier_auxiliary_hashes_that_do_not_match_frozen_inputs(tmp_path: Path) -> None:
    request, camera, commentary = _request(tmp_path)
    stages: list[str] = []
    verifier = _Verifier(stages, wrong_auxiliary_hash=True)
    service, _, _, _, publisher = _service(tmp_path, request, camera, commentary, verifier=verifier)

    with pytest.raises(VideoRenderError, match="verification narration hash"):
        service.render(request)

    assert publisher.manifests == []
    assert not list(service.settings.video_run_directory.glob("*/final-*.mp4"))


@pytest.mark.parametrize(
    "capture_result",
    [
        {"status": "failed", "failure_code": "pipe_write_failed"},
        {"logic_frames": 59},
        {"presentation_frames": 61},
        {"requested_width": 320},
        {"process_exit_code": 1},
    ],
)
def test_render_rejects_unsuccessful_or_mismatched_native_capture_result(
    tmp_path: Path, capture_result: dict[str, object]
) -> None:
    request, camera, commentary = _request(tmp_path)
    stages: list[str] = []
    process = _ProcessRunner(stages, capture_result=capture_result)
    service, _, _, _, publisher = _service(tmp_path, request, camera, commentary, process=process)

    with pytest.raises((VideoRenderError, ValueError), match="native capture result"):
        service.render(request)

    assert publisher.manifests == []


def test_render_accepts_scaled_native_backbuffer_when_requested_output_and_verification_match(
    tmp_path: Path,
) -> None:
    request, camera, commentary = _request(tmp_path)
    stages: list[str] = []
    process = _ProcessRunner(stages, capture_result={"actual_width": 1686, "actual_height": 916})
    service, _, _, _, publisher = _service(tmp_path, request, camera, commentary, process=process)

    result = service.render(request)

    assert result.final_video_path.is_file()
    assert publisher.manifests[0].verified_media.observed.width == 640
    assert publisher.manifests[0].verified_media.observed.height == 360


@pytest.mark.parametrize("failure_stage", ["engine_capture", "mux"])
def test_launch_failures_leave_no_published_final_or_manifest(tmp_path: Path, failure_stage: str) -> None:
    request, camera, commentary = _request(tmp_path)
    stages: list[str] = []
    process = _ProcessRunner(stages, fail_stage=failure_stage)
    publisher = _ManifestPublisher()
    service, _, _, _, _ = _service(
        tmp_path,
        request,
        camera,
        commentary,
        process=process,
        publisher=publisher,
    )

    with pytest.raises(VideoProcessError, match="launch failed"):
        service.render(request)

    assert publisher.manifests == []
    assert not list(service.settings.video_run_directory.glob("*/final-*.mp4"))
    assert not list(service.settings.video_run_directory.glob("*/video-manifest-v1.json"))


def test_cancellation_between_stages_never_launches_or_publishes_later_work(tmp_path: Path) -> None:
    cancellation = _Cancellation()
    request, camera, commentary = _request(tmp_path, cancellation=cancellation)
    service, stages, process, _, publisher = _service(
        tmp_path,
        request,
        camera,
        commentary,
        cancellation_camera=cancellation,
    )

    with pytest.raises(VideoRenderCancelled):
        service.render(request)

    assert stages == ["camera_plan"]
    assert process.specs == []
    assert publisher.manifests == []


def test_cancellation_after_engine_capture_leaves_only_unpublished_stage_artifacts(tmp_path: Path) -> None:
    cancellation = _Cancellation()
    request, camera, commentary = _request(tmp_path, cancellation=cancellation)
    settings, _, _, _ = _settings(tmp_path)
    stages: list[str] = []
    staged: list[Path] = []

    def inspect(spec: VideoProcessSpec) -> None:
        staged.append(Path(spec.argv[0]))

    process = _ProcessRunner(stages, cancellation=cancellation, inspect_engine_capture=inspect)
    publisher = _ManifestPublisher()
    service = VideoRenderService(
        settings=settings,
        camera_planner=_CameraPlanner(camera, stages),
        commentary_planner=_CommentaryPlanner(commentary, stages),
        voice_provider=_VoiceProvider(stages),
        process_runner=process,
        media_verifier=_Verifier(stages),
        manifest_publisher=publisher,
        uuid_factory=lambda: RUN_ID,
    )

    with pytest.raises(VideoRenderCancelled):
        service.render(request)

    assert stages[-1] == "engine_capture"
    assert len(staged) == 1 and not staged[0].exists()
    assert publisher.manifests == []
    assert not list(settings.video_run_directory.glob("*/final-*.mp4"))


def test_manifest_failure_rolls_back_content_addressed_final_publication(tmp_path: Path) -> None:
    request, camera, commentary = _request(tmp_path)
    publisher = _ManifestPublisher(fail=True)
    service, _, _, _, _ = _service(tmp_path, request, camera, commentary, publisher=publisher)

    with pytest.raises(OSError, match="publication failed"):
        service.render(request)

    assert not list(service.settings.video_run_directory.glob("*/final-*.mp4"))
    assert not list(service.settings.video_run_directory.glob("*/video-manifest-v1.json"))


def test_render_directory_is_exclusive_and_never_reuses_a_render_identity(tmp_path: Path) -> None:
    request, camera, commentary = _request(tmp_path)
    service, _, _, _, _ = _service(tmp_path, request, camera, commentary)
    collision = service.settings.video_run_directory / str(RUN_ID)
    collision.mkdir(parents=True)
    sentinel = collision / "caller-owned.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(VideoRenderError, match="already exists"):
        service.render(request)

    assert sentinel.read_text(encoding="utf-8") == "keep"
