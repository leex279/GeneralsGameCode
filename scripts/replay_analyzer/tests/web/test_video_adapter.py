"""Verified replay-video media boundary tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.models import Job, JobStageResult
from generals_replay_analyzer.web.adapters.video import AnalyticsVideoAdapter
from generals_replay_analyzer.web.errors import PublicProblem

JOB_ID = "123e4567-e89b-42d3-a456-426614174020"
RUN_ID = "123e4567-e89b-42d3-a456-426614174021"
RESULT_ID = "123e4567-e89b-42d3-a456-426614174022"
MANIFEST_ID = "123e4567-e89b-42d3-a456-426614174023"
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def video_database(tmp_path: Path) -> Iterator[tuple[AnalyzerSettings, sessionmaker[Session]]]:
    settings = AnalyzerSettings.model_validate({"data_root": tmp_path / "product-data"})
    settings.ensure_directories()
    upgrade_database(settings.database_path)
    engine = create_database_engine(settings.database_path)
    factory = create_session_factory(engine)
    try:
        yield settings, factory
    finally:
        engine.dispose()


def _seed_result(factory: sessionmaker[Session], output: dict[str, object]) -> None:
    with factory.begin() as session:
        job = Job(
            public_id=JOB_ID,
            replay_id=None,
            stage="render_video",
            component_version="7",
            idempotency_key="render-video-security-boundary",
            status="succeeded",
            priority=100,
            attempt_count=1,
            max_attempts=3,
            available_at=NOW,
            started_at=NOW,
            completed_at=NOW,
            input_json={},
            output_json=output,
            retryable=False,
            created_at=NOW,
        )
        session.add(job)
        session.flush()
        session.add(
            JobStageResult(
                public_id=RESULT_ID,
                job_id=job.id,
                stage=job.stage,
                component_version=job.component_version,
                idempotency_key=job.idempotency_key,
                output_json=output,
                created_at=NOW,
            )
        )


@pytest.mark.parametrize(
    ("manifest", "final_hash", "manifest_hash"),
    (
        (False, "a/../../../" + "x" * 53, "b" * 64),
        (True, "a" * 64, "g" * 64),
    ),
)
def test_verified_media_rejects_noncanonical_persisted_hash_before_file_access(
    video_database: tuple[AnalyzerSettings, sessionmaker[Session]],
    manifest: bool,
    final_hash: str,
    manifest_hash: str,
) -> None:
    settings, factory = video_database
    output = {
        "schema_version": "video-stage-output-v1",
        "run_public_id": RUN_ID,
        "final_video_sha256": final_hash,
        "manifest_public_id": MANIFEST_ID,
        "manifest_sha256": manifest_hash,
    }
    _seed_result(factory, output)
    run_directory = settings.video_run_directory / RUN_ID
    run_directory.mkdir(parents=True)
    raw_path = run_directory / ("video-manifest-v1.json" if manifest else f"final-{final_hash}.mp4")
    escaped_path = raw_path.resolve()
    escaped_path.parent.mkdir(parents=True, exist_ok=True)
    escaped_path.write_bytes(b"malicious persisted media reference")
    adapter = AnalyticsVideoAdapter(factory, settings, clock=lambda: NOW)

    with pytest.raises(PublicProblem) as caught:
        adapter.read_verified_video_media(JOB_ID, manifest)

    assert caught.value.status == 404
    assert caught.value.code == "verified_video_unavailable"
