# Replay Analyzer Engine-Native Insights Design

Date: 23 August 2026

Status: approved product direction

## Outcome

Replay Analyzer reuses additional authoritative Zero Hour providers for final score totals, economy-rate validation, scouting visibility, and engine AI pressure/value heuristics. The engine remains the source of reconstructed match state. Python validates, stores, reconciles, analyzes, and presents exported observations; it never simulates movement, combat, economy, shroud, or scoring.

## Safety boundary

- Zero Hour is implemented and verified first.
- Sampling is observational, deterministically ordered, bounded, and compiled only for `RTS_REPLAY_ANALYZER && !IS_VS6_BUILD`.
- Sampling occurs after `PartitionManager::UPDATE()` and before the logic-frame increment.
- Terminal samples occur before `match_outcome` and telemetry completion.
- No sampler consumes RNG, retains `Object *`, changes iteration order, calls `ScoreKeeper::calculateScore()`, or writes GameLogic state.
- Never call `Object::getShroudedStatus()` or `PartitionData::getShroudedStatus()` from telemetry. Those paths mutate shroudedness, ever-seen flags, and ghost snapshots.
- Visibility v1 means the sampled state of an object's center partition cell through `PartitionManager::getShroudStatusForPlayer()`. It is labelled `first_observed_clear`, not exact engine ever-seen state.

## Versioned telemetry additions

Telemetry v2 accepts four optional event families. Older valid v2 traces remain valid.

### `scorekeeper_snapshot`

Emit exactly once at the terminal frame, immediately before `match_outcome`. One sorted player entry contains the raw signed `Int` getters for money earned/spent, units and buildings built/lost/destroyed, and tech/faction buildings captured. The source is `Player::getScoreKeeper()`. Do not call `calculateScore()`.

### `cash_per_minute_snapshot`

Emit a sorted aggregate snapshot every logic second plus a forced terminal sample: 30 frames for retail profiles and 60 frames for the Generals Online high-FPS profile. Each resolved occupied player records `has_money` and `Money::getCashPerMinute()`. Extend `cash_changed` with optional `tracked_income_amount` and `income_bucket_index`, present exactly when the deposit tracks income, so the reader can reproduce the 60 one-second unsigned buckets across rotation-boundary deposits. The payload interval and bucket width must equal the manifest `logic_frames_per_second` value.

### `object_visibility_changed`

Sample resolved-player/live-object pairs every half logic second (15 frames at 30 Hz, 30 frames at 60 Hz) with a deterministic round-robin cursor and a maximum of 8,192 pairs per pass. Emit transitions among telemetry-owned `unseen` and engine states `clear|fogged|shrouded`, including `first_observed_clear`, object/template/player identities, sampled position, cycle identity, interval, source, and `object_center_partition_cell` basis. Emit a sampling summary for every pass so capped scans cannot appear exhaustive.

### `partition_engine_grid_sample`

Every ten logic seconds plus terminal (300 frames at 30 Hz, 600 frames at 60 Hz), sample a deterministic row-major uniform lattice of at most 128 unique partition cells per resolved player, including map edges. Record cell index/world position, shroud status, `getThreatValue()`, and `getCashValue()`. Labels must say `Engine AI threat heuristic` and `Engine AI cash-value heuristic`; these values are not territory, danger probability, resources, or objective map control.

The lattice contract is exact. Choose integer `(sample_count_x, sample_count_y)` from the valid range `1..cell_count_x`, `1..cell_count_y` with a product no greater than 128. Maximize sample count first, minimize aspect distortion `abs(sample_count_x * cell_count_y - sample_count_y * cell_count_x)` second, then prefer the larger X count for a deterministic tie. For either axis with one sample use index `0`; otherwise axis sample `i` maps to `floor(i * (cell_count - 1) / (sample_count - 1))`. Emit the Cartesian product with Y outer and X inner. This produces true 2D coverage, includes all four available map corners, fixes row-major ordering, and remains cross-run comparable.

## Analytics and reconciliation

The generic telemetry-event table stores the new events; no specialized-table migration is required. Derived features expose cash-per-minute series/latest/peak, exact bucket reconciliation share, terminal ScoreKeeper snapshots, and explicitly caveated event reconciliation.

ScoreKeeper and event-derived totals use different filters and controller attribution. A difference is `partial` or `not_comparable`, never trace corruption. Every comparison records engine total, event-derived total, delta, status, and a stable semantic reason.

## Product presentation

The fixed-report map contract advances to `replay-map-scene-v2`. It exposes typed visibility transitions and opt-in sampled shroud, engine AI threat, and engine AI cash-value overlays with evidence links and equivalent non-JavaScript tables. Scouting summaries answer when important enemy objects were first observed clear and where sampled visibility remained incomplete. Strategy/coaching may cite these observations but cannot infer unseen intent or rename heuristics as control.

## Acceptance

- Contract, reader, engine-source, feature, map, accessibility, and old-trace compatibility tests pass.
- Modern Zero Hour Release and Debug build.
- VC6 builds prove analyzer-only exclusion.
- Telemetry disabled/enabled replay terminal facts and CRC behavior are identical.
- Three runs produce byte-identical normalized traces.
- The ten-replay corpus passes before regenerated hashes are accepted.
