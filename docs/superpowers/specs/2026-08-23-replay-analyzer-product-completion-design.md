# Replay Analyzer V2 Product Completion Design

Date: 23 August 2026

Status: approved for implementation

## 1. Product outcome

Replay Analyzer V2 must answer a Zero Hour player's questions before it exposes implementation details:

1. What did each player try to do?
2. How did the opening and strategy develop?
3. What worked, what failed, and where did momentum change?
4. What should the player learn or improve?
5. Which observations support those conclusions?

The current parser, engine telemetry, immutable SQLite evidence graph, deterministic features, Ollama adapter, and report store remain the trusted foundation. The remaining work completes the intended product by adding real strategy definitions and projecting existing evidence into a player-facing coaching experience.

## 2. Current gap

The packaged strategy taxonomy contains only `unknown_or_mixed`; it has no actual Zero Hour strategies. The web interface consequently leads with pipeline states, schema versions, UUIDs, evidence tiers, raw feature names, and long tables of unavailable longitudinal results. This is a technically inspectable system, but not yet a useful replay-analysis product.

The pinned replay also stops at a known modern-engine CRC boundary at frame 105. The product must make the observed opening useful without implying a complete match result, later phase, or winner.

## 3. Approaches considered

### A. Deterministic strategy core with optional Ollama coaching (selected)

Versioned rules identify evidence-supported strategies and phases from normalized build, production, economy, combat, activity, and spatial features. A deterministic coaching projection explains those results in plain language. Ollama may enrich wording and interpretation only after its structured output passes the existing schema and citation checks.

This preserves reproducibility, works offline, and keeps the model from inventing match facts.

### B. Ollama-first analysis

Send the evidence dossier to Ollama and let it name strategies and lessons. This would produce fluent text quickly, but model availability, reproducibility, strategy naming, and citation fidelity would become product-critical. It conflicts with the approved evidence model and is rejected.

### C. UI-only simplification

Reorder current claims and hide technical fields. This would improve appearance but leave the strategy taxonomy empty and the analysis shallow. It is insufficient and rejected.

## 4. Deterministic strategy capability

### 4.1 Strategy taxonomy

Replace the fallback-only taxonomy with a versioned, tested initial Zero Hour library. It covers evidence-distinguishable families rather than pretending to identify variants the telemetry cannot prove.

- Universal: oil capture, economic expansion, defensive opening, fast technology, all-in aggression, mixed or unknown.
- USA families: Humvee pressure, fast Strategy Center, dual Airfield, Combat Chinook pressure, defensive firebase expansion.
- China families: dual War Factory pressure, fast Propaganda Center, Helix pressure, infantry pressure, bunker or gatling defense.
- GLA families: forward Tunnel pressure, Technical aggression, Terror Tech, dual Arms Dealer pressure, fast Palace, defensive network expansion.

Applicability uses exact engine faction and general template identities. Predicates use only registered deterministic features and explicit phase windows. A strategy is reported only when its required evidence exists and its score clears the accepted rule threshold. Similar strategies may appear as candidates with confidence; the UI must not force one label when evidence is ambiguous.

### 4.2 Human-readable game vocabulary

Add a closed presentation catalog for known faction, building, unit, upgrade, command, phase, feature, and quality-reason identifiers. The catalog changes display text only; persisted evidence identities remain unchanged. Unknown identifiers are safely title-cased and clearly labelled, never silently mapped to a different game concept.

### 4.3 Coaching projection

Build a read-only player-facing projection from immutable report data. It contains:

- Match status and evidence horizon
- A concise “what happened” summary
- One strategy card per player with phase, confidence, supporting timings, and ambiguity
- A chronological opening/build-order sequence
- Economy, production, aggression, combat, and map-control highlights
- Evidence-supported advantages and vulnerabilities
- Actionable review prompts phrased conditionally from deterministic facts
- Explicit unknowns collected in one compact limitations panel

The projection does not mutate reports or create new facts. Advice templates are versioned and keyed to accepted feature/strategy combinations. Ollama text, when present and validated, is visibly identified as local-model interpretation and cannot replace the deterministic summary.

## 5. Player-facing web experience

### 5.1 Dashboard

The first screen becomes a replay-analysis home rather than an operations console. It shows:

- Import replay as the primary action
- Recently analyzed matches with players, factions, map, evidence horizon, and detected strategies
- Direct links to “View analysis”
- Compact library-level patterns only when sample thresholds are met
- A small system-status link instead of a dominant operations rail

### 5.2 Match report

The first viewport contains:

- Match identity in human game vocabulary
- Clear partial/complete evidence notice
- “What happened” summary
- Side-by-side player strategy cards
- Three to five useful highlights or review prompts

Below it:

1. Strategy timeline: opening, early, mid, late, with unavailable future phases omitted rather than rendered as empty tables.
2. Build order and production: readable names and frame/time labels.
3. Economy and activity: a small set of explained metrics, not raw registry paths.
4. Combat and turning points: only when authoritative events exist.
5. Map and spatial evidence: routes, pressure, resources, and control when real map data exists.
6. Technical evidence: collapsed, optional, and linked from each conclusion.

### 5.3 Player profile

The profile leads with player tendencies, not identity implementation:

- Match count and evidence quality
- Recurring openings and strategy preferences
- Typical economy/activity/production signals
- Matchup and map patterns when comparable cohorts exist
- Recent analyses

If only one replay exists, show a compact “more matches needed” explanation and the useful facts from that replay. Do not display ten rows of `minimum_sample_not_met`. Identity bindings, provider identities, revision digests, and audit history move under an “Identity and data” disclosure.

### 5.4 Comparison and map

Comparison remains strict: cross-faction or version-incompatible values are not presented as comparable. The empty result explains which additional replay cohort is required. The map retains its useful spatial scene and gains plain-language annotations tied to strategy and phase when supported.

### 5.5 Visual and interaction rules

- Plain game language before engineering vocabulary
- One obvious primary action per screen
- Progressive disclosure for provenance and diagnostics
- No overlapping tables; wide data uses labelled keyboard-scroll regions
- Import dialog uses consistent spacing, groups, help text, and a clear submit action
- Responsive at 390, 1024, and 1440 CSS pixels
- Complete text alternatives for charts and maps
- No external assets or browser requests

### 5.6 Approved visual reference

Use `Command and Conquer UI Redesign.zip` only as a visual reference. It does not define or limit product features, and its placeholder statistics, routes, units, charts, and match facts are not evidence.

The accepted visual language is:

- Deep blue-black canvas `#080f15` with blue-black surface layers around `#0d1a24`, `#111f2b`, `#152838`, and `#1a2f41`
- Cyan information and primary-action accents around `#7fc6f5`, `#4a9fd8`, and `#bfe3ff`
- Lime success accent `#a9d05a`
- Orange warning and opponent accents around `#f0966e` and `#d9764f`
- Condensed tactical headings, readable sans-serif body text, and monospaced telemetry or evidence labels, with local system fallbacks so no web-font request is required
- Thin cyan-blue rules, clipped-corner controls and marks, restrained grid or radial texture, and command-center panel composition
- Dense but clearly grouped information, with the strategy summary and coaching decisions receiving the strongest visual hierarchy

The reference may influence color, typography, spacing, panel shape, navigation treatment, and layout rhythm. It must not introduce unsupported APM, win probability, spend splits, army-value estimates, routes, units, or other invented values.

## 6. Data flow and boundaries

```text
Replay bytes
  -> strict parser and real GameLogic telemetry
  -> immutable observed evidence
  -> deterministic features and strategy rules
  -> immutable report graph
  -> coaching projection and human vocabulary
  -> dashboard, report, player, comparison, and map views
  -> optional validated Ollama interpretation
```

GameLogic remains passive and deterministic. No web, display, Ollama, or coaching state enters simulation. The product layer reads public report/query contracts and does not query private ORM state from templates.

## 7. Error and partial-evidence behavior

- CRC mismatch: show the exact observed horizon and label later conclusions unavailable.
- Missing telemetry: retain parser-derived opening facts and explain which deeper analyses require engine playback.
- Missing map: omit spatial conclusions and show one concise reason.
- Insufficient player history: show the sample requirement once and keep the single-match analysis useful.
- Ambiguous strategy: show the leading evidence-supported candidates or “mixed/unclear”; never force certainty.
- Ollama unavailable or invalid: keep deterministic coaching intact and record the inferred stage as unavailable.
- Internal identifiers and diagnostics appear only in optional evidence or operations views.

## 8. Verification

### Deterministic analytics

- Taxonomy schema, sorted identity, applicability, predicate, scoring, ambiguity, and fallback tests
- Faction-specific positive and negative fixtures
- Partial-trace tests proving no post-horizon strategy or winner claim
- Stable coaching-projection snapshots from immutable report DTOs
- Offline and unavailable-Ollama equivalence for deterministic content

### Product behavior

- Installed-wheel end-to-end journey from replay import to useful match analysis
- First-viewport semantic assertions for summary, strategies, build order, and limitations
- Player profile hides repetitive unavailable rows and explains sample requirements
- Comparison never relaxes cohort rules
- Keyboard, no-script, reduced-motion, Axe, CSP, and hostile-origin gates
- Actual-size screenshots at desktop, tablet, mobile, and 200% zoom
- Manual producer review asks: can a Zero Hour player understand the opening and learn something without opening evidence details?

### Existing compatibility gates

- Parser parity and telemetry schema suites
- Telemetry-off versus telemetry-on non-interference
- Modern Win32 Release and Debug builds
- VC6/MinGW static exclusion evidence when toolchains remain unavailable
- SQLite migrations, WAL, foreign keys, immutable evidence, worker isolation, wheel, Ruff, and strict mypy

## 9. Delivery checkpoints

1. Strategy taxonomy and human vocabulary
2. Coaching projection and report-first useful view
3. Dashboard and player profile
4. Comparison, map annotations, import-dialog and table fixes
5. Frozen-wheel browser/manual QA and completion audit

Each checkpoint is committed and pushed with only Replay Analyzer-owned files. The protected unrelated engine/audio edits and protected untracked watcher test remain untouched.

## 10. Completion criteria

The product is complete only when:

- At least one supported replay produces a useful strategy-first analysis from real imported evidence.
- A partial replay produces an honest but still useful opening analysis.
- Actual strategies can be identified when their required evidence exists.
- The first match-report viewport answers what happened, what each player attempted, and what is worth reviewing.
- Technical evidence remains fully available but is not the primary experience.
- The full acceptance, packaging, deterministic, accessibility, security, and visual gates pass against one final frozen wheel.
