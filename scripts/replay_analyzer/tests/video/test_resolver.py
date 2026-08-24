from pathlib import Path

import pytest

from generals_replay_analyzer.video.resolver import VideoResolutionError, _managed_replay_path


def test_managed_replay_path_rejects_root_escape(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    outside = tmp_path / "outside.rep"
    outside.write_bytes(b"replay")
    with pytest.raises(VideoResolutionError, match="managed replay path escapes"):
        _managed_replay_path(managed, "../outside.rep")
