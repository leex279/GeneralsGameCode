# Task 5 Report: Economy and Production Telemetry

Date: 20 August 2026

## Outcome

Implemented passive, modern-only Zero Hour telemetry for:

- unit production queue, cancellation, and completion;
- upgrade queue, cancellation, and completion as a structurally separate identity domain;
- actual science purchases and committed special-power activations;
- exact central Money mutations with closed observational reason values;
- pickup-sourced supply handoff observations before their matching cash deposits; and
- ordered terminal engine cash balances for strict replay-wide reconciliation.

The implementation does not change base `Generals/`, replay commands, simulation RNG, UI/client state, gameplay
returns, or authoritative Money arithmetic. All instrumentation is guarded by
`RTS_REPLAY_ANALYZER && !IS_VS6_BUILD`.

## Authoritative seams

| Observation | Seam | Evidence carried |
|---|---|---|
| Unit queued | `ProductionUpdate::queueCreateUnit`, after queue insertion | Trace-global production ID, producer-local engine ID, producer/player, stable thing-template name, queue position/frame, authoritative cost and quantity. |
| Unit cancelled/completed | `ProductionUpdate::cancelUnitCreate` / successful final-quantity branch in `update`, after queue removal | The immutable queued identity and one mutually exclusive terminal frame/state. |
| Upgrade queued/cancelled/completed | Corresponding `ProductionUpdate` queue/removal/grant paths | Separate trace-global upgrade queue ID, producer/player, stable upgrade name, queue position/frame, cost and terminal state. |
| Science purchased | `Player::attemptToPurchaseScience`, after point deduction and `addScience` | Stable science name, player, exact point cost and before/after points. Initial grants and scripts do not pass this seam. No source object exists at this player-owned transition, so it is explicit null. |
| Special power used | `SpecialPowerModule::triggerSpecialPower` | The common committed trigger for immediate and deferred powers, before recharge. Immediate object-targeted dispatch carries the already-resolved target transiently; deferred paths report only the finite location they expose. Intent, readiness and grant paths are not labelled as use. |
| Cash changed | `Money::withdraw`, `deposit`, and `setStartingCash`, after the engine mutation | Owned player, unsigned engine before/after, wide signed delta, `track_income`, and one closed reason. Zero changes are not emitted. |
| Supply pickup provenance | Successful `SupplyWarehouseDockUpdate::action` box transfer | Copied collector and warehouse object IDs for each actually acquired box. |
| Supply collected | `SupplyCenterDockUpdate::action`, at positive-value handoff | Collector, resolved/mixed/unknown source, receiving dropoff, player, exact raw positive value and finite raw collector location. The supply event is emitted before the same-value deposit. |
| Final balances | `ReplayTelemetry::finish`, transiently reading `PlayerList` | Every engine player ordered by player index, with explicit `has_money` and balance/null state. |

The receiving Supply Center is serialized only as `dropoff_object_id`; it is never mislabelled as the resource source.
Source status is `resolved` only when the successful pickup count and one warehouse identity match, `mixed` for a
complete multi-source load, and `unknown` when pickup provenance is incomplete.

## Trace-local state and reason isolation

The focused `ReplayEconomy` helper retains only copied scalar/string state. It stores no engine pointers. Production
trace IDs are monotonically assigned and keyed by producer object ID plus the authoritative engine production ID.
Upgrade IDs are independently monotonically assigned and keyed by producer ID plus stable upgrade name while active.
Terminal flags prevent duplicate or conflicting terminal emission. Telemetry reconfiguration and `GameLogic::reset`
clear IDs, active queues, supply provenance, pending cash and reason scopes before object-ID reuse.

`Money` does not attribute player zero until `setPlayerIndex` proves ownership. Starting balances copied from a player
template are observed only when that ownership is attached. Cash captured before telemetry initialization is buffered
as immutable values, then flushed after manifest, players, and Task 4 lifecycle creations. No raw pointer or later live
value is retained.

The cash reason stack is scoped at proven call sites and consumed by at most one Money operation. Unit/upgrade charges
and refunds, construction charges, starting cash, supply income, sale refunds and script money changes receive narrow
contexts. A central mutation without proven context remains `unknown`; nested or later operations cannot inherit a
consumed reason. Delta validation uses `long long` in C++ and unbounded Python arithmetic in the reader, so unsigned
wrap or signed overflow cannot be reported as ordinary signed arithmetic.

## Contract and reader validation

Telemetry v2 now strictly defines the Task 5 payloads, closed cash reasons, finite positive supply data, coherent
resolved/null source status, stable queue identity, and non-empty ordered `final_cash_balances`. Historical v1 remains
readable because the shared Pydantic model keeps the new completion field optional while the v2 schema and reader
require it.

Before any record is returned, the atomic reader validates:

- exactly one queue event per trace ID and at most one cancellation or completion;
- terminal-after-queue ordering, immutable identity fields, and envelope/payload frame agreement;
- distinct unit-production and upgrade identity domains;
- exact cash `before + delta == after` and per-player before/after continuity;
- closed reason values and terminal balance equality for every observed cash chain;
- ordered, duplicate-free final balances, including explicit players with no Money or no changes;
- exact science point subtraction and catalog membership for thing templates, upgrades and sciences;
- legal finite JSON numbers, positive supply amounts and coherent source status; and
- Task 4 field-specific creation/liveness policy for producer, source, collector, dropoff and special-power target IDs.

Catalog or state validation failure discards the buffered trace; no partially validated records escape.

## TDD evidence

Tests were written before production changes. The initial Task 5 focused contract run was **18 failed**: the schema,
model and reader did not yet provide the required fields or state validation. The pre-existing v2/entity baseline was
**62 passed**. After implementation, the combined Task 5 plus prior v2/entity contracts were **80 passed**.

Two later strictness regressions were also captured RED before their fixes:

- a terminal frame earlier than its queued frame was accepted: **1 failed, 4 passed**; after temporal validation,
  **5 passed**;
- the installed-wheel v2 smoke trace omitted the new required terminal balance: the full non-engine run was
  **339 passed, 1 failed, 42 deselected**; after representing a player with no Money explicitly, the rerun was
  **340 passed, 42 deselected**.

The final focused Task 5 contract file is **19 passed** and the real-engine Task 5 file is **5 passed**. The latter
asserts exact observed fixture availability, cash reasons,
supply-before-cash adjacency, resolved source provenance, queue/terminal subset identity, and final-balance folding.

## Replay evidence

Tracked replay fixture:

`scripts/replay_analyzer/tests/fixtures/zero_hour_1_04/leex279_vs_fox27.rep`

SHA-256: `EA085767BFA11D2CFC167D9007173CE2EB29B5F557702FFD042E2E9A1A8F6BB8`

### Natural, unmodified replay

The natural replay remains authoritative only until its frame-108 CRC mismatch. Task 5 observations available before
that boundary are exactly:

- `cash_changed`: **6** — starting cash 4, unit cost 1, construction cost 1;
- `production_queued`: **1**;
- `production_cancelled` / `production_completed`: **0 / 0**;
- all upgrade events: **0**;
- `science_purchased`: **0**;
- `special_power_used`: **0**;
- `supply_collected`: **0**.

The one queued unit has no terminal before the authoritative stop. No natural-fixture upgrade, science, special-power,
supply, or owner-transition claim is made. Every natural cash chain reconciles to the terminal engine balance snapshot.

Two natural runs with different run IDs produced byte-identical complete streams after normalizing only run ID and the
terminal trace hash. This includes all lifecycle and Task 5 events.

### Disposable CRC-stripped derivative

A disposable derivative removes only CRC commands to exercise later mechanics. It is not used for strategy, player
behavior, outcome, or natural match-history evidence. The pinned replay bytes remain unchanged.

Mechanics-only Task 5 availability is exactly:

- unit production: **23 queued, 0 cancelled, 23 completed**;
- upgrades: **0 queued, 0 cancelled, 0 completed**;
- sciences: **2** (`SCIENCE_SpyDrone`, `SCIENCE_ScudLauncher`);
- committed special powers: **1** (`SpecialPowerSpyDrone`);
- supply handoffs: **794**, all source-resolved and each immediately followed by its matching same-frame deposit;
- cash changes: **864**.

Queued templates are `AirF_AmericaVehicleDozer` (1), `AFG_AmericaVehicleChinook` (2), and
`GLAInfantryWorker` (20). Cash reasons are starting cash 4, unit cost 23, construction cost 18, supply income 794,
sale refund 1, and `unknown` 24. The 24 periodic +250 deposits have no proven authoritative category at their central
call sites, so they deliberately remain `unknown`. No upgrade transition exists in any repository fixture; upgrade
runtime behavior is covered by compiled authoritative seams plus strict synthetic reader contracts.

## Verification

Final commands and results:

- `uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests -m "not engine" -q`
  - **340 passed, 42 deselected**
- `uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/engine -q`
  - **41 passed, 1 skipped**
  - the skip is the existing Windows symlink-alias case when this host cannot create an unprivileged symlink
- `uv run --project scripts/replay_analyzer ruff check scripts/replay_analyzer/src scripts/replay_analyzer/tests`
  - **passed**
- configured strict `uv run --project scripts/replay_analyzer mypy`
  - **passed, 15 source files**
- x86 VS 2022 environment plus `cmake --build build/win32 --target z_generals --config Release -- -j 4`
  - **passed and linked `generalszh.exe`**

The engine suite includes telemetry-on versus telemetry-off return/stdout/stderr equality, natural normalized double
run determinism, final cash folding, CRC-stop behavior, and passive open/late/publish writer-failure paths. Existing
compiler warnings are unchanged surrounding legacy signedness/enum warnings; no Task 5 compilation error or new
diagnostic was introduced.

VC6 and MinGW could not be built on this host. Their cached configurations report
`CMAKE_CXX_COMPILER-NOTFOUND` and `CMAKE_MAKE_PROGRAM-NOTFOUND`. Legacy signatures and member layout remain behind the
modern guard, and static tests verify that the helper is excluded from VC6 and retains no engine pointer.

## Minimal scope extensions

Source inspection proved the original four-file brief insufficient. The implementation therefore adds the focused
modern-only `ReplayEconomy` helper and minimally extends:

- `ReplayTelemetry` completion/initialization and `GameLogic` reset;
- `Money.h` ownership tracking;
- the authoritative warehouse pickup, science purchase and committed special-power seams;
- the unit/upgrade/construction/sell/script reason call sites;
- CMake's existing modern analyzer source block; and
- the v2 schema/model/reader, lifecycle reference table, wheel fixture and focused contract/engine tests.

The five inherited stat-only worktree paths were not edited or staged.
