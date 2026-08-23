# Replay Analyzer Engine-Native Insights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reuse authoritative Zero Hour score, economy-rate, visibility, and partition-heuristic providers to deliver truthful scouting and reconciliation insights without duplicating simulation.

**Architecture:** Add optional, bounded telemetry-v2 observations behind analyzer-only modern-build guards. Validate and persist them through the generic event graph, derive explicitly caveated features, then publish typed fixed-report map-v2 overlays and scouting summaries. All sampling is deterministic and observational.

**Tech Stack:** Legacy-compatible C++, Python 3.12, JSON Schema, Pydantic 2, SQLAlchemy 2, SQLite, FastAPI/Jinja2, ECharts, pytest, Playwright, uv, Ruff, strict mypy.

**Spec:** `docs/superpowers/specs/2026-08-23-replay-analyzer-engine-native-insights-design.md`

## Global Constraints

- Zero Hour first; no gameplay or balance changes.
- Never call object/partition-data shroud getters that mutate ever-seen or ghost state.
- No RNG, retained engine pointers, simulation writes, or debug-overlay scraping.
- New engine code is guarded by `RTS_REPLAY_ANALYZER && !IS_VS6_BUILD`.
- New architectural/user-facing code carries `TheSuperHackers @feature Leex 23/08/2026 ... (#0)`.
- Old telemetry-v2 traces remain valid.
- Never stage protected user files or `tests/watching/test_ingress_contract.py`.
- Every task uses an observed RED test, focused GREEN verification, independent review, commit, and push.

---

### Task 1: Freeze engine-native telemetry contracts

**Files:**
- Modify: `scripts/replay_analyzer/contracts/telemetry-v2.schema.json`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/telemetry/model.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/telemetry/reader.py`
- Create: `scripts/replay_analyzer/tests/telemetry/test_engine_native_observation_contract.py`

**Interfaces:** Accept optional `scorekeeper_snapshot`, `cash_per_minute_snapshot`, `object_visibility_changed`, `visibility_sampling_summary`, and `partition_engine_grid_sample` events with the exact fields and semantics in the spec. Preserve acceptance of old v2 traces.

- [ ] Write literal RED fixtures for strict fields/ranges, sorted unique players/cells, one terminal score snapshot, CPM cadence/final sample/bucket wrap, live-object visibility transitions and one-time first-clear, sampling-summary counts, grid caps/bounds, and old-trace compatibility.
- [ ] Run `uv run --project . pytest tests/telemetry/test_engine_native_observation_contract.py -q` and confirm failures are caused by missing event contracts.
- [ ] Implement the minimal closed schema/models/reader sequence checks; reject extra fields and contradictory counts without requiring the optional families in old traces.
- [ ] Run the focused test, `uv run --project . ruff check src/generals_replay_analyzer/telemetry tests/telemetry/test_engine_native_observation_contract.py`, and `uv run --project . mypy --strict src/generals_replay_analyzer/telemetry`.
- [ ] Commit and push `feat(telemetry): Define engine-native insight events`.

### Task 2: Export terminal ScoreKeeper snapshots

**Files:**
- Create: `GeneralsMD/Code/GameEngine/Include/Common/ReplayScoreKeeper.h`
- Create: `GeneralsMD/Code/GameEngine/Source/Common/ReplayScoreKeeper.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp`
- Modify: `GeneralsMD/Code/GameEngine/CMakeLists.txt`
- Create: `scripts/replay_analyzer/tests/engine/test_scorekeeper_telemetry.py`

**Interfaces:** `ReplayScoreKeeper::writeTerminalSnapshot(Int frame)` emits one sorted resolved-occupied-player aggregate immediately before outcome/completion using raw getters only.

- [ ] Write RED source/runtime tests requiring terminal ordering, exact signed getter values, stable player domain, one event, and absence of `calculateScore()`.
- [ ] Run the focused test and confirm the provider is missing.
- [ ] Implement the guarded provider and terminal hook without modifying `ScoreKeeper`.
- [ ] Run the focused test plus `cmake --build build/win32 --target z_generals --config Release` and Debug.
- [ ] Commit and push `feat(telemetry): Export terminal score totals`.

### Task 3: Export exact cash-per-minute evidence

**Files:**
- Modify: `GeneralsMD/Code/GameEngine/Include/Common/ReplayEconomy.h`
- Modify: `GeneralsMD/Code/GameEngine/Source/Common/ReplayEconomy.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Source/Common/RTS/Money.cpp`
- Modify: `scripts/replay_analyzer/tests/engine/test_economy_production.py`

**Interfaces:** `cash_changed` carries bucket provenance exactly when income is tracked. The economy sampler emits aggregate 30-frame CPM snapshots after income-bucket rotation and at the terminal frame.

- [ ] Add RED deposits at frames 29/30 and 1799/1800, uint32 wrap, bucket expiry, forced-final, source ordering, and reader-fold assertions.
- [ ] Run the focused test and confirm provenance/snapshots are absent.
- [ ] Pass original unsigned deposit and current bucket from `Money::deposit()`; sample sorted players after `Player::updateIncomeBucket()` without modifying money state.
- [ ] Run economy, telemetry-contract, Release, and Debug gates.
- [ ] Commit and push `feat(telemetry): Export engine income rates`.

### Task 4: Export safe sampled scouting visibility

**Files:**
- Create: `GeneralsMD/Code/GameEngine/Include/Common/ReplayVisibilitySampler.h`
- Create: `GeneralsMD/Code/GameEngine/Source/Common/ReplayVisibilitySampler.cpp`
- Create: `scripts/replay_analyzer/tests/engine/test_visibility_telemetry.py`
- Create: `scripts/replay_analyzer/tests/engine/test_engine_native_source_contract.py`

**Interfaces:** Sample object-center partition cells every 15 frames with deterministic `(player_index, object_id)` order, an 8,192-pair pass cap, cycle/cursor summaries, and no retained `Object *`.

- [ ] Write RED behavioral tests for transitions, first-clear exactly once, object destruction, capped cycles, reset, deterministic ordering, and repeat-run bytes.
- [ ] Write a source safety test that rejects `getShroudedStatus(`, `m_everSeenByPlayer`, `GhostObject`, RNG, and retained object pointers while requiring `PartitionManager::getShroudStatusForPlayer`.
- [ ] Run both tests and confirm the sampler is absent.
- [ ] Implement telemetry-owned state keyed by stable integer/public identities and side-effect-free cell reads only.
- [ ] Run focused, Release, Debug, and telemetry off/on parity gates.
- [ ] Commit and push `feat(telemetry): Export sampled scouting visibility`.

### Task 5: Export bounded engine heuristic lattices

**Files:**
- Create: `GeneralsMD/Code/GameEngine/Include/Common/ReplayPartitionSampler.h`
- Create: `GeneralsMD/Code/GameEngine/Source/Common/ReplayPartitionSampler.cpp`
- Create: `scripts/replay_analyzer/tests/engine/test_partition_heuristic_telemetry.py`

**Interfaces:** Every 300 frames plus terminal, emit at most 128 unique row-major uniform cells per resolved player with map edges, world positions, shroud, owner threat, and owner cash-value reads.

- [ ] Write RED tests for tiny/large grids, edge inclusion, no duplicates, eight-player cap, source labels, final sample, and repeat determinism.
- [ ] Run the focused test and confirm the provider is missing.
- [ ] Implement integer-only index selection and direct provider values; do not normalize through debug-display maxima.
- [ ] Run focused, Release, Debug, and telemetry determinism gates.
- [ ] Commit and push `feat(telemetry): Export engine pressure heuristics`.

### Task 6: Integrate samplers at one deterministic engine seam

**Files:**
- Modify: `GeneralsMD/Code/GameEngine/Source/GameLogic/System/GameLogic.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp`
- Modify: `GeneralsMD/Code/GameEngine/CMakeLists.txt`
- Create: `scripts/replay_analyzer/tests/engine/test_engine_native_integration.py`

**Interfaces:** Initialize/reset providers with replay telemetry; sample after partition/subsystem updates and the movement sampler, before frame increment; force final events before outcome/completion.

- [ ] Write RED ordering, guard, reset, no-simulation-write, and terminal-order tests.
- [ ] Implement one guarded call site and lifecycle; do not spread hooks across GameLogic.
- [ ] Run all new engine tests, Release/Debug builds, and available VC6 build.
- [ ] Commit and push `feat(replay): Integrate engine insight sampling`.

### Task 7: Derive reconciliation and economy features

**Files:**
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/features/service.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/features/registry.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/features/economy.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/features/scorekeeper.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/analysis_pipeline/composition.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/analysis_pipeline/handlers.py`
- Modify: `scripts/replay_analyzer/tests/features/test_economy.py`
- Create: `scripts/replay_analyzer/tests/features/test_scorekeeper.py`
- Modify: `scripts/replay_analyzer/tests/features/test_service.py`

**Interfaces:** Add `economy.cash_per_minute_latest|peak|series|reconciled_share` and `scorekeeper.terminal_snapshot|event_reconciliation`. Project the current replay-player entry from aggregate events and bind every value to observed evidence.

- [ ] Write RED fixtures for exact CPM folds, partial traces, missing optional families, every score metric, semantic mismatch reasons, and per-player projection.
- [ ] Implement deterministic extractors; ScoreKeeper deltas are `partial|not_comparable`, never validation failure.
- [ ] Run feature/pipeline/report suites, Ruff, and strict mypy.
- [ ] Commit and push `feat(analyzer): Reconcile engine match totals`.

### Task 8: Publish scouting and heuristic map-v2 insights

**Files:**
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/spatial/query.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/spatial/features.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/web/ports.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/web/viewmodels/map.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/maps/detail.html`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/web/static/js/map.js`
- Modify: `scripts/replay_analyzer/tests/web/test_map_scene.py`
- Modify: `scripts/replay_analyzer/tests/web/test_map_adapter.py`
- Modify: `scripts/replay_analyzer/tests/web/test_map.py`
- Modify: `scripts/replay_analyzer/tests/web/test_map_accessibility.py`

**Interfaces:** Publish `replay-map-scene-v2` with typed visibility transitions and opt-in latest-in-window sampled overlays. Preserve fixed-report evidence membership, filters, bounded payloads, and equivalent no-script tables.

- [ ] Write RED tests for first-observed timings, incomplete-cycle disclosure, overlay opt-in, exact labels, evidence links, fixed-report/run identity, filters, accessibility, and prohibition of `map control|territory|resources` for heuristic values.
- [ ] Implement DTOs/query/view model and ECharts layers; v1 payloads remain rejected rather than silently reinterpreted.
- [ ] Run map/Web/browser/accessibility suites, Ruff, and strict mypy.
- [ ] Commit and push `feat(web): Show engine-backed scouting insights`.

### Task 9: Prove determinism and corpus compatibility

**Files:**
- Modify: `scripts/replay_analyzer/docs/acceptance-matrix.md`
- Modify: `scripts/replay_analyzer/docs/release-status.md`
- Modify: this plan

**Interfaces:** Bind accepted evidence to exact source commit, Release/Debug/VC6 binary hashes, replay hashes, normalized trace hashes, and map/catalog identities.

- [ ] Run all telemetry/engine/feature/map tests and the complete Python suite.
- [ ] Run Ruff and strict mypy on the complete package.
- [ ] Build Zero Hour Release and Debug plus available VC6.
- [ ] Run the pinned replay three times and require byte-identical normalized telemetry.
- [ ] Run all ten retail replays with telemetry disabled/enabled and require identical CRC behavior and terminal facts.
- [ ] Refresh expected hashes only from those exact runs; inspect the useful scouting/heuristic Web journey and accessibility at desktop/mobile.
- [ ] Commit and push `test(replay): Verify engine-native insights`.

