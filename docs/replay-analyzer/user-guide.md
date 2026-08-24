# Replay Analyzer V2 user guide

Replay Analyzer V2 is a local-first Zero Hour replay library and evidence-backed analysis application. The web server never starts analysis work in-process: run the web server for reading and a separate worker for queued imports and analysis.

## Current scope

- Zero Hour 1.04 is the first supported target.
- Embedded replay names are player evidence. Strata match IDs and filename user tokens are provenance only.
- Authoritative mechanics, map, and spatial analysis require the modern Zero Hour engine exporter.
- Deterministic analysis remains usable when Ollama is stopped. Ollama prose is optional inferred evidence.
- Missing or partial inputs stay unavailable or partial; the analyzer does not reconstruct synthetic facts.

## Install and build

Python 3.11 or 3.12, `uv`, CMake 3.25+, Visual Studio 2022 with x86 C++ tools, and a legal Zero Hour installation are required for the complete workflow.

From the repository root:

```powershell
cmake --preset win32
cmake --build build/win32 --target z_generals --config Release
uv sync --project scripts/replay_analyzer
```

Keep the instrumented development executable in the build output and set the legal Zero Hour installation as its runtime directory. For every telemetry or native-video run, the analyzer creates one random, exclusive hardlink to that build beside the installed runtime modules, verifies it is the same ordinary file, starts it without a shell, settles the child process tree, and deletes only that unchanged owned link. It never replaces, renames, or modifies the retail executable:

```powershell
$env:GENERALS_REPLAY_ANALYZER_ENGINE_EXECUTABLE = (Resolve-Path 'build/win32/GeneralsMD/Release/generalszh.exe').Path
$env:GENERALS_REPLAY_ANALYZER_ENGINE_RUNTIME_DIRECTORY = (Resolve-Path 'C:\Program Files (x86)\Steam\steamapps\common\Command & Conquer Generals - Zero Hour').Path
$env:GENERALS_REPLAY_ANALYZER_DATA_ROOT = "$env:LOCALAPPDATA\GeneralsReplayAnalyzer"
```

If the executable was deliberately deployed into the installed game directory, `ENGINE_RUNTIME_DIRECTORY` may be omitted and the executable's parent is used. A bare build folder is not a complete Zero Hour runtime.

Do not put the data root inside a Git checkout. By default it contains the SQLite database, managed replay copies, immutable artifacts, map assets, logs, engine runs, and migration backups.

## Inspect, import, and analyze

Inspect replay bytes without launching the engine:

```powershell
uv run --project scripts/replay_analyzer replay-analyzer inspect path\match.rep --format json
```

Import one replay or a folder snapshot:

```powershell
uv run --project scripts/replay_analyzer replay-analyzer import path\match.rep --json
uv run --project scripts/replay_analyzer replay-analyzer import path\replays --recursive --json
```

Imports are content-addressed. Reimporting identical bytes adds source provenance without creating a second replay. Copy mode is the safe default; reference mode keeps the external source dependency.

Run the worker in one terminal and the local web app in another:

```powershell
uv run --project scripts/replay_analyzer replay-analyzer worker --poll-seconds 1 --lease-seconds 120
uv run --project scripts/replay_analyzer replay-analyzer web --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`. The loopback-only default is intentional. The server exposes Library, Reports, Maps, Players, Compare, Jobs, Settings, and immutable evidence pages.

To plan or execute deterministic analysis from the CLI:

```powershell
uv run --project scripts/replay_analyzer replay-analyzer analyze REPLAY_PUBLIC_ID --json
uv run --project scripts/replay_analyzer replay-analyzer analyze REPLAY_PUBLIC_ID --execute --json
```

Add `--allow-ollama` only when optional local-model interpretation is wanted.

## Watched folders and settings

Configure watched folders in Settings or set `GENERALS_REPLAY_ANALYZER_WATCHED_FOLDERS` to a JSON array of absolute paths before process startup. The watcher waits for stable size and modified time, never deletes source files, and records discovery failures as jobs.

The Settings page can safely change import mode, movement sample interval, minimum longitudinal sample size, Ollama URL, and Ollama model. Environment-owned settings are read-only in the UI. Changes that alter analysis identity show their impact before confirmation and apply to future runs.

## Ollama

The default endpoint is `http://127.0.0.1:11434` and the default model is `qwen3.6:27b`. Configure alternatives with Settings or:

```powershell
$env:GENERALS_REPLAY_ANALYZER_OLLAMA_URL = 'http://127.0.0.1:11434'
$env:GENERALS_REPLAY_ANALYZER_OLLAMA_MODEL = 'qwen3.6:27b'
```

Ollama output must pass the closed JSON schema and cite accepted evidence. A stopped server, missing model, timeout, or invalid response records an unavailable inferred stage; it does not invalidate deterministic results.

## Player identity

Exact normalized embedded names auto-link. Use a player’s Identity page to preview a merge or split, review affected replays/features/reports, enter a human reason, and confirm. Operations are revision-guarded, audited, reversible through the inverse operation, and queue invalidated downstream work. Provider identities and Strata provenance remain separately labelled.

## Reports and versions

Reports are immutable. New parser, telemetry, feature, rule, identity, model, or report versions create new runs; old evidence remains addressable. “Latest” URLs resolve to a fixed report URL before rendering. Use Jobs to inspect failures or retry eligible work.

## Backup and removal

Stop the worker and web process before making a manual backup. Copy the complete data root and the external configuration directory `%LOCALAPPDATA%\GeneralsReplayAnalyzer`; preserve file timestamps and permissions. Automatic pre-migration database backups live below the data root.

Uninstalling the Python environment or deleting a checkout does not delete analyzer data. Remove the external data/configuration directories only after the user explicitly chooses to discard the replay library and evidence history.
