# Task 7 Report: Orders, Engine State, and Bounded Movement Telemetry

Date: 21 August 2026

## Final outcome

Implemented and source-verified the Task 7 order, state-transition, and movement-sampling layer for the modern
Zero Hour replay analyzer. The implementation is passive and deterministic: it reads replay-dispatched engine state,
copies scalar/string/ID observations, emits telemetry, and never mutates `GameLogic`, invokes RNG, or reads client
state. It is compiled only under `RTS_REPLAY_ANALYZER && !IS_VS6_BUILD`; the base `Generals/` target and frozen
telemetry v1 schema remain unchanged.

The previously blocked real-replay gates now pass after host storage was restored. Natural CRC behavior, full
CRC-stripped mechanics playback, deterministic interval-15 traces, interval-15/30 density isolation, bounded moving
gaps, telemetry-on/off outcome parity, writer failure cleanup, the full engine suite, and the original replay hash are
all verified below.

## Authoritative order seam and closed coverage

`GameLogic::logicMessageDispatcher` calls `ReplayMovementSampler::observeResolvedOrder` exactly once after the full
dispatch switch and the existing destroyed-`AIGroup` guard. The helper re-resolves every selected ID and object
target through current `GameLogic`, rejects destroyed/unowned/duplicate selected objects, and ensures Task 4
`object_created` observations precede all references. It preserves:

- `GameLogic::getFrame()` as both the order envelope and command frame;
- the authoritative `getMessagePlayer` result;
- numeric command ID plus the exact `GameMessage::getCommandAsString()` name;
- selected object IDs in `AIGroup::getAllIDs()` source order, with current template identities;
- a nullable current/live object target and its template, or the raw command location for location commands; and
- the handlers' exact `CMD_FROM_PLAYER` numeric/name source.

The source audit deliberately exports a closed subset rather than implying complete dispatch coverage:

- combat drop at location/object;
- attack object, force-attack object, and force-attack ground;
- get repaired/healed, repair, resume construction, enter, and dock;
- move, attack-move, force-move, add waypoint, guard position/object, stop, scatter, and salvage; and
- create formation.

The manifest lists all 21 supported numeric/name/target-argument identities in strictly numeric order. It records the
single post-resolution seam, frame and player sources, live-reference policies, and historical provenance. Sample
`current_order_*` fields are explicitly declared as the last supported post-dispatch order reference and **not** as
engine execution state. Diagnostics, CRC, camera, selection/team bookkeeping, cheats, production, weapons, special
powers, and every other dispatch family remain outside the advertised subset. Unsupported messages are ignored under
that closed capability; a supported command with unknown name/type/data fails telemetry closed.

## State classification policy

`entity_state_changed` is emitted only when copied direct engine facts actually transition at the end-of-update seam.
It preserves before/after:

- raw AI state ID and stable `AIStateType` enum name where exposed;
- raw locomotor-set ID, stable `LocomotorSetType` enum name where exposed, and explicit unavailable/unknown status;
- the direct engine-moving flag; and
- source-grounded classification plus the last supported order reference.

Classification precedence is direct only: object disabled, enclosing garrisonable container, `AIUpdate::isAttacking`,
the exact guard AI states, `AIUpdate::isMoving`, and `AIUpdate::isIdle`. All other states are explicit `unknown` with
raw AI/locomotor evidence. No collecting or returning state is inferred. `object_destroyed` from Task 4 remains the
only terminal lifecycle event; Task 7 does not duplicate destruction semantics.

## Deterministic bounded sampler

`ReplayMovementSampler` runs after simulation updates, destruction processing, weapon/locomotor updates, and victory
conditions, before the logic frame increments. It collects live object IDs, sorts them numerically, then re-resolves
each ID. Retained state contains no `Object *`; reset/reconfigure clears all copied observations, and every copied
state/order/forced-sample map prunes missing or destroyed IDs independently.

For each live entity the payload preserves raw finite float32 position/orientation, exact layer ID plus stable name or
explicit dynamic/unknown status, physics velocity magnitude or nullable unavailable provenance, direct state facts,
last supported order reference, and path-tail goal or explicit unavailable status. Coordinates remain raw and
unclamped for Task 8.

New entities receive a same-frame lifecycle baseline; a same-frame state/order event may supply the more specific
forced reason. State and supported-order events force a same-frame sample. Enabled
mobile non-structures coalesce intervening pose changes until the configured interval, then emit a changed sample;
engine-moving entities also emit an exact-interval heartbeat even when the payload is otherwise unchanged. Structures
never receive periodic or change-poll samples, only lifecycle/state/order-forced samples. The default interval is 15;
the established CLI rejects missing, malformed, non-positive, duplicate, telemetry-less, and now values above 3600
before playback.

## Telemetry-independent outcome parity

The opt-in modern-analyzer `-replay-outcome <absolute.json>` channel makes telemetry-disabled replay termination facts
independently observable without enabling any telemetry event producer. It counts the same successfully decoded
commands handed to `GameLogic`, receives the same first authoritative CRC mismatch frame, and finalizes from the same
clean/CRC/truncated/interrupted recorder boundary. It emits only final frame, command count, terminal reason, CRC flag,
and nullable CRC frame. Publication uses an exclusive temporary file and no-replacement move; invalid, relative,
duplicate, existing, or telemetry-aliased destinations fail before playback.

The outcome channel is passive, deterministic, modern-only, and disabled by default. It does not query client state,
mutate `GameLogic`, or enable the telemetry writer. Its tests compare separate telemetry-off and telemetry-on outcome
files, so telemetry is never used as the source of the disabled-run baseline.

## Contract and atomic-reader validation

The canonical telemetry-v2 schema, version-aware Pydantic records, installed-wheel schema contract, event registry,
and atomic reader now cover the three Task 7 event types and manifest coverage. Frozen telemetry v1 remains byte
unchanged at SHA-256 `BFAA0279A9BAA9264121709063F0DD763C534333926CBC2846DD4E4D56761350`.

Before exposing any buffered record, v2 validation now checks:

- manifest movement interval bounds and closed supported-command metadata;
- numeric/name correspondence against the packaged Zero Hour 1.04 command catalog;
- authoritative source player membership in the full initialized engine-player domain;
- current/live selected and target references, source-order identity, ownership, and template continuity;
- contiguous historical order IDs and coherent later order references;
- actual before/after raw engine transitions and full state/sample coherence;
- finite producer-serialized float32 bounds for coordinates, orientation, speed, and path goals;
- layer, speed, AI-name, locomotor-name, and path-goal value/provenance agreement;
- monotonic Task 7 frames, same-frame forced samples, duplicate coalescing, exact moving heartbeats, adjacent and
  terminal moving-sample interval bounds; and
- no state/sample after authoritative destruction, plus recomputed event counts and completion hash.

Malformed state-name provenance, guessed locomotor names, missing forced samples, post-destroy observations,
incoherent current state/order, duplicate samples, and unbounded moving tails fail the entire trace atomically.

## TDD and review evidence

The initial focused Task 7 selection was captured RED at **14 failed** because coverage metadata, strict payloads,
reader state, and sampler seams did not exist. The first implementation brought that selection to **14 passed**.

Corrective self-review captured additional RED evidence before each minimum correction:

- terminal moving gaps were accepted: **1 failed**, then **1 passed** after end-of-trace interval validation;
- stable AI-name provenance with no exposed name was accepted: **2 failed**, then the malformed set passed;
- a state transition's raw engine-moving flag could contradict its forced sample: **1 failed**, then the malformed set
  reached **10 passed** after full overlapping state coherence;
- ID-only order/forced maps were not independently pruned if an object disappeared before its first sample:
  **1 failed**, then **1 passed** after lifecycle-safe pruning; and
- stable locomotor identities are now carried and guessed names rejected;
- missing, partial, or target-index-drifted manifest order coverage was accepted before exact canonical equality was
  enforced in both Pydantic and atomic-reader validation;
- historical v1 records were accidentally exposed to the new v2 order-coverage rule: the preserved v1 regression
  failed once, then passed after restoring the version boundary;
- stable status could pair an unmapped AI or layer ID with a null name: **2 failed**, then the malformed selection
  reached **15 passed** after exact catalog membership was required; and
- an order-selected object destroyed later in the same frame left an impossible pending forced sample: **1 failed**,
  then passed after authoritative destruction retires copied order/state/sample obligations;
- v1 extension payloads whose names collide with new typed v2 fields were narrowed by Pydantic before the version
  boundary: the regression was extended with arbitrary colliding values, then passed after v1 wrap validators restored
  those values unchanged;
- traces could omit the producer-required lifecycle baseline: **1 failed**, then passed after the atomic reader began
  requiring a same-frame sample for every surviving creation;
- reader interval enforcement treated structures, disabled objects, and non-mobile objects as periodic candidates even
  though the producer deliberately excludes them: the malformed/valid set now passes with the reader using the same
  explicit interval-eligibility predicate; and
- destruction discarded the last sample before checking a moving entity's terminal interval tail: **1 failed**, then
  passed after the bound is validated before authoritative lifecycle cleanup; and
- sample reasons were not tied to the producer's actual forcing and interval decisions: **4 failed**, then passed
  after validation enforced `state_forced` over `order_forced` over the creation baseline and required changed versus
  heartbeat provenance to match eligibility, payload equality, engine movement, and interval timing; and
- change detection incorrectly included emitted lifecycle ownership even though `EngineSampleSnapshot::sameSample`
  does not: **2 failed**, then passed after reader comparison was narrowed to exactly the snapshot fields, preserving
  an unchanged moving heartbeat across a valid owner transfer and rejecting `changed` justified only by that transfer;
  and
- telemetry-disabled final frame, command count, and CRC were not independently observable: the natural parity test
  failed on the missing outcome file, then passed after the opt-in passive atomic outcome channel was connected to the
  same recorder command, CRC, and finalization seams. The initial mechanics comparison also exposed nondeterministic
  wall-clock progress text; tests now exclude only those elapsed-time lines while comparing all deterministic output;
- distinct spellings of one nonexistent telemetry/outcome destination reached playback: **1 failed**, then passed
  after comparison canonicalized the absolute parent, resolved reparse-point parent identity, and appended the common
  basename before a case-insensitive comparison; and
- trace command counting began only after telemetry initialization while the independent outcome counted the full
  active replay transaction: the source-seam regression failed, then passed after both counted the same recorder
  handoff, including pre-initialization frame-zero commands. The real parity tests pass with the corrected count.

Final contract and static evidence:

- Task 7 plus lifecycle contracts: **62 passed**;
- Task 7, v2, lifecycle, and static contract selection: **98 passed, 2 real tests deselected**;
- Task 7 static engine seams: **4 passed, 2 real behavior tests deselected**;
- full non-engine suite: **481 passed**;
- Ruff: **passed**;
- strict mypy: **passed, 18 source files**; and
- `git diff --check`: **passed**; and
- modern VS 2022 x86 `z_generals`: **built and linked `generalszh.exe`** after initializing the x86 VS 2022
  developer environment.

## Real replay and non-interference evidence

The tracked natural replay still exists unchanged:

`scripts/replay_analyzer/tests/fixtures/zero_hour_1_04/leex279_vs_fox27.rep`

SHA-256: `EA085767BFA11D2CFC167D9007173CE2EB29B5F557702FFD042E2E9A1A8F6BB8`

The natural replay is used only for checksum-boundary evidence. Telemetry-off and telemetry-on runs have identical
machine-readable outcome records and return behavior: final frame **108**, first CRC mismatch frame **105**, identical
executed command count, and the expected nonzero CRC return. The trace agrees with that independent outcome, and the
pinned replay SHA-256 remains unchanged.

A disposable CRC-stripped derivative is used only for mechanics/density evidence, never player, winner, or strategy
claims. Its telemetry-off baseline, two interval-15 runs, and interval-30 run all complete cleanly at the same final
frame with identical command count and no CRC mismatch. Evidence:

- interval-15 deterministic traces are byte-identical;
- interval 15 emits **46,086** entity samples and interval 30 emits **28,324**;
- after normalizing only manifest interval, sequence, entity-sample count, and trace hash, every non-sample record is
  byte-identical;
- at least one closed-subset supported order is observed;
- the maximum interval-15 gap from a moving eligible sample to its next sample or authoritative destroy/final boundary
  is exactly **15 frames**; and
- both the original replay and disposable derivative retain their pre-run SHA-256 values.

Console comparison removes only the engine's wall-clock `Elapsed Time:` progress lines, whose values naturally vary
between any two executions; all deterministic console content, stderr, return code, and independent outcome facts are
identical across telemetry-off/on and interval variants.

Outcome-path validation covers missing, relative, duplicate, existing, exact telemetry-alias, and alternate-spelling
telemetry-alias paths: **6 passed**, all before playback. Writer safety evidence covers open failure, exact/hardlink
input alias rejection, injected late failure, destination
creation during publish, temporary candidate exhaustion, and pre-initialization cleanup: **7 passed**. The symlink
alias case is **1 skipped** because this Windows host lacks symlink privilege. The full real/static engine suite is
**67 passed, 1 skipped**. The focused post-review natural/mechanics rerun is **2 passed** with the exact counts above.

## Final review hardening

The reviewed Task 7 commit was followed by a scoped corrective pass for four evidence-integrity findings.

Windows destination identity now fails closed before playback for final components that Win32 can normalize or
reinterpret: trailing dot/space, alternate-data-stream colon, control characters, wildcard/forbidden characters, and
the documented DOS device basenames (`CON`, `PRN`, `AUX`, `NUL`, `CLOCK$`, `COM1`-`COM9`, and `LPT1`-`LPT9`). Replay,
telemetry, and outcome paths use the same final-component policy. Output identity comparison still resolves the
existing parent through `GetFinalPathNameByHandleA`, appends the validated final component, and compares
case-insensitively. Quoted argument edges are preserved only in the analyzer build until validation; normal builds
retain the existing trimming behavior. Exact canonical paths and case-insensitive replay spelling remain accepted.
Trailing-dot/space, mixed-case/dot aliases, ADS, and reserved devices reject before `Simulating Replay`; existing and
late-created destinations remain caller-owned. The outcome late-collision test also proves the replay and writer-owned
temporary cleanup remain unchanged. This policy does not claim to prove privileged final-component symlink aliases;
the symlink test remains skipped on this host because symlink creation privilege is unavailable.

`ReplayOutcome` now opens an attempt after prior-game cleanup but before replay input access. Every tested configured
startup attempt publishes exactly one no-replace JSON outcome:

- missing input: `playback_started=false`, frame/count zero, `input_unavailable`, and no CRC facts;
- full but wrong `GENREP` signature: the same zero facts with `invalid_replay_header`;
- exact short header reads: the same zero facts with `truncated_input`; and
- a valid decoded header/setup with a short first-frame read: the same zero facts with `truncated_input`.

The last case is not marked started: `playback_started` becomes true only after setup, the first command-frame read,
recorder mode, current filename, and advertised frame count are ready. Natural and clean mechanics outcomes now carry
`playback_started=true`. Startup outcome writer failure is diagnostic-only, preserves the replay return/stdout, and
removes any writer-owned temporary file. Configure/begin reset counters without publishing a false successful outcome.

The atomic v2 reader now binds `entity_state_changed.owner_player_index` and every sample owner to Task 4's current
lifecycle owner at that record. Tests cover wrong in-domain owners, valid and stale facts after transfer, nullable
neutral ownership, and post-destroy rejection. A per-frame producer-order validator also requires all supported
`order_issued` records before the end-update state/sample block; numeric object-ID ordering; at most one state and one
sample per object/frame; and state before sample for a common object. Non-Task-7 authoritative facts may precede or
interleave before the sampler block, and terminal outcome/completion may follow it.

Strict TDD evidence for this corrective pass:

- focused reader RED: **5 failed, 36 passed**; final focused reader GREEN: **42 passed**;
- focused real alias/startup RED: **8 failed, 7 passed**; final focused alias/startup GREEN: **15 passed**;
- focused owner/order/static/late-collision selection: **15 passed**;
- natural CRC plus interval-15/30 mechanics gates: **2 passed in 88.72s**;
- natural outcome: playback started, frame **108**, **16** executed commands, CRC mismatch frame **105**;
- CRC-free outcome: playback started, clean frame **56,004**, **2,874** executed commands, no CRC mismatch;
- interval-15 samples: **46,086**; interval-30 samples: **28,324**; all existing determinism, normalized
  non-sample byte identity, and maximum eligible-moving gap assertions pass;
- full non-engine suite: **491 passed, 80 engine tests deselected**;
- full engine suite: **79 passed, 1 symlink-privilege skip, 491 non-engine tests deselected**;
- Ruff: **passed**; strict mypy: **passed, 18 source files**; `git diff --check`: **passed**; and
- modern VS 2022 x86 `z_generals`: **built and linked** after the final C++ refinement.

The pinned natural replay remains SHA-256
`EA085767BFA11D2CFC167D9007173CE2EB29B5F557702FFD042E2E9A1A8F6BB8`; no strategy, player, or winner conclusion is
drawn from the disposable CRC-stripped mechanics derivative.

## Malformed replay startup hardening

The final malformed-input re-review found two memory-safety and startup-settlement gaps in the legacy replay-header
reader. A serialized local index of `-1` passed the header check and was dereferenced later, and the fixed 1024-unit
string buffers could write one element past their bounds for an overlong unterminated field. The string readers also
wrote the EOF sentinel into their buffers and exposed no status that could distinguish a short input from a complete
but invalid overlong field.

The header reader now closes all failures through one cleanup seam. Once `GameInfo` has been entered, that seam ends
and resets it; it always closes the replay source, and a configured analyzer playback attempt atomically settles once
as `input_unavailable`, `truncated_input`, or `invalid_replay_header`. Exact fixed-width read counts and every string
status are checked before the header is accepted. The unused generic `startup_failed` outcome reason was removed, and the source
audit proves that `readReplayHeader` has no independent false-return path outside the central seam. The two remaining
`playbackFile` false returns are the already-settled header failure and the explicitly settled short setup/first-frame
failure.

Both C++98-compatible NUL readers now accept at most 1023 payload units followed by the terminator. EOF before the
terminator reports `truncated_input`; a 1024th nonzero unit proves an overlong field and reports
`invalid_replay_header`. No EOF value is stored and every buffer write is bounded. This safe behavior applies to all
replay-header callers, while outcome publication remains modern-analyzer playback only. Runtime tests prove UTF-16
and ASCII fields at 1023 units plus NUL still decode and play normally, 1024 units plus NUL and 1024 unterminated
units reject as invalid, and immediate or partial EOF rejects as truncated.

The local index parser now accepts only the exact decimal spelling written by `Recorder`, requires the domain
`0..MAX_SLOTS-1`, resolves the slot before use, and requires `GameSlot::isHuman()`. This accepts human observer slots
because observers remain `SLOT_PLAYER`, while rejecting negative, out-of-range, syntactically tailed, AI, open,
closed, or null local slots. Source evidence confirms replay recording is disabled for single-player games, skirmish
records local slot zero, and network replay recording derives an actual local human slot.

Strict TDD and final verification evidence for this pass:

- focused RED: **10 failed, 5 passed**, including an access violation for local `-1`, accepted closed/syntax-tailed
  slots, missing string statuses, and unclassified overlong/EOF inputs;
- focused post-build GREEN: **15 passed**, including local-slot, UTF-16/ASCII boundary, overlong, unterminated, EOF,
  and static return-path cases;
- complete startup, writer-failure, and collision file: **56 passed, 1 symlink-privilege skip**;
- natural CRC and interval-15/30 mechanics rerun: **2 passed**; interval 15 emitted **46,086** samples, interval 30
  emitted **28,324**, and maximum eligible-moving gap remained exactly **15** frames;
- full engine suite: **94 passed, 1 symlink-privilege skip**;
- full non-engine suite: **491 passed, 95 engine tests deselected**;
- Ruff: **passed**; strict mypy: **passed, 18 source files**; `git diff --check`: **passed**; and
- modern VS 2022 x86 `z_generals`: **built and linked `generalszh.exe`** after the final C++ changes.

Every malformed-input test preserves the source replay bytes, requires no writer-owned temporary residue, and checks
the exact passive zero-fact outcome. Natural and interval-density outcomes, command counts, frame/CRC behavior,
determinism, and the pinned replay SHA-256 remain unchanged.

## Fixed-width scalar read validation

The last source review found that the legacy fixed-width timestamp reader declared an uninitialized `replay_time_t`
temporary and assigned it into `ReplayHeader` before its read count was validated. A `GENREP`-only input could
therefore read the uninitialized value even though the central failure seam subsequently classified the replay as
truncated. A one-to-seven-byte timestamp suffix could also commit a partial timestamp before rejection.

All fixed-width replay-header values are now read into initialized C++98-compatible local storage. Start/end time,
frame count, desync/quit/disconnect flags, `SYSTEMTIME`, version number, executable CRC, and INI CRC are copied into
`ReplayHeader` only after exact read counts prove the corresponding block complete. The adjacent playback setup game
mode is likewise staged locally and committed only when the analyzer's complete setup check passes; non-analyzer
builds retain the legacy assignment behavior. The existing central header failure, cleanup, and outcome settlement
seam is unchanged.

Strict TDD and final evidence for this scalar pass:

- RED: **1 failed, 8 passed**; all eight byte-level startup cases already settled as truncated, while the source
  audit exposed the pre-validation assignment;
- focused rebuilt GREEN: **9 passed**, covering `GENREP` only and every one-to-seven-byte timestamp suffix;
- complete startup/outcome/writer suite: **65 passed, 1 symlink-privilege skip**;
- natural pinned replay CRC/non-interference gate: **1 passed**;
- full engine suite: **103 passed, 1 symlink-privilege skip**;
- full non-engine suite: **491 passed, 104 engine tests deselected**;
- Ruff: **passed**; strict mypy: **passed, 18 source files**; `git diff --check`: **passed**; and
- modern VS 2022 x86 `z_generals`: **built and linked `generalszh.exe`** after the scalar change.

Each short-timestamp case publishes exactly one `truncated_input` outcome with `playback_started=false`, zero final
frame and command count, no CRC facts, no access violation, no writer-owned temporary residue, and unchanged replay
bytes. Valid replay bytes and the natural frame-108/CRC-frame-105 behavior remain unchanged.

## Toolchain limitations

VC6 and MinGW remain unavailable on this host, so no compile pass is claimed for either toolchain. Compatibility
evidence is limited to modern-only preprocessor guards, CMake exclusion from VC6, source/static checks, and the modern
x86 build. The five inherited stat-only zero-content worktree paths were not edited or staged.
