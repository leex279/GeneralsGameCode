"""Dedicated SQLite cache and append-only audit for Strata identity evidence."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Generic, Protocol, Self, TypeVar, cast
from uuid import uuid4

from .contracts import (
    AliasRecord,
    JsonValue,
    MatchDocument,
    MatchParticipantDocument,
    NameResolution,
    ProfileAliasDocument,
    ProfileDocument,
)

SOURCE = "strata.gamereplays.org"
GAME = "zh"
SCHEMA_VERSION = 1
T = TypeVar("T")


class CachePathError(ValueError):
    """Raised when the cache path could escape the dedicated resolver database."""


class SchemaVersionError(RuntimeError):
    """Raised when a database was created by a newer resolver version."""


class ClockLike(Protocol):
    def now(self) -> datetime: ...


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ValueCacheEntry(Generic[T]):
    value: T
    fetched_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SearchCacheEntry:
    source: str
    game: str
    query_name_raw: str
    candidate_ids: tuple[int, ...]
    complete: bool
    fetched_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class MatchPageCacheEntry:
    source: str
    game: str
    player_id: int
    page_number: int
    match_ids: tuple[int, ...]
    complete: bool
    fetched_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CacheStatus:
    path: Path
    schema_version: int
    players: int
    aliases: int
    searches: int
    matches: int
    match_pages: int
    audit_rows: int
    bytes_on_disk: int


@dataclass(frozen=True, slots=True)
class PurgeResult:
    players: int
    searches: int
    matches: int
    match_pages: int


def _encode_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cache timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decode_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _canonical_json(value: object) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _validate_path(path: Path) -> Path:
    text = str(path)
    if not path.is_absolute() or "://" in text or text.startswith("file:"):
        raise CachePathError("resolver cache path must be an absolute filesystem path")
    parent = path.parent
    if not parent.exists() or not parent.is_dir() or parent.is_symlink():
        raise CachePathError("resolver cache parent must be an existing real directory")
    if path.exists():
        if path.is_dir() or path.is_symlink():
            raise CachePathError("resolver cache path must be a regular file")
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise CachePathError("resolver cache path must not be a reparse point")
    return path


class ResolverCache:
    """Own one isolated resolver database with transactional cache replacement."""

    def __init__(self, path: Path, clock: ClockLike | None = None) -> None:
        self.path = _validate_path(path)
        self.clock = clock or _SystemClock()
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> Self:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version > SCHEMA_VERSION:
            connection.close()
            raise SchemaVersionError(
                f"resolver cache schema {current_version} is newer than supported schema {SCHEMA_VERSION}"
            )
        self._connection = connection
        if current_version == 0:
            self._create_schema()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("resolver cache is not open")
        return self._connection

    def _create_schema(self) -> None:
        # TheSuperHackers @feature Leex 27/08/2026 Keep crawled aliases and immutable resolution evidence outside the product database.
        self._db().executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE strata_players(
                source TEXT NOT NULL, game TEXT NOT NULL, player_id INTEGER NOT NULL,
                profile_url TEXT NOT NULL, most_known_name TEXT NOT NULL,
                fetched_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                PRIMARY KEY(source, game, player_id)
            );
            CREATE TABLE strata_player_aliases(
                source TEXT NOT NULL, game TEXT NOT NULL, player_id INTEGER NOT NULL,
                alias_raw TEXT NOT NULL, alias_nfc TEXT NOT NULL, alias_casefold TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL CHECK(occurrence_count >= 0),
                source_rank INTEGER NOT NULL CHECK(source_rank >= 0),
                fetched_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                PRIMARY KEY(source, game, player_id, alias_raw),
                FOREIGN KEY(source, game, player_id) REFERENCES strata_players(source, game, player_id) ON DELETE CASCADE
            );
            CREATE TABLE strata_searches(
                source TEXT NOT NULL, game TEXT NOT NULL, query_name_raw TEXT NOT NULL,
                candidate_ids_json TEXT NOT NULL, complete INTEGER NOT NULL CHECK(complete IN (0, 1)),
                fetched_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                PRIMARY KEY(source, game, query_name_raw)
            );
            CREATE TABLE strata_matches(
                source TEXT NOT NULL, game TEXT NOT NULL, match_id INTEGER NOT NULL,
                match_url TEXT NOT NULL, played_start_utc TEXT NOT NULL, played_end_utc TEXT NOT NULL,
                map_id INTEGER, map_name TEXT NOT NULL, match_type TEXT NOT NULL,
                duration_seconds INTEGER NOT NULL CHECK(duration_seconds >= 0), starting_cash INTEGER,
                game_version TEXT, data_pack TEXT, fetched_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                PRIMARY KEY(source, game, match_id)
            );
            CREATE TABLE strata_match_players(
                source TEXT NOT NULL, game TEXT NOT NULL, match_id INTEGER NOT NULL,
                source_rank INTEGER NOT NULL CHECK(source_rank >= 0), player_id INTEGER NOT NULL,
                displayed_name TEXT NOT NULL, faction TEXT NOT NULL, result TEXT NOT NULL, replay_url TEXT,
                replay_sha256 TEXT, command_stream_sha256 TEXT, match_signature_sha256 TEXT,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY(source, game, match_id, source_rank),
                FOREIGN KEY(source, game, match_id) REFERENCES strata_matches(source, game, match_id) ON DELETE CASCADE
            );
            CREATE TABLE strata_player_match_pages(
                source TEXT NOT NULL, game TEXT NOT NULL, player_id INTEGER NOT NULL,
                page_number INTEGER NOT NULL CHECK(page_number > 0), match_ids_json TEXT NOT NULL,
                complete INTEGER NOT NULL CHECK(complete IN (0, 1)),
                fetched_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                PRIMARY KEY(source, game, player_id, page_number)
            );
            CREATE TABLE identity_resolutions(
                resolution_id TEXT NOT NULL, replay_sha256 TEXT, slot_index INTEGER NOT NULL,
                player_index INTEGER, query_name_raw TEXT NOT NULL, selected_player_id INTEGER,
                status TEXT NOT NULL, confidence TEXT NOT NULL, evidence_json TEXT NOT NULL,
                resolved_at TEXT NOT NULL,
                PRIMARY KEY(resolution_id, slot_index)
            );
            CREATE INDEX ix_strata_alias_raw ON strata_player_aliases(source, game, alias_raw);
            CREATE INDEX ix_strata_alias_nfc ON strata_player_aliases(source, game, alias_nfc);
            CREATE INDEX ix_strata_alias_casefold ON strata_player_aliases(source, game, alias_casefold);
            CREATE INDEX ix_strata_match_time ON strata_matches(source, game, played_start_utc);
            CREATE INDEX ix_strata_match_player ON strata_match_players(source, game, player_id, match_id);
            CREATE INDEX ix_resolution_replay_sha256 ON identity_resolutions(replay_sha256);
            CREATE INDEX ix_resolution_replay_slot ON identity_resolutions(replay_sha256, slot_index);
            CREATE TRIGGER identity_resolutions_no_update
            BEFORE UPDATE ON identity_resolutions BEGIN SELECT RAISE(ABORT, 'identity_resolutions is append-only'); END;
            CREATE TRIGGER identity_resolutions_no_delete
            BEFORE DELETE ON identity_resolutions BEGIN SELECT RAISE(ABORT, 'identity_resolutions is append-only'); END;
            PRAGMA user_version=1;
            COMMIT;
            """
        )

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._db()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def put_search(
        self,
        query_name_raw: str,
        candidate_ids: tuple[int, ...],
        *,
        complete: bool,
        fetched_at: datetime,
        expires_at: datetime,
        source: str = SOURCE,
        game: str = GAME,
    ) -> None:
        with self._write() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO strata_searches
                (source, game, query_name_raw, candidate_ids_json, complete, fetched_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    source,
                    game,
                    query_name_raw,
                    _canonical_json(list(candidate_ids)),
                    int(complete),
                    _encode_time(fetched_at),
                    _encode_time(expires_at),
                ),
            )

    def get_search(self, source: str, game: str, query_name_raw: str) -> SearchCacheEntry | None:
        row = self._db().execute(
            "SELECT * FROM strata_searches WHERE source=? AND game=? AND query_name_raw=?",
            (source, game, query_name_raw),
        ).fetchone()
        if row is None:
            return None
        ids = tuple(int(value) for value in json.loads(row["candidate_ids_json"]))
        return SearchCacheEntry(source, game, query_name_raw, ids, bool(row["complete"]), _decode_time(row["fetched_at"]), _decode_time(row["expires_at"]))

    def put_profile(
        self,
        profile: ProfileDocument,
        *,
        fetched_at: datetime,
        expires_at: datetime,
        source: str = SOURCE,
        game: str = GAME,
    ) -> None:
        fetched = _encode_time(fetched_at)
        expires = _encode_time(expires_at)
        with self._write() as connection:
            connection.execute(
                "DELETE FROM strata_players WHERE source=? AND game=? AND player_id=?",
                (source, game, profile.player_id),
            )
            connection.execute(
                """INSERT INTO strata_players
                (source, game, player_id, profile_url, most_known_name, fetched_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (source, game, profile.player_id, profile.profile_url, profile.most_known_name, fetched, expires),
            )
            connection.executemany(
                """INSERT INTO strata_player_aliases
                (source, game, player_id, alias_raw, alias_nfc, alias_casefold,
                 occurrence_count, source_rank, fetched_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        source,
                        game,
                        profile.player_id,
                        alias.name_raw,
                        alias.name_nfc,
                        alias.name_casefold,
                        alias.occurrence_count,
                        alias.source_rank,
                        fetched,
                        expires,
                    )
                    for alias in profile.aliases
                ],
            )

    def get_profile(self, source: str, game: str, player_id: int) -> ValueCacheEntry[ProfileDocument] | None:
        row = self._db().execute(
            "SELECT * FROM strata_players WHERE source=? AND game=? AND player_id=?",
            (source, game, player_id),
        ).fetchone()
        if row is None:
            return None
        alias_rows = self._db().execute(
            """SELECT * FROM strata_player_aliases
            WHERE source=? AND game=? AND player_id=? ORDER BY source_rank""",
            (source, game, player_id),
        ).fetchall()
        profile = ProfileDocument(
            player_id=player_id,
            profile_url=row["profile_url"],
            most_known_name=row["most_known_name"],
            aliases=tuple(
                ProfileAliasDocument(
                    name_raw=alias["alias_raw"],
                    name_nfc=alias["alias_nfc"],
                    name_casefold=alias["alias_casefold"],
                    occurrence_count=alias["occurrence_count"],
                    source_rank=alias["source_rank"],
                )
                for alias in alias_rows
            ),
        )
        return ValueCacheEntry(profile, _decode_time(row["fetched_at"]), _decode_time(row["expires_at"]))

    def aliases_exact(self, alias_raw: str, source: str = SOURCE, game: str = GAME) -> tuple[AliasRecord, ...]:
        rows = self._db().execute(
            """SELECT a.*, p.profile_url, p.most_known_name
            FROM strata_player_aliases a JOIN strata_players p
              ON p.source=a.source AND p.game=a.game AND p.player_id=a.player_id
            WHERE a.source=? AND a.game=? AND a.alias_raw=?
            ORDER BY a.occurrence_count DESC, a.source_rank, a.player_id""",
            (source, game, alias_raw),
        ).fetchall()
        return tuple(
            AliasRecord(
                source=row["source"],
                player_id=row["player_id"],
                profile_url=row["profile_url"],
                most_known_name=row["most_known_name"],
                alias_raw=row["alias_raw"],
                alias_nfc=row["alias_nfc"],
                alias_casefold=row["alias_casefold"],
                occurrence_count=row["occurrence_count"],
                source_rank=row["source_rank"],
            )
            for row in rows
        )

    def put_match(
        self,
        match: MatchDocument,
        *,
        fetched_at: datetime,
        expires_at: datetime,
        source: str = SOURCE,
        game: str = GAME,
    ) -> None:
        fetched = _encode_time(fetched_at)
        with self._write() as connection:
            connection.execute(
                "DELETE FROM strata_matches WHERE source=? AND game=? AND match_id=?",
                (source, game, match.match_id),
            )
            connection.execute(
                """INSERT INTO strata_matches
                (source, game, match_id, match_url, played_start_utc, played_end_utc, map_id, map_name,
                 match_type, duration_seconds, starting_cash, game_version, data_pack, fetched_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source,
                    game,
                    match.match_id,
                    match.match_url,
                    _encode_time(match.played_start_utc),
                    _encode_time(match.played_end_utc),
                    match.map_id,
                    match.map_name,
                    match.match_type,
                    match.duration_seconds,
                    match.starting_cash,
                    match.game_version,
                    match.data_pack,
                    fetched,
                    _encode_time(expires_at),
                ),
            )
            connection.executemany(
                """INSERT INTO strata_match_players
                (source, game, match_id, source_rank, player_id, displayed_name, faction, result,
                 replay_url, replay_sha256, command_stream_sha256, match_signature_sha256, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)""",
                [
                    (
                        source,
                        game,
                        match.match_id,
                        participant.source_rank,
                        participant.player_id,
                        participant.displayed_name,
                        participant.faction,
                        participant.result,
                        participant.replay_url,
                        fetched,
                    )
                    for participant in match.participants
                ],
            )

    def get_match(self, source: str, game: str, match_id: int) -> ValueCacheEntry[MatchDocument] | None:
        row = self._db().execute(
            "SELECT * FROM strata_matches WHERE source=? AND game=? AND match_id=?",
            (source, game, match_id),
        ).fetchone()
        if row is None:
            return None
        participant_rows = self._db().execute(
            """SELECT * FROM strata_match_players
            WHERE source=? AND game=? AND match_id=? ORDER BY source_rank""",
            (source, game, match_id),
        ).fetchall()
        match = MatchDocument(
            match_id=match_id,
            match_url=row["match_url"],
            played_start_utc=_decode_time(row["played_start_utc"]),
            played_end_utc=_decode_time(row["played_end_utc"]),
            map_id=row["map_id"],
            map_name=row["map_name"],
            match_type=row["match_type"],
            duration_seconds=row["duration_seconds"],
            starting_cash=row["starting_cash"],
            game_version=row["game_version"],
            data_pack=row["data_pack"],
            participants=tuple(
                MatchParticipantDocument(
                    source_rank=participant["source_rank"],
                    player_id=participant["player_id"],
                    displayed_name=participant["displayed_name"],
                    faction=participant["faction"],
                    result=participant["result"],
                    replay_url=participant["replay_url"],
                )
                for participant in participant_rows
            ),
        )
        return ValueCacheEntry(match, _decode_time(row["fetched_at"]), _decode_time(row["expires_at"]))

    def put_match_page(
        self,
        player_id: int,
        page_number: int,
        match_ids: tuple[int, ...],
        *,
        complete: bool,
        fetched_at: datetime,
        expires_at: datetime,
        source: str = SOURCE,
        game: str = GAME,
    ) -> None:
        with self._write() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO strata_player_match_pages
                (source, game, player_id, page_number, match_ids_json, complete, fetched_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source,
                    game,
                    player_id,
                    page_number,
                    _canonical_json(list(match_ids)),
                    int(complete),
                    _encode_time(fetched_at),
                    _encode_time(expires_at),
                ),
            )

    def get_match_page(self, source: str, game: str, player_id: int, page_number: int) -> MatchPageCacheEntry | None:
        row = self._db().execute(
            """SELECT * FROM strata_player_match_pages
            WHERE source=? AND game=? AND player_id=? AND page_number=?""",
            (source, game, player_id, page_number),
        ).fetchone()
        if row is None:
            return None
        ids = tuple(int(value) for value in json.loads(row["match_ids_json"]))
        return MatchPageCacheEntry(
            source,
            game,
            player_id,
            page_number,
            ids,
            bool(row["complete"]),
            _decode_time(row["fetched_at"]),
            _decode_time(row["expires_at"]),
        )

    def append_resolution(
        self,
        replay_sha256: str | None,
        slot_index: int,
        result: NameResolution,
        evidence: Mapping[str, object],
    ) -> str:
        resolution_id = str(uuid4())
        selected_id = None if result.selected is None else result.selected.player_id
        payload = {"evidence": dict(evidence), "result": result.to_dict()}
        with self._write() as connection:
            connection.execute(
                """INSERT INTO identity_resolutions
                (resolution_id, replay_sha256, slot_index, player_index, query_name_raw, selected_player_id,
                 status, confidence, evidence_json, resolved_at)
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
                (
                    resolution_id,
                    replay_sha256,
                    slot_index,
                    result.query.raw,
                    selected_id,
                    result.status.value,
                    result.confidence.value,
                    _canonical_json(payload),
                    _encode_time(self.clock.now()),
                ),
            )
        return resolution_id

    def audit(self, resolution_id: str, slot_index: int) -> dict[str, JsonValue] | None:
        row = self._db().execute(
            "SELECT * FROM identity_resolutions WHERE resolution_id=? AND slot_index=?",
            (resolution_id, slot_index),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        del result["evidence_json"]
        result["evidence"] = json.loads(row["evidence_json"])
        return cast(dict[str, JsonValue], result)

    def purge_expired(self) -> PurgeResult:
        now = _encode_time(self.clock.now())
        with self._write() as connection:
            searches = connection.execute("DELETE FROM strata_searches WHERE expires_at <= ?", (now,)).rowcount
            players = connection.execute("DELETE FROM strata_players WHERE expires_at <= ?", (now,)).rowcount
            matches = connection.execute("DELETE FROM strata_matches WHERE expires_at <= ?", (now,)).rowcount
            pages = connection.execute("DELETE FROM strata_player_match_pages WHERE expires_at <= ?", (now,)).rowcount
        return PurgeResult(players=players, searches=searches, matches=matches, match_pages=pages)

    def status(self) -> CacheStatus:
        connection = self._db()
        counts = {
            "players": int(connection.execute("SELECT COUNT(*) FROM strata_players").fetchone()[0]),
            "aliases": int(connection.execute("SELECT COUNT(*) FROM strata_player_aliases").fetchone()[0]),
            "searches": int(connection.execute("SELECT COUNT(*) FROM strata_searches").fetchone()[0]),
            "matches": int(connection.execute("SELECT COUNT(*) FROM strata_matches").fetchone()[0]),
            "match_pages": int(connection.execute("SELECT COUNT(*) FROM strata_player_match_pages").fetchone()[0]),
            "audit_rows": int(connection.execute("SELECT COUNT(*) FROM identity_resolutions").fetchone()[0]),
        }
        return CacheStatus(
            path=self.path,
            schema_version=int(connection.execute("PRAGMA user_version").fetchone()[0]),
            players=counts["players"],
            aliases=counts["aliases"],
            searches=counts["searches"],
            matches=counts["matches"],
            match_pages=counts["match_pages"],
            audit_rows=counts["audit_rows"],
            bytes_on_disk=os.path.getsize(self.path),
        )
