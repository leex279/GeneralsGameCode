# Replay CRC Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task. Every task uses a test-first RED/GREEN cycle and an independent review before the next implementation task.

**Goal:** Remove the proven false early replay CRC mismatch, report the actual paired checkpoint frame, and generate enough deterministic diagnostics to localize the real retail-versus-modern frame-100 state divergence without masking it.

**Architecture:** Reuse `GameInfo::isMultiPlayer()` as the authoritative recorded-game policy instead of treating a zero local IP as single-player. Keep CRC comparison decisions inside `Recorder`, but record analyzer-only pairing diagnostics after already-authoritative CRC calculations. Use the existing VC6 per-frame/deep-CRC path as the retail-compatible reference; do not change simulation state until a first divergent component and field are proven.

**Scope:** Zero Hour first, then the one-line classification parity change in Generals. Analyzer diagnostics are modern Zero Hour only. No gameplay, balance, object-ID remapping, CRC bypass, or speculative GameLogic change.

---

## Task 1: Correct replay CRC warmup classification

**Files:**

- Modify: `GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp`
- Modify after Zero Hour verification: `Generals/Code/GameEngine/Source/Common/Recorder.cpp`
- Modify: `scripts/replay_analyzer/tests/test_header.py`
- Modify: `scripts/replay_analyzer/tests/engine/test_cpp_parity.py`
- Modify: `scripts/replay_analyzer/tests/engine/test_combat_outcome.py`
- Modify as proven by post-fix runtime evidence: `scripts/replay_analyzer/tests/engine/test_order_movement.py`
- Modify as proven by post-fix runtime evidence: `scripts/replay_analyzer/tests/engine/test_runner.py`
- Modify as proven by post-fix runtime evidence: `scripts/replay_analyzer/tests/engine/test_map_export.py`
- Modify: `scripts/replay_analyzer/docs/release-status.md`
- Modify: `docs/replay-analyzer/troubleshooting.md`
- Modify: `docs/replay-analyzer/telemetry-verification.md`

**Contract:** A replay containing two human slots is multiplayer even when the recorded local human IP is zero. One human plus AI remains skirmish/single-player for CRC warmup. The pinned replay must advance beyond the false frame-105 boundary, but this task must not claim clean EOF or hide a later real mismatch.

- [ ] Add failing header/source/runtime tests for two humans with local IP zero, nonzero local IP, and one-human-plus-AI.
- [ ] Verify RED against the existing IP-sentinel policy.
- [ ] Replace the Zero Hour policy with `m_gameInfo.isMultiPlayer()` and add the required `TheSuperHackers @bugfix` comment.
- [ ] Run parser/source tests, build modern Zero Hour Release, and run the pinned replay gate. Assert only that the old frame-105/108 boundary is gone.
- [ ] Mirror the one-line policy and comment to Generals, run source parity tests, and build available Generals targets.
- [ ] Run the VC6 build/replay gate before commit; if the reference toolchain is unavailable, record the exact blocker and do not describe retail compatibility as verified.
- [ ] Update every test fixture and evidence document that encoded frame 105/108 from the newly observed runtime result; never mechanically bless a later mismatch as success.
- [ ] Commit `bugfix(replay): Align CRC warmup with human slots` and push.

## Task 2: Attribute each replay CRC to its computed snapshot frame

**Files:**

- Modify: `GeneralsMD/Code/GameEngine/Include/Common/Recorder.h`
- Modify: `GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp`
- Modify: `Generals/Code/GameEngine/Include/Common/Recorder.h`
- Modify: `Generals/Code/GameEngine/Source/Common/Recorder.cpp`
- Modify: `scripts/replay_analyzer/tests/engine/test_cpp_parity.py`
- Modify: `scripts/replay_analyzer/tests/engine/test_combat_outcome.py`

**Contract:** `CRCInfo` queues a legacy-compatible value/frame pair, skips the initial multiplayer entry as one atomic record, and returns the actual computed frame alongside the CRC. Mismatch reporting uses that paired frame instead of deriving a frame from current queue depth. The queue remains deterministic and contains no clock, UI, telemetry, or heap-order dependency.

- [ ] Add failing source-contract tests for atomic value/frame pairing, skip behavior, FIFO order, and direct paired-frame mismatch reporting.
- [ ] Create a deterministic CRC-corrupted replay derivative for the runtime assertion so mismatch reporting is exercised even if the natural replay reaches clean EOF; never rely on an expected natural mismatch.
- [ ] Add a runtime assertion that outcome/telemetry reports the actual paired checkpoint frame for that corrupted derivative.
- [ ] Implement the smallest C++98-compatible pair record in both Zero Hour and Generals, with Zero Hour verified first.
- [ ] Build modern Release/Debug and available VC6 targets; run replay determinism tests.
- [ ] Commit `fix(replay): Report paired CRC checkpoint frames` and push.

## Task 3: Localize the real frame-100 retail divergence

**Files:**

- Create: `GeneralsMD/Code/GameEngine/Include/Common/ReplayCRCDiagnostics.h`
- Create: `GeneralsMD/Code/GameEngine/Source/Common/ReplayCRCDiagnostics.cpp`
- Modify: `GeneralsMD/Code/GameEngine/CMakeLists.txt`
- Modify: `GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp`
- Modify if lifecycle ownership requires it: `GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp`
- Modify: `scripts/replay_analyzer/contracts/telemetry-v2.schema.json`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/telemetry/model.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/telemetry/reader.py`
- Modify: `scripts/replay_analyzer/tests/telemetry/test_telemetry_v2_contract.py`
- Modify: `scripts/replay_analyzer/tests/engine/test_combat_outcome.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/engine/crc_diff.py`
- Create: `scripts/replay_analyzer/tests/engine/test_crc_diff.py`

**Contract:** Modern analyzer builds emit a noisy `crc_pair` telemetry-v2 event after authoritative CRC work with computed frame/value, recorded receive frame/value, match flag, queue depth, local player, and monotonic pair index. A separate deterministic reader finds the first differing component/object/field in paired VC6 and modern `SaveDebugCRCPerFrame` output. Diagnostics never affect queue decisions or report strategy claims.

- [ ] Add failing schema/reader tests for closed `crc_pair` records and rejection of malformed/duplicate/out-of-order pairs.
- [ ] Add failing exact-text fixtures for the first-component/first-line diff reader.
- [ ] Implement analyzer-only passive capture behind `RTS_REPLAY_ANALYZER && !IS_VS6_BUILD`; reset with telemetry lifecycle and preserve atomic trace publication.
- [ ] Build modern Zero Hour Release/Debug and verify telemetry-on/off terminal behavior is identical aside from diagnostic records.
- [ ] Select the debug frame window from Task 2's observed paired mismatch rather than assuming frame 100 remains divergent.
- [ ] Build/run `vc6-releaselog` and modern debug with identical replay/runtime assets using `-DebugCRCFromFrame`, `-DebugCRCUntilFrame`, and `-SaveDebugCRCPerFrame`. First compare the authoritative stream without `-CRCLogicModuleData`.
- [ ] Run `-CRCLogicModuleData` only as a separately labelled deeper diagnostic because it adds `TheModuleFactory` to the CRC stream and is not identical to the authoritative replay CRC.
- [ ] Run the diff reader and record the first proven divergent logged component/line. If existing logs do not identify a field, add targeted passive instrumentation in a later reviewed task before claiming a field-level cause. Do not implement a simulation fix in this task.
- [ ] Commit `feat(replay): Add CRC divergence diagnostics` and push.

## Completion gate

- The pinned zero-IP multiplayer replay no longer produces the false frame-105 mismatch.
- Any remaining mismatch names its actual paired snapshot frame.
- At least one normal retail replay has a paired VC6/modern deep-CRC comparison naming the first divergent logged component/line, or the exact unavailable reference-toolchain blocker is recorded. A field-level claim requires separate targeted evidence.
- No CRC is bypassed, ignored, forced equal, or relabelled as clean completion.
- Protected unrelated user changes remain unstaged.
