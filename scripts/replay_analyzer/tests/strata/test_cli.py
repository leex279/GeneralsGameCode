from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from generals_replay_analyzer.strata.cli import main
from generals_replay_analyzer.strata.contracts import (
    Confidence,
    MatchKind,
    NameResolution,
    PlayerCandidate,
    QueryName,
    ResolutionStatus,
)

PINNED_REPLAY = Path("tests/fixtures/zero_hour_1_04/leex279_vs_fox27.rep")
NOW = datetime(2026, 8, 27, 20, 32, 43, tzinfo=UTC)


def _fish_result() -> NameResolution:
    def candidate(player_id: int, name: str, count: int) -> PlayerCandidate:
        return PlayerCandidate(
            source="strata.gamereplays.org",
            player_id=player_id,
            profile_url=f"https://strata.gamereplays.org/zh/player/{player_id}",
            most_known_name=name,
            matched_alias="fish",
            match_kind=MatchKind.EXACT,
            alias_occurrence_count=count,
            source_rank=0,
        )

    return NameResolution(
        query=QueryName("fish", "fish", "fish"),
        status=ResolutionStatus.AMBIGUOUS,
        selected=candidate(17945, "-DoMiNaToR-", 8),
        alternatives=(candidate(6522, "StockFish'", 3), candidate(30124, "fish", 2)),
        case_insensitive_suggestions=(),
        fuzzy_suggestions=(),
        confidence=Confidence.MEDIUM,
        needs_replay_context=True,
        search_complete=True,
        checked_at=NOW,
    )


class FakeResolver:
    def resolve_name(self, value: str, **kwargs: object) -> NameResolution:
        assert value == "fish"
        return _fish_result()


@contextmanager
def _application(settings):  # type: ignore[no-untyped-def]
    yield FakeResolver()


def test_resolve_name_writes_one_json_document_and_diagnostics_to_stderr(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(
        ["resolve-name", "fish", "--cache", str(tmp_path / "resolver.sqlite3")],
        application_factory=_application,
    )
    captured = capsys.readouterr()

    assert code == 3
    assert json.loads(captured.out)["status"] == "ambiguous"
    assert captured.out.count("\n") == 1
    assert "candidate profiles" in captured.err


def test_inspect_replay_never_emits_external_id(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(["inspect-replay", str(PINNED_REPLAY), "--cache", str(tmp_path / "resolver.sqlite3")])
    document = json.loads(capsys.readouterr().out)

    assert code == 0
    assert document["slots"][0]["slot_index"] == 0
    assert document["slots"][0]["player_index"] is None
    assert "strata_player_id" not in document["slots"][0]


def test_invalid_utf8_name_file_is_a_stable_invalid_request(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    name_file = tmp_path / "name.txt"
    name_file.write_bytes(b"\xff")

    code = main(
        ["resolve-name", "--name-file", str(name_file), "--cache", str(tmp_path / "resolver.sqlite3")],
        application_factory=_application,
    )
    document = json.loads(capsys.readouterr().out)

    assert code == 2
    assert document == {"error": {"code": "invalid_name_file", "message": "name file must be strict UTF-8"}, "status": "invalid"}


def test_offline_and_refresh_are_rejected_before_application_creation(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    called = False

    @contextmanager
    def factory(settings):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        yield FakeResolver()

    code = main(
        [
            "resolve-name",
            "fish",
            "--offline",
            "--refresh",
            "--cache",
            str(tmp_path / "resolver.sqlite3"),
        ],
        application_factory=factory,
    )

    assert code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "offline_refresh_conflict"
    assert called is False


def test_cache_status_and_expired_purge_are_standalone(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    cache_path = tmp_path / "resolver.sqlite3"

    assert main(["cache", "status", "--cache", str(cache_path)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["schema_version"] == 1
    assert main(["cache", "purge", "--cache", str(cache_path)]) == 0
    purged = json.loads(capsys.readouterr().out)
    assert purged["scope"] == "expired"
