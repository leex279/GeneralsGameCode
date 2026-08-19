# Copyright 2026 TheSuperHackers
#
# Authentic C&C Generals: Zero Hour In-Game Styled Video Renderer.
# Features real .tga map textures, the iconic Generals Control Bar, unit health bars, and selection rings.

import os
import math
import subprocess
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from typing import Dict, Any, List, Optional, Tuple

from .parser import ParsedReplay
from .metrics import MatchMetrics
from .spatial import SpatialAnalysis
from .camera_director import CameraDirector, CameraKeyframe
from .heuristics import PlayerSkillReport, MatchQualityScorecard
from .unit_tracker import UnitTracker, WorldSimulationState
from .map_loader import MapPreviewLoader

class ScreencastRenderer:
    """Renders broadcast video styled identically to the real C&C Generals Zero Hour game."""

    def __init__(
        self,
        replay: ParsedReplay,
        metrics: MatchMetrics,
        spatial: SpatialAnalysis,
        scorecard: MatchQualityScorecard,
        player_reports: Dict[str, PlayerSkillReport],
        commentary: Dict[str, Any]
    ):
        self.replay = replay
        self.meta = replay.metadata
        self.metrics = metrics
        self.spatial = spatial
        self.scorecard = scorecard
        self.player_reports = player_reports
        self.commentary = commentary

        self.width = 1920
        self.height = 1080
        self.fps = 30

    def render_video(
        self,
        audio_path: str,
        output_mp4: str = "match_screencast.mp4",
        speedup: float = 6.0
    ):
        director = CameraDirector(self.replay, self.spatial, self.metrics)
        keyframes = director.generate_choreography()

        # Load Real Map Preview Texture
        map_loader = MapPreviewLoader(self.meta.map_name, self.spatial.map_bounds)
        map_base_img = map_loader.get_map_image((2560, 2560))

        # Run Unit Tracker Simulation
        tracker = UnitTracker(self.replay, self.spatial)
        sim_state = tracker.simulate()

        total_sim_sec = max(self.meta.duration_seconds, 10.0)
        video_duration_sec = total_sim_sec / speedup
        total_frames = int(video_duration_sec * self.fps)

        print(f"[VideoRenderer] Loaded authentic map texture for: {self.meta.map_name}")
        print(f"[VideoRenderer] Rendering {total_frames} frames ({video_duration_sec:.1f}s video at {self.fps} FPS)...")

        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{self.width}x{self.height}",
            "-r", str(self.fps),
            "-i", "-",
        ]

        if audio_path and os.path.exists(audio_path):
            cmd.extend(["-i", audio_path, "-shortest"])

        cmd.extend([
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "19",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            output_mp4
        ])

        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        try:
            f_title = ImageFont.truetype("arial.ttf", 24)
            f_header = ImageFont.truetype("arial.ttf", 18)
            f_body = ImageFont.truetype("arial.ttf", 13)
            f_small = ImageFont.truetype("arial.ttf", 10)
            f_led = ImageFont.truetype("arial.ttf", 22)
        except Exception:
            f_title = ImageFont.load_default()
            f_header = ImageFont.load_default()
            f_body = ImageFont.load_default()
            f_small = ImageFont.load_default()
            f_led = ImageFont.load_default()

        bounds = self.spatial.map_bounds
        p_names = list(self.player_reports.keys())
        p1_name = p_names[0] if len(p_names) > 0 else "Player 1"
        p2_name = p_names[1] if len(p_names) > 1 else "Player 2"

        player_colors = {
            p1_name: (0, 240, 255),    # USA Electric Blue
            p2_name: (248, 113, 113),  # GLA Red / Orange
        }

        for frame_idx in range(total_frames):
            sim_time = (frame_idx / total_frames) * total_sim_sec
            mins = int(sim_time // 60)
            secs = int(sim_time % 60)
            time_str = f"{mins:02d}:{secs:02d}"

            # Camera coordinates
            cam_x, cam_y, cam_zoom, focus_label = director.get_camera_state_at(sim_time, keyframes)
            snap = tracker.get_world_snapshot_at(sim_state, sim_time)

            # Center of camera viewport (above control bar)
            cx_screen, cy_screen = 960, 470
            scale = 0.35 * cam_zoom

            def world_to_screen(wx, wy):
                sx = cx_screen + (wx - cam_x) * scale
                sy = cy_screen - (wy - cam_y) * scale
                return sx, sy

            # 1. Render Map Terrain
            # Crop/Transform authentic map texture to match camera
            mw, mh = map_base_img.size
            crop_size = int(mw / max(cam_zoom, 0.4))
            norm_cx = (cam_x - bounds["min_x"]) / max(bounds["width"], 1)
            norm_cy = 1.0 - (cam_y - bounds["min_y"]) / max(bounds["height"], 1)
            
            src_x = int(norm_cx * mw - crop_size / 2)
            src_y = int(norm_cy * mh - crop_size / 2)
            src_x = max(0, min(src_x, mw - crop_size))
            src_y = max(0, min(src_y, mh - crop_size))

            cropped = map_base_img.crop((src_x, src_y, src_x + crop_size, src_y + crop_size))
            view_bg = cropped.resize((self.width, self.height), Image.Resampling.BILINEAR)

            img = view_bg.copy()
            draw = ImageDraw.Draw(img)

            # Darken edges for tactical RTS feel
            draw.rectangle([0, 0, self.width, self.height], outline=(0, 0, 0), width=3)

            # 2. Draw Supply Docks & Tech Oil Derricks
            for sd in snap["supply_docks"]:
                sx, sy = world_to_screen(sd["x"], sd["y"])
                # Supply Dock Platform
                draw.rectangle([sx - 18, sy - 18, sx + 18, sy + 18], fill=(30, 41, 59), outline=(16, 185, 129), width=2)
                draw.rectangle([sx - 8, sy - 8, sx + 8, sy + 8], fill=(16, 185, 129))
                draw.text((sx - 16, sy - 30), "SUPPLY DOCK", fill=(16, 185, 129), font=f_small)

            for od in snap["oil_derricks"]:
                ox, oy = world_to_screen(od["x"], od["y"])
                draw.polygon([(ox, oy - 18), (ox - 12, oy + 12), (ox + 12, oy + 12)], fill=(245, 158, 11), outline=(255, 255, 255), width=2)
                draw.text((ox - 14, oy + 16), "OIL DERRICK", fill=(245, 158, 11), font=f_small)

            # 3. Draw All Buildings with In-Game Health Bars
            for st in snap["structures"]:
                sx, sy = world_to_screen(st["x"], st["y"])
                p_col = player_colors.get(st["player"], (0, 240, 255))

                if st["is_building"]:
                    # Construction scaffolding
                    draw.rectangle([sx - 16, sy - 16, sx + 16, sy + 16], outline=p_col, width=2)
                    draw.text((sx - 14, sy - 6), "CONSTRUCT", fill=p_col, font=f_small)
                else:
                    # Completed Building Base
                    draw.rectangle([sx - 20, sy - 20, sx + 20, sy + 20], fill=(15, 23, 42), outline=p_col, width=3)
                    draw.ellipse([sx - 6, sy - 6, sx + 6, sy + 6], fill=p_col)
                    draw.text((sx - 24, sy - 34), st["name"][:20], fill=(255, 255, 255), font=f_body)

                    # Building Health Bar
                    draw.rectangle([sx - 20, sy - 24, sx + 20, sy - 20], fill=(0, 0, 0))
                    draw.rectangle([sx - 19, sy - 23, sx + 19, sy - 21], fill=(34, 197, 94)) # Green HP

            # 4. Draw All Living Units with Selection Rings & Turrets
            for u in snap["units"]:
                ux, uy = world_to_screen(u["x"], u["y"])
                u_col = player_colors.get(u["player"], (0, 240, 255))

                # Selection Circle (In-Game Style)
                draw.ellipse([ux - 12, uy - 12, ux + 12, uy + 12], outline=u_col, width=1)

                if u["category"] == "VEHICLE":
                    # Armored Vehicle Body
                    ang = math.radians(u["heading"])
                    draw.rectangle([ux - 8, uy - 6, ux + 8, uy + 6], fill=u_col, outline=(255, 255, 255), width=1)
                    # Gun Turret & Barrel
                    draw.line([(ux, uy), (ux + math.cos(ang) * 16, uy - math.sin(ang) * 16)], fill=(255, 255, 255), width=2)

                elif u["category"] == "AIRCRAFT":
                    # Chinook with spinning rotor
                    draw.ellipse([ux - 10, uy - 10, ux + 10, uy + 10], fill=u_col, outline=(255, 255, 255), width=1)
                    rotor_ang = frame_idx * 0.4
                    draw.line([(ux - math.cos(rotor_ang)*18, uy - math.sin(rotor_ang)*18),
                               (ux + math.cos(rotor_ang)*18, uy + math.sin(rotor_ang)*18)], fill=(255, 255, 255), width=2)

                elif u["category"] == "WORKER":
                    draw.polygon([(ux, uy - 7), (ux - 6, uy + 5), (ux + 6, uy + 5)], fill=(251, 191, 36))

                else: # INFANTRY
                    draw.ellipse([ux - 5, uy - 5, ux + 5, uy + 5], fill=u_col)

                # Unit Health Bar Hovering Above
                draw.rectangle([ux - 10, uy - 15, ux + 10, uy - 12], fill=(0, 0, 0))
                draw.rectangle([ux - 9, uy - 14, ux + 9, uy - 13], fill=(34, 197, 94))

            # 5. Draw Combat Laser Beams & Rockets
            for fx in snap["combat_fx"]:
                fx1 = world_to_screen(fx.get("from_x", fx.get("fx", 0)), fx.get("from_y", fx.get("fy", 0)))
                fx2 = world_to_screen(fx.get("to_x", fx.get("tx", 0)), fx.get("to_y", fx.get("ty", 0)))

                if fx["type"] == "LASER":
                    # USA Electric Cyan Laser Lock
                    draw.line([fx1, fx2], fill=(0, 240, 255), width=3)
                    # Crosshair on target
                    draw.ellipse([fx2[0] - 8, fx2[1] - 8, fx2[0] + 8, fx2[1] + 8], outline=(0, 240, 255), width=2)
                    draw.line([(fx2[0] - 12, fx2[1]), (fx2[0] + 12, fx2[1])], fill=(0, 240, 255), width=1)
                    draw.line([(fx2[0], fx2[1] - 12), (fx2[0], fx2[1] + 12)], fill=(0, 240, 255), width=1)
                elif fx["type"] == "ROCKET":
                    # RPG / Missile Rocket Trail
                    draw.line([fx1, fx2], fill=(251, 191, 36), width=2)
                    draw.ellipse([fx2[0] - 10, fx2[1] - 10, fx2[0] + 10, fx2[1] + 10], fill=(248, 113, 113))
                else:
                    draw.line([fx1, fx2], fill=(255, 255, 100), width=1)

            # 6. AUTHENTIC ZERO HOUR CONTROL BAR & HUD
            # Control Bar Base Frame (Bottom 180px)
            cb_y = self.height - 180
            draw.rectangle([0, cb_y, self.width, self.height], fill=(15, 20, 28))
            draw.line([(0, cb_y), (self.width, cb_y)], fill=(50, 65, 85), width=3)

            # Left: Tactical Radar Minimap Frame
            draw.rectangle([20, cb_y + 12, 176, self.height - 12], fill=(8, 12, 18), outline=(59, 130, 246), width=2)
            # Radar Sweep
            sw_ang = (frame_idx * 0.15) % (2 * math.pi)
            rcx, rcy = 98, cb_y + 86
            draw.line([(rcx, rcy), (rcx + math.cos(sw_ang)*65, rcy + math.sin(sw_ang)*65)], fill=(0, 240, 255), width=1)
            draw.text((32, cb_y + 18), "TACTICAL RADAR", fill=(0, 240, 255), font=f_small)

            # Center: Generals Command Grid (3x3 Buttons)
            grid_start_x = 220
            btn_labels = ["MOVE", "ATTACK", "GUARD", "STOP", "SCATTER", "PATROL", "FORMATION", "CHEER", "OPTIONS"]
            for bi in range(9):
                bx = grid_start_x + (bi % 3) * 54
                by = cb_y + 16 + (bi // 3) * 50
                draw.rectangle([bx, by, bx + 48, by + 44], fill=(25, 34, 48), outline=(60, 80, 110), width=1)
                draw.text((bx + 6, by + 16), btn_labels[bi], fill=(148, 163, 184), font=f_small)

            # Center-Right: Selected Unit / Army Status Box
            stat_x = grid_start_x + 180
            draw.rectangle([stat_x, cb_y + 16, stat_x + 360, self.height - 16], fill=(20, 28, 40), outline=(50, 70, 95), width=1)
            p1_count = len([u for u in snap["units"] if u["player"] == p1_name])
            p2_count = len([u for u in snap["units"] if u["player"] == p2_name])
            draw.text((stat_x + 16, cb_y + 24), f"ARMY STATUS // OBSERVER MODE", fill=(0, 240, 255), font=f_header)
            draw.text((stat_x + 16, cb_y + 54), f"• {p1_name} (USA): {p1_count} Units Active", fill=(0, 240, 255), font=f_body)
            draw.text((stat_x + 16, cb_y + 78), f"• {p2_name} (GLA): {p2_count} Units Active", fill=(248, 113, 113), font=f_body)
            draw.text((stat_x + 16, cb_y + 104), f"Focus: {focus_label}", fill=(251, 191, 36), font=f_body)

            # Right: DoMiNaToR AI Radio Comms & Subtitles
            comm_x = stat_x + 380
            draw.rectangle([comm_x, cb_y + 16, self.width - 20, self.height - 16], fill=(10, 15, 22), outline=(0, 240, 255), width=1)
            draw.text((comm_x + 16, cb_y + 22), "🎙️ -DoMiNaToR- // BROADCAST CASTER", fill=(0, 240, 255), font=f_header)
            # Audio wave
            for wi in range(25):
                wave_h = int(abs(math.sin(frame_idx * 0.2 + wi * 0.5)) * 14) + 2
                draw.line([(comm_x + 16 + wi * 8, cb_y + 56 - wave_h), (comm_x + 16 + wi * 8, cb_y + 56 + wave_h)], fill=(34, 197, 94), width=2)

            sub_txt = self.commentary.get("opening_phase" if sim_time < 180 else "midgame_phase", "")[:130] + "..."
            draw.text((comm_x + 16, cb_y + 80), f"\"{sub_txt}\"", fill=(241, 245, 249), font=f_body)

            # TOP HEADER BAR: In-Game Money Counter & Promotion Stars
            draw.rectangle([0, 0, self.width, 50], fill=(12, 16, 24, 220))
            draw.line([(0, 50), (self.width, 50)], fill=(40, 55, 75), width=2)

            draw.text((30, 12), f"C&C GENERALS: ZERO HOUR // {self.meta.map_name.upper()}", fill=(255, 255, 255), font=f_header)
            draw.text((880, 12), f"⏱ {time_str}", fill=(255, 255, 255), font=f_title)

            # Iconic LED Money Counter (Top Right)
            draw.rectangle([self.width - 280, 8, self.width - 30, 42], fill=(8, 12, 18), outline=(251, 191, 36), width=1)
            draw.text((self.width - 270, 12), f"$ {self.meta.starting_cash:,}", fill=(251, 191, 36), font=f_led)

            # Write Frame to FFmpeg pipe
            try:
                proc.stdin.write(img.tobytes())
            except (BrokenPipeError, OSError):
                break

        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.wait()
        print(f"[VideoRenderer] Successfully rendered in-game styled MP4: {output_mp4}")
        return output_mp4

