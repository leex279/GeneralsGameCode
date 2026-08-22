# Task 3 Report: Transactional Replay Intake and Resumable Stage Orchestration

Date: 21 August 2026

## Accepted baseline and contract verification

- Worktree: `.worktrees/replay-analyzer-v2-product`, branch `feat/replay-analyzer-v2-product`.
- Accepted Task 1 implementation/hardening: `d542ec73e8893d732999ca047917e1214751ab67`.
- Accepted Task 2 implementation: `7b908df5df91cc275a15c2cf3d27dc8d2f7a0392`.
- Accepted frozen Task 2 review baseline and Task 3 BASE: `d3f2ee2fd88bd92e954636a3ae2884a0351e209d`.
- Migration head verified as `0001_replay_analyzer_v2`.
- Verified public Task 1 contracts: `AnalyzerSettings.database_path`, managed replay/cache paths,
  `ContentAddressedStore.store_file()`, and lowercase `StoredContent.sha256`.
- Verified accepted Task 2 models: `Source`, `Replay`, `ManagedAsset`, `ParserRun`, `TelemetryRun`,
  `ReplayQualityIssue`, `Job`, and `JobDependency`, including successful observation immutability triggers.
- Verified database access remains through `create_database_engine()` and `create_session_factory()`.
- Baseline broad offline suite before Task 3: `610 passed, 238 deselected in 60.11s`.

## RED evidence

The mandated service command was run before the production package existed:

```powershell
uv run --project scripts/replay_analyzer pytest `
  scripts/replay_analyzer/tests/importing/test_import_service.py -q
```

Observed RED: collection failed with
`ModuleNotFoundError: No module named 'generals_replay_analyzer.importing'`.

After adding only the public DTO/protocol skeleton, the same command collected and reported nine expected behavior
failures at `ImportService.submit()` with `NotImplementedError`. The RED therefore advanced beyond import collection.

The mandated orchestration command was then run before `jobs.py` existed:

```powershell
uv run --project scripts/replay_analyzer pytest `
  scripts/replay_analyzer/tests/importing/test_jobs.py -q
```

Observed RED: collection failed with
`ModuleNotFoundError: No module named 'generals_replay_analyzer.importing.jobs'`.

The CLI integration was also captured RED before the adapter change: `main(["import", ...])` exited 2 because the
only registered commands were `inspect` and `export-telemetry`.

## Implemented boundary

- Added frozen public intake/acquisition DTOs and the runner-independent `TelemetryAcquirer` / `TelemetryArtifact`
  port. No importing module imports engine runner, runner config/result, or private telemetry readers.
- Added the ten closed stages and exact immutable version constants from the brief. Content identities use Task 2
  canonical JSON and exact lowercase `stage:version:replay_sha256:input_digest` keys.
- Added bounded file/folder discovery with case-insensitive `.rep` filtering, deterministic normalized sorting,
  non-following symlink/reparse handling, source provenance history, and structured diagnostics.
- Added streaming lowercase SHA-256 replay identity, unique-race recovery, one replay for duplicate bytes, and distinct
  `Source` rows for distinct paths/invocations. Strata values remain only on `Source`.
- Added copy mode through `ContentAddressedStore` and product-relative POSIX `ManagedAsset` metadata. Reference mode
  creates no managed replay asset. Managed copies remain usable after source deletion; missing references fail with
  stable `source_missing` evidence.
- Added parser summary handling only. Task 3 creates no `ParserRun`, command, replay-player, alias, evidence-item, or
  quality-issue row.
- Added optional injected telemetry acquisition, full artifact validation/copy, exact status/quality/scope retention,
  and public asset manifests without source/run/managed paths. Task 3 creates no telemetry observations.
- Added the full durable future DAG. Task 3 registers handlers only for `discover`, `hash`, `manage_copy`, `parse`, and
  injected `telemetry`; unregistered later stages remain pending and cannot be claimed.
- Added transactional idempotent creation, indirect cycle rejection, dependency gating/failure projection,
  conditionally guarded claims, leases, deterministic bounded retry, expiry reclamation, owner-checked completion,
  explicit retry, descendant unblocking, and restart recovery through independently constructed coordinators.
- Added thin CLI adapters for `import ... [--recursive] [--reference-only] [--json]` and `jobs retry <job-id>` while
  preserving `inspect` and `export-telemetry`. CLI initialization is the only new directory/migration boundary.

## Focused evidence

Final focused suite:

```text
29 passed in 17.38s
```

Required importing-package coverage:

```text
29 passed in 22.69s
TOTAL 779 statements, 67 missed, 91.40% coverage
```

The seeded standard-library invariant test uses seed `0xC0FFEE`, creates 100 independently prefixed acyclic graphs,
randomizes edge insertion, proves dependency-respecting claim order, and proves a closing cycle is rejected without
changing edge count.

Duplicate/copy/reference evidence proves two source rows link to one replay and one content-stage set, managed metadata
uses a relative POSIX path, a deleted copied source still parses, and a deleted reference fails without a managed asset.

Fake-acquirer success preserves trace/stdout asset manifests. Fake-acquirer failure preserves copied diagnostics/assets
in job error details. Invalid/duplicate artifacts are rejected. All cases assert zero `TelemetryRun` and
`TelemetryEvent` rows.

Parser/acquirer failure tests assert zero parser/telemetry observations, commands, replay players, players, aliases,
and quality issues. This is the Task 3 zero-partial-row proof; final observation transactions remain Task 4-owned.

## Broad, static, and packaging evidence

- Broad offline suite: `639 passed, 238 deselected in 77.80s`.
- Ruff: `All checks passed!`.
- Strict mypy: `Success: no issues found in 40 source files`.
- Existing installed-wheel smoke: `2 passed in 15.60s`.
- Extra fresh wheel install: successfully imported
  `generals_replay_analyzer.importing.service` from the installed wheel.
- `git diff --check`: exit 0.

## Changed files

- `scripts/replay_analyzer/src/generals_replay_analyzer/importing/__init__.py`
- `scripts/replay_analyzer/src/generals_replay_analyzer/importing/stages.py`
- `scripts/replay_analyzer/src/generals_replay_analyzer/importing/jobs.py`
- `scripts/replay_analyzer/src/generals_replay_analyzer/importing/service.py`
- `scripts/replay_analyzer/tests/importing/__init__.py`
- `scripts/replay_analyzer/tests/importing/conftest.py`
- `scripts/replay_analyzer/tests/importing/test_import_service.py`
- `scripts/replay_analyzer/tests/importing/test_jobs.py`
- `scripts/replay_analyzer/src/generals_replay_analyzer/cli.py`
- `.superpowers/sdd/2026-08-18-replay-analyzer-v2-analytics/task-3-report.md`

The five inherited C++/audio/CMake paths remained unstaged and untouched by Task 3. No database model, migration,
configuration, storage, parser, provenance, engine, telemetry, package, lock, wheel-test, or later-task file changed.

## Final commit

Commit: `f6880f46de3e30cec55db1ecf66af4ceebe09be3`

Subject: `feat(replay): Add resumable replay import`

## Review fix round 1/5

Date: 22 August 2026

### RED evidence

The five Important review findings were reproduced with tests before production changes. The combined focused run
reported `5 failed, 1 skipped in 2.88s`:

- parser and telemetry reference mutations were accepted instead of failing with `source_changed`;
- a failed reference branch and later copy branch shared one downstream observation job;
- explicit retry cleared a live worker lease;
- graph-local duplicate job creation raised `UNIQUE constraint failed: jobs.idempotency_key`; and
- real symlink creation was unavailable on this Windows host and skipped.

A deterministic supplied-alias/reparse regression was then run separately and reported `1 failed in 1.68s`: resolving
the supplied alias before inspecting it allowed the telemetry job to succeed.

The regression command covered the new cases in `test_import_service.py` and `test_jobs.py`:

```powershell
uv run --project . pytest `
  tests/importing/test_import_service.py::test_reference_parser_revalidates_bytes_after_consumption `
  tests/importing/test_import_service.py::test_reference_telemetry_revalidates_bytes_before_copying_artifacts `
  tests/importing/test_import_service.py::test_failed_reference_branch_cannot_block_later_successful_copy_branch `
  tests/importing/test_import_service.py::test_artifact_port_rejects_supplied_symlink_before_path_canonicalization `
  tests/importing/test_import_service.py::test_artifact_port_inspects_supplied_alias_before_resolving_target `
  tests/importing/test_jobs.py::test_retry_rejects_running_job_without_clearing_another_workers_lease `
  tests/importing/test_jobs.py::test_graph_local_idempotency_conflict_reloads_competing_job_and_keeps_transaction_usable -q
```

### Fixes and GREEN evidence

- Reference-mode parser and acquirer calls now hash and validate the ordinary, non-reparse source immediately before
  and after consumption. Mutation/disappearance has stable nonretryable code `source_changed`; managed-copy behavior
  is unchanged.
- Downstream job identity now includes import mode plus the selected parse and optional telemetry identities, so a
  failed reference DAG cannot poison a later copy DAG for identical replay bytes.
- Explicit retry now accepts only pending/failed jobs and returns stable `running_job` without modifying another
  worker's active lease.
- Artifact validation now calls `lstat`, symlink, and reparse checks on the exact supplied path before canonicalizing
  it for uniqueness. The real symlink case remains an environment skip; the deterministic reparse-alias test proves
  resolution is never reached.
- SQLite job creation now uses conflict-safe insert-and-reload within the caller's graph transaction. The deterministic
  competing-commit regression proves coalescing returns the durable winner and the outer transaction remains usable.

Targeted GREEN: `4 passed, 1 skipped in 2.19s`; the reference mutation subset separately reported `3 passed in
1.68s`. Final focused suite: `35 passed, 1 skipped in 22.49s`.

### Fix-round gates

- Importing coverage: `35 passed, 1 skipped`; `809` statements, `71` missed, `91.22%` coverage.
- Broad offline suite: `645 passed, 1 skipped, 238 deselected in 83.56s`.
- Installed-wheel smoke: `2 passed in 16.27s`.
- Ruff: `All checks passed!`.
- Strict mypy: `Success: no issues found in 40 source files`.
- `git diff --check`: exit 0.
- Import-boundary prohibited-import scan: no engine runner/config/result or private telemetry reader/catalog/map import.
- Diff scope from `f6880f46de3e30cec55db1ecf66af4ceebe09be3`: only `importing/jobs.py`,
  `importing/service.py`, and their two owned importing test files.
- The five inherited C++/audio/CMake paths remained untouched and unstaged.

Fix commit: `bda3c69c8e4a22fc74ae642dc9124c508f645295`

Subject: `fix(replay): Harden resumable replay import`

## Review fix round 2/5: public future-stage handler seam

Date: 22 August 2026

### Entry-gate defect and approved boundary

Task 4 correctly stopped because Task 3 exposed neither a supported future-stage handler registration seam nor an
immutable dependency-output context. The approved amendment keeps the existing durable job system and adds only a
construction-time registration boundary. A registered handler receives public job/replay identity, the bound stage and
component version, deeply frozen canonical input, and deterministically stage-ordered direct succeeded dependency
snapshots. No ORM row, database ID, timestamp, source/managed path, runner type, or private Task 3 state crosses it.

### RED evidence

Before production edits, the wished-for public types and constructor argument were added to the focused service tests:

```powershell
uv run --project . pytest tests/importing/test_import_service.py -q
```

Collection failed as expected with `ImportError: cannot import name 'StageExecutionContext' from
'generals_replay_analyzer.importing'`. This proved the registration/context seam was absent rather than hidden behind
private `_handlers` mutation.

A second TDD cycle covered the existing typed retry/failure contract at the same public package boundary:

```powershell
uv run --project . pytest `
  tests/importing/test_import_service.py::test_registered_stage_uses_existing_typed_failure_and_retry_semantics -q
```

Collection failed with `ImportError: cannot import name 'StageFailure' from
'generals_replay_analyzer.importing'`. The GREEN change exports the existing type; it does not add a second failure or
job system.

### Implemented seam and direct evidence

- Added frozen `StageHandlerRegistration`, `StageExecutionContext`, and `StageDependencyOutput` contracts plus the
  recursive `FrozenJSONValue` type and callable `StageHandler` protocol; all are exported from `importing.__init__`.
- Constructor registration is copied once and held behind an immutable mapping. Unknown stages, version mismatches,
  duplicate future-stage registrations, and every Task 3 built-in stage override fail with stable validation codes.
- Registration accepts only the existing closed DAG stage/version contracts. Built-in discovery, hash, copy/reference,
  parse, and optional telemetry handlers are unchanged.
- `run_available()` still uses the same coordinator claim, lease ownership, retry delay, dependency projection,
  completion, and transaction methods. Only registered future stages are adapted from `ClaimedJob` to the frozen
  public context.
- Direct dependency rows are loaded after claim, checked succeeded, sorted by closed stage order/version/public ID,
  converted to public IDs plus deeply frozen canonical outputs, and detached before the handler runs.
- Handler results retain the existing mapping contract but are canonical-JSON round-tripped before persistence, so a
  caller cannot mutate nested returned state into a durable output after the boundary.
- Direct tests prove parse/telemetry dependency ordering, immutable context and nested artifacts, parser-only omission
  of absent telemetry, failure-output non-leakage, typed nonretryable handler failure, construction-list mutation
  isolation, validation rejection, and the unchanged default in which `import_observations` remains pending.

### Fix-round gates

- Focused importing coverage: `41 passed, 1 skipped`; `894` statements, `78` missed, `91.28%` coverage.
- Broad offline suite: `651 passed, 1 skipped, 238 deselected in 84.56s`.
- Installed-wheel smoke: `2 passed in 16.37s`.
- Ruff: `All checks passed!`.
- Strict mypy: `Success: no issues found in 40 source files`.
- `git diff --check`: exit 0.
- Import-boundary prohibited-import scan: no engine runner/config/result or private telemetry reader/catalog/map import.
- Diff scope from `bda3c69c8e4a22fc74ae642dc9124c508f645295`: only `importing/__init__.py`,
  `importing/service.py`, and `tests/importing/test_import_service.py`.
- The five inherited C++/audio/CMake paths remained untouched and unstaged.

Fix commit: `77bef09a5addf793c6f87fbfe324d53aa84239c3`

Subject: `fix(replay): Expose future stage handlers`

## Review fix round 3/5: telemetry bundle descriptors and dependency-bound observation identity

Date: 22 August 2026

### Entry-gate defect and approved boundary

Task 4 proved that SHA-only telemetry manifests could not reconstruct the validated trace/catalog/map bundle through
the public reader, and that the precreated `import_observations` key could not distinguish the dependency evidence
selected after parse/telemetry completion. This amendment remains Task 3-owned: it preserves a safe logical artifact
descriptor in the existing telemetry output and materializes the existing future job before claim. It adds no reader,
engine adapter, observation importer, database table, migration, or second job system.

### RED evidence

The focused service file was extended before production changes with descriptor, unsafe-topology, final-identity, and
coalescing regressions:

```powershell
uv run --project . pytest tests/importing/test_import_service.py -q
```

Observed RED: `22 failed, 27 passed, 1 skipped in 19.50s`. Failures proved that manifests lacked `logical_path`, no
safe-path validator existed, traversal/outside/case-collision artifacts were accepted, the frozen context lacked the
materialized idempotency identity, logical topology did not bind the observation key, and converged provisional rows
both executed.

A separate diagnostic-root regression then reported `1 failed in 0.81s`, proving an acquisition diagnostic could
persist the private bundle root before deterministic redaction was added.

### Implemented amendment and direct evidence

- Telemetry validation now inspects every supplied path as an ordinary non-reparse file before canonicalization,
  rejects physical duplicates, and validates slash-normalized product-relative logical descriptors with no empty,
  absolute, drive/colon, backslash, NUL, dot, dot-dot, empty-segment, trailing-separator, or Windows-casefold collision.
- A trace-bearing artifact uses the trace parent as its logical root. Catalogs retain the validated
  `game-data-catalog-v1-<sha256>.json` basename, and map members retain exact
  `map-assets-v1|v2/<content-sha256>/<member>` topology. Every supplied success artifact must resolve strictly beneath
  that root without alias/traversal.
- Trace-less failures retain deterministic closed role descriptors (`catalog.json`, `outcome.json`, `stdout.log`,
  `stderr.log`, and `map-assets/<content-sha256>.asset`). Manifest ordering is deterministic by case-folded logical
  path, kind, and content hash; existing public asset/kind/hash/size fields remain intact.
- Validation completes before any artifact store or metadata write. Direct traversal, outside-root, case-collision,
  duplicate, symlink, and synthetic Windows reparse regressions assert zero telemetry managed assets. Private replay,
  run, artifact paths and roots are deterministically redacted from retained telemetry diagnostics/errors.
- `import_observations` keeps a stable provisional marker in canonical input. Once all direct dependencies are
  succeeded, a short transaction verifies their status and conditionally rebinds only a pending, unleased row to a
  final content key before claim.
- The final projection includes replay SHA, the semantic branch recipe, dependency stage/version/input, and complete
  canonical selected outputs, including telemetry run UUID and the safe artifact topology. ORM/database/job/asset
  public IDs and timestamps are excluded; canonical ordering removes insertion-order dependence.
- The frozen handler context exposes the final idempotency identity plus the existing public replay/stage/version,
  deeply frozen input, and deterministic succeeded dependency snapshots. The key suffix equals the retained
  `selected_dependency_digest`.
- Repeated graph creation resolves the marker-bearing materialized row. If provisional rows converge on one final
  key, the loser is coalesced only while pending/unleased, incoming and downstream edges are preserved, and no
  fabricated failed row is exposed. Tests prove identical topology is stable across independent databases despite
  random public asset IDs, while the same bytes under changed safe topology produce a distinct final key.

Final focused importing suite and required coverage:

```text
63 passed, 1 skipped in 41.85s
TOTAL 1254 statements, 117 missed, 90.67% coverage
```

The coverage count includes the concurrent Task 4-owned `parser_import.py`; Task 3 neither modified nor staged it.

### Fix-round gates

- Broad offline suite: `674 passed, 1 skipped, 238 deselected in 92.06s`.
- Installed-wheel smoke: `2 passed in 16.38s`.
- Ruff: `All checks passed!`.
- Strict mypy: `Success: no issues found in 41 source files`.
- `git diff --check`: exit 0.
- Import-boundary prohibited-import scan: no engine runner/config/result or private telemetry reader/catalog/map import.
- Diff scope from `77bef09a5addf793c6f87fbfe324d53aa84239c3`: only `importing/jobs.py`,
  `importing/service.py`, and `tests/importing/test_import_service.py`.
- The five inherited C++/audio/CMake paths and concurrent Task 4 telemetry/parser-import paths remained untouched and
  unstaged by Task 3.

Fix commit: `74c3b608b6791f1466653a4c28bab80f09a6b0c0`

Subject: `fix(replay): Preserve telemetry bundle topology`

## Review fix round 4/5: terminal dependency evidence policy

Date: 22 August 2026

### Root cause and public contract

Task 4 could not persist failed parser or telemetry attempts through the real DAG because Task 3 projected every
dependent job to `dependency_failed` as soon as any direct dependency failed. The claim path accepted only succeeded
dependencies, the observation materializer waited only for succeeded dependencies, and the public execution context
exposed only succeeded outputs. Failure evidence therefore existed durably on the parser/telemetry job but was
unreachable from the registered observation handler.

The fix adds frozen `TerminalDependencyPolicy(failed_stages=frozenset(...))` to construction-time
`StageHandlerRegistration`. The policy is opt-in, names exact closed direct dependency stages, and is rejected for
unknown, wildcard, mutable/untyped, or non-direct stages. Default registrations remain success-required. The
coordinator, observation materializer, and claim/context boundary now accept only `failed` dependencies explicitly
named by that policy; pending, running, and retryable-pending dependencies remain non-runnable.

`StageDependencyOutput` now preserves public job/stage/version/status and exactly one evidence form: a deeply frozen
canonical success output, or sanitized failure code/message/details. Absolute paths, Task 2 integer IDs, job
timestamps/leases, and runner-private configuration/result fields are removed; safe logical artifact descriptors and
public managed-asset links remain available. Dependency ordering is deterministic. The materialized observation
identity includes terminal status plus semantic success output or sanitized failure evidence, while excluding public
database/job/asset IDs, so changed failure evidence cannot collide and equivalent failures remain stable.

The minimum Task 4 consumer amendment records a failed parser dependency as a new failed `ParserRun` shell with a
quality issue and no parser children. A failed telemetry dependency with a retained artifact manifest is converted to
the existing public `TelemetryAttempt`, producing one failed `TelemetryRun`, retained safe assets/diagnostics,
`exporter_failure`, and zero telemetry observation children. Persisting expected terminal evidence is a successful
`import_observations` execution; successful dependencies keep their prior validation behavior.

### RED and GREEN evidence

The direct Task 3 tests were written first. Initial collection RED:

```text
ImportError: cannot import name 'TerminalDependencyPolicy' from 'generals_replay_analyzer.importing'
```

After adding only the typed public skeleton and an inert coordinator argument, the combined direct suite produced the
behavioral RED required by the review:

```text
5 failed, 59 passed, 1 skipped in 31.33s
```

The failures proved that opted-in parse/telemetry terminal dependencies still never ran, failure evidence did not
materialize an identity, invalid policies were accepted, and the coordinator still rejected an allowed terminal
failure. Tests also cover unchanged default auto-failure, mixed success/failure context, deterministic ordering,
deep immutability and path/timestamp redaction, changed-failure identity, existing converged-job coalescing, and the
pending/running/retryable-pending exclusion.

The real Task 3 `ImportService` plus Task 4 `ObservationImportHandler` tests were then added before Task 4 source was
changed. Their RED was:

```text
2 failed in 2.53s
```

The telemetry case showed no `TelemetryRun` was created because the handler treated failed output as absent. The
parser case showed `import_observations` failed because no succeeded parser output existed. After the minimum consumer
change, targeted GREEN was `2 passed in 2.43s`.

Final focused importing GREEN:

```text
86 passed, 1 skipped in 43.44s
```

The real telemetry failure test proves one exact failed run UUID, `runner_status=invalid_trace`, retained
`outcome.json`/`stdout.log` managed-asset links, exact `exporter_failure`, and zero `TelemetryEvent`, `Entity`,
`EntitySample`, `ProductionEvent`, `EconomyEvent`, `CombatEvent`, or telemetry evidence children. The parser failure
test proves one failed `ParserRun`, `parser_failure`, and zero `ReplayCommand`, `ReplayPlayer`, or parser evidence
children. In both cases the observation job succeeds only after the expected failure evidence is committed.

### Verification gates

- Scoped importing coverage: `86 passed, 1 skipped in 57.85s`; `1913` statements, `184` missed, `90.38%` total.
- Affected Task 4 parser/telemetry/reader suite: `55 passed in 11.77s`.
- Broad offline suite: `697 passed, 1 skipped, 238 deselected, 5 warnings in 103.61s`.
- Installed-wheel smoke: `2 passed in 16.14s`.
- Ruff: `All checks passed!`.
- Strict mypy: `Success: no issues found in 43 source files`.
- `git diff --check`: exit 0; only inherited Windows line-ending notices.
- Import-boundary prohibited-import scan: no engine runner/config/result or private telemetry reader imports.
- Diff scope contains only the Task 3 public export/coordinator/service and their two tests, plus the minimum Task 4
  parser/handler/integration paths listed below.
- The five inherited C++/audio/CMake paths remain untouched and unstaged.

### Changed fix paths

- `scripts/replay_analyzer/src/generals_replay_analyzer/importing/__init__.py`
- `scripts/replay_analyzer/src/generals_replay_analyzer/importing/jobs.py`
- `scripts/replay_analyzer/src/generals_replay_analyzer/importing/service.py`
- `scripts/replay_analyzer/tests/importing/test_import_service.py`
- `scripts/replay_analyzer/tests/importing/test_jobs.py`
- `scripts/replay_analyzer/src/generals_replay_analyzer/importing/parser_import.py`
- `scripts/replay_analyzer/src/generals_replay_analyzer/importing/telemetry_import.py`
- `scripts/replay_analyzer/tests/importing/test_telemetry_import.py`

Fix commit: `4d380176edf25ff878ecb565e9ddb6aa3d21d319`

Subject: `fix(replay): Preserve terminal dependency evidence`

## Review fix round 5/5: settled terminal evidence and crash-idempotent failed attempts

Date: 22 August 2026

### Residual contract and implementation

This final Task 3 review round closes the three remaining terminal-evidence defects without changing the accepted
closed `TerminalDependencyPolicy`, the default success-required branch behavior, the immutable public context, or the
real-DAG materialized identity.

- A retryable failure is now terminal only after its attempt budget is exhausted. `fail()` settles the exhausted row
  as `failed + retryable=False`; claim, default dependency projection, observation materialization, direct context
  construction, dependency output, and dependency identity all use the same settled predicate. Pending retries retain
  the existing deterministic backoff and cannot supply terminal evidence.
- Every post-acquisition telemetry artifact validation failure is normalized to a typed, versioned, canonical failure
  envelope. It carries only safe run/status/quality/scope/exit/engine/hash/diagnostic facts and a deterministically
  sorted manifest of already registered managed content. Supplied paths are never retained; all known replay/artifact
  spellings are redacted from safe text. Copying registers each completed object immediately, so a later copy failure
  can link only verified completed content. An empty prefix yields an empty manifest.
- The Task 4 consumer strictly validates the envelope type, version, exact field set, UUID/hash/scalar types,
  immutable diagnostics, canonical descriptor order, safe logical paths, registered metadata, managed path, and bytes
  before persisting retained links. A valid failure envelope creates one failed `TelemetryRun`, exact deterministic
  quality issues, and zero observation children. Tampered/unregistered descriptors create no run shell.
- The final `import_observations` idempotency key is persisted in failed parser metadata and telemetry settings.
  Telemetry settings also bind the complete immutable attempt manifest and upstream failure code/message/issue.
  Replaying the same key reuses only an exact completed childless failed shell with exact diagnostics and quality
  issues. A different key or evidence rejects telemetry UUID reuse and creates a distinct parser failure history.
  Local parser/telemetry validation failures use the same recovery rule; successful cache matching remains exact.

### RED and GREEN evidence

#### 1. Retryable-to-exhausted terminal transition

The first focused RED proved that exhausted retryable work remained retryable and that status-only checks admitted it
to terminal context/materialization:

```powershell
uv run --project . pytest `
  tests/importing/test_jobs.py::test_retryable_dependency_settles_nonretryable_only_when_attempts_are_exhausted `
  tests/importing/test_jobs.py::test_terminal_failure_policy_rejects_failed_dependency_still_marked_retryable `
  tests/importing/test_import_service.py::test_exhausted_retryable_parse_materializes_stable_terminal_context_and_identity `
  tests/importing/test_import_service.py::test_retryable_failed_dependency_cannot_materialize_or_enter_direct_context -q
```

```text
4 failed in 3.50s
```

After the settled predicate was applied, the same command was GREEN:

```text
4 passed in 2.16s
```

A subsequent audit found default failure projection was the one remaining status-only path. Its direct regression was
captured RED and then GREEN:

```powershell
uv run --project . pytest `
  tests/importing/test_jobs.py::test_terminal_failure_policy_rejects_failed_dependency_still_marked_retryable -q
```

```text
RED:   1 failed in 0.84s (`strict-import` was wrongly projected to `failed`)
GREEN: 1 passed in 0.48s
```

#### 2. Typed artifact-validation/copy failure evidence

The real-DAG validation and mid-copy tests were added before the service/consumer changes:

```powershell
uv run --project . pytest `
  tests/importing/test_telemetry_import.py::test_real_dag_artifact_validation_failure_retains_typed_attempt_and_zero_children `
  tests/importing/test_telemetry_import.py::test_real_dag_mid_copy_failure_links_only_completed_asset_and_zero_children -q
```

```text
RED:   2 failed in 3.56s (the observation job failed and no failed TelemetryRun was retained)
GREEN: 2 passed in 3.26s
```

The accepted runner-status failure path remained GREEN:

```text
4 passed in 2.98s
```

Strict envelope/adversarial and retained-content checks then exposed missing scalar/descriptor validation,
unregistered-link validation, and local failed-attempt replay:

```text
RED:   10 failed, 15 passed in 9.53s
GREEN: 26 passed in 8.03s
```

Canonical descriptor ordering had its own RED/GREEN:

```text
RED:   1 failed in 0.35s (an unsorted typed manifest was accepted)
GREEN: 1 passed in 0.17s
```

Registration, unreadable-file, non-file, and byte-identity tamper checks are GREEN:

```powershell
uv run --project . pytest `
  tests/importing/test_telemetry_import.py::test_typed_failed_attempt_revalidates_every_retained_managed_asset -q
```

```text
4 passed in 1.93s
```

#### 3. Crash/lease replay idempotency

The real handler crash-window tests first failed because neither failed parser metadata nor telemetry settings stored
the final job key:

```powershell
uv run --project . pytest `
  tests/importing/test_telemetry_import.py::test_parser_failure_handler_replay_after_lease_expiry_reuses_exact_attempt `
  tests/importing/test_telemetry_import.py::test_telemetry_failure_handler_replay_after_lease_expiry_reuses_exact_attempt -q
```

```text
RED:   2 failed in 2.23s (`import_observations_idempotency_key` was absent)
GREEN: 2 passed in 2.12s
```

The tests simulate the handler committing its failed run, settlement being lost, lease expiry/backoff, and a second
handler execution. The replay settles with one stable attempt. Changed parser evidence/key creates a new parser run;
changed telemetry key/message/diagnostics or tampered quality facts rejects the immutable telemetry UUID. Local Task 4
parser/telemetry validation failures also reuse only the same key and retain no partial children.

### Final verification gates

Focused importing:

```powershell
uv run --project . pytest tests/importing -q
```

```text
116 passed, 1 skipped in 56.27s
```

Scoped importing coverage:

```powershell
uv run --project . pytest tests/importing `
  --cov=generals_replay_analyzer.importing --cov-fail-under=90 -q
```

```text
116 passed, 1 skipped in 81.20s
TOTAL 2223 statements, 222 missed, 90.01% coverage
Required test coverage of 90% reached.
```

The coverage gate was itself observed RED twice while expanding the contract: first `89.77%`, then `89.79%` after
the strict consumer grew. No exclusions or threshold changes were used; retained-content behavior tests closed the
final five-line gap.

Affected Task 4 parser/telemetry/public-reader contract:

```powershell
uv run --project . pytest `
  tests/importing/test_parser_import.py `
  tests/importing/test_telemetry_import.py `
  tests/telemetry/test_telemetry_v2_contract.py -q
```

```text
78 passed in 21.46s
```

Broad offline regression:

```powershell
uv run --project . pytest -m "not engine and not ollama" -q
```

```text
727 passed, 1 skipped, 238 deselected in 117.69s
```

Installed-wheel smoke:

```powershell
uv run --project . pytest tests/test_wheel.py -q
```

```text
2 passed in 16.56s
```

Static and repository gates:

```text
Ruff:        All checks passed!
Strict mypy: Success: no issues found in 43 source files
diff check:  exit 0 (only inherited Windows line-ending notices)
boundary scan: no engine runner/config/result or private telemetry reader/catalog imports
```

The single importing/offline skip is the pre-existing real Windows symlink/reparse test on a host where link creation
is unavailable; deterministic synthetic reparse coverage remains GREEN.

### Final owned scope

- `scripts/replay_analyzer/src/generals_replay_analyzer/importing/jobs.py`
- `scripts/replay_analyzer/src/generals_replay_analyzer/importing/service.py`
- `scripts/replay_analyzer/src/generals_replay_analyzer/importing/parser_import.py`
- `scripts/replay_analyzer/src/generals_replay_analyzer/importing/telemetry_import.py`
- `scripts/replay_analyzer/tests/importing/test_jobs.py`
- `scripts/replay_analyzer/tests/importing/test_import_service.py`
- `scripts/replay_analyzer/tests/importing/test_parser_import.py`
- `scripts/replay_analyzer/tests/importing/test_telemetry_import.py`
- this Task 3 report

No migration, schema baseline, dependency/lock, runner/C++, or later-task file changed. The five inherited
C++/audio/CMake worktree paths remain untouched and unstaged.
