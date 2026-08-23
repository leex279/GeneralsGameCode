# Replay Analyzer Production Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a normal configured worker and CLI acquire, validate, import, and analyze real engine telemetry instead of depending on the browser fixture's injected telemetry.

**Architecture:** Add one narrow adapter from the already validated `engine.runner.export_telemetry` result to the importer's `TelemetryAcquirer` contract. Compose that adapter only in processes allowed to launch the engine (worker and foreground CLI); Web remains an enqueue-only boundary. Accepted CRC/truncation traces remain importable as partial evidence, while launch, asset, trace, and outcome failures stay typed failures.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Pydantic 2, SQLite, pytest, uv, Ruff, strict mypy, existing Zero Hour engine runner.

**Spec:** `docs/superpowers/specs/2026-08-18-replay-analyzer-v2-design.md`

## Global Constraints

- Zero Hour first; do not alter gameplay or balance.
- GameLogic stays deterministic and has no web, SQLite, clock, Ollama, or presentation dependency.
- Web requests may enqueue work but must never launch `generalszh.exe`.
- The adapter must use the existing no-shell isolated runner and must not parse unvalidated trace bytes.
- A validated CRC/truncation trace is partial evidence, not a full-match success claim.
- Every architectural production-code change uses the repository `TheSuperHackers` comment convention.
- Never stage the five protected engine/audio files or the protected untracked watcher test.
- Use test-first red/green cycles and commit/push each independently verified task.

---

### Task 1: Adapt validated engine runs to telemetry acquisition

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/importing/engine_acquirer.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/importing/__init__.py`
- Test: `scripts/replay_analyzer/tests/importing/test_engine_acquirer.py`

**Interfaces:**
- Consumes: `EngineRunConfig`, `EngineRunResult`, `EngineRunStatus`, and `export_telemetry(replay, config)`.
- Produces: `EngineTelemetryAcquirer(settings, exporter=export_telemetry).acquire(replay, replay_sha256) -> TelemetryArtifact`.
- Maps runner `success` to importer `runner_status="success"`, `replay_quality="complete"`, `strategy_analysis_scope="full"`.
- Maps a validated trace ending in CRC mismatch, replay truncation, or interruption to importer `runner_status="success"`, `replay_quality="partial"`, `strategy_analysis_scope="observed_boundary_only"`, retaining the engine terminal status as a diagnostic.
- Maps all other runner outcomes to their exact failure status with no fabricated trace/catalog/map paths.

- [ ] **Step 1: Write failing adapter tests**

Cover replay SHA mismatch before launch, exact `EngineRunConfig` construction, full success mapping, partial validated-trace mapping, failed-run mapping, diagnostic redaction, and executable SHA-256 binding.

- [ ] **Step 2: Verify the tests fail for the missing adapter**

Run: `uv run --project . pytest tests/importing/test_engine_acquirer.py -q`

Expected: test collection fails because `importing.engine_acquirer` does not exist.

- [ ] **Step 3: Implement the minimal adapter**

Use a frozen class with an injected exporter callable. Hash the replay and executable using bounded streaming reads, reject mismatched replay content before launch, construct `EngineRunConfig` from `AnalyzerSettings`, translate only the closed runner statuses above, and convert `RunDiagnostic` to `AcquisitionDiagnostic` without caller paths.

- [ ] **Step 4: Verify focused tests and static checks**

Run: `uv run --project . pytest tests/importing/test_engine_acquirer.py tests/engine/test_runner.py -q`

Run: `uv run --project . ruff check src/generals_replay_analyzer/importing/engine_acquirer.py tests/importing/test_engine_acquirer.py`

Run: `uv run --project . mypy --strict src/generals_replay_analyzer/importing/engine_acquirer.py`

- [ ] **Step 5: Commit and push**

```powershell
git add scripts/replay_analyzer/src/generals_replay_analyzer/importing/engine_acquirer.py scripts/replay_analyzer/src/generals_replay_analyzer/importing/__init__.py scripts/replay_analyzer/tests/importing/test_engine_acquirer.py
git commit -m "feat(analyzer): Adapt engine telemetry acquisition"
git push
```

### Task 2: Wire engine acquisition into production worker and CLI

**Files:**
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/analysis_pipeline/composition.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/worker.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/cli.py`
- Test: `scripts/replay_analyzer/tests/analysis_pipeline/test_composition.py`
- Test: `scripts/replay_analyzer/tests/test_worker.py`
- Test: `scripts/replay_analyzer/tests/test_cli.py`

**Interfaces:**
- Produces: `configured_engine_telemetry_acquirer(settings) -> EngineTelemetryAcquirer | None` in the composition root.
- Worker and foreground CLI pass that result and version `engine-telemetry-v1` when `settings.engine_executable` is configured; otherwise they keep parser-only behavior and version `none`.
- Web continues to submit durable telemetry intent and never receives an engine-launching adapter.

- [ ] **Step 1: Write failing production-composition tests**

Assert configured worker and CLI composition expose a non-null telemetry acquirer, unconfigured composition remains parser-only, and the Web application never invokes an exporter during request handling.

- [ ] **Step 2: Verify the tests fail at the current `telemetry_acquirer=None` wiring**

Run: `uv run --project . pytest tests/analysis_pipeline/test_composition.py tests/test_worker.py tests/test_cli.py -q`

Expected: configured production wiring assertions fail.

- [ ] **Step 3: Implement the composition helper and wire worker/CLI**

Construct the adapter from the immutable process-lifetime `AnalyzerSettings`. Do not add an exporter to `web/dependencies.py`; its import service remains enqueue-only.

- [ ] **Step 4: Verify focused and integration tests**

Run: `uv run --project . pytest tests/analysis_pipeline tests/test_worker.py tests/test_cli.py tests/importing -q`

Run: `uv run --project . ruff check src tests/analysis_pipeline tests/test_worker.py tests/test_cli.py tests/importing`

Run: `uv run --project . mypy --strict src`

- [ ] **Step 5: Commit and push**

```powershell
git add scripts/replay_analyzer/src/generals_replay_analyzer/analysis_pipeline/composition.py scripts/replay_analyzer/src/generals_replay_analyzer/worker.py scripts/replay_analyzer/src/generals_replay_analyzer/cli.py scripts/replay_analyzer/tests/analysis_pipeline/test_composition.py scripts/replay_analyzer/tests/test_worker.py scripts/replay_analyzer/tests/test_cli.py
git commit -m "fix(analyzer): Wire production engine telemetry"
git push
```

### Task 3: Prove a normal configured import consumes real runner output

**Files:**
- Create: `scripts/replay_analyzer/tests/integration/test_production_telemetry_import.py`
- Modify: `scripts/replay_analyzer/docs/acceptance-matrix.md`
- Modify: `scripts/replay_analyzer/docs/release-status.md`

**Interfaces:**
- Consumes: the public production composition, a configured executable, and the pinned retail replay.
- Produces: a clean-root durable job graph containing parse, telemetry, imported observations, deterministic features, strategy assessment, and a fixed report, with the exact observed evidence horizon.

- [ ] **Step 1: Write the installed production-path test**

Use the real configured adapter, not `_FixtureTelemetryAcquirer` and not direct ORM seeding. Assert the telemetry job is terminal, every accepted telemetry artifact hash verifies, the report horizon equals the engine completion record, and no post-horizon conclusion exists.

- [ ] **Step 2: Run the test against the current executable**

Run: `uv run --project . pytest tests/integration/test_production_telemetry_import.py -q -s`

Expected today: a truthful partial report at the known CRC boundary; after replay compatibility is repaired, the same test must become a full-match report without changing the ingestion API.

- [ ] **Step 3: Run the complete Python/static gates**

Run: `uv run --project . pytest -q`

Run: `uv run --project . ruff check src tests`

Run: `uv run --project . mypy --strict src`

- [ ] **Step 4: Record exact evidence and push**

Record commands, pass/fail counts, executable hash, replay hash, telemetry trace hash, observed horizon, and the remaining replay-compatibility boundary. Do not describe partial evidence as a complete match.

```powershell
git add scripts/replay_analyzer/tests/integration/test_production_telemetry_import.py scripts/replay_analyzer/docs/acceptance-matrix.md scripts/replay_analyzer/docs/release-status.md
git commit -m "test(analyzer): Verify production telemetry ingestion"
git push
```
