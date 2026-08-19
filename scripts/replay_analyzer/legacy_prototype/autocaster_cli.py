# Copyright 2026 TheSuperHackers
#
# Standalone Native In-Engine Auto-Caster Pipeline.
# Orchestrates automated 3D game engine replay playback, AI camera direction,
# DoMiNaToR commentary synthesis, and final broadcast MP4 video assembly.

import os
import sys
import argparse
import subprocess
import shutil

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.replay_analyzer.parser import ReplayParser
from scripts.replay_analyzer.metrics import MetricsCalculator
from scripts.replay_analyzer.reporter import ReplayReporter
from scripts.replay_analyzer.tts_caster import TTSVoiceCaster
from scripts.replay_analyzer.camera_director import CameraDirector

GAME_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\Command & Conquer Generals - Zero Hour"
USER_DATA_DIR = os.path.expanduser(r"~\Documents\Command and Conquer Generals Zero Hour Data")
REPLAYS_DIR = os.path.join(USER_DATA_DIR, "Replays")

class NativeInEngineAutoCaster:
    """Automates end-to-end 3D video casting using the native C++ RTS game engine."""

    def __init__(self, replay_path: str, output_path: str = "broadcast_cast.mp4", resolution: str = "1920x1080", fps: int = 60, auto_launch: bool = True):
        self.replay_path = os.path.abspath(replay_path)
        self.output_path = os.path.abspath(output_path)
        self.res_w, self.res_h = map(int, resolution.split("x"))
        self.fps = fps
        self.auto_launch = auto_launch

    def run(self):
        if not os.path.exists(self.replay_path):
            print(f"Error: Replay file not found: {self.replay_path}", file=sys.stderr)
            sys.exit(1)

        print("================================================================================")
        print(f"  COMMAND & CONQUER: GENERALS ZERO HOUR — NATIVE 3D AUTO-CASTER")
        print(f"  Target Replay: {os.path.basename(self.replay_path)}")
        print(f"  Output Video:  {self.output_path}")
        print(f"  Resolution:    {self.res_w}x{self.res_h} @ {self.fps} FPS")
        print("================================================================================")

        # 1. Parse Replay & Extract Spatial Telemetry
        print("[1/4] Analyzing replay telemetry & combat hotspots...")
        parser = ReplayParser(self.replay_path)
        parsed = parser.parse(parse_commands=True)

        calculator = MetricsCalculator(parsed)
        metrics = calculator.calculate()
        reporter = ReplayReporter(parsed, metrics)

        # 2. Generate DoMiNaToR Commentary & AI Voiceover
        print("[2/4] Synthesizing DoMiNaToR AI broadcast voiceover...")
        temp_audio = os.path.splitext(self.output_path)[0] + "_commentary.mp3"
        try:
            tts = TTSVoiceCaster(reporter.commentary)
            audio_path = tts.generate_commentary_audio(temp_audio)
            has_voiceover = os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000
        except Exception as e:
            print(f"Warning: TTS synthesis skipped ({e})")
            has_voiceover = False

        # 3. Generate In-Engine Camera Choreography Script
        print("[3/4] Generating AI camera trajectory script...")
        director = CameraDirector(parsed, reporter.spatial, metrics)
        cam_choreography = director.generate_choreography(reporter.commentary.get("events"))

        camera_script_path = os.path.splitext(self.output_path)[0] + "_camera.txt"
        with open(camera_script_path, "w", encoding="utf-8") as f:
            for kf in cam_choreography:
                f.write(f"{kf.time_sec:.2f},{kf.x:.1f},{kf.y:.1f},0.0,{kf.zoom:.2f}\n")

        # Copy replay to game directory
        os.makedirs(REPLAYS_DIR, exist_ok=True)
        replay_filename = os.path.basename(self.replay_path)
        dest_replay = os.path.join(REPLAYS_DIR, replay_filename)
        if os.path.abspath(self.replay_path) != os.path.abspath(dest_replay):
            shutil.copy2(self.replay_path, dest_replay)

        print(f"  ✓ Replay in Game Directory: {dest_replay}")
        print(f"  ✓ In-Engine Camera Script:  {camera_script_path} ({len(cam_choreography)} keyframes)")
        if has_voiceover:
            print(f"  ✓ DoMiNaToR AI Voice Track: {temp_audio}")

        # 4. Automatically Launch In-Engine 3D Game Simulation
        if self.auto_launch:
            print("[4/4] Launching 3D Game Engine in Automated Auto-Cast Mode...")
            
            # Use generalszh.exe (which matches Windows Direct3D 8 App Compatibility layer)
            game_exe = os.path.join(GAME_DIR, "generalszh.exe")

            if not os.path.exists(game_exe):
                print(f"Error: Zero Hour executable not found at: {game_exe}", file=sys.stderr)
                return

            temp_video = os.path.splitext(self.output_path)[0] + "_gameplay.mp4"

            cmd = [
                game_exe,
                "-win",
                "-autocast", replay_filename,
                "-autocast-out", temp_video,
                "-autocast-script", camera_script_path,
                "-autocast-fps", str(self.fps),
                "-xres", str(self.res_w),
                "-yres", str(self.res_h),
                "-noaudio",
                "-quickstart",
                "-noshellmap"
            ]

            print(f"  • Executing Modern In-Engine Caster: {game_exe}")
            try:
                proc = subprocess.Popen(cmd, cwd=GAME_DIR)
                print("  • 3D Game engine running in background.")
                print("  • Processing match simulation & camera movements...")
                proc.wait()
                print(f"✓ In-engine process completed (exit code: {proc.returncode})")
            except Exception as e:
                print(f"Error launching game engine: {e}", file=sys.stderr)

            # 5. Mix Commentary Audio with Gameplay
            if os.path.exists(temp_video) and os.path.getsize(temp_video) > 1000:
                if has_voiceover and os.path.exists(temp_audio):
                    print("[5/5] Multiplexing DoMiNaToR AI commentary audio track with gameplay...")
                    mix_cmd = [
                        "ffmpeg", "-y",
                        "-i", temp_video,
                        "-i", temp_audio,
                        "-map", "0:v:0",
                        "-map", "1:a:0",
                        "-c:v", "copy",
                        "-c:a", "aac",
                        "-b:a", "192k",
                        "-shortest",
                        self.output_path
                    ]
                    subprocess.run(mix_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if os.path.exists(self.output_path) and os.path.getsize(self.output_path) > 1000:
                        try:
                            os.remove(temp_video)
                        except OSError:
                            pass
                else:
                    shutil.move(temp_video, self.output_path)
                print(f"✓ Broadcast video finalized: {self.output_path}")

        print("================================================================================")
        print("  ✓ AUTO-CASTER FINISHED")
        print("================================================================================")

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Zero Hour In-Engine Automated 3D Video Caster")
    parser.add_argument("replay_file", help="Path to .rep replay file")
    parser.add_argument("--output", "-o", default="broadcast_cast.mp4", help="Output MP4 file")
    parser.add_argument("--res", default="1920x1080", help="Video resolution (e.g. 1920x1080)")
    parser.add_argument("--fps", type=int, default=60, help="Frame rate")
    parser.add_argument("--no-launch", action="store_true", help="Prepare assets only without launching game")

    args = parser.parse_args()

    caster = NativeInEngineAutoCaster(args.replay_file, args.output, args.res, args.fps, auto_launch=not args.no_launch)
    caster.run()

if __name__ == "__main__":
    main()
