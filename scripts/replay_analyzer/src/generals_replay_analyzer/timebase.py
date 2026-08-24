"""Explicit replay logic-frame timebase resolution and conversion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SUPPORTED_LOGIC_FRAMES_PER_SECOND = frozenset((30, 60))
_MAX_HEADER_RATE_RELATIVE_ERROR = 0.10


@dataclass(frozen=True, slots=True)
class ReplayTimebase:
    """One resolved or explicitly unknown replay clock with its provenance."""

    logic_frames_per_second: int | None
    source: Literal["replay_header_wall_clock", "engine_manifest", "unknown"]
    observed_frames_per_second: float | None = None

    def to_dict(self) -> dict[str, int | float | str | None]:
        """Return a deterministic JSON-ready timebase description."""
        return {
            "logic_frames_per_second": self.logic_frames_per_second,
            "source": self.source,
            "observed_frames_per_second": self.observed_frames_per_second,
        }


# TheSuperHackers @feature Leex 24/08/2026 Distinguish legacy 30 Hz and high-FPS 60 Hz replay clocks. (#TBD)
def infer_replay_timebase(*, frame_count: int, start_time: int, end_time: int) -> ReplayTimebase:
    """Infer a supported clock from recorded wall time, or preserve uncertainty explicitly."""
    for value, name in ((frame_count, "frame_count"), (start_time, "start_time"), (end_time, "end_time")):
        if type(value) is not int:
            raise TypeError(f"{name} must be a built-in integer")
    elapsed_seconds = end_time - start_time
    if frame_count <= 0 or elapsed_seconds <= 0:
        return ReplayTimebase(None, "unknown")
    observed = frame_count / elapsed_seconds
    nearest = min(SUPPORTED_LOGIC_FRAMES_PER_SECOND, key=lambda candidate: abs(candidate - observed))
    if abs(observed - nearest) / nearest > _MAX_HEADER_RATE_RELATIVE_ERROR:
        return ReplayTimebase(None, "unknown", observed)
    return ReplayTimebase(nearest, "replay_header_wall_clock", observed)


def frame_to_seconds(frame: int, logic_frames_per_second: int) -> float:
    """Convert one nonnegative logic-frame coordinate through an explicit supported clock."""
    if type(frame) is not int:
        raise TypeError("frame must be a built-in integer")
    if frame < 0:
        raise ValueError("frame must be nonnegative")
    if type(logic_frames_per_second) is not int:
        raise TypeError("logic_frames_per_second must be a built-in integer")
    if logic_frames_per_second not in SUPPORTED_LOGIC_FRAMES_PER_SECOND:
        raise ValueError("logic_frames_per_second must be 30 or 60")
    return frame / logic_frames_per_second
