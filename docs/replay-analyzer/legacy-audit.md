# Replay Analyzer legacy trust boundary

## Decision

Replay Analyzer V2 may treat only the Zero Hour engine's replay reader and
observational outputs derived from it as factual match evidence. The legacy
Python modules below are not validated against that reader, a pinned retail
fixture, or an in-engine parse dump. They must not be imported by the V2
package or presented as factual analysis until separately revalidated.

This audit covers the current replay-specific engine modules, the legacy
`scripts/replay_analyzer` and `scripts/replay_player` modules, the standalone
root replay experiment, and repository-root generated replay outputs. Generic
engine callers that merely invoke replay controls are not independent replay
parsers and remain outside this module inventory.

## Authoritative engine code

- `GeneralsMD/Code/GameEngine/Include/Common/Recorder.h` and
  `GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp` — the Zero Hour
  recorder owns the retail replay header/command reader and playback path.
- `Core/GameEngine/Include/Common/MessageStream.h` and
  `Core/GameEngine/Source/Common/MessageStream.cpp` — authoritative message
  and argument-type contract consumed by the recorder.
- `Core/GameEngine/Include/Common/ReplaySimulation.h` and
  `Core/GameEngine/Source/Common/ReplaySimulation.cpp` — headless/in-engine
  replay simulation used for compatibility checks.
- `Core/GameEngine/Source/Common/CommandLine.cpp` — command-line entry points
  for replay playback and headless replay simulation.
- `Core/GameEngine/Source/GameClient/GUI/GUICallbacks/ReplayControls.cpp` and
  the Zero Hour replay menus (`GeneralsMD/Code/GameEngine/Source/GameClient/
  GUI/GUICallbacks/Menus/PopupReplay.cpp`, `ReplayMenu.cpp`) — engine playback
  controls, not offline analysis.
- `Generals/Code/GameEngine/Include/Common/Recorder.h`,
  `Generals/Code/GameEngine/Source/Common/Recorder.cpp`, and its replay menus
  are the base-Generals counterparts. They are reference code only for this
  Zero Hour-first effort.
- `GeneralsReplays/` and `.github/workflows/check-replays.yml` — replay corpus
  and compatibility workflow; they are test evidence, not parsed analytics.

## Reusable but unverified prototype

These files may contain rendering, report, or partial decoding utility worth
revalidating, but none may make factual claims until it passes the V2 parser
and engine-parity gates.

- `scripts/replay_analyzer/__init__.py`, `cli.py`, `parser.py`, and
  `constants.py` — duplicate Python parsing/CLI path without fixture or parity
  coverage.
- `scripts/replay_analyzer/autocaster_cli.py`, `camera_director.py`,
  `commentary.py`, `reporter.py`, `server.py`, `screencast_renderer.py`,
  `tts_caster.py`, and `html_generator.py` — presentation and casting helpers
  coupled to legacy prototype data.
- `scripts/replay_analyzer/spatial.py` — spatial helper that requires a real
  map export before it can support factual conclusions.
- `scripts/replay_player/headless_runner.py` and `webgl_generator.py` —
  experiment runners/renderers that must consume validated telemetry instead
  of reconstructed state.
- `dump_replay.py` — standalone root decoding experiment with a fixed local
  path and no parity or fixture verification.
- `Core/GameEngine/Include/GameClient/AutoCameraDirector.h` and
  `Core/GameEngine/Source/GameClient/AutoCameraDirector.cpp` — in-engine
  casting support that is useful after the analytics source is trusted, but it
  is not itself factual replay analysis.

## Synthetic/non-factual prototype

The following modules contain assumptions, generated world state, procedural
map data, hard-coded mappings, or heuristic labels. They are explicitly
**untrusted for factual analysis**.

- `scripts/replay_analyzer/map_loader.py` — procedural/fallback map geography;
  untrusted for factual analysis.
- `scripts/replay_analyzer/unit_tracker.py` — inferred unit state and player
  mapping; untrusted for factual analysis.
- `scripts/replay_analyzer/heuristics.py` — hard-coded strategy/skill rules;
  untrusted for factual analysis.
- `scripts/replay_analyzer/metrics.py` — metrics derived from unverified
  assumptions; untrusted for factual analysis.
- `scripts/replay_player/simulator.py` — synthetic world reconstruction,
  entities, movement, combat, and effects; untrusted for factual analysis.

## Generated artifact

Repository-root `.mp4`, `.webm`, `.wav`, `.mp3`, `.png`, `.jpg`, `.log`,
generated `.html`, replay diagnostic `.txt`, and standalone replay-experiment
`.py` outputs are not source evidence. Inventory them with
`scripts/replay_analyzer/tools/inventory_legacy_outputs.ps1` before archival;
the inventory records a relative path, byte length, SHA-256, Git tracked
status, and proposed archive category. The inventory does not move or delete
artifacts. A later, explicit archival phase must verify the stored hash and
refuse tracked files.

Narrow ignore rules intentionally cover only replay-analyzer staging and
diagnostics. They do not suppress general JSON, PNG, or MP4 files.
