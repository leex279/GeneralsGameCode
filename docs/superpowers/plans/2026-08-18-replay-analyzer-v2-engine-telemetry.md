# Replay Analyzer V2 Engine Telemetry Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export versioned, passive, authoritative Zero Hour GameLogic and map observations for replay analysis without changing replay execution, CRC results, or VC6 builds.

**Architecture:** Extend the modern-only replay-analyzer seam from the foundation plan with an NDJSON `ReplayTelemetry` sink and a zlib-compressed map sidecar exporter. Central GameLogic lifecycle seams emit immutable events; a frame sampler observes moving entities at a bounded interval. A Python runner launches headless playback into an isolated run directory and accepts output only after schema, checksum, sequence, and completion validation.

**Tech Stack:** Modern-only C++20 exporter, engine C++ interfaces, zlib, Python 3.11, Pydantic 2, pytest, JSON Schema, NDJSON.

**Spec:** `docs/superpowers/specs/2026-08-18-replay-analyzer-v2-design.md` sections 5, 8, 9, 16, 17, and 18.

## Global Constraints

- Depend on the completed foundation acceptance gate and reuse `RTS_REPLAY_ANALYZER`; do not create a second build flag.
- Telemetry methods are one-way observers. Return values are ignored and must never affect a branch in GameLogic.
- Sequence numbers are monotonic within one run. `(run_id, sequence)` is the immutable evidence identity.
- Record raw engine coordinates and identifiers. Normalization and player-centric transforms belong to derived Python features.
- File errors set exporter status and diagnostics, but never alter simulation decisions or random state.
- Do not use GameClient/UI/audio state to classify simulation events.
- Emit events only during replay playback with `-telemetry`; normal games and telemetry-disabled replays take the existing path.
- Instrument Zero Hour only. Shared `Core` files may contain guarded call sites, but Generals must compile with those calls removed.

---

## Task 1: Define and validate telemetry schema version 1

**Files:**
- Create: `scripts/replay_analyzer/contracts/telemetry-v1.schema.json`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/telemetry/model.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/telemetry/reader.py`
- Create: `scripts/replay_analyzer/tests/telemetry/test_schema.py`
- Modify: `scripts/replay_analyzer/pyproject.toml`

- [ ] Add runtime dependencies `pydantic>=2.11,<3` and `jsonschema>=4.25,<5`.
- [ ] Write failing tests for the common envelope: `schema_version`, UUID `run_id`, strictly increasing non-negative `sequence`, non-negative `frame`, `logic_time_seconds == frame / 30.0`, `event_type`, and object `payload`.
- [ ] Define required records `manifest` first and `complete` last. The completion payload contains final frame, command count, event counts, CRC mismatch state, replay truncation state, clean shutdown, writer error, trace SHA-256 excluding its own completion line, and map asset references.
- [ ] Define payload schemas for every event family named in later tasks. Set `additionalProperties: true` inside versioned payloads for forward compatibility, but `additionalProperties: false` on the envelope and manifest.
- [ ] Implement Pydantic discriminated models and `iter_validated_trace(path)`. Reject blank lines, invalid UTF-8, duplicate/decreasing sequences, time/frame disagreement, unknown schema major versions, records after completion, and incomplete traces.
- [ ] Run `uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/telemetry/test_schema.py -q`.
- [ ] Commit with `feat(replay): Define telemetry schema`.

## Task 2: Implement the passive NDJSON writer and command-line activation

**Files:**
- Create: `GeneralsMD/Code/GameEngine/Include/Common/ReplayTelemetry.h`
- Create: `GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp`
- Modify: `GeneralsMD/Code/GameEngine/CMakeLists.txt`
- Modify: `Core/GameEngine/Source/Common/CommandLine.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp`
- Create: `scripts/replay_analyzer/tests/engine/test_telemetry_envelope.py`

- [ ] Add `ReplayTelemetry.cpp` inside the existing `if(NOT IS_VS6_BUILD)` analyzer source block.
- [ ] Implement one process-local sink with:

  ```cpp
  static void configure(const AsciiString &tracePath, const AsciiString &runId, Int movementSampleFrames);
  static Bool isEnabled();
  static void begin(const RecorderClass::ReplayHeader &header);
  static void emit(UnsignedInt frame, const char *eventType, const AsciiString &payloadJson);
  static void finish(UnsignedInt finalFrame, Bool cleanShutdown);
  static void fail(const char *code, const char *message);
  ```

- [ ] Make `emit` assign sequence and `logic_time_seconds`; centralize UTF-8 JSON escaping and SHA-256 updates. Buffer output, flush manifest immediately, and flush at least every 256 events and at completion.
- [ ] Add startup options `-telemetry <absolute-trace.ndjson>`, `-telemetry-run-id <uuid>`, and `-telemetry-movement-frames <positive-int>`. Require `-telemetry` only in combination with headless `-replay`; invalid combinations print an error and exit non-zero before playback.
- [ ] Start the sink after `readReplayHeader()` succeeds and finish it from the same replay termination path that owns final replay/CRC status. An abnormal writer failure must still produce stderr diagnostics.
- [ ] Add the mandatory `TheSuperHackers @feature Leex 18/08/2026` comments.
- [ ] Build Zero Hour and run the pinned replay with `-telemetry-movement-frames 15`. Validate the resulting trace with the Python reader.
- [ ] Build VC6 and confirm neither telemetry source nor CLI strings are present.
- [ ] Commit with `feat(replay): Add passive telemetry writer`.

## Task 3: Export player initialization and a semantic game-data catalog

**Files:**
- Create: `GeneralsMD/Code/GameEngine/Include/Common/ReplayGameDataExport.h`
- Create: `GeneralsMD/Code/GameEngine/Source/Common/ReplayGameDataExport.cpp`
- Modify: `GeneralsMD/Code/GameEngine/CMakeLists.txt`
- Modify: `GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp`
- Create: `scripts/replay_analyzer/contracts/game-data-catalog-v1.schema.json`
- Create: `scripts/replay_analyzer/tests/engine/test_game_data_catalog.py`

- [ ] Write the integration test first. Require one `players_initialized` event and a catalog asset whose SHA-256 matches the manifest reference.
- [ ] Export each player slot with replay name, engine player index, team, faction/template name, color, start position when resolved, human/AI state, and local-player flag.
- [ ] Export one content-addressed UTF-8 JSON catalog per engine data identity. For each relevant `ThingTemplate`, include stable template name, faction, kind-of flags, build cost, build time, prerequisites, locomotor set names, production capability, weapon names, and category tags derived directly from engine metadata. Export upgrades, sciences, weapons, and locomotor surface capabilities by stable name.
- [ ] Do not use numeric object IDs as semantic unit categories. Replay-local object IDs remain event identities only.
- [ ] Validate the asset against `game-data-catalog-v1.schema.json`, hash it, and add it to the telemetry manifest.
- [ ] Commit with `feat(replay): Export semantic game data catalog`.

## Task 4: Emit object lifecycle, ownership, and construction observations

**Files:**
- Modify: `GeneralsMD/Code/GameEngine/Source/GameLogic/System/GameLogic.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Source/GameLogic/Object/Object.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Source/GameLogic/Object/Body/ActiveBody.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Source/GameLogic/Object/Update/ProductionUpdate.cpp`
- Create: `scripts/replay_analyzer/tests/engine/test_entity_lifecycle.py`

- [ ] Write an engine test requiring every emitted entity ID to have exactly one creation event before ownership, construction, damage, position, or destruction events reference it.
- [ ] Emit `object_created` from the centralized object registration path after object ID, template, team, and initial status are valid. Payload includes template name, owner index, team ID, initial raw position, orientation, kind-of flags, and creation source when authoritative.
- [ ] Emit `construction_started`, `construction_completed`, `owner_changed`, `sold`, and `object_destroyed` only at state-transition seams. Include previous/new state and relevant responsible object/player IDs where available.
- [ ] Ensure map-loaded neutral objects are distinguished from player-created objects with an observed creation context, not a guessed category.
- [ ] Add comments at each instrumentation seam and keep calls behind `#if defined(RTS_REPLAY_ANALYZER)`.
- [ ] Run the pinned replay twice and assert the ordered lifecycle event stream is byte-identical after normalizing `run_id` and output paths.
- [ ] Commit with `feat(replay): Export entity lifecycle telemetry`.

## Task 5: Emit production, upgrade, science, and economy observations

**Files:**
- Modify: `GeneralsMD/Code/GameEngine/Source/GameLogic/Object/Update/ProductionUpdate.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Source/Common/RTS/Money.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Source/Common/RTS/Player.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Source/GameLogic/Object/Update/DockUpdate/SupplyCenterDockUpdate.cpp`
- Create: `scripts/replay_analyzer/tests/engine/test_economy_production.py`

- [ ] Write tests for queue/cancel/complete balance: each production ID must have one queue and at most one terminal cancellation or completion. Require cash balances never be inferred by the test from commands; compare emitted before/after balances.
- [ ] Emit `production_queued`, `production_cancelled`, `production_completed`, `upgrade_queued`, `upgrade_cancelled`, `upgrade_completed`, `science_purchased`, and `special_power_used` with stable template/upgrade/science names and involved entity/player IDs.
- [ ] Instrument `Money::withdraw`, `deposit`, and `setStartingCash` to emit `cash_changed` with before, delta, after, track-income flag, and an explicit reason enum. Where the central money API lacks reason context, pass an observational reason from call sites or emit `unknown`; never guess.
- [ ] Emit `supply_collected` at the supply dock handoff with collector, source, player, raw amount, and raw location.
- [ ] Validate that emitted cash transitions fold to the engine's final balance for every player in the fixture.
- [ ] Commit with `feat(replay): Export economy and production telemetry`.

## Task 6: Emit combat, veterancy, defeat, and outcome observations

**Files:**
- Modify: `GeneralsMD/Code/GameEngine/Source/GameLogic/Object/Object.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Source/GameLogic/Object/Body/ActiveBody.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Source/Common/RTS/Player.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp`
- Create: `scripts/replay_analyzer/tests/engine/test_combat_outcome.py`

- [ ] Write tests requiring damage events to reference existing attacker/victim entities when the engine supplies them, killing blows to precede destruction, and exactly one final replay outcome record.
- [ ] Emit `damage_applied` after authoritative damage calculation with attempted and applied amount, prior/new health, damage type, death type, attacker, victim, weapon/template name, raw location, and killing-blow flag.
- [ ] Emit `healing_applied` and `veterancy_changed` at their state changes.
- [ ] Emit `player_defeated`, `player_surrendered`, `player_disconnected`, and `match_outcome` from authoritative player/replay state. Represent unknown winner as unknown; do not infer winner from surviving entities.
- [ ] Include replay quit-early, desync, CRC mismatch frame, and clean completion in the final trace.
- [ ] Commit with `feat(replay): Export combat and outcome telemetry`.

## Task 7: Export orders and bounded movement samples

**Files:**
- Create: `GeneralsMD/Code/GameEngine/Include/Common/ReplayMovementSampler.h`
- Create: `GeneralsMD/Code/GameEngine/Source/Common/ReplayMovementSampler.cpp`
- Modify: `GeneralsMD/Code/GameEngine/CMakeLists.txt`
- Modify: `Core/GameEngine/Source/GameLogic/System/GameLogicDispatch.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Source/GameLogic/System/GameLogic.cpp`
- Create: `scripts/replay_analyzer/tests/engine/test_orders_movement.py`

- [ ] Write tests requiring order events to preserve command frame/player/entity/target data and movement samples to be no farther apart than the configured 15 frames while a mobile entity is moving.
- [ ] Emit `order_issued` after a replay command resolves to engine entities/targets, including numeric/symbolic command type, source player, selected object IDs, target object or raw target location, and command source.
- [ ] Emit `entity_state_changed` when locomotor or AI state changes between idle, moving, attacking, collecting, returning, guarding, garrisoned, disabled, and destroyed, using engine states only.
- [ ] At the end of `GameLogic::update`, sample enabled mobile entities when position/orientation changed and either an event forced a sample or `movementSampleFrames` elapsed. Structures sample only on lifecycle/state events.
- [ ] Coalesce exact duplicates. Payload includes raw position, orientation, layer, speed, current order/state, and path goal when exposed by GameLogic.
- [ ] Run the fixture at sampling intervals 15 and 30. Assert lifecycle/economy/combat events are identical and only sample density changes.
- [ ] Commit with `feat(replay): Export order and movement telemetry`.

## Task 8: Export real map, terrain, resource, and navigation data

**Files:**
- Create: `GeneralsMD/Code/GameEngine/Include/Common/ReplayMapExport.h`
- Create: `GeneralsMD/Code/GameEngine/Source/Common/ReplayMapExport.cpp`
- Modify: `GeneralsMD/Code/GameEngine/CMakeLists.txt`
- Modify: `GeneralsMD/Code/GameEngine/Source/GameLogic/Map/TerrainLogic.cpp`
- Modify: `Core/GameEngine/Include/GameLogic/AIPathfind.h`
- Create: `scripts/replay_analyzer/contracts/map-asset-v1.schema.json`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/telemetry/map_asset.py`
- Create: `scripts/replay_analyzer/tests/engine/test_map_export.py`

- [ ] Define a content-addressed map asset directory containing `manifest.json`, `height.f32.zlib`, `terrain.u8.zlib`, `pathing-ground.u8.zlib`, `pathing-amphibious.u8.zlib`, and `zones.i32.zlib`. Manifest records dimensions, world bounds, cell size, endianness, data types, uncompressed sizes, and SHA-256 for every member.
- [ ] Export only after terrain and pathfinding initialization is complete. Read actual terrain heights/types and pathfinder cells/zones. Add read-only const accessors where the current public API cannot expose required cell classification; do not mutate or recalculate pathfinding.
- [ ] Export starting positions, named waypoints, bridges, static blockers, cliffs/water classification, supply docks/piles, oils, and capturable objects with raw engine coordinates and stable template names.
- [ ] Validate decompressed lengths and hashes in Python. Reject partial assets and path cells outside declared dimensions.
- [ ] Cache by map content hash. A cache hit must reproduce the same manifest and must not rewrite sidecars.
- [ ] Add a test proving the pinned replay's exported map bounds contain every entity sample; out-of-bounds observations fail validation rather than being clamped.
- [ ] Commit with `feat(replay): Export authoritative map data`.

## Task 9: Implement the isolated headless telemetry runner

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/engine/config.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/engine/runner.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/engine/result.py`
- Create: `scripts/replay_analyzer/tests/engine/test_runner.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/cli.py`

- [ ] Write unit tests around an injected process launcher for success, timeout, non-zero exit, missing trace, invalid completion, writer error, CRC mismatch, and stale pre-existing output.
- [ ] Define `EngineRunConfig(executable, timeout_seconds, movement_sample_frames, data_root)` and `EngineRunResult(run_id, trace_path, map_assets, stdout_path, stderr_path, exit_code, status)`.
- [ ] Create a unique `%LOCALAPPDATA%\GeneralsReplayAnalyzer\runs\<run-id>\` directory. Refuse reuse, launch with explicit argument array and no shell, capture stdout/stderr, and enforce timeout with process-tree termination.
- [ ] Validate the complete trace and every referenced asset before returning success. Leave failed run directories intact for diagnostics.
- [ ] Add `replay-analyzer export-telemetry <replay> --engine <generalszh.exe>`. JSON output contains paths/status only after validation.
- [ ] Run unit tests, then the pinned engine integration test.
- [ ] Commit with `feat(replay): Add headless telemetry runner`.

## Task 10: Prove non-interference and deterministic output

**Files:**
- Create: `scripts/replay_analyzer/tests/engine/test_telemetry_determinism.py`
- Create: `docs/replay-analyzer/telemetry-verification.md`

- [ ] Run the pinned replay three times with telemetry disabled and three times enabled. Record exit code, final frame, CRC mismatch state, outcome, normalized trace SHA-256, and map asset SHA-256.
- [ ] Normalize only run ID, paths, and wall-clock creation timestamps. Do not normalize frame, sequence, values, ordering, or float bits.
- [ ] Require the three enabled normalized traces to be identical. Require enabled versus disabled final frame, result, and CRC status to match.
- [ ] Run the repository retail replay suite with telemetry disabled and enabled using a unique trace directory per replay. Compare replay pass/fail and CRC mismatch results.
- [ ] Document exact commands, engine commit, fixture hashes, sampling interval, results, and any unsupported event fields as unknown.
- [ ] Commit with `test(replay): Verify telemetry non-interference`.

## Engine Telemetry Acceptance Gate

- [ ] Modern Zero Hour Release and Debug builds pass; VC6 builds without analyzer sources.
- [ ] All schema, runner, and engine-marked telemetry tests pass.
- [ ] The pinned replay produces valid manifest and completion records, monotonic evidence IDs, real player names, complete referenced assets, and no writer errors.
- [ ] Repeated normalized traces and map assets are byte-identical.
- [ ] Telemetry-enabled playback matches telemetry-disabled final frame, outcome, and CRC state across the pinned fixture and retail replay suite.
- [ ] No synthetic prototype module participates in telemetry or map export.
- [ ] Push completed commits to `origin/feat/replay-analyzer-v2` before starting analytics.
