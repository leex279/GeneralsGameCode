# Replay Analyzer V2 Analytics Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist replay evidence in SQLite and produce reproducible match, spatial, strategy, and longitudinal player analysis with optional schema-constrained Ollama interpretation.

**Architecture:** A resumable staged importer writes immutable parser and telemetry observations, then versioned deterministic extractors write replaceable derived feature sets. Strategy rules and population comparisons create evidence-backed candidates. Ollama receives compact evidence bundles and returns validated inferred assessments; cache identity includes every fact and model setting that could change the result.

**Tech Stack:** Python 3.11, Pydantic 2, SQLAlchemy 2, Alembic, SQLite WAL/foreign keys, NumPy, NetworkX, SciPy, httpx, Ollama HTTP API, pytest and Hypothesis.

**Spec:** `docs/superpowers/specs/2026-08-18-replay-analyzer-v2-design.md` sections 5, 10 through 15, 17, and 18.

## Global Constraints

- Depend on completed parser-parity and telemetry acceptance gates.
- Store source paths and Strata identifiers as provenance, never as automatic player identity.
- Treat parser and telemetry observations as immutable per schema/version. Re-analysis creates new derived/inferred rows.
- Every feature has scope, window, unit, extractor version, and evidence links.
- Never substitute a fabricated zero for missing evidence. Store null/absent plus a quality reason.
- Never let Ollama write observations, derived values, canonical player identities, or manual corrections.
- Use database transactions per pipeline stage; an incomplete stage must not look successful.
- Default to `%LOCALAPPDATA%\GeneralsReplayAnalyzer\`; tests use temporary roots only.

---

## Task 1: Add application paths, configuration, and content-addressed storage

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/config.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/storage.py`
- Create: `scripts/replay_analyzer/tests/test_config.py`
- Create: `scripts/replay_analyzer/tests/test_storage.py`
- Modify: `scripts/replay_analyzer/pyproject.toml`

- [ ] Add `platformdirs>=4.4,<5` and `pydantic-settings>=2.10,<3`.
- [ ] Write tests for Windows default root, explicit environment/config overrides, directory creation, unsafe repository-root rejection, atomic content writes, hash mismatch, and deduplication.
- [ ] Define `AnalyzerSettings` with data root, database path, managed replay directory, run directory, map asset directory, cache directory, log directory, engine executable, Ollama URL/model, watched folders, copy/reference import mode, and minimum longitudinal sample size.
- [ ] Reject a default or configured runtime root inside the Git checkout unless tests explicitly set `allow_repository_data_root=True`.
- [ ] Implement content-addressed storage with temporary-file write, fsync, SHA-256 verification, and atomic rename. Never overwrite different bytes at an existing hash path.
- [ ] Run tests and commit with `feat(replay): Add analyzer runtime storage`.

## Task 2: Create the SQLite schema and migration baseline

**Files:**
- Create: `scripts/replay_analyzer/alembic.ini`
- Create: `scripts/replay_analyzer/alembic/env.py`
- Create: `scripts/replay_analyzer/alembic/script.py.mako`
- Create: `scripts/replay_analyzer/alembic/versions/0001_replay_analyzer_v2.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/db/base.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/db/models.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/db/session.py`
- Create: `scripts/replay_analyzer/tests/db/test_migrations.py`
- Create: `scripts/replay_analyzer/tests/db/test_constraints.py`
- Modify: `scripts/replay_analyzer/pyproject.toml`

- [ ] Add `sqlalchemy>=2.0,<2.1` and `alembic>=1.16,<2`.
- [ ] Write migration tests that upgrade an empty database to head, downgrade to base, upgrade again, enable `PRAGMA foreign_keys=ON`, select WAL mode, and pass `PRAGMA foreign_key_check` and `PRAGMA integrity_check`.
- [ ] Implement all spec tables: `sources`, `replays`, `maps`, `map_resources`, `map_regions`, `players`, `player_aliases`, `replay_players`, `commands`, `telemetry_runs`, `telemetry_events`, `entities`, `entity_samples`, `production_events`, `economy_events`, `combat_events`, `feature_sets`, `features`, `strategy_assessments`, `assessment_evidence`, `analysis_runs`, `reports`, and `jobs`.
- [ ] Use integer primary keys internally and stable public UUIDs/run IDs where evidence references cross files. Require unique replay SHA-256, unique `(telemetry_run_id, sequence)`, unique managed asset hash, and unique normalized alias per canonical player mapping operation.
- [ ] Store evidence tier as a check-constrained string: `observed`, `derived`, or `inferred`. Store structured payloads as canonical JSON text with validated companion columns for frequent queries.
- [ ] Index player aliases, replay hash, source external IDs, replay-player identity, event frame/type, entity samples, feature name/scope/window, strategy label, job state/lease, and analysis cache key.
- [ ] Run tests and commit with `feat(replay): Add replay analysis database`.

## Task 3: Implement deduplicating replay import and resumable jobs

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/importing/service.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/importing/jobs.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/importing/stages.py`
- Create: `scripts/replay_analyzer/tests/importing/test_import_service.py`
- Create: `scripts/replay_analyzer/tests/importing/test_jobs.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/cli.py`

- [ ] Write tests for file import, recursive folder import, extension filtering, duplicate content at multiple paths, copy/reference mode, source deletion after copy, parser failure, stage retry, lease expiration, and process restart.
- [ ] Define stages `discover`, `hash`, `manage_copy`, `parse`, `telemetry`, `import_observations`, `derive_features`, `assess_strategies`, `analyze_llm`, and `render_report` with explicit dependencies.
- [ ] Use SHA-256 as replay identity. Create a new `sources` row for each discovered path/filename even when `replays` deduplicates content.
- [ ] Parse filename provenance with the foundation module. Never derive a canonical player or alias from match ID or source token.
- [ ] Implement database-backed jobs with pending/running/succeeded/failed states, attempt count, lease owner/expiry, error code/message, and retryability. Stage execution must be idempotent by replay and component version.
- [ ] Add CLI `replay-analyzer import <file-or-folder> [--recursive] [--reference-only] [--json]` and `replay-analyzer jobs retry <job-id>`.
- [ ] Run tests and commit with `feat(replay): Add resumable replay import`.

## Task 4: Import parser and telemetry observations transactionally

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/importing/parser_import.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/importing/telemetry_import.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/importing/map_import.py`
- Create: `scripts/replay_analyzer/tests/importing/test_parser_import.py`
- Create: `scripts/replay_analyzer/tests/importing/test_telemetry_import.py`

- [ ] Write tests that import the pinned parser result and a valid telemetry trace, then assert row counts, evidence tiers, original names, command offsets, event sequence identity, entity foreign keys, and map asset hashes.
- [ ] Add rollback tests for invalid event sequence, missing entity reference, asset hash mismatch, unsupported schema major, and incomplete trace. The database must retain the failed run metadata but no partial observed event rows.
- [ ] Import parser observations with parser version and replay hash. Store commands and typed argument payloads losslessly.
- [ ] Import telemetry events by event family into normalized tables while retaining the canonical raw event JSON. Never rewrite an existing successful run with the same run ID.
- [ ] Import map resources/regions and sidecar metadata; keep dense grids in the content-addressed asset directory.
- [ ] Compute a replay quality state from explicit parser/telemetry facts: complete, truncated, CRC mismatch, version mismatch, missing telemetry, or exporter failure. Do not collapse distinct failures into one boolean.
- [ ] Commit with `feat(replay): Import authoritative replay evidence`.

## Task 5: Implement safe player identity and reversible alias operations

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/identity/normalize.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/identity/service.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/identity/audit.py`
- Create: `scripts/replay_analyzer/tests/identity/test_normalize.py`
- Create: `scripts/replay_analyzer/tests/identity/test_service.py`
- Modify: `scripts/replay_analyzer/db/models.py`
- Create: `scripts/replay_analyzer/alembic/versions/0002_player_identity_audit.py`

- [ ] Write property tests using Hypothesis for Unicode NFC normalization, trimming, and case-folding. Distinct normalized strings must never auto-merge.
- [ ] On replay import, auto-link exact normalized embedded names and preserve each observed spelling/casing in `replay_players` and `player_aliases`.
- [ ] Implement manual `merge_players(source_ids, target_id, actor, reason)` and `split_alias(alias_id, new_player_id, actor, reason)` in transactions. Record before/after JSON audit records and expose an inverse operation for both.
- [ ] Prohibit aliases whose provider is `strata_filename` from satisfying embedded-name identity unless a user explicitly promotes the provider link.
- [ ] Add a regression test: the pinned replay creates aliases for normalized `leex279` and `fox27`, and creates no player for `3133811` or `e80b96708aa4254945941fd5f81489bb`.
- [ ] Commit with `feat(replay): Add reversible player identity`.

## Task 6: Build versioned deterministic feature extraction

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/features/base.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/features/evidence.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/features/build_order.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/features/economy.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/features/production.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/features/combat.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/features/activity.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/features/service.py`
- Create: `scripts/replay_analyzer/tests/features/`

- [ ] Define `FeatureExtractor` with stable `name`, semantic `version`, required observation families, and pure `extract(context) -> FeatureBundle`. Hash canonical inputs and extractor version into `feature_sets.cache_key`.
- [ ] Define each `FeatureValue` with name, typed value, unit, replay/player/team/entity scope, inclusive frame window, evidence references, quality, and optional explanation. Validate at least one evidence reference for every non-null derived value.
- [ ] Write fixture-based tests before each extractor for build milestones/order, spend/income/float/worker efficiency, composition/transitions, army/loss/trade values, upgrades, effective actions, command distribution, engagements, response/retreat, and final result.
- [ ] Use game-data catalog costs/build times and telemetry outcomes. Do not use hard-coded numeric entity IDs or infer production completion from a queue command.
- [ ] Define effective actions by versioned command categories and deduplication windows; call the metric `effective_actions_per_minute`, never `skill`.
- [ ] Re-running the same version/input must reuse the feature set; a version change creates a new set without deleting the old one.
- [ ] Commit feature families in focused commits ending with `feat(replay): Add deterministic replay features`.

## Task 7: Derive spatial transforms, paths, control, and engagements

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/spatial/assets.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/spatial/coordinates.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/spatial/navigation.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/spatial/engagements.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/spatial/features.py`
- Create: `scripts/replay_analyzer/tests/spatial/`
- Modify: `scripts/replay_analyzer/pyproject.toml`

- [ ] Add `numpy>=2.2,<3`, `scipy>=1.15,<2`, and `networkx>=3.4,<4`.
- [ ] Write tests for raw-to-normalized round trips, all start-position rotations/mirrors, degenerate bounds, unreachable paths, multiple locomotor surfaces, sample gaps, and player-centric invariance on mirrored synthetic grids. Synthetic grids are test input only and never user evidence.
- [ ] Preserve raw coordinates. Derive map-normalized `[0,1]` coordinates and a versioned affine player-centric transform using observed start/enemy direction. Mark transform unavailable when starts cannot be resolved.
- [ ] Load and hash-validate map sidecars. Build navigation graphs from real path cells/zones; cache graphs by map hash, locomotor class, and algorithm version.
- [ ] Cluster combat observations into engagements by temporal gap, reachable spatial radius, and participant continuity. Store every member event as evidence.
- [ ] Derive reachable travel distance, resource routing efficiency, forward placement, army concentration/separation, harassment/flanks, chokepoint/resource control, map presence/control windows, engagement location/intensity/outcome, and retreat/reinforcement paths.
- [ ] If map or samples are absent/invalid, emit unavailable quality reasons and no guessed values.
- [ ] Commit with `feat(replay): Add evidence-backed spatial features`.

## Task 8: Implement versioned strategy taxonomy and deterministic candidates

**Files:**
- Create: `scripts/replay_analyzer/data/strategy-taxonomy-v1.yaml`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strategy/taxonomy.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strategy/rules.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strategy/candidates.py`
- Create: `scripts/replay_analyzer/tests/strategy/test_taxonomy.py`
- Create: `scripts/replay_analyzer/tests/strategy/test_candidates.py`
- Modify: `scripts/replay_analyzer/pyproject.toml`

- [ ] Add `pyyaml>=6.0,<7`.
- [ ] Define each strategy with stable ID, display name, synonyms, faction/subfaction, matchup applicability, phase, required feature predicates, supporting predicates, contradictions, minimum evidence quality, and rule version.
- [ ] Seed only strategies supported by explicit catalog/feature names and tests. Include conservative `unknown_or_mixed` fallback; do not force every phase into a named strategy.
- [ ] Validate unique IDs, known feature names/operators/units, non-conflicting required predicates, and explicit faction applicability.
- [ ] Produce deterministic `strategy_assessments` with method `rule`, confidence components, supporting/contradicting evidence, and taxonomy version. Confidence is a transparent rule score, not a probability claim.
- [ ] Add regression fixtures for fast production pressure, economic expansion, defensive posture, tech transition, mixed/contradictory evidence, and insufficient data.
- [ ] Commit with `feat(replay): Add strategy candidate detection`.

## Task 9: Add longitudinal player baselines and pattern comparison

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/longitudinal/segments.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/longitudinal/statistics.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/longitudinal/patterns.py`
- Create: `scripts/replay_analyzer/tests/longitudinal/`

- [ ] Write tests using a generated database corpus for faction, opponent faction/identity, map, start, patch, and time-period segmentation; exact sample counts; median/quantiles; personal baseline deviation; and minimum-sample suppression.
- [ ] Define a segment key with nullable filters and replay quality floor. Default comparisons exclude failed/truncated/CRC-mismatch replays unless explicitly requested.
- [ ] Report count, missing count, median, interquartile range, 10th/90th percentiles, and bootstrap confidence interval for continuous features; report count/share with Wilson interval for categorical patterns.
- [ ] Detect recurring openings, timing bands, transitions, map-position habits, opponent-specific adaptations, and trend changes only when the configured minimum sample size is met.
- [ ] Link each aggregate back to the feature set IDs and replay/player rows used. A manual alias change invalidates affected cached longitudinal results.
- [ ] Commit with `feat(replay): Add longitudinal player patterns`.

## Task 10: Add schema-constrained Ollama interpretation and cache

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/llm/provider.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/llm/ollama.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/llm/evidence_bundle.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/llm/schema.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/llm/service.py`
- Create: `scripts/replay_analyzer/prompts/strategy-report-v1.txt`
- Create: `scripts/replay_analyzer/tests/llm/`
- Modify: `scripts/replay_analyzer/pyproject.toml`

- [ ] Add `httpx>=0.28,<1` and `tenacity>=9,<10`.
- [ ] Define provider interface `generate_structured(request, schema) -> ProviderResult`; implement Ollama `/api/chat` using configured `http://127.0.0.1:11434` and default `qwen3.6:27b`.
- [ ] Build compact evidence bundles containing only replay metadata, quality, deterministic features, strategy candidates, longitudinal aggregates, and stable evidence IDs. Include explicit unknown/missing sections.
- [ ] Define output schema: summary, phase assessments, strategy assessments, comparative observations, strengths, vulnerabilities, uncertainty notes, and cited evidence IDs. Every claim requires at least one supplied evidence ID and confidence in `[0,1]`.
- [ ] Validate JSON, reject unknown evidence IDs, reject observed/derived value mutation, and retry once with validation errors. After a second failure, store failed analysis status and leave deterministic reports available.
- [ ] Cache by SHA-256 of provider, exact model name/digest, prompt version/text, response schema, temperature/options, and canonical evidence bundle. Store raw response separately from validated report.
- [ ] Mock tests cover success, malformed JSON, schema error, hallucinated evidence ID, timeout, model unavailable, retry, and cache hit. An optional `ollama` marker tests the live local endpoint without making CI depend on it.
- [ ] Commit with `feat(replay): Add local Ollama interpretation`.

## Task 11: Assemble versioned evidence-backed reports

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/report/model.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/report/service.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/report/render_json.py`
- Create: `scripts/replay_analyzer/tests/report/test_report.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/cli.py`

- [ ] Define a report document with replay/player/map identity, quality, observed timeline, derived metrics, strategy candidates, spatial analysis, longitudinal context, optional LLM interpretation, component versions, evidence index, and warnings.
- [ ] Require every displayed claim to carry evidence tier. Inferred sections include model/digest, prompt version, confidence, and evidence IDs.
- [ ] Make report construction deterministic: stable ordering, canonical JSON, fixed numeric rounding for display only, raw values retained in evidence.
- [ ] Add CLI `replay-analyzer analyze <replay-or-id> [--skip-telemetry] [--skip-llm] [--json]` and `replay-analyzer compare <player-id> [filters]`.
- [ ] Add golden report tests for the pinned fixture. Golden files may assert structure and evidence integrity but must not freeze an Ollama narrative.
- [ ] Commit with `feat(replay): Assemble versioned replay reports`.

## Analytics Acceptance Gate

- [ ] Alembic upgrade/downgrade, SQLite integrity, constraints, and transactional rollback tests pass.
- [ ] The pinned replay imports once by hash with multiple source provenance rows allowed.
- [ ] Embedded players are `leex279` and `FOX27`; no Strata filename identifier becomes a player.
- [ ] Every derived feature and strategy candidate has version, window/scope, quality, and valid evidence references.
- [ ] Spatial features use only validated map assets and telemetry samples; unavailable evidence produces explicit absence, never procedural fallback.
- [ ] Longitudinal output shows sample sizes/distributions and enforces minimum samples.
- [ ] The full deterministic pipeline passes with Ollama stopped.
- [ ] Live Ollama `qwen3.6:27b` produces schema-valid cited output, or records a clean model-unavailable status without failing deterministic analysis.
- [ ] `uv run --project scripts/replay_analyzer pytest -m "not engine and not ollama" --cov=generals_replay_analyzer --cov-fail-under=90 -q`, Ruff, and strict mypy pass.
- [ ] Push completed commits to `origin/feat/replay-analyzer-v2` before starting the web plan.
