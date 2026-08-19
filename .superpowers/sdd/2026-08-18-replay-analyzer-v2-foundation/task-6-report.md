# Task 6: replay command-stream parser report

## Result

Implemented a fail-closed Zero Hour replay command parser in
`scripts/replay_analyzer/src/generals_replay_analyzer/commands.py` and
`parser.py`. It decodes the exact `RecorderClass::writeToFile` layout:
unsigned frame, four-byte message number, signed player index, byte-sized type
runs, then their ordered payloads. All eleven engine argument values are
decoded with their serialized widths and retain typed values plus raw bytes.

`parse_replay(path)` returns an immutable `ParsedReplay` containing the parsed
header, complete immutable commands, warnings, the last trustworthy byte
offset, and either `complete` or `truncated` completion status. Exact EOF
after a command is clean; one to three bytes of a next frame and every partial
command form are retained only as a truncation warning. It never scans forward
to resynchronize. Unknown numeric message values remain intact; an unknown
argument type raises `UnsupportedArgumentTypeError` before a payload width is
guessed.

## Source grounding

- `Core/GameEngine/Include/Common/MessageStream.h` declares argument values
  `0` through `10` in order and `ARGUMENTDATATYPE_UNKNOWN` as `11`.
- `GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp` writes the frame,
  message enum, player, type runs, then contiguous argument bytes; its reader
  consumes the runs in the recorded order.
- The parser deliberately preserves `frame` as stored and exposes seconds only
  as `frame / 30.0`.

## TDD evidence

### RED

The newly created command/parser tests were run before either production module
existed:

```powershell
uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests/test_commands.py scripts/replay_analyzer/tests/test_parser.py -q
```

They failed at collection with the expected missing implementation:

```text
ModuleNotFoundError: No module named 'generals_replay_analyzer.commands'
```

### GREEN

After the smallest source-grounded implementation, the focused suite passed:

```text
24 passed in 0.07s
```

The tests construct minimal byte streams and cover all eleven argument types,
zero type runs, repeated type runs, unknown message numbers, unknown argument
types, truncated type runs and payloads, 1--3 trailing next-frame bytes, exact
EOF, offsets, immutable records, and preservation of the last complete command.

## Final verification

```powershell
uv run --project scripts/replay_analyzer ruff check scripts/replay_analyzer/src scripts/replay_analyzer/tests
uv run --project scripts/replay_analyzer mypy --config-file scripts/replay_analyzer/pyproject.toml scripts/replay_analyzer/src
uv run --project scripts/replay_analyzer pytest scripts/replay_analyzer/tests -q
```

```text
All checks passed!
Success: no issues found in 9 source files
90 passed in 0.12s
```
