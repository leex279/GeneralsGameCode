"""Replay timebase inference and conversion contracts."""

from __future__ import annotations

import pytest

from generals_replay_analyzer.timebase import ReplayTimebase, frame_to_seconds, infer_replay_timebase


def test_pinned_high_fps_replay_is_inferred_as_sixty_hertz() -> None:
    timebase = infer_replay_timebase(frame_count=56_003, start_time=1_785_594_093, end_time=1_785_595_033)

    assert timebase.logic_frames_per_second == 60
    assert timebase.source == "replay_header_wall_clock"
    assert timebase.observed_frames_per_second == pytest.approx(56_003 / 940)
    assert frame_to_seconds(56_003, timebase.logic_frames_per_second) == pytest.approx(933.3833333333333)


def test_legacy_replay_is_inferred_as_thirty_hertz() -> None:
    timebase = infer_replay_timebase(frame_count=27_900, start_time=100, end_time=1_030)

    assert timebase == ReplayTimebase(
        logic_frames_per_second=30,
        source="replay_header_wall_clock",
        observed_frames_per_second=30.0,
    )


@pytest.mark.parametrize(
    ("frame_count", "start_time", "end_time"),
    (
        (0, 100, 200),
        (100, 200, 200),
        (100, 300, 200),
        (4_500, 100, 200),
    ),
)
def test_untrustworthy_header_timebase_remains_unknown(frame_count: int, start_time: int, end_time: int) -> None:
    timebase = infer_replay_timebase(frame_count=frame_count, start_time=start_time, end_time=end_time)

    assert timebase.logic_frames_per_second is None
    assert timebase.source == "unknown"


@pytest.mark.parametrize(("frame", "fps"), ((-1, 30), (1, 0), (1, 45), (True, 30), (1, True)))
def test_frame_conversion_rejects_invalid_coordinates(frame: int, fps: int) -> None:
    with pytest.raises((TypeError, ValueError)):
        frame_to_seconds(frame, fps)
