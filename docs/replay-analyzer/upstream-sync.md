# Replay-analyzer upstream compatibility sync

<!-- TheSuperHackers @info Leex 24/08/2026 Document the isolated, fetch-only compatibility reference; this is not an integration workflow. (#TBD) -->

## Pinned reference

`upstream-sync-manifest-v1.json` pins the recorder reference to
`https://github.com/GeneralsOnlineDevelopmentTeam/GameClient.git` at commit
`b7cfeaf08e53044240c8674ea1960738d49776b7`. The manifest records the recorder
executable provenance and purpose, the exact replay SHA-256, the expected
frame-100 CRC (`0x582083DA`), and the comparison scope prefixes.

The recorder identity is closed: `GeneralsOnlineZH_60.exe`, SHA-256
`15619eba088abd6a24e790d8203b95f84925fcf0ea04c7b8203a29c9fdfe5ca6`, executable
CRC `0x48D67663`, build time `Jun 20 2026 23:29:03`, and simulation profile
`GENERALS_ONLINE_HIGH_FPS_SERVER`. The tested non-causal profile is recorded as
`GENERALS_ONLINE_IBRA_STARTING_POS_LOGIC` for explicit provenance, not as an
unreviewed compatibility switch.

`comparison_scope_prefixes` is deliberately over-inclusive review scope. It
does not label every file in these directories as causal, and it must be
narrowed only after a proven component-level CRC bisection. The checker rejects
omissions, reordering, and extra prefixes so a future narrowing is explicit and
reviewed. It never auto-merges or infers causality from the scope.

The exact replay gate is:

- replay SHA-256: `EA085767BFA11D2CFC167D9007173CE2EB29B5F557702FFD042E2E9A1A8F6BB8`
- frame 100 CRC: `0x582083DA`

These values are compatibility evidence, not permission to mask a mismatch.

## Current remotes and proposed optional remote

This checkout currently has:

- `origin`: `https://github.com/leex279/GeneralsGameCode.git` (fork)
- `upstream`: `https://github.com/TheSuperHackers/GeneralsGameCode.git` (project upstream)

If a maintainer explicitly needs to inspect the pinned recorder source, the
dedicated optional remote name is `generals-online`, pointing at the pinned
repository. It is fetch-only by policy; adding it is an operator action and is
not performed by the checker or this documentation change.

## Integration boundary

The integration branch is exactly `integration/upstream-compat`.
`upstream` (TheSuperHackers/GeneralsGameCode) remains the primary project
upstream. `generals-online` is compatibility-only and must never be wholesale
merged into the primary upstream or product branch.
Fetching a commit does not authorize a wholesale merge, branch tracking, force
operation, or automatic update. Any comparison or port must be narrow, reviewed,
and replay-gated with Zero Hour first. No push to `generals-online` or any other
upstream is permitted by this guard.

`scripts/check_replay_analyzer_upstream_sync.py` only validates the closed
manifest and prints a deterministic JSON plan. It never performs network I/O,
adds a remote, fetches, merges, writes source files, or pushes. An explicit
`--sync` request checks the clean-worktree prerequisite before an operator-led
fetch; ordinary plan generation does not require a clean tree.

Example (no network or mutation):

```powershell
python scripts/check_replay_analyzer_upstream_sync.py `
  --manifest docs/replay-analyzer/upstream-sync-manifest-v1.json --plan
```
