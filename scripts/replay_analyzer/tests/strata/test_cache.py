from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from generals_replay_analyzer.strata.cache import CachePathError, ResolverCache, SchemaVersionError
from generals_replay_analyzer.strata.contracts import Confidence, NameResolution, QueryName, ResolutionStatus
from generals_replay_analyzer.strata.extract import extract_match, extract_profile

FIXTURES = Path("tests/fixtures/strata")
NOW = datetime(2026, 8, 27, 20, 32, 43, tzinfo=UTC)


@dataclass
class MutableClock:
    value: datetime = NOW

    def now(self) -> datetime:
        return self.value


def _profile(player_id: int):  # type: ignore[no-untyped-def]
    html = (FIXTURES / f"player-{player_id}.html").read_text(encoding="utf-8")
    return extract_profile(html, player_id)


def _match():  # type: ignore[no-untyped-def]
    return extract_match((FIXTURES / "match-3133811.html").read_text(encoding="utf-8"), 3133811)


def _ambiguous_resolution() -> NameResolution:
    return NameResolution(
        query=QueryName("fish", "fish", "fish"),
        status=ResolutionStatus.AMBIGUOUS,
        selected=None,
        alternatives=(),
        case_insensitive_suggestions=(),
        fuzzy_suggestions=(),
        confidence=Confidence.MEDIUM,
        needs_replay_context=True,
        search_complete=True,
        checked_at=NOW,
    )


def test_exact_alias_can_belong_to_multiple_strata_players(tmp_path: Path) -> None:
    with ResolverCache(tmp_path / "resolver.sqlite3", clock=MutableClock()) as cache:
        cache.put_profile(_profile(17945), fetched_at=NOW, expires_at=NOW + timedelta(days=7))
        cache.put_profile(_profile(6522), fetched_at=NOW, expires_at=NOW + timedelta(days=7))

        assert [row.player_id for row in cache.aliases_exact("fish")] == [17945, 6522]


def test_profile_replacement_is_transactional_and_removes_old_aliases(tmp_path: Path) -> None:
    with ResolverCache(tmp_path / "resolver.sqlite3", clock=MutableClock()) as cache:
        cache.put_profile(_profile(17945), fetched_at=NOW, expires_at=NOW + timedelta(days=7))
        cache.put_profile(_profile(30124), fetched_at=NOW, expires_at=NOW + timedelta(days=7))
        replacement = _profile(30124)
        cache.put_profile(replacement, fetched_at=NOW + timedelta(hours=1), expires_at=NOW + timedelta(days=8))

        entry = cache.get_profile("strata.gamereplays.org", "zh", 30124)
        assert entry is not None and entry.value == replacement
        assert entry.fetched_at == NOW + timedelta(hours=1)


def test_expired_purge_keeps_resolution_audit(tmp_path: Path) -> None:
    clock = MutableClock()
    with ResolverCache(tmp_path / "resolver.sqlite3", clock=clock) as cache:
        cache.put_search(
            "fish",
            (17945, 6522),
            complete=True,
            fetched_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
        audit_id = cache.append_resolution(None, 0, _ambiguous_resolution(), {"search_complete": True})
        clock.value = NOW + timedelta(hours=2)

        result = cache.purge_expired()

        assert result.searches == 1
        assert cache.get_search("strata.gamereplays.org", "zh", "fish") is None
        assert cache.audit(audit_id, 0) is not None


def test_incomplete_and_negative_search_results_remain_explicit(tmp_path: Path) -> None:
    with ResolverCache(tmp_path / "resolver.sqlite3", clock=MutableClock()) as cache:
        cache.put_search("fish", (17945,), complete=False, fetched_at=NOW, expires_at=NOW + timedelta(hours=24))
        cache.put_search("nobody", (), complete=True, fetched_at=NOW, expires_at=NOW + timedelta(hours=1))

        partial = cache.get_search("strata.gamereplays.org", "zh", "fish")
        negative = cache.get_search("strata.gamereplays.org", "zh", "nobody")
        assert partial is not None and partial.complete is False and partial.candidate_ids == (17945,)
        assert negative is not None and negative.complete is True and negative.candidate_ids == ()


def test_match_cache_preserves_participant_source_order(tmp_path: Path) -> None:
    with ResolverCache(tmp_path / "resolver.sqlite3", clock=MutableClock()) as cache:
        cache.put_match(_match(), fetched_at=NOW, expires_at=NOW + timedelta(days=30))

        entry = cache.get_match("strata.gamereplays.org", "zh", 3133811)

        assert entry is not None
        assert [participant.player_id for participant in entry.value.participants] == [27965, 102894]


def test_database_pragmas_indexes_and_append_only_triggers(tmp_path: Path) -> None:
    with ResolverCache(tmp_path / "resolver.sqlite3", clock=MutableClock()) as cache:
        audit_id = cache.append_resolution("A" * 64, 0, _ambiguous_resolution(), {"source": "test"})
        connection = cache._connection
        assert connection is not None
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {
            "ix_strata_alias_raw",
            "ix_strata_alias_nfc",
            "ix_strata_alias_casefold",
            "ix_strata_match_time",
            "ix_strata_match_player",
            "ix_resolution_replay_slot",
        } <= indexes
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute(
                "UPDATE identity_resolutions SET status='resolved' WHERE resolution_id=? AND slot_index=0",
                (audit_id,),
            )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute(
                "DELETE FROM identity_resolutions WHERE resolution_id=? AND slot_index=0",
                (audit_id,),
            )


def test_future_schema_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "resolver.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=2")
    connection.close()

    with pytest.raises(SchemaVersionError, match="newer"), ResolverCache(path, clock=MutableClock()):
        pass


@pytest.mark.parametrize("kind", ["relative", "directory", "missing_parent", "uri"])
def test_database_path_validation_fails_closed(tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    if kind == "relative":
        path = Path("resolver.sqlite3")
    elif kind == "directory":
        path = tmp_path
    elif kind == "missing_parent":
        path = tmp_path / "missing" / "resolver.sqlite3"
    else:
        path = Path("file:///resolver.sqlite3")

    with pytest.raises(CachePathError):
        ResolverCache(path, clock=MutableClock())
