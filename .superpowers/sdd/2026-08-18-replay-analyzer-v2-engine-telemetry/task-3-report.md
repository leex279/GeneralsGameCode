# Task 3 Report: Player Initialization and Semantic Game-Data Catalog

## Status

PASS for the modern Zero Hour analyzer target. The real pinned replay emits telemetry v2 only after map overrides and player resolution are complete, with one authoritative eight-slot `players_initialized` event. The manifest and event bind one deterministic content-addressed catalog, the catalog validates against its versioned JSON Schema, and telemetry remains passive and non-interfering.

Task 3 implementation base: `18f4114a5` (`fix(replay): Enable no-audio analyzer playback`). The prerequisite is intentionally separate from the feature commit.

## Fix Round 1: Authority and Contract Migration

Review identified that the first Task 3 implementation prepared the catalog from `ReplayTelemetry::begin`, before `GameLogic::loadMapINI`, solo-map INI `CREATE_OVERRIDES`, and resolved players were authoritative. It also silently omitted open/unresolved replay slots, redefined telemetry v1, flattened weapon sets to a name union, and let the Python reader yield a v2 manifest without proving the referenced catalog.

The correction introduces a two-phase transaction. Header decode exclusively opens a pending trace and stores immutable header facts; it writes no record and creates no catalog. `RecorderClass::initControls` calls `ReplayTelemetry::initialize` only after map/player initialization. That phase builds the eight-slot snapshot first, serializes already-loaded final metadata, publishes the content-addressed catalog, writes and flushes the manifest, and only then emits the single player event. Any failure before initialization closes and removes the owned temporary trace without publishing a final trace.

Telemetry v1 is restored byte-for-byte to the `628e26e84` contract and remains readable, including historical traces with no player event and historical player payloads whose catalog reference lived only on the player event. Telemetry v2 is a new packaged schema: its manifest requires the strict catalog reference and parsed `audio_enabled` provenance; it requires exactly one closed eight-slot player snapshot. The Python reader selects validation by record major version, buffers the complete trace, and for v2 validates the referenced safe relative basename, exact bytes and SHA-256, strict UTF-8/JSON, catalog schema, catalog engine identity, manifest reference, and player reference before returning any iterator record.

Each of all eight replay slots records `slot_index`, replay slot state, occupied/resolution state, nullable resolved engine fields, replay-header local-slot provenance, and separately the resolved local-player-pointer result. Occupied engine player mappings must be unique; open and closed slots are explicit rather than skipped. Synthetic strict fixtures cover human, AI, open, closed, unresolved, duplicate, reordered, and contradictory cases; the pinned replay provides the real two-human resolved case.

Thing templates now retain ordered weapon sets rather than only a flattened name list. Each set records the raw condition mask and stable condition names, shared reload and cross-set lock flags, and all three weapon slots with weapon identity, raw auto-choice mask plus stable command-source names, and preferred-against kind-of names. `derived_weapon_names` is explicitly a derived convenience union; the top-level weapon collection remains explicitly scoped to names referenced by loaded thing templates.

All catalog/player `Real` values pass through a finite-number guard before JSON serialization. A deterministic analyzer-only fault seam proves both metadata and waypoint nonfinite values fail closed with no trace, catalog, or temporary residue. Python JSON parsing rejects non-standard `NaN` and `Infinity` constants in both traces and catalog assets.

No tracked custom-map replay or `.map` fixture exists (`git ls-files "*.rep"` returns only the pinned retail replay and `git ls-files "*.map"` is empty), so the strongest available evidence is the real engine replay plus the static lifecycle assertion that catalog preparation is absent from `begin` and initialization occurs at `initControls`.

### Fix-Round RED Evidence

Tests were introduced before each correction. The recorded RED groups were:

```text
historical v1 / new v2 selection: 2 failed
v2 atomic asset validation and exact player event count: 8 failed
eight-slot ordering/resolution invariants: 5 failed
structured weapon-set catalog schema: 1 failed
real-engine lifecycle/catalog/nonfinite/audio/pre-init tests: 6 failed
installed-wheel old fake/missing catalog smoke: 1 failed
explicit modern analyzer / non-VC6 translation-unit guard: 1 failed
```

The real-engine RED proved `prepareCatalog` was still called by `begin`, the emitted manifest remained version 1, the player snapshot skipped unused slots, the catalog still emitted `weapon_names`, injected nonfinite values could publish output, and a pre-initialization replay failure left a published trace. The wheel RED proved the old smoke used a fake v1 catalog reference with no asset.

## Fix Round 2: Terminal Totals and Exponent Overflow

Two remaining atomic-reader findings were reproduced before implementation:

```text
v1 missing/wrong/extra/zero event-count maps: 4 failed
v2 missing/wrong/extra/zero event-count maps: 4 failed
player start-position exponent overflow 1e999/-1e999: 2 failed
hash-valid catalog build-time exponent overflow 1e999: 1 failed
11 failed in 0.41s
```

The count fixtures modify only the terminal `event_counts` map, so the existing pre-completion `trace_sha256` remains valid. The writer source defines the authoritative semantics: increment every emitted event, set manifest to one, increment `complete` before serializing its payload, and serialize only keys that occurred. The reader now independently recomputes those exact positive totals from every buffered validated record, including `manifest` and `complete`, and requires exact key/value equality. Missing, wrong, unknown/extra, and zero-only keys all fail at `complete.payload.event_counts` before an iterator can expose the manifest. The real-engine suite passes unchanged, proving no C++ count mismatch.

Python's JSON decoder invokes `parse_float` for legal exponent syntax and silently converts `1e999` to infinity; `parse_constant` covers only literal `NaN`/`Infinity`. Trace and catalog decoding now share a finite `parse_float` hook that rejects positive and negative exponent overflow before JSON Schema or Pydantic. Existing literal-constant rejection remains. Focused acceptances cover `1e308`, `-1e308`, and underflowing `1e-999`, so valid finite/underflowed JSON numbers remain readable.

Fix-round-2 verification:

```text
focused count/overflow/boundary tests: 16 passed in 0.58s
complete telemetry contracts: 56 passed in 0.71s
installed wheel: 1 passed in 6.75s
all non-engine: 290 passed, 29 deselected in 20.06s
all real engine: 28 passed, 1 skipped, 290 deselected in 44.15s
Ruff: All checks passed
strict mypy: Success, 15 source files
```

No C++ file changed in fix round 2, so the modern x86 build was explicitly not rerun. The unchanged executable is the same modern Release artifact exercised by all 28 passing real-engine tests. VC6/MinGW status is likewise unchanged and irrelevant to this Python-only delta.

## Exact RED Evidence

The real-engine integration test was written before the exporter and run against the rebuilt Task 2 implementation:

```text
uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/engine/test_game_data_catalog.py::test_replay_emits_one_resolved_player_event_and_a_strict_catalog_asset -q
E       assert 0 == 1
E        +  where 0 = len([])
1 failed
```

The trace contained no `players_initialized` record. The companion schema-first run also failed before production changes because the Task 2 manifest could not satisfy the new required catalog identity:

```text
uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/telemetry/test_schema.py -q
22 failed, 2 passed
```

Before these new tests, the unchanged Task 1 schema baseline was green at 20 passed. This distinguishes the new contract RED from a pre-existing schema failure.

## Release No-Audio Prerequisite and Crash Evidence

The first Release real-engine run terminated with `0xC0000005` during initialization. Task 3 C++ code was disconnected, then the exact Task 2 source and a fresh build tree were tested; both still failed. Disabling the analyzer definition also failed, disproving a broad Task 3 layout or analyzer ABI hypothesis.

The minidump `CrashMZ-20260820-002019-628e26e84-pid66348.dmp` reported an execute access violation at unmapped address `0x0001032c`. The stack returned through Steam retail `mss32.dll+0x109d8` to `AIL_quick_handles` and `MilesAudioManager::openDevice`. The loaded retail DLL SHA-256 was `441B290E7DC6334EB5023CD9B7937739298FDD66C104D4C96E5EDCF642AE912D`; the executable had linked the build-tree Miles import library, while normal Windows application-directory loading correctly selected the retail DLL beside the hardlinked executable.

The root cause was narrower: Release command-line tables exposed `-noaudio` only under `RTS_DEBUG`, so modern Release analyzer tests silently ignored the explicit switch and entered Miles. The guarded parser fix and a focused real-engine regression were committed separately:

```text
18f4114a5 fix(replay): Enable no-audio analyzer playback
1 passed in 1.57s
```

The fix is limited to `RTS_REPLAY_ANALYZER && !IS_VS6_BUILD`; it does not broaden `-noaudio` for retail, VC6, or base Generals.

## Files Changed

- `GeneralsMD/Code/GameEngine/Include/Common/ReplayGameDataExport.h`
- `GeneralsMD/Code/GameEngine/Source/Common/ReplayGameDataExport.cpp`
- `GeneralsMD/Code/GameEngine/CMakeLists.txt`
- `GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp`
- `GeneralsMD/Code/GameEngine/Include/Common/ReplayTelemetry.h`
- `GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp`
- `GeneralsMD/Code/GameEngine/Include/Common/ProductionPrerequisite.h`
- `GeneralsMD/Code/GameEngine/Include/Common/ThingTemplate.h`
- `GeneralsMD/Code/GameEngine/Include/GameLogic/Locomotor.h`
- `scripts/replay_analyzer/contracts/game-data-catalog-v1.schema.json`
- `scripts/replay_analyzer/contracts/telemetry-v1.schema.json`
- `scripts/replay_analyzer/contracts/telemetry-v2.schema.json`
- `scripts/replay_analyzer/pyproject.toml`
- `scripts/replay_analyzer/src/generals_replay_analyzer/telemetry/model.py`
- `scripts/replay_analyzer/src/generals_replay_analyzer/telemetry/reader.py`
- `scripts/replay_analyzer/tests/engine/conftest.py`
- `scripts/replay_analyzer/tests/engine/test_cpp_parity.py`
- `scripts/replay_analyzer/tests/engine/test_game_data_catalog.py`
- `scripts/replay_analyzer/tests/engine/test_telemetry_envelope.py`
- `scripts/replay_analyzer/tests/telemetry/test_schema.py`
- `scripts/replay_analyzer/tests/telemetry/test_game_data_catalog_schema.py`
- `scripts/replay_analyzer/tests/telemetry/test_telemetry_v2_contract.py`
- `scripts/replay_analyzer/tests/test_wheel.py`
- this report

The schema/model/reader extensions preserve historical telemetry v1 and introduce telemetry v2 for the mandatory catalog/player contract. In v2, type, safe content-addressed basename, SHA-256, and engine-data identity are required; the filename must embed the same SHA; manifest engine identity must equal `engine_build`; and the player event reference must equal the manifest reference. Both telemetry schemas and the catalog schema are included in the wheel.

## Design and Authoritative Sources

- `ReplayTelemetry::begin` creates only its exclusive pending trace transaction and stores decoded replay identity. It emits no observation and performs no catalog discovery. A replay that never reaches initialized state discards that pending transaction.
- `RecorderClass::initControls` is the established post-GameLogic-start seam. At that point map INIs/final overrides and replay players are resolved. `ReplayTelemetry::initialize` builds player/catalog evidence, writes and flushes the manifest first, and then emits exactly one player record.
- Every one of `MAX_SLOTS` exports its stable slot index and replay state. Occupied slots additionally expose replay name/team, actual `Player::getPlayerIndex` when resolved, resolved `PlayerTemplate` name, color or null, waypoint-derived start position or explicit unknown, human/AI state, header-local provenance, and resolved-local-player provenance. Open/closed and unresolved fields remain explicit null/not-applicable rather than guessed.
- The catalog iterates already-loaded `ThingFactory` templates, follows their final overrides, deduplicates by stable template name, and sorts names before assigning ordinals. It does not instantiate a module, `Locomotor`, store, `Object`, or other gameplay state.
- Template fields come directly from loaded metadata: stable template name, default owning side, `KindOfMaskType` names, raw configured build cost, accurately named raw `configured_build_time_seconds`, resolved prerequisite template/science names, loaded AI module locomotor-set references, `isBuildFacility`, structured weapon-set metadata, and category tags derived only from those flags/capabilities.
- Upgrade and science names come from their loaded stores. Weapon scope is explicitly `referenced_by_thing_templates`; locomotor scope is explicitly `referenced_by_thing_templates`. This avoids claiming global enumeration where the engine exposes no safe iterator.
- The only new metadata accessors are guarded, inline, const views of configured build time, resolved prerequisite vectors, and loaded locomotor name/surface mask. They expose no mutator and perform no resolution or allocation.
- No numeric object ID is used as a semantic category. Replay-local IDs remain observation identities in the unchanged later-event contract.

## Catalog Transaction and Identity

Catalog JSON is deterministic compact UTF-8 with a trailing newline. Top-level collections are sorted by stable name and receive contiguous ordinal positions. Nested sets are sorted or retain authoritative enum/configuration order.

The SHA-256 of the exact catalog bytes determines the relative filename:

```text
game-data-catalog-v1-<64 lowercase hex SHA-256>.json
```

The file is confined to the configured telemetry output directory. Existing identical bytes are reused. New bytes are written to an exclusive same-directory transaction and published with no replacement. A pre-existing or racing destination with unrelated bytes is preserved and produces an explicit telemetry diagnostic; the trace transaction is not published and temporary files are removed.

## GREEN Verification

Focused fix-round real-engine behavior:

```text
post-map lifecycle + catalog + two nonfinite paths + noaudio provenance + pre-init cleanup
6 passed in 8.60s
```

The focused tests prove:

- one manifest and exactly one resolved player event from the real pinned replay;
- strict version-selected reader validation plus catalog JSON Schema, UTF-8 decode, SHA/path/type/engine identity, stable ordinals, structured weapon sets, populated semantic collections, and absence of numeric template/object category IDs;
- byte-identical catalog content/reference and identical normalized complete trace records across two distinct run IDs/output envelopes; only `run_id` and the consequent terminal `trace_sha256` are normalized;
- unrelated collision bytes survive unchanged, no trace is published, and no owned transaction remains.
- nonfinite catalog/player values and a replay that fails before initialization publish neither trace nor catalog;
- the parsed manifest records `audio_enabled: false` under `-noaudio`.

Focused telemetry v1/v2 and catalog-schema gate after cross-field hardening:

```text
40 passed in 0.40s
```

Full final real-engine suite after the final build:

```text
uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests -m engine -q
28 passed, 1 skipped, 274 deselected in 44.73s
```

The skip is the pre-existing Windows symlink-alias case when this host denies symlink creation. Hardlink collision coverage remains green.

The engine harness now centralizes one uniquely named temporary hardlink beside Steam Zero Hour data, refuses replacement, verifies source/runtime file identity before cleanup, and removes only the same hardlink in `finally`. The existing parity test was changed to use this fixture after a deterministic launch-boundary investigation proved that the build executable returns 1 before replay parsing when run outside game data, while the identical hardlink produces the 1,144,727-byte authoritative parse dump.

Non-engine and quality gates:

```text
uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests -m "not engine" -q
274 passed, 29 deselected in 21.31s

uv run --project scripts/replay_analyzer ruff check scripts/replay_analyzer/src scripts/replay_analyzer/tests
All checks passed!

uv run --project scripts/replay_analyzer mypy --strict scripts/replay_analyzer/src
Success: no issues found in 15 source files

uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/test_wheel.py -q
1 passed in 6.59s
```

Final modern x86 Release build:

```text
cmd.exe /d /c "call C:\PROGRA~2\MICROS~2\2022\BUILDT~1\Common7\Tools\VsDevCmd.bat -arch=x86 -host_arch=x86 >nul && cmake --build build\win32 --target z_generals --config Release -- -j 4"
[4/8] Building ... ReplayTelemetry.cpp.obj
[6/8] Building ... ReplayGameDataExport.cpp.obj
[7/8] Linking ... z_gameengine.lib
[8/8] Linking ... generalszh.exe
Exit code: 0
```

The only build diagnostics were the two pre-existing signed/unsigned C4018 warnings in `ReplaySimulation.cpp` lines 165 and 187.

`git diff --check` passed after the final source and report edits.

## Real Trace and Non-Interference

The pinned retail replay trace validates through the version-selecting `iter_validated_trace` as `manifest`, `players_initialized`, `complete`. It contains exactly eight ordered slots: two occupied humans with stable faction templates, colors, resolved map starts, unique engine player indices, one header-local slot and one resolved local player, plus six explicit open/closed slots. Completion remains at the existing CRC boundary (frame 108, 16 executed commands), and `event_counts` contains exactly one manifest, one player event, and one complete event.

The existing real-engine non-interference test compares telemetry disabled/enabled runs and remains green: return code, stdout, and stderr are identical. Catalog/export calls return no gameplay value, consult no random or UI state, and cannot modify GameLogic, command execution, CRC decisions, replay termination, or normal diagnostics. Only explicit telemetry/catalog failure diagnostics are added on output failure paths.

## VC6 and MinGW Evidence

Neither optional toolchain exists on this host, so no actual VC6 or MinGW result is claimed:

```text
i686-w64-mingw32-g++.exe: absent
mingw32-g++.exe: absent
VC98\Bin\CL.EXE: absent
build/mingw-w64-i686: CMAKE_MAKE_PROGRAM-NOTFOUND
build/vc6: CMAKE_C_COMPILER-NOTFOUND; CMAKE_CXX_COMPILER-NOTFOUND
```

Static exclusion is explicit:

- catalog sources are added only inside `if(NOT IS_VS6_BUILD)`;
- `RTS_REPLAY_ANALYZER=1` is defined only in the same modern block;
- exporter/accessor declarations use `RTS_REPLAY_ANALYZER && !IS_VS6_BUILD`;
- Recorder calls are guarded and have void/passive semantics;
- no `Generals/` base-game file changed.

MinGW remains an honest environmental cannot-verify. The exporter uses standard C++ and Win32 transaction primitives already used by the modern Windows writer, but no MinGW pass is asserted without the compiler and build program.

## Self-Review

- Confirmed catalog discovery reads only authoritative loaded metadata and allocates no temporary gameplay modules or stores.
- Confirmed player facts come from replay `GameInfo`, resolved engine `Player`/`PlayerTemplate`, loaded terrain waypoints, and the local-player pointer; unavailable values are explicit null/unknown rather than guessed.
- Confirmed content bytes contain no run ID, trace path, timestamp, temporary filename, object ID, RNG value, locale-dependent float formatting, or filesystem state.
- Confirmed catalog and trace publication never replace unrelated files and clean only transactions they own.
- Confirmed all Task 3 architectural C++ comments use the mandated `// TheSuperHackers @feature Leex 18/08/2026 ... (#TBD)` form.
- Confirmed the separate no-audio prerequisite uses the mandated `@bugfix` comment dated 20/08/2026.
- Confirmed no temporary debug probes remain and no base Generals file is modified.

## Commit Subjects

`feat(replay): Export semantic game data catalog`

`fix(replay): Make semantic catalog authoritative`

`fix(replay): Verify telemetry evidence totals`
