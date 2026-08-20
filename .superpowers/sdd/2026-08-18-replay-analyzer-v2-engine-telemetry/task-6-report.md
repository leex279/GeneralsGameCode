# Task 6 Report: Combat and Outcome Telemetry

Date: 20 August 2026

## Outcome

Implemented passive, deterministic, modern-only Zero Hour telemetry for:

- authoritative applied damage and healing health transitions;
- authoritative object veterancy level changes;
- executed player defeat, surrender, and corroborated disconnect transitions;
- replay-header quit, desync, and disconnect metadata without treating metadata as executed state;
- the exact first CRC mismatch frame; and
- exactly one authoritative `match_outcome` immediately before every published v2 `complete` record.

The implementation does not change replay commands, health/veterancy/player state, RNG, client state, gameplay
returns, or the base `Generals/` target. Dedicated C++ helpers and shared call sites are directly guarded by
`RTS_REPLAY_ANALYZER && !IS_VS6_BUILD`; recorder-only code is compiled solely in the modern non-VC6 analyzer source
set. Telemetry return values remain ignored.

## Authoritative seams and available evidence

| Observation | Seam | Evidence carried |
|---|---|---|
| Damage applied | `ActiveBody::attemptDamage`, after armor/scalar/kill/clipping and health mutation, before callbacks and `Object::onDie` | Victim and observed owner; raw historical source object ID; authoritative raw `DamageInfo::m_sourcePlayerMask` and its exact sorted player bits; raw source template from `DamageInfo`; attempted, calculated, and actually applied amount; prior/new health; exact numeric/stable damage and death type pair; raw victim location; killing-blow flag. |
| Healing applied | `ActiveBody::attemptHealing`, after authoritative health clipping, before healing callbacks | Target and owner, raw optional source ID/owner, attempted/calculated/applied amount, prior/new health, and raw target location. |
| Veterancy changed | `Object::onVeterancyLevelChanged`, after the tracker has changed level and the object has applied body/weapon/upgrade state | Object/current owner plus prior/new numeric level and stable level name. |
| Player defeated | `Player::killPlayer` at the actual active-to-dead assignment, scoped only by `VictoryConditions::update` or `ScriptActions::doPlayerKill` | Player, prior/new status, exact victory-condition or script source, and replay slot when one exists. |
| Player surrendered | The same `killPlayer` transition scoped by executed `GameLogic::onSelfDestruct(TRUE)` | Player, active-to-surrendered status, exact executed-true source, and replay slot. |
| Player disconnected | Executed `GameLogic::onSelfDestruct(FALSE)` plus matching replay-header `playerDiscons[slot]` | Player, disconnected status, combined corroborating source, and required replay slot. False commands without header support and header metadata without an executed transition do not emit one. |
| CRC mismatch | `RecorderClass::handleCRCMessage`, using the recorder's authoritative queue-offset formula | First mismatch frame only. |
| Match outcome | `ReplayTelemetry::finish`, immediately before `complete` | Decided victory-condition winners/losers only when an authoritative end frame and winner exist; otherwise explicit unknown with empty winner/loser arrays. Includes the exact initialized engine-player domain and terminal facts. |

`DamageInfo` exposes no authoritative weapon identity at the post-calculation body seam. `weapon_name` is therefore
explicitly `null`; no weapon is inferred from the attacker/template. A missing source object is not reconstructed.
Its raw source ID/template are preserved only when the engine supplied them. Player attribution never consults the
attacker's current controller: ownership transfer or source destruction cannot rewrite the immutable source mask.
The helper retains copied scalar, string, set, and vector state only; it retains no `Object *` or `Player *`.

## Terminal-state policy

The replay header is copied when telemetry begins and survives the later new-game reset. The exact initialized
`PlayerList` domain is frozen after `players_initialized`; terminal output must match that domain even if teardown no
longer exposes `PlayerList`.

Recorder call sites carry an explicit modern-only termination enum: clean parsed EOF, CRC mismatch, partial/truncated
input, or intact-file interruption. Reset/reconfigure transactions are discarded rather than reclassified. Header
quit/desync/disconnect facts remain independent metadata and do not choose the termination reason.
`match_outcome` and `complete` must agree exactly on CRC state/frame, quit-early, replay-header desync,
header-disconnected slots, and clean shutdown. A CRC mismatch without the recorder's exact mismatch frame fails
closed and discards the owned transaction. Unknown outcomes always have empty winner/loser arrays; entity survival is
never used to infer a winner.

Player terminal transitions are at most once per player. Replay-header disconnect/quit values remain metadata unless
an executed authoritative transition corroborates them. Nested transition causes use a bounded RAII stack and all
trace-local state is reset at telemetry reconfiguration/begin.

## Contract and atomic-reader validation

Telemetry v2 now has closed JSON Schema payloads and schema-version-aware Pydantic checks for all Task 6 events and
the extended completion record. Direct Pydantic v2 use rejects unknown top-level and raw-location fields plus legacy
outcome fields, while frozen telemetry v1 remains permissive and byte-unchanged (SHA-256
`BFAA0279A9BAA9264121709063F0DD763C534333926CBC2846DD4E4D56761350`). Illegal/nonfinite/float-overflow numbers are
rejected before any record is exposed.

Exact Zero Hour damage/death identifiers live in packaged `zero-hour-combat-types-v1.json`. Tests compare it with
the authoritative C++ arrays and with the installed-wheel bytes; Pydantic rejects unknown IDs and any ID/name drift.
Every health and coordinate `Real` in these events must be finite and within the closed producer-serialization range
of +/-`3.40282347e38`. That decimal is the engine writer's nine-significant-digit JSON representation of finite binary
float32 maximum, rather than the slightly smaller unrounded binary value; any larger magnitude remains rejected.

Before returning any record, the atomic reader validates:

- every explicit player index belongs to the immutable `engine_player_indices` domain;
- current victims, healing targets/sources, and veterancy subjects exist and are alive, while a delayed damage
  attacker is historical provenance requiring prior creation and may already be transferred or destroyed;
- explicit current victim/target/source owners match lifecycle state;
- strict event-local damage/healing arithmetic, positive applied/calculated values, and killing-blow state;
- no health transition after `object_destroyed`; a killing blow alone is not treated as lifecycle destruction;
- veterancy numeric/name agreement and observed transition continuity, rejecting duplicate impossible transitions;
- at most one defeat/surrender/disconnect transition per player, coherent replay-slot mapping, and terminal-header
  corroboration for every emitted disconnect;
- exactly one `match_outcome`, immediately before `complete`, at the final frame;
- sorted unique disjoint authoritative winner/loser sets within the exact initialized full player domain; and
- recomputed terminal `event_counts`, including exactly one outcome and exactly one completion.

Validation remains whole-trace atomic: malformed terminal, arithmetic, reference, ordering, or provenance evidence
discards the buffered trace rather than exposing a valid prefix.

Damage and healing records are transition observations, not a complete health ledger. Veterancy ratio preservation,
construction/internal health changes, and bridge repair can change health between observed events, so cross-event
health continuity and a permanent post-killing-blow lock would be false claims and are intentionally absent.

## TDD evidence

Focused schema/reader and real-engine tests were written before production code. The initial Task 6 selection was
**10 failed, 6 passed** because the new event contracts, health/lifecycle state, terminal facts, and engine seams did
not exist. After the minimum implementation it was **16 passed**.

The final audit captured two additional focused RED cases before their fixes:

- two identical consecutive veterancy changes were accepted: **1 failed**, then the full focused contract was
  **17 passed** after trace-local level continuity;
- direct Pydantic v2 models accepted unknown and legacy Task 6 fields: **1 failed**, then the final focused contract
  was **18 passed** while direct v1 behavior remained unchanged.

Before corrective review, the focused real-engine Task 6 file was **6 passed** against the rebuilt executable. It checked the natural CRC boundary,
mechanics-only combat reachability, killing-blow ordering, header/reset ordering, modern guards/no retained engine
pointers, and explicit player-transition sources.

The corrective review began with **26 failed, 18 passed** focused cases covering team victory overlap, independent
health transitions, exact self-destruct sources, explicit termination, immutable player-mask attribution, closed
combat types, and float32 bounds. The contract then reached **34 passed**; a final current-healing-source liveness
case was captured **1 failed** and fixed to reach **35 passed**. Focused real/static engine coverage is now **9 passed**,
including a real partial replay that ends explicitly as `replay_truncated`.

The second corrective review pinned the writer-format boundary before changing production code. The targeted run was
**3 failed, 4 passed**: the model constant differed from the writer's nine-digit representation and the schema rejected
both signed producer limits. After updating only the v2 model/schema ceiling, the same selection was **10 passed** and
the full focused contract was **45 passed**. Above-boundary, overflow, NaN, and infinity probes remain rejected; the
installed-wheel test proves its packaged v2 schema bytes exactly match the canonical source while v1 stays unchanged.

## Replay evidence

Tracked replay fixture:

`scripts/replay_analyzer/tests/fixtures/zero_hour_1_04/leex279_vs_fox27.rep`

SHA-256: `EA085767BFA11D2CFC167D9007173CE2EB29B5F557702FFD042E2E9A1A8F6BB8`

### Natural, unmodified replay

The authoritative trace stops at real frame **108** after **16** executed commands. The recorder identifies the first
CRC mismatch at exact frame **105**. It contains exactly one `match_outcome` immediately before `complete`, with
status `unknown` and empty winner/loser arrays. It contains **0** damage, healing, or veterancy transitions before the
CRC boundary, so no later combat or outcome claim is made.

### Disposable CRC-stripped derivative

A temporary derivative removes only CRC commands and leaves the pinned bytes unchanged. It reaches frame **56004**
after **2874** executed commands and exposes **24** damage events and **61** object destructions. Every killing damage
observation precedes its matching destruction. It exposes **0** healing and **0** veterancy transitions, so those
events have compiled authoritative seams plus synthetic contract coverage but no real-fixture occurrence.

The derivative is mechanics-only evidence. It is not retained and is never used for player strategy, behavior,
winner, or match-history claims.

## Verification

Final commands and results (Python commands run from `scripts/replay_analyzer/`):

- `uv run --project . pytest tests/telemetry/test_combat_outcome_contract.py -q`
  - **45 passed**
- `uv run --project . pytest tests/test_wheel.py -q`
  - **1 passed**, including exact canonical/installed-wheel v1 and v2 schema byte parity
- `uv run --project . pytest tests/engine/test_combat_outcome.py -q`
  - **9 passed**
- `uv run --project . pytest -m "not engine" -q`
  - **448 passed, 54 deselected**
- `uv run --project . pytest -m engine -q`
  - **53 passed, 1 skipped, 448 deselected**
  - the skip is the existing Windows symlink-alias case when this host cannot create an unprivileged symlink
- `uv run --project . ruff check src tests`
  - **passed**
- `uv run --project . mypy --strict src`
  - **passed, 16 source files**
- No C++ changed in the second corrective review, so no build was rerun. The latest Task 6 x86 VS 2022 evidence remains
  `cmake --build build/win32 --target z_generals --config Release`: **passed and linked `generalszh.exe`** after the
  preceding C++ correction.

The full engine suite includes deterministic normalized double runs, telemetry-enabled versus disabled replay
return/stdout/stderr equality, CRC-stop behavior, exact event-count validation, and open/late/publish failure and
temporary-name collision cleanup. The runtime fixture hardlinks the built executable beside the Steam data and does
not copy or replace user game data.

VC6 and MinGW could not be built on this host. `Get-Command` finds no VC6/MinGW compiler or build tool; the VC6 cache
reports `CMAKE_CXX_COMPILER-NOTFOUND`, while the MinGW Makefiles cache reports
`CMAKE_MAKE_PROGRAM-NOTFOUND` and the Ninja cache has no configured C++ compiler. No unavailable compiler pass is
claimed. The final corrective attempt produced missing `build.ninja` for `build/vc6` and a missing Makefile build tool
for `build/mingw-w64-i686`. Modern-only guards, CMake exclusion, and focused static tests are the available
compatibility evidence.

## Minimal scope extensions and limitations

Source inspection proved the four-file brief insufficient. The implementation adds the focused modern-only
`ReplayCombat` helper and minimally extends:

- replay initialization/completion and the authoritative CRC mismatch seam;
- the existing surrender dispatcher and victory/script defeat call sites;
- CMake's existing modern analyzer source block;
- the closed v2 schema, Pydantic models, atomic reader, prior v2/wheel fixtures, and focused contract/engine tests.

No real repository replay currently exposes healing, veterancy, surrender, defeat, disconnect, or a decided outcome
before its authoritative stop. These are therefore reported as unavailable fixture evidence, not inferred facts.
Weapon identity is likewise unavailable at the selected damage seam and remains explicit null.

The five inherited stat-only zero-content worktree paths were not edited or staged.
