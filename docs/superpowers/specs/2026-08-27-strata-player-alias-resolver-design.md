# Strata Player Alias Resolver Design

**Status:** Approved

**Date:** 2026-08-27

**Branch:** `sync/replay-analyzer-v2-product-upstream-2026-08-23`

**Primary target:** Command & Conquer: Generals - Zero Hour 1.04 and compatible modern Zero Hour replays

## 1. Outcome

Build an operationally standalone command-line tool and Python library that:

1. Parses a Zero Hour `.rep` file with the existing strict replay parser.
2. Preserves every human player slot, embedded name, and replay-local identifier.
3. Discovers every Strata player whose Known Names contain the queried replay name.
4. Separates exact case-sensitive, Unicode-NFC case-insensitive, and fuzzy matches.
5. Correlates replay metadata with Strata match history and downloadable replay evidence.
6. Returns one selected candidate when ranking permits while retaining all alternatives and evidence.
7. Reports multiple name-only candidates as `ambiguous`, never as confirmed identities.
8. Stores bounded, refreshable source data and immutable resolution audit records in a dedicated SQLite database.

The installed command is `strata-resolver`. It ships in the existing `generals-replay-analyzer` Python distribution so it can reuse the verified parser, but it does not require the web application or the Replay Analyzer production database.

## 2. Evidence Boundary

A replay does not contain a Strata player ID. It contains replay-local player and slot identifiers plus embedded player names and match metadata. The resolver must keep these namespaces distinct:

- `slot_index`: the serialized GameInfo slot from `0` through `7`.
- `player_index`: the replay command-stream player index when it can be observed.
- `query_name_raw`: the exact embedded replay name after removing terminal NUL padding only.
- `strata_player_id`: an external candidate or resolved identity obtained from Strata evidence.

The tool must never label a Strata player ID as replay-derived. A filename containing a Strata match ID or source token is only a discovery hint. It cannot confirm an identity until page metadata and replay evidence agree.

## 3. Approaches Considered

### 3.1 Hybrid standalone resolver inside the existing package (selected)

Add an isolated `generals_replay_analyzer.strata` subsystem, a dedicated console entry point, and a dedicated SQLite cache. Use Playwright only for lazy-loaded listing pages and ordinary HTTP for server-rendered detail pages. Reuse the existing replay parser and expose a later opt-in adapter for the main product.

This approach preserves parser fidelity, is independently runnable, and prevents unreviewed external rankings from changing canonical player identity.

### 3.2 Direct integration with the Replay Analyzer database (rejected)

This would reduce storage duplication, but it would not be standalone and would put an external crawler in the same mutation boundary as canonical identity. The current identity schema also assumes one normalized alias owner, while Strata proves that an exact alias can belong to multiple players.

### 3.3 Separate package with a duplicated replay parser (rejected)

This would maximize packaging isolation at the cost of duplicating binary parsing, fixtures, and safety limits. The two parsers would drift and create conflicting replay facts.

## 4. Architecture

```text
.rep file ----> existing strict parser ----> ReplayContext
                                               |
name ------------------------------------------+
                                               v
                                  browser candidate discovery
                                  Playwright, lazy listings
                                               |
                                               v
                                  HTTP profile/match enrichment
                                  server-rendered details
                                               |
                                               v
                                  deterministic evidence scorer
                                               |
                                               v
                                  JSON result + SQLite audit/cache
```

The subsystem has the following responsibilities:

- `replay_context`: adapts the existing parser output into resolver metadata and fingerprints.
- `normalization`: preserves the raw query and derives NFC and case-folded comparison values.
- `browser`: collects paginated player and match links from Livewire-rendered listing pages.
- `http`: fetches bounded server-rendered profiles, match pages, and replay bytes.
- `extract`: parses Known Names, match details, participants, and replay links from frozen HTML.
- `cache`: owns the dedicated SQLite schema, transactions, TTL decisions, and immutable audit rows.
- `matching`: partitions aliases, correlates matches, ranks candidates, and assigns status/confidence.
- `contracts`: defines stable JSON-serializable inputs, candidates, evidence, and results.
- `cli`: provides commands, exit codes, JSON stdout, and diagnostic stderr.

Browser and HTTP dependencies are injected behind ports so deterministic tests use frozen pages and real extraction code without contacting Strata.

## 5. Command-Line Interface

### 5.1 Inspect a replay

```text
strata-resolver inspect-replay <file.rep>
```

The command returns replay SHA-256, match-signature fingerprint, command-stream fingerprint, version, start/end times, frame count, calculated duration, map fields, local player index, and all slots in source order. Human slots include `slot_index`, nullable `player_index`, exact embedded name, faction/template, team, color, and start position.

### 5.2 Resolve one name

```text
strata-resolver resolve-name <name>
```

The command searches the complete candidate result set, fetches profiles, ranks alias matches, and emits the required name-only resolution contract. Shell encoding is preserved as Unicode input. The tool also accepts `--name-file` for names that are unsafe or impractical to express in a shell; the file must be UTF-8 and is limited to 4 KiB.

### 5.3 Resolve a replay

```text
strata-resolver resolve-replay <file.rep>
```

The command resolves all human slots as one match. It preserves slot order, discovers shared Strata match candidates, correlates match-level evidence, and returns both:

- a shared replay/match resolution containing nullable `strata_match_id`; and
- one player resolution per human slot containing replay-local IDs and external candidates.

### 5.4 Cache administration

```text
strata-resolver cache status
strata-resolver cache purge --expired
strata-resolver cache purge --all
```

`purge --all` deletes only the resolver database selected by the resolved configuration. It prints that absolute path to stderr before requesting confirmation. `--yes` is required for non-interactive deletion.

### 5.5 Common options

- `--cache PATH`: use an explicit dedicated SQLite path.
- `--offline`: use only cached source data and report incomplete evidence instead of contacting Strata.
- `--refresh`: ignore unexpired source cache entries while retaining audit history.
- `--pretty`: format JSON for human inspection; compact stable JSON is the default.
- `--include-fuzzy`: include fuzzy suggestions in a separate non-selectable bucket.
- `--browser chromium|chrome`: select the supported listing browser. Bundled Playwright Chromium is the default.
- `--timeout-seconds N`: set a bounded per-operation network timeout within validated limits.

Progress, retries, cache decisions, and diagnostics go to stderr. Stdout contains exactly one JSON document. Secret values, cookies, CSRF tokens, and private Livewire payloads are never logged.

## 6. Replay Context and Fingerprints

The existing parser remains the source of replay facts. The adapter computes:

### 6.1 Raw replay fingerprint

`replay_sha256` hashes the exact local bytes. Equality proves the same recorded file, including recorder-specific header values.

### 6.2 Command-stream fingerprint

`command_stream_sha256` hashes bytes from the parser-verified command-stream boundary through the verified end of stream. It is compared only when both files parse completely under a compatible parser contract. Equality is strong match evidence across recorder copies, but it is not assumed to be universal until fixture comparisons prove the property.

### 6.3 Match-signature fingerprint

`match_signature_sha256` hashes canonical JSON containing only replay-observed match fields:

- game and supported version;
- start time and end time;
- frame count;
- map path, map CRC, map size, and seed;
- starting cash;
- ordered human/AI slot kinds;
- exact human names, faction/template IDs, team IDs, colors, and start positions.

It excludes replay name, system clock milliseconds, local player index, IP addresses, ports, accepted/map-transfer flags, and filenames. A signature collision is contextual evidence, not cryptographic proof of player identity.

The resolver also exposes a human-readable `ReplayContext` with UTC timestamp candidates, participant multiset, map, player count/match type, duration candidates, and faction assignments.

## 7. Name Preservation and Matching

For every input name the resolver stores:

- `query_name_raw`: original string with terminal `\u0000` padding removed and no other stripping.
- `query_name_nfc`: Unicode NFC of the raw value.
- `query_name_casefold`: Unicode NFC, then case-fold, then NFC again.

Clan tags, punctuation, spaces, quotes, and non-ASCII characters are preserved. Empty names, embedded NULs after terminal-padding removal, unpaired surrogates, and names longer than 255 Unicode code points are rejected as invalid input.

Candidate aliases are partitioned in this strict order:

1. `exact`: `alias_raw == query_name_raw`.
2. `case_insensitive`: NFC plus case-fold equality, excluding exact matches.
3. `fuzzy`: optional similarity suggestions, excluding both earlier buckets.

Fuzzy suggestions use a deterministic standard-library similarity score over NFC case-folded values. They are never placed in `selected`, never auto-linked, and never contribute evidence that changes `status` to `resolved`.

The existing `normalize_embedded_name` function is not reused for Strata candidate partitioning because it strips whitespace, case-folds immediately, and assumes a canonical local alias namespace.

## 8. Strata Acquisition

### 8.1 Candidate search

The browser opens:

```text
https://strata.gamereplays.org/zh/players?search=<URL_ENCODED_RAW_NAME>&amount=100
```

It waits for the Livewire skeleton to become player rows or a verified empty state. It collects only links whose canonical path matches `/zh/player/<digits>`, deduplicates numeric IDs, and uses the visible Next control until exhausted.

The resolver never emulates private Livewire update requests. CSRF tokens, cookies, snapshots, checksums, and lazy-mount payloads remain browser implementation details.

### 8.2 Profile details

Profiles are fetched with ordinary HTTP. Extraction requires a numeric player ID, a Known Names section, and at least one valid `{name, count}` alias. `most_known_name` is the alias with maximum count; source order breaks ties. The visible search-row label is retained only as acquisition evidence and is not used as the most-known name.

### 8.3 Match discovery

Replay resolution uses the cheapest verified path first:

1. Treat a filename match ID as an untrusted hint and fetch that match detail directly.
2. Verify the hinted page against replay timestamp, participant names, map, player count/type, duration, and available replay fingerprints.
3. If the hint is absent or fails, search candidate player match histories with Playwright.
4. Paginate newest-to-oldest until the replay date window has been exhausted for every viable candidate.
5. Fetch deduplicated match details with ordinary HTTP and compare them as a shared match, not as independent player rows.

Match detail extraction retains match ID/URL, played UTC interval, map ID/name, match type, duration, starting cash, game/data versions when present, ordered participants, Strata player IDs, displayed match names, factions, results, and replay URLs.

### 8.4 Replay download evidence

The resolver downloads a candidate replay only after metadata makes the match viable. Each download is size-limited, read as a stream, hashed, and parsed without executing it. The resolver compares raw, command-stream, and match-signature fingerprints and stores only hashes and parsed metadata by default. Replay bytes are not retained after comparison.

## 9. Bounded Network Behavior

The default source policy is:

- descriptive user agent containing tool name, version, repository URL, and contact URL;
- one Playwright listing page active at a time;
- at most two concurrent ordinary HTTP requests;
- a minimum 500 ms interval between request starts per host;
- 20-second connect/read timeout per HTTP request;
- three attempts for transient failures using bounded exponential backoff with jitter;
- honor a valid `Retry-After` header up to 120 seconds;
- maximum 100 player-search pages, 10,000 candidate IDs, 200 match-history pages per candidate, and 20 MiB per replay download;
- fail with `incomplete` when a safety cap prevents exhaustion; never resolve from silently truncated evidence.

Default cache TTLs are:

- candidate-search results: 24 hours;
- profiles and aliases: 7 days;
- player match-list pages: 24 hours;
- match details: 30 days;
- negative/empty results: 1 hour.

TTL values are configuration fields with validated lower and upper bounds. HTTP 429 and 503 responses reduce concurrency to one for the remainder of the run. Repeated source failures open a run-local circuit breaker and produce incomplete evidence.

## 10. Dedicated SQLite Model

The resolver uses a separately named database under the platform-specific application data directory unless `--cache` is supplied.

```text
strata_players(
  source, game, player_id, profile_url, most_known_name,
  fetched_at, expires_at,
  PRIMARY KEY(source, game, player_id)
)

strata_player_aliases(
  source, game, player_id, alias_raw, alias_nfc, alias_casefold,
  occurrence_count, source_rank, fetched_at, expires_at,
  PRIMARY KEY(source, game, player_id, alias_raw)
)

strata_searches(
  source, game, query_name_raw, candidate_ids_json,
  complete, fetched_at, expires_at,
  PRIMARY KEY(source, game, query_name_raw)
)

strata_matches(
  source, game, match_id, match_url, played_start_utc, played_end_utc,
  map_id, map_name, match_type, duration_seconds, starting_cash,
  game_version, data_pack, fetched_at, expires_at,
  PRIMARY KEY(source, game, match_id)
)

strata_match_players(
  source, game, match_id, source_rank, player_id, displayed_name,
  faction, result, replay_url, replay_sha256, command_stream_sha256,
  match_signature_sha256, fetched_at,
  PRIMARY KEY(source, game, match_id, source_rank)
)

strata_player_match_pages(
  source, game, player_id, page_number, match_ids_json,
  complete, fetched_at, expires_at,
  PRIMARY KEY(source, game, player_id, page_number)
)

identity_resolutions(
  resolution_id, replay_sha256, slot_index, player_index,
  query_name_raw, selected_player_id, status, confidence,
  evidence_json, resolved_at,
  PRIMARY KEY(resolution_id, slot_index)
)
```

Indexes cover `alias_raw`, `alias_nfc`, `alias_casefold`, match time, match player ID, replay SHA-256, and `(replay_sha256, slot_index)`. Cache refreshes are transactional. Resolution audit rows are append-only and are not removed by `cache purge --expired`; `purge --all` explicitly removes the whole dedicated database.

## 11. Evidence Scoring and Resolution Policy

### 11.1 Name-only ranking

Within the exact bucket, rank by:

1. supplied replay-context score, if any;
2. matched alias occurrence count descending;
3. Strata profile source order;
4. numeric player ID ascending as a final deterministic ordering rule.

One exhausted exact candidate may be `resolved` with `medium` confidence. Multiple exact name-only candidates are always `ambiguous`, even when the highest alias count produces `selected`. A case-insensitive-only result is never resolved from name evidence alone. Fuzzy results never produce a selection.

### 11.2 Match-context evidence

Evidence is evaluated as independently recorded facts rather than one opaque score:

- exact raw replay SHA-256;
- exact command-stream SHA-256;
- exact match-signature SHA-256;
- replay timestamp within the Strata UTC interval or configured tolerance;
- exact participant-name multiset;
- candidate player IDs jointly cover the Strata participant set;
- map path/name agreement;
- player count and match-type agreement;
- duration agreement within five seconds by default;
- faction assignment agreement for every comparable human slot;
- exact opponent or FFA participant-set agreement.

Contradictory high-value facts, such as a different participant count, map, or command-stream fingerprint, eliminate a match candidate rather than merely lowering its score.

### 11.3 Resolution thresholds

- `high`: one unique Strata match is confirmed by an exact raw or command-stream fingerprint and its participant mapping is one-to-one.
- `medium`: one unique Strata match is confirmed by timestamp, complete participant set, map, player count/type, duration, and all available faction assignments with no contradictions.
- `low`: a best candidate exists from incomplete or case-insensitive evidence, but status remains `ambiguous` or `incomplete`.
- `none`: no viable candidate or source acquisition failed before evidence was collected.

A replay-context resolution changes a player to `resolved` only when the shared match uniquely maps that slot to one Strata player ID. Alternative name candidates remain in audit evidence and in the public result.

## 12. Output Contracts

### 12.1 Name resolution

The name command preserves the required contract and adds explicit comparison metadata:

```json
{
  "schema_version": "strata-name-resolution-v1",
  "query_name": "fish",
  "query_name_nfc": "fish",
  "status": "ambiguous",
  "selected": {
    "source": "strata.gamereplays.org",
    "player_id": 17945,
    "profile_url": "https://strata.gamereplays.org/zh/player/17945",
    "most_known_name": "-DoMiNaToR-",
    "matched_alias": "fish",
    "match_kind": "exact",
    "alias_occurrence_count": 8,
    "selection_reason": "highest occurrence count among exact case-sensitive alias matches"
  },
  "alternatives": [
    {
      "player_id": 6522,
      "profile_url": "https://strata.gamereplays.org/zh/player/6522",
      "most_known_name": "StockFish'",
      "matched_alias": "fish",
      "match_kind": "exact",
      "alias_occurrence_count": 3
    },
    {
      "player_id": 30124,
      "profile_url": "https://strata.gamereplays.org/zh/player/30124",
      "most_known_name": "fish",
      "matched_alias": "fish",
      "match_kind": "exact",
      "alias_occurrence_count": 2
    }
  ],
  "case_insensitive_suggestions": [
    {
      "player_id": 30427,
      "profile_url": "https://strata.gamereplays.org/zh/player/30427",
      "most_known_name": "Fish",
      "matched_alias": "Fish",
      "match_kind": "case_insensitive",
      "alias_occurrence_count": 149
    },
    {
      "player_id": 52866,
      "profile_url": "https://strata.gamereplays.org/zh/player/52866",
      "most_known_name": "Fish_3",
      "matched_alias": "Fish",
      "match_kind": "case_insensitive",
      "alias_occurrence_count": 8
    }
  ],
  "fuzzy_suggestions": [],
  "confidence": "medium",
  "needs_replay_context": true,
  "search_complete": true,
  "checked_at": "2026-08-27T20:32:43Z"
}
```

Case-insensitive candidates do not appear in `alternatives`, which remains the exact-candidate audit list. They appear in `case_insensitive_suggestions` so callers cannot mistake them for exact matches.

### 12.2 Replay resolution

The replay command returns:

- `schema_version`;
- `replay` metadata and fingerprints;
- `match_resolution` with status, confidence, selected Strata match, alternatives, and evidence;
- `players` in replay slot order;
- each player's replay-local IDs, exact query values, selected candidate, alternatives, suggestions, confidence, and evidence;
- `acquisition` completeness, cache use, caps reached, and checked time.

Statuses are `resolved`, `ambiguous`, `not_found`, `incomplete`, `unsupported`, or `invalid`. A transport or parser exception is mapped to a stable status/error object and a nonzero CLI exit code; tracebacks are shown only with `--debug`.

## 13. Failure and Safety Behavior

- Invalid or unsupported replay bytes fail before any network request.
- Symlinks, directories, devices, and replay files above the existing parser input limit are rejected by the existing ingress boundary.
- HTML parsers require expected structural evidence and fail closed when Strata changes markup.
- An empty page is accepted only when a verified empty-state marker replaces the skeleton.
- Pagination cycles, duplicate pages, missing Next transitions, and safety-cap exhaustion return incomplete acquisition.
- Profile IDs and match IDs must be decimal and must round-trip through canonical Strata URLs.
- Redirects are allowed only to approved HTTPS hosts and approved path shapes.
- Replay downloads are limited to the approved `matchdata.playgenerals.online` HTTPS host.
- Downloaded replay bytes are treated as untrusted input and are never launched.
- SQLite uses parameterized statements, foreign keys, WAL mode, a busy timeout, and explicit transactions.
- A database schema version newer than the tool is rejected instead of downgraded.
- Offline mode never reports fresh source confirmation.

## 14. Packaging and Configuration

The existing project declares a second console script:

```text
strata-resolver = generals_replay_analyzer.strata.cli:main
```

Playwright becomes a runtime dependency for the resolver feature. Installation documentation includes the one-time Chromium provisioning command and `strata-resolver doctor`, which verifies Python version, browser availability, writable cache location, SQLite schema, and HTTPS connectivity without crawling results.

Configuration precedence is command line, environment variables prefixed `STRATA_RESOLVER_`, then documented defaults. Credentials are not required. Proxy support uses standard HTTP proxy environment variables but does not inspect or log their values.

Every new architectural or user-facing code path includes the repository-required `TheSuperHackers` comment with an allowed keyword, author `Leex`, date `27/08/2026`, and the eventual pull-request reference.

## 15. Testing Strategy

Implementation follows strict test-driven development.

### 15.1 Deterministic unit tests

- query normalization, terminal NUL handling, Unicode NFC, case folding, and invalid Unicode;
- exact, case-insensitive, and fuzzy partitioning;
- alias-count ranking and deterministic tie-breaking;
- status/confidence thresholds and contradiction elimination;
- stable JSON output and slot ordering;
- cache TTL boundaries, migrations, append-only audit behavior, and purge scope;
- rate limiting, retry bounds, circuit breaker, redirect allowlists, and response size limits.

### 15.2 Frozen source-contract tests

Sanitized HTML fixtures cover:

- lazy player-search results, verified empty state, deduplication, and pagination;
- profile 17945 Known Names including `fish` count 8 and most-known name `-DoMiNaToR-`;
- exact `fish` alternatives 6522 and 30124;
- case-insensitive `Fish` candidates 30427 and 52866;
- player match listings;
- match 3133811 detail fields and both participant IDs;
- malformed, partial, redirected, and structurally changed pages.

Fixtures record source URL, capture date, SHA-256, and the minimal terms needed to reproduce the extraction contract. Tests exercise the real extractors, not source-text greps.

### 15.3 Replay integration tests

The checksum-pinned `leex279_vs_fox27.rep` fixture must produce:

- replay-local names `leex279` and `FOX27` in slots 0 and 1;
- match metadata matching Strata match 3133811;
- Strata player IDs 27965 and 102894 after context correlation;
- a unique shared match resolution;
- preserved alternative-candidate audit data.

Additional generated fixtures cover AI/open/closed slots, non-ASCII names, terminal padding, malformed files, and duration/timestamp tolerances.

### 15.4 Browser and live smoke tests

Default tests never crawl Strata. A separately marked, opt-in live suite launches installed Chromium and verifies one bounded player search, one profile fetch, and one match-detail fetch. It must use the production rate limiter and cache, and it skips with an explicit reason when network access is unavailable.

### 15.5 Verification gates

- focused resolver tests;
- existing parser and package regression tests;
- complete non-browser Python test suite with bounded execution;
- Ruff;
- strict mypy;
- wheel build and installed-console-script smoke test;
- opt-in live Strata smoke test;
- manual inspection of JSON for `resolve-name fish` and the pinned replay.

No C++ build is required because the approved implementation changes only the Python tool and documentation.

## 16. Acceptance Criteria

The feature is complete only when:

1. `strata-resolver` runs without the Replay Analyzer server or production database.
2. `inspect-replay` exposes replay-local identifiers and never invents a Strata ID.
3. `resolve-name fish` returns the three exact candidates in the required order and remains `ambiguous`.
4. Case-insensitive `Fish` candidates are visibly separate and cannot be selected as exact matches.
5. Candidate discovery and relevant match discovery exhaust pagination or return `incomplete`.
6. Profile titles and Known Names, not search-row labels, determine `most_known_name`.
7. `resolve-replay` parses all human slots and uses shared match context.
8. A unique matching Strata match maps each replay slot to the page's numeric player ID and can return `resolved`.
9. Exact and semantic replay fingerprints are retained as explicit evidence when available.
10. All name alternatives remain in public or audit evidence after context resolution.
11. Cache TTLs, rate limits, retry limits, request caps, redirect allowlists, and download limits are enforced.
12. Source markup drift and partial acquisition fail closed.
13. Deterministic tests do not require live network access.
14. The opt-in live smoke test validates the current acquisition path.
15. Existing replay parser behavior and package entry points remain passing.

## 17. Deferred Work

- Generals 1.08 and non-Zero-Hour Strata namespaces.
- Automatic writes into canonical Replay Analyzer player identities.
- A hosted multi-user crawler or shared central cache.
- Private Livewire protocol emulation.
- A public Strata API client until GameReplays publishes an API or export contract.
- Retaining or redistributing downloaded replay files.
