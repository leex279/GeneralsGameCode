import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from generals_replay_analyzer.spatial.query import MapSceneReadModel
from generals_replay_analyzer.video import resolver as video_resolver
from generals_replay_analyzer.video.resolver import (
    VideoRequestResolver,
    VideoResolutionError,
    _authority_from_payload,
    _managed_replay_path,
    _resolve_authoritative_logic_fps,
)


@pytest.mark.parametrize(
    ("frame_count", "accepted_evidence_end", "expected_frame_end"),
    ((108, 108, 107), (108, 99, 99), (1, 1, 0)),
)
def test_renderable_horizon_caps_evidence_at_the_last_presentable_replay_frame(
    frame_count: int,
    accepted_evidence_end: int,
    expected_frame_end: int,
) -> None:
    assert video_resolver._renderable_frame_end(frame_count, accepted_evidence_end) == expected_frame_end


def test_video_request_authority_uses_the_last_presentable_frame_not_the_header_count(tmp_path: Path) -> None:
    replay_bytes = b"fixed replay bytes"
    replay_sha256 = hashlib.sha256(replay_bytes).hexdigest()
    (tmp_path / "accepted.rep").write_bytes(replay_bytes)
    replay = SimpleNamespace(
        id=1,
        public_id="123e4567-e89b-42d3-a456-426614174000",
        managed_asset_id=2,
        sha256=replay_sha256,
        frame_count=108,
        header_json={
            "timebase": {
                "source": "engine_manifest",
                "logic_frames_per_second": 30,
            }
        },
    )
    asset = SimpleNamespace(sha256=replay_sha256, relative_path="accepted.rep")

    class Session:
        def __enter__(self) -> object:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def scalar(self, _: object) -> object:
            return replay

        def get(self, *_: object) -> object:
            return asset

        def scalars(self, _: object) -> tuple[object, ...]:
            return ()

    scene = MapSceneReadModel(
        {
            "available_frame_window": {"frame_start": 0, "frame_end": 108},
            "telemetry_run_public_id": "123e4567-e89b-42d3-a456-426614174002",
            "telemetry_trace_sha256": "a" * 64,
            "map_public_id": "123e4567-e89b-42d3-a456-426614174003",
            "map_content_sha256": "b" * 64,
        }
    )
    resolver = VideoRequestResolver.__new__(VideoRequestResolver)
    resolver._sessions = Session
    resolver._settings = SimpleNamespace(data_root=tmp_path)
    resolver._reports = SimpleNamespace(get_report=lambda _: SimpleNamespace())
    resolver._scenes = SimpleNamespace(get_canonical_scene=lambda _: scene)

    request = resolver.resolve(
        {
            "replay_public_id": replay.public_id,
            "replay_sha256": replay_sha256,
            "report_public_id": "123e4567-e89b-42d3-a456-426614174001",
            "evidence_horizon": "partial",
            "diagnostic_preview": True,
            "logic_frames_per_second": 30,
        }
    )

    assert request.authority.evidence_horizon.frame_end == 107


def test_managed_replay_path_rejects_root_escape(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    outside = tmp_path / "outside.rep"
    outside.write_bytes(b"replay")
    with pytest.raises(VideoResolutionError, match="managed replay path escapes"):
        _managed_replay_path(managed, "../outside.rep")


def test_authority_payload_rejects_noncanonical_uuid_and_uppercase_sha() -> None:
    with pytest.raises(VideoResolutionError, match="authority identities are invalid"):
        _authority_from_payload(
            "123e4567-e89b-42d3-a456-426614174000", "123e4567-e89b-42d3-a456-426614174001", "A" * 64,
            {
                "telemetry_run_public_id": "123e4567-e89b-42d3-a456-426614174002",
                "telemetry_trace_sha256": "b" * 64,
                "map_public_id": "123e4567-e89b-42d3-a456-426614174003",
                "map_content_sha256": "c" * 64,
            }, 10, 30,
        )


def test_logic_timebase_resolution_prefers_engine_manifest_and_rejects_request_mismatch() -> None:
    header = {
        "timebase": {
            "logic_frames_per_second": 60,
            "source": "engine_manifest",
            "parser_inferred_logic_frames_per_second": 30,
        }
    }

    assert _resolve_authoritative_logic_fps(header, ()) == 60
    assert _resolve_authoritative_logic_fps(header, (), requested=60) == 60
    with pytest.raises(VideoResolutionError, match="differs from engine authority"):
        _resolve_authoritative_logic_fps(header, (), requested=30)


def test_logic_timebase_resolution_fails_closed_for_unknown_v2_and_preserves_explicit_v1() -> None:
    with pytest.raises(VideoResolutionError, match="engine logic timebase is unavailable"):
        _resolve_authoritative_logic_fps({}, ((2, {}),), requested=30)

    assert _resolve_authoritative_logic_fps(
        {},
        ((1, {"logic_frames_per_second": 30, "logic_timebase_source": "historical_v1_contract"}),),
        requested=30,
    ) == 30
