# Replay Analyzer Strategy-First Product Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Turn the evidence platform into a useful Zero Hour replay-analysis product that identifies supportable strategies, explains the observed opening, and gives evidence-backed review guidance before exposing diagnostics.

**Architecture:** Enrich deterministic strategy contexts from persisted replay, player, map, catalog, feature, and evidence rows without changing parser or telemetry truth. Project immutable report DTOs through a display-only vocabulary and coaching view model; templates consume that projection and retain evidence as progressive disclosure. Apply the supplied redesign ZIP only as a visual-system reference and never copy its placeholder facts.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Pydantic 2, FastAPI, Jinja2, SQLite, pytest, Playwright, Axe, vanilla JavaScript, packaged CSS, uv, Ruff, strict mypy.

**Spec:** docs/superpowers/specs/2026-08-23-replay-analyzer-product-completion-design.md

## Global Constraints

- Zero Hour is primary; no game feature or balance work.
- GameLogic stays deterministic; no web, coaching, filesystem, random, clock, or Ollama state enters simulation.
- Every conclusion cites immutable evidence. Never invent a winner, later phase, route, unit, strategy, or metric.
- The pinned CRC-mismatch replay is opening-only through its exact observed horizon.
- Ollama is optional and cannot replace deterministic coaching.
- The redesign ZIP controls only palette, typography, spacing, panel shape, navigation treatment, and layout rhythm.
- All browser assets remain local.
- Never stage the five protected engine/audio files or protected untracked watcher test.
- Use TDD, then commit and push every independently reviewable task.
- New architectural or user-facing production code uses the repository TheSuperHackers comment convention.

## File structure

- strategy/service.py: assemble provenance-backed strategy context.
- strategy/rules.py: exact recursive membership for canonical JSON features.
- data/strategy-taxonomy-v1.json: deterministic Zero Hour strategy families.
- presentation/vocabulary.py: display-only names for factions, templates, features, phases, and reasons.
- web/viewmodels/coaching.py: immutable summary, strategy, build-order, highlight, prompt, and limitation projection.
- web/viewmodels/report.py: bind a fixed report and timeline to coaching.
- web/templates/replays/detail.html: player-first analysis and collapsed technical evidence.
- web templates for dashboard, players, maps, comparisons, imports: complete the surrounding user journeys.
- web/static/css/app.css and map.css: approved tactical visual system and responsive fixes.
- tests/strategy, tests/presentation, tests/web, tests/browser: deterministic, semantic, accessibility, security, packaging, and visual gates.

---

### Task 1: Make named strategy rules executable from persisted evidence

**Files:**
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/strategy/rules.py
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/strategy/service.py
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/analysis_pipeline/composition.py
- Test: scripts/replay_analyzer/tests/strategy/test_rules.py
- Test: scripts/replay_analyzer/tests/strategy/test_service.py
- Test: scripts/replay_analyzer/tests/analysis_pipeline/test_composition.py

**Interfaces:**
- Consumes: ReplayPlayer.faction, Replay.map_id, latest successful TelemetryRun, managed catalog bytes, observed telemetry evidence, and StrategyFeature rows.
- Produces: StrategyAssessmentService(session_factory, *, data_root: Path | None = None, taxonomy_resource: Traversable | None = None).
- Produces: _canonical_contains(raw: object, expected: str) -> bool for exact canonical JSON keys/leaves.

- [ ] **Step 1: Write failing recursive-membership tests**

    @pytest.mark.parametrize(
        ("raw", "expected", "matched"),
        [
            ({"AmericaVehicleHumvee": 3}, "AmericaVehicleHumvee", True),
            ([{"template_name": "AmericaStrategyCenter", "frame": 900}], "AmericaStrategyCenter", True),
            ({"AmericaVehicleHumveeFake": 1}, "AmericaVehicleHumvee", False),
        ],
    )
    def test_contains_predicate_matches_exact_canonical_json_members(raw, expected, matched):
        predicate = replace(_predicate(), operator="contains", expected_value=expected)
        assert _predicate_value(predicate, raw) is matched

- [ ] **Step 2: Run the rule tests**

Run: uv run --project . pytest tests/strategy/test_rules.py -q

Expected: nested sequence membership fails under the current direct-in behavior.

- [ ] **Step 3: Implement exact recursive membership**

    def _canonical_contains(raw: object, expected: str) -> bool:
        if type(raw) is str:
            return raw == expected
        if isinstance(raw, Mapping):
            return expected in raw or any(_canonical_contains(value, expected) for value in raw.values())
        if isinstance(raw, (list, tuple)):
            return any(_canonical_contains(value, expected) for value in raw)
        return False

Use it for contains and not_contains; never use substring matching.

- [ ] **Step 4: Write failing persisted-context tests**

Create two replay players (FactionAmerica and FactionChina), one persisted map, one successful telemetry run, a managed catalog containing those factions and template categories, and catalog observed evidence. Assert a named assessment is applicable and cites faction, map, and catalog evidence. Add missing/hash-mismatched catalog and ambiguous-opponent cases that remain unavailable.

- [ ] **Step 5: Run service tests**

Run: uv run --project . pytest tests/strategy/test_service.py -q

Expected: named assessment remains unavailable because _build_context currently writes None for faction, opponent, map, and catalog.

- [ ] **Step 6: Implement fail-closed catalog/context loading**

Resolve ManagedAsset.relative_path beneath data_root; reject traversal, reparse, size, SHA-256, and schema mismatches. Parse via the existing telemetry catalog model and create sorted CatalogProof tuples. Select an opponent only when exactly one other occupied player exists. Bind faction, opponent, map, and catalog to observed evidence references.

- [ ] **Step 7: Wire production composition**

    strategies = StrategyAssessmentService(session_factory, data_root=settings.data_root)

The data_root=None unit-fixture path must fail closed for named rules.

- [ ] **Step 8: Run focused tests and static gates**

Run: uv run --project . pytest tests/strategy tests/analysis_pipeline/test_composition.py -q

Run: uv run --project . ruff check src/generals_replay_analyzer/strategy src/generals_replay_analyzer/analysis_pipeline/composition.py tests/strategy tests/analysis_pipeline/test_composition.py

Run: uv run --project . mypy --strict src/generals_replay_analyzer/strategy src/generals_replay_analyzer/analysis_pipeline/composition.py

- [ ] **Step 9: Commit and push**

    git add scripts/replay_analyzer/src/generals_replay_analyzer/strategy/rules.py scripts/replay_analyzer/src/generals_replay_analyzer/strategy/service.py scripts/replay_analyzer/src/generals_replay_analyzer/analysis_pipeline/composition.py scripts/replay_analyzer/tests/strategy/test_rules.py scripts/replay_analyzer/tests/strategy/test_service.py scripts/replay_analyzer/tests/analysis_pipeline/test_composition.py
    git commit -m "feat(analyzer): Populate deterministic strategy context"
    git push

### Task 2: Ship a real Zero Hour strategy library and human vocabulary

**Files:**
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/data/strategy-taxonomy-v1.json
- Create: scripts/replay_analyzer/src/generals_replay_analyzer/presentation/__init__.py
- Create: scripts/replay_analyzer/src/generals_replay_analyzer/presentation/vocabulary.py
- Test: scripts/replay_analyzer/tests/strategy/test_taxonomy.py
- Create: scripts/replay_analyzer/tests/presentation/__init__.py
- Create: scripts/replay_analyzer/tests/presentation/test_vocabulary.py
- Modify: scripts/replay_analyzer/tests/test_wheel.py

**Interfaces:**
- Consumes: registered build, production, economy, combat, activity, and spatial features.
- Produces: taxonomy version strategy-taxonomy-v1.1.0 and one final fallback.
- Produces: game_label(identifier), feature_label(identifier), phase_label(identifier), reason_label(identifier), and format_frame(frame, fps=30).

- [ ] **Step 1: Replace the fallback-only taxonomy expectation**

Assert sorted IDs for universal oil/economy/defense/aggression, USA Humvee/Strategy Center/Airfield/Combat Chinook/firebase, China War Factory/Propaganda/Helix/infantry/defense, GLA Tunnel/Technical/Terror Tech/Arms Dealer/Palace/defense, and unknown_or_mixed. Every named definition has exact faction applicability, phase, nonempty required predicates, and semantic rule version.

- [ ] **Step 2: Run taxonomy tests**

Run: uv run --project . pytest tests/strategy/test_taxonomy.py -q

Expected: library assertion fails because only unknown_or_mixed is packaged.

- [ ] **Step 3: Add deterministic family rules**

Required evidence is exact membership in build.completed_sequence or production.completed_composition. Count, economy, combat, activity, and spatial values are supporting/contradicting only. Primary proofs:

| Strategy family | Required exact proof |
|---|---|
| USA Humvee pressure | AmericaVehicleHumvee |
| USA fast Strategy Center | AmericaStrategyCenter |
| USA dual Airfield | exact Airfield template plus build-count threshold |
| USA Combat Chinook | AirF_AmericaVehicleCombatChinook |
| USA firebase expansion | AmericaBuildingFirebase |
| China dual War Factory | exact China War Factory plus build-count threshold |
| China fast Propaganda | exact Propaganda Center |
| China Helix pressure | exact Helix |
| China infantry pressure | exact infantry templates plus production count |
| GLA forward Tunnel | exact Tunnel Network |
| GLA Technical aggression | exact Technical |
| GLA Terror Tech | exact Technical and Terrorist |
| GLA dual Arms Dealer | exact Arms Dealer plus build-count threshold |
| GLA fast Palace | exact Palace |
| Universal families | only registered oil/economy/defense/aggression evidence; otherwise unavailable |

Canonicalize JSON with canonical_json and retain one fallback.

- [ ] **Step 4: Write failing vocabulary tests**

    def test_known_identifiers_use_player_language():
        assert game_label("FactionAmericaAirForceGeneral") == "USA Air Force General"
        assert game_label("AmericaVehicleHumvee") == "Humvee"
        assert game_label("GLAArmsDealer") == "Arms Dealer"
        assert feature_label("economy.supply_collection_rate") == "Supply income"
        assert reason_label("minimum_sample_not_met") == "More analyzed matches are needed"

    def test_unknown_identifier_is_explicit():
        assert game_label("Modded_SuperUnitX") == "Modded Super Unit X (unrecognized)"

- [ ] **Step 5: Implement immutable display vocabulary**

Use MappingProxyType. Known identities map explicitly. Unknowns use a bounded camel/snake splitter and append (unrecognized). format_frame(105) returns 0:03.5 (frame 105).

- [ ] **Step 6: Verify wheel contents and quality gates**

Run: uv run --project . pytest tests/strategy/test_taxonomy.py tests/presentation tests/test_wheel.py -q

Run: uv run --project . ruff check src/generals_replay_analyzer/presentation tests/presentation tests/strategy/test_taxonomy.py

Run: uv run --project . mypy --strict src/generals_replay_analyzer/presentation

- [ ] **Step 7: Commit and push**

    git add scripts/replay_analyzer/src/generals_replay_analyzer/data/strategy-taxonomy-v1.json scripts/replay_analyzer/src/generals_replay_analyzer/presentation scripts/replay_analyzer/tests/strategy/test_taxonomy.py scripts/replay_analyzer/tests/presentation scripts/replay_analyzer/tests/test_wheel.py
    git commit -m "feat(analyzer): Add Zero Hour strategy vocabulary"
    git push

### Task 3: Build the deterministic coaching projection

**Files:**
- Create: scripts/replay_analyzer/src/generals_replay_analyzer/web/viewmodels/coaching.py
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/viewmodels/report.py
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/viewmodels/__init__.py
- Create: scripts/replay_analyzer/tests/web/test_coaching_viewmodel.py
- Modify: scripts/replay_analyzer/tests/web/test_report_adapter.py

**Interfaces:**
- Consumes: one ReplayReportDTO, its exact TimelineChartDTO, and Task 2 vocabulary.
- Produces: coaching_view(report, timeline) -> CoachingViewModel.
- Produces frozen EvidenceHorizonView, PlayerStrategyView, BuildOrderStepView, CoachingHighlightView, ReviewPromptView, and CoachingViewModel.
- Extends ReplayReportViewModel with coaching: CoachingViewModel.

- [ ] **Step 1: Write failing complete-report coaching snapshot**

Build a two-player USA Humvee fixture and assert complete status, concise what-happened summary, Humvee strategy, timed build steps, evidence on highlights, and at most five prompts.

- [ ] **Step 2: Write failing partial-trace truthfulness test**

Use a desynced frame-105 report. Assert Observed opening through 0:03.5, no winner/result language, no future phases, and one CRC limitation.

- [ ] **Step 3: Run tests**

Run: uv run --project . pytest tests/web/test_coaching_viewmodel.py -q

Expected: collection fails because coaching.py does not exist.

- [ ] **Step 4: Implement frozen deterministic projection**

Select claims by exact section and claim identity/details, never label substring. Sort strategies by player slot, phase, confidence descending, then ID. Sort build steps by frame/player/claim ID. Bound highlights/prompts to five.

- [ ] **Step 5: Add versioned conditional review prompts**

    ADVICE = MappingProxyType({
        "usa_humvee_pressure": "Review whether Humvee production had steady supply income and timely healing or evacuation.",
        "gla_forward_tunnel_pressure": "Review whether the forward Tunnel created safe reinforcement access before Workers were exposed.",
        "china_dual_war_factory_pressure": "Review whether both production structures stayed active without delaying supply collection.",
    })

Emit a prompt only when its trigger is available/partial and carries evidence. Prompts ask what to review; they do not claim unseen mistakes.

- [ ] **Step 6: Keep Ollama separate**

Expose validated local-model prose only for succeeded status. Deterministic output is byte-identical when Ollama is offline, unavailable, failed, or not requested.

- [ ] **Step 7: Bind projection after report/timeline identity validation**

Remove the generic highlight-priority behavior only after coaching preserves every evidence link.

- [ ] **Step 8: Run focused tests and static gates**

Run: uv run --project . pytest tests/web/test_coaching_viewmodel.py tests/web/test_report_adapter.py tests/web/test_report.py -q

Run: uv run --project . ruff check src/generals_replay_analyzer/web/viewmodels tests/web/test_coaching_viewmodel.py

Run: uv run --project . mypy --strict src/generals_replay_analyzer/web/viewmodels

- [ ] **Step 9: Commit and push**

    git add scripts/replay_analyzer/src/generals_replay_analyzer/web/viewmodels scripts/replay_analyzer/tests/web/test_coaching_viewmodel.py scripts/replay_analyzer/tests/web/test_report_adapter.py
    git commit -m "feat(web): Add evidence-backed coaching projection"
    git push

### Task 4: Rebuild the match report as a strategy-first experience

**Files:**
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/replays/detail.html
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/components/evidence_drawer.html
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/static/js/report.js
- Modify: scripts/replay_analyzer/tests/web/test_report.py
- Modify: scripts/replay_analyzer/tests/web/test_report_accessibility.py
- Modify: scripts/replay_analyzer/tests/web/test_report_charts.py

**Interfaces:**
- Consumes: ReplayReportViewModel.coaching.
- Produces semantic order: match status, what happened, strategies, highlights/prompts, timeline, build order, metrics, combat, map, local interpretation, limitations, collapsed technical evidence.

- [ ] **Step 1: Write failing semantic-order tests**

Assert What happened, Strategy, Build order, and Worth reviewing precede Technical evidence. UUIDs, schemas, and lifecycle details are absent before the disclosure.

- [ ] **Step 2: Write partial-report tests**

Assert frame-105 CRC report shows one horizon, omits later empty phases/result, and keeps evidence keyboard-reachable.

- [ ] **Step 3: Run tests**

Run: uv run --project . pytest tests/web/test_report.py tests/web/test_report_accessibility.py -q

Expected: current technical-first structure fails.

- [ ] **Step 4: Implement player-first top half**

Render a versus header, evidence horizon, summary, side-by-side strategy cards, three-to-five highlights, and prompts. Each evidence-backed conclusion links to evidence.

- [ ] **Step 5: Implement readable analysis sections**

Build order uses time/player/game name/evidence. Economy/activity use explained metrics. Combat and map render only when authoritative inputs exist.

- [ ] **Step 6: Collapse but preserve technical evidence**

Use a native details element named Technical evidence and provenance. Keep all claims, source mode, lifecycle, report identity, timeline data, limitations, and Ollama diagnostics inside.

- [ ] **Step 7: Preserve no-script/chart fallbacks**

The table remains complete without JavaScript; chart values equal the table.

- [ ] **Step 8: Run report suites**

Run: uv run --project . pytest tests/web/test_report.py tests/web/test_report_accessibility.py tests/web/test_report_charts.py tests/web/test_evidence.py -q

- [ ] **Step 9: Commit and push**

    git add scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/replays/detail.html scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/components/evidence_drawer.html scripts/replay_analyzer/src/generals_replay_analyzer/web/static/js/report.js scripts/replay_analyzer/tests/web/test_report.py scripts/replay_analyzer/tests/web/test_report_accessibility.py scripts/replay_analyzer/tests/web/test_report_charts.py
    git commit -m "feat(web): Lead reports with strategy coaching"
    git push

### Task 5: Apply the approved tactical visual system

**Files:**
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/base.html
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/static/css/app.css
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/static/css/map.css
- Modify: scripts/replay_analyzer/tests/web/test_command_center_design.py
- Modify: scripts/replay_analyzer/tests/web/test_assets.py
- Modify: scripts/replay_analyzer/tests/web/test_shell_accessibility.py

**Interfaces:**
- Consumes: approved reference tokens from the spec.
- Produces: local-only blue-black/cyan/lime/orange visual system, 390/1024/1440 responsiveness, 200% zoom reflow, forced-colors, and reduced-motion support.

- [ ] **Step 1: Change visual-token tests**

Assert canvas #080f15; surfaces #0d1a24, #111f2b, #1a2f41; rule #31536a; text #eaf6ff; muted #9bb4c4; cyan #7fc6f5; blue #4a9fd8; success #a9d05a; warning #f0966e; opponent #d9764f. Assert no font-face or external URL.

- [ ] **Step 2: Run design tests**

Run: uv run --project . pytest tests/web/test_command_center_design.py tests/web/test_assets.py -q

Expected: old charcoal palette fails.

- [ ] **Step 3: Implement palette, typography, and surfaces**

Use Arial Narrow, Roboto Condensed, Segoe UI, and system fallbacks; retain local mono for evidence. Add subtle CSS-only grid/radial texture, clipped controls, thin cyan rules, and high-contrast focus.

- [ ] **Step 4: Implement responsive grids**

At 1024 secondary rails move below primary. At 767 use one column and existing menu dialog. At 479 remove width-reducing decoration. Targets stay at least 44 by 44. Use minmax(0,1fr), min-width:0, overflow-wrap, labelled scroll regions, and stable map aspect ratio.

- [ ] **Step 5: Run accessibility/style tests**

Run: uv run --project . pytest tests/web/test_command_center_design.py tests/web/test_assets.py tests/web/test_shell_accessibility.py tests/web/test_report_accessibility.py tests/web/test_map_accessibility.py -q

- [ ] **Step 6: Commit and push**

    git add scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/base.html scripts/replay_analyzer/src/generals_replay_analyzer/web/static/css/app.css scripts/replay_analyzer/src/generals_replay_analyzer/web/static/css/map.css scripts/replay_analyzer/tests/web/test_command_center_design.py scripts/replay_analyzer/tests/web/test_assets.py scripts/replay_analyzer/tests/web/test_shell_accessibility.py
    git commit -m "style(web): Apply tactical replay visual system"
    git push

### Task 6: Complete dashboard, import, profile, comparison, and map journeys

**Files:**
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/ports.py
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/adapters/library.py
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/adapters/players.py
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/viewmodels/players.py
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/viewmodels/map.py
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/viewmodels/comparisons.py
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/dashboard.html
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/imports/dialog.html
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/replays/_table.html
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/players/detail.html
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/maps/detail.html
- Modify: scripts/replay_analyzer/src/generals_replay_analyzer/web/templates/compare/_result.html
- Test: scripts/replay_analyzer/tests/web/test_imports.py
- Test: scripts/replay_analyzer/tests/web/test_players.py
- Test: scripts/replay_analyzer/tests/web/test_map.py
- Test: scripts/replay_analyzer/tests/web/test_comparisons.py
- Test: scripts/replay_analyzer/tests/web/test_command_center_design.py

**Interfaces:**
- Consumes: fixed reports, strategy labels, player history, map evidence, strict comparison states.
- Produces: direct View analysis links, compact horizons/strategies, tendencies only at valid sample sizes, and one concise cohort requirement otherwise.

- [ ] **Step 1: Write failing dashboard tests**

Populated recent replay must show players/factions, map, horizon, detected strategies, and direct fixed-report link. Operations becomes a small secondary link, not a rail.

- [ ] **Step 2: Write failing import tests**

Dialog has replay-file group, optional watched-folder group, bounded help, Analyze replay primary, Cancel secondary, and no nested mobile scroll.

- [ ] **Step 3: Write failing profile tests**

One replay shows useful recent strategy/opening facts then one More analyzed matches are needed message. Repeated minimum_sample_not_met rows are absent. Identity internals live under Identity and data.

- [ ] **Step 4: Write comparison/map tests**

Noncomparable cohorts stay not_comparable, explain the required cohort once, and show no delta. Map annotations use only real routes, engagements, structures, resources, or control windows and cite evidence.

- [ ] **Step 5: Run focused tests**

Run: uv run --project . pytest tests/web/test_imports.py tests/web/test_players.py tests/web/test_map.py tests/web/test_comparisons.py tests/web/test_command_center_design.py -q

Expected: current technical layouts fail usefulness assertions.

- [ ] **Step 6: Extend bounded public DTOs**

Add optional fixed-report identity, horizon, and at most two detected strategies per player. Populate only from report/query services and reject cross-replay identity.

- [ ] **Step 7: Rebuild templates**

Dashboard uses analysis cards. Import dialog has consistent groups. Profile groups available opening/strategy/economy/activity/matchup facts and collapses identity. Comparison translates labels/reasons without changing strict state. Map callouts render only from available collections.

- [ ] **Step 8: Run full focused web/adapter gates**

Run: uv run --project . pytest tests/web/test_imports.py tests/web/test_library_adapter.py tests/web/test_players.py tests/web/test_players_adapter.py tests/web/test_map.py tests/web/test_map_adapter.py tests/web/test_comparisons.py tests/web/test_comparison_determinism.py -q

Run: uv run --project . ruff check src/generals_replay_analyzer/web tests/web

Run: uv run --project . mypy --strict src/generals_replay_analyzer/web

- [ ] **Step 9: Commit and push**

Stage only the listed Replay Analyzer files.

    git commit -m "feat(web): Complete player-first analysis journeys"
    git push

### Task 7: Prove one replay produces a useful truthful MVP

**Files:**
- Modify: scripts/replay_analyzer/tests/browser/populated_fixture.py
- Create: scripts/replay_analyzer/tests/browser/test_strategy_first_journey.py
- Modify: scripts/replay_analyzer/tests/browser/test_accessibility.py
- Modify: scripts/replay_analyzer/tests/browser/test_keyboard_workflows.py
- Modify: scripts/replay_analyzer/tests/browser/test_security.py
- Modify: scripts/replay_analyzer/tests/browser/test_installed_wheel.py
- Modify: scripts/replay_analyzer/docs/acceptance-matrix.md

**Interfaces:**
- Consumes: fresh wheel, isolated data root, pinned replay, real parser/telemetry artifacts, immutable partial-horizon contract.
- Produces: installed-wheel evidence that a user can import, open, understand, and navigate one useful analysis.

- [ ] **Step 1: Write failing installed-wheel journey**

Import pinned replay, wait for terminal state, open View analysis, assert What happened, observed horizon, at least one opening/strategy signal, build-order content, Worth reviewing, no winner, and no post-frame-105 phase.

- [ ] **Step 2: Run source journey**

Run: uv run --project . pytest tests/browser/test_strategy_first_journey.py -q

Expected: useful content contract fails before product work.

- [ ] **Step 3: Update fixture without fabricated facts**

Production path uses only parser/telemetry/report rows. Any complete synthetic example is clearly fixture-only; pinned path remains source-grounded and partial.

- [ ] **Step 4: Build/install a frozen wheel**

Run: uv build --wheel

Record SHA-256, install into a new temporary uv environment, launch only installed resources.

- [ ] **Step 5: Run browser gates**

Run: uv run --project . pytest tests/browser/test_strategy_first_journey.py tests/browser/test_accessibility.py tests/browser/test_keyboard_workflows.py tests/browser/test_security.py tests/browser/test_installed_wheel.py -q

- [ ] **Step 6: Capture actual-size screenshots**

Capture dashboard, report first viewport, technical disclosure, profile, map, comparison, import at 1440, 1024, 390, and desktop 200% zoom. Retain outside repo and record path/hash.

- [ ] **Step 7: Perform producer review**

From screenshots verify a Zero Hour player can answer what happened, what each side attempted, and what to review without opening evidence. Any overlap, truncation, false certainty, or empty technical dominance fails the gate.

- [ ] **Step 8: Commit and push**

    git add scripts/replay_analyzer/tests/browser/populated_fixture.py scripts/replay_analyzer/tests/browser/test_strategy_first_journey.py scripts/replay_analyzer/tests/browser/test_accessibility.py scripts/replay_analyzer/tests/browser/test_keyboard_workflows.py scripts/replay_analyzer/tests/browser/test_security.py scripts/replay_analyzer/tests/browser/test_installed_wheel.py scripts/replay_analyzer/docs/acceptance-matrix.md
    git commit -m "test(web): Verify strategy-first installed journey"
    git push

### Task 8: Run final deterministic and release acceptance

**Files:**
- Modify: scripts/replay_analyzer/docs/acceptance-matrix.md
- Modify: scripts/replay_analyzer/docs/release-status.md
- Modify: docs/superpowers/plans/2026-08-23-replay-analyzer-product-completion.md

**Interfaces:**
- Consumes: final frozen wheel and prior commits.
- Produces: pushed release checkpoint with pass counts, wheel hash, retained artifacts, partial-replay boundary, Ollama status, and external-toolchain limits.

- [x] **Step 1: Run the complete suite**

Run: uv run --project . pytest -q

Expected: all pass except explicit environment-dependent skips.

- [x] **Step 2: Run complete static gates**

Run: uv run --project . ruff check src tests

Run: uv run --project . mypy --strict src

- [x] **Step 3: Run grouped subsystem gates**

Run parser, telemetry, parity, SQLite, worker, report, strategy, longitudinal, spatial, CLI, security, and packaging commands already recorded in release-status.md. Preserve exact counts/output.

- [x] **Step 4: Verify non-interference and available engine builds**

Run telemetry-off/on non-interference and available Win32 Release/Debug targets. Record VC6/MinGW unverified if absent; never present static exclusion as retail replay execution.

- [x] **Step 5: Rebuild final wheel**

Build a new wheel after all changes, hash it, install fresh, rerun the entire installed-wheel browser set against that exact hash.

- [x] **Step 6: Audit repository scope**

Run git status --short, git diff --check, and git diff --name-only from the starting commit. Confirm protected files/test were never committed.

- [x] **Step 7: Record release evidence**

Write exact commands, pass counts, wheel hash, screenshot path/hash, frame-105 CRC boundary, live Ollama status, and absent toolchains. Mark only actually verified plan items complete.

- [x] **Step 8: Commit and push final checkpoint**

    git add scripts/replay_analyzer/docs/acceptance-matrix.md scripts/replay_analyzer/docs/release-status.md docs/superpowers/plans/2026-08-23-replay-analyzer-product-completion.md
    git commit -m "docs(replay): Record strategy-first release evidence"
    git push

- [ ] **Step 9: Handoff**

Report branch, final commit, wheel hash, launch command, retained screenshots, verified counts, and only remaining external limits. Lead with player-visible outcome and label the pinned replay opening-only at its CRC boundary.
