# Replay Analyzer Web QA

Date: 23 August 2026

Status: **PARTIAL - release acceptance remains blocked.** The production-service populated fixture now builds successfully and has been consumed by a fresh installed wheel. The exact responsive table proof is green. A required comparable result cannot be composed truthfully from the sole pinned replay, whose two players have different factions, while every accepted comparison definition is same-faction-only. The current fixed result remains truthfully not comparable; it is not weakened or presented as comparable. Settings preview/cancel remains a focused pending owner RED, and the remaining populated keyboard, Axe, security, worker, screenshot, and manual-review matrices are not yet complete.

## Accepted inputs

- Web Task 1 base: `cec977d8f`; browser-isolation owner fix: `73830f2dd`.
- Web Task 2: `cd90ef682` and `task-2-report.md`.
- Web Task 3: `a8c9a2e37` and `task-3-report.md`.
- Web Task 4: `task-4-report.md`, including the accepted external-worker contract.
- Web Task 5: `156ca55a6` and accepted report/evidence source at the inspected product state.
- Web Task 6: `4dac2a2df`, `task-6-report.md`, and `task-6-map-service-report.md`.
- Web Task 7: `6fbfef943` and `task-7-report.md`.
- Web Task 8: `020476916`, `task-8-report.md`, and `task-8-settings-adapter-report.md`.
- Exact-origin owner fix: `b69c3914b`; native form CSRF owner fix: `849bb4345`.
- Production Library/import adapter: `3cf4ed45f`; focus/packaged-HTMX owner fix: `a2ba92741`.

## Installed environment

| Item | Observed value |
|---|---|
| Latest populated-probe application wheel SHA-256 | `bef8a0bc5beb37d994c5666de892cfa7e385206dc5a4260c95ec58ab19df10f8` |
| Application module | isolated wheel environment |
| Python | 3.12.9 |
| pytest | 9.1.1 |
| pytest-playwright | 0.8.0 |
| Playwright | 1.61.0 |
| Chromium | 149.0.7827.55, pinned revision 1228, hardware GPU disabled |
| axe-playwright-python | 0.1.8 |
| Axe engine | 4.12.1, loaded from the wrapper's package-local `axe.min.js` |
| Runtime | cloned external temporary data/config/root-import trees; production bootstrap; installed-process configuration revision `1`; minimum longitudinal sample size `1` |
| Fixture | real `leex279_vs_fox27.rep`, production parser/import/telemetry/identity/planner/report/map/player/comparison/settings services; dynamic public IDs resolved into a digest-bound external manifest |
| Ollama | `not_requested`; no browser request |

The fixture manifest and database remain external. No ORM seed, forged job row, fabricated replay, cross-faction semantic relaxation, engine call, or model call was used. Dynamic UUIDs prevent a byte-identical manifest between independent builds, so the final run-specific manifest digest and fixed versions will be recorded only from the final retained artifact run.

## Automated results

| Gate | Exact command | Result |
|---|---|---|
| Dependency/package RED | `uv run --project scripts/replay_analyzer --no-sync pytest scripts/replay_analyzer/tests/browser/test_package_resources.py -q` | RED: browser/Axe distributions absent |
| Package resources | same focused command after exact pins and lock | 3 passed |
| Harness RED | `uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/browser/test_offline.py -k smoke -q --browser chromium` | RED: `installed_server` fixture absent |
| Installed-wheel smoke | same command after external harness | 1 passed |
| Security RED | `uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/browser/test_security.py -q --browser chromium` | RED: final CSP/isolation headers absent |
| Security owner fix | accepted commit `73830f2dd` | 13 passed installed-wheel; owner focused gate 41 passed |
| Axe and explicit semantics | `uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/browser/test_accessibility.py -q --browser chromium` | 8 passed |
| Offline/no-script/reflow | `uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/browser/test_offline.py -q --browser chromium` | 24 passed |
| Keyboard/screenshots | `uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/browser/test_user_flows.py -q --browser chromium` | 11 passed |
| Pre-populated browser scope | `uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/browser -q --browser chromium` | 59 passed in 17.62s; superseded by populated additions |
| Production fixture builder | focused helper gate reported by fixture owner | 1 passed in 80.80s; completed discover requested telemetry, pending discover did not |
| Installed populated configuration clone | focused clone/environment tests | 2 passed; fresh wheel process observed revision `1` and minimum sample size `1` |
| Populated mobile-table owner RED | installed report/map mobile width probe before owner fix | report document `792/390`; map document `681/390` |
| Populated wide-table release proof | `uv run pytest tests/browser/test_offline.py -k wide_evidence_tables_are_keyboard_scroll_regions -q` | 3 passed, 34 deselected in 218.16s; desktop/tablet/mobile width, Tab reachability, ArrowRight scroll, same-origin, console, and page-error checks green |
| Populated keyboard work-in-progress | `uv run pytest tests/browser/test_user_flows.py -k 'report_timeline_and_evidence_disclosure or map_filters_and_semantic_evidence' -q` | 2 RED in 162.88s: replay-wide/player-specific test selection and numeric key assumptions were test defects; corrected statically, rerun pending |
| Web and wheel | `uv run --no-sync pytest tests/web tests/test_wheel.py -q` | 536 passed, 1 skipped in 39.17s |
| Ruff | `uv run --no-sync ruff check src tests` | all checks passed |
| Strict mypy | `uv run --no-sync mypy --strict src` | success, 157 source files |
| Package build | `uv build` | sdist and wheel built successfully |

## Axe and semantic matrix

| Page/state | WCAG 2.0/2.1/2.2 A/AA violations | Best-practice findings |
|---|---:|---:|
| Dashboard unavailable | 0 | 0 |
| Library unavailable | 0 | 0 |
| Jobs empty | 0 | 0 |
| Maps index empty | 0 | 0 |
| Players index unavailable | 0 | 0 |
| Compare selector unavailable | 0 | 0 |
| Settings available shell | 0 | 0 |

The tested pages also passed one-H1, banner, named primary navigation, main, footer, skip-link focus, nonempty main content without JavaScript, same-origin resource, and reduced-motion assertions. Populated report, evidence, map, two player profiles, job, and identity Axe/semantic cases are implemented but have not yet completed their final rerun. Their console/page-error guard was tightened after the prior partial review. No populated Axe pass is claimed here.

All contexts use the same locale/timezone/device-scale/light/reduced-motion/service-worker/download policy. Every interactive flow fails on console or page errors. Child server processes receive isolated profile, app-data, and temporary directories and no inherited product-prefixed environment setting.

## Security and offline matrix

| Contract | Result |
|---|---|
| CSP directives and prohibitions | pass after `73830f2dd` |
| nosniff on HTML, JSON, CSS, JavaScript, and problem responses | pass |
| Referrer, frame, opener, and permissions isolation | pass |
| hostile Host authorities | 6/6 rejected with accepted 403 contract |
| missing Origin/CSRF on import mutation | rejected 403; token canary not echoed |
| locator/ORM/traceback/lease canaries in problem response | not exposed |
| exact-origin browser guard and packaged assets | pass on all tested pages |
| JavaScript-disabled essential index content | pass on Library, Jobs, Maps, Players, Compare, Settings |
| document overflow | none on Library, Maps index, or Compare at 1440x900, 1024x768, or 390x844 |
| browser-direct Ollama/external request | zero |

Full unsafe-route Origin/CSRF session swapping, GET non-mutation, worker log canaries, fixed JSON/map PNG headers, and populated adapter redaction remain pending with the fixture.

## External screenshots

Artifacts are external and are not in Git or the application wheel.

| Logical name | Viewport | SHA-256 | Review |
|---|---:|---|---|
| `dashboard--desktop.png` | 1440x900 | `e3d7c0e8116429e7ef4820b43b5bd6b64237213677aae2c2d7829a580f0b85cb` | captured; full manual matrix pending |
| `library--desktop.png` | 1440x900 | `b29057b46107c8211a50a284ef6a369f1f7f588436059c4611835a6201f2edf9` | reviewed at actual size; readable, no overflow |
| `jobs--desktop.png` | 1440x900 | `baabc5e19a986ff286028703cb1aee7e629eb9aa76b7c9807b34bea062f3adbd` | captured; populated state pending |
| `maps-index--desktop.png` | 1440x900 | `5c71cbbe9f82650399b7d6a395582a15343af1251e2a6381f9db72e686aafa47` | captured; map detail pending |
| `players--desktop.png` | 1440x900 | `a9fe3a6cd7dbcda67b963197c86f700a3b306bcf6d4f47e49d2179006cb20ebc` | captured; profile/identity pending |
| `compare--desktop.png` | 1440x900 | `ac757d4d48bb38587e6ab9b2aa6cf4f0652f2700c7481a734b9bc60e8f30e1f9` | captured; fixed result pending |
| `settings--desktop.png` | 1440x900 | `3a01a40cbba7b195d970a8a0b06d2e0156da3147583d143db91edfca390d3251` | captured; mutation/diagnostic pending |
| `library--tablet.png` | 1024x768 | `4a328260ea794e0fdef63faf7fa83da77c2f94909996cc1ae128958e4aaf6eb9` | captured; automated reflow pass |
| `library--mobile.png` | 390x844 | `d37d8c4b32d7b70eb9989e7ce4b0252c304b44894194864884198258fa399762` | reviewed at actual size; readable, no overflow |
| `compare--mobile.png` | 390x844 | `cf51e1a9692ebb8db7ee9e4eb54aa4fdac67dd049416aab109865b6e342dfd49` | captured; automated reflow pass |

Required populated screenshot cases are now implemented for import dialog, fixed report, evidence disclosure, map detail, player profile, identity confirmation, report tablet/mobile, and map-detail tablet/mobile. They have not yet run against the final frozen wheel, so no hashes or review result are claimed.

## Release blockers

The production-service populated fixture and worker-supported pending discover now exist. Release acceptance is still blocked by these non-waived items:

- No truthful comparable result can be derived from the only available replay: `leex279` and `FOX27` use different factions, while all accepted match and longitudinal comparison definitions are same-faction-only. The current fixed result correctly reports `faction_mismatch` and `subject_value_unavailable`. A fabricated replay, self-comparison, or relaxed comparison semantics is forbidden.
- The required Settings preview/cancel path is pending a focused owner fix. Current native `/settings/preview` returns a fragment with Apply but no cancel/return action and no application shell.
- The corrected populated keyboard tests, external-worker browser flow, populated Axe/security/offline matrices, remaining screenshots, full static/build gates, and manual 100%/200% review have not yet completed against one final frozen installed wheel.

These items are pending, not skipped or waived. `web-qa.md` remains a factual partial record rather than release closure.
