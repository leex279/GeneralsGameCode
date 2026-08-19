# Copyright 2026 TheSuperHackers
#
# AI Camera Director for automated Zero Hour replay video casting.

import math
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
from .parser import ParsedReplay
from .spatial import SpatialAnalysis, MapCoordinate
from .metrics import MatchMetrics

@dataclass
class CameraKeyframe:
    time_sec: float
    x: float
    y: float
    zoom: float  # 1.0 = standard, 1.5 = zoomed in on micro, 0.7 = wide map overview
    focus_label: str
    target_player: Optional[str] = None
    commentary_event_id: Optional[str] = None

class CameraDirector:
    """Orchestrates professional esports camera choreography across a Zero Hour match."""

    def __init__(self, replay: ParsedReplay, spatial: SpatialAnalysis, metrics: MatchMetrics):
        self.replay = replay
        self.spatial = spatial
        self.metrics = metrics
        self.meta = replay.metadata

    def generate_choreography(self, commentary_events: Optional[List[Any]] = None) -> List[CameraKeyframe]:
        keyframes: List[CameraKeyframe] = []
        bounds = self.spatial.map_bounds
        mid_x = (bounds["min_x"] + bounds["max_x"]) / 2.0
        mid_y = (bounds["min_y"] + bounds["max_y"]) / 2.0

        if commentary_events:
            for idx, ev in enumerate(commentary_events):
                t_start = ev.time_sec
                if idx + 1 < len(commentary_events):
                    next_t = commentary_events[idx + 1].time_sec
                    t_end = max(t_start + 2.0, next_t - 1.5)
                else:
                    t_end = max(t_start + 4.0, self.meta.duration_seconds)

                cx = ev.target_coord["x"] if ev.target_coord and "x" in ev.target_coord else mid_x
                cy = ev.target_coord["y"] if ev.target_coord and "y" in ev.target_coord else mid_y
                zoom = ev.zoom

                keyframes.append(CameraKeyframe(
                    time_sec=t_start,
                    x=cx,
                    y=cy,
                    zoom=zoom,
                    focus_label=ev.label,
                    target_player=ev.focus_player,
                    commentary_event_id=f"event_{idx}"
                ))
                keyframes.append(CameraKeyframe(
                    time_sec=t_end,
                    x=cx,
                    y=cy,
                    zoom=zoom,
                    focus_label=ev.label,
                    target_player=ev.focus_player,
                ))

            # Final match conclusion overview
            dur = max(self.meta.duration_seconds, commentary_events[-1].time_sec + 4.0)
            if keyframes[-1].time_sec < dur:
                keyframes.append(CameraKeyframe(
                    time_sec=dur,
                    x=mid_x,
                    y=mid_y,
                    zoom=0.85,
                    focus_label="Match Concluded & Final Battlefield Overview",
                ))

            keyframes.sort(key=lambda k: k.time_sec)
            return keyframes

        p_bases = self.spatial.player_bases
        p_names = list(p_bases.keys())
        p1_name = p_names[0] if len(p_names) > 0 else "Player 1"
        p2_name = p_names[1] if len(p_names) > 1 else "Player 2"
        p1_base = p_bases.get(p1_name, {"x": mid_x - 400, "y": mid_y - 400})
        p2_base = p_bases.get(p2_name, {"x": mid_x + 400, "y": mid_y + 400})

        # 1. Opening Intro (0:00 - 0:02): Wide Overview
        keyframes.append(CameraKeyframe(
            time_sec=0.0,
            x=mid_x,
            y=mid_y,
            zoom=0.85,
            focus_label="Battlefield Overview & Match Start",
            commentary_event_id="intro"
        ))

        # 2. Player 1 Base Opening (0:02 - 0:16): Arrive and dwell on Player 1 (USA / Bars)
        keyframes.append(CameraKeyframe(
            time_sec=2.0,
            x=p1_base["x"],
            y=p1_base["y"],
            zoom=1.35,
            focus_label=f"{p1_name} Base Opening Setup",
            target_player=p1_name,
            commentary_event_id="p1_opening"
        ))
        keyframes.append(CameraKeyframe(
            time_sec=16.0,
            x=p1_base["x"],
            y=p1_base["y"],
            zoom=1.35,
            focus_label=f"{p1_name} Base Opening Setup",
            target_player=p1_name,
        ))

        # 3. Player 2 Base Opening (0:18 - 0:32): Pan and dwell on Player 2 (China / Cristall)
        keyframes.append(CameraKeyframe(
            time_sec=18.5,
            x=p2_base["x"],
            y=p2_base["y"],
            zoom=1.35,
            focus_label=f"{p2_name} Base Opening Setup",
            target_player=p2_name,
            commentary_event_id="p2_opening"
        ))
        keyframes.append(CameraKeyframe(
            time_sec=32.0,
            x=p2_base["x"],
            y=p2_base["y"],
            zoom=1.35,
            focus_label=f"{p2_name} Base Opening Setup",
            target_player=p2_name,
        ))

        keyframes.sort(key=lambda k: k.time_sec)
        return keyframes

    def get_camera_state_at(self, time_sec: float, keyframes: List[CameraKeyframe]) -> Tuple[float, float, float, str]:
        """Interpolates camera (x, y, zoom) smoothly using cubic easing."""
        if not keyframes:
            return 2000.0, 2000.0, 1.0, "Tactical Overview"

        if time_sec <= keyframes[0].time_sec:
            return keyframes[0].x, keyframes[0].y, keyframes[0].zoom, keyframes[0].focus_label

        if time_sec >= keyframes[-1].time_sec:
            return keyframes[-1].x, keyframes[-1].y, keyframes[-1].zoom, keyframes[-1].focus_label

        # Find surrounding keyframes
        prev_k = keyframes[0]
        next_k = keyframes[-1]
        for i in range(len(keyframes) - 1):
            if keyframes[i].time_sec <= time_sec <= keyframes[i+1].time_sec:
                prev_k = keyframes[i]
                next_k = keyframes[i+1]
                break

        span = max(next_k.time_sec - prev_k.time_sec, 0.001)
        raw_t = (time_sec - prev_k.time_sec) / span

        # Smooth cubic ease-in-out
        t = raw_t * raw_t * (3.0 - 2.0 * raw_t)

        interp_x = prev_k.x + (next_k.x - prev_k.x) * t
        interp_y = prev_k.y + (next_k.y - prev_k.y) * t
        interp_z = prev_k.zoom + (next_k.zoom - prev_k.zoom) * t
        label = next_k.focus_label if raw_t > 0.5 else prev_k.focus_label

        return interp_x, interp_y, interp_z, label
