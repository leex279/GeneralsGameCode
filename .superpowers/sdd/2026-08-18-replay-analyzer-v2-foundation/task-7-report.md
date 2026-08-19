# Task 7 Report: Generated Message Catalog and Replay Inspection CLI

## Delivered

- Added the provisional generated Zero Hour 1.04 message catalog at
  `scripts/replay_analyzer/contracts/zero_hour_1_04_message_types.json`.
  It carries schema, game, patch, engine-build, source-header, UTC generation,
  generated-artifact, and replacement provenance. The map uses a list of
  `{id, name}` records so duplicate numeric IDs and duplicate symbolic names
  are detectable during validation rather than being silently overwritten by
  JSON object-key semantics.
- Added `contracts.py`, which validates the full catalog schema and exposes an
  immutable numeric-to-symbolic lookup. Unknown numeric message types remain
  usable and resolve to `None`.
- Added `replay-analyzer inspect <file> [--format human|json] [--commands]`.
  JSON is deterministic and always includes `evidence_tier: observed`, the
  uppercase byte SHA-256, parser/package version, all parser warnings, the full
  parsed header, recorder setup, command count, and completion status.
  `commands` is omitted unless `--commands` is present. When requested, every
  command has `frame`, `seconds`, `player_index`, `message_type`,
  `message_name`, `arguments`, `start_offset`, and `end_offset`; every
  argument has `type`, `type_name`, `value`, and `raw_bytes_hex`.
- Malformed replay parsing returns status 2 with the existing typed parser
  error text and no traceback.

## Authorized Boundary Repair

The required real-fixture CLI inspection initially failed with
`unsupported_argument_type` at byte 429. Source inspection of
`GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp` established that
`RecorderClass::playbackFile` reads four little-endian `Int` values after
`readReplayHeader` and before the first `readNextFrame`:
`difficulty`, `original_game_mode`, `rank_points`, and `max_fps`.

The parser now exposes this source-recorded block as frozen `ReplaySetup` on
`ParsedReplay`, including `start_offset`, `end_offset`, and
`command_stream_offset`; it does not silently discard it. The pinned replay
has setup `(1, 5, 0, 0)` at `[326, 342)`, and its first command starts at 342.
Partial setup blocks fail closed as typed `truncated_replay` errors. This
authorized cross-task repair makes Task 7 inspection and future Task 9 parity
auditable against the engine stream layout.

## TDD Evidence

1. RED: catalog/CLI tests initially failed at collection because
   `generals_replay_analyzer.contracts` and `.cli` did not exist.
2. GREEN investigation: the new pinned-fixture CLI tests exposed the recorder
   setup boundary defect.
3. RED: a focused parser test reproduced the real failure before the setup
   model/parser repair; setup truncation tests also failed before `ReplaySetup`
   was introduced.
4. GREEN: focused parser/catalog/CLI tests passed: `23 passed in 0.19s`.

## Verification

- `uv run --project . ruff check src tests` -> `All checks passed!`
- `uv run --project . mypy --strict src` -> `Success: no issues found in 11 source files`
- Pinned fixture inspection followed by `ConvertFrom-Json` -> evidence tier
  `observed`; SHA-256
  `EA085767BFA11D2CFC167D9007173CE2EB29B5F557702FFD042E2E9A1A8F6BB8`;
  `3993` commands; `complete`; zero warnings; setup end/command-stream offset
  `342`.
- `uv run --project . pytest -q` -> `106 passed in 0.22s`.

No LLM or network dependency is used by the catalog loader or inspection CLI.
