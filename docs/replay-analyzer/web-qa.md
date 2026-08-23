# Replay Analyzer Web QA

Date: 23 August 2026

Status: **PARTIAL - release acceptance remains blocked.** The production-service populated fixture and installed-wheel browser matrix are green. The retained frozen-wheel run completed 138 browser tests and produced a digest-bound manifest with 20 screenshots plus accessibility evidence. Manual review found two real visual defects: the import dialog lacks usable spacing and the player-pattern table overlaps columns. A required comparable result also cannot be composed truthfully from the sole pinned replay, whose two players have different factions, while every accepted comparison definition is same-faction-only. The current result remains truthfully unavailable; it is not weakened or presented as comparable. The later parser-only provenance repair at `592ec74a8` requires one new final wheel and browser run after the visual corrections.

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
| Retained populated-probe application wheel SHA-256 | `fa5431c92cf808c60e457c208d40ce01691cfb9a1501515bf0a44c5e2bed99b3` |
| Retained run-manifest SHA-256 | `9a4d6282c5d0387f7186236c147d6c390fe69a1525cbe504d7d34b56d6fcc242` |
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
| Keyboard/screenshots | `uv run --project . pytest -q tests/browser/test_user_flows.py` | 27 passed in 76.26s; installed-wheel keyboard, focus, configured-root import, Settings, external-worker, and populated screenshot capture green |
| Pre-populated browser scope | `uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/browser -q --browser chromium` | 59 passed in 17.62s; superseded by populated additions |
| Production fixture builder | focused helper gate reported by fixture owner | 1 passed in 80.80s; completed discover requested telemetry, pending discover did not |
| Installed populated configuration clone | focused clone/environment tests | 2 passed; fresh wheel process observed revision `1` and minimum sample size `1` |
| Populated mobile-table owner RED | installed report/map mobile width probe before owner fix | report document `792/390`; map document `681/390` |
| Populated wide-table release proof | `uv run pytest tests/browser/test_offline.py -k wide_evidence_tables_are_keyboard_scroll_regions -q` | 3 passed, 34 deselected in 218.16s; desktop/tablet/mobile width, Tab reachability, ArrowRight scroll, same-origin, console, and page-error checks green |
| Populated report/map keyboard flows | `uv run --project . pytest tests/browser/test_user_flows.py -k "report_timeline_and_evidence_disclosure or map_filters_and_semantic_evidence" -q --browser chromium` | 2 passed, 25 deselected in 39.14s; fixed report scope, event filters, map controls, honest unavailable player options, evidence disclosure, and semantic alternatives green |
| Settings native mutation flow | `uv run --project . pytest tests/browser/test_user_flows.py::test_settings_preview_cancel_apply_and_diagnostic_are_keyboard_operable -q --browser chromium` | 1 passed in 27.59s; exact-origin native POST, keyboard cancel, confirmed apply, and explicit diagnostic green |
| Installed external-worker flow | `uv run --project . pytest tests/browser/test_user_flows.py::test_pending_job_is_settled_only_by_the_external_installed_worker -q --browser chromium` | 1 passed in 33.16s; separate worker ownership, HTMX polling, focus preservation, and succeeded detail green |
| Populated installed-wheel security | `uv run --project . pytest tests/browser/test_security.py -q --browser chromium` | 52 passed in 32.93s; fixed JSON timeline, map JSON, and real map PNG hardening included |
| Populated Axe and semantic matrix | `uv run --project . pytest -q tests/browser/test_accessibility.py` | 10 passed in 48.68s; report, evidence, map, both player profiles, comparison unavailable state, job, and identity audit green |
| Populated offline/no-script/reflow matrix | `uv run --project . pytest -q tests/browser/test_offline.py` | 37 passed in 65.64s; desktop/tablet/mobile, installed-wheel isolation, no-script, packaged assets, and comparison overflow green |
| Final retained installed-wheel browser matrix | `uv run --project . pytest tests/browser -q --browser chromium` | 138 passed in 167.86s |
| Web and wheel | `uv run --project . pytest tests/web tests/test_wheel.py -q` | 578 passed, 1 skipped in 46.62s |
| SQLite/report/pipeline/telemetry/parity/CLI/wheel audit after `592ec74a8` | focused aggregate gate | 931 passed, 1 skipped in 438.05s |
| Feature/strategy/longitudinal/spatial audit after `592ec74a8` | focused aggregate gate | 464 passed in 105.12s |
| Ruff | `uv run --project . ruff check src tests` | all checks passed |
| Strict mypy | `uv run --project . mypy --strict src` | success, 158 source files |
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

The tested pages also passed one-H1, banner, named primary navigation, main, footer, skip-link focus, nonempty main content without JavaScript, same-origin resource, and reduced-motion assertions. Populated report, evidence, map, two player profiles, comparison unavailable state, job, and identity Axe/semantic cases passed the installed-wheel rerun. Chart ARIA names are restored after ECharts initialization, and the identity audit accepts durable automatic-link history without treating it as an executable operator mutation.

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
| document overflow | none on populated Library, fixed Report, Map detail, or Compare at 1440x900, 1024x768, or 390x844 |
| browser-direct Ollama/external request | zero |

Unsafe-route Origin/CSRF session swapping, GET non-mutation, worker log canaries, fixed JSON/map PNG headers, and populated adapter redaction passed the installed-wheel security matrix.

## External screenshots

Artifacts are external and are not in Git or the application wheel.

| Logical name | Viewport | SHA-256 | Review |
|---|---:|---|---|
| `dashboard--desktop.png` | 1440x900 | `bcc4c1ec021b471d7de6f6b4821117430f964aade0fa0cef03916fe0d798e9a5` | captured; no automated defect |
| `library--desktop.png` | 1440x900 | `dfcb5029be3c2d435d2ebd7a05beae362f25e057f35314278fa365c85ea069ed` | reviewed at actual size; readable, no overflow |
| `jobs--desktop.png` | 1440x900 | `baabc5e19a986ff286028703cb1aee7e629eb9aa76b7c9807b34bea062f3adbd` | captured; populated state pending |
| `maps-index--desktop.png` | 1440x900 | `5c71cbbe9f82650399b7d6a395582a15343af1251e2a6381f9db72e686aafa47` | captured; map detail pending |
| `players--desktop.png` | 1440x900 | `a9fe3a6cd7dbcda67b963197c86f700a3b306bcf6d4f47e49d2179006cb20ebc` | captured; profile/identity pending |
| `compare--desktop.png` | 1440x900 | `ac757d4d48bb38587e6ab9b2aa6cf4f0652f2700c7481a734b9bc60e8f30e1f9` | captured; fixed result pending |
| `settings--desktop.png` | 1440x900 | `3a01a40cbba7b195d970a8a0b06d2e0156da3147583d143db91edfca390d3251` | captured; mutation/diagnostic pending |
| `library--tablet.png` | 1024x768 | `95e467ec0da57ed5548d425220c4936e056c40f1906d968316a84e21574a4913` | captured; automated reflow pass |
| `library--mobile.png` | 390x844 | `d8f697a076c0add9d1e6004502b6673e2ff1d874ca42124248cd68eb68b61269` | reviewed at actual size; readable, no overflow |
| `compare--mobile.png` | 390x844 | `0094503bb88f3315e43d7bd2a9fd7d8123a6bbbd4326646277ff687c3198fcaa` | captured; automated reflow pass |
| `import-dialog--desktop.png` | 1440x900 | `262b26bfd0efdd47082afc661136cd7fd73aeec19fda8bb2435d4c22e919a891` | manual fail: controls and labels are cramped |
| `map-detail--desktop.png` | 1440x900 | `c7efa5ce9dddf412837a6afdd2ce5696966fc3742c505ea00e22e8925d02bba0` | manual pass: useful spatial evidence is visible |
| `player-profile--desktop.png` | 1440x900 | `12d954962a2b67486a371be2ec98118f7370251b4b51de8561b0f38499e40aee` | manual fail: player-pattern columns overlap |

Required populated screenshot cases ran successfully for import dialog, fixed report, evidence disclosure, map detail, player profile, identity confirmation, report tablet/mobile, and map-detail tablet/mobile. The retained manifest is external at `C:\tmp\replay-analyzer-final-20260823-1610\task9-fa5431c92cf8`. Full-page report screenshots are too tall to establish actual-size readability by themselves; targeted viewport captures remain required in the final rerun.

## Release blockers

The production-service populated fixture and worker-supported pending discover now exist. Release acceptance is still blocked by these non-waived items:

- No truthful comparable result can be derived from the only available replay: `leex279` and `FOX27` use different factions, while all accepted match and longitudinal comparison definitions are same-faction-only. The current fixed result correctly reports `subject_value_unavailable`; the underlying cross-faction cohort cannot supply an aligned value. A fabricated replay, self-comparison, or relaxed comparison semantics is forbidden.
- The import dialog and player-pattern table fail manual visual review and need the approved spacing/scroll-container corrections.
- The final wheel, 138-test browser matrix, targeted report captures, and manual 100%/200% review must be rerun after those corrections and the later provenance repair.

These items are pending, not skipped or waived. `web-qa.md` remains a factual partial record rather than release closure.
