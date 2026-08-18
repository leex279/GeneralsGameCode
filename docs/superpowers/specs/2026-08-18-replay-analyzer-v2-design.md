# Replay Analyzer V2 Design

**Status:** Approved

**Date:** 2026-08-18

**Branch:** `feat/replay-analyzer-v2`

**Primary target:** Command & Conquer: Generals - Zero Hour 1.04

## 1. Summary

Replay Analyzer V2 is a local, evidence-backed replay library and analysis application for Zero Hour. It combines a strict offline `.rep` parser with passive telemetry exported by real headless GameLogic replay playback. The resulting facts, derived features, strategy assessments, and longitudinal player patterns are stored in SQLite and presented through a local web application.

Ollama supplies contextual strategy interpretation and narrative reports. It is not a source of match facts. Every LLM conclusion is schema-validated, confidence-scored, and linked to observed or reproducibly derived evidence.

This design replaces the current prototype's synthetic world reconstruction, procedural map guesses, hard-coded skill scoring, duplicate parsers, and generated repository-root media with a versioned and testable analysis pipeline.

## 2. Context and Current-State Findings

The existing checkout contains useful replay work alongside provisional and generated artifacts:

- `RecorderClass` already implements the authoritative engine replay header and command reader.
- Headless replay simulation already provides the basis for retail compatibility checks.
- The Python prototype can decode portions of the header and command stream, but it lacks fixture tests and parser parity checks.
- Player mapping, entity-template mapping, timing, and strategy rules contain unverified assumptions and magic numbers.
- The CLI defaults to 15 logic frames per second while the parser defaults to 30, making timestamps inconsistent.
- Synthetic replay modules invent starting structures, resource nodes, completion times, units, movement, weapon effects, and health state that are not present in the command stream.
- The procedural map fallback invents geography and cannot support factual spatial analysis.
- Strategy and skill labels are presented without evidence provenance or calibrated confidence.
- There is no persistent database, migration system, collection analysis, or trustworthy same-player history.
- There are no automated replay-analyzer tests.
- Generated videos, audio, images, binaries, and diagnostic files are mixed into the repository root.

These findings make incremental polishing unsafe. V2 establishes authoritative boundaries first and reuses prototype code only where tests prove it correct.

## 3. Goals

1. Parse Zero Hour replay headers and commands strictly and reproducibly.
2. Verify offline parsing against the authoritative C++ reader.
3. Execute replays through real GameLogic and passively export authoritative match telemetry.
4. Extract real map, navigation, resource, and coordinate context for spatial analysis.
5. Persist replay, player, map, event, feature, strategy, and analysis-run history in SQLite.
6. Identify opening strategies, phase transitions, tactical patterns, and player tendencies with explicit evidence and confidence.
7. Compare the same player across collections by faction, matchup, opponent, map, patch, and time period.
8. Use local Ollama models for structured interpretation without making the LLM a source of facts.
9. Provide a polished local web application plus a CLI for batch import and automation.
10. Preserve retail replay determinism and VC6 compatibility.

## 4. Non-Goals for V1

- Reimplementing Zero Hour GameLogic in Python or JavaScript.
- Treating command-only reconstruction as authoritative world state.
- Automated scraping or bulk downloading from Strata.
- A remotely hosted multi-user service.
- Authentication, public sharing, or internet-facing deployment.
- Live-game coaching or multiplayer interception.
- Generals 1.08 support before the Zero Hour pipeline is reliable.
- TTS, commentary audio, broadcast video generation, or camera direction as part of the analysis core.
- A vector database or embeddings without demonstrated retrieval value.
- Balance recommendations based on uncalibrated model opinion.

## 5. Evidence Model

All stored conclusions belong to exactly one evidence tier:

| Tier | Definition | Example |
|---|---|---|
| Observed | Decoded directly from the replay or emitted by GameLogic | Player queued a War Factory at frame 1,842 |
| Derived | Reproducibly calculated from observed records | War Factory timing was 61.4 seconds |
| Inferred | Contextual interpretation with confidence | Fast War Factory pressure, confidence 0.91 |

Observed records are immutable for a given parser or telemetry schema version. Derived records are replaceable by a newer feature extractor without changing observations. Inferred records never overwrite either layer.

Every derived or inferred record includes:

- The analyzer or detector version.
- The exact input replay and telemetry versions.
- Evidence references.
- Creation time and run identifier.
- Confidence where the output is probabilistic.

## 6. System Architecture

```text
Downloaded .rep files
        |
        +--> Source/provenance extraction
        |       filename, Strata match ID, source user token, SHA-256
        |
        +--> Strict offline replay parser
        |       header, players, map, version, commands, arguments
        |                         |
        |                         +--> C++ parser parity validation
        |
        +--> Headless Zero Hour replay playback
                real GameLogic + passive telemetry exporter
                                |
                                +--> Versioned NDJSON trace
                                +--> Real map/navigation export
                                             |
                                             v
                                          SQLite
                                             |
                    +------------------------+-----------------------+
                    |                        |                       |
             Deterministic features   Collection statistics   Evidence bundles
                    |                        |                       |
                    +------------------------+--> Ollama <----------+
                                             |
                                     Structured assessments
                                             |
                                      Local web application
```

The offline parser and engine playback are complementary. The offline parser provides fast discovery, metadata, deduplication, and command inspection. GameLogic playback provides actual outcomes and state transitions that the command stream alone cannot establish.

## 7. Offline Replay Parser

### 7.1 Source of Truth

The binary contract is derived from the C++ definitions used by `RecorderClass`, `GameMessage`, `MessageStream`, and replay header serialization. Python enum values, argument widths, player-slot decoding, frame rate, and command schemas may not be maintained as independent guesses.

The implementation will document:

- Header field order, width, signedness, and string encoding.
- Game options and player slot grammar.
- Command frame and player-index encoding.
- Argument type runs and data widths.
- Replay terminators and truncated-file behavior.
- Version-specific differences.
- Unknown command and argument handling.

### 7.2 Parser Output

The parser emits a typed representation containing:

- Replay hash and source metadata.
- Version, build, CRCs, map, seed, starting cash, dates, and frame count.
- Original player names, slot data, factions, colors, teams, and local player index.
- Commands with frame, resolved player reference, numeric message type, symbolic name where known, and typed arguments.
- Warnings with exact byte offsets.
- Completion, truncation, desync-header, and unsupported-version status.

Unknown data is preserved as raw bytes where its length is known. The parser fails closed when an unknown type prevents reliable stream alignment.

### 7.3 Parser Parity

A modern-build diagnostic mode will dump the authoritative C++ interpretation for fixture replays. Automated tests compare:

- Header values.
- Player slots and names.
- Command counts.
- Frames and message types.
- Player indices.
- Argument types and values.
- End-of-stream position.

Offline parsing is not considered authoritative until parity passes for the supported fixture corpus.

## 8. GameLogic Telemetry Exporter

### 8.1 Invocation

The target user workflow is equivalent to:

```text
generalszh.exe -headless -replay <file.rep> -telemetry <trace.ndjson>
```

The orchestration layer supplies an isolated output path and analysis-run identifier, launches the game, records process output and exit status, validates the completed trace, and only then imports it.

### 8.2 Event Envelope

Every NDJSON record contains:

- `schema_version`
- `run_id`
- `sequence`
- `frame`
- `logic_time_seconds`
- `event_type`
- `payload`

The trace begins with a manifest record containing engine build identity, replay version, map identity, initial seed, and exporter settings. It ends with a completion record containing final frame, outcome, replay/CRC status, counts, and clean-shutdown state.

Evidence references use `(run_id, sequence)` and remain stable after import.

### 8.3 Observation Families

The exporter records:

- Player and team initialization.
- Object creation, construction start/completion, owner changes, sale, and destruction.
- Template, kind-of flags, owner, team, locomotor class, and relevant cost/value data.
- Unit queues, cancellations, production completion, upgrades, sciences, and special powers.
- Cash balance changes, income, spending, supply collection, and classified reason where the engine has authoritative context.
- Damage, healing, attacker, victim, weapon, killing blow, and veterancy changes.
- Orders, actual entity state transitions, and resolved targets.
- Player defeat, surrender, disconnect, and winner state.
- Replay mismatch, CRC, truncation, and completion diagnostics.

The event family is extensible. Unknown future event payload fields remain importable under the versioned raw-event record.

### 8.4 Determinism and Compatibility

Telemetry is observational only:

- No telemetry result may feed back into GameLogic.
- The exporter may not call client/UI state to classify simulation facts.
- Telemetry ordering is derived from logic frame and a monotonic local sequence.
- File I/O failure marks the analysis failed but does not alter simulation decisions.
- The exporter and its `-telemetry` command-line option are compiled out for VC6 through the existing compiler guards.
- Modern-only implementation details must not change legacy data layout or command execution.
- Replay compatibility and CRC tests run with telemetry disabled and enabled to prove non-interference.

Every user-facing or architectural C++ change follows the required `TheSuperHackers @keyword` comment convention.

## 9. Map and Spatial Intelligence

### 9.1 Static Map Export

Game/map data is loaded through the actual engine. V1 does not depend on a Python reimplementation of the map format. For each unique map content hash, the exporter produces:

- Canonical map identity and world bounds.
- Height and terrain grids.
- Traversability by relevant locomotor class.
- Blocked cells, cliffs, water, bridges, structures, and obstacles.
- Starting positions and waypoints.
- Supply docks, piles, oils, and capturable objects.
- Connectivity regions and navigation links.

Dense grids are stored as compressed, versioned sidecar assets keyed by map hash. SQLite stores asset metadata, dimensions, cell size, checksums, resources, regions, and graph relationships. It does not store millions of grid cells as individual rows.

### 9.2 Dynamic Spatial Telemetry

The exporter records:

- Entity position, orientation, owner, and locomotor class.
- Movement/order targets and actual path samples.
- Spawn, retreat, reinforcement, capture, and destruction locations.
- Engagement centers, participants, damage, and casualties.

Structures are sampled on events. Moving entities receive samples on state/order changes and at a default maximum interval of 15 logic frames while moving. The exporter may coalesce unchanged positions. The sampling interval is recorded in the trace manifest and is configurable for performance studies.

### 9.3 Coordinate Frames

Every analyzed position supports:

1. Raw engine coordinates.
2. Map-normalized coordinates in the range `0.0` to `1.0`.
3. Player-centric coordinates rotated or mirrored so forward, rear, left flank, right flank, and enemy side are comparable across starts.

Player-centric transforms are versioned derived data. Raw coordinates are never discarded.

### 9.4 Spatial Features

Spatial analysis includes:

- Worker and supply routing efficiency.
- Expansion, forward production, tunnel, and defensive placement.
- Reachable travel distance rather than only Euclidean distance.
- Army concentration, separation, reinforcement, and retreat.
- Harassment and flank routes.
- Chokepoint and resource control.
- Map-control estimates by time window.
- Engagement location, duration, intensity, and outcome.
- Repeated positioning habits by player, faction, map, and matchup.

When real map or engine telemetry is unavailable, these features are absent and reports are explicitly downgraded. Procedural map guesses are never presented as analysis evidence.

## 10. Persistence and Data Ownership

### 10.1 Technology and Location

The application uses SQLite with foreign keys and WAL mode. SQLAlchemy 2 manages persistence and Alembic manages migrations. Runtime data is never stored in the Git repository by default.

The default Windows data root is:

```text
%LOCALAPPDATA%\GeneralsReplayAnalyzer\
```

It contains the database, managed replay copies, traces, map sidecars, logs, and cached reports. Every path is configurable.

### 10.2 Core Tables

| Table | Responsibility |
|---|---|
| `sources` | Original path/URL, filename, Strata identifiers, discovery time, and import provenance |
| `replays` | SHA-256 identity, header, version, map, duration, quality state, and deduplication |
| `maps` | Canonical map hash, bounds, exporter version, and sidecar references |
| `map_resources` | Supplies, oils, capturable objects, and starting positions |
| `map_regions` | Connectivity regions, chokepoints, lanes, and derived labels |
| `players` | Canonical player identities |
| `player_aliases` | Normalized and original names mapped to canonical players |
| `replay_players` | Slot, team, faction, result, start position, and original replay name |
| `commands` | Strictly decoded replay commands and typed argument payloads |
| `telemetry_runs` | Engine/exporter identity, settings, status, and trace checksums |
| `telemetry_events` | Versioned raw observed events and evidence sequence |
| `entities` | Replay-local entity identity and stable template/owner attributes |
| `entity_samples` | Event-driven and sampled trajectories |
| `production_events` | Queues, cancellations, completions, upgrades, and sciences |
| `economy_events` | Cash, income, spending, and supply events |
| `combat_events` | Damage, kills, healing, trades, and engagement membership |
| `feature_sets` | Analyzer/version identity and feature-bundle metadata |
| `features` | Typed derived values with scopes, windows, units, and evidence |
| `strategy_assessments` | Rule, statistical, LLM, or manual strategy conclusions |
| `assessment_evidence` | Links from conclusions to observed and derived evidence |
| `analysis_runs` | Model, prompt, schema, inputs, settings, status, and cache identity |
| `reports` | Structured report JSON and rendered report versions |
| `jobs` | Resumable background pipeline stages, leases, retries, and errors |

High-volume payloads retain a normalized query surface and a versioned JSON payload for forward compatibility. Migrations are transactional and backed up before destructive schema changes.

### 10.3 Player Identity

The replay's embedded player name is the primary observed name. Identity rules are:

- Unicode-normalized, trimmed, case-folded exact names auto-link.
- Original spelling and casing are preserved per replay.
- Different normalized names are never silently merged.
- Manual merge and split operations are available in the web application.
- Alias changes are audited and reversible.
- Strata match IDs and filename `user_...` tokens are source provenance, not player identity.
- External provider identities may be linked only through an explicit provider namespace and evidence.

The verified sample replay contains `leex279` and `FOX27` in its embedded player slots. It does not contain the Strata match number `3133811` or the filename user token in text, standard integer encodings, or raw token bytes.

## 11. Import and Provenance

V1 imports individual `.rep` files, selected folders, and configured watched folders. It does not scrape Strata.

For filenames matching:

```text
match_<match-id>_user_<source-user-token>_replay.rep
```

the importer records both external values in `sources`, without treating either as a player ID. The SHA-256 of replay content is the deduplication key. Multiple source records may point to the same replay.

Imports copy replays into the managed library by default, using their content hash as the managed filename. The original path and filename remain provenance. A configuration option permits reference-only imports when the user controls source retention.

## 12. Deterministic Analysis

### 12.1 Feature Families

Versioned feature extractors calculate:

- Build orders and milestone timings.
- Production composition and transitions.
- Economy balance, income, spend, float, and worker efficiency.
- Army value, reinforcements, losses, and trade efficiency.
- Technology and upgrade timings.
- Activity, effective actions, and command distributions without equating raw APM with skill.
- Aggression, harassment, defensive posture, map presence, and control windows.
- Engagement initiation, response, focus, retreat, and outcome.
- Strategy candidates and phase transitions.

Hard-coded entity IDs are not the long-term semantic source. Templates and game data are exported into a versioned catalog containing stable names, faction, categories, costs, build time, prerequisites, and relevant capabilities.

### 12.2 Strategy Taxonomy

Strategies use a versioned taxonomy with:

- Faction and matchup applicability.
- Phase: opening, early, mid, late, or cross-phase.
- Required and supporting features.
- Contradicting evidence.
- Synonyms and display names.
- Minimum evidence quality.

Rule detectors propose candidates. Statistical comparison measures similarity to known patterns and player baselines. Ollama interprets the combined evidence. Manual corrections remain distinct and may become annotated evaluation fixtures.

### 12.3 Longitudinal Analysis

Player comparisons are segmented by:

- Faction and subfaction.
- Opponent faction and identity.
- Map and start position.
- Game build/patch.
- Time period.
- Replay quality tier.

The system reports distributions and sample size, not only averages. It identifies recurring openings, timing bands, transition preferences, opponent-specific adaptations, deviations from personal baselines, and changes over time. Claims requiring multiple matches enforce configurable minimum sample sizes and show uncertainty.

## 13. Ollama Integration

### 13.1 Provider Contract

Ollama at `http://127.0.0.1:11434` is the first provider. The provider interface remains small enough to support another local OpenAI-compatible server later without changing analysis-domain code.

The initial configurable default model is `qwen3.6:27b`. The exact model digest, context settings, prompt version, response schema, and generation settings are stored with every run.

The default generation settings use temperature `0` and seed `0`. Any override becomes part of the analysis-run identity and cache key.

### 13.2 Evidence Bundle

Ollama receives a compact, deterministic dossier rather than replay bytes or an unbounded event stream. It contains:

- Match and evidence-quality context.
- Player, opponent, faction, map, and start context.
- Phase timelines and feature summaries.
- Economy, army, engagement, and spatial evidence.
- Deterministic strategy candidates.
- Relevant collection baselines selected by explicit SQLite queries.
- Evidence identifiers for all supplied claims.

### 13.3 Structured Output

The response is schema-constrained JSON containing:

- Phase strategy labels.
- Transitions and adaptations.
- Recurring patterns and deviations.
- Strengths, weaknesses, and likely mistakes.
- Opponent-specific responses.
- Confidence for each conclusion.
- Evidence identifiers.
- A concise narrative summary.

Validation rejects unknown evidence IDs, malformed schemas, and unsupported numeric claims. Valid inferences are stored separately from observed and derived data.

### 13.4 Reproducibility and Failure

- Generation uses low-temperature settings.
- Results are cached by replay/feature/model/prompt/schema digest.
- Users can rerun or compare models.
- Ollama unavailability does not block deterministic analysis.
- Failed model output is retained as diagnostic run data but not shown as a valid assessment.
- Manual corrections never mutate the original model response.

## 14. Local Web Application

### 14.1 Stack

The recommended stack is:

- Python 3.11 or newer.
- FastAPI and Pydantic 2.
- Jinja templates with HTMX for navigation, forms, filters, and job updates.
- ECharts and focused browser-side code for timelines, heatmaps, paths, and comparisons.
- SQLAlchemy 2 and Alembic.
- Pytest for application, storage, and API tests.

V1 does not introduce React, Vite, or a separate Node application.

### 14.2 Screens

- Dashboard with library totals, recent imports, analysis status, notable matches, and quality warnings.
- Replay library with filters for player, faction, map, matchup, patch, date, result, strategy, and evidence tier.
- Match report with phases, build order, curves, engagements, turning points, assessment confidence, and evidence links.
- Spatial analysis with the real map, trajectories, attack routes, expansions, proxies, control overlays, and time scrubber.
- Player profile with strategy frequencies, timing distributions, matchup performance, recurring tendencies, and evolution over time.
- Comparison view for players, matches, openings, strategies, or time periods.
- Identity manager with audited merge, split, and alias operations.
- Import/jobs view with watched folders, progress, failures, and reruns.
- Settings for executable, game data, replay folders, data root, Ollama endpoint/model, and telemetry sampling.
- Evidence inspector linking reports to commands, telemetry, features, prompts, and model output.

### 14.3 Local Security

The server binds to `127.0.0.1` by default and has no authentication in V1. It never binds to all interfaces without an explicit setting and warning. Replays and analysis data remain local. Ollama requests target the configured local endpoint.

## 15. Background Job Pipeline

The resumable stages are:

```text
discover -> hash -> parse -> engine telemetry -> map/spatial -> features -> Ollama -> report
```

Each stage has explicit inputs, outputs, version identity, status, attempt count, lease, timestamps, and structured error. A worker process claims jobs transactionally. Browser closure does not interrupt analysis.

Re-import is idempotent. A stage reruns automatically when its implementation version or relevant settings change, without duplicating immutable observations.

## 16. Quality States and Error Handling

Every replay has one of these user-visible quality states:

- `discovered`
- `parsed`
- `engine_verified`
- `partial`
- `desynced`
- `unsupported`
- `failed`

Reports display the available evidence tier. A raw-only replay cannot receive conclusions that require engine telemetry. Partial traces remain inspectable but are excluded from conclusions requiring complete match outcomes.

All errors include stage, category, message, relevant path or record, retryability, and diagnostic details. The web interface provides retry actions only where retry is meaningful.

## 17. Repository Cleanup Boundary

### 17.1 Keep and Validate

- Replay playback fixes that pass fixtures and retail compatibility checks.
- Object-ID and player-index corrections proven necessary by representative replays.
- Headless replay execution.
- Useful camera/video code isolated as a separate optional concern.
- Parser logic proven by parity tests.

### 17.2 Replace or Quarantine

- Synthetic unit and world simulators.
- Procedural fake map data.
- Hard-coded semantic entity mappings without an exported catalog.
- Duplicate Python and browser replay parsers.
- Fixed local installation paths.
- Monolithic generated HTML reports.
- Unversioned heuristic skill scores.
- Analysis code coupled to TTS, commentary, or video rendering.

### 17.3 Generated Artifacts

Repository-root MP4, MP3, PNG, EXE, OBJ, and diagnostic outputs are not source. Before removal, implementation work will inventory them, identify any required fixtures, move small intentional fixtures into test-data directories, and document recoverability. Large generated artifacts are excluded from Git.

Cleanup commits remain separate from new behavior so reviewers can distinguish deletion, refactoring, and implementation.

## 18. Testing and Verification

### 18.1 Parser

- Golden replay-header fixtures.
- Command/argument fixtures for every supported type.
- Truncated and corrupt replay cases.
- Unknown command/type behavior.
- Full Python-versus-C++ parity across representative Zero Hour replays.
- Exact timestamp/frame-rate tests.

### 18.2 Engine Telemetry

- Schema contract tests.
- Event ordering and completion manifest tests.
- Object lifecycle, economy, combat, and outcome fixtures.
- Replay CRC compatibility with telemetry disabled and enabled.
- VC6 build verification with the exporter and telemetry option compiled out.
- Modern Win32 build verification.

### 18.3 Storage and Jobs

- Migration up/down safety for supported development migrations.
- Foreign-key and transaction tests.
- Duplicate replay/source handling.
- Identity merge/split reversibility.
- Worker lease, retry, crash recovery, and idempotency tests.

### 18.4 Spatial

- Raw-to-normalized coordinate invariants.
- Player-centric rotation/mirroring fixtures.
- Map hash and sidecar integrity.
- Path-distance and region fixtures.
- Sampling/coalescing correctness.

### 18.5 Analysis and Ollama

- Deterministic feature fixtures.
- Human-annotated strategy examples.
- Minimum-sample and confidence behavior.
- Schema-valid mocked Ollama outputs.
- Unknown-evidence and unsupported-claim rejection.
- Optional live Ollama integration test.
- Cache-key and rerun reproducibility.

### 18.6 Web

- API and form tests.
- Import, filter, comparison, identity, retry, and evidence-inspection browser flows.
- Accessible table/chart alternatives for essential values.
- Large-library pagination and query performance.

## 19. Performance Expectations

- Discovery and hashing stream files without loading entire collections into memory.
- SQLite uses indexed foreign keys and common filter composites.
- Large event imports use bounded transactions and prepared/bulk inserts.
- Map grids and raw traces remain compressed sidecars after verified import.
- Match pages query precomputed feature bundles rather than scanning raw events.
- Collection recomputation is incremental by player and feature version.
- A benchmark corpus measures parser throughput, telemetry overhead, database growth, report latency, and batch recovery.

No fixed performance claim is made before the benchmark corpus exists. Regressions are evaluated against recorded baselines.

## 20. Delivery Stages

1. Cleanup inventory and representative fixture corpus.
2. Authoritative offline parser and C++ parity dump.
3. SQLite schema, migrations, ingestion, provenance, and identity.
4. Passive GameLogic telemetry exporter.
5. Real map export and spatial intelligence.
6. Deterministic features, strategy taxonomy, and collection statistics.
7. Ollama structured analysis and validation.
8. Local web application and background jobs.
9. Longitudinal reports, calibration, performance, and hardening.

Each stage ends in independently testable software. Zero Hour remains first. Generals work starts only after the corresponding Zero Hour stage is reliable and the implementation can be mirrored safely.

## 21. Acceptance Criteria

V1 is complete when:

1. A folder of Zero Hour replays can be imported idempotently.
2. Embedded player names and Strata filename provenance are stored separately and correctly.
3. Offline parser output matches the C++ reader for the supported fixture corpus.
4. Supported replays can run headlessly and produce a complete, versioned telemetry trace without changing replay CRC behavior.
5. Real map/navigation data and player-relative coordinates support movement and control analysis.
6. SQLite persists observed, derived, and inferred data with evidence links and migrations.
7. The application identifies phase strategies and recurring player patterns with sample size, confidence, and evidence.
8. Ollama produces schema-valid assessments and deterministic reports remain available when Ollama is offline.
9. The local web application supports replay library, match report, spatial view, player history, comparison, identity management, jobs, settings, and evidence inspection.
10. Generated artifacts and synthetic analysis prototypes are no longer mixed with trusted source or presented as factual analysis.
11. Modern Win32 builds, VC6 compatibility checks, parser tests, telemetry tests, database tests, analysis tests, and web tests pass at the required stage boundaries.

## 22. Approved Decisions

- Use a hybrid offline-parser plus real-GameLogic telemetry architecture.
- Make map-aware spatial analysis mandatory for deep analysis.
- Use SQLite as the local persistent store.
- Use Ollama first, with `qwen3.6:27b` as the initial configurable default.
- Keep deterministic facts and features independent of Ollama.
- Use a local FastAPI/Jinja/HTMX web application with ECharts.
- Import local files and folders in V1; store Strata provenance without scraping Strata.
- Normalize exact embedded names automatically and provide explicit alias merge/split controls.
- Preserve Zero Hour-first development and retail determinism.
