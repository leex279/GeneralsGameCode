# Generals Replay Analyzer

<!-- TheSuperHackers @feature Leex 24/08/2026 Document the complete local replay-analysis and broadcast workflow. (#TBD) -->

A local strategy, scouting, and broadcast application for Command & Conquer:
Generals Zero Hour replays. It imports `.rep` files, runs them through the
authoritative game engine, stores structured telemetry, explains build orders,
economy, scouting, engagements, and turning points, and can produce a directed
and narrated in-engine MP4.

The replay parser imports and validates recorded commands. The Zero Hour engine
remains authoritative for simulated state; the analyzer does not reimplement
movement, combat, or economy rules.

## Run the product on Windows

Prerequisites:

- a Release `generalszh.exe` built from this checkout;
- an installed Zero Hour runtime containing the retail game data;
- FFmpeg and FFprobe;
- Python 3.11+ and `uv`.

From `scripts\replay_analyzer`, configure the closed local runtime paths in each
PowerShell terminal. Adapt the two Zero Hour paths to your installation:

```powershell
$env:GENERALS_REPLAY_ANALYZER_ENGINE_EXECUTABLE = "C:\path\to\GeneralsGameCode\build\win32\GeneralsMD\Release\generalszh.exe"
$env:GENERALS_REPLAY_ANALYZER_ENGINE_RUNTIME_DIRECTORY = "C:\Program Files (x86)\Steam\steamapps\common\Command & Conquer Generals - Zero Hour"
$env:GENERALS_REPLAY_ANALYZER_FFMPEG_EXECUTABLE = "C:\Program Files\FFmpeg\bin\ffmpeg.exe"
$env:GENERALS_REPLAY_ANALYZER_FFPROBE_EXECUTABLE = "C:\Program Files\FFmpeg\bin\ffprobe.exe"
```

Start the durable analysis/video worker in the first terminal:

```powershell
uv run --project . replay-analyzer worker
```

Start the loopback-only Web application in the second terminal:

```powershell
uv run --project . replay-analyzer web --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765/replays`, import a replay or replay folder, and open
the generated player report. A completed, full-horizon report exposes the
commented replay-cast action; video status, verified MP4 download, subtitles,
and the immutable artifact manifest remain attached to that job.

Product data is stored outside the repository under the platform-local
`GeneralsReplayAnalyzer` directory by default. Override it with
`GENERALS_REPLAY_ANALYZER_DATA_ROOT` when an isolated installation is desired.

## Inspect or validate one replay

Parser-only inspection does not launch the game:

```powershell
uv run --project . replay-analyzer inspect "C:\path\match.rep" --format json
```

An authoritative telemetry probe can be run independently of the Web app:

```powershell
uv run --project . replay-analyzer export-telemetry "C:\path\match.rep" --engine "C:\path\generalszh.exe"
```

## Resolve replay players against Strata

The standalone resolver does not require the Web server or Replay Analyzer
database. Install its listing browser once:

```powershell
uv run --project . playwright install chromium
```

Inspect replay-local metadata without using the network, return every owner of
an exact historical name, or resolve every human slot through shared Strata
match evidence:

```powershell
uv run --project . strata-resolver inspect-replay "C:\path\match.rep" --pretty
uv run --project . strata-resolver resolve-name fish --pretty
uv run --project . strata-resolver resolve-replay "C:\path\match.rep" --pretty
```

The JSON keeps exact alternatives, case-insensitive suggestions, replay-local
indices, and external Strata IDs separate. Name-only ambiguity is never hidden.
Exit codes are 0 for resolved/success, 2 for invalid input, 3 for
ambiguous/not-found, 4 for incomplete source acquisition, and 5 for local
runtime failure. Diagnostics go to stderr.

Cached profiles, aliases, match pages, and immutable resolution audit records
live in the platform data directory by default. Use `--cache <absolute-path>`
for an isolated database, `--offline` for cache-only resolution, `cache status`
or `cache purge`, and `doctor` to verify cache, Chromium, and HTTPS access.
Source requests use bounded pagination, two HTTP workers, a 500 ms host interval,
three attempts, allowlisted HTTPS URLs, and a 20 MiB replay-download limit.

## Development

Run the package checks from the repository root:

```powershell
uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests -q
uv run --project scripts/replay_analyzer ruff check scripts/replay_analyzer/src scripts/replay_analyzer/tests
uv run --project scripts/replay_analyzer mypy scripts/replay_analyzer/src
```
