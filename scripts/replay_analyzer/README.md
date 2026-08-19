# Generals Replay Analyzer

An isolated Python foundation for analyzing Command & Conquer: Generals Zero
Hour replays. This package will provide deterministic replay-time analysis and
command-line reporting without coupling to the legacy scripts in this folder.

## Development

Run the package checks from the repository root:

```powershell
uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests -q
uv run --project scripts/replay_analyzer ruff check scripts/replay_analyzer/src scripts/replay_analyzer/tests
uv run --project scripts/replay_analyzer mypy scripts/replay_analyzer/src
```
