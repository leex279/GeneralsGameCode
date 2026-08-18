# Replay Analyzer V2 Foundation Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a clean, testable Python analyzer package whose strict Zero Hour 1.04 parser matches the authoritative C++ replay reader byte-for-byte on a pinned real replay fixture.

**Architecture:** Keep the new package isolated under `scripts/replay_analyzer/src/`. Parse binary data with bounded readers and immutable typed models, preserve byte offsets and unknown numeric message types, and verify results against a modern-only NDJSON observer attached to `RecorderClass::appendNextCommand()`. Quarantine the old synthetic prototype outside the import path.

**Tech Stack:** Python 3.11, uv, pytest, pytest-cov, Ruff, mypy, C++20 diagnostic writer using engine `File`/`GameMessage` APIs, CMake modern-build guard.

**Spec:** `docs/superpowers/specs/2026-08-18-replay-analyzer-v2-design.md` sections 2, 5, 7, 11, 17, and 18.

## Global Constraints

- Treat `GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp` and `Core/GameEngine/Include/Common/MessageStream.h` as the binary-contract source of truth.
- Use 30 logic frames per second from `WWSyncPerSecond`; do not accept a CLI override for replay time conversion.
- Fail closed with an exact offset when an unknown argument type prevents reliable alignment.
- Preserve unknown message numbers and raw argument bytes when their width is known.
- The C++ dump is observational and modern-only. It may not modify messages, frame order, CRC state, or playback decisions.
- Do not delete generated root artifacts in this phase. Inventory first; archive only through a dry-run-capable script after hashes are written.
- Do not import any module from `scripts/replay_analyzer/legacy_prototype/` in production or tests.

---

## Task 1: Record the legacy trust boundary and cleanup inventory

**Files:**
- Create: `docs/replay-analyzer/legacy-audit.md`
- Create: `scripts/replay_analyzer/tools/inventory_legacy_outputs.ps1`
- Modify: `.gitignore`

- [ ] Write the failing acceptance check by running:

  ```powershell
  Test-Path docs\replay-analyzer\legacy-audit.md
  ```

  Expected: `False` before implementation.

- [ ] Implement `inventory_legacy_outputs.ps1` with no mutation. It must enumerate repository-root `.mp4`, `.webm`, `.wav`, `.mp3`, `.png`, `.jpg`, `.log`, generated `.html`, replay diagnostic `.txt`, and standalone replay experiment `.py` files; emit relative path, byte length, SHA-256, tracked status, and proposed archive category as UTF-8 JSON.
- [ ] Document every current replay-related module in `legacy-audit.md` under one of four headings: authoritative engine code, reusable but unverified prototype, synthetic/non-factual prototype, or generated artifact. Explicitly classify `map_loader.py`, `unit_tracker.py`, `heuristics.py`, `metrics.py`, and `scripts/replay_player/simulator.py` as untrusted for factual analysis.
- [ ] Add narrow ignore entries for `/replay-analyzer-artifacts/`, `/autocast_diag.txt`, `/replay_parse_dump.ndjson`, `/telemetry*.ndjson`, and `/scripts/replay_analyzer/.coverage`. Do not add a blanket `*.json`, `*.png`, or `*.mp4` rule.
- [ ] Run:

  ```powershell
  powershell -NoProfile -File scripts\replay_analyzer\tools\inventory_legacy_outputs.ps1 -OutputPath .tmp\replay-legacy-inventory.json
  Get-Content -Raw .tmp\replay-legacy-inventory.json | ConvertFrom-Json | Select-Object -ExpandProperty files | Measure-Object
  ```

  Expected: a non-zero count and no moved or deleted files.

- [ ] Commit only these files:

  ```powershell
  git add .gitignore docs/replay-analyzer/legacy-audit.md scripts/replay_analyzer/tools/inventory_legacy_outputs.ps1
  git commit -m "chore(replay): Inventory legacy replay artifacts"
  ```

## Task 2: Scaffold the isolated Python package and quality gates

**Files:**
- Create: `scripts/replay_analyzer/pyproject.toml`
- Create: `scripts/replay_analyzer/README.md`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/__init__.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/py.typed`
- Create: `scripts/replay_analyzer/tests/test_package.py`
- Create: `scripts/replay_analyzer/uv.lock`

- [ ] Write `test_package.py` first. Assert that `generals_replay_analyzer.__version__` is a non-empty semantic version and `LOGIC_FRAMES_PER_SECOND == 30`.
- [ ] Run:

  ```powershell
  uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/test_package.py -q
  ```

  Expected: failure because the package does not exist.

- [ ] Define the package as `generals-replay-analyzer`, Python `>=3.11,<3.13`, with console script `replay-analyzer = generals_replay_analyzer.cli:main`. Runtime dependencies remain empty in this phase. Add dev dependencies `pytest`, `pytest-cov`, `ruff`, and `mypy`.
- [ ] Set `__version__ = "0.1.0"` and `LOGIC_FRAMES_PER_SECOND = 30`. Configure Ruff for 120 columns and Python 3.11; configure mypy strict mode for `src`.
- [ ] Run `uv lock`, then:

  ```powershell
  uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/test_package.py -q
  uv run --project scripts/replay_analyzer ruff check scripts/replay_analyzer/src scripts/replay_analyzer/tests
  uv run --project scripts/replay_analyzer mypy scripts/replay_analyzer/src
  ```

  Expected: all commands pass.

- [ ] Commit:

  ```powershell
  git add scripts/replay_analyzer/pyproject.toml scripts/replay_analyzer/README.md scripts/replay_analyzer/uv.lock scripts/replay_analyzer/src scripts/replay_analyzer/tests/test_package.py
  git commit -m "build(replay): Add analyzer Python package"
  ```

## Task 3: Add checksum-pinned fixtures and source provenance parsing

**Files:**
- Create: `scripts/replay_analyzer/tests/fixtures/zero_hour_1_04/leex279_vs_fox27.rep`
- Create: `scripts/replay_analyzer/tests/fixtures/zero_hour_1_04/leex279_vs_fox27.expected.json`
- Create: `scripts/replay_analyzer/tests/fixtures/zero_hour_1_04/README.md`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/provenance.py`
- Create: `scripts/replay_analyzer/tests/test_provenance.py`

- [ ] Copy the user-provided replay from `C:\Users\Leex279\Downloads\match_3133811_user_e80b96708aa4254945941fd5f81489bb_replay.rep` into the fixture path without changing bytes. Record its SHA-256 `EA085767BFA11D2CFC167D9007173CE2EB29B5F557702FFD042E2E9A1A8F6BB8`, source filename grammar, embedded-name expectation, and testing purpose in the fixture README.
- [ ] Write failing tests for `extract_source_provenance(path)` and `sha256_file(path)`. Require match ID `3133811` and source user token `e80b96708aa4254945941fd5f81489bb` from the filename, but assert neither field is a player identity.
- [ ] Implement immutable `SourceProvenance` with `original_filename`, optional `strata_match_id`, optional `strata_source_user_token`, and `sha256`. Match only `^match_(\d+)_user_([0-9a-fA-F]+)_replay\.rep$`; other names produce `None` external fields without error.
- [ ] Add a negative binary-content test for the pinned replay: neither ASCII `3133811` nor the raw 16-byte user token may appear in its bytes.
- [ ] Run:

  ```powershell
  uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/test_provenance.py -q
  ```

  Expected: all tests pass.

- [ ] Commit:

  ```powershell
  git add scripts/replay_analyzer/src/generals_replay_analyzer/provenance.py scripts/replay_analyzer/tests/test_provenance.py scripts/replay_analyzer/tests/fixtures/zero_hour_1_04
  git commit -m "test(replay): Add pinned Zero Hour replay fixture"
  ```

## Task 4: Implement bounded binary primitives and typed parser errors

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/binary.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/errors.py`
- Create: `scripts/replay_analyzer/tests/test_binary.py`

- [ ] Write tests first for little-endian signed/unsigned 8-, 16-, and 32-bit reads; IEEE-754 float32; `Coord3D`; `ICoord2D`; `IRegion2D`; length-prefixed ASCII; length-prefixed UTF-16LE; current offset; exact slice capture; and EOF at every byte boundary.
- [ ] Require `ReplayParseError(code, offset, message)` and subclasses `InvalidMagicError`, `TruncatedReplayError`, `UnsupportedArgumentTypeError`, and `InvalidStringLengthError`.
- [ ] Run the tests and confirm import failures.
- [ ] Implement `BinaryReader` over immutable `bytes` or a seekable binary stream. Every method must call one `read_exact(size)` guard; no `struct.unpack` may consume unchecked data.
- [ ] Cap string lengths at 1 MiB of encoded data and report the length-field offset on failure.
- [ ] Run:

  ```powershell
  uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/test_binary.py -q
  uv run --project scripts/replay_analyzer pytest --cov=generals_replay_analyzer.binary --cov-fail-under=95 -q
  ```

  Expected: all tests pass and binary-module coverage is at least 95%.

- [ ] Commit with `feat(replay): Add strict binary reader`.

## Task 5: Parse the Zero Hour replay header and player slots

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/model.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/header.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/game_options.py`
- Create: `scripts/replay_analyzer/tests/test_header.py`
- Modify: `scripts/replay_analyzer/tests/fixtures/zero_hour_1_04/leex279_vs_fox27.expected.json`

- [ ] Transcribe the exact `readReplayHeader()` field order into the expected JSON using the C++ reader output or direct verified values. Include magic, start/end timestamps, frame count, flags, disconnect slots, replay/version strings, version number, EXE/INI CRCs, game options, local player index, header-end offset, map, seed, starting cash, and all slots.
- [ ] Write tests that assert the pinned replay exposes player names `leex279` and `FOX27`, preserves original casing, and does not manufacture a player from the filename token.
- [ ] Add generated minimal fixtures for invalid magic, negative/oversized string length, truncated flags, truncated player slot, and malformed game-options grammar.
- [ ] Implement frozen models `ReplayHeader`, `ReplaySlot`, `ReplayFlags`, and `ParseWarning`. Keep original game-options text alongside parsed fields.
- [ ] Implement slot parsing with explicit tokens and warnings for unknown optional tokens. Do not map a slot to a player using arithmetic such as `player_index - 2`.
- [ ] Run:

  ```powershell
  uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/test_header.py -q
  ```

  Expected: pinned and generated fixture tests pass.

- [ ] Commit with `feat(replay): Parse Zero Hour replay headers`.

## Task 6: Parse command frames, player indices, and every argument type

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/commands.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/parser.py`
- Create: `scripts/replay_analyzer/tests/fixture_builder.py`
- Create: `scripts/replay_analyzer/tests/test_commands.py`
- Create: `scripts/replay_analyzer/tests/test_parser.py`

- [ ] Build minimal replay bytes in tests for all eleven `GameMessageArgumentDataType` values: integer, real, boolean, object ID, drawable ID, team ID, location, pixel, pixel region, timestamp, and wide character.
- [ ] Add failing cases for a zero-type command, repeated argument-type runs, unknown message number with known argument widths, unknown argument type, truncated type run, truncated payload, missing next frame, and trailing bytes.
- [ ] Implement frozen `ReplayCommand` and `ReplayArgument` models with `frame`, `player_index`, numeric `message_type`, optional symbolic name, command-start/end offsets, argument type, typed value, and raw bytes.
- [ ] Implement `parse_replay(path)` returning `ParsedReplay(header, commands, warnings, end_offset, completion_status)`. Use frame values as stored; expose seconds only as `frame / 30.0`.
- [ ] Treat a clean recorder end as complete; classify early EOF as truncated with the last trustworthy offset. Never resynchronize by scanning for plausible frames.
- [ ] Run:

  ```powershell
  uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/test_commands.py scripts/replay_analyzer/tests/test_parser.py -q
  ```

  Expected: all argument and failure-mode tests pass.

- [ ] Commit with `feat(replay): Parse replay command streams`.

## Task 7: Add the generated message catalog and inspect CLI

**Files:**
- Create: `scripts/replay_analyzer/contracts/zero_hour_1_04_message_types.json`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/contracts.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/cli.py`
- Create: `scripts/replay_analyzer/tests/test_contracts.py`
- Create: `scripts/replay_analyzer/tests/test_cli.py`

- [ ] Define a catalog document with `schema_version`, game, patch, engine build, source header path, generated timestamp, and numeric-to-symbolic message map. Mark it generated and do not hand-edit it after C++ generation exists.
- [ ] Write tests requiring numeric types to remain usable when a catalog entry is absent and requiring catalog duplicate numbers/names to fail validation.
- [ ] Write CLI tests for `replay-analyzer inspect <file> --format json`, `--commands`, and malformed input. JSON output must include evidence tier `observed`, SHA-256, parser version, warnings, header, command count, and completion state.
- [ ] Implement the catalog loader and `argparse` CLI. Human output may summarize; JSON output must never omit parser warnings.
- [ ] Run `uv run --project scripts/replay_analyzer replay-analyzer inspect scripts/replay_analyzer/tests/fixtures/zero_hour_1_04/leex279_vs_fox27.rep --format json` and validate it with `ConvertFrom-Json`.
- [ ] Commit with `feat(replay): Add replay inspection CLI`.

## Task 8: Add the authoritative C++ parse-dump observer

**Files:**
- Create: `GeneralsMD/Code/GameEngine/Include/Common/ReplayParseDump.h`
- Create: `GeneralsMD/Code/GameEngine/Source/Common/ReplayParseDump.cpp`
- Modify: `GeneralsMD/Code/GameEngine/CMakeLists.txt`
- Modify: `Core/GameEngine/Source/Common/CommandLine.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp`

- [ ] Add an `if(NOT IS_VS6_BUILD)` CMake block that appends `ReplayParseDump.cpp` and defines `RTS_REPLAY_ANALYZER=1` for `z_gameengine`. Do not add the source or definition to `g_gameengine` yet.
- [ ] Define a process-local `ReplayParseDump` with these guarded methods:

  ```cpp
  static void setOutputPath(const AsciiString &path);
  static Bool isEnabled();
  static Bool beginReplay(const RecorderClass::ReplayHeader &header, Int endOffset);
  static void writeCommand(Int frame, Int startOffset, Int endOffset, const GameMessage &message);
  static void writeMessageCatalog();
  static void finishReplay(Int endOffset, Bool complete);
  ```

- [ ] Serialize one UTF-8 NDJSON object per line with explicit JSON escaping. Encode float values both as decimal and raw 32-bit hex so parity is bit-verifiable. Include numeric and symbolic message types, argument type numbers/names, typed values, offsets, and raw scalar bits.
- [ ] Add startup option `-replay-parse-dump <absolute-output.ndjson>` under `#if defined(RTS_REPLAY_ANALYZER)`. It only configures the sink; the existing `-replay` option selects input.
- [ ] In `readReplayHeader()`, emit the complete decoded header only after successful parsing. In `appendNextCommand()`, capture `m_file->seek(0, File::CURRENT)` before type read and after the last argument, then call `writeCommand()` before the message can be deleted. In `stopPlayback()`, emit completion before closing the sink.
- [ ] Add `TheSuperHackers @feature Leex 18/08/2026` comments at the CMake/command-line/Recorder architecture seams.
- [ ] Build:

  ```powershell
  cmake --build build/win32 --target z_generals --config Release
  ```

  Expected: `build\win32\GeneralsMD\Release\generalszh.exe` builds successfully.

- [ ] Run the pinned fixture:

  ```powershell
  New-Item -ItemType Directory -Force .tmp\replay-parity
  & .\build\win32\GeneralsMD\Release\generalszh.exe -headless -replay "C:\Users\Leex279\Downloads\match_3133811_user_e80b96708aa4254945941fd5f81489bb_replay.rep" -replay-parse-dump "$pwd\.tmp\replay-parity\cpp.ndjson"
  Get-Content .tmp\replay-parity\cpp.ndjson | ForEach-Object { $_ | ConvertFrom-Json | Out-Null }
  ```

  Expected: exit code 0; first record `header`, last record `complete`, all lines valid JSON.

- [ ] Configure and build VC6:

  ```powershell
  cmake --preset vc6
  cmake --build build/vc6
  ```

  Expected: the build succeeds and contains no `ReplayParseDump.cpp` compilation step.

- [ ] Commit with `feat(replay): Add authoritative replay parse dump`.

## Task 9: Enforce Python-to-C++ parity

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/parity.py`
- Create: `scripts/replay_analyzer/tests/test_parity.py`
- Create: `scripts/replay_analyzer/tests/engine/test_cpp_parity.py`
- Create: `scripts/replay_analyzer/tests/engine/conftest.py`
- Modify: `scripts/replay_analyzer/pyproject.toml`
- Modify: `scripts/replay_analyzer/contracts/zero_hour_1_04_message_types.json`

- [ ] Register pytest marker `engine`. Unit parity tests use a checked-in small NDJSON sample; engine tests launch `build\win32\GeneralsMD\Release\generalszh.exe` and write only to pytest's temporary directory.
- [ ] Compare header values, original player slot strings, command count, frame, player index, numeric message type, argument type, integer/scalar bits, coordinates, each command end offset, and final stream offset. Report the first mismatch with replay byte offset and both values.
- [ ] Generate `zero_hour_1_04_message_types.json` from the C++ catalog record, then re-run catalog validation. Do not scrape `MessageStream.h` with a regex.
- [ ] Run:

  ```powershell
  uv run --project scripts/replay_analyzer pytest -m "not engine" -q
  uv run --project scripts/replay_analyzer pytest -m engine -q
  ```

  Expected: both suites pass against the pinned fixture.

- [ ] Commit with `test(replay): Enforce C++ parser parity`.

## Task 10: Quarantine synthetic modules and archive generated artifacts safely

**Files:**
- Create: `scripts/replay_analyzer/legacy_prototype/README.md`
- Create: `scripts/replay_analyzer/tools/archive_legacy_outputs.ps1`
- Move: existing prototype modules from `scripts/replay_analyzer/*.py` except the new package files into `scripts/replay_analyzer/legacy_prototype/`
- Move: `scripts/replay_player/` into `scripts/replay_analyzer/legacy_prototype/replay_player/`
- Modify: `docs/replay-analyzer/legacy-audit.md`

- [ ] Write the archival script with mandatory `-InventoryPath`, `-Destination`, and explicit `-Apply`. Without `-Apply`, it prints planned moves only. With `-Apply`, verify each source hash against inventory, create the destination, move only untracked files listed in the inventory, and write an archive manifest. Refuse tracked files and hash mismatches.
- [ ] Move prototype source with `git mv` where tracked; move untracked prototype files normally. Add a README stating that the directory is historical, non-factual, excluded from packaging, and scheduled for deletion only after any reusable rendering utilities are separately revalidated.
- [ ] Run all Python tests to prove no production import reaches `legacy_prototype`.
- [ ] Dry-run artifact archival to `%LOCALAPPDATA%\GeneralsReplayAnalyzer\legacy-artifacts\2026-08-18` and review the exact list before applying it.
- [ ] Apply the archive only after the dry-run list and hashes match `legacy-audit.md`; then confirm repository-root generated media count is zero and the archive manifest count equals the inventory count selected for movement.
- [ ] Commit source moves, audit changes, and the script with `refactor(replay): Quarantine synthetic replay prototype`. Do not commit archived media or inventory output.

## Foundation Acceptance Gate

- [ ] `uv run --project scripts/replay_analyzer pytest -m "not engine" --cov=generals_replay_analyzer --cov-fail-under=90 -q` passes.
- [ ] `uv run --project scripts/replay_analyzer pytest -m engine -q` passes against the pinned real replay.
- [ ] Ruff and strict mypy pass.
- [ ] Modern Zero Hour Release builds.
- [ ] VC6 builds without replay-analyzer exporter sources.
- [ ] The Python and C++ readers agree on every supported header field, player slot, command, argument, and byte offset.
- [ ] The parser reports `leex279` and `FOX27`; provenance separately reports Strata match `3133811` and source token; no external identifier is promoted to player identity.
- [ ] No production import references `legacy_prototype`, and no synthetic map/unit/combat value appears in parser output.
- [ ] Push the completed foundation commits to `origin/feat/replay-analyzer-v2` before starting the telemetry plan.
