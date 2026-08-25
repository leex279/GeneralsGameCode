# Strategy-first product acceptance matrix

<!-- TheSuperHackers @info Leex 25/08/2026 Separate current production evidence from external-toolchain limits. (#TBD) -->

| Acceptance outcome | Status | Evidence |
|---|---|---|
| Full replay report reaches the observed terminal frame | Pass | Current head `f1a8c9014`; 15:33.3 report evidence through frame 56003; telemetry complete event records final frame 56004 |
| Production media package is complete and standards-labelled | Pass | v47 manifest: video/audio/subtitle, BT.709, 933.4s |
| Current report is reviewable in the live web application | Pass | v48 review artifact and live browser review |
| Video QA suite | Pass | 158 passed, 1 skipped |
| Packaged scouting journey | Pass | Packaged scouting test passed |
| Deterministic fallback survives Ollama failures | Pass | 27B timeout, 4B invalid envelope, 9B schema-validation failure; fallback retained |
| Broad Python analyzer regression suite | In progress | Current run has not completed; last verified 2026-08-23 baseline was 3191 passed, 16 skipped |
| VC6 / MinGW engine builds and retail replay non-interference | Not verified | Toolchain and protected-engine scope remain external limits |
| Protected browser fixture/import test blocker | Explicit blocker | Protected files remain untouched and are excluded from branch-owned proof |

The v48 review video is `C:\Users\Leex279\Documents\GitHub\GeneralsGameCode\.tmp\replay-analyzer-review\commented-cast-v48-normal-color-battle-safe-full-match.mp4`; the v47 production manifest is `C:\gra-prod-20260825-status15\test_configured_production_imp0\p\video-runs\2174509c-d38b-4e3d-85a8-b348f97ebdb4\video-manifest-v1.json`.
