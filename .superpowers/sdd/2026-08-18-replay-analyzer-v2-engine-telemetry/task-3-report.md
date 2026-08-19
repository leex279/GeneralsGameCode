# Task 3 Report: Player Initialization and Semantic Game-Data Catalog

## Status

PASS for the modern Zero Hour analyzer target. The real pinned replay emits exactly one authoritative `players_initialized` event, the manifest and event bind one deterministic content-addressed catalog, the catalog validates against its versioned JSON Schema, and telemetry remains passive and non-interfering.

Task 3 implementation base: `18f4114a5` (`fix(replay): Enable no-audio analyzer playback`). The prerequisite is intentionally separate from the feature commit.

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
- `scripts/replay_analyzer/pyproject.toml`
- `scripts/replay_analyzer/src/generals_replay_analyzer/telemetry/model.py`
- `scripts/replay_analyzer/src/generals_replay_analyzer/telemetry/reader.py`
- `scripts/replay_analyzer/tests/engine/conftest.py`
- `scripts/replay_analyzer/tests/engine/test_cpp_parity.py`
- `scripts/replay_analyzer/tests/engine/test_game_data_catalog.py`
- `scripts/replay_analyzer/tests/engine/test_telemetry_envelope.py`
- `scripts/replay_analyzer/tests/telemetry/test_schema.py`
- `scripts/replay_analyzer/tests/test_wheel.py`
- this report

The schema/model/reader extensions keep telemetry major version 1 and make the newly required asset reference strict: type, safe content-addressed basename, SHA-256, and engine-data identity are required; the filename must embed the same SHA; manifest engine identity must equal `engine_build`; and the player event reference must equal the manifest reference. The catalog schema is included in the wheel.

## Design and Authoritative Sources

- `ReplayTelemetry::begin` creates its exclusive trace transaction, establishes the engine-data identity from version/executable/INI CRC metadata, prepares the required catalog, and only then writes the manifest. Catalog failure closes and discards the owned trace transaction without affecting replay control flow.
- `RecorderClass::initControls` is the established post-GameLogic-start seam. At that point replay slots have resolved to `Player` objects, player templates, the local player, map waypoints, and start positions. A process-local guard emits only one player record.
- Each occupied slot exports its replay name, actual `Player::getPlayerIndex`, replay team number, resolved `PlayerTemplate` name, color or null, waypoint-derived start position or explicit unknown, human/AI controller state, and local-player flag. The schema rejects contradictory resolved/null and controller/boolean combinations.
- The catalog iterates already-loaded `ThingFactory` templates, follows their final overrides, deduplicates by stable template name, and sorts names before assigning ordinals. It does not instantiate a module, `Locomotor`, store, `Object`, or other gameplay state.
- Template fields come directly from loaded metadata: stable template name, default owning side, `KindOfMaskType` names, raw configured build cost, accurately named raw `configured_build_time_seconds`, resolved prerequisite template/science names, loaded AI module locomotor-set references, `isBuildFacility`, referenced weapon-template names, and category tags derived only from those flags/capabilities.
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

Focused Task 3 real-engine behavior:

```text
uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/engine/test_game_data_catalog.py -q
3 passed in 5.91s
```

The three tests prove:

- one manifest and exactly one resolved player event from the real pinned replay;
- strict Task 1 reader validation plus catalog JSON Schema, UTF-8 decode, SHA/path/type/engine identity, stable ordinals, populated semantic collections, and absence of numeric template/object category IDs;
- byte-identical catalog content and identical content reference across two distinct run IDs/output envelopes;
- unrelated collision bytes survive unchanged, no trace is published, and no owned transaction remains.

Focused strict schema/runtime gate after cross-field hardening:

```text
33 passed in 8.09s
```

Full final real-engine suite after the final build:

```text
uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/engine -q
24 passed, 1 skipped in 33.78s
```

The skip is the pre-existing Windows symlink-alias case when this host denies symlink creation. Hardlink collision coverage remains green.

The engine harness now centralizes one uniquely named temporary hardlink beside Steam Zero Hour data, refuses replacement, verifies source/runtime file identity before cleanup, and removes only the same hardlink in `finally`. The existing parity test was changed to use this fixture after a deterministic launch-boundary investigation proved that the build executable returns 1 before replay parsing when run outside game data, while the identical hardlink produces the 1,144,727-byte authoritative parse dump.

Non-engine and quality gates:

```text
uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests -m "not engine" -q
263 passed, 25 deselected in 21.39s

uv run --project . ruff check src tests
All checks passed!

uv run --project . mypy --strict src
Success: no issues found in 15 source files
```

Final modern x86 Release build:

```text
cmd.exe /d /c "call C:\PROGRA~2\MICROS~2\2022\BUILDT~1\Common7\Tools\VsDevCmd.bat -arch=x86 -host_arch=x86 >nul && cmake --build build\win32 --target z_generals --config Release -- -j 4"
[2/4] Building ... ReplayGameDataExport.cpp.obj
[3/4] Linking ... z_gameengine.lib
[4/4] Linking ... generalszh.exe
Exit code: 0
```

`git diff --check` passed after the final source and report edits.

## Real Trace and Non-Interference

The pinned retail replay trace validates through Task 1 `iter_validated_trace` as `manifest`, `players_initialized`, `complete`. It contains two occupied human slots with stable faction templates, colors, resolved map starts, unique engine player indices, and exactly one local player. Completion remains at the existing CRC boundary (frame 108, 16 executed commands), and `event_counts` contains exactly one manifest, one player event, and one complete event.

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

## Commit Subject

`feat(replay): Export semantic game data catalog`
