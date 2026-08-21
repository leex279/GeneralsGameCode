# Task 9 Report: Isolated Headless Telemetry Runner

Date: 21 August 2026

## Final outcome

Implemented the product-owned Python runner and public `replay-analyzer export-telemetry` command for one isolated,
headless Zero Hour replay execution. Every run receives a canonical UUID directory below
`<data_root>/runs/<run-id>/`; directories are exclusively created, never reused, never cleaned after failure, and
preflighted against the longest actual Task 8 ANSI transaction path before the engine is launched. Every engine-bound
path must convert exactly through the active Windows code page without best-fit/default substitution and fit
MAX_PATH by encoded bytes.

The runner accepts only strict immutable `EngineRunConfig` values. Executable and replay inputs must be absolute,
already resolved, ordinary files with no symlink/junction/reparse component. Timeout and movement intervals are
strict bounded integers. Win32 device names, forbidden characters, trailing-dot/space aliases, device namespaces,
and unresolved parent aliases fail before any product output is created. The default root is
`%LOCALAPPDATA%/GeneralsReplayAnalyzer`, while every test and real integration injects a temporary root.

## Launch and process containment

The exact explicit argument vector is:

`generalszh.exe -headless -noaudio -replay <input> -telemetry <trace> -telemetry-run-id <uuid>
-telemetry-movement-frames <N> -replay-outcome <outcome>`

The process working directory is the executable directory so retail runtime files resolve without copying or
modifying Steam data. Standard output and error are opened exclusively and their identity-stable handles remain owned
by the runner through launch; missing, replaced, reparse, or hardlinked logs are never exposed publicly. No shell is
used.

The process launcher is injected for unit tests. Production Windows execution uses `CreateProcessW` in suspended
state, exact `ctypes` signatures, explicitly wired standard handles, and a kill-on-close Job Object assigned before the
main thread is resumed. Child ownership is recorded immediately after successful creation, before any fallible handle
reset. Timeout calls `TerminateJobObject` and polls `QueryInformationJobObject` until the whole job reports zero active
processes; normal root exit also terminates and settles any remaining descendants. If Job assignment fails, the
still-suspended child is terminated directly, when it cannot yet have descendants. Every terminate/wait result is
checked, and handles and inheritable state are closed on every path. The POSIX development fallback creates a new
session and uses `killpg`; it makes no Windows PID-race claim.

## Metadata and evidence boundary

Before launch, canonical `request.json` is published atomically and binds:

- replay path, size, and SHA-256;
- engine path, size, SHA-256, and nullable version;
- exact argv, working directory, no-shell flag, timeout, movement interval, run ID, and UTC request time.

Replay and engine identities are rechecked after the process settles. `result.json` is atomically published last and
contains the request hash, UTC start/finish timestamps, duration, exit code, typed status, process-tree termination
facts, evidence quality/scope, public paths, and stable diagnostics. The runner never copies, rewrites, restores, or
deletes caller replay/engine bytes.

`result.json` and its transaction name are caller-preserving. If either fixed name is already occupied, those bytes
remain intact and the typed result is atomically written to the first exclusive `runner-result-<index>.json` fallback
rather than escaping as an invalid request.

The closed result statuses distinguish success, validated CRC-boundary evidence, startup input/header/truncation,
playback truncation/interruption, timeout, nonzero engine failure, missing/invalid trace, writer error,
missing/invalid/mismatched outcome, invalid assets, launch failure, changed input, and unsafe output. Process exit is
not trusted as evidence: the independent outcome and fully buffered trace are validated and cross-bound first. A
clean trace/outcome paired with a nonzero exit remains a typed engine failure but retains its strictly validated paths;
a CRC outcome remains `valid_crc_mismatch` even with the expected nonzero exit.

Only `success` is full-match strategy evidence. A validated CRC, playback truncation, or interrupted trace is explicit
partial evidence with `observed_boundary_only` eligibility. Every other result has no strategy-analysis eligibility.

## Independent outcome and asset validation

Added a strict frozen Python model/loader for replay outcome schema version 1. It requires one single-link ordinary
UTF-8 file, one newline-terminated JSON record, no duplicate/extra fields or non-standard numbers, bounded integer
facts, closed terminal reasons, playback/startup coherence, and exact CRC reason/frame coherence. The engine outcome
producer now includes `schema_version: 1`; this was the only C++ source change required by the strict runner.

After process settlement the runner rejects unexpected root files, transactions, hardlinks, or reparse outputs. It
then buffers the complete telemetry trace through the Task 1-8 reader, which validates sequence, counts, digest,
terminal completion, semantic game-data catalog, and the complete Task 8 map asset. The run ID and requested movement
interval are independently rebound to manifest zero. Outcome final frame, command count, terminal reason, CRC flag,
and mismatch frame must exactly equal telemetry completion. The run must contain the sole referenced catalog and sole
referenced map content directory; unexpected nested map transactions are rejected and preserved.

No trace, catalog, or map path is public before all of these checks pass. A strictly loaded startup outcome may be
exposed without a trace because no playback trace is expected. Failed run directories, request/result metadata,
outcome when valid, stdout, stderr, and rejected artifacts remain available for diagnosis.

## CLI and wheel

`replay-analyzer export-telemetry <replay> --engine <generalszh.exe>` supports optional `--data-root`, `--timeout`,
and `--movement-sample-frames`. It emits one compact sorted JSON object and stable exit classes: success `0`, invalid
request `2`, validated partial `3`, startup input/header/truncation `4`, process/input failure `5`, and evidence validation failure
`6`. Failure JSON never publishes unvalidated trace/catalog/map paths.

Engine-only imports are lazy, so the established `inspect` command and no-dependency installed-wheel inspection smoke
remain unchanged. The installed wheel exposes the new command help and packages every prior schema/catalog asset.

## TDD and debugging evidence

Development began with a collection RED (`ModuleNotFoundError` for the new engine package). The first implementation
had 18 focused failures, which isolated strict Windows `Path` typing, newline canonicalization, and ANSI path-preflight
fixture length. The initial runner matrix reached 23 passing tests. A new timeout contract then failed until a timed-out
launcher result was required to prove whole-tree termination. CLI tests began with three expected failures before the
new command existed.

The first pinned real runner execution produced `invalid_outcome` with the exact missing `schema_version` diagnostic.
Adding the minimal version field to the independent C++ producer made the real integration pass. Later RED cases found
and fixed unsafe Win32 aliases, engine-binary mutation, unexpected nested map outputs, and CLI alias resolution. The
first full non-engine run found one installed-wheel regression: eager Pydantic import broke the dependency-isolated
inspection smoke. Lazy engine imports fixed that boundary without changing inspection behavior.

The independent read-only review found seven release blockers. Focused RED cases and source guards now cover: recording
the suspended child before a fallible inheritable-handle reset; terminating and polling the whole Job Object to zero;
strict active-code-page bytes for every engine-bound path; separate startup `truncated_input` and playback
`replay_truncated` statuses/exit classes; caller-preserving result-name fallback; identity-stable mandatory log handles;
and rejection of startup outcomes accompanied by playback artifacts. The first attempt to wait directly on an empty
Job Object caused the real runner test to remain active; systematic debugging showed that was not a valid empty-job
signal. The corrected code polls authoritative Job accounting after checked termination, and the complete runner plus
real replay returned green.

Direct Windows behavioral tests additionally prove that a fallible handle-reset path cannot resume or orphan the
suspended child and that a timed-out launcher settles a spawned descendant tree before returning: **2 passed in
2.54 s**. The final complete runner/CLI/wheel gate after review hardening is **56 passed, 1 honest host symlink skip in
16.72 s**.

## Real replay evidence

The established Steam-runtime hardlink fixture ran the pinned unmodified Zero Hour 1.04 replay through the public
runner with a temporary product root. The result is `valid_crc_mismatch`, not success:

- final playback frame: **108**;
- executed replay commands: **16**;
- first CRC mismatch frame: **105**;
- outcome/trace terminal reason: `crc_mismatch`;
- strategy scope: `observed_boundary_only`;
- one strict game-data catalog and all six Task 8 map files validated.

`request.json`, `result.json`, `stdout.log`, and `stderr.log` were present; independent outcome and telemetry completion
were exactly cross-bound. The original replay SHA-256 remained unchanged and a before/after snapshot proved the user
Zero Hour Replays directory was not written.

## Final verification

- complete non-engine suite after review fixes: **515 passed, 196 engine tests deselected** in **32.35 s**;
- complete engine-marked suite: **182 passed, 4 honest host-capability skips, 512 deselected** in **334.37 s**;
- affected Task 7/8 engine regression files: **72 passed, 1 host skip** in **199.26 s**;
- direct Windows orphan/descendant behavioral tests: **2 passed** in **2.54 s**;
- post-review runner/CLI/wheel gate including the pinned real replay: **56 passed, 1 host skip** in **16.72 s**;
- Ruff over `src` and `tests`: **passed**;
- strict mypy: **passed, 24 source files**;
- installed wheel build/help/inspection/telemetry smoke: **passed**;
- `git diff --check`: **passed**;
- modern VS 2022 x86 Zero Hour Release build/link: **passed**; and
- modern VS 2022 x86 Zero Hour Debug build/link: **passed**.

The complete engine-marked suite preceded the final Python-only independent-review hardening. Its affected surface was
then verified by the complete runner/CLI suite, including the production Windows launcher and pinned real integration;
the long unrelated engine suite was not duplicated because no engine source or telemetry contract changed. The full
non-engine suite and lint/type gates were rerun after all review fixes.

Actual VC6 and MinGW compilers remain unavailable on this host, so no compiler-pass claim is made for either. The
single engine-source adjustment stays inside the already modern-only ReplayOutcome target and both available modern
x86 configurations compile it. Symlink/reparse tests skip only when the Windows host denies creation; hardlink,
canonical-path, output-collision, and reparse-aware validation remain covered. The five inherited stat-only worktree
paths have zero content delta and were neither edited by Task 9 nor staged.
