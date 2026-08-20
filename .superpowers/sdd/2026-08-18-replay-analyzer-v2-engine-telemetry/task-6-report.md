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
returns, or the base `Generals/` target. Every C++ helper and call site is guarded by
`RTS_REPLAY_ANALYZER && !IS_VS6_BUILD`, and telemetry return values remain ignored.

## Authoritative seams and available evidence

| Observation | Seam | Evidence carried |
|---|---|---|
| Damage applied | `ActiveBody::attemptDamage`, after armor/scalar/kill/clipping and health mutation, before callbacks and `Object::onDie` | Victim and observed owner; raw source object ID and observed source owner when available; raw source template from `DamageInfo`; attempted, calculated, and actually applied amount; prior/new health; numeric and stable raw damage/death names; raw victim location; killing-blow flag. |
| Healing applied | `ActiveBody::attemptHealing`, after authoritative health clipping, before healing callbacks | Target and owner, raw optional source ID/owner, attempted/calculated/applied amount, prior/new health, and raw target location. |
| Veterancy changed | `Object::onVeterancyLevelChanged`, after the tracker has changed level and the object has applied body/weapon/upgrade state | Object/current owner plus prior/new numeric level and stable level name. |
| Player defeated | `Player::killPlayer` at the actual active-to-dead assignment, scoped only by `VictoryConditions::update` or `ScriptActions::doPlayerKill` | Player, prior/new status, exact victory-condition or script source, and replay slot when one exists. |
| Player surrendered | The same `killPlayer` transition scoped by executed `GameLogic::onSelfDestruct` | Player, active-to-surrendered status, replay-command source, and replay slot. |
| Player disconnected | Executed surrender plus matching replay-header `playerDiscons[slot]` | Player, disconnected status, combined corroborating source, and required replay slot. Header metadata alone never emits a transition. |
| CRC mismatch | `RecorderClass::handleCRCMessage`, using the recorder's authoritative queue-offset formula | First mismatch frame only. |
| Match outcome | `ReplayTelemetry::finish`, immediately before `complete` | Decided victory-condition winners/losers only when an authoritative end frame and winner exist; otherwise explicit unknown with empty winner/loser arrays. Includes the exact initialized engine-player domain and terminal facts. |

`DamageInfo` exposes no authoritative weapon identity at the post-calculation body seam. `weapon_name` is therefore
explicitly `null`; no weapon is inferred from the attacker/template. A missing source object is not reconstructed.
Its raw source ID/template are preserved only when the engine supplied them. The helper retains copied scalar,
string, set, and vector state only; it retains no `Object *` or `Player *`.

## Terminal-state policy

The replay header is copied when telemetry begins and survives the later new-game reset. The exact initialized
`PlayerList` domain is frozen after `players_initialized`; terminal output must match that domain even if teardown no
longer exposes `PlayerList`.

Terminal precedence is CRC mismatch, replay truncation, interrupted header/engine state, then clean completion.
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

Before returning any record, the atomic reader validates:

- every explicit player index belongs to the immutable `engine_player_indices` domain;
- current victims, healing targets, and veterancy subjects exist and are alive, while historical attacker/healing
  provenance requires prior creation and may already be destroyed;
- explicit current owners match lifecycle state and historical source owners match when supplied;
- damage/healing arithmetic, positive applied/calculated values, health continuity, and killing-blow state;
- no health transition after a killing blow or object destruction;
- veterancy numeric/name agreement and observed transition continuity, rejecting duplicate impossible transitions;
- at most one defeat/surrender/disconnect transition per player and coherent replay-slot mapping;
- exactly one `match_outcome`, immediately before `complete`, at the final frame;
- sorted unique disjoint authoritative winner/loser sets within the exact initialized full player domain; and
- recomputed terminal `event_counts`, including exactly one outcome and exactly one completion.

Validation remains whole-trace atomic: malformed terminal, arithmetic, reference, ordering, or provenance evidence
discards the buffered trace rather than exposing a valid prefix.

## TDD evidence

Focused schema/reader and real-engine tests were written before production code. The initial Task 6 selection was
**10 failed, 6 passed** because the new event contracts, health/lifecycle state, terminal facts, and engine seams did
not exist. After the minimum implementation it was **16 passed**.

The final audit captured two additional focused RED cases before their fixes:

- two identical consecutive veterancy changes were accepted: **1 failed**, then the full focused contract was
  **17 passed** after trace-local level continuity;
- direct Pydantic v2 models accepted unknown and legacy Task 6 fields: **1 failed**, then the final focused contract
  was **18 passed** while direct v1 behavior remained unchanged.

The focused real-engine Task 6 file is **6 passed** against the rebuilt executable. It checks the natural CRC boundary,
mechanics-only combat reachability, killing-blow ordering, header/reset ordering, modern guards/no retained engine
pointers, and explicit player-transition sources.

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
  - **18 passed**
- `uv run --project . pytest tests/engine/test_combat_outcome.py -q`
  - **6 passed**
- `uv run --project . pytest -m "not engine and not ollama" -q`
  - **421 passed, 50 deselected**
- `uv run --project . pytest tests/engine -q`
  - **49 passed, 1 skipped**
  - the skip is the existing Windows symlink-alias case when this host cannot create an unprivileged symlink
- `uv run --project . ruff check src tests`
  - **passed**
- configured strict `uv run --project . mypy`
  - **passed, 15 source files**
- x86 VS 2022 `VsDevCmd` environment plus
  `cmake --build build/win32 --target z_generals --config Release -- -j 4`
  - **passed and linked `generalszh.exe`** after the final C++ changes

The full engine suite includes deterministic normalized double runs, telemetry-enabled versus disabled replay
return/stdout/stderr equality, CRC-stop behavior, exact event-count validation, and open/late/publish failure and
temporary-name collision cleanup. The runtime fixture hardlinks the built executable beside the Steam data and does
not copy or replace user game data.

VC6 and MinGW could not be built on this host. `Get-Command` finds no VC6/MinGW compiler or build tool; the VC6 cache
reports `CMAKE_CXX_COMPILER-NOTFOUND`, while the MinGW Makefiles cache reports
`CMAKE_MAKE_PROGRAM-NOTFOUND` and the Ninja cache has no configured C++ compiler. No unavailable compiler pass is
claimed. Modern-only guards, CMake exclusion, and focused static tests are the available compatibility evidence.

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
