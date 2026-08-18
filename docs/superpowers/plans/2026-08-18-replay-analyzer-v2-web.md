# Replay Analyzer V2 Web Application Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a polished localhost web application for importing, analyzing, comparing, and inspecting replay evidence while keeping the CLI and deterministic pipeline fully usable without the browser or Ollama.

**Architecture:** A FastAPI application factory owns request-scoped SQLAlchemy sessions and delegates all domain work to existing services. A separate database-backed worker executes resumable jobs. Server-rendered Jinja pages and HTMX partials handle interaction; locally vendored ECharts renders timelines, maps, distributions, and comparisons from typed JSON endpoints. Every conclusion opens an evidence drawer showing its tier and source records.

**Tech Stack:** FastAPI, Uvicorn, Pydantic 2, SQLAlchemy 2, Jinja2, HTMX, locally vendored ECharts, vanilla CSS/TypeScript-free JavaScript, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-08-18-replay-analyzer-v2-design.md` sections 10, 11, 14 through 18.

## Global Constraints

- Depend on all prior acceptance gates.
- Bind to `127.0.0.1` by default. Refuse a non-loopback bind unless the user passes `--allow-remote` and a generated secret is configured.
- Keep route handlers thin. They validate input, call services, and render/serialize results; no analysis formulas live in web code.
- Vendor HTMX and ECharts locally with license and SHA-256 records. The application must render with internet access disabled.
- Use semantic HTML, keyboard navigation, visible focus, reduced-motion support, and WCAG AA color contrast.
- Display evidence tier, quality, version, confidence where applicable, and evidence links for every analytical claim.
- Never show incomplete job output as a finished report.
- Do not expose arbitrary filesystem browsing or accept a path outside configured import roots through HTTP.

---

## Task 1: Scaffold the FastAPI application and localhost server

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/app.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/dependencies.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/errors.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/routes/health.py`
- Create: `scripts/replay_analyzer/tests/web/test_app.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/cli.py`
- Modify: `scripts/replay_analyzer/pyproject.toml`

- [ ] Add `fastapi>=0.116,<1`, `uvicorn[standard]>=0.35,<1`, `jinja2>=3.1,<4`, `python-multipart>=0.0.20,<1`, and test dependency `pytest-playwright`.
- [ ] Write failing tests for `create_app(settings)`, `/health/live`, `/health/ready`, database-unavailable readiness, exception-to-problem response mapping, and one SQLAlchemy session per request.
- [ ] Implement app lifespan that validates data directories, migrates the database to head, verifies SQLite integrity, and initializes services. It must not start an in-process analyzer worker.
- [ ] Add `replay-analyzer web --host 127.0.0.1 --port 8765`. Reject a non-loopback host without `--allow-remote`; log data root/database/model/engine availability without secrets.
- [ ] Run tests and commit with `feat(replay): Add local web application`.

## Task 2: Build the shell, navigation, design tokens, and local assets

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/base.html`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/components/`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/static/css/app.css`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/static/js/app.js`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/static/vendor/htmx.min.js`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/static/vendor/echarts.min.js`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/static/vendor/THIRD_PARTY_LICENSES.txt`
- Create: `scripts/replay_analyzer/tests/web/test_assets.py`

- [ ] Pin exact HTMX and ECharts release URLs and SHA-256 values in `THIRD_PARTY_LICENSES.txt`; download once during implementation and commit the minified assets. Do not use CDN links.
- [ ] Define CSS tokens for neutral surfaces, player colors, observed/derived/inferred tiers, success/warning/error, typography, spacing, borders, and focus. Support light/dark system preference and `prefers-reduced-motion`.
- [ ] Build responsive navigation for Library, Players, Compare, Maps, Jobs, and Settings. Add a global pipeline-status indicator and command palette limited to navigation/import actions.
- [ ] Add reusable components: evidence badge, quality badge, confidence meter, empty state, error panel, pagination, filter chips, job progress, metric display, and evidence drawer.
- [ ] Tests assert all static references are local, CSP blocks remote scripts, required landmarks exist, and evidence tier is not communicated by color alone.
- [ ] Commit with `feat(replay): Add analyzer web design system`.

## Task 3: Implement replay import, library, and watched folders

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/routes/replays.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/routes/imports.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/viewmodels/replays.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/replays/index.html`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/replays/_table.html`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/imports/dialog.html`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/watching/service.py`
- Create: `scripts/replay_analyzer/tests/web/test_replays.py`
- Create: `scripts/replay_analyzer/tests/watching/test_watch_service.py`

- [ ] Write tests for file upload, configured-folder import, duplicate replay, invalid extension, oversized upload, outside-root path refusal, watcher stabilization, rename/duplicate events, and job creation.
- [ ] Support drag/drop `.rep` upload into the managed library and server-side selection only from configured watched/import roots. Stream uploads to a temporary file with a configurable 64 MiB default limit; hash before moving.
- [ ] Build the replay library with search, quality, player, faction, map, date, patch, analysis status, and source filters. Paginate in SQL and preserve filters in URLs.
- [ ] Watch configured folders by polling metadata and importing only after size and modified time remain stable across two scans. Store discovery errors as jobs; never delete source files.
- [ ] Show Strata match ID/source token only in a provenance panel, never in the player column.
- [ ] Commit with `feat(replay): Add replay library and import UI`.

## Task 4: Implement the external job worker and job operations UI

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/worker.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/routes/jobs.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/jobs/index.html`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/jobs/_rows.html`
- Create: `scripts/replay_analyzer/tests/test_worker.py`
- Create: `scripts/replay_analyzer/tests/web/test_jobs.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/cli.py`

- [ ] Add `replay-analyzer worker --poll-seconds 1 --lease-seconds 120`. Claim jobs atomically, heartbeat leases during engine/Ollama stages, and release or fail cleanly on shutdown.
- [ ] Write concurrency tests with two workers proving one execution per job lease, recovery after expired lease, retry limits, and idempotent stage reuse.
- [ ] Build job list/detail views with stage, status, progress, attempts, created/started/finished time, concise error, log paths, retry, and cancel-before-start. HTMX polls only while jobs are pending/running.
- [ ] Cancellation marks pending jobs cancelled. For running engine/model processes, set cancel-requested and let the worker terminate only its owned child process before marking cancelled.
- [ ] Commit with `feat(replay): Add analysis job worker`.

## Task 5: Build the replay report and evidence inspector

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/routes/reports.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/routes/evidence.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/viewmodels/report.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/replays/detail.html`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/evidence/detail.html`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/static/js/report.js`
- Create: `scripts/replay_analyzer/tests/web/test_report.py`

- [ ] Test complete, deterministic-only, telemetry-missing, Ollama-failed, truncated, and CRC-mismatch reports. Require quality warnings to remain visible above analysis.
- [ ] Render overview, players/results, opening/build order, economy, production/composition, combat/engagements, activity, strategy phases, spatial analysis, longitudinal context, and optional LLM interpretation.
- [ ] Add timeline charts with shared frame/second axis and toggles for player/event families. Chart JSON endpoints return typed data only for the requested replay and report version.
- [ ] Every metric/assessment links to `/evidence/<tier>/<id>`. The evidence drawer shows raw observed event/command, derived formula/version/inputs, or inferred model/prompt/confidence/citations as appropriate.
- [ ] Clearly label unavailable panels and reasons. Do not replace missing telemetry/map/model analysis with placeholder numeric values or generic narrative.
- [ ] Commit with `feat(replay): Add evidence-backed replay report UI`.

## Task 6: Build real map, paths, control, and engagement visualization

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/routes/maps.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/viewmodels/map.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/maps/detail.html`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/static/js/map.js`
- Create: `scripts/replay_analyzer/tests/web/test_map.py`

- [ ] Write endpoint tests for bounds, normalized/raw transform metadata, resources, start positions, paths, samples, engagement overlays, control windows, locomotor class, downsampling, and unavailable map data.
- [ ] Render the actual map bounds and terrain/pathability raster derived from sidecars. Overlay resources, starts, structures, player-colored trajectories, orders, engagements, casualties, and control regions.
- [ ] Add time-window slider and player/entity/event filters. Downsample server-side with a deterministic algorithm that preserves first/last/event-forced samples; expose original and returned sample counts.
- [ ] Offer raw, map-normalized, and player-centric coordinate display. Clearly show transform version and disable player-centric mode when unavailable.
- [ ] Do not draw procedural terrain, randomized resources, straight-line paths in place of missing navigation, or inferred unit positions.
- [ ] Commit with `feat(replay): Visualize authoritative spatial analysis`.

## Task 7: Build player history, aliases, and comparison views

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/routes/players.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/routes/comparisons.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/viewmodels/players.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/players/index.html`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/players/detail.html`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/players/identity.html`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/compare/index.html`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/static/js/compare.js`
- Create: `scripts/replay_analyzer/tests/web/test_players.py`

- [ ] Test exact-name identity, manual merge, manual split, undo, audit visibility, CSRF, and the pinned replay external-ID regression.
- [ ] Build player detail with aliases, faction/map/matchup filters, replay history, recurring openings, timing distributions, transitions, spatial habits, personal-baseline deviations, opponent adaptations, sample sizes, intervals, and quality exclusions.
- [ ] Build comparison for two players or one player versus a segment baseline. Use aligned feature definitions/versions and display `not comparable` when versions, units, factions, or quality filters conflict.
- [ ] Identity changes require a confirmation form, human-entered reason, CSRF token, and display of affected replay/feature/report counts. Queue invalidated derived stages after commit.
- [ ] Show provider identities and Strata provenance in separate sections with explicit labels.
- [ ] Commit with `feat(replay): Add player pattern comparison UI`.

## Task 8: Add settings and component diagnostics

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/routes/settings.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/settings/index.html`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/diagnostics.py`
- Create: `scripts/replay_analyzer/tests/web/test_settings.py`

- [ ] Display data root/database, engine executable/build, parser/exporter/catalog versions, map cache, Ollama URL/model/digest/availability, watched folders, movement interval, import mode, and minimum sample size.
- [ ] Add explicit test actions for engine launch/version, writable data root, SQLite integrity, Ollama model availability, and one schema-constrained minimal generation. Never test by importing a replay without confirmation.
- [ ] Permit editing only safe settings. Validate paths and URLs server-side, redact secrets, and write configuration atomically outside the repository.
- [ ] Settings that invalidate analysis must display impact and queue no work until the user confirms.
- [ ] Commit with `feat(replay): Add analyzer settings and diagnostics`.

## Task 9: Verify accessibility, responsive behavior, and offline operation

**Files:**
- Create: `scripts/replay_analyzer/tests/browser/test_accessibility.py`
- Create: `scripts/replay_analyzer/tests/browser/test_user_flows.py`
- Create: `scripts/replay_analyzer/tests/browser/test_offline.py`
- Create: `docs/replay-analyzer/web-qa.md`

- [ ] Start the web app and worker against a temporary copy of the pinned fixture database. Use Playwright at 1440x900, 1024x768, and 390x844.
- [ ] Test keyboard-only navigation, focus order, dialog focus trapping/return, evidence drawer, filters, import, job progress, replay report, map time control, player comparison, and alias confirmation.
- [ ] Run automated accessibility checks plus manual assertions for headings, labels, table captions, chart summaries, contrast, reduced motion, and non-color evidence tiers.
- [ ] Block all non-local network requests in the browser. Confirm library, report, map, and comparison pages remain functional; only the optional Ollama request may use configured localhost.
- [ ] Capture named screenshots for each primary page and record review results in `web-qa.md`. Screenshots are test artifacts outside Git.
- [ ] Commit with `test(replay): Verify analyzer web experience`.

## Task 10: Complete end-to-end operations and documentation

**Files:**
- Modify: `scripts/replay_analyzer/README.md`
- Create: `docs/replay-analyzer/user-guide.md`
- Create: `docs/replay-analyzer/evidence-model.md`
- Create: `docs/replay-analyzer/troubleshooting.md`
- Create: `scripts/replay_analyzer/tests/engine/test_end_to_end.py`

- [ ] Add a clean-data-root end-to-end test: import pinned replay, deduplicate second source, parse, export telemetry/map, import observations, derive features, run strategy rules, optionally call Ollama, assemble report, and fetch every primary web page/evidence link.
- [ ] Document installation with uv, modern engine build, data-root behavior, CLI commands, server/worker startup, Ollama configuration, import/watch folders, identity merge/split, evidence tiers, re-analysis/versioning, backup, and uninstall that preserves user choice over data deletion.
- [ ] Troubleshooting includes missing Python, engine executable, replay version mismatch, CRC mismatch, partial trace, locked SQLite, unavailable Ollama, invalid model output, and map/spatial absence.
- [ ] Document limitations plainly: Zero Hour 1.04 first, no Strata scraping, no synthetic reconstruction, map/spatial dependence on engine export, and LLM inference not factual observation.
- [ ] Run the full release gate from the implementation index.
- [ ] Commit with `docs(replay): Add replay analyzer operations guide`.

## Web Application Acceptance Gate

- [ ] FastAPI route, worker, browser, accessibility, offline, and end-to-end tests pass.
- [ ] All JS/CSS/vendor assets load locally with external browser network blocked.
- [ ] Importing the pinned filename displays `leex279` and `FOX27` as players and `3133811`/the user token only as provenance.
- [ ] Reports, maps, comparisons, and evidence drawers distinguish observed, derived, and inferred content.
- [ ] Missing telemetry, map data, sufficient history, or Ollama results produce explicit unavailable states without invented substitutes.
- [ ] Manual identity merge/split is confirmed, audited, reversible, and invalidates affected analysis.
- [ ] The CLI, deterministic pipeline, and web reports work while Ollama is stopped.
- [ ] The full repository modern/VC6 and replay non-interference release gate passes.
- [ ] Push all commits to `origin/feat/replay-analyzer-v2` and open a draft PR from the fork for review; do not merge without replay compatibility evidence.
