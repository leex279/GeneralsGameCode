# Replay Analyzer troubleshooting

Start with `http://127.0.0.1:8765/health/ready`, then check Settings diagnostics and Jobs. Preserve the data root and logs when reporting a problem.

## Python or uv is missing

Use Python 3.11 or 3.12 and install `uv`. Confirm `uv --version`, then run `uv sync --project scripts/replay_analyzer`. PowerShell execution-policy errors from shim scripts can usually be avoided by invoking the `.cmd` executable where applicable.

## Engine executable is missing

Build Zero Hour first:

```powershell
cmake --preset win32
cmake --build build/win32 --target z_generals --config Release
```

Set `GENERALS_REPLAY_ANALYZER_ENGINE_EXECUTABLE` to the absolute `generalszh.exe` path and restart both worker and web processes. The engine still needs the legally installed Zero Hour runtime data. Telemetry is unavailable without it, but replay-byte inspection remains available.

## Replay version or parser mismatch

The initial trusted target is Zero Hour 1.04. Verify the file is an unmodified `.rep`, inspect it with the CLI, and review parser warnings. Do not rename a different binary format to `.rep` or suppress unsupported-version diagnostics.

## CRC mismatch

A CRC mismatch is a replay-simulation boundary, not a telemetry success. The exporter records the mismatch frame and stops according to engine behavior. Reports may use evidence observed before that point only when labelled partial or deterministic-only.

The pinned `leex279` versus `FOX27` fixture currently reaches a genuine modern-versus-retail CRC mismatch for snapshot frame 100 and stops at playback frame 108. This is a known release blocker for authoritative full-match playback; do not describe its natural short trace as complete. Compare telemetry-off and telemetry-on outcome/CRC results before attributing a mismatch to the analyzer.

## Partial or truncated trace

Open the job and evidence diagnostics. Check final frame, command count, completion record, clean-shutdown flag, writer error, trace hash, and replay truncation flag. Keep available pre-boundary facts labelled partial. Never repair the trace by scanning for plausible records or merging another run.

## SQLite is locked or readiness fails

Run one web coordinator for migrations and use separate workers. Stop stale analyzer processes, then retry. Do not delete SQLite journal/WAL files by hand. If readiness reports integrity failure, preserve the database, use the automatic pre-migration backup, and run the Settings SQLite diagnostic before restoring.

## Ollama is unavailable

Deterministic analysis does not require Ollama. Confirm the configured loopback URL and exact local model in Settings. A stopped service or absent model should produce `unavailable`, not fail the report. Restart the worker after changing environment-owned settings.

## Ollama output is invalid

The analyzer rejects prose or JSON that does not match its schema, evidence citations, or model identity. Inspect the job error and keep the deterministic report. Do not paste unvalidated text into the database or relabel it as derived evidence.

## Map or spatial analysis is absent

Spatial analysis requires a validated engine map export, compatible engine-data identity, real bounds/navigation resources, usable player start positions for player-relative views, and movement/entity telemetry. Check the map asset references in the telemetry completion record. The UI intentionally renders an unavailable state instead of procedural terrain or invented paths.

## Import does not appear

For folder watching, use an absolute configured root and wait for two stable scans. Confirm the source is readable, has a `.rep` extension, is within the selected root, and is below the upload limit. Duplicate bytes resolve to the existing replay and add provenance; they do not create a second library row.

## A job remains pending

The web process does not execute jobs. Start `replay-analyzer worker`, inspect the job’s dependencies and lease, and retry only when the UI marks it eligible. A running engine or model child is owned by the claiming worker; cancellation is cooperative and must settle that child before the job becomes cancelled.

## Player names or comparisons look wrong

Verify embedded names on the replay detail page and keep filename/Strata values in Provenance. Review identity audit history before merging or splitting aliases. Comparisons require aligned feature definitions, versions, units, factions/segments, quality filters, and sufficient samples; otherwise `not comparable` is correct.

## Safe diagnostic bundle

When escalating a defect, include analyzer version, schema revision, engine build identity, replay SHA-256, job public ID, report public ID, reason codes, and redacted logs. Do not publish replay files, local filesystem paths, provider tokens, or the complete SQLite database without explicit permission.
