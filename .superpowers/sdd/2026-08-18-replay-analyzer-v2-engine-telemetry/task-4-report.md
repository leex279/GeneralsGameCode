# Task 4 Report: Entity Lifecycle Telemetry

Date: 20 August 2026

## Outcome

Implemented passive, modern-only Zero Hour telemetry for these authoritative entity lifecycle observations:

- `object_created`
- `construction_started`
- `construction_completed`
- `owner_changed`
- `sold`
- `object_destroyed`

The implementation preserves the Task 3 manifest/catalog/player boundary. `manifest` remains first, exactly one
`players_initialized` remains second, and entity observations captured during map/player setup are flushed only after
that player snapshot.

## Authoritative engine seams

| Observation | Seam | Reason |
|---|---|---|
| Registration identity | `GameLogic::registerObject` | ID, template, team, initial status, and modules are registered and authoritative. |
| First placement/orientation | `Thing::setPosition`, with changed-position `Object::reactToTransformChange` fallback for composite transforms | Freezes position and orientation on the first explicit position-setting invocation even when coordinates equal the default; later orientation/movement cannot overwrite it. |
| Map source | Returned root from the direct bridge and ordinary map-object `ThingFactory::newObject` calls in `GameLogic::tryStartNewGame` | Observed map-load call site, not an owner/template guess or inherited callback scope. |
| Starting source | Returned root from the direct `ThingFactory::newObject` call in `placeObjectAtPosition` | Observed starting-object call site without classifying callback-created children. |
| Factory source | Returned root from the direct `ThingFactory::newObject` call in `ProductionUpdate` | Observed production call site without classifying callback-created children. |
| Construction/sale | `Object::setStatus` | Emits only actual `UNDER_CONSTRUCTION` and `SOLD` bit transitions. |
| Ownership | `Object::setOrRestoreTeam` immediately after the team switch | Carries nullable previous/new owner and team identities. |
| Destruction | `GameLogic::destroyObject` immediately after the irreversible `DESTROYED` state | Exactly-once guarded seam and remains after Task 6's future killing-damage observation point. |

`ActiveBody.cpp` was deliberately not modified. Health/death detection is not the irreversible object-destruction seam,
and adding Task 4 emission there would either duplicate deletion-without-damage paths or put destruction before the
Task 6 killing-damage observation. `GameLogic::destroyObject` covers damage death, sale cleanup, script deletion, and
delete-without-damage uniformly.

Creation calls without one of the three marked returned-root call sites are emitted with explicit
`creation_source: "unknown"`.
This includes script/OCL and other central registrations when no source scope is exposed; no owner/template heuristic is
used. The direct-call scope only defers lifecycle emission until the returned root can be marked; it carries no ambient
source classification. Consequently a nested `newObject` registration remains `unknown` unless its own authoritative
call site marks that exact returned ID.

## Pre-initialization and reset design

The focused modern-only `ReplayEntityLifecycle` helper buffers copied values only:

- object ID and registration frame/order;
- stable template name;
- nullable initial owner/team;
- initial status names and stable kind-of names;
- first explicit position-set pose, or first changed-position composite transform, and explicit placed/unplaced state;
- observed creation source;
- registration-time producer/builder/responsible identities, including explicit null when not yet exposed;
- immutable pending event type, frame, order, and serialized payload.

No entry retains a raw `Object *`. Live finalization resolves the object transiently by ID only to prove it still owns
the registered ID; it never rereads pose, owner/team, producer, builder, or initial-construction identity. A pre-init object destroyed
before player publication is finalized from the still-valid destroy seam into copied `object_created` and
`object_destroyed` snapshots in observation order. A live object with no observed placement is represented as
`position_status: "unplaced", position: null`; zero coordinates are never fabricated. Missing live registrations fail
the telemetry transaction without changing replay execution.

Both telemetry reconfiguration and `GameLogic::reset` clear trace-local maps, pending events, direct-call deferral depth, and order
counters before old-game destruction. This permits object ID reuse in later replay lifecycles without conflation.

## Contract and reader validation

Telemetry v2 now strictly defines:

- positive object IDs (zero remains the invalid sentinel);
- nullable owner/team fields only when absent;
- coherent `placed`/position and `unplaced`/null pairs;
- stable `kind_of_flags`, `initial_status`, source enum, and registration/producer context;
- explicit previous/new construction, sale, and destruction states;
- previous/new nullable owner/team identities;
- nullable producer, builder, and responsible-player identities only where exposed.

Pydantic records enforce the v2-only requirements while leaving historical v1 payloads readable. The v1 schema was not
changed.

Before returning any record, the reader now validates a trace-local entity state table. Its centralized typed
reference-policy table covers lifecycle, production, supply, damage, healing, veterancy, orders, state changes, and
samples. Current subjects require prior creation and current liveness: lifecycle subject IDs, supply collectors, damage
victims, healing targets, veterancy/state/sample subjects, and selected/order targets. Historical provenance requires
prior creation but remains valid after destruction: creation/construction producers, construction builders, production
producers, supply sources, and damage attackers.
It rejects:

- reference before `object_created`;
- entity events before `players_initialized`;
- duplicate creation, sale, or destruction;
- current-subject references after destruction while retaining legitimate historical provenance;
- invalid construction ordering or a missing initial construction-start transition;
- owner/team state mismatch;
- direct object template identity changes.

The validator intentionally does not treat `production_id` as an object ID and does not compare a production target's
`template_name` with its producer's object template.

## TDD evidence

Tests were written and run before production changes.

Initial focused RED:

- Lifecycle contract: **19 failed, 2 passed**. The baseline lacked nullable/explicit lifecycle payload rules and
  cross-record identity validation.
- Natural engine integration: **failed** because Task 3 emitted no `object_created` records.

Later focused RED/GREEN additions verified current owner/team consistency and the required construction-start record for
an initial `UNDER_CONSTRUCTION` status. Final focused lifecycle contract: **23 passed**.

Fix round 1 added regressions before changing production code:

- reader RED: **4 failed, 26 passed**, covering destroyed attacker/source/producer/builder provenance;
- engine seam RED: **3 failed, 5 deselected**, covering same-coordinate placement plus immutable orientation,
  returned-root-only source binding, and registration-time initial construction identity;
- focused GREEN: **30 passed** lifecycle contracts and **3 passed, 5 deselected** engine seam tests;
- final lifecycle contract file after the explicit A-to-B reader characterization: **31 passed**;
- final focused entity lifecycle file: **8 passed**.

The synthetic contracts retain current-subject-after-destruction rejection for damage victims, supply collectors, and
construction subjects. Initial identity is copied at registration, so a reserved construction-start payload remains
owner/team A even when a later ownership transition moves the object to B before flush.

Synthetic strict traces cover duplicate/reference/state/template rules, nullable/unplaced identity, pre-init
create/place/destroy flush ordering, and trace-local reset. They supplement but do not replace engine seam verification.

## Replay evidence

Tracked replay search found one fixture:

`scripts/replay_analyzer/tests/fixtures/zero_hour_1_04/leex279_vs_fox27.rep`

SHA-256: `EA085767BFA11D2CFC167D9007173CE2EB29B5F557702FFD042E2E9A1A8F6BB8`

### Natural, unmodified replay (authoritative real evidence)

The product run stops naturally at the replay's CRC mismatch:

- terminal frame: **108**;
- executed commands: **16**;
- `crc_mismatch: true`;
- `clean_shutdown: false`.

Observed before that boundary:

- **171** unique `object_created` records;
- sources: **166** `map_loaded`, **4** `starting_object`, **1** explicit `unknown`;
- all entity records occur after `players_initialized`;
- every reference resolves to exactly one earlier creation;
- map-loaded placed neutral/source evidence is present;
- **1** real `construction_started` at frame 81 for object ID 171, owner player 2/team 1. Producer, builder, and
  responsible-player are explicitly null because those fields were not exposed at registration; later live state is no
  longer backfilled into an earlier observation.

Two natural runs used different run IDs. Their raw ordered lifecycle lines were byte-identical after replacing only the
run ID. The complete normalized streams were also byte-identical after replacing only run ID and terminal trace hash,
and the single content-addressed catalog had identical bytes.

No natural pre-CRC ownership, construction-completion, sale, or destruction transition is present. No such claim is
made.

### Disposable CRC-stripped derivative (synthetic mechanics coverage only)

The original fixture bytes were left unchanged. A disposable derivative removed only replay CRC commands so engine
mechanics could be exercised beyond the natural frame-108 stop. This derivative is not real/full-match evidence and is
not used for strategy, player behavior, or authoritative match-outcome claims.

Observed mechanics-only counts:

- **282** creations: 166 map, 4 starting, 23 player production, 89 unknown;
- **18** construction starts;
- **18** construction completions;
- **1** sale;
- **61** destructions;
- **0** owner changes.

Therefore an ownership transition was not available in any repository replay fixture. `owner_changed` is verified by
the authoritative engine seam/build plus strict synthetic reader traces, not claimed as replay-fixture runtime coverage.

The derivative exercises containment-generated objects, but the fixture does not synchronously create one beneath any
of the three marked direct roots. Nested-source isolation is therefore proven by the helper/call-site source-order
regression and compiled engine path, not claimed as a fixture transition. A diagnostic rerun using a long workspace-local
pytest base path failed before trace creation with `catalog_open_failed`; the unchanged short disposable pytest path is
the supported test setup and passed the full engine suite.

## Verification

Final commands and results:

- `uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests -m "not engine" -q`
  - **321 passed, 37 deselected**
- `uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/engine -q`
  - **36 passed, 1 skipped**
- `uv run --project scripts/replay_analyzer ruff check scripts/replay_analyzer/src scripts/replay_analyzer/tests`
  - **passed**
- `uv run --project scripts/replay_analyzer mypy --strict scripts/replay_analyzer/src`
  - **passed, 15 source files**
- x86 developer environment plus `cmake --build build/win32 --target z_generals --config Release -- -j 4`
  - **passed**

The existing envelope integration compares telemetry-on and telemetry-off return status, stdout, and stderr; it passed
with lifecycle records. Transactional writer/error-path tests also passed. No RNG, UI/client-derived telemetry identity,
gameplay return/status, replay bytes, retail runtime files, or base `Generals/` sources changed.

VC6 and MinGW could not be built on this host. Their cached configure directories report
`CMAKE_CXX_COMPILER-NOTFOUND` and `CMAKE_MAKE_PROGRAM-NOTFOUND`, respectively. The new header/source and every engine
include/call are explicitly guarded with `RTS_REPLAY_ANALYZER && !IS_VS6_BUILD`; static tests verify the exclusion and
that the pre-init entry contains no raw object pointer.

## Minimal scope extensions

Beyond the original planned engine source files, the fix round instruments the authoritative `Thing::setPosition` seam,
retains the focused modern-only helper, and minimally extends
`ReplayTelemetry`, CMake, the v2 schema/model/reader, and the existing envelope test. The envelope assertion had to stop
requiring exactly three records because Task 4 intentionally inserts lifecycle observations between players and
completion; it still checks manifest/player/terminal order and exact recomputed event counts.
