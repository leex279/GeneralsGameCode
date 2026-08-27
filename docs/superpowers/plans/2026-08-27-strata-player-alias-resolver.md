# Strata Player Alias Resolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a standalone `strata-resolver` CLI and library that parses Zero Hour replays, discovers every Strata alias owner, correlates full match context, and returns evidence-preserving identity resolutions.

**Architecture:** Add an isolated `generals_replay_analyzer.strata` subsystem inside the existing Python distribution. It reuses the strict replay parser, owns a separate SQLite cache/audit database, uses Playwright only for lazy listings, uses bounded HTTP for server-rendered details and replay bytes, and never mutates the main Replay Analyzer identity database.

**Tech Stack:** Python 3.11, dataclasses, argparse, sqlite3, hashlib, Unicode NFC/casefold, httpx, Beautiful Soup 4, Playwright Chromium, pytest, Ruff, strict mypy, Hatch/uv.

**Spec:** `docs/superpowers/specs/2026-08-27-strata-player-alias-resolver-design.md`

## Global Constraints

- Target Zero Hour `/zh` only; do not add Generals 1.08 behavior.
- Preserve `query_name_raw`; remove terminal NUL padding only.
- Never present a Strata player ID as replay-derived.
- Multiple exact name-only candidates remain `ambiguous` even when `selected` is populated.
- Case-insensitive candidates stay separate; fuzzy candidates never auto-select.
- Preserve replay slot order and every alternative candidate in public or audit evidence.
- Use the existing `parse_replay` implementation; do not create a second binary parser.
- Keep the resolver database separate from the Replay Analyzer production database.
- Default tests must be network-free; live crawling is opt-in and bounded.
- Source caps, timeouts, allowlists, cache TTLs, and rate limits fail closed with `incomplete` evidence.
- Every architectural or user-facing Python change includes a `TheSuperHackers` comment with an allowed keyword, author `Leex`, and date `27/08/2026`.
- Do not touch or stage the unrelated dirty engine, telemetry, browser-fixture, or vendor files already present in the worktree.

---

## File Structure

Create these focused production modules:

- `src/generals_replay_analyzer/strata/__init__.py`: stable public resolver API exports.
- `src/generals_replay_analyzer/strata/contracts.py`: immutable public values, enums, and stable JSON serialization.
- `src/generals_replay_analyzer/strata/normalization.py`: raw/NFC/casefold validation and fuzzy comparison inputs.
- `src/generals_replay_analyzer/strata/replay_context.py`: strict parser adapter and replay fingerprints.
- `src/generals_replay_analyzer/strata/extract.py`: pure HTML extraction for profiles and match details.
- `src/generals_replay_analyzer/strata/cache.py`: dedicated SQLite schema, TTL cache, and append-only audit writes.
- `src/generals_replay_analyzer/strata/ports.py`: browser, HTTP, clock, and sleeper protocols.
- `src/generals_replay_analyzer/strata/http.py`: allowlisted bounded httpx client, rate limiter, retries, and replay streaming.
- `src/generals_replay_analyzer/strata/browser.py`: Playwright listing discovery and pagination.
- `src/generals_replay_analyzer/strata/acquisition.py`: cache-aware source orchestration.
- `src/generals_replay_analyzer/strata/matching.py`: pure alias partitioning, match evidence, ranking, and status policy.
- `src/generals_replay_analyzer/strata/service.py`: name and whole-replay resolution orchestration.
- `src/generals_replay_analyzer/strata/config.py`: validated defaults, environment, paths, and caps.
- `src/generals_replay_analyzer/strata/cli.py`: standalone command parser, JSON stdout, stderr diagnostics, and exit codes.

Create deterministic tests under `tests/strata/` and sanitized HTML fixtures under `tests/fixtures/strata/`. Modify `pyproject.toml`, `uv.lock`, `README.md`, `tests/test_package.py`, and `tests/test_wheel.py` only for installed-command and dependency behavior.

---

### Task 1: Public Contracts and Exact Name Normalization

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/__init__.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/contracts.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/normalization.py`
- Create: `scripts/replay_analyzer/tests/strata/__init__.py`
- Create: `scripts/replay_analyzer/tests/strata/test_normalization.py`
- Create: `scripts/replay_analyzer/tests/strata/test_contracts.py`

**Interfaces:**
- Produces: `normalize_query_name(value: str) -> QueryName`
- Produces: `MatchKind`, `ResolutionStatus`, `Confidence`, `QueryName`, `AliasRecord`, `PlayerCandidate`, `NameResolution`
- Produces: every public contract method `to_dict() -> dict[str, object]`

- [ ] **Step 1: Write failing normalization tests**

```python
def test_query_normalization_removes_only_terminal_nul_padding() -> None:
    query = normalize_query_name(" [TAG]Fish! \x00\x00")
    assert query.raw == " [TAG]Fish! "
    assert query.nfc == " [TAG]Fish! "
    assert query.casefold == " [tag]fish! "


def test_query_normalization_preserves_case_and_canonically_composes_unicode() -> None:
    query = normalize_query_name("Fi\u0301sh")
    assert query.raw == "Fi\u0301sh"
    assert query.nfc == "F\u00edsh"
    assert query.casefold == "f\u00edsh"
```

Add table-driven rejection tests for empty-after-padding, embedded NUL, unpaired surrogate, non-string input, and 256 code points. Each expected error is `InvalidQueryNameError` with stable code `invalid_query_name`.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run --project . pytest tests/strata/test_normalization.py tests/strata/test_contracts.py -q`

Expected: collection fails because `generals_replay_analyzer.strata` does not exist.

- [ ] **Step 3: Implement immutable contracts and normalization**

Use string enums with these exact values:

```python
class MatchKind(StrEnum):
    EXACT = "exact"
    CASE_INSENSITIVE = "case_insensitive"
    FUZZY = "fuzzy"


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    INCOMPLETE = "incomplete"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"
```

`QueryName` contains `raw`, `nfc`, and `casefold`. `AliasRecord` contains source, player ID, profile URL, most-known name, alias values, occurrence count, and source rank. `PlayerCandidate` contains the matched alias, match kind, selection reason, and a tuple of evidence records. `NameResolution` contains the selected candidate, exact alternatives excluding selected, separate case-insensitive/fuzzy tuples, completeness, confidence, context need, and checked time.

Serialize datetimes as UTC `YYYY-MM-DDTHH:MM:SSZ`, emit tuples as arrays, use integer player IDs, and reject naive datetimes in constructors.

Implement normalization exactly as:

```python
raw = value.rstrip("\x00")
nfc = unicodedata.normalize("NFC", raw)
casefold = unicodedata.normalize("NFC", nfc.casefold())
```

Do not call `.strip()` and do not reuse `normalize_embedded_name`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run --project . pytest tests/strata/test_normalization.py tests/strata/test_contracts.py -q`

Expected: all Task 1 tests pass with no warnings.

- [ ] **Step 5: Run focused static checks**

Run: `uv run --project . ruff check src/generals_replay_analyzer/strata tests/strata/test_normalization.py tests/strata/test_contracts.py`

Run: `uv run --project . mypy src/generals_replay_analyzer/strata`

Expected: both exit 0.

- [ ] **Step 6: Commit Task 1**

```text
git add scripts/replay_analyzer/src/generals_replay_analyzer/strata scripts/replay_analyzer/tests/strata
git commit -m "feat(identity): Add Strata resolution contracts"
```

---

### Task 2: Replay Context and Evidence Fingerprints

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/replay_context.py`
- Create: `scripts/replay_analyzer/tests/strata/test_replay_context.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/contracts.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/__init__.py`

**Interfaces:**
- Consumes: `parse_replay(path: Path) -> ParsedReplay`, `extract_source_provenance(path: Path) -> SourceProvenance`
- Produces: `ReplayParticipant`, `ReplayFingerprints`, `ReplayContext`
- Produces: `build_replay_context(path: Path) -> ReplayContext`

- [ ] **Step 1: Write failing fixture-backed replay tests**

```python
FIXTURE = Path("tests/fixtures/zero_hour_1_04/leex279_vs_fox27.rep")


def test_replay_context_preserves_local_identifiers_names_and_match_hint() -> None:
    context = build_replay_context(FIXTURE)
    assert context.replay_sha256 == "EA085767BFA11D2CFC167D9007173CE2EB29B5F557702FFD042E2E9A1A8F6BB8"
    assert context.hinted_strata_match_id == 3133811
    assert [(item.slot_index, item.player_index, item.name_raw) for item in context.human_players] == [
        (0, None, "leex279"),
        (1, None, "FOX27"),
    ]
    assert context.map_path == "userdata/maps/[rank] sand scorpion"
    assert context.header_duration_seconds == 940


def test_command_stream_and_match_signature_fingerprints_are_stable() -> None:
    context = build_replay_context(FIXTURE)
    assert re.fullmatch(r"[0-9A-F]{64}", context.command_stream_sha256)
    assert re.fullmatch(r"[0-9A-F]{64}", context.match_signature_sha256)
    assert context.to_dict()["fingerprints"]["raw_replay_sha256"] == context.replay_sha256
```

Add tests proving `completion_status != "complete"` yields `command_stream_sha256=None`, filenames without the strict provenance grammar yield no match hint, and changing only `replay_name`, system time, local slot, IP/port, accepted, or map-transfer flags does not change the match-signature payload.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run --project . pytest tests/strata/test_replay_context.py -q`

Expected: import fails for missing `replay_context`.

- [ ] **Step 3: Implement the parser adapter**

`ReplayContext` must contain:

```python
@dataclass(frozen=True, slots=True)
class ReplayContext:
    path: Path
    replay_sha256: str
    command_stream_sha256: str | None
    match_signature_sha256: str
    hinted_strata_match_id: int | None
    version_string: str
    version_number: int
    start_time: int
    end_time: int
    frame_count: int
    header_duration_seconds: int
    logic_duration_seconds: float | None
    map_path: str
    map_crc: int
    map_size: int
    seed: int
    starting_cash: int | None
    local_player_index: int
    slots: tuple[ReplayParticipant, ...]
```

Hash the raw file bytes for `replay_sha256`. Hash `data[parsed.command_stream_offset:parsed.end_offset]` only for complete parses. Build `match_signature_sha256` from canonical UTF-8 JSON with sorted keys and compact separators. Include only the spec-approved match fields; construct the participant list in `slot.index` order.

Keep `player_index=None`: the current raw parser observes command player indices but does not prove a canonical slot-to-command-player mapping. Do not infer equality with `slot_index`.

- [ ] **Step 4: Run focused parser and context tests**

Run: `uv run --project . pytest tests/strata/test_replay_context.py tests/test_parser.py tests/test_header.py -q`

Expected: all pass.

- [ ] **Step 5: Commit Task 2**

```text
git add scripts/replay_analyzer/src/generals_replay_analyzer/strata scripts/replay_analyzer/tests/strata/test_replay_context.py
git commit -m "feat(replay): Add Strata replay context fingerprints"
```

---

### Task 3: Frozen Strata HTML Extraction Contracts

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/extract.py`
- Create: `scripts/replay_analyzer/tests/strata/test_extract.py`
- Create: `scripts/replay_analyzer/tests/fixtures/strata/manifest.json`
- Create: `scripts/replay_analyzer/tests/fixtures/strata/player-17945.html`
- Create: `scripts/replay_analyzer/tests/fixtures/strata/player-6522.html`
- Create: `scripts/replay_analyzer/tests/fixtures/strata/player-30124.html`
- Create: `scripts/replay_analyzer/tests/fixtures/strata/player-30427.html`
- Create: `scripts/replay_analyzer/tests/fixtures/strata/player-52866.html`
- Create: `scripts/replay_analyzer/tests/fixtures/strata/match-3133811.html`
- Create: `scripts/replay_analyzer/tests/fixtures/strata/malformed-profile.html`
- Modify: `scripts/replay_analyzer/pyproject.toml`
- Modify: `scripts/replay_analyzer/uv.lock`

**Interfaces:**
- Produces: `ProfileDocument`, `MatchDocument`, `MatchParticipantDocument`
- Produces: `extract_profile(html: str, expected_player_id: int) -> ProfileDocument`
- Produces: `extract_match(html: str, expected_match_id: int) -> MatchDocument`

- [ ] **Step 1: Add sanitized source fixtures**

Each fixture must retain the structural elements the extractor consumes and remove scripts, cookies, CSRF values, analytics identifiers, and unrelated layout. `manifest.json` records exact source URL, capture date `2026-08-27`, fixture SHA-256, and purpose.

The player 17945 fixture contains the heading `-DoMiNaToR-` and all supplied Known Names, including `<span>fish</span><span>8</span>`. The other four fixtures contain the supplied exact and case-insensitive evidence. Match 3133811 contains the UTC interval, map, type, duration, starting cash, player links 27965/102894, factions/results, and both replay URLs.

- [ ] **Step 2: Write failing extraction tests**

```python
def test_profile_uses_highest_count_alias_not_search_row_label() -> None:
    profile = extract_profile(_fixture("player-17945.html"), expected_player_id=17945)
    assert profile.most_known_name == "-DoMiNaToR-"
    assert profile.aliases[15].name_raw == "fish"
    assert profile.aliases[15].occurrence_count == 8
    assert len(profile.aliases) == 80


def test_match_detail_extracts_shared_context_and_external_ids() -> None:
    match = extract_match(_fixture("match-3133811.html"), expected_match_id=3133811)
    assert match.map_name == "[RANK] Sand Scorpion"
    assert match.match_type == "1v1"
    assert match.duration_seconds == 942
    assert [(item.player_id, item.displayed_name) for item in match.participants] == [
        (27965, "leex279"),
        (102894, "FOX27"),
    ]
```

Add failures for wrong expected ID, missing Known Names, duplicate alias text, non-integer/negative counts, missing player links, non-HTTPS replay URL, wrong host/path, impossible UTC interval, duplicate participant IDs, and structurally incomplete HTML.

- [ ] **Step 3: Run tests and verify RED**

Run: `uv run --project . pytest tests/strata/test_extract.py -q`

Expected: missing module or missing Beautiful Soup dependency.

- [ ] **Step 4: Add Beautiful Soup and lock dependencies**

Add `beautifulsoup4>=4.13,<5` to `[project].dependencies`, then run:

Run: `uv lock`

Expected: `uv.lock` updates with Beautiful Soup and its transitive parser dependency only.

- [ ] **Step 5: Implement fail-closed extractors**

Use Beautiful Soup with the standard `html.parser`. Do not parse source facts by regex. Convert all IDs through strict decimal text and require canonical URLs:

```python
PLAYER_PATH = re.compile(r"/zh/player/(?P<id>[1-9][0-9]*)\Z")
MATCH_PATH = re.compile(r"/zh/match/(?P<id>[1-9][0-9]*)\Z")
REPLAY_PATH = re.compile(r"/replays/[0-9]{4}/[0-9]{1,2}/[0-9]{1,2}/match_[0-9]+/user_[0-9a-f]+/.+_replay\.rep\Z")
```

Parse UTC dates with an English month lookup owned by the extractor, not locale-sensitive `strptime`. Preserve source order. Raise `SourceContractError(code="profile_contract_changed" | "match_contract_changed")` without including raw HTML.

- [ ] **Step 6: Verify GREEN and static checks**

Run: `uv run --project . pytest tests/strata/test_extract.py -q`

Run: `uv run --project . ruff check src/generals_replay_analyzer/strata/extract.py tests/strata/test_extract.py`

Run: `uv run --project . mypy src/generals_replay_analyzer/strata/extract.py`

Expected: all exit 0.

- [ ] **Step 7: Commit Task 3**

```text
git add scripts/replay_analyzer/pyproject.toml scripts/replay_analyzer/uv.lock scripts/replay_analyzer/src/generals_replay_analyzer/strata/extract.py scripts/replay_analyzer/tests/strata/test_extract.py scripts/replay_analyzer/tests/fixtures/strata
git commit -m "feat(identity): Parse Strata profile and match evidence"
```

---

### Task 4: Dedicated SQLite Cache and Append-Only Resolution Audit

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/cache.py`
- Create: `scripts/replay_analyzer/tests/strata/test_cache.py`

**Interfaces:**
- Produces: `ResolverCache(path: Path, clock: Clock)` context manager
- Produces: `get_search`, `put_search`, `get_profile`, `put_profile`, `get_match`, `put_match`, `get_match_page`, `put_match_page`
- Produces: `append_resolution(replay_sha256: str | None, slot_index: int, result: NameResolution, evidence: Mapping[str, object]) -> str`
- Produces: `status() -> CacheStatus`, `purge_expired() -> PurgeResult`

- [ ] **Step 1: Write failing cache tests**

```python
def test_exact_alias_can_belong_to_multiple_strata_players(tmp_path: Path) -> None:
    with ResolverCache(tmp_path / "resolver.sqlite3", clock=_clock()) as cache:
        cache.put_profile(_profile(17945, "-DoMiNaToR-", (("fish", 8),)))
        cache.put_profile(_profile(6522, "StockFish'", (("fish", 3),)))
        assert [row.player_id for row in cache.aliases_exact("fish")] == [17945, 6522]


def test_expired_purge_keeps_resolution_audit(tmp_path: Path) -> None:
    clock = MutableClock(NOW)
    with ResolverCache(tmp_path / "resolver.sqlite3", clock=clock) as cache:
        cache.put_search(_search(expires_at=NOW + timedelta(hours=1)))
        audit_id = cache.append_resolution(None, 0, _ambiguous_resolution(), {"search_complete": True})
        clock.value = NOW + timedelta(hours=2)
        cache.purge_expired()
        assert cache.get_search("strata.gamereplays.org", "zh", "fish") is None
        assert cache.audit(audit_id, 0) is not None
```

Add tests for WAL/foreign keys/busy timeout, schema version rejection, alias indexes, transactional profile replacement, incomplete search caching, negative-result TTL, match participant order, database path validation, and update/delete triggers on `identity_resolutions`.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run --project . pytest tests/strata/test_cache.py -q`

Expected: missing `ResolverCache`.

- [ ] **Step 3: Implement schema version 1 with stdlib sqlite3**

Use `PRAGMA user_version=1`, `foreign_keys=ON`, `journal_mode=WAL`, and `busy_timeout=5000`. Create all spec tables and these indexes:

```sql
CREATE INDEX ix_strata_alias_raw ON strata_player_aliases(source, game, alias_raw);
CREATE INDEX ix_strata_alias_nfc ON strata_player_aliases(source, game, alias_nfc);
CREATE INDEX ix_strata_alias_casefold ON strata_player_aliases(source, game, alias_casefold);
CREATE INDEX ix_strata_match_time ON strata_matches(source, game, played_start_utc);
CREATE INDEX ix_strata_match_player ON strata_match_players(source, game, player_id, match_id);
CREATE INDEX ix_resolution_replay_slot ON identity_resolutions(replay_sha256, slot_index);
```

Add no-update and no-delete triggers for `identity_resolutions`. Store canonical JSON with sorted keys and compact separators. Every cache replacement uses `BEGIN IMMEDIATE`, deletes only the replaced profile/match children, inserts complete new children, then commits.

Reject relative paths, URI paths, directories, symlinks/reparse points, missing parents for explicit paths, and schema versions greater than 1.

- [ ] **Step 4: Verify GREEN**

Run: `uv run --project . pytest tests/strata/test_cache.py -q`

Expected: all pass.

- [ ] **Step 5: Commit Task 4**

```text
git add scripts/replay_analyzer/src/generals_replay_analyzer/strata/cache.py scripts/replay_analyzer/tests/strata/test_cache.py
git commit -m "feat(identity): Add isolated Strata evidence cache"
```

---

### Task 5: Bounded HTTP Client, Rate Limiter, and Replay Downloads

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/config.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/ports.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/http.py`
- Create: `scripts/replay_analyzer/tests/strata/test_config.py`
- Create: `scripts/replay_analyzer/tests/strata/test_http.py`

**Interfaces:**
- Produces: `ResolverSettings`, `AcquisitionCaps`, and `ResolverSettings.from_sources(values, environment)`
- Produces: `Clock.now() -> datetime`, `MonotonicClock() -> float`, `Sleeper(seconds: float) -> None`
- Produces: `StrataHttpPort.get_profile(player_id: int) -> str`
- Produces: `StrataHttpPort.get_match(match_id: int) -> str`
- Produces: `StrataHttpPort.download_replay(url: str) -> bytes`
- Produces: `HttpxStrataClient(settings: ResolverSettings, transport: httpx.BaseTransport | None, monotonic, sleeper, random)`

- [ ] **Step 1: Write failing configuration and boundary tests**

Configuration tests assert command/composition values override `STRATA_RESOLVER_*` environment values, which override defaults. Use these exact defaults: search TTL 24 hours, profile TTL 7 days, match-list TTL 24 hours, match-detail TTL 30 days, negative TTL 1 hour, request timeout 20 seconds, two concurrent HTTP requests, 500 ms host interval, three attempts, 100 player-search pages, 10,000 candidate IDs, 200 match-history pages per candidate, 20 MiB replay bytes, and Playwright Chromium.

Validate absolute cache paths, timeout 1-120 seconds, positive caps within the design maxima, TTLs from 60 seconds through 90 days, and browser values `chromium`/`chrome`.

Use real httpx `MockTransport` for request behavior:

```python
def test_profile_request_uses_canonical_url_and_descriptive_user_agent() -> None:
    requests: list[httpx.Request] = []
    client = _client(_recording_transport(requests, html="<html></html>"))
    client.get_profile(17945)
    assert requests[0].url == "https://strata.gamereplays.org/zh/player/17945"
    assert requests[0].headers["user-agent"].startswith("generals-strata-resolver/")


def test_replay_download_rejects_redirect_to_unapproved_host() -> None:
    client = _client(_redirect_transport("https://example.com/replay.rep"))
    with pytest.raises(SourceRequestError, match="redirect_not_allowed"):
        client.download_replay(APPROVED_REPLAY_URL)
```

Add tests for two-request concurrency cap, 500 ms host interval, connect/read timeout, 429 Retry-After, three-attempt retry exhaustion, 120-second Retry-After ceiling, circuit breaker after repeated failures, 20 MiB streaming limit, non-HTTPS URLs, wrong hosts, wrong path shapes, oversized HTML, and response-body cleanup.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run --project . pytest tests/strata/test_config.py tests/strata/test_http.py -q`

Expected: missing ports/client.

- [ ] **Step 3: Implement validated settings and the client**

`ResolverSettings.from_sources` accepts an explicit values mapping plus an environment mapping and never reads global environment state in tests. Resolve the default cache with `PlatformDirs("generals-strata-resolver", "TheSuperHackers").user_data_path / "resolver.sqlite3"`. Validate before constructing any browser, HTTP client, or database.

Use one `httpx.Client` with explicit `Timeout(connect=20, read=20, write=20, pool=20)`, `follow_redirects=False`, and a semaphore of two. Validate every initial and redirect URL before requesting it. Allow only:

- `https://strata.gamereplays.org/zh/player/<id>`
- `https://strata.gamereplays.org/zh/match/<id>`
- approved replay paths on `https://matchdata.playgenerals.online`

Read HTML with a 2 MiB limit and strict UTF-8. Stream replay bytes into a bounded `bytearray` and abort at 20 MiB. Retry only connection/timeouts, 429, and 5xx. Jitter comes from an injected `random.Random`, so tests assert exact sleep bounds without asserting a mock call as the product outcome.

- [ ] **Step 4: Verify GREEN and static checks**

Run: `uv run --project . pytest tests/strata/test_config.py tests/strata/test_http.py -q`

Run: `uv run --project . ruff check src/generals_replay_analyzer/strata/config.py src/generals_replay_analyzer/strata/http.py src/generals_replay_analyzer/strata/ports.py tests/strata/test_config.py tests/strata/test_http.py`

Run: `uv run --project . mypy src/generals_replay_analyzer/strata/config.py src/generals_replay_analyzer/strata/http.py src/generals_replay_analyzer/strata/ports.py`

Expected: all exit 0.

- [ ] **Step 5: Commit Task 5**

```text
git add scripts/replay_analyzer/src/generals_replay_analyzer/strata/config.py scripts/replay_analyzer/src/generals_replay_analyzer/strata/ports.py scripts/replay_analyzer/src/generals_replay_analyzer/strata/http.py scripts/replay_analyzer/tests/strata/test_config.py scripts/replay_analyzer/tests/strata/test_http.py
git commit -m "feat(identity): Bound Strata source requests"
```

---

### Task 6: Playwright Listing Discovery and Cache-Aware Acquisition

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/browser.py`
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/acquisition.py`
- Create: `scripts/replay_analyzer/tests/strata/fakes.py`
- Create: `scripts/replay_analyzer/tests/strata/test_browser.py`
- Create: `scripts/replay_analyzer/tests/strata/test_acquisition.py`
- Create: `scripts/replay_analyzer/tests/fixtures/strata/player-search-fish-page-1.html`
- Create: `scripts/replay_analyzer/tests/fixtures/strata/player-search-empty.html`
- Create: `scripts/replay_analyzer/tests/fixtures/strata/player-17945-matches-page-1.html`
- Modify: `scripts/replay_analyzer/pyproject.toml`
- Modify: `scripts/replay_analyzer/uv.lock`

**Interfaces:**
- Consumes: `ResolverCache`, `StrataHttpPort`, `extract_profile`, `extract_match`
- Produces: `ListingBrowserPort.search_players(query: QueryName, caps: AcquisitionCaps) -> PlayerSearchDiscovery`
- Produces: `ListingBrowserPort.player_matches(player_id: int, caps: AcquisitionCaps) -> MatchListDiscovery`
- Produces: `PlaywrightListingBrowser(settings: ResolverSettings)`
- Produces: `StrataAcquirer(cache: ResolverCache, browser: ListingBrowserPort, http: StrataHttpPort, settings: ResolverSettings, clock: Clock)`
- Produces: `StrataAcquirer.search`, `profile`, `match`, `candidate_matches`

- [ ] **Step 1: Write failing browser DOM-contract tests**

Use an injectable `BrowserPagePort` in unit tests. Feed a sequence of DOM snapshots derived from the sanitized fixtures and assert observable discovery:

```python
def test_player_search_deduplicates_numeric_profile_ids_and_exhausts_next() -> None:
    browser = PlaywrightListingBrowser(page_factory=_pages(SEARCH_PAGE_1, SEARCH_PAGE_2))
    result = browser.search_players(normalize_query_name("fish"), AcquisitionCaps())
    assert result.player_ids == (17945, 6522, 30124, 30427, 52866)
    assert result.complete is True
    assert result.pages_visited == 2


def test_skeleton_without_rows_or_verified_empty_state_is_incomplete() -> None:
    browser = PlaywrightListingBrowser(page_factory=_pages(SKELETON_ONLY))
    result = browser.search_players(normalize_query_name("fish"), AcquisitionCaps())
    assert result.complete is False
    assert result.reason_codes == ("listing_not_ready",)
```

Add tests for exact URL encoding, `amount=100`, canonical link filtering, pagination cycles, disabled Next, 100-page cap, 10,000-ID cap, player-match 200-page cap, and duplicate match IDs.

- [ ] **Step 2: Write failing cache-aware acquisition tests**

```python
def test_unexpired_profile_cache_avoids_http_and_preserves_alias_order(tmp_path: Path) -> None:
    cache = _cache_with_profile(tmp_path, player_id=17945)
    acquirer = StrataAcquirer(cache, browser=FailIfCalledBrowser(), http=FailIfCalledHttp(), clock=_clock())
    profile = acquirer.profile(17945, refresh=False, offline=False)
    assert profile.most_known_name == "-DoMiNaToR-"
    assert profile.aliases[15].name_raw == "fish"


def test_offline_cache_miss_is_incomplete_not_not_found(tmp_path: Path) -> None:
    acquirer = _empty_acquirer(tmp_path)
    result = acquirer.search(normalize_query_name("fish"), refresh=False, offline=True)
    assert result.complete is False
    assert result.reason_codes == ("offline_cache_miss",)
```

- [ ] **Step 3: Run tests and verify RED**

Run: `uv run --project . pytest tests/strata/test_browser.py tests/strata/test_acquisition.py -q`

Expected: missing browser/acquisition modules.

- [ ] **Step 4: Move Playwright into runtime dependencies and lock**

Move `playwright==1.61.0` from the dev-only list to `[project].dependencies`; keep `pytest-playwright` in the dev group. Run `uv lock` and confirm no Playwright version drift.

- [ ] **Step 5: Implement browser and acquisition adapters**

The concrete browser imports `playwright.sync_api` lazily inside `__enter__`. It launches one browser, owns one page, navigates sequentially, waits for one of these explicit conditions, and closes in `__exit__`:

- valid player/match rows exist;
- verified empty state exists;
- timeout expires.

Use locators and DOM attributes, not coordinate clicks. Collect only canonical numeric links. After each Next click, require the page number or first row key to change.

`StrataAcquirer` applies the configured TTLs and records whether each value came from fresh source, unexpired cache, stale offline cache, or failed acquisition. It never turns incomplete discovery into an empty complete result.

- [ ] **Step 6: Verify GREEN**

Run: `uv run --project . pytest tests/strata/test_browser.py tests/strata/test_acquisition.py -q`

Expected: all pass without launching a real browser.

- [ ] **Step 7: Commit Task 6**

```text
git add scripts/replay_analyzer/pyproject.toml scripts/replay_analyzer/uv.lock scripts/replay_analyzer/src/generals_replay_analyzer/strata/browser.py scripts/replay_analyzer/src/generals_replay_analyzer/strata/acquisition.py scripts/replay_analyzer/tests/strata scripts/replay_analyzer/tests/fixtures/strata
git commit -m "feat(identity): Discover complete Strata candidate sets"
```

---

### Task 7: Deterministic Alias and Match-Evidence Policy

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/matching.py`
- Create: `scripts/replay_analyzer/tests/strata/test_matching.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/contracts.py`

**Interfaces:**
- Produces: `DownloadedReplayEvidence`, `MatchFact`, `MatchEvidence`, `MatchResolution`, and `AliasPartitions` contracts
- Produces: `partition_aliases(query: QueryName, aliases: Sequence[AliasRecord], include_fuzzy: bool) -> AliasPartitions`
- Produces: `resolve_name(query: QueryName, aliases: Sequence[AliasRecord], search_complete: bool, checked_at: datetime, include_fuzzy: bool = False) -> NameResolution`
- Produces: `evaluate_match(context: ReplayContext, match: MatchDocument, downloaded: Sequence[DownloadedReplayEvidence]) -> MatchEvidence`
- Produces: `rank_match_evidence(evidence: Sequence[MatchEvidence]) -> MatchResolution`

- [ ] **Step 1: Write failing `fish` policy tests**

```python
def test_multiple_exact_fish_owners_select_by_count_but_remain_ambiguous() -> None:
    result = resolve_name(FISH, FISH_ALIASES, search_complete=True, checked_at=NOW)
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.selected is not None and result.selected.player_id == 17945
    assert [item.player_id for item in result.alternatives] == [6522, 30124]
    assert [item.player_id for item in result.case_insensitive_suggestions] == [30427, 52866]
    assert result.confidence is Confidence.MEDIUM
    assert result.needs_replay_context is True


def test_case_insensitive_only_candidate_never_resolves_from_name_alone() -> None:
    result = resolve_name(normalize_query_name("FISH"), (_alias(17945, "fish", 8),), True, NOW)
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.selected is None
    assert result.case_insensitive_suggestions[0].player_id == 17945
```

Add tests for one exact exhausted candidate resolving medium, incomplete search overriding resolution to incomplete, deterministic tie order, fuzzy non-selection, zero candidates, and duplicate aliases.

- [ ] **Step 2: Write failing match-evidence tests**

Use literal replay/match values and assert:

- raw SHA equality gives high confidence;
- command-stream equality gives high confidence across different raw hashes;
- exact timestamp/participants/map/type/duration/factions gives medium confidence;
- participant count, map, or nonmatching available command fingerprint eliminates the match;
- duration tolerance is inclusive at five seconds and rejects six seconds;
- one unique shared match maps replay slots to Strata IDs;
- two viable shared matches remain ambiguous;
- alternative name candidates remain attached after context resolution.

- [ ] **Step 3: Run tests and verify RED**

Run: `uv run --project . pytest tests/strata/test_matching.py -q`

Expected: missing matching functions.

- [ ] **Step 4: Implement pure deterministic matching**

Exact ranking key:

```python
(
    -candidate.replay_context_score,
    -candidate.alias_occurrence_count,
    candidate.source_rank,
    candidate.player_id,
)
```

Do not collapse aliases by normalized name. Deduplicate only the same `(source, player_id, alias_raw)`.

Match evidence contains individual labelled facts with `observed`, `expected`, `agreement`, and `weight_class`. High-value contradictions set `viable=False`. Medium resolution requires timestamp, complete participant set, map, count/type, duration, and every available faction assignment with no contradiction.

Define `DownloadedReplayEvidence` in `contracts.py` with replay URL plus raw, nullable command-stream, and match-signature SHA-256 values. Define `MatchResolution` with status, confidence, selected match ID/URL, alternatives, evidence, and the unique tuple of `(slot_index, player_id)` assignments when resolved.

Participant mapping must be a one-to-one bipartite assignment. First require exact displayed/embedded name equality; then allow a candidate player's profile aliases for that slot. If more than one complete assignment exists, keep the match ambiguous.

- [ ] **Step 5: Verify GREEN and static checks**

Run: `uv run --project . pytest tests/strata/test_matching.py -q`

Run: `uv run --project . ruff check src/generals_replay_analyzer/strata/matching.py tests/strata/test_matching.py`

Run: `uv run --project . mypy src/generals_replay_analyzer/strata/matching.py`

Expected: all exit 0.

- [ ] **Step 6: Commit Task 7**

```text
git add scripts/replay_analyzer/src/generals_replay_analyzer/strata/contracts.py scripts/replay_analyzer/src/generals_replay_analyzer/strata/matching.py scripts/replay_analyzer/tests/strata/test_matching.py
git commit -m "feat(identity): Rank Strata candidates by replay evidence"
```

---

### Task 8: Name and Whole-Replay Resolver Service

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/service.py`
- Create: `scripts/replay_analyzer/tests/strata/test_service.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/__init__.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/contracts.py`

**Interfaces:**
- Consumes: `StrataAcquirer`, `ResolverCache`, matching functions, `build_replay_context`
- Produces: `ReplayPlayerResolution` and `ReplayResolution` contracts in `contracts.py`
- Produces: `StrataResolver.resolve_name(value: str, *, refresh: bool, offline: bool, include_fuzzy: bool) -> NameResolution`
- Produces: `StrataResolver.resolve_replay(path: Path, *, refresh: bool, offline: bool, include_fuzzy: bool) -> ReplayResolution`

- [ ] **Step 1: Write failing name-service tests**

Use real normalization, matching, and cache with fake external ports. Assert the service:

- searches raw `fish`, fetches every deduplicated profile, and returns 17945 selected plus exact alternatives;
- marks results incomplete when any candidate profile fails;
- writes one append-only audit record per invocation;
- reuses cached profiles on the second invocation;
- exposes no cookie, CSRF, or raw HTML content in public errors.

- [ ] **Step 2: Write failing replay-service integration test**

```python
def test_pinned_replay_resolves_shared_match_and_both_player_ids(tmp_path: Path) -> None:
    resolver = _resolver_for_match_3133811(tmp_path)
    result = resolver.resolve_replay(PINNED_REPLAY, refresh=False, offline=False, include_fuzzy=False)
    assert result.match_resolution.status is ResolutionStatus.RESOLVED
    assert result.match_resolution.selected_match_id == 3133811
    assert [(item.slot_index, item.query_name.raw, item.selected.player_id) for item in result.players] == [
        (0, "leex279", 27965),
        (1, "FOX27", 102894),
    ]
    assert all(item.confidence in {Confidence.HIGH, Confidence.MEDIUM} for item in result.players)
```

Add tests for an invalid replay causing zero network calls, filename hint verification before acceptance, rejected hint fallback to match-history discovery, shared match deduplication across candidates, replay download only after metadata viability, exact raw/command fingerprint confirmation, ambiguous assignments, and preservation of all name alternatives.

- [ ] **Step 3: Run tests and verify RED**

Run: `uv run --project . pytest tests/strata/test_service.py -q`

Expected: missing service.

- [ ] **Step 4: Implement orchestration in bounded stages**

`resolve_replay` performs:

1. parse and validate replay;
2. name resolution for every human slot in slot order;
3. verify hinted match, if present;
4. otherwise discover match IDs for every viable exact/case-insensitive candidate;
5. fetch and evaluate each deduplicated match detail;
6. download replay bytes only for metadata-viable matches;
7. parse downloaded bytes in a temporary directory and compute fingerprints;
8. select a unique shared match/assignment under the policy;
9. produce per-slot results without deleting name alternatives;
10. append audit rows in one transaction after the public result is complete.

Temporary replay files use a private `TemporaryDirectory`, strict filenames generated by the tool, and guaranteed cleanup. Download parsing errors eliminate only that fingerprint fact unless the page replay was required for high-confidence confirmation; they never execute the game.

- [ ] **Step 5: Verify GREEN with complete resolver tests**

Run: `uv run --project . pytest tests/strata/test_service.py tests/strata/test_matching.py tests/strata/test_replay_context.py -q`

Expected: all pass.

- [ ] **Step 6: Commit Task 8**

```text
git add scripts/replay_analyzer/src/generals_replay_analyzer/strata scripts/replay_analyzer/tests/strata/test_service.py
git commit -m "feat(identity): Resolve replay players against Strata matches"
```

---

### Task 9: Configuration, Standalone CLI, Doctor, and Cache Commands

**Files:**
- Create: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/cli.py`
- Create: `scripts/replay_analyzer/tests/strata/test_cli.py`
- Modify: `scripts/replay_analyzer/src/generals_replay_analyzer/strata/config.py`
- Modify: `scripts/replay_analyzer/tests/strata/test_config.py`
- Modify: `scripts/replay_analyzer/pyproject.toml`
- Modify: `scripts/replay_analyzer/README.md`
- Modify: `scripts/replay_analyzer/tests/test_package.py`
- Modify: `scripts/replay_analyzer/tests/test_wheel.py`

**Interfaces:**
- Consumes: `ResolverSettings.from_sources(values, environment) -> ResolverSettings`
- Produces: `main(argv: Sequence[str] | None = None) -> int`
- Produces installed command: `strata-resolver = generals_replay_analyzer.strata.cli:main`

- [ ] **Step 1: Write failing configuration tests**

Extend the Task 5 settings tests for CLI spelling, `--cache`, `--offline`, `--refresh`, `--browser`, `--timeout-seconds`, and offline/refresh incompatibility. Keep configuration precedence and limits unchanged.

- [ ] **Step 2: Write failing CLI tests**

```python
def test_resolve_name_writes_one_json_document_and_diagnostics_to_stderr(capsys: CaptureFixture[str]) -> None:
    code = main(["resolve-name", "fish", "--cache", str(CACHE)], application_factory=_application)
    captured = capsys.readouterr()
    assert code == 3
    assert json.loads(captured.out)["status"] == "ambiguous"
    assert captured.out.count("\n") == 1
    assert "candidate profiles" in captured.err


def test_inspect_replay_never_emits_external_id_without_resolution(capsys: CaptureFixture[str]) -> None:
    assert main(["inspect-replay", str(PINNED_REPLAY), "--cache", str(CACHE)]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["players"][0]["slot_index"] == 0
    assert document["players"][0]["player_index"] is None
    assert "strata_player_id" not in document["players"][0]
```

Add tests for `resolve-replay`, `--name-file`, invalid UTF-8, `--pretty`, offline incomplete status, stable exit codes, `cache status`, expired purge, interactive all-purge refusal, `--yes` all-purge scope, and `doctor` with browser/database/HTTPS outcomes.

Use these exit codes: 0 resolved/success, 2 invalid/unsupported request, 3 ambiguous/not-found, 4 incomplete acquisition, 5 local runtime/database failure.

Add an installed-wheel test using the existing isolated-environment helpers and pytest's `tmp_path`. Build and install the wheel, invoke the installed `strata-resolver` command against a copied pinned replay with `tmp_path / "resolver.sqlite3"`, and assert exit 0, valid JSON, the pinned SHA-256, slot-order names, and no checkout-path dependency. Also assert wheel metadata contains `beautifulsoup4` and `playwright`, and both console scripts exist.

- [ ] **Step 3: Run tests and verify RED**

Run: `uv run --project . pytest tests/strata/test_config.py tests/strata/test_cli.py tests/test_package.py tests/test_wheel.py -q -k "strata or package or config or cli"`

Expected: missing config/CLI, missing console-script declaration, and installed-wheel smoke failure.

- [ ] **Step 4: Implement settings and CLI composition**

Build a dedicated argparse parser with commands `inspect-replay`, `resolve-name`, `resolve-replay`, `cache status`, `cache purge`, and `doctor`. Inject `application_factory` for tests; the installed path uses the real cache/browser/http composition.

Write compact JSON with:

```python
json.dumps(document, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
```

Write progress to stderr through a small reporter port. Stable public errors contain code/message only. `--debug` may print the traceback to stderr. `cache purge --all` prints the resolved database path to stderr and requires an interactive literal `yes` unless `--yes` is present.

Add to `pyproject.toml`:

```toml
[project.scripts]
replay-analyzer = "generals_replay_analyzer.cli:main"
strata-resolver = "generals_replay_analyzer.strata.cli:main"
```

Document installation, `uv run playwright install chromium`, the three primary commands, JSON/exit behavior, cache location, offline use, and source limits in README.

- [ ] **Step 5: Verify GREEN**

Run: `uv run --project . pytest tests/strata/test_config.py tests/strata/test_cli.py tests/test_package.py tests/test_wheel.py -q -k "strata or package or config or cli"`

Expected: all pass.

- [ ] **Step 6: Commit Task 9**

```text
git add scripts/replay_analyzer/src/generals_replay_analyzer/strata/config.py scripts/replay_analyzer/src/generals_replay_analyzer/strata/cli.py scripts/replay_analyzer/tests/strata/test_config.py scripts/replay_analyzer/tests/strata/test_cli.py scripts/replay_analyzer/pyproject.toml scripts/replay_analyzer/README.md scripts/replay_analyzer/tests/test_package.py scripts/replay_analyzer/tests/test_wheel.py
git commit -m "feat(cli): Add standalone Strata resolver command"
```

---

### Task 10: Opt-In Live Smoke and Final Verification

**Files:**
- Create: `scripts/replay_analyzer/tests/strata/test_live.py`
- Modify: `scripts/replay_analyzer/pyproject.toml`
- Modify: `scripts/replay_analyzer/README.md`

**Interfaces:**
- Consumes: installed `strata-resolver` console script and real public Strata pages.
- Produces: marker `strata_live` for bounded opt-in source-contract validation.

- [ ] **Step 1: Add opt-in live tests**

Declare the marker:

```toml
"strata_live: performs a bounded read-only check against public Strata pages",
```

The live test performs exactly:

1. one `fish` player search with `amount=100`, stopping after the first page while asserting `complete=False` for the deliberately capped probe;
2. one profile fetch for player 17945 and assertion that `fish` count is 8;
3. one match fetch for 3133811 and assertion of IDs 27965 and 102894.

It uses the production limiter/cache, skips with a precise message when Chromium or network is unavailable, and never runs in the default suite.

- [ ] **Step 2: Run all focused resolver tests**

Run: `uv run --project . pytest tests/strata tests/test_package.py -q -m "not strata_live"`

Expected: all pass, live test deselected.

- [ ] **Step 3: Run parser and wheel regressions**

Run: `uv run --project . pytest tests/test_binary.py tests/test_header.py tests/test_parser.py tests/test_provenance.py tests/test_package.py tests/test_wheel.py -q`

Expected: all pass.

- [ ] **Step 4: Run complete bounded non-external suite**

Run: `uv run --project . pytest -q -m "not browser and not engine and not ollama and not strata_live"`

Expected: exit 0 with no failed tests. Use an explicit process timeout in the execution harness; do not repeat a stalled full run without diagnosing process ownership and resource state.

- [ ] **Step 5: Run Ruff and strict mypy**

Run: `uv run --project . ruff check src tests/strata tests/test_package.py tests/test_wheel.py`

Run: `uv run --project . mypy src`

Expected: both exit 0.

- [ ] **Step 6: Build the wheel and smoke the installed command**

Run: `uv build --wheel --out-dir dist/strata-resolver-verification`

Install into an isolated temporary virtual environment, invoke `strata-resolver --help`, then run `inspect-replay` on a copied fixture with a cache path below the verification temporary directory. Expected: exit 0 and valid JSON from the installed wheel. The wheel must include the `strata` Python package automatically through the existing Hatch package root; include no test HTML or frozen fixtures as runtime data.

- [ ] **Step 7: Provision Chromium and run live smoke**

Run: `uv run --project . playwright install chromium`

Run: `uv run --project . pytest tests/strata/test_live.py -q -m strata_live`

Expected: all three bounded source checks pass. If network or installation is sandbox-blocked, request the required permission once and rerun only this bounded command.

- [ ] **Step 8: Manually verify required outputs**

Run with an isolated cache created for this verification:

```powershell
$resolverVerification = Join-Path ([System.IO.Path]::GetTempPath()) 'strata-resolver-final'
New-Item -ItemType Directory -Force -Path $resolverVerification | Out-Null
$resolverCache = Join-Path $resolverVerification 'resolver.sqlite3'
strata-resolver resolve-name fish --pretty --cache $resolverCache
strata-resolver resolve-replay tests/fixtures/zero_hour_1_04/leex279_vs_fox27.rep --pretty --cache $resolverCache
```

Verify line by line:

- name result is `ambiguous`;
- selected player is 17945 with count 8;
- exact alternatives are 6522/count 3 and 30124/count 2;
- case-insensitive suggestions include 30427 and 52866 separately;
- replay result selects match 3133811 only after evidence verification;
- replay slots map to 27965 and 102894;
- replay-local IDs remain separate from external IDs;
- all alternatives and evidence remain present.

- [ ] **Step 9: Commit Task 10**

```text
git add scripts/replay_analyzer/tests/strata/test_live.py scripts/replay_analyzer/pyproject.toml scripts/replay_analyzer/README.md
git commit -m "test(identity): Verify installed Strata resolver"
```

- [ ] **Step 10: Final diff and requirement audit**

Run:

```text
git status --short
git diff --check e41f436f8..HEAD
git log --oneline e41f436f8..HEAD
```

Confirm only resolver/spec/plan files and explicitly listed package/docs/tests changed in these commits. Re-read all 15 acceptance criteria in the design and record the exact command or result proving each one. Do not stage, rewrite, or commit unrelated pre-existing worktree changes.
