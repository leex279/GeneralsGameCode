# Replay Analyzer strategy-first release status

<!-- TheSuperHackers @info Leex 25/08/2026 Record reproducible product evidence and explicit verification limits. (#TBD) -->

Date: 2026-08-28
Tested product head: `1cc86793c`

## Player-visible outcome

- The live application remains available at `http://127.0.0.1:8781/`.
- Canonical players can expose an optional verified public HTTPS profile. The live Dominator page links to `https://strata.gamereplays.org/zh/player/17945`, shows 23 analyzed replay histories, and identifies each embedded replay name in a **Played as** column.
- The supplied Dominator corpus contained 25 files and 23 unique replay hashes. All 23 unique replays parsed, persisted, and produced parser-backed reports without inventing telemetry-derived strategy claims.
- The pinned retail replay now completes cleanly in the modern Zero Hour engine through frame 56004. Its retained strict trace contains 248,459 records and authoritative full-match economy, production, upgrade, science, special-power, and supply evidence.
- The full web report covers 15:33.3, with report evidence through frame 56003 and the telemetry complete event at frame 56004.
- The v48 review video contains the corrected natural color and safer two-sided battle framing requested during review.

## Exact verification

- Zero Hour Win32 Release incremental build: passed on `747b7dd38`.
- Ruff: all source and tests passed.
- Mypy strict: no issues in 186 source files.
- Browser suite: `152 passed`.
- Focused identity/player/web/import lane: `95 passed`.
- Migration and wheel lane: `17 passed`.
- Focused fast engine contracts: map export `7 passed`; forced-CRC order/outcome `1 passed`; runner cross-binding and isolated runtime staging both passed.
- Natural replay economy trace: 248,459 strictly validated records; frame 56004; clean shutdown; no CRC mismatch.
- Broad non-engine Python suite: `3399 passed, 12 skipped, 463 deselected` in 12:03.
- v47 production manifest: video, audio, subtitles, BT.709, 933.4 seconds.
- v48 review video: `C:\Users\Leex279\Documents\GitHub\GeneralsGameCode\.tmp\replay-analyzer-review\commented-cast-v48-normal-color-battle-safe-full-match.mp4`, 226,857,916 bytes, SHA-256 `C2ACAAF48ACA647C7E95950D2959A4CD7CA07338D20A2B7E16D746E65A7DBFF6`.

## Ollama verification

- 27B: timeout.
- 4B: invalid response envelope.
- 9B: schema-validation failure.

All three probes failed closed and retained the deterministic evidence-backed fallback. No live Ollama prose is treated as verified product evidence.

## Retained production evidence

- v47 production manifest: `C:\gra-prod-20260825-status15\test_configured_production_imp0\p\video-runs\2174509c-d38b-4e3d-85a8-b348f97ebdb4\video-manifest-v1.json`.
- v48 review video: `C:\Users\Leex279\Documents\GitHub\GeneralsGameCode\.tmp\replay-analyzer-review\commented-cast-v48-normal-color-battle-safe-full-match.mp4`.
- Live Dominator profile: `http://127.0.0.1:8781/players/1087c4d1-a37d-4e8f-ad6c-c61a27206f9c`.
- Full replay report evidence: 15:33.3 / frame 56003; telemetry complete event final frame 56004.

## Explicit limits

- The 23 unique Dominator corpus reports are parser-backed. Their economy, army, engagements, and strategy remain unavailable until each replay completes authoritative engine telemetry; the UI intentionally labels that evidence unavailable rather than fabricating coaching.
- VC6 and MinGW builds remain unverified. Modern Win32 retail-replay playback is verified for the pinned match.
- Protected unrelated/stat-only engine, fixture, vendor-manifest, and telemetry-model edits remain in the worktree and are outside this checkpoint.
