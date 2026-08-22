# Replay Analyzer Command Center UI Design

**Status:** Approved visual and UX direction

**Date:** 2026-08-22

**Scope:** Local Replay Analyzer V2 web presentation only. This document defines the command-center redesign; it does not change the replay-analysis evidence model, engine, persistence schema, web security boundary, or job semantics.

## 1. Outcome

The web application becomes a restrained, modern Command & Conquer-inspired replay command center: a wide, charcoal operational workspace for finding a replay, judging whether its analysis is trustworthy, and moving into evidence. It borrows the clarity and tactical density of an RTS operations screen without imitation UI, faction iconography, military cosplay, glow effects, or novelty decoration.

The primary user journey is replay-first:

```text
Dashboard signal -> Replay Library -> Replay report -> Evidence / map / player context
                     ^
                     Import and pipeline state
```

The browser is an evidence viewer and operator surface. It does not invent match facts, conceal incompleteness, or become a second analysis engine.

## 2. Goals and non-goals

### Goals

- Make the next useful replay, current pipeline state, and analysis quality legible at a glance.
- Make the Replay Library a dense, fast, readable working table rather than a gallery of cards.
- Preserve an evidence-first reading order: quality and availability before metrics, conclusions, and actions.
- Use a calm charcoal workspace with muted olive and amber accents; reserve red exclusively for actual failure or desync.
- Give every page a compact shared shell, clear keyboard operation, responsive reflow, and durable offline behavior.
- Show realistic demo content while the production analytics adapter is unavailable, without letting it resemble production evidence.
- Give later report, map, player, comparison, jobs, settings, and evidence pages one coherent vocabulary rather than separate mini-products.

### Non-goals

- Recreating a game HUD, loading screen, faction interface, or a retro parody.
- Adding a frontend framework, remote font, image CDN, analytics beacon, or a second web application.
- Replacing absent telemetry, map exports, history, or Ollama output with fabricated production values.
- Changing FastAPI routes, the `WebApplicationPort` contract, persistence, import safety, CSRF, CSP, localhost binding, or background-job behavior as part of this design.
- Promoting unavailable roadmap areas to clickable pages before their route and adapter capability exist.

## 3. Design principles

1. **Operational, not ornamental.** One strong page heading, one principal action, and useful information density beat decorative panels.
2. **Replay before dashboard.** The dashboard routes people into a recent, notable, or blocked replay; the Library remains the working home for large collections.
3. **Evidence precedes interpretation.** Observed, derived, and inferred labels are explicit text-and-icon markers, never color-only treatment.
4. **State is a first-class object.** Pending, partial, unavailable, failed, verified, and demo states use consistent wording, location, and behavior.
5. **Tactical restraint.** Thin rules, map-grid echoes, compact metadata lines, and deliberate state transitions create the character. No gradients, neon/glow, beveled chrome, fake scan lines, excessive borders, or animated background noise.
6. **Calm motion.** Motion confirms a user action or a genuine state change. It never competes with evidence or obstructs reduced-motion users.

## 4. Visual system

### 4.1 Tokens

The command center is dark by default. It must not silently swap into a pale application merely because the OS selects a light scheme; the visual identity is the charcoal workspace. `forced-colors` and browser contrast settings retain their native behavior, and a future explicit high-contrast preference may alter tokens without altering information hierarchy.

| Token group | Token | Value / rule | Use |
|---|---|---|---|
| Canvas | `--cc-canvas` | `#151815` | Page background |
| Surface | `--cc-surface-1` | `#1d211c` | Main panels and table body |
| Surface | `--cc-surface-2` | `#252a23` | Raised controls, sticky bars, dialog |
| Surface | `--cc-surface-3` | `#2d332a` | Selected or hover surface, never a glow |
| Rule | `--cc-rule` | `#4b5246` | Borders, table separators, grid accents |
| Text | `--cc-text` | `#f1f0e8` | Primary text |
| Text | `--cc-text-muted` | `#bac0b2` | Supporting text; still AA at normal size |
| Olive | `--cc-olive` | `#a6b27b` | Current view, verified state, primary affirmative affordance |
| Amber | `--cc-amber` | `#dfaf51` | Attention, partial data, active filter count, pending work |
| Failure | `--cc-failure` | `#e06a60` | Failed or desynced; never a decorative accent |
| Info | `--cc-info` | `#8eb8c7` | Observed/evidence navigation and neutral technical status |
| Focus | `--cc-focus` | `#f4cf72` | 3 px visible focus ring with 2 px offset |
| Player series | `--cc-player-a`, `--cc-player-b`, `--cc-player-c`, `--cc-player-d` | Accessible muted blue, amber, teal, violet series | Charts only, paired with names/patterns |

Use a 4 px base spacing scale: 4, 8, 12, 16, 24, 32, 48, and 64 px. Borders are one physical pixel; selected rows can use a 3 px left olive rule. Corners are 2 px for controls and 4 px for dialog/panels. Shadows are limited to a soft dialog elevation shadow; ordinary cards do not float.

### 4.2 Typography and data treatment

- Body: `system-ui, -apple-system, "Segoe UI", sans-serif`, 15–16 px, 1.5 line height.
- Tactical headings: `"Arial Narrow", "Bahnschrift", "Segoe UI", sans-serif`, uppercase only for short labels, 600–700 weight, normal text capitalization for page titles. Tracking is modest (`0.04em` maximum), never used for paragraphs.
- Code, IDs, SHA prefixes, frame ranges, timestamps, paths in protected diagnostic disclosure, and evidence references: `"Cascadia Mono", Consolas, monospace`. Mono is never the default body face.
- Tabular numeric data uses `font-variant-numeric: tabular-nums`; values are right-aligned and units stay attached to their value.
- Minimum interactive text is 14 px. Compact table metadata may be 13 px only when it retains AA contrast and has a 40 px row target or an adjacent row action.

### 4.3 Command-center motifs and motion

Motifs are structural, not background wallpaper: a thin 8 px map-coordinate gutter beside a spatial panel, 1 px crosshair dividers around an active timeline point, and sparse labelled grid ticks in a map or trend chart. Do not use CSS gradients to fake a grid. Any map grid must be a semantic SVG/canvas/chart layer with an accessible summary, or a simple ruled container.

Allowed motion is 120–180 ms ease-out for a filter result replacement, drawer expansion, dialog entry, and selected-row transition. Job progress may advance at the refresh cadence; it does not pulse. Respect `prefers-reduced-motion: reduce` by removing transforms and replacing animated updates with immediate changes plus live-region text. Never auto-scroll, marquee, pulse, shimmer, or loop a decorative animation.

## 5. Information architecture and shared shell

### 5.1 Navigation

At desktop widths, the shell is a compact two-row masthead within the wide workspace:

1. Top utility row: product mark, persistent mode label (`Local` or `Demo data`), current pipeline summary, command button, and the compact import action.
2. Primary row: Dashboard, Library, Players, Compare, Maps, Jobs, Settings. Only installed, route-backed capabilities are links. A not-yet-installed area remains visible as a disabled label in the command menu with a plain-language reason; it is not a dead link.

The current page has an olive lower rule and `aria-current="page"`; it is not communicated only by color. The command button reads `Navigate` at first render and may advertise the shortcut as `Navigate (Ctrl+K)` after keyboard enhancement is active. It contains navigation and permitted import commands only, never arbitrary filesystem access or administrative actions.

The global pipeline summary is a compact text control: `1 running · import > parse · 42%`. It opens Jobs when that route exists; before then it is a non-action status label. A genuine failure changes this text to `1 failed · review` and uses the failure token plus icon and text. Partial or unavailable analysis uses amber or muted text, not red.

### 5.2 Workspace geometry

The desktop application uses `max-width: 1600px`, 24–32 px outer gutters, and a minimum main content width of 960 px. The shell may be full-width, while the content remains centered inside the same command-center grid. Use a 12-column grid at 1440 px and a 6-column grid at tablet width. Avoid the current narrow 70rem layout for the Library and reports.

Every page follows this hierarchy:

```text
Context line / mode and quality
Page title + one primary action
Operational content
Evidence, provenance, diagnostics, or secondary controls
```

The skip link, header, main landmark, single page `h1`, footer, focus return, and live feedback region remain present. Footer wording is compact: `Local-only evidence viewer · no remote data transfer`.

## 6. Page specifications

### 6.1 Dashboard — operational overview

The dashboard answers: *What happened recently, what needs attention, and where should I go next?* It is not a generic card grid.

1. **Command strip.** Above the title: mode marker, collection coverage (`24 replays indexed`, or `Demo dataset: 24 illustrative replays`), current adapter availability, and Import Replay action. If the real adapter is unavailable, the production state remains explicit even in Demo mode.
2. **Recent matches table.** The primary 8-column area lists the latest five to eight replays: replay identity, players, result, map, analysis state, evidence tier, observed time, and a `Review` link. It uses the same row model as the Library so the user learns one reading pattern.
3. **Operations rail.** A 4-column companion at desktop: current pipeline stages, blocked/failed work, and next action. It has one concise status line per job, attempt count, and no fake progress for absent jobs.
4. **Trends band.** Two restrained analytical views, not KPI tiles: import/verification activity over time and faction or quality distribution. Each includes its date range, sample count, availability state, text summary, and route to a filtered Library. The chart area has a table/text alternative.
5. **Notable evidence queue.** A short, deterministic list of matches requiring review: CRC mismatch, partial telemetry, unreviewed inferred assessment, or a comparison-ready player history. It may only render a row when the adapter supplies the qualifying condition.

On an empty real library, show an operator-oriented empty state: `No replay evidence in this library yet` followed by the safe configured-root import action and an explanation of the blocked upload state when applicable. Do not show zeroed trend charts.

### 6.2 Replay Library — the primary working surface

The Library is a wide, dense table. It defaults to the most recently observed replay and retains the canonical filter URL. It has no decorative thumbnails and no duplicated card view at desktop.

**Toolbar and filters**

- The sticky toolbar contains search, an `All filters` button, active-filter count, sort control, result count, and Import Replay. It remains under the masthead while the table scrolls.
- The always-visible compact filters are result, faction, map, evidence tier, and analysis status. These are precisely the fields a user needs to triage the collection. The advanced sheet contains player, matchup, patch, strategy, lifecycle, source kind, observed date range, and page size.
- Selected filter chips are textual (`Faction: China ×`), individually removable, keyboard reachable, and preserved in the canonical URL. `Clear filters` returns to the canonical unfiltered list.
- Form submission works without JavaScript. HTMX may replace only the results region and announce `24 replay results loaded`; it must preserve focus on the submitting control and not silently reset filters.

**Table**

The header and the filter toolbar are sticky. The first Replay column is sticky on desktop; other columns may scroll horizontally rather than becoming illegible. Header cells provide sort state in text and `aria-sort` when sortable.

| Column | Content and behavior |
|---|---|
| Replay | Filename or safe public identity, patch, short hash; sticky left; row opens report only when available |
| Players | Display names, slot/faction/result labels; no external provenance treated as identity |
| Result | Win/loss/team outcome or `Result unavailable` |
| Faction | Per-player faction chips with text, not color-only |
| Map | Display name and known map availability; no invented map preview |
| Evidence | Observed / Derived / Inferred label plus icon; opening the drawer exposes tier and source detail |
| Status | Lifecycle plus pipeline state; `Verified`, `Partial`, `Pending`, `Unsupported`, `Desynced`, or `Failed` with explicit text |
| Observed | UTC date/time, tabular numerals |
| More | Evidence/provenance disclosure and permitted report action |

Rows are 52–64 px depending on player count. Alternating fills are not required; use horizontal rules, hover/focus surface, and a selected left rule. A failed or desynced row has a small failure icon and label, not a full red background. The provenance disclosure remains secondary and never replaces replay-local player identity.

**Empty, partial, and unavailable states**

- `No results match these filters` retains filters and offers `Clear filters`.
- Partial results show the received rows plus a compact amber data-quality notice with the adapter reason code available in Details.
- Unavailable results replace only the table body with an honest unavailable state. Filters and import action remain usable where their adapters are available.

### 6.3 Replay report and evidence inspector

The report begins with a full-width replay identity strip: players, result, map, patch, duration, quality state, source mode, and evidence coverage. Quality warnings and data absence are above all interpretation. A compact `Jump to` row targets Overview, Timeline, Economy, Production, Combat, Spatial, Strategy, and Evidence only when their sections exist.

The desktop report uses a 8/4 composition: narrative and charts left; quality, player context, report version, and evidence summary right. Mobile collapses to a single reading order. Timeline, map, and comparison controls keep their text equivalents and current selection in the page heading or summary.

Every metric and conclusion carries a tier label and evidence link. The evidence drawer opens in place for quick inspection and has a route-backed detail page for deep links. It identifies exactly one of:

- **Observed:** raw event/command and stable run/sequence reference.
- **Derived:** formula/extractor version and observed inputs.
- **Inferred:** model/prompt/version, confidence, and cited evidence.

Unavailable content says why and what is missing. It never becomes an empty chart frame, generic narrative, straight-line unit path, or substitute number.

### 6.4 Map and spatial analysis

The map view is an analytical instrument, not a decorative minimap. Its ruler shows map identity, coordinate mode, transform version, time window, selected players, and source availability. The main canvas contains only authoritative exported terrain/pathability and supplied overlays. A narrow legend names every series and provides non-color distinction.

The time scrubber is keyboard-operable: arrow keys step by the documented interval, Page Up/Page Down change windows, and the live summary announces the resulting time. When map assets, navigation, or player-centric transform are unavailable, disable the specific control and state the reason; do not draw procedural terrain or approximate paths.

### 6.5 Players and Compare

Players lists identity and replay history as evidence-bearing records, not scores. Profiles lead with original names and alias audit status, then filters, sample size, quality exclusions, strategy distribution, timing ranges, and replay list. Provider tokens and Strata provenance have a separately labelled disclosure.

Compare is a two-column aligned evidence surface. The comparison header names both selected scopes, feature definition/version, units, quality filters, sample sizes, and date range. If any of those are incompatible, display `Not comparable` in the result region with the exact incompatibility rather than a chart. Merge/split remains a deliberate confirmation flow with user-entered reason, CSRF, and affected-count preview.

### 6.6 Import, Jobs, and Settings

Import is a focused dialog/sheet, not a file-browser imitation. It offers only configured-root selection and the existing opaque ingress handoff. The dialog says when upload is unavailable and why. It never renders absolute paths, directory trees, or a client-side filesystem picker beyond the explicitly permitted ingress design.

Jobs is a dense queue with stage, state, attempt, progress, time, retryability, and concise safe diagnostic. Jobs poll only when real pending/running work exists. Retry and cancel controls require the existing guarded command boundary; failure red appears only for actual failed states. Cancelled work is neutral slate unless its underlying job failed.

Settings is a diagnostics-led configuration surface. It distinguishes current effective configuration from editable values, redacts secrets, labels operations with side effects, and requires confirmation before queuing invalidation work. It is never a visual dumping ground for raw server paths or logs.

## 7. Component and state behavior

### 7.1 Shared components

| Component | Required behavior |
|---|---|
| Mode banner | `Production data`, `Demo data`, or `No analytics adapter`; icon + text + Details; persistent in Demo mode |
| Status badge | Textual status, icon/shape, color token, reason link; no status communicates only by color |
| Evidence marker | Observed/Derived/Inferred text, icon, public evidence ID data attribute; no new evidence tier |
| Quality notice | Sits before affected result; distinguishes partial, unavailable, unsupported, desynced, failed |
| Command palette | Dialog with focus trap, Escape/Close, return focus, available links/actions only; no remote search |
| Filter sheet | Native form fallback; visible active count; modal only at compact widths |
| Dense table | Caption, sticky header, horizontal scroll cue, keyboard focus, labelled empty state |
| Chart/map frame | Visual title, data source/availability, textual summary/table alternative, local rendering only |
| Evidence drawer | Accessible disclosure with deep link and visible tier; no hidden evidence assumptions |

### 7.2 State hierarchy

Production state and visual status must not be conflated:

1. **Mode** describes where the screen values originate: production adapter, fixed demo fixture, or unavailable adapter.
2. **Availability** describes whether the requested data is available, partial, or unavailable.
3. **Quality/lifecycle** describes the replay/job: discovered, parsed, engine verified, partial, desynced, unsupported, failed, etc.
4. **Evidence tier** describes the claim: observed, derived, or inferred.

For example, a Demo screen can show an illustrative `engine verified` label only alongside the persistent `Demo data — illustrative, not replay evidence` mode banner. A production report with missing telemetry shows `Partial` availability and no telemetry-dependent claim, regardless of its surrounding visual density.

## 8. Demo-data boundary

The current `UnavailableWebApplicationPort` is correct for the production safety contract: no production adapter means no completed report. The approved command-center preview needs realistic content, but that content must be a separate, explicit presentation mode.

- Demo mode uses a versioned, local, fixed fixture dataset through a dedicated demo port/factory or equivalent composition root. It never enters SQLite as observations, never runs the engine, never populates production reports, and never affects real filters, counts, or jobs.
- A persistent masthead marker reads `Demo data — illustrative UI preview; not replay evidence`. It is visible before all page content, remains in screenshots, and is repeated in chart/table captions where numerical demo content appears.
- Demo rows use plausible filenames, maps, players, status sequences, pipeline states, and time ranges, but their provenance/evidence drawer says `Illustrative fixture; production evidence unavailable`. It must not mint an observed/derived/inferred record or a public evidence ID that looks authentic.
- Switching to production is explicit. Production mode with no adapter returns to the existing unavailable state; it does not carry over demo filters, links, or numeric summaries.
- Browser and snapshot tests use fixed demo values so visual review is deterministic. Tests for real production adapters continue to assert the current honest unavailable behavior where no adapter is composed.

This resolves the apparent tension between the requested demo preview and the V2 prohibition on synthetic analysis: demo data is an interface fixture, visibly non-evidentiary, never an analysis fallback.

## 9. Responsive, keyboard, and accessibility requirements

### Responsive behavior

| Viewport | Required layout |
|---|---|
| 1440 px and wider | 12-column command center; dashboard 8/4 split; Library sticky toolbar/header and sticky replay column |
| 1024–1439 px | 6–12 column fluid layout; dashboard operations rail drops below recent table if needed; Library keeps horizontal table scroll rather than reducing columns to unreadable labels |
| Below 768 px | Compact masthead, command menu for navigation, single-column dashboard/report, filter dialog, table converts each replay to a labelled row summary with result/faction/map/evidence/status preserved |
| Below 480 px | 16 px page gutters, 44 px minimum hit targets, one primary action per line, no persistent side rail |

No essential information is available only on hover. Horizontal table scrolling has an explicit visual edge cue and an accessible instruction. A row summary on narrow screens keeps the replay name, players, result, faction, map, evidence, and status before optional provenance.

### Keyboard and assistive technology

- Tab order follows the visual task order: skip link, shell, primary action, filters, results, disclosures.
- `Ctrl+K`/`Cmd+K` opens the command dialog only when focus is not in a text-entry or selectable control; Escape closes and returns focus to the invoker.
- Dialogs use native `dialog` semantics where supported, trap focus, offer a visible close control, and return focus predictably.
- All controls have visible labels. Filter chips, sort, pagination, result updates, job progress, and drawer state have clear accessible names and polite live feedback.
- Tables retain captions, `th` scope, and text labels; charts/maps provide concise summaries plus inspectable tabular data or an equivalent list.
- Meet WCAG 2.2 AA contrast for normal text, controls, focus indicators, and non-text state contrast. Test text labels without color, in forced-colors mode, at 200% zoom, and with reduced motion.

## 10. Security and offline constraints

The visual layer preserves the existing contracts:

- Serve only package-owned local CSS, JavaScript, SVG/data assets, and vendored chart libraries. No CDN, remote font, analytics, background fetch, external image, `@import`, inline event handler, dynamic script injection, or remote CSS.
- Preserve the current CSP: self-only script/style/font/image sources, `object-src 'none'`, `base-uri 'none'`, and `frame-ancestors 'none'`. Any necessary local asset must work under that policy.
- Preserve literal loopback default binding, local-host request checks, same-origin protection, CSRF for native mutation forms, opaque public IDs, and safe problem responses.
- Do not expose source locations, managed paths, raw logs, absolute map/cache paths, secret configuration, or arbitrary filesystem navigation. Provenance and diagnostics remain allow-listed public DTO content.
- HTMX and charts enhance an HTML-first screen. With JavaScript blocked, pages, filter submissions, import form safety messaging, quality notices, and evidence disclosures remain usable.
- Offline review is a release requirement: browser network tests block non-local requests; only an explicitly configured local Ollama endpoint may be contacted by the backend, never directly by page JavaScript.

## 11. Implementation decomposition and preserved boundaries

This is a design decomposition, not a production-code change list. Implementers preserve the existing web seams and build presentation capability outward from them.

| Slice | Presentation responsibility | Existing boundary that must remain intact |
|---|---|---|
| Design tokens and shell | Replace the minimal CSS/layout with the charcoal shell, compact navigation, status/mode strip, focus/reduced-motion behavior | `base.html`, package-local static assets, CSP/local-asset tests, landmark contracts |
| Demo composition | Provide deterministic illustrative snapshots for visual preview | Separate demo composition from `UnavailableWebApplicationPort`; do not mutate production DTO semantics or create evidence |
| Dashboard | Add operational view models for recent matches, pipeline, trends, and review queue | `WebApplicationPort.dashboard()` remains a safe immutable snapshot; no ORM or analysis logic in templates/routes |
| Library | Recompose current query/page DTOs into dense toolbar, table, sticky/filter behavior, responsive row summaries | `ReplayLibraryQueryDTO`, canonical URL ordering, HTML/HTMX route split, pagination, locator redaction |
| Report, map, players, compare | Apply shared status/evidence components to future route-backed pages | Only render adapter-supplied content; retain evidence-tier and unavailable-state rules |
| Import, jobs, settings | Apply command-center forms, queues, diagnostics, and confirmation states | Opaque ingress, configured-root validation, CSRF/origin checks, no path exposure, local-only constraints |
| Visual QA | Capture and compare designated states without network access | Existing route/security/accessibility tests remain authoritative; screenshots are external test artifacts |

The active Task 3 replay/import files, staged strategy work, and inherited engine/CMake changes are not redesign targets. The UI work must stay in the web presentation layer and its approved port/view-model extensions.

## 12. Acceptance criteria and visual QA

### Acceptance criteria

- The rendered desktop shell reads as a restrained charcoal command center; there are no gradients, glow, faction parody, generic dashboard-card grids, remote assets, or red used for normal selection/success/pending state.
- Dashboard shows operational recents, trends, import/pipeline state, and review queue when adapter data exists; it shows an honest empty/unavailable state otherwise.
- Library renders a dense readable table with sticky filter toolbar/header and explicit Result, Faction, Map, Evidence, and Status fields. Desktop retains readable columns; mobile preserves those fields in each row summary.
- Every production claim visibly declares availability, quality where relevant, and evidence tier. Missing evidence produces an explicit unavailable state, never a placeholder metric, narrative, route, path, or synthetic map.
- Demo preview is immediately and persistently labelled as illustrative/non-evidentiary, is deterministic and local, and cannot be mistaken for an adapter-produced report.
- All navigation is compact and route/capability-aware; unavailable areas are visibly non-actions, not broken links.
- Keyboard-only operation covers skip navigation, command dialog, filters, table, disclosures, import dialog, map time controls, and future confirmation forms.
- WCAG AA, reduced-motion, forced-colors, 200% zoom, semantic table/chart/map alternatives, CSP, localhost, CSRF, locator-redaction, and external-network-blocking requirements remain satisfied.

### Screenshot and review matrix

Capture screenshots outside Git using a fixed local fixture at these viewport sizes: 1440x900, 1024x768, and 390x844. Each screenshot records mode, fixture revision, browser zoom, and reduced-motion setting in the visual QA log.

| Screenshot ID | Required state |
|---|---|
| `01-dashboard-demo-desktop` | Demo banner, recent matches, operations rail, trends and review queue |
| `02-library-demo-desktop` | Sticky filters, dense table, evidence/status columns, active filter chips |
| `03-library-no-results` | Retained filters and clear-filters state |
| `04-library-unavailable` | Honest unavailable adapter state with no fake rows |
| `05-report-partial` | Quality warning above affected analysis and evidence marker |
| `06-map-unavailable` | Disabled spatial controls with explicit reason, no procedural substitute |
| `07-import-safety` | Configured-root dialog and blocked opaque upload disclosure |
| `08-jobs-failure` | Genuine failure rendered red with retryability/diagnostic context |
| `09-library-mobile` | Compact navigation and readable replay row summary retaining mandatory fields |
| `10-command-keyboard-focus` | Visible focus, open dialog, and return-focus verification |

Review each capture for hierarchy, text wrapping, 44 px targets on compact layouts, table overflow cue, contrast, non-color state labels, mode banner persistence, absence of unintended paths/identifiers, and absence of remote network requests. Validate a separate reduced-motion capture to confirm there is no animated progress, transform, shimmer, or auto-scroll.

## 13. Reconciliation findings

The inspection found no unresolved placeholder in this specification. The following existing conditions are intentional but must be reconciled during implementation:

1. The current CSS is a minimal light/dark generic shell with a 70rem maximum width; this spec intentionally replaces its visual language with a charcoal, wider replay-first workspace while retaining local assets and accessibility contracts.
2. Current navigation exposes only Dashboard, Players, and Library, while older planning lists Maps/Jobs/Settings. This spec makes future areas capability-aware and non-clickable until their route and adapter exist; it does not require premature routing.
3. Current dashboard and library adapters correctly report `analytics_adapter_pending` / `replay_library_adapter_pending` rather than inventing data. The requested demo preview is resolved as a distinct, labelled fixture composition, never as a production adapter fallback.
4. Current upload remains intentionally blocked at `opaque_ingress_handoff_pending`. The redesigned import surface must keep that block and the configured-root/CSRF contracts rather than styling it as a working upload.
5. Existing web-plan language permits local HTMX/ECharts enhancement; this design keeps the HTML-first fallback and requires chart/map alternatives, so the command-center appearance cannot become JavaScript-dependent.
