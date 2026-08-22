"""Transactional normalization of immutable parser observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..binary import Coord3D, ICoord2D, IRegion2D
from ..commands import ReplayArgument
from ..commands import ReplayCommand as ParsedCommand
from ..db.models import (
    EvidenceItem,
    ManagedAsset,
    ParserRun,
    Replay,
    ReplayCommand,
    ReplayPlayer,
    ReplayQualityIssue,
)
from ..errors import ReplayParseError, UnsupportedArgumentTypeError
from ..parser import ParsedReplay
from .stages import canonical_json

_SHA256_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class ParserImportResult:
    """Public identity and outcome of one parser observation attempt."""

    run_id: str
    status: str
    completion_status: str | None
    command_count: int
    cache_hit: bool


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("parser import clock must return an aware datetime")
    return value.astimezone(UTC)


def _require_sha256(value: str) -> str:
    if len(value) != 64 or value != value.lower() or any(character not in _SHA256_HEX for character in value):
        raise ValueError("replay SHA-256 must be lowercase hexadecimal")
    return value


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json_value(value: object) -> object:
    if isinstance(value, Coord3D):
        return {"x": value.x, "y": value.y, "z": value.z}
    if isinstance(value, ICoord2D):
        return {"x": value.x, "y": value.y}
    if isinstance(value, IRegion2D):
        return {"lo": _json_value(value.lo), "hi": _json_value(value.hi)}
    return value


def _argument_json(index: int, argument: ReplayArgument) -> dict[str, object]:
    return {
        "argument_index": index,
        "type_value": argument.type.value,
        "type_name": argument.type.name,
        "value": _json_value(argument.value),
        "raw_bytes_hex": argument.raw_bytes.hex(),
    }


def _ordered_commands(parsed: ParsedReplay) -> tuple[ParsedCommand, ...]:
    """Canonicalize parser collection presentation without changing byte-stream identity."""
    return tuple(
        sorted(
            parsed.commands,
            key=lambda command: (
                command.start_offset,
                command.end_offset,
                command.frame,
                command.player_index,
                command.message_type,
            ),
        )
    )


def _validate_commands(parsed: ParsedReplay) -> None:
    if parsed.completion_status not in {"complete", "truncated"}:
        raise ValueError("unsupported parser completion status")
    if parsed.command_stream_offset < parsed.setup.end_offset or parsed.end_offset < parsed.command_stream_offset:
        raise ValueError("parser stream boundaries are invalid")
    prior_end = parsed.command_stream_offset
    for command in _ordered_commands(parsed):
        if command.frame < 0 or command.player_index < 0 or command.message_type < 0:
            raise ValueError("parser command numeric identity is negative")
        if command.start_offset < prior_end or command.end_offset <= command.start_offset:
            raise ValueError("parser command byte boundaries are invalid")
        if command.end_offset > parsed.end_offset:
            raise ValueError("parser command exceeds the trustworthy end offset")
        prior_end = command.end_offset


def _result_projection(parsed: ParsedReplay) -> dict[str, object]:
    return {
        "header": parsed.header.to_dict(),
        "setup": parsed.setup.to_dict(),
        "command_stream_offset": parsed.command_stream_offset,
        "end_offset": parsed.end_offset,
        "completion_status": parsed.completion_status,
        "warnings": [warning.to_dict() for warning in parsed.warnings],
        "commands": [
            {
                "command_index": index,
                "frame": command.frame,
                "player_index": command.player_index,
                "message_type": command.message_type,
                "message_name": command.message_name,
                "start_offset": command.start_offset,
                "end_offset": command.end_offset,
                "arguments": [_argument_json(argument_index, argument) for argument_index, argument in enumerate(command.arguments)],
            }
            for index, command in enumerate(_ordered_commands(parsed))
        ],
    }


# TheSuperHackers @feature Leex 22/08/2026 Import each parser observation graph atomically with immutable evidence keys. (#TBD)
class ParserObservationImporter:
    """Re-parse one managed replay and commit its observed prefix as one graph."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        data_root: Path,
        *,
        parser: Callable[[Path], ParsedReplay],
        parser_version: str,
        schema_version: int,
        clock: Callable[[], datetime],
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if not parser_version or schema_version < 0:
            raise ValueError("parser version and schema version are required")
        self._session_factory = session_factory
        self._data_root = data_root.resolve(strict=False)
        self._parser = parser
        self._parser_version = parser_version
        self._schema_version = schema_version
        self._clock = clock
        self._uuid_factory = uuid_factory

    def import_replay(
        self,
        replay_sha256: str,
        *,
        parser_version: str | None = None,
        idempotency_key: str | None = None,
    ) -> ParserImportResult:
        sha256 = _require_sha256(replay_sha256)
        version = parser_version or self._parser_version
        if not version:
            raise ValueError("parser version must be nonempty")
        if idempotency_key is not None and not idempotency_key:
            raise ValueError("import observation idempotency key must be nonempty")
        cached = self._successful_run(sha256, version)
        if cached is not None:
            return ParserImportResult(
                cached.run_id,
                cached.status,
                cached.completion_status,
                self._command_count(cached.id),
                True,
            )
        if idempotency_key is not None:
            failed_cached = self._failed_import_cache(sha256, version, idempotency_key)
            if failed_cached is not None:
                return failed_cached
        run_id = str(self._uuid_factory())
        now = _utc(self._clock())
        replay_id = self._create_attempt(sha256, version, run_id, now)
        try:
            managed_path = self._verified_managed_replay(replay_id, sha256)
            parsed = self._parser(managed_path)
            _validate_commands(parsed)
            projection = _result_projection(parsed)
            result_sha256 = hashlib.sha256(canonical_json(projection).encode("utf-8")).hexdigest()
            self._commit_success(replay_id, run_id, parsed, projection, result_sha256, now)
        except Exception as error:  # noqa: BLE001 - the attempt shell must retain every parser/import failure.
            code = "parser_unsupported" if isinstance(error, UnsupportedArgumentTypeError) else "parser_failure"
            if isinstance(error, ValueError) and "unsupported" in str(error).lower():
                code = "parser_unsupported"
            self._commit_failure(replay_id, run_id, error, code, now, idempotency_key)
            return ParserImportResult(run_id, "failed", "unsupported" if code == "parser_unsupported" else "failed", 0, False)
        return ParserImportResult(run_id, "succeeded", parsed.completion_status, len(parsed.commands), False)

    # TheSuperHackers @feature Leex 22/08/2026 Retain an upstream terminal parser attempt without inventing observations. (#TBD)
    def record_failed_dependency(
        self,
        replay_sha256: str,
        *,
        parser_version: str,
        idempotency_key: str,
        error_code: str,
        error_message: str,
        error_details: Mapping[str, object],
    ) -> ParserImportResult:
        sha256 = _require_sha256(replay_sha256)
        if not parser_version or not idempotency_key or not error_code or not error_message:
            raise ValueError("failed parser dependency metadata is incomplete")
        unsupported = "unsupported" in error_code
        issue_code = "parser_unsupported" if unsupported else "parser_failure"
        error_json = json.loads(
            canonical_json(
                {
                    "type": "stage_dependency",
                    "code": error_code,
                    "message": error_message,
                    "details": dict(error_details),
                    "import_observations_idempotency_key": idempotency_key,
                }
            )
        )
        cached = self._failed_dependency_cache(
            sha256,
            parser_version,
            error_json,
            issue_code,
            error_code,
        )
        if cached is not None:
            return cached
        run_id = str(self._uuid_factory())
        now = _utc(self._clock())
        replay_id = self._create_attempt(sha256, parser_version, run_id, now)
        with self._session_factory.begin() as session:
            replay = session.get(Replay, replay_id)
            run = session.scalar(select(ParserRun).where(ParserRun.run_id == run_id))
            if replay is None or run is None or run.status != "running":
                raise RuntimeError("parser attempt shell disappeared while recording dependency failure")
            run.status = "failed"
            run.completion_status = "unsupported" if unsupported else "failed"
            run.error_json = error_json
            run.completed_at = now
            self._add_issue(
                session,
                replay,
                run,
                issue_code,
                "error",
                {"error_code": error_code},
                now,
            )
            if unsupported:
                replay.lifecycle_state = "unsupported"
            elif replay.lifecycle_state == "discovered":
                replay.lifecycle_state = "failed"
            replay.updated_at = now
        return ParserImportResult(
            run_id,
            "failed",
            "unsupported" if unsupported else "failed",
            0,
            False,
        )

    def _failed_dependency_cache(
        self,
        sha256: str,
        parser_version: str,
        error_json: object,
        issue_code: str,
        error_code: str,
    ) -> ParserImportResult | None:
        expected_completion = "unsupported" if issue_code == "parser_unsupported" else "failed"
        expected_error = canonical_json(error_json)
        with self._session_factory() as session:
            runs = session.scalars(
                select(ParserRun)
                .join(Replay, Replay.id == ParserRun.replay_id)
                .where(
                    Replay.sha256 == sha256,
                    ParserRun.parser_version == parser_version,
                    ParserRun.schema_version == self._schema_version,
                    ParserRun.input_sha256 == sha256,
                    ParserRun.status == "failed",
                )
                .order_by(ParserRun.id)
            )
            for run in runs:
                if (
                    run.completion_status != expected_completion
                    or run.result_sha256 is not None
                    or run.command_stream_offset is not None
                    or run.end_offset is not None
                    or run.warnings_json != []
                    or run.completed_at is None
                    or canonical_json(run.error_json) != expected_error
                ):
                    continue
                child_count = sum(
                    int(session.scalar(statement) or 0)
                    for statement in (
                        select(func.count()).select_from(ReplayPlayer).where(ReplayPlayer.parser_run_id == run.id),
                        select(func.count()).select_from(ReplayCommand).where(ReplayCommand.parser_run_id == run.id),
                        select(func.count()).select_from(EvidenceItem).where(EvidenceItem.parser_run_id == run.id),
                    )
                )
                if child_count != 0:
                    continue
                issues = list(
                    session.scalars(
                        select(ReplayQualityIssue).where(ReplayQualityIssue.parser_run_id == run.id)
                    )
                )
                if len(issues) != 1:
                    continue
                issue = issues[0]
                if (
                    issue.stage != "import_observations"
                    or issue.issue_code != issue_code
                    or issue.severity != "error"
                    or issue.details_json != {"error_code": error_code}
                    or issue.resolved_at is not None
                ):
                    continue
                # TheSuperHackers @bugfix Leex 22/08/2026 Reuse only the exact childless failed attempt for one final job key. (#TBD)
                return ParserImportResult(
                    run.run_id,
                    "failed",
                    expected_completion,
                    0,
                    True,
                )
        return None

    def _failed_import_cache(
        self,
        sha256: str,
        parser_version: str,
        idempotency_key: str,
    ) -> ParserImportResult | None:
        with self._session_factory() as session:
            runs = session.scalars(
                select(ParserRun)
                .join(Replay, Replay.id == ParserRun.replay_id)
                .where(
                    Replay.sha256 == sha256,
                    ParserRun.parser_version == parser_version,
                    ParserRun.schema_version == self._schema_version,
                    ParserRun.input_sha256 == sha256,
                    ParserRun.status == "failed",
                )
                .order_by(ParserRun.id)
            )
            for run in runs:
                error_json = run.error_json
                if not isinstance(error_json, dict) or set(error_json) != {
                    "code",
                    "import_observations_idempotency_key",
                    "type",
                }:
                    continue
                stable_code = error_json.get("code")
                error_type = error_json.get("type")
                if (
                    error_json.get("import_observations_idempotency_key") != idempotency_key
                    or not isinstance(stable_code, str)
                    or not stable_code
                    or not isinstance(error_type, str)
                    or not error_type
                    or run.completion_status not in {"failed", "unsupported"}
                    or run.result_sha256 is not None
                    or run.command_stream_offset is not None
                    or run.end_offset is not None
                    or run.warnings_json != []
                    or run.completed_at is None
                ):
                    continue
                child_count = sum(
                    int(session.scalar(statement) or 0)
                    for statement in (
                        select(func.count()).select_from(ReplayPlayer).where(ReplayPlayer.parser_run_id == run.id),
                        select(func.count()).select_from(ReplayCommand).where(ReplayCommand.parser_run_id == run.id),
                        select(func.count()).select_from(EvidenceItem).where(EvidenceItem.parser_run_id == run.id),
                    )
                )
                if child_count != 0:
                    continue
                issue_code = "parser_unsupported" if run.completion_status == "unsupported" else "parser_failure"
                issues = list(
                    session.scalars(
                        select(ReplayQualityIssue).where(ReplayQualityIssue.parser_run_id == run.id)
                    )
                )
                if len(issues) != 1:
                    continue
                issue = issues[0]
                if (
                    issue.stage != "import_observations"
                    or issue.issue_code != issue_code
                    or issue.severity != "error"
                    or issue.details_json != {"error_code": stable_code}
                    or issue.resolved_at is not None
                ):
                    continue
                # TheSuperHackers @bugfix Leex 22/08/2026 Reuse a local failed parser shell only for its final job key. (#TBD)
                return ParserImportResult(
                    run.run_id,
                    "failed",
                    run.completion_status,
                    0,
                    True,
                )
        return None

    def _successful_run(self, sha256: str, parser_version: str) -> ParserRun | None:
        with self._session_factory() as session:
            return session.scalar(
                select(ParserRun)
                .join(Replay, Replay.id == ParserRun.replay_id)
                .where(
                    Replay.sha256 == sha256,
                    ParserRun.parser_version == parser_version,
                    ParserRun.schema_version == self._schema_version,
                    ParserRun.input_sha256 == sha256,
                    ParserRun.status == "succeeded",
                )
            )

    def _command_count(self, parser_run_id: int) -> int:
        with self._session_factory() as session:
            return len(list(session.scalars(select(ReplayCommand.id).where(ReplayCommand.parser_run_id == parser_run_id))))

    def _create_attempt(self, sha256: str, parser_version: str, run_id: str, now: datetime) -> int:
        with self._session_factory.begin() as session:
            replay = session.scalar(select(Replay).where(Replay.sha256 == sha256))
            if replay is None:
                raise ValueError("replay identity is unavailable")
            session.add(
                ParserRun(
                    run_id=run_id,
                    replay_id=replay.id,
                    parser_version=parser_version,
                    schema_version=self._schema_version,
                    input_sha256=sha256,
                    result_sha256=None,
                    status="pending",
                    completion_status=None,
                    command_stream_offset=None,
                    end_offset=None,
                    warnings_json=[],
                    error_json=None,
                    started_at=now,
                    completed_at=None,
                )
            )
            session.flush()
            run = session.scalar(select(ParserRun).where(ParserRun.run_id == run_id))
            assert run is not None
            run.status = "running"
            return replay.id

    def _verified_managed_replay(self, replay_id: int, sha256: str) -> Path:
        with self._session_factory() as session:
            replay = session.get(Replay, replay_id)
            if replay is None or replay.managed_asset_id is None:
                raise ValueError("managed replay asset is unavailable")
            asset = session.get(ManagedAsset, replay.managed_asset_id)
            if asset is None or asset.kind != "replay" or asset.sha256 != sha256:
                raise ValueError("managed replay asset identity is invalid")
            path = self._data_root / Path(*asset.relative_path.split("/"))
        resolved = path.resolve(strict=True)
        if resolved != path or self._data_root not in resolved.parents:
            raise ValueError("managed replay path escapes the product data root")
        if _hash_file(resolved) != sha256:
            raise ValueError("managed replay content SHA-256 is invalid")
        return resolved

    def _commit_success(
        self,
        replay_id: int,
        run_id: str,
        parsed: ParsedReplay,
        projection: Mapping[str, object],
        result_sha256: str,
        now: datetime,
    ) -> None:
        with self._session_factory.begin() as session:
            replay = session.get(Replay, replay_id)
            run = session.scalar(select(ParserRun).where(ParserRun.run_id == run_id))
            if replay is None or run is None or run.status != "running":
                raise ValueError("parser attempt identity changed")
            self._reverify_managed_replay(session, replay, run.input_sha256)
            replay.replay_name = parsed.header.replay_name
            replay.version_string = parsed.header.version_string
            replay.version_number = parsed.header.version_number
            replay.frame_count = parsed.header.frame_count
            replay.start_time = parsed.header.start_time
            replay.end_time = parsed.header.end_time
            replay.exe_crc = parsed.header.exe_crc
            replay.ini_crc = parsed.header.ini_crc
            replay.map_crc = parsed.header.map_crc
            replay.map_name = parsed.header.map
            replay.seed = parsed.header.seed
            replay.starting_cash = parsed.header.starting_cash
            replay.header_json = {
                "header": cast(dict[str, Any], projection["header"]),
                "setup": cast(dict[str, Any], projection["setup"]),
            }
            run.result_sha256 = result_sha256
            run.completion_status = parsed.completion_status
            run.command_stream_offset = parsed.command_stream_offset
            run.end_offset = parsed.end_offset
            run.warnings_json = cast(list[Any], projection["warnings"])
            run.error_json = None
            run.completed_at = now
            session.flush()

            for slot in sorted(parsed.header.slots, key=lambda item: item.index):
                session.add(
                    ReplayPlayer(
                        public_id=str(self._uuid_factory()),
                        replay_id=replay.id,
                        parser_run_id=run.id,
                        player_id=None,
                        slot_index=slot.index,
                        slot_kind=slot.kind,
                        original_name=slot.name,
                        normalized_name=None,
                        player_index=None,
                        team_id=slot.team,
                        faction=None if slot.player_template is None else str(slot.player_template),
                        color=None if slot.color is None else str(slot.color),
                        start_position=slot.start_position,
                        result=None,
                        observed_json=slot.to_dict(),
                    )
                )
            session.flush()

            for command_index, command in enumerate(_ordered_commands(parsed)):
                evidence = EvidenceItem(
                    public_id=str(self._uuid_factory()),
                    replay_id=replay.id,
                    parser_run_id=run.id,
                    telemetry_run_id=None,
                    tier="observed",
                    source_kind="parser_command",
                    source_key=f"parser:{run.run_id}:command:{command_index}",
                    schema_version=run.schema_version,
                    created_at=now,
                )
                session.add(evidence)
                session.flush()
                session.add(self._command_row(run, replay, command_index, command, evidence.id))
            session.flush()
            if parsed.completion_status == "truncated":
                self._add_issue(session, replay, run, "parser_truncated", "warning", {"end_offset": parsed.end_offset}, now)
                if replay.lifecycle_state not in {"unsupported", "desynced", "engine_verified"}:
                    replay.lifecycle_state = "partial"
            elif replay.lifecycle_state in {"discovered", "failed"}:
                replay.lifecycle_state = "parsed"
            replay.updated_at = now
            run.status = "succeeded"
            session.flush()

    def _reverify_managed_replay(self, session: Session, replay: Replay, sha256: str) -> None:
        if replay.sha256 != sha256 or replay.managed_asset_id is None:
            raise ValueError("managed replay identity changed before parser commit")
        asset = session.get(ManagedAsset, replay.managed_asset_id)
        if asset is None or asset.kind != "replay" or asset.sha256 != sha256:
            raise ValueError("managed replay registration changed before parser commit")
        path = self._data_root / Path(*asset.relative_path.split("/"))
        try:
            resolved = path.resolve(strict=True)
            size = resolved.stat().st_size
        except OSError as error:
            raise ValueError("managed replay disappeared before parser commit") from error
        if resolved != path or self._data_root not in resolved.parents:
            raise ValueError("managed replay path changed before parser commit")
        if size != asset.size_bytes or _hash_file(resolved) != sha256:
            raise ValueError("managed replay bytes changed before parser commit")

    @staticmethod
    def _command_row(
        run: ParserRun,
        replay: Replay,
        command_index: int,
        command: ParsedCommand,
        evidence_item_id: int,
    ) -> ReplayCommand:
        return ReplayCommand(
            parser_run_id=run.id,
            replay_id=replay.id,
            replay_player_id=None,
            command_index=command_index,
            frame=command.frame,
            player_index=command.player_index,
            message_type=command.message_type,
            message_name=command.message_name,
            start_offset=command.start_offset,
            end_offset=command.end_offset,
            arguments_json=[_argument_json(index, argument) for index, argument in enumerate(command.arguments)],
            evidence_item_id=evidence_item_id,
        )

    def _commit_failure(
        self,
        replay_id: int,
        run_id: str,
        error: Exception,
        issue_code: str,
        now: datetime,
        idempotency_key: str | None,
    ) -> None:
        with self._session_factory.begin() as session:
            replay = session.get(Replay, replay_id)
            run = session.scalar(select(ParserRun).where(ParserRun.run_id == run_id))
            if replay is None or run is None:
                raise RuntimeError("parser attempt shell disappeared while recording failure")
            stable_code = error.code if isinstance(error, ReplayParseError) else issue_code
            run.status = "failed"
            run.completion_status = "unsupported" if issue_code == "parser_unsupported" else "failed"
            error_json: dict[str, object] = {"type": type(error).__name__, "code": stable_code}
            if idempotency_key is not None:
                error_json["import_observations_idempotency_key"] = idempotency_key
            run.error_json = error_json
            run.completed_at = now
            self._add_issue(session, replay, run, issue_code, "error", {"error_code": stable_code}, now)
            if issue_code == "parser_unsupported":
                replay.lifecycle_state = "unsupported"
            elif replay.lifecycle_state == "discovered":
                replay.lifecycle_state = "failed"
            replay.updated_at = now

    def _add_issue(
        self,
        session: Session,
        replay: Replay,
        run: ParserRun,
        code: str,
        severity: str,
        details: dict[str, object],
        now: datetime,
    ) -> None:
        existing = session.scalar(
            select(ReplayQualityIssue).where(
                ReplayQualityIssue.replay_id == replay.id,
                ReplayQualityIssue.parser_run_id == run.id,
                ReplayQualityIssue.stage == "import_observations",
                ReplayQualityIssue.issue_code == code,
            )
        )
        if existing is None:
            session.add(
                ReplayQualityIssue(
                    public_id=str(self._uuid_factory()),
                    replay_id=replay.id,
                    parser_run_id=run.id,
                    telemetry_run_id=None,
                    evidence_item_id=None,
                    stage="import_observations",
                    issue_code=code,
                    severity=severity,
                    details_json=details,
                    detected_at=now,
                    resolved_at=None,
                )
            )
