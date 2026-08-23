# Replay Analyzer strategy-first release status

<!-- TheSuperHackers @info Leex 23/08/2026 Record reproducible product evidence and explicit verification limits. (#TBD) -->

Date: 2026-08-23  
Tested product head: `40dd1e7df` on `feat/replay-analyzer-v2-product`  
Base branch: `feat/replay-analyzer-v2` at `9387086be`

## Player-visible outcome

The installed web application opens the most useful player-specific report directly from the replay library. The pinned replay proof shows an honest opening-only analysis through frame 105 (3.5 seconds at 30 FPS): two identified players, 600 observed supplies, one observed Crusader, no invented winner, and no named strategy when the evidence is insufficient. Raw frame rows and provenance remain available behind disclosures.

Player profiles no longer present zero-sample placeholders as established tendencies. Timeline controls are compact, the duplicate series legend is removed, and desktop/mobile reports retain the strategy summary above technical evidence.

## Exact verification

From `scripts/replay_analyzer`:

```powershell
uv run --no-sync --project . pytest tests --ignore=tests\browser --ignore=tests\watching\test_ingress_contract.py -q
```

Result: `3191 passed, 16 skipped in 1087.75s`.

The excluded `tests/watching/test_ingress_contract.py` is a separate untracked user file and was neither edited nor committed by this branch.

```powershell
uv run --no-sync --project . ruff check src tests --exclude tests\watching\test_ingress_contract.py
uv run --no-sync --project . mypy --strict src\generals_replay_analyzer
uv run --no-sync --project . pytest tests\browser -q
```

Results:

- Ruff: `All checks passed!`
- Mypy: `Success: no issues found in 161 source files`
- Installed-wheel browser matrix: `139 passed in 137.45s`

The browser matrix covers packaged-wheel isolation, same-origin/offline behavior, keyboard workflows, Axe scans, desktop/tablet/mobile reflow, populated reports, maps, player profiles, comparisons, and the pinned one-replay journey.

## Frozen wheel and retained artifacts

- Application wheel SHA-256: `7ac55932f1253a6cdd0c078d53c508c5b121b61badfc86445aacb371076ca558`
- Installed module class: isolated wheel environment
- Retained artifact folder: `replay-analyzer-artifacts/2026-08-23-task9-7ac55932f125/`
- Run manifest: `run-manifest.json`
- Primary proof: `strategy-first-one-replay--desktop.png`
  - SHA-256: `6060de838532d4da1e774794118a2696755d513f9400c230a602a7e7618eea28`
- Mobile report: `report--mobile.png`
  - SHA-256: `d733b934147ae4f632effbfe8ed0ac25d0689e360e4e3354be63c3d9c13b3c3c`
- Honest one-match profile: `player-profile--desktop.png`
  - SHA-256: `bd0a73d33cac216d92ba41705fd8157815bb5f77520e55b30d4ff3b71320b075`

The artifact folder is intentionally ignored by Git; the manifest records SHA-256 and byte size for all 38 release artifacts.

## Launch

```powershell
cd scripts\replay_analyzer
uv run --no-sync --project . replay-analyzer web --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765/replays`. New replay analysis uses the watched-folder import journey and an external worker process.

## Explicit limits

- The pinned replay proof is partial at the frame-105 CRC mismatch. It is not a full-match result and does not establish midgame, late-game, winner, or recurring player tendencies.
- Ollama is unavailable in this environment. The deterministic evidence-backed coaching projection works without it; live LLM commentary was not verified.
- CMake 4.2.1 and Visual Studio Community are installed, but Win32/VC6/MinGW engine builds and replay non-interference were not rerun for this product checkpoint. The worktree contains five unrelated protected engine edits, so compiling them would not prove the isolated web-product change.
- `cl`, `gcc`, `g++`, `mingw32-make`, and `msdev` were not available on the current shell PATH. VC6 and MinGW retail/toolchain validation remain unverified here.

