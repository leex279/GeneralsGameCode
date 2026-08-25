from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from generals_replay_analyzer.importing.stages import RENDER_VIDEO_VERSION
from generals_replay_analyzer.video.jobs import VideoJobPlanner, VideoJobRequestError, VideoRenderStageHandler


def _id() -> str:
    return str(uuid4())


def test_complete_evidence_creates_worker_owned_render_job() -> None:
    planner = VideoJobPlanner(clock=lambda: datetime(2026, 8, 24, tzinfo=UTC))

    job = planner.plan(
        replay_public_id=_id(),
        replay_sha256="a" * 64,
        logic_frames_per_second=30,
        report_public_id=_id(),
        report_job_public_id=_id(),
        evidence_horizon="complete",
        diagnostic_preview=False,
    )

    assert job.stage == "render_video"
    assert job.component_version == RENDER_VIDEO_VERSION
    assert job.input_json["evidence_horizon"] == "complete"
    assert job.input_json["diagnostic_preview"] is False
    assert job.input_json["replay_sha256"] == "a" * 64
    assert job.input_json["logic_frames_per_second"] == 30
    assert "path" not in job.input_json


def test_partial_evidence_requires_explicit_diagnostic_preview() -> None:
    planner = VideoJobPlanner(clock=lambda: datetime(2026, 8, 24, tzinfo=UTC))

    with pytest.raises(VideoJobRequestError, match="diagnostic preview"):
        planner.plan(
            replay_public_id=_id(),
            replay_sha256="a" * 64,
            logic_frames_per_second=30,
            report_public_id=_id(),
            report_job_public_id=_id(),
            evidence_horizon="partial",
            diagnostic_preview=False,
        )


def test_partial_preview_is_explicitly_marked_in_durable_input() -> None:
    planner = VideoJobPlanner(clock=lambda: datetime(2026, 8, 24, tzinfo=UTC))

    job = planner.plan(
        replay_public_id=_id(),
        replay_sha256="a" * 64,
        logic_frames_per_second=30,
        report_public_id=_id(),
        report_job_public_id=_id(),
        evidence_horizon="partial",
        diagnostic_preview=True,
    )

    assert job.input_json["diagnostic_preview"] is True
    assert job.input_json["evidence_horizon"] == "partial"


def test_enqueue_adds_report_dependency_without_running_a_renderer() -> None:
    class Coordinator:
        def create_job(self, spec: object) -> object:
            self.spec = spec
            return type("Snapshot", (), {"public_id": "created"})()

        def add_dependency(self, job_public_id: str, depends_on_public_id: str) -> None:
            self.edge = (job_public_id, depends_on_public_id)

    coordinator = Coordinator()
    planner = VideoJobPlanner(clock=lambda: datetime(2026, 8, 24, tzinfo=UTC))
    report_job_id = _id()

    public_id = planner.enqueue(
        coordinator, replay_public_id=_id(), replay_sha256="a" * 64, logic_frames_per_second=30,
        report_public_id=_id(), report_job_public_id=report_job_id,
        evidence_horizon="complete", diagnostic_preview=False,
    )

    assert public_id == "created"
    assert coordinator.edge == ("created", report_job_id)


@pytest.mark.parametrize("value", ("A" * 64, "a" * 63, "g" * 64))
def test_video_job_rejects_a_noncanonical_replay_content_hash(value: str) -> None:
    planner = VideoJobPlanner(clock=lambda: datetime(2026, 8, 24, tzinfo=UTC))

    with pytest.raises(VideoJobRequestError, match="replay SHA-256"):
        planner.plan(
            replay_public_id=_id(),
            replay_sha256=value,
            logic_frames_per_second=30,
            report_public_id=_id(),
            report_job_public_id=_id(),
            evidence_horizon="complete",
            diagnostic_preview=False,
        )


@pytest.mark.parametrize("value", (0, 29, 59, True))
def test_video_job_rejects_an_invalid_logic_timebase(value: object) -> None:
    planner = VideoJobPlanner(clock=lambda: datetime(2026, 8, 24, tzinfo=UTC))

    with pytest.raises(VideoJobRequestError, match="logic timebase"):
        planner.plan(
            replay_public_id=_id(),
            replay_sha256="a" * 64,
            logic_frames_per_second=value,  # type: ignore[arg-type]
            report_public_id=_id(),
            report_job_public_id=_id(),
            evidence_horizon="complete",
            diagnostic_preview=False,
        )


def test_stage_handler_returns_only_public_verified_media_identity() -> None:
    class Renderer:
        def render(self, _request: object) -> object:
            return type("Result", (), {
                "run_public_id": _id(), "final_video_sha256": "a" * 64,
                "manifest_public_id": _id(), "manifest_sha256": "b" * 64,
            })()

    handler = VideoRenderStageHandler(request_factory=lambda _input: object(), renderer=Renderer())
    output = handler(type("Context", (), {"input": {"replay_public_id": _id()}})())

    assert output["schema_version"] == "video-stage-output-v1"
    assert set(output) == {"schema_version", "run_public_id", "final_video_sha256", "manifest_public_id", "manifest_sha256"}


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("run_public_id", "123E4567-E89B-42D3-A456-426614174000"),
        ("manifest_public_id", "not-a-public-id"),
        ("final_video_sha256", "A" * 64),
        ("final_video_sha256", "../" + "a" * 61),
        ("manifest_sha256", "g" * 64),
    ),
)
def test_stage_handler_rejects_noncanonical_persisted_media_identity(field: str, value: str) -> None:
    values = {
        "run_public_id": _id(),
        "final_video_sha256": "a" * 64,
        "manifest_public_id": _id(),
        "manifest_sha256": "b" * 64,
    }
    values[field] = value

    class Renderer:
        def render(self, _request: object) -> object:
            return type("Result", (), values)()

    handler = VideoRenderStageHandler(request_factory=lambda _input: object(), renderer=Renderer())

    with pytest.raises(RuntimeError, match="verified public media identities"):
        handler(type("Context", (), {"input": {"replay_public_id": _id()}})())
