# Replay telemetry determinism and non-interference verification

## Status

The pinned authoritative replay gate and the repository-wide ten-replay gate are verified. The analyzer-only
`-replay-user-data-root` option redirects every early user-data consumer into a validated, test-owned root, so the two
checked-in custom maps are staged without writing to the real profile. The test snapshots the registry-derived user-data
root recursively and cryptographically in `finally`; no real map, replay, registry value, or profile content changed.

This evidence was produced from engine commit
`8770634d8b736bcbd9a658663a0d09e95d275e60` with the `GeneralsReplays` submodule pinned at
`0711bf9d1b60aeaaad98a52edd9b7b1c16be4937`. The requested movement sampling interval was 15 logic frames. The
verified Release executable SHA-256 was
`44e6bf135869747ea4c01e794790968f68c12f3acf5d9f45cf1e652caad1d911`. Every engine invocation was sequential, used
a unique short destination, and omitted `-jobs`.

## Commands

Commands were run from `scripts/replay_analyzer` unless noted otherwise:

```powershell
uv run --cache-dir .tmp\task10-uv-cache --project . pytest tests/engine/test_telemetry_determinism.py -q -k "normalization or stdout"
uv run --cache-dir .tmp\task10-uv-cache --project . pytest tests/engine/test_telemetry_determinism.py::test_pinned_replay_three_runs_are_deterministic_and_non_interfering -q -s
uv run --project . pytest tests/engine/test_telemetry_determinism.py::test_all_retail_replays_match_with_telemetry_disabled_and_enabled -q
uv run --cache-dir .tmp\task10-uv-cache --project . ruff check src tests
uv run --cache-dir .tmp\task10-uv-cache --project . mypy --strict src
uv run --cache-dir .tmp\task10-uv-cache --project . pytest -m "not engine" -q
uv run --cache-dir .tmp\task10-uv-cache --project . pytest -m engine -q -k "not test_all_retail_replays_match_with_telemetry_disabled_and_enabled"
# Eventual unrestricted engine gate (no corpus deselection):
uv run --cache-dir .tmp\task10-uv-cache --project . pytest -m engine -q
```

Modern builds are run from the repository root:

```powershell
cmd.exe /d /c "call ""C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat"" x86 >nul && cmake --build build\win32 --target z_generals --config Release"
cmd.exe /d /c "call ""C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat"" x86 >nul && cmake --build build\win32 --target z_generals --config Debug"
```

The enabled path uses the public `export_telemetry` runner. The disabled path uses Task 9's public
`ProcessLaunchRequest` and `default_process_launcher` with this explicit no-shell argument vector:

```text
generalszh.exe -headless -noaudio -replay <absolute-replay> -replay-outcome <unique-outcome> -replay-user-data-root <absolute-existing-root>
```

It intentionally has no `-telemetry` and no `-jobs`. Enabled launches use the same isolated-root option. The modern
analyzer defers the legacy constructor's default-root creation, validates exactly one non-wildcard replay and a safe
absolute existing ANSI path during startup parsing, selects it before filesystem/MapCache discovery, and only then
creates the selected directory. Non-analyzer and VC6 behavior remains registry-derived. Identity-marked short output roots are removed after each test,
and cleanup refuses any root whose resolved parent, generated name, or marker identity changed. Enabled and disabled
results are compared through independently
published `ReplayOutcome` terminal facts: `playback_started`, final frame, executed command count, terminal reason,
CRC mismatch state, and mismatch frame. `match_outcome` is recorded separately from enabled telemetry and is never
invented for disabled runs.

## Normalization contract

The test normalizer operates on raw NDJSON bytes. It does not parse and reserialize records. It may replace only:

- the JSON value of a `run_id` field;
- an explicitly supplied run-local `path` field;
- an explicitly supplied wall-clock `created_at` field; and
- the terminal `trace_sha256`, recomputed from the already-normalized pre-completion bytes.

It preserves record and field ordering, sequence, frame, event values, whitespace, numeric spelling, and floating-point
text exactly. Mutation tests prove changes to sequence, frame, field ordering, event values, numeric spelling, and the
last digit of a floating-point spelling remain detectable. Standard output removes only complete lines matching the
known engine diagnostic `Elapsed Time: MM:SS Game Time: MM:SS/MM:SS`; prefix/suffix variants and every other stdout
byte remain compared. Stderr is compared exactly.

## Pinned replay: three disabled and three enabled runs

Fixture: `scripts/replay_analyzer/tests/fixtures/zero_hour_1_04/leex279_vs_fox27.rep`

- Replay SHA-256: `ea085767bfa11d2cfc167d9007173ce2eb29b5f557702ffd042e2e9a1a8f6bb8`
- All six runs: exit code `1`, playback started, final frame `108`, command count `16`, terminal reason
  `crc_mismatch`, mismatch frame `105`.
- All three normalized enabled traces:
  `4219ea4b378dce32b21ed3694ebf09f75201e6838f27aff674eeef626fe54da8`.
- Enabled versus disabled deterministic stdout: byte-identical after removing only `Elapsed Time` lines.
- Enabled versus disabled stderr: byte-identical without normalization.
- Replay and executable SHA-256: unchanged before/after all runs, including failure paths.
- User `Maps` and `Replays` inventories, including entry kind, byte size, modification time, and regular-file SHA-256: unchanged
  before/after all runs, including failure paths.
- Manifest and completion records: accepted by the strict v2 reader.
- Evidence IDs: exact sequence `0..N-1` in every enabled trace.
- Replay player names: exact `leex279`, `FOX27` in every enabled trace.
- Writer error: `null` in every completion.

The enabled `match_outcome` is kept separate: status `unknown`, source `unavailable`, engine player domain
`[0,1,2,3,4]`, empty winner and loser lists, terminal reason `crc_mismatch`, and mismatch frame `105`. The historical
single-winner fields are `null`. Disabled winner/loser facts are therefore unknown, not inferred.

### Pinned map asset identity

- Content SHA-256: `fbbb36b202a76f35c90228c537d76ba2f15f985eae092ada798b827c7bcc1ffa`
- Exact manifest SHA-256: `4c1e3e84e2f246c80727c1372cbab698e556c54bf5937442553d24f140491eae`

All six emitted file hashes were identical in the three enabled runs:

| File | SHA-256 |
|---|---|
| `height.f32.zlib` | `3f2edd7a13e5bcf1e5746c6424ed6f4174dc615ba6252f19f9a476acee9e9645` |
| `manifest.json` | `4c1e3e84e2f246c80727c1372cbab698e556c54bf5937442553d24f140491eae` |
| `pathing-amphibious.u8.zlib` | `fea4b10015030f0fd7b21aa0d1ca0e3a4f7bdb76f99586336af26d70a107d6af` |
| `pathing-ground.u8.zlib` | `34d8c296e88744dea0aeae2a28b48a50cb1953ce9cc98e94e1948a4d98ebb2f1` |
| `terrain.u8.zlib` | `58b84ba400e5aa883d71cc119f725d6f11cccc95e7f3d2e8edda8ee2bf1ed958` |
| `zones.i32.zlib` | `7aff66599548d07db7e57c40163449190871eea9fa31cf0196e99ad48b404955` |

## Repository Zero Hour 1.04 corpus

The test validates the exact `(filename, size, SHA-256)` tuples before launch, then runs each replay disabled and enabled
in order with unique destinations. `D/E` is the shared disabled/enabled terminal tuple
`exit, playback_started, final_frame, command_count, terminal_reason, mismatch_frame`.

| # | Replay | Size | Replay SHA-256 | D/E terminal | Normalized trace SHA-256 | Map content SHA-256 | Exact manifest SHA-256 |
|---:|---|---:|---|---|---|---|---|
| 1 | `!Golden Replay #1.rep` | 1,294,958 | `cf56e1081eff70e6cfad972f5b52b096e4540cbc0c144a1becd03820d89b4d8c` | `1,true,113,98,crc_mismatch,111` | `980ede483b17a20ca4192ef497c40c160ef4077355d0b717c34be1abd255891c` | `c2020f3f1be50672fcac4dda1f92c5204a79fa3db44373d1cefe010bbcc6ddc8` | `284fa6ee7489c1b89eae169a1f03e092a98f2363f529cffa2e332d61af7e2902` |
| 2 | `00-03-45_2v6_PC03_ss_HardAI_HardAI_HardAI_HardAI_HardAI_HardAI.rep` | 53,996 | `7484beb03e790f3915ca45e8a302bfebd093edad53776bd64cdd3a707688fe9c` | `1,true,112,32,crc_mismatch,110` | `93f3e86f7ab18abba8f01621bfaf0c4e81800eb38bb7b1609511b5454b5557af` | `e287941919a544abc23cbf8a1c4d054c5c2804d6eddfa8ea8e1b943f3e8ce497` | `b9534dc62cd102c1dbd48e5f2c889ecdd5473e74a8fcf5ab4a09f2a266c1afa4` |
| 3 | `00-31-22_2v2_Derky_DESKTOPJ_HardAI_HardAI.rep` | 94,463 | `01c7766fabb7d1dc5d3d5383ba79adf0a52942c9587509483997cf3807210688` | `1,true,112,19,crc_mismatch,110` | `ae1b0f1dead48e43c0977a45426924db648bd0526ccfc9d151828eb9123f5439` | `1e4b13098510c96c42baf16b0648ec57ed67089bc22c78be7fcae97340d705f4` | `4b3f8a263175c28c6494c6e9ded394fe6d752c15fcd6df56c8862bb13d223398` |
| 4 | `00-41-30_2v2_Nic_BOMD2MAS_HardAI_HardAI.rep` | 77,245 | `afc4de634de7d8f97baadba43f6e7db56dc7a72a3de2e0cc328854ba5980537f` | `1,true,112,13,crc_mismatch,110` | `2f3f263497d0035ca37c8f166d6dcda90eb0c6a1087d232ef4082bdeb57de086` | `5944b8fec6e3a2e9dc5921c5e04f18fbf3a3b9fc3629e6f34cc42b620e2ca957` | `3f76a4a06ebad76d3df3efaaab68fbfd0df2a727b20ecd8a5eb028e8d756acd8` |
| 5 | `05-01-50_2v2_amoor123_beshr_HardAI_HardAI.rep` | 46,449 | `c587bef129122788e1c5c71fced777bfd5ca60c84058d603435beb7ab09051f8` | `1,true,112,24,crc_mismatch,110` | `8efd00c8f20c2167f021963e99ebc4e892432eec6848efa3abbeefeb141bbd94` | `a959eb0a9a1bec4e1270933ed346a81fa44403e25a68c047acc9e5752dead4a8` | `3e42fbc5f442317190ad3098496aadda389e7a19c2fd9949fb49fe57738a36ea` |
| 6 | `11-25-57_2v2_Kana_HardAI_Erbolat_Hulk.rep` | 25,093 | `7221f78559f9f7d96536b149ffe708902160612668614cb4c6dec0e4b74f2f73` | `1,true,112,34,crc_mismatch,110` | `cba0f7383363fb62dd5229df4aaf021259108c131a3af8b3bacec6202c5c3559` | `86c49dcbd0a4a5edebe363bfd3cbbcccdbea48535a7b772a931ccf07ce843cbf` | `8e48452f1193cd26af5fb2dab370fb2269fc4f8ed033211ea4232c1ef915f69c` |
| 7 | `12-11-35_2v2_babai_ILnur_HardAI_HardAI.rep` | 171,969 | `e4b1d5a48f76abedb705c4a77ad4c38590bac762e6a73f6fc112dc28d9e4cc40` | `1,true,112,14,crc_mismatch,110` | `cbf7b7839f39248f374384cf6802173ae93ac1fee076a9201e2984df1e81b22c` | `1e6adcb672216d24cf2d5e95ac39e77432a44e2e10bc6ccd273ede1fcceaaff9` | `1ff3a5f793c1718d1968757bf76a0d118bbf40c523c0d05798096d6163315022` |
| 8 | `15-07-24_2v2v2_Emkill_haker_HardAI_HardAI_HardAI_HardAI.rep` | 158,710 | `e53fda38ce6e7a3934ce608c1dcd9decc2a60d0d77055740c1eda34dbdf5d2d5` | `1,true,112,14,crc_mismatch,110` | `7ffd5ed398769c5a59c3b3c8cb61755a89235a030f727d83d68e5be23c15bd06` | `e34d32191f533a1bf2f3357c7b8ad935e5fdf5c7daee2e3c6eb02896000f0180` | `196ee9a76a977c0355cee3cc34d4e08c4e7ed61348a93f54bbd8cca2c6412f7c` |
| 9 | `18-13-02_3v3_Supremac_Loonen_JB_HardAI_HardAI_HardAI.rep` | 154,620 | `be3a803aba44449ee0be5d841d51d0775370ffecea81418cd58f625201f81d7f` | `1,true,112,29,crc_mismatch,110` | `89efc28e2153e866f1bf1f4078a764738da4d5188ae52e26ab32fc8fd0699bd8` | `8cec43daaac9f839b835e10adb28787c6fde79394dce61526d714dc25185eaf7` | `f3daa91a0593d08dd51b9701bc27a57280199db439a3d3aebdef3f1e82aa7a46` |
| 10 | `366648.rep` | 72,613 | `19537ab5e4021e0306bde18badab1f6788eadbb5270aef0114831933ffec89f8` | `1,true,112,28,crc_mismatch,110` | `ff7d294ccd208f6051ef9a50e9ca94434dfcd94fdc197d36f6ef7aad5bda8e03` | `f9f5e9ca1f31c1ab396a9469ff5263910c59801262a2a562f0c91dbd79e1cf35` | `27cbdda0248251efabe00f395170dfdcfbae7f08700a7d5c1efacce75c0785f7` |

All six emitted-file hashes for each enabled run (`height`, exact `manifest`, amphibious, ground, terrain, zones):

| # | `height.f32.zlib` | `manifest.json` | `pathing-amphibious.u8.zlib` | `pathing-ground.u8.zlib` | `terrain.u8.zlib` | `zones.i32.zlib` |
|---:|---|---|---|---|---|---|
| 1 | `a1c4b3e5de32840d0df2f63ada179140005c309592ca53c828a9774c9e771cb1` | `284fa6ee7489c1b89eae169a1f03e092a98f2363f529cffa2e332d61af7e2902` | `8a4e23b49cbec83e982aca862b61743715520f7c43aa15a29f975eb93638ae8d` | `a4934df6afd0351c3a2bc4edbc3c46569c3fb4ac78aa987fd710e0265b0628a5` | `8328484d7b4b0dfad1cc8c754089889d529c2aafa636d17e2a934b023308568e` | `33cea6e0a8d6020dca5452d7ddad58ff4e4422e8b5e821a5e3929752a92ba3bb` |
| 2 | `cc9ac0de3643de9aa132cd58b8e32736957d6498c4ea6a23d0561c9ef3c13738` | `b9534dc62cd102c1dbd48e5f2c889ecdd5473e74a8fcf5ab4a09f2a266c1afa4` | `bba276e01517636960d2ae1a92eaf64915768128a20f309dfb2b4d80a2a75bb4` | `bba276e01517636960d2ae1a92eaf64915768128a20f309dfb2b4d80a2a75bb4` | `d77fc0256efec4dc25eb29eadfd8b08bbe8ea1f01ae232d6e0f1654e31eb699d` | `8f4cf4a5c53e235ac521f20a78ccdee157481de48af85bbac35b699bb1605733` |
| 3 | `3cc7d42c2ff6073c95ff71e28341cb66944e2f283dd072c4f4cb3637301af3fa` | `4b3f8a263175c28c6494c6e9ded394fe6d752c15fcd6df56c8862bb13d223398` | `8d74d1e82b916275461bfaaadf8638357813760d1d659d75b9ff3619229cd929` | `96a27eea3e371e1c8d4f5c5bd5e0bfb62dda82670e1fa7c214ba1a0e184d9c2f` | `7a2da9924ec918d546b3626fcf52c73d9b0d8369d7ac2c6d9ef8c9963f8ba335` | `7297b73a5ed9cc864c7d09675081962712ddc9691da032bc8047df53196becbf` |
| 4 | `62e255202508b0bd09084aff7765f2a87265aa7d2bbf6fd2c2758e0634281f01` | `3f76a4a06ebad76d3df3efaaab68fbfd0df2a727b20ecd8a5eb028e8d756acd8` | `e3363793fd1fcf2e75e573ed92d00982d6cf47ea6ec8bfe0ec8e9895e1a7a719` | `0dc3eddcd3ff9e53f8d706213ed9d1136f43d6b0fdb2fb35cdd868b4fd920d3d` | `2bdf4efc3838d61807f471e9388e833bac18b0f0640ebdf9271a9c4d895f1b9a` | `8807ed335cf7c1597f57bb1e3dd00b1febf5ffd252f0098361dcbc987c45b6ad` |
| 5 | `62e255202508b0bd09084aff7765f2a87265aa7d2bbf6fd2c2758e0634281f01` | `3e42fbc5f442317190ad3098496aadda389e7a19c2fd9949fb49fe57738a36ea` | `8c1e28db3f5ab362085707859c13382d8e93779d70223c495c5b8117384bcbca` | `d6a1ecbb11811a60e2ed46d9dab3cb6b29f2af65c729299047c56421f0043c3f` | `c93ca637e1148209bbdb0c431f989afc69beb1ee74f142b31a1f8bb979ce78e3` | `1c4d68d8ab9fc17bfe3b1b8a5e8a38aa5df82b219abf2746e5cb478eb55b923a` |
| 6 | `66431b1e413d47ff55078a83b1fe36259e0f3742f80e84a9702667a72f951edb` | `8e48452f1193cd26af5fb2dab370fb2269fc4f8ed033211ea4232c1ef915f69c` | `37a4ca084bca6f9cca8e6829ee1e5b0ec0383b175721a5a047a7cf551479e09d` | `8af5d366cc79a04d8d8982e1fced3aa79c54955954acfc274ae89fb6b46c8893` | `5609a15af963efc582b6836a212752a29f38fd9ac817c3fa991dae424dfa1a6e` | `8ca1aba4b5f0f25f5b487f57b87be22bea1374d0847f0132ceecea83636b9c79` |
| 7 | `8a0efbf948012186d173b46de95ade837cab874df0e6022272aab7814eb3f3c5` | `1ff3a5f793c1718d1968757bf76a0d118bbf40c523c0d05798096d6163315022` | `a26f413763379bba1f0f277ab868f31727bb46da30ea247f81ec9d7e40601dca` | `a26f413763379bba1f0f277ab868f31727bb46da30ea247f81ec9d7e40601dca` | `994a5944a3ba3e98c86ab1bb2235811bb971ac9622435f9481e29b6211853635` | `f1dd1282c7386553f93a504c6da54cdff25e85496a9ab3825f73aa07f12e544d` |
| 8 | `e5c082fd6016258b8a2eec4b253961e9f106c89797369af4d80bdce05d69a780` | `196ee9a76a977c0355cee3cc34d4e08c4e7ed61348a93f54bbd8cca2c6412f7c` | `ef3cb8cbcc13572e90d49ffb9a07e5ee7a2d3d92e7e5c75fad82613b3f0b8c84` | `ef3cb8cbcc13572e90d49ffb9a07e5ee7a2d3d92e7e5c75fad82613b3f0b8c84` | `49c1752e821d65846252b796d036b1818512c8b4d7cfdf677f0578c835f18c94` | `18fee8836e46ae7635825a55cad748cdde57699ab14e9d47a511f9aa429c6cef` |
| 9 | `aee8f6f31f9c30ca806b217d890ff59b36393d33fd60fb6ce2f5a327b3722790` | `f3daa91a0593d08dd51b9701bc27a57280199db439a3d3aebdef3f1e82aa7a46` | `8b536079951bd13b3da76ad7efb20b202cd6df45f9431bf8ccfce2a3992fee6a` | `8b536079951bd13b3da76ad7efb20b202cd6df45f9431bf8ccfce2a3992fee6a` | `7a8960b720ebb7a307689862d3857b55059868c0a948f45a1fcccac2119395b2` | `8fe9e0b98b2da9da9a99cc489d5563bbad7dc85d3f7831a7bdeecd7fb6d4e34d` |
| 10 | `ec7e9728360f07571f83e09b9292a4eaf6b09ff6083e8a8eceb7c2466d005abf` | `27cbdda0248251efabe00f395170dfdcfbae7f08700a7d5c1efacce75c0785f7` | `01f6be578b5d98bc9e02234f78ef61113c61a644403c4ef78b9921fd43673eff` | `7bda4fa5951c7d59bea4cab916757d9448698802a50afaa10f5a466fedb1a3a9` | `4c967fb3fa89d9e8e67288b220e15fcf08dcec444da0552a88f27ea199625c92` | `e228d754c9455b6a80455af9dd401378795c82ce9ede0148422572783a03a03f` |

The isolated map manifest is exactly six source files: four `[RANK] Arctic Arena ZH v1` files and two `tansooo`
files. Their `(relative path, size, SHA-256)` tuples are asserted before every copy. The corpus gate completed in
60.11 seconds; all 20 launches passed and the real registry-derived user-data root remained byte-for-byte unchanged.

The matrix also source-grounded three previously unrepresented retail-map facts without weakening validation:

- terrain-created `GenericBridge` objects now receive a buffered `map_loaded` lifecycle identity, and the exporter
  observes the existing authoritative orientation setter rather than calling any gameplay setter twice;
- out-of-bounds movement is exempt only for `map_loaded` objects whose catalog/lifecycle KindOf contains `IMMOBILE`
  and whose ID is absent the exact closed classified static-feature set; and
- duplicate raw waypoint display names are preserved, while graph links are sorted, unique, non-self waypoint IDs
  and each linked name is cross-bound to its ID target.

## Unknown and unsupported observations

- Disabled runs intentionally expose no telemetry `match_outcome`; winner/loser facts are unknown.
- The pinned enabled trace terminates at a CRC boundary before victory conditions decide a result, so winner and loser
  identities are unknown rather than empty-result claims.
- No unsupported telemetry event type was accepted by the strict reader. Any field not present in the validated v2
  records remains unknown; the verification does not synthesize it from replay headers, stdout, or disabled runs.

## Acceptance status

Verified: raw-byte normalization mutation sensitivity; pinned three-by-three trace determinism; pinned independent
terminal-fact parity; exact map asset identity; complete v2 validation; real player names; monotonic evidence identity;
no writer errors; deterministic logs; complete ten-replay enabled/disabled parity; replay/executable/user-data
non-interference; and exclusion of the quarantined
synthetic prototype. The existing `tests/test_quarantine.py` AST import-graph guard passed in the complete non-engine
gate, and the wheel packages only `src/generals_replay_analyzer` rather than `legacy_prototype`.

The Engine Telemetry Acceptance Gate is complete. Match winners and losers remain deliberately unknown where the
authoritative terminal is an early CRC mismatch; this gate does not invent analytics facts from replay filenames.

Modern VS 2022 x86 Release and Debug both built and linked `generalszh.exe`. The final Debug executable SHA-256 was
`ce4775461513a1be77d641edac4e8f8f0689cd4acea01ed46783e3e33c722cb7`. An initial build from a plain shell failed at
`gitinfo.cpp` with `fatal error C1083: Cannot open include file: 'time.h'`; entering the installed x86 developer
environment supplied the SDK include paths and both builds then exited zero.

VC6 and MinGW were not available and no compiler-pass claim is made. `cmake --preset vc6` reported unknown C/C++
compiler identities and `No CMAKE_C_COMPILER could be found` / `No CMAKE_CXX_COMPILER could be found`.
`cmake --preset mingw-w64-i686` reported that no `Unix Makefiles` build program was available and
`CMAKE_MAKE_PROGRAM is not set`. Static CMake inspection confirms the analyzer-only source list and
`RTS_REPLAY_ANALYZER` definition are both inside `if(NOT IS_VS6_BUILD)` in
`GeneralsMD/Code/GameEngine/CMakeLists.txt`; this is exclusion evidence, not a VC6 compile result.

Final gates after isolated-root integration and corpus closure:

- focused Task 10 determinism/non-interference tests: 22 passed in 82.30 seconds;
- non-engine tests: 521 passed, 233 deselected in 33.41 seconds;
- all-ten isolated corpus matrix: 1 passed in 60.11 seconds;
- unrestricted engine tests: 229 passed, 4 host-capability skips, 521 deselected in 440.05 seconds;
- Ruff over `src` and `tests`: passed;
- strict mypy: passed for 24 source files; and
- wheel and source-distribution build, including packaged schema smoke coverage: passed; and
- modern Release and Debug build/link: passed with the exact hashes above.
