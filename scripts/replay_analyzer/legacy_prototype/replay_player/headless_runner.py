# Copyright 2026 TheSuperHackers
#
# Headless Replay Simulator & 3D WebGL Player CLI for AI Agents & Continuous Integration.

import os
import sys
import argparse
import json

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.replay_analyzer.parser import ReplayParser
from scripts.replay_player.simulator import StandaloneReplaySimulator
from scripts.replay_player.webgl_generator import WebGLPlayerGenerator

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Standalone Headless C&C Zero Hour Replay Simulator & 3D Player"
    )

    parser.add_argument("replay_file", help="Path to .rep replay file")
    parser.add_argument("--html-3d", "-3d", help="Export standalone interactive 3D WebGL player HTML file")
    parser.add_argument("--json", "-j", help="Export 3D simulation trajectory as JSON for AI agents")
    parser.add_argument("--summary", action="store_true", help="Print headless simulation summary")

    args = parser.parse_args()

    if not os.path.exists(args.replay_file):
        print(f"Error: File not found: {args.replay_file}", file=sys.stderr)
        sys.exit(1)

    print(f"[Simulator] Parsing replay: {args.replay_file}...")
    p = ReplayParser(args.replay_file)
    parsed = p.parse(parse_commands=True)

    sim = StandaloneReplaySimulator(parsed)
    sim_data = sim.simulate_all_entities()

    units = [e for e in sim_data["entities"] if e["type"] != "STRUCTURE"]
    structs = [e for e in sim_data["entities"] if e["type"] == "STRUCTURE"]
    fx = sim_data["combat_fx"]

    print(f"✓ Headless Simulation Complete:")
    print(f"  • Map: {sim_data['metadata']['map']}")
    print(f"  • Duration: {sim_data['metadata']['duration_sec']/60.0:.1f} minutes")
    print(f"  • Total Simulated Units: {len(units)}")
    print(f"  • Total Constructed Buildings: {len(structs)}")
    print(f"  • Simulated Combat Clashes: {len(fx)}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(sim_data, f, indent=2)
        print(f"✓ Exported 3D Telemetry JSON to: {args.json}")

    if args.html_3d or (not args.json and not args.summary):
        out_html = args.html_3d or "replay_3d_player.html"
        gen = WebGLPlayerGenerator(parsed)
        gen.generate_html(out_html)
        print(f"✓ Exported Standalone 3D Player to: {out_html}")

if __name__ == "__main__":
    main()
