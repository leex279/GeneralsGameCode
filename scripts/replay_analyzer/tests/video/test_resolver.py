from pathlib import Path

import pytest

from generals_replay_analyzer.video.resolver import VideoResolutionError, _authority_from_payload, _managed_replay_path


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
            }, 10,
        )
