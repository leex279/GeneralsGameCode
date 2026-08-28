# Strategy-first product acceptance matrix

<!-- TheSuperHackers @info Leex 25/08/2026 Separate current production evidence from external-toolchain limits. (#TBD) -->

| Acceptance outcome | Status | Evidence |
|---|---|---|
| External profile is visible for a canonical player | Pass | Live Dominator page links to the verified public Strata profile |
| Player aliases resolve to one canonical history | Pass | `-dominator-`, `bigjohn`, `ea-fames`, and `octopussy` resolve to one player with 23 replay histories and a **Played as** column |
| Supplied replay corpus imports without duplicate inflation | Pass | 25 source files, 23 unique SHA-256 hashes, 23 successful parser-backed reports |
| Corpus strategy scouting uses authoritative evidence | Awaiting engine telemetry | Parser-only reports correctly expose unavailable strategy evidence; no inferred economy, army, battle, or build-order facts are fabricated |
| Full replay report reaches the observed terminal frame | Pass | Head `1cc86793c`; 15:33.3 report evidence through frame 56003; telemetry complete records frame 56004 |
| Retail replay completes deterministically in the modern engine | Pass | Pinned natural replay exits cleanly at frame 56004 with no CRC mismatch |
| Full-match economy telemetry is internally consistent | Pass | 248,459-record trace passes strict schema, cash-fold, sampling-cadence, exact-count, and clean-completion assertions |
| Production media package is complete and standards-labelled | Pass | v47 manifest: video/audio/subtitle, BT.709, 933.4s |
| Current report and player profile are reviewable in the live web application | Pass | Local app HTTP 200; full browser suite `152 passed` |
| Static Python quality gates | Pass | Ruff passed; strict mypy passed across 186 source files |
| Zero Hour modern Release build | Pass | Incremental `z_generals` Win32 Release build succeeded on current head |
| Deterministic fallback survives Ollama failures | Pass | 27B timeout, 4B invalid envelope, 9B schema-validation failure; fallback retained |
| Broad non-engine analyzer regression suite | Pass | `3399 passed, 12 skipped, 463 deselected` in 12:03 |
| VC6 / MinGW engine builds | Not verified | Those external toolchains were not run in this checkpoint |
| Protected unrelated worktree edits | Preserved | Stat-only and unrelated user-owned edits remain outside branch-owned proof |

The v48 review video is `C:\Users\Leex279\Documents\GitHub\GeneralsGameCode\.tmp\replay-analyzer-review\commented-cast-v48-normal-color-battle-safe-full-match.mp4`; SHA-256 `C2ACAAF48ACA647C7E95950D2959A4CD7CA07338D20A2B7E16D746E65A7DBFF6`.
The final wheel hash is `2BB0E0427054D5EB561EEA81FCD4FD73D254A37EE3B16CE5E04BCE82DFBCBD38`.
