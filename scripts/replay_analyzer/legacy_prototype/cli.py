# Copyright 2026 TheSuperHackers
#
# Command-line interface for analyzing C&C Generals & Zero Hour replays.

import argparse
import sys
import os

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.replay_analyzer.parser import ReplayParser
from scripts.replay_analyzer.metrics import MetricsCalculator
from scripts.replay_analyzer.reporter import ReplayReporter

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Command & Conquer: Generals / Zero Hour Replay Analyzer & Automated Video Caster"
    )
    parser.add_argument("replay_paths", nargs="+", help="Path to .rep replay file(s) or directories")
    parser.add_argument("--html", action="store_true", help="Output interactive HTML visual dashboard")
    parser.add_argument("--video", "-v", nargs="?", const="match_cast.mp4", help="Generate automated narrated video screencast MP4")
    parser.add_argument("--speedup", type=float, default=8.0, help="Video playback speedup factor (default: 8x action highlight)")
    parser.add_argument("--json", action="store_true", help="Output raw analysis as JSON")
    parser.add_argument("--markdown", "-md", action="store_true", help="Output analysis in Markdown format")
    parser.add_argument("--output", "-o", help="Save output to specific file")
    parser.add_argument("--fps", type=float, default=15.0, help="Simulation logic tick rate (default: 15.0 FPS)")

    args = parser.parse_args()

    # Expand any directories into .rep files
    all_files = []
    for path in args.replay_paths:
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                for f in files:
                    if f.lower().endswith(".rep"):
                        all_files.append(os.path.join(root, f))
        elif os.path.isfile(path):
            all_files.append(path)
        else:
            print(f"Error: Path not found: {path}", file=sys.stderr)

    for path in all_files:
        try:
            p = ReplayParser(path, logic_fps=args.fps)
            parsed_replay = p.parse(parse_commands=True)
            calculator = MetricsCalculator(parsed_replay)
            metrics = calculator.calculate()
            reporter = ReplayReporter(parsed_replay, metrics)

            if args.video:
                from scripts.replay_analyzer.tts_caster import TTSVoiceCaster
                from scripts.replay_analyzer.screencast_renderer import ScreencastRenderer

                out_mp4 = args.video if isinstance(args.video, str) and not args.video.startswith("-") else (args.output or "match_cast.mp4")
                audio_file = os.path.splitext(out_mp4)[0] + "_audio.mp3"

                print(f"[Caster] Synthesizing DoMiNaToR AI broadcast voiceover for {os.path.basename(path)}...")
                tts = TTSVoiceCaster(reporter.commentary)
                audio_path = tts.generate_commentary_audio(audio_file)

                print(f"[Caster] Rendering 1080p Tactical Video with AI Camera Director...")
                renderer = ScreencastRenderer(
                    parsed_replay, metrics, reporter.spatial,
                    reporter.scorecard, reporter.player_reports, reporter.commentary
                )
                renderer.render_video(audio_path, out_mp4, speedup=args.speedup)
                if os.path.exists(audio_file):
                    os.remove(audio_file)
                print(f"✓ Video cast saved to: {out_mp4}")

            elif args.html:
                result = reporter.to_html()
                out_path = args.output or (os.path.splitext(path)[0] + "_report.html")
                with open(out_path, "w", encoding="utf-8") as out_f:
                    out_f.write(result)
                print(f"Report saved to: {out_path}")

            elif args.json:
                result = reporter.to_json()
                if args.output:
                    with open(args.output, "w", encoding="utf-8") as out_f:
                        out_f.write(result)
                    print(f"JSON saved to: {args.output}")
                else:
                    print(result)

            elif args.markdown:
                result = reporter.to_markdown()
                if args.output:
                    with open(args.output, "w", encoding="utf-8") as out_f:
                        out_f.write(result)
                    print(f"Markdown saved to: {args.output}")
                else:
                    print(result)

            else:
                result = reporter.to_terminal()
                print(result)
                if len(all_files) > 1:
                    print("\n" + "=" * 80 + "\n")

        except Exception as e:
            print(f"Error analyzing {path}: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()



if __name__ == "__main__":
    main()
