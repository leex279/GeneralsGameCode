# Replay CRC Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task. Every task uses a test-first RED/GREEN cycle and an independent review before the next implementation task.

**Goal:** Correct CRC queue alignment and checkpoint attribution, then generate enough deterministic diagnostics to localize the genuine retail-versus-modern frame-100 state divergence without masking it.

**Architecture:** Reuse `GameInfo::isMultiPlayer()` as the authoritative recorded-game policy instead of treating a zero local IP as single-player. Keep CRC comparison decisions inside `Recorder`, but record analyzer-only pairing diagnostics after already-authoritative CRC calculations. Use the existing VC6 per-frame/deep-CRC path as the retail-compatible reference; do not change simulation state until the first divergent logged component/line is proven and any required field-level instrumentation is separately reviewed.

**Scope:** Zero Hour first, then the classification and paired-record parity changes in Generals. Analyzer diagnostics are modern Zero Hour only. No gameplay, balance, object-ID remapping, CRC bypass, or speculative GameLogic change.

---

## Task 1: Align and attribute replay CRC checkpoints

**Files:**

- Modify: `GeneralsMD/Code/GameEngine/Source/Common/Recorder.cpp`
- Modify: `GeneralsMD/Code/GameEngine/Include/Common/Recorder.h`
- Modify after Zero Hour verification: `Generals/Code/GameEngine/Source/Common/Recorder.cpp`
- Modify after Zero Hour verification: `Generals/Code/GameEngine/Include/Common/Recorder.h`
- Modify: `scripts/replay_analyzer/tests/test_header.py`
- Modify: `scripts/replay_analyzer/tests/engine/test_cpp_parity.py`
- Modify: `scripts/replay_analyzer/tests/engine/test_combat_outcome.py`
- Modify as proven by post-fix runtime evidence: `scripts/replay_analyzer/tests/engine/test_order_movement.py`
- Modify as proven by post-fix runtime evidence: `scripts/replay_analyzer/tests/engine/test_runner.py`
- Modify as proven by post-fix runtime evidence: `scripts/replay_analyzer/tests/engine/test_map_export.py`
- Modify: `scripts/replay_analyzer/docs/release-status.md`
- Modify: `docs/replay-analyzer/troubleshooting.md`
- Modify: `docs/replay-analyzer/telemetry-verification.md`

**Contract:** A replay containing two human slots is multiplayer even when the recorded local human IP is zero, so its generated frame-0 CRC is skipped. One human plus AI remains skirmish/single-player. `CRCInfo` queues a legacy-compatible `(snapshot_frame, crc)` record and returns that exact frame with the CRC; mismatch reporting never derives a frame from receive-frame queue arithmetic. The pinned replay is expected to remain a real CRC mismatch and end at frame 108, but it must identify the correctly aligned modern frame-100 CRC `0x4D70DE82` versus retail frame-100 CRC `0x582083DA`, reporting checkpoint frame 100 rather than false frame 105/106.

- [ ] Add failing header/source/runtime tests for two humans with local IP zero, nonzero local IP, one-human-plus-AI, atomic `(frame, crc)` FIFO behavior, and direct paired-frame mismatch reporting.
- [ ] Verify RED against both the existing IP-sentinel policy and queue-size-derived `mismatchFrame`.
- [ ] Implement the smallest C++98-compatible pair record and replace the Zero Hour policy with `m_gameInfo.isMultiPlayer()`, using required `TheSuperHackers` comments.
- [ ] Build modern Zero Hour Release/Debug and run the pinned replay gate. Assert final frame 108 remains a truthful CRC failure while the reported paired checkpoint becomes exactly frame 100; do not describe this as replay compatibility.
- [ ] Mirror the pair record and policy to Generals, run source parity tests, and build available Generals targets.
- [ ] Run the VC6 build/replay gate before commit; if the reference toolchain is unavailable, record the exact blocker and do not describe retail compatibility as verified.
- [ ] Update every test fixture and evidence document that encoded frame 105 from the newly observed frame-100 attribution, preserving final frame 108 and failure status unless runtime proves otherwise.
- [ ] Commit `fix(replay): Align CRC checkpoint attribution` and push.

## Task 2: Localize the real frame-100 retail divergence

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

**Contract:** Modern analyzer builds emit a noisy `crc_pair` telemetry-v2 event after authoritative CRC work with computed frame/value, recorded receive frame/value, match flag, queue depth, local player, and monotonic pair index. A separate deterministic reader finds the first differing logged component/line in paired VC6 and modern `SaveDebugCRCPerFrame` output. Diagnostics never affect queue decisions or report strategy claims.

- [ ] Add failing schema/reader tests for closed `crc_pair` records and rejection of malformed/duplicate/out-of-order pairs.
- [ ] Add failing exact-text fixtures for the first-component/first-line diff reader.
- [ ] Implement analyzer-only passive capture behind `RTS_REPLAY_ANALYZER && !IS_VS6_BUILD`; reset with telemetry lifecycle and preserve atomic trace publication.
- [ ] Build modern Zero Hour Release/Debug and verify telemetry-on/off terminal behavior is identical aside from diagnostic records.
- [ ] Select the debug frame window from Task 1's observed paired mismatch rather than assuming frame 100 remains divergent.
- [ ] Build/run `vc6-releaselog` and modern debug with identical replay/runtime assets using `-DebugCRCFromFrame`, `-DebugCRCUntilFrame`, and `-SaveDebugCRCPerFrame`. First compare the authoritative stream without `-CRCLogicModuleData`.
- [ ] Run `-CRCLogicModuleData` only as a separately labelled deeper diagnostic because it adds `TheModuleFactory` to the CRC stream and is not identical to the authoritative replay CRC.
- [ ] Run the diff reader and record the first proven divergent logged component/line. If existing logs do not identify a field, add targeted passive instrumentation in a later reviewed task before claiming a field-level cause. Do not implement a simulation fix in this task.
- [ ] Commit `feat(replay): Add CRC divergence diagnostics` and push.

## Completion gate

- The pinned zero-IP multiplayer replay compares the correct frame-100 pair and reports frame 100 rather than false frame 105/106 attribution.
- The genuine frame-100 CRC mismatch remains a typed failure until a separately proven simulation repair makes the CRC values equal.
- At least one normal retail replay has a paired VC6/modern deep-CRC comparison naming the first divergent logged component/line, or the exact unavailable reference-toolchain blocker is recorded. A field-level claim requires separate targeted evidence.
- No CRC is bypassed, ignored, forced equal, or relabelled as clean completion.
- Protected unrelated user changes remain unstaged.
