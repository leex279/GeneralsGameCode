"""WebVTT subtitle generation from the measured narration schedule."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from generals_replay_analyzer.video.voice import NarrationScheduleV1


def _timestamp(frame: int, logic_hz: int) -> str:
    milliseconds = (frame * 1000 + logic_hz // 2) // logic_hz
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


# TheSuperHackers @feature Leex 24/08/2026 Keep subtitle cues exactly aligned to scheduled narration frame windows. (#TBD)
def render_webvtt(schedule: NarrationScheduleV1, destination: Path) -> Path:
    if type(schedule) is not NarrationScheduleV1:
        raise TypeError("subtitle rendering requires a validated narration schedule")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = ["WEBVTT", ""]
    for item in schedule.events:
        subtitle = item.event.subtitle_text.replace("\r", " ").replace("\n", " ").replace("-->", "→")
        lines.extend(
            (
                item.event.event_id,
                (
                    f"{_timestamp(item.start_frame, schedule.logic_hz)} --> "
                    f"{_timestamp(item.end_frame + 1, schedule.logic_hz)}"
                ),
                subtitle,
                "",
            )
        )
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
