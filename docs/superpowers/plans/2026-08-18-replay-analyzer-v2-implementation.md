# Replay Analyzer V2 Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved Replay Analyzer V2 as a trustworthy local pipeline from `.rep` files through engine observations, SQLite-backed analysis, Ollama interpretation, and a polished web application.

**Architecture:** Build four sequential layers. The strict Python parser is first verified against the engine's C++ `RecorderClass`; passive engine telemetry and map export then provide authoritative state; SQLite and deterministic feature extractors turn observations into evidence; Ollama and the local FastAPI application consume only that evidence.

**Tech Stack:** C++98-compatible engine seams with modern-only C++20 exporters, Python 3.11, uv, pytest, Pydantic 2, SQLAlchemy 2, Alembic, SQLite, FastAPI, Jinja, HTMX, ECharts, Ollama HTTP API.

**Spec:** `docs/superpowers/specs/2026-08-18-replay-analyzer-v2-design.md`

## Global Constraints

- Implement and verify Zero Hour first. Do not backport analyzer code to Generals until the Zero Hour acceptance gates pass.
- Never let parser diagnostics or telemetry feed data back into GameLogic or alter command execution.
- Compile modern-only exporters out of VC6 builds with a CMake-provided `RTS_REPLAY_ANALYZER` definition.
- Preserve every numeric message type and raw observed value; symbolic names and analysis labels are additional metadata.
- Label every persisted claim as `observed`, `derived`, or `inferred` and retain evidence references.
- Do not present procedural maps, simulated units, guessed outcomes, or LLM text as replay facts.
- Keep runtime databases, managed replays, traces, reports, and model caches under `%LOCALAPPDATA%\GeneralsReplayAnalyzer\` by default, never in Git.
- Add the required `TheSuperHackers @keyword Leex 18/08/2026 ...` comment to every user-facing or architectural C++ change.
- Preserve the dirty worktree. Stage only files named by the active task and review `git diff --cached` before every commit.

---

## Delivery Sequence

1. [Foundation and parser parity](2026-08-18-replay-analyzer-v2-foundation.md)
2. [Engine telemetry and spatial truth](2026-08-18-replay-analyzer-v2-engine-telemetry.md)
3. [SQLite, deterministic analytics, and Ollama](2026-08-18-replay-analyzer-v2-analytics.md)
4. [Local web application and operational workflow](2026-08-18-replay-analyzer-v2-web.md)

Each plan has its own acceptance gate. Do not start a later plan while an earlier gate is failing.

## Release Gate

- [ ] Execute all four plan acceptance gates in order.
- [ ] Run `cmake --build build/win32 --target z_generals --config Release`.
- [ ] Run `cmake --build build/vc6` and confirm the analyzer exporter is absent from that build.
- [ ] Run the retail Zero Hour replay suite once with telemetry disabled and once with telemetry enabled; compare exit codes and CRC results.
- [ ] Run `uv run --project scripts/replay_analyzer pytest` with the engine integration marker enabled.
- [ ] Import the checksum-pinned `leex279` versus `FOX27` fixture and confirm the UI never shows Strata `3133811` or its filename user token as a player identity.
- [ ] Confirm all spatial and strategy panels display evidence tier, evidence links, analyzer version, and confidence where applicable.
- [ ] Confirm the application remains useful when Ollama is stopped: deterministic analysis succeeds and the UI reports the optional narrative stage as unavailable.
- [ ] Commit with `feat(replay): Add evidence-backed replay analyzer` only after all gates pass.
