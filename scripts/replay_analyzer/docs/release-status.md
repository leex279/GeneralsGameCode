# Replay Analyzer strategy-first release status

<!-- TheSuperHackers @info Leex 25/08/2026 Record reproducible product evidence and explicit verification limits. (#TBD) -->

Date: 2026-08-25
Tested product head: `f1a8c9014`

## Player-visible outcome

The current web report covers the full 15:33.3 replay, with report evidence through frame 56003; the telemetry complete event records final frame 56004. The full v47 production manifest verifies video/audio/subtitle evidence, BT.709 metadata, and 933.4 seconds. The v48 review video passed technical verification and was inspected separately from the live web report. Deterministic coaching remains available when Ollama interpretation fails.

## Exact verification

- Zero Hour Release build: successful.
- v47 production manifest: video, audio, subtitles, BT.709, 933.4 seconds.
- v48 review video: `C:\Users\Leex279\Documents\GitHub\GeneralsGameCode\.tmp\replay-analyzer-review\commented-cast-v48-normal-color-battle-safe-full-match.mp4`.
- Video suite: `158 passed, 1 skipped`.
- Packaged scouting test: passed.
- Live browser review: completed against the current web report.
- Broad Python full-suite run: in progress; no new aggregate result is claimed. Last verified baseline (2026-08-23): `3191 passed, 16 skipped`.

## Ollama verification

- 27B: timeout.
- 4B: invalid response envelope.
- 9B: schema-validation failure.

All three probes failed closed and retained the deterministic evidence-backed fallback. No live Ollama prose is treated as verified product evidence.

## Retained production evidence

- v47 production manifest: `C:\gra-prod-20260825-status15\test_configured_production_imp0\p\video-runs\2174509c-d38b-4e3d-85a8-b348f97ebdb4\video-manifest-v1.json`.
- v48 review video: `C:\Users\Leex279\Documents\GitHub\GeneralsGameCode\.tmp\replay-analyzer-review\commented-cast-v48-normal-color-battle-safe-full-match.mp4`.
- Full replay report evidence: 15:33.3 / frame 56003; telemetry complete event final frame 56004.
- Production media duration: 933.4 seconds, BT.709.

## Explicit limits

- VC6 and MinGW builds, and retail replay non-interference, remain unverified.
- Protected unrelated engine edits remain in the worktree and are outside this product checkpoint.
- The protected browser fixture and telemetry-import test blocker remain untouched and are not branch-owned completion evidence.
