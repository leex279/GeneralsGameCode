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
dtype, little-endian encoding, zlib level, grid, and element count. The current build emits deterministic zlib 1.3.1
level-9 streams and records that producer version in the canonical manifest.

Grid facts come directly from initialized engine state:

- dimensions and origin from `Pathfinder::m_extent` through a guarded read-only accessor;
- raw cell types and zone IDs from `PathfindCell::getType/getZone`;
- terrain heights from `TerrainLogic::getGroundHeight` at each authoritative path-cell center; and
- ground/amphibious booleans from the closed `Pathfinder::validLocomotorSurfacesForCellType` mapping: ground is
  `CELL_CLEAR`; amphibious is `CELL_CLEAR` or `CELL_WATER`.

No heuristic terrain or locomotion labels are emitted. The manifest records all seven exact cell enum values and the
raw/derived provenance.

The v1 grid descriptor is closed to the producer's actual layout: terrain and pathing descriptors must be identical;
storage order is `row_major_y_then_x_x_fastest`; index origin times cell size must equal the minimum edge; and
`(origin + count) * cell_size` must equal the maximum-exclusive storage edge. The closed world XY bounds are exactly
the pathing outer edges. Boundary inclusion for world coordinates remains separate from maximum-exclusive grid
storage indexing.

## Initialized static snapshot

The post-initialization snapshot exports:

- occupied replay-slot start positions resolved through `Player_N_Start` waypoints;
- every initialized named waypoint, path label, link, and direction flag;
- initialized bridges ordered by their authoritative unique `BridgeInfo::bridgeIndex`, with endpoints, four corners,
  width, layer, nullable template, and nullable lifecycle object ID; and
- only lifecycle-proven `map_loaded` objects that have a closed exact category source.

Static object categories are `KINDOF_OBSTACLE`, `KINDOF_SUPPLY_SOURCE`, `KINDOF_CAPTURABLE`,
`KINDOF_TECH_BUILDING`, `KINDOF_CASH_GENERATOR`, `SupplyWarehouseDockUpdate`, or a capturable/tech object with
`AutoDepositUpdate`. Stable template names, lifecycle IDs, raw finite float32 position/orientation, category source,
and `post_map_initialization` scope are explicit. No resource or oil classification uses a template-name substring.
Later dynamic objects are not represented as static map facts.

The reader independently derives the exact expected static set from `map_loaded` lifecycle evidence plus catalog
KindOf/module metadata. Each lifecycle creation carries a closed `initialization_snapshot_status`: map-loaded objects
are `present` or `absent` according to current `GameLogic` membership at the post-map export lifecycle flush, while
all other sources are `not_applicable`. This preserves static facts for objects destroyed later while excluding
objects destroyed before the initialization snapshot. Starts exactly match `players_initialized`; waypoint names,
IDs, and links are unique and closed; bridge/static IDs and templates bind to lifecycle evidence; and invented,
omitted, duplicated, or source-forged oil/supply/capturable/static features fail the whole trace atomically.

## Coordinate contract

The world/pathing bound is the closed XY region from initialized pathfinder cell edges. Grid manifests separately
record minimum-inclusive/maximum-exclusive cell-edge bounds, origin, cell size, dimensions, and center sampling.
Starts, bridges, and classified static objects must be inside the closed world bound. Waypoints record
`pathfinder_xy_closed` when inside and `not_asserted_by_source` otherwise; coordinates remain raw and are never
clamped.

Every v2 entity sample now carries one closed engine-sourced position policy. Normal entities and path goals must be
inside the map, including every unknown layer value. Exemptions are limited to AIRCRAFT, PROJECTILE, BRIDGE, or
PARACHUTABLE KindOf and the current locomotor's AIR surface. KindOf evidence must agree independently between the
lifecycle snapshot and game-data catalog; locomotor evidence must match the exact catalog template, locomotor-set ID,
locomotor-set name, and locomotor template. Wander/module/physics booleans cannot exempt bounds. Zero Hour's layer enum
has no air layer: the pinned map's ambient `Bird` passes through its catalog-verified AIR locomotor, and the mechanics
derivative's out-of-map `SupplyDropZoneCrate` passes through its catalog/lifecycle-verified PARACHUTABLE KindOf. Forged
policy, mask, set, module, or unknown-layer evidence fails. Edge coordinates pass, one-float OOB tampering fails, and
no validation path clamps a value.

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

Every replay begin, discard, reset, initialization failure, and reconfiguration clears the trace-local map-ready and
reference state without deleting immutable published cache bytes. Guarded static source tests verify those reset
call sites and that immutable cache deletion is absent. No safe two-replay, same-process runtime harness exists in
the current engine tests, so sequential runtime stale-reference rejection remains an explicit limitation rather than
a claimed runtime proof.

## Python contract and atomic validation

`map-asset-v1.schema.json` is closed and packaged verbatim in the wheel. The strict typed loader validates safe UTF-8
names and paths, schema/content/directory/reference identities, engine/map identity, all file types/link counts,
exact members, compressed size/hash before decompression, bounded streaming zlib EOF/trailing-data behavior,
uncompressed size/hash, exact dtype/dimension lengths, finite float32 values, binary pathing flags, valid cell types,
and zone IDs in `0..16383`. No partially constructed map model escapes on failure and decompression is bounded against
zip bombs.

Before any read, the loader `lstat`s and caps the manifest and every member, then rechecks the opened file identity,
exact size, regular-file type, single-link count, and compressed hash. Decompression is chunked with an output budget
of declared raw length plus one byte and requires one complete zlib stream with EOF, no unused/unconsumed bytes, no
concatenated stream, and no trailing data. Sparse oversized files, small-declared zip bombs, truncated streams, and
concatenated streams fail before partial object exposure. Every component from the resolved trusted trace directory
through `map-assets-v1`, the content hash directory, and each member must be plain and non-reparse; resolved common-path
containment rejects junction/symlink parent escapes as well as member links. Before opening each file, the loader
snapshots device/inode, mode, link count, size, and reparse state; immediately after descriptor open it requires the
same identity from `fstat`, then revalidates the complete directory-chain identities and resolved containment both
before and after bounded reading. File replacement, cache/hash-directory replacement, nonregular final handles,
reparse final handles, and unavailable identity fields all fail closed.

The atomic v2 reader loads and validates both catalog and map from manifest record zero before exposing buffered
records. Every catalog template must explicitly contain the producer's exact sorted `behavior_modules` array, with
an empty array representing no modules; omission is a schema failure rather than a default. The reader applies the
declared sample policy to every entity position/path goal and requires completion to repeat the exact single manifest
reference. Historical v1 remains permissive and byte-frozen.

## TDD and debugging evidence

The review-hardening pass began with focused RED collection failing because the hostile-file preflight constant did
not exist. After the first implementation pass the focused set was **67 passed, 7 failed, 2 skipped**; the seven were
four stale expected diagnostics and three assertions exposing incomplete closed contracts. The corrected focused set
reached **74 passed, 2 honest host skips** before full-suite integration.

Dedicated adversarial tests cover a small-declared zip bomb, oversized sparse manifest/member, concatenated streams,
truncation, trailing compressed data, exact compressed/raw size and hash mismatches, reparse-parent escape, hardlinks,
grid origin/dimension/storage-order tampering, duplicate feature identities, dangling waypoint links, invented/omitted
static categories, forged KindOf/lifecycle mask/locomotor set/module policy, unknown layers, and every guarded reset
call site. All load/reader tests assert atomic failure with no partial model exposure.

The final review corrections were also driven from RED: catalog omission originally advanced to a later feature-set
comparison; five file/directory replacement and unsafe-handle cases originally loaded without rejection; and the
duplicate-static-ID case hit an unrelated forbidden field first. Requiring catalog `behavior_modules`, descriptor
identity equality plus ancestor revalidation, and an otherwise-valid duplicate fixture made those exact cases GREEN.
The focused final contract group is **127 passed, 2 honest host skips in 20.59 s**.

The first hardened full-engine run was **136 passed, 6 failed, 3 skipped**. All six failures were the same strict OOB
sample: `SupplyDropZoneCrate` at `(2140.72949, -615.182434)`. Its exact lifecycle/catalog KindOf evidence is
`CRATE,PARACHUTABLE`; adding the closed PARACHUTABLE policy resolved all six without a heuristic exemption.

The next representative mechanics run reached static cross-binding and exposed a second real defect. The asset and
lifecycle/catalog both identified exactly 21 source-classified map-loaded objects, but the reader expected 15 because
six `SupplyPileSmall` objects (IDs 65-68, 119-120) were destroyed later in the match. A new RED test reproduced the
error. The producer now emits independent initialization membership at the post-map lifecycle flush, so later
destruction cannot erase initialization facts and pre-initialization destruction stays absent. The representative
CRC-free mechanics test then passed in **68.42 s**.

One apparent full-suite write failure was isolated to the diagnostic runner: forcing pytest under the long worktree
made the longest ANSI Win32 transaction member path 263 characters, while the normal pytest temp path is 220. The
exact failing catalog-determinism test passed under normal temp in **4.81 s**; the corrected definitive full suite
then passed. This limitation remains honest: overlong paths fail closed, discard owned telemetry/map temporaries, and
do not alter replay execution.

After the final descriptor/ancestor race hardening, the complete focused map-export file is **46 passed, 2 honest
host skips in 18.82 s**.

## Real replay evidence

The natural pinned replay remains SHA-256
`EA085767BFA11D2CFC167D9007173CE2EB29B5F557702FFD042E2E9A1A8F6BB8` and is used only to its established CRC
boundary. Its map asset strictly loads and has:

- content SHA-256 `32883fd68a05088a53690455369ae96d22bc8427e410b725b811ca1560e6d228` and exact
  manifest SHA-256 `ea80347103e3d674ddf436c9863582d0f3fa8a72e8652d30ebf660ef4fb95794`;
- pathing and sampled terrain dimensions `260 x 260`, 10-unit cells, and closed XY world bounds `[0,2600]`;
- height `39,123 / 270,400` compressed/raw bytes;
- ground pathing `1,583 / 67,600` bytes;
- amphibious pathing `1,078 / 67,600` bytes;
- raw terrain/cell types `1,811 / 67,600` bytes;
- zones `5,782 / 270,400` bytes; and
- 2 starts, 20 named waypoints, 0 bridges, and 21 classified static objects.

Exact current member hashes (compressed / uncompressed) are:

- height: `3f2edd7a13e5bcf1e5746c6424ed6f4174dc615ba6252f19f9a476acee9e9645` /
  `fbd74a377a40f624990645f3d534e1ea4c1a457281373d11d8936cbc64a90709`;
- ground pathing: `34d8c296e88744dea0aeae2a28b48a50cb1953ce9cc98e94e1948a4d98ebb2f1` /
  `ce6bb84f7e1a2be67315a2fabc1116448022934175a3152b88a60fc6e0470203`;
- amphibious pathing: `fea4b10015030f0fd7b21aa0d1ca0e3a4f7bdb76f99586336af26d70a107d6af` /
  `6e8771d563ec152c91480e03b2fd187ac2f8da3114369862268b6a9d3c808546`;
- terrain: `58b84ba400e5aa883d71cc119f725d6f11cccc95e7f3d2e8edda8ee2bf1ed958` /
  `68d63addb06213a35cfa277a4533517e75ae1b4e3b21bf7a3d983f86c54e071c`; and
- zones: `7aff66599548d07db7e57c40163449190871eea9fa31cf0196e99ad48b404955` /
  `7d349953208ff5e9dcb449c2f679e8130807dd1150fafcb436cd9da352d4b29e`.

Two natural runs produce the exact same reference and bytes; the validated cache hit changes no timestamp. All
eligible natural entity samples are inside the declared policy. The disposable CRC-stripped derivative remains
mechanics-only evidence, makes no strategy/winner/player-performance claim, and now strictly validates every eligible
sample through clean completion with telemetry-on/off replay outcome parity.

The retained clean-completion derivative trace contains 46,086 entity samples; the atomic reader strictly validated
every position/path goal against the asset and independently bound all 21 static initialization objects. It remains
mechanics-only evidence and supports no strategy, winner, or player-performance conclusion.

## Final verification

- full non-engine suite: **509 passed, 152 engine tests deselected** in **29.05 s**;
- full engine suite: **149 passed, 3 skipped** in **325.51 s**;
- Ruff: **passed**;
- strict mypy: **passed, 19 source files**;
- `git diff --check`: **passed**; and
- modern VS 2022 x86 `z_generals`: **built and linked `generalszh.exe`** after the final source changes.

The three engine skips are honest host capability limits, including unprivileged symlink/reparse creation. VC6 and
MinGW are unavailable on this host, so no compile claim is made for either. Compatibility evidence is limited to modern-only
guards, CMake exclusion from VC6, static source tests, and the modern x86 build. The pinned map contains no bridge, so
bridge export has source/static contract coverage but no nonempty real-map bridge fixture. The five inherited stat-only
worktree paths were neither edited by Task 8 nor staged.
