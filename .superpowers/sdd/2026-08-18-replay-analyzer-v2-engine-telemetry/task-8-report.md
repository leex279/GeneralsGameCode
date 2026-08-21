# Task 8 Report: Authoritative Map Data Export

Date: 21 August 2026

## Final outcome

Implemented Task 8 for the modern Zero Hour replay analyzer. After terrain, map overrides, map objects, and
`AIPathfind` are initialized, the engine now reads the authoritative map state and transactionally publishes one
strict content-addressed `map_asset` before telemetry manifest record zero. The v2 manifest and completion record are
cross-bound to that exact asset. Telemetry v1, base Generals, replay simulation state, and retail replay bytes remain
unchanged.

The export is passive. It never recalculates pathfinding, simulates locomotion, mutates terrain or objects, parses map
files in Python, queries client state, or uses RNG. The new pathfinder and lifecycle accessors are read-only and
compiled only under `RTS_REPLAY_ANALYZER && !IS_VS6_BUILD`.

## Authoritative asset

The trace parent contains `map-assets-v1/<content_sha256>/` with exactly:

- `manifest.json`;
- `height.f32.zlib`;
- `terrain.u8.zlib`;
- `pathing-ground.u8.zlib`;
- `pathing-amphibious.u8.zlib`; and
- `zones.i32.zlib`.

The content hash is SHA-256 of canonical compact, sorted-key UTF-8 manifest bytes with `content_sha256` replaced by
64 ASCII zeroes. The directory name and telemetry reference must equal that hash. The final manifest hash separately
binds the exact non-placeholder manifest bytes. Every member records exact compressed/uncompressed size and SHA-256,
dtype, little-endian encoding, zlib level, grid, and element count. C++ emits deterministic zlib 1.1.4 level-9 streams.

Grid facts come directly from initialized engine state:

- dimensions and origin from `Pathfinder::m_extent` through a guarded read-only accessor;
- raw cell types and zone IDs from `PathfindCell::getType/getZone`;
- terrain heights from `TerrainLogic::getGroundHeight` at each authoritative path-cell center; and
- ground/amphibious booleans from the closed `Pathfinder::validLocomotorSurfacesForCellType` mapping: ground is
  `CELL_CLEAR`; amphibious is `CELL_CLEAR` or `CELL_WATER`.

No heuristic terrain or locomotion labels are emitted. The manifest records all seven exact cell enum values and the
raw/derived provenance.

## Initialized static snapshot

The post-initialization snapshot exports:

- occupied replay-slot start positions resolved through `Player_N_Start` waypoints;
- every initialized named waypoint, path label, link, and direction flag;
- initialized bridges with endpoints, four corners, width, layer, template, and nullable lifecycle object ID; and
- only lifecycle-proven `map_loaded` objects that have a closed exact category source.

Static object categories are `KINDOF_OBSTACLE`, `KINDOF_SUPPLY_SOURCE`, `KINDOF_CAPTURABLE`,
`KINDOF_TECH_BUILDING`, `KINDOF_CASH_GENERATOR`, `SupplyWarehouseDockUpdate`, or a capturable/tech object with
`AutoDepositUpdate`. Stable template names, lifecycle IDs, raw finite float32 position/orientation, category source,
and `post_map_initialization` scope are explicit. No resource or oil classification uses a template-name substring.
Later dynamic objects are not represented as static map facts.

## Coordinate contract

The world/pathing bound is the closed XY region from initialized pathfinder cell edges. Grid manifests separately
record minimum-inclusive/maximum-exclusive cell-edge bounds, origin, cell size, dimensions, and center sampling.
Starts, bridges, and classified static objects must be inside the closed world bound. Waypoints record
`pathfinder_xy_closed` when inside and `not_asserted_by_source` otherwise; coordinates remain raw and are never
clamped.

Every v2 entity sample now carries one closed engine-sourced position policy. Normal entities and path goals must be
inside the map. Exemptions are limited to live engine evidence: AIRCRAFT/PROJECTILE/BRIDGE KindOf, current locomotor
AIR surface, exact WanderAIUpdate, or physics motion without AI pathing. KindOf exemptions are cross-checked against
the bound game-data catalog. This was necessary because Zero Hour's layer enum has no air layer and the pinned map's
ambient `Bird` uses `LAYER_GROUND`, no KindOf flags, and an AIR-capable locomotor. Edge coordinates pass, one-float OOB
tampering fails, and no validation path clamps a value.

## Transaction and cache safety

Publication uses an exclusive owned temporary directory beside the final cache, exact read-back validation, and a
no-replace `MoveFileA`. A pre-existing hash directory is a cache hit only when it is a safe ordinary directory with
exactly the six expected ordinary, single-link files and exact bytes. Corrupt, partial, extra, symlink/reparse,
hardlink, path-alias, or collision state fails closed and is never repaired, touched, or overwritten. At most 100
exclusive names are attempted. Cleanup names only the exporter-owned temporary files/directory and a newly created
empty cache root.

Pre-initialization, nonfinite, compression, write, validation, publication, and injected failures discard the owned
trace transaction and owned partial map transaction without changing replay execution. A telemetry writer failure
after map publication discards the trace but preserves the already valid immutable cache asset.

## Python contract and atomic validation

`map-asset-v1.schema.json` is closed and packaged verbatim in the wheel. The strict typed loader validates safe UTF-8
names and paths, schema/content/directory/reference identities, engine/map identity, all file types/link counts,
exact members, compressed size/hash before decompression, bounded streaming zlib EOF/trailing-data behavior,
uncompressed size/hash, exact dtype/dimension lengths, finite float32 values, binary pathing flags, valid cell types,
and zone IDs in `0..16383`. No partially constructed map model escapes on failure and decompression is bounded against
zip bombs.

The atomic v2 reader loads and validates both catalog and map from manifest record zero before exposing buffered
records, applies the declared sample policy to every entity position/path goal, and requires completion to repeat the
exact single manifest reference. Historical v1 remains permissive and byte-frozen.

## TDD and debugging evidence

Initial RED evidence included missing map reference/schema/loader/exporter behavior and **20 existing v2 fixture
failures** when the new mandatory v2 binding was first enabled. The focused loader/engine contract reached
**15 passed, 1 host symlink-privilege skip** after implementation.

Runtime testing then exposed two real defects:

- the repository's zlib 1.1.4 has no `compressBound`; the modern build failed until the documented conservative
  zlib 1.1.x worst-case allocation was used; and
- C++ member metadata was valid JSON but not canonical sorted-key JSON, so the strict loader rejected the first real
  asset at the exact first differing byte. Emission order was corrected and the real asset loaded.

The first full engine run was **112 passed, 2 skipped, 6 failed**. All failures were strict OOB validation of the same
ambient `Bird` completion sample at `(2605.30542, 311.569916)` against `[0,2600] x [0,2600]`. Source audit disproved
the proposed air-layer and AIRCRAFT-KindOf assumptions. The final current-locomotor AIR policy is source-grounded;
the full CRC-free movement/mechanics test then passed, followed by the complete suite.

Final focused Task 8 evidence is **21 passed, 1 symlink-privilege skip**. It covers strict decode, all member tampering,
trailing streams, bad lengths/types/flags/zones/nonfinite values, unexpected files, content-directory binding,
feature/sample bounds, false KindOf exemptions, hardlinks, deterministic bytes/hash, cache timestamp preservation,
telemetry-off behavior, corrupt/partial cache collision, export failure cleanup, terminal writer failure, and original
replay immutability.

## Real replay evidence

The natural pinned replay remains SHA-256
`EA085767BFA11D2CFC167D9007173CE2EB29B5F557702FFD042E2E9A1A8F6BB8` and is used only to its established CRC
boundary. Its map asset strictly loads and has:

- content SHA-256 `a95461fea9a3e82596502a0b3e8d76d889c8784b7f808a7f513f98b8152bc73a`;
- pathing and sampled terrain dimensions `260 x 260`, 10-unit cells, and closed XY world bounds `[0,2600]`;
- height `39,123 / 270,400` compressed/raw bytes;
- ground pathing `1,583 / 67,600` bytes;
- amphibious pathing `1,078 / 67,600` bytes;
- raw terrain/cell types `1,811 / 67,600` bytes;
- zones `5,782 / 270,400` bytes; and
- 2 starts, 20 named waypoints, 0 bridges, and 21 classified static objects.

Two natural runs produce the exact same reference and bytes; the validated cache hit changes no timestamp. All
eligible natural entity samples are inside the declared policy. The disposable CRC-stripped derivative remains
mechanics-only evidence, makes no strategy/winner/player-performance claim, and now strictly validates every eligible
sample through clean completion with telemetry-on/off replay outcome parity.

## Final verification

- full non-engine suite: **491 passed, 126 engine tests deselected**;
- full engine suite: **124 passed, 2 skipped, 491 non-engine tests deselected**;
- Ruff: **passed**;
- strict mypy: **passed, 19 source files**;
- `git diff --check`: **passed**; and
- modern VS 2022 x86 `z_generals`: **built and linked `generalszh.exe`** after the final source changes.

The two engine skips are honest host capability limits, including unprivileged symlink creation. VC6 and MinGW are
unavailable on this host, so no compile claim is made for either. Compatibility evidence is limited to modern-only
guards, CMake exclusion from VC6, static source tests, and the modern x86 build. The pinned map contains no bridge, so
bridge export has source/static contract coverage but no nonempty real-map bridge fixture. The five inherited stat-only
worktree paths were neither edited by Task 8 nor staged.
