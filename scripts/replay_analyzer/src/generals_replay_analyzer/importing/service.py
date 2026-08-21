"""Transactional replay intake with source provenance and injected acquisition."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..config import AnalyzerSettings
from ..db.models import Job, ManagedAsset, Replay, Source
from ..parser import ParsedReplay
from ..provenance import SourceProvenance, extract_source_provenance
from ..storage import ContentAddressedStore, ContentStorageError, StoredContent
from .jobs import ClaimedJob, JobCoordinator, JobSnapshot, JobSpec, JobStateError, StageFailure
from .stages import (
    ANALYZE_LLM,
    ANALYZE_LLM_VERSION,
    ASSESS_STRATEGIES,
    ASSESS_STRATEGIES_VERSION,
    DERIVE_FEATURES,
    DERIVE_FEATURES_VERSION,
    DISCOVER,
    DISCOVER_VERSION,
    HASH,
    HASH_VERSION,
    IMPORT_OBSERVATIONS,
    IMPORT_OBSERVATIONS_VERSION,
    MANAGE_COPY,
    MANAGE_COPY_VERSION,
    PARSE,
    PARSE_VERSION,
    RENDER_REPORT,
    RENDER_REPORT_VERSION,
    TELEMETRY,
    TELEMETRY_VERSION,
    content_key,
)


@dataclass(frozen=True)
class AcquisitionDiagnostic:
    code: str
    message: str


@dataclass(frozen=True)
class TelemetryArtifact:
    run_id: str
    runner_status: str
    replay_quality: str
    strategy_analysis_scope: str
    trace_path: Path | None
    catalog_path: Path | None
    map_asset_paths: tuple[Path, ...]
    outcome_path: Path | None
    stdout_path: Path | None
    stderr_path: Path | None
    exit_code: int | None
    engine_build: str | None
    engine_executable_sha256: str | None
    diagnostics: tuple[AcquisitionDiagnostic, ...]


class TelemetryAcquirer(Protocol):
    def acquire(self, replay: Path, replay_sha256: str) -> TelemetryArtifact: ...


@dataclass(frozen=True)
class ImportRequest:
    path: Path
    recursive: bool = False
    reference_only: bool | None = None
    request_telemetry: bool = False


@dataclass(frozen=True)
class JobDTO:
    public_id: str
    replay_public_id: str | None
    stage: str
    component_version: str
    status: str
    attempt_count: int
    max_attempts: int
    retryable: bool
    error_code: str | None


@dataclass(frozen=True)
class ImportSubmissionDTO:
    discovery_job: JobDTO
    accepted_path_count: int
    rejected_path_count: int


@dataclass(frozen=True)
class ImportResultDTO:
    replay_public_id: str
    sha256: str
    lifecycle_state: str
    source_public_ids: tuple[str, ...]
    jobs: tuple[JobDTO, ...]


# TheSuperHackers @feature Leex 21/08/2026 Import replay bytes transactionally without coupling analytics to engine internals. (#TBD)
class ImportService:
    """Accept bounded snapshots and execute only registered, dependency-ready stages."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: AnalyzerSettings,
        replay_store: ContentAddressedStore,
        artifact_store: ContentAddressedStore,
        *,
        parser: Callable[[Path], ParsedReplay],
        telemetry_acquirer: TelemetryAcquirer | None = None,
        clock: Callable[[], datetime],
        parser_version: str,
        telemetry_acquirer_version: str,
    ) -> None:
        if not parser_version or not telemetry_acquirer_version:
            raise ValueError("component versions must be nonempty")
        self._session_factory = session_factory
        self._settings = settings
        self._replay_store = replay_store
        self._artifact_store = artifact_store
        self._parser = parser
        self._telemetry_acquirer = telemetry_acquirer
        self._parser_version = parser_version
        self._telemetry_acquirer_version = telemetry_acquirer_version
        self._jobs = JobCoordinator(session_factory, clock=clock)
        self._handlers: dict[str, Callable[[ClaimedJob], Mapping[str, Any]]] = {
            DISCOVER: self._discover,
            HASH: self._hash,
            MANAGE_COPY: self._manage_copy,
            PARSE: self._parse,
        }
        if telemetry_acquirer is not None:
            self._handlers[TELEMETRY] = self._telemetry

    def submit(self, request: ImportRequest) -> ImportSubmissionDTO:
        path = _absolute_without_following(request.path)
        _validate_submission_path(path)
        mode = "reference" if request.reference_only is True else self._settings.import_mode
        invocation_id = str(uuid4())
        snapshot = self._jobs.create_job(
            JobSpec(
                stage=DISCOVER,
                component_version=DISCOVER_VERSION,
                idempotency_key=f"{DISCOVER}:{DISCOVER_VERSION}:{invocation_id}",
                input_json={
                    "invocation_id": invocation_id,
                    "path": str(path),
                    "recursive": request.recursive,
                    "import_mode": mode,
                    "request_telemetry": request.request_telemetry,
                },
            )
        )
        return ImportSubmissionDTO(self._dto(snapshot), 0, 0)

    def run_available(self, worker_id: str, *, limit: int = 1) -> tuple[JobDTO, ...]:
        if limit < 1:
            raise ValueError("run limit must be positive")
        completed: list[JobDTO] = []
        for _ in range(limit):
            claimed = self._jobs.claim(worker_id, self._handlers)
            if claimed is None:
                break
            handler = self._handlers[claimed.stage]
            try:
                output = handler(claimed)
            except StageFailure as failure:
                snapshot = self._jobs.fail(claimed.public_id, worker_id, failure)
            except Exception as error:  # noqa: BLE001 - the durable worker boundary must retain unexpected failures.
                snapshot = self._jobs.fail(
                    claimed.public_id,
                    worker_id,
                    StageFailure(
                        "stage_failed",
                        f"{claimed.stage} failed: {error}",
                        retryable=True,
                        details={"exception_type": type(error).__name__},
                    ),
                )
            else:
                snapshot = self._jobs.succeed(claimed.public_id, worker_id, output)
            completed.append(self._dto(snapshot))
        return tuple(completed)

    def retry(self, job_public_id: str) -> JobDTO:
        return self._dto(self._jobs.retry(job_public_id))

    def result_for_replay(self, replay_public_id: str) -> ImportResultDTO:
        with self._session_factory() as session:
            replay = session.scalar(select(Replay).where(Replay.public_id == replay_public_id))
            if replay is None:
                raise JobStateError("unknown_replay", "unknown replay public ID")
            sources = tuple(
                session.scalars(select(Source.public_id).where(Source.replay_id == replay.id).order_by(Source.id))
            )
            rows = list(session.scalars(select(Job).where(Job.replay_id == replay.id).order_by(Job.id)))
            jobs = tuple(self._dto_from_row(session, row) for row in rows)
            return ImportResultDTO(replay.public_id, replay.sha256, replay.lifecycle_state, sources, jobs)

    @staticmethod
    def _dto(snapshot: JobSnapshot) -> JobDTO:
        return JobDTO(
            public_id=snapshot.public_id,
            replay_public_id=snapshot.replay_public_id,
            stage=snapshot.stage,
            component_version=snapshot.component_version,
            status=snapshot.status,
            attempt_count=snapshot.attempt_count,
            max_attempts=snapshot.max_attempts,
            retryable=snapshot.retryable,
            error_code=snapshot.error_code,
        )

    @staticmethod
    def _dto_from_row(session: Session, row: Job) -> JobDTO:
        replay_public_id = None
        if row.replay_id is not None:
            replay_public_id = session.scalar(select(Replay.public_id).where(Replay.id == row.replay_id))
        return JobDTO(
            row.public_id,
            replay_public_id,
            row.stage,
            row.component_version,
            row.status,
            row.attempt_count,
            row.max_attempts,
            row.retryable,
            row.error_code,
        )

    def _discover(self, claimed: ClaimedJob) -> Mapping[str, Any]:
        path = Path(cast(str, claimed.input_json["path"]))
        recursive = bool(claimed.input_json["recursive"])
        mode = cast(str, claimed.input_json["import_mode"])
        request_telemetry = bool(claimed.input_json["request_telemetry"])
        accepted_paths, diagnostics = _snapshot_replays(path, recursive)
        prepared: list[tuple[Path, os.stat_result, SourceProvenance]] = []
        for replay_path in accepted_paths:
            try:
                info = replay_path.lstat()
                if not stat.S_ISREG(info.st_mode) or replay_path.is_symlink() or _is_reparse(info):
                    raise OSError("entry is no longer an ordinary file")
                provenance = extract_source_provenance(replay_path)
                prepared.append((replay_path, info, provenance))
            except OSError as error:
                diagnostics.append(_diagnostic("source_unreadable", replay_path, str(error)))

        with self._session_factory.begin() as session:
            discovery = session.scalar(select(Job).where(Job.public_id == claimed.public_id))
            assert discovery is not None
            for replay_path, info, provenance in prepared:
                locator = str(_absolute_without_following(replay_path))
                source = self._find_discovery_source(session, claimed.public_id, locator)
                sha256 = provenance.sha256.lower()
                if source is None:
                    source = Source(
                        public_id=str(uuid4()),
                        replay_id=None,
                        source_kind="filesystem",
                        original_locator=locator,
                        original_filename=provenance.original_filename,
                        strata_match_id=provenance.strata_match_id,
                        strata_source_user_token=provenance.strata_source_user_token,
                        file_size_bytes=info.st_size,
                        source_modified_at=datetime.fromtimestamp(info.st_mtime, UTC),
                        discovered_at=self._jobs.now(),
                        provenance_json={
                            "content_sha256": sha256,
                            "discovery_job_public_id": claimed.public_id,
                            "import_mode": mode,
                            "request_telemetry": request_telemetry,
                        },
                    )
                    session.add(source)
                    session.flush()
                replay = session.scalar(select(Replay).where(Replay.sha256 == sha256))
                if replay is not None:
                    source.replay_id = replay.id
                    self._ensure_content_graph(session, replay, mode, request_telemetry)
                hash_spec = self._hash_spec(sha256)
                existing_hash = session.scalar(select(Job).where(Job.idempotency_key == hash_spec.idempotency_key))
                hash_job = self._jobs.ensure_job(session, hash_spec)
                if existing_hash is None or hash_job.status == "pending":
                    self._jobs.ensure_dependency(session, hash_job.id, discovery.id)

        return {
            "accepted_path_count": len(prepared),
            "rejected_path_count": len(diagnostics),
            "diagnostics": diagnostics,
        }

    @staticmethod
    def _find_discovery_source(session: Session, discovery_public_id: str, locator: str) -> Source | None:
        for source in session.scalars(select(Source).where(Source.original_locator == locator)):
            provenance = cast(Mapping[str, Any], source.provenance_json)
            if provenance.get("discovery_job_public_id") == discovery_public_id:
                return source
        return None

    @staticmethod
    def _hash_spec(replay_sha256: str) -> JobSpec:
        identity = {"hash_version": HASH_VERSION}
        return JobSpec(
            stage=HASH,
            component_version=HASH_VERSION,
            idempotency_key=content_key(HASH, HASH_VERSION, replay_sha256, identity),
            input_json={"replay_sha256": replay_sha256},
        )

    def _hash(self, claimed: ClaimedJob) -> Mapping[str, Any]:
        expected_sha256 = cast(str, claimed.input_json["replay_sha256"]).lower()
        candidates = self._unlinked_source_records(expected_sha256)
        source_path = _first_regular_path(source.original_locator for source in candidates)
        if source_path is None:
            raise StageFailure("source_missing", "no ordinary source remains for hashing", retryable=False)
        try:
            actual_sha256, size = _hash_file(source_path)
        except OSError as error:
            raise StageFailure("source_unreadable", str(error), retryable=True) from error
        if actual_sha256 != expected_sha256:
            raise StageFailure(
                "source_changed",
                "source bytes changed after discovery",
                retryable=False,
                details={"expected_sha256": expected_sha256, "actual_sha256": actual_sha256},
            )

        with self._session_factory.begin() as session:
            replay = session.scalar(select(Replay).where(Replay.sha256 == expected_sha256))
            if replay is None:
                representative = candidates[0]
                try:
                    with session.begin_nested():
                        replay = Replay(
                            public_id=str(uuid4()),
                            sha256=expected_sha256,
                            managed_asset_id=None,
                            map_id=None,
                            replay_name=representative.original_filename,
                            version_string="pending",
                            version_number=0,
                            frame_count=0,
                            start_time=0,
                            end_time=0,
                            exe_crc=0,
                            ini_crc=0,
                            map_crc=0,
                            map_name="pending",
                            seed=0,
                            starting_cash=None,
                            header_json={"status": "pending"},
                            lifecycle_state="discovered",
                            updated_at=self._jobs.now(),
                        )
                        session.add(replay)
                        session.flush()
                except IntegrityError:
                    replay = session.scalar(select(Replay).where(Replay.sha256 == expected_sha256))
                    if replay is None:
                        raise
            assert replay is not None
            matching = self._matching_sources(session, expected_sha256)
            variants: set[tuple[str, bool]] = set()
            for source in matching:
                source.replay_id = replay.id
                provenance = cast(Mapping[str, Any], source.provenance_json)
                variants.add(
                    (
                        cast(str, provenance["import_mode"]),
                        bool(provenance["request_telemetry"]),
                    )
                )
            hash_row = session.scalar(select(Job).where(Job.public_id == claimed.public_id))
            assert hash_row is not None
            hash_row.replay_id = replay.id
            for mode, request_telemetry in sorted(variants):
                self._ensure_content_graph(session, replay, mode, request_telemetry)
            replay_public_id = replay.public_id
        return {"replay_public_id": replay_public_id, "sha256": expected_sha256, "size_bytes": size}

    def _unlinked_source_records(self, sha256: str) -> list[Source]:
        with self._session_factory() as session:
            rows = self._matching_sources(session, sha256)
            for row in rows:
                session.expunge(row)
            return rows

    @staticmethod
    def _matching_sources(session: Session, sha256: str) -> list[Source]:
        return [
            source
            for source in session.scalars(select(Source).order_by(Source.id))
            if cast(Mapping[str, Any], source.provenance_json).get("content_sha256") == sha256
        ]

    def _ensure_content_graph(self, session: Session, replay: Replay, mode: str, request_telemetry: bool) -> None:
        sha256 = replay.sha256
        hash_job = self._jobs.ensure_job(session, self._hash_spec(sha256))

        manage_identity = {"import_mode": mode, "manage_copy_version": MANAGE_COPY_VERSION}
        manage = self._jobs.ensure_job(
            session,
            JobSpec(
                MANAGE_COPY,
                MANAGE_COPY_VERSION,
                content_key(MANAGE_COPY, MANAGE_COPY_VERSION, sha256, manage_identity),
                {"replay_public_id": replay.public_id, "replay_sha256": sha256, "import_mode": mode},
                replay.id,
            ),
        )
        parse_identity = {
            "import_mode": mode,
            "parse_version": PARSE_VERSION,
            "parser_version": self._parser_version,
        }
        parse = self._jobs.ensure_job(
            session,
            JobSpec(
                PARSE,
                PARSE_VERSION,
                content_key(PARSE, PARSE_VERSION, sha256, parse_identity),
                {"replay_public_id": replay.public_id, "replay_sha256": sha256, "import_mode": mode},
                replay.id,
            ),
        )
        self._jobs.ensure_dependency(session, manage.id, hash_job.id)
        self._jobs.ensure_dependency(session, parse.id, manage.id)

        telemetry: Job | None = None
        use_telemetry = request_telemetry and self._telemetry_acquirer is not None
        if use_telemetry:
            telemetry_identity = {
                "acquirer_version": self._telemetry_acquirer_version,
                "import_mode": mode,
                "telemetry_version": TELEMETRY_VERSION,
            }
            telemetry = self._jobs.ensure_job(
                session,
                JobSpec(
                    TELEMETRY,
                    TELEMETRY_VERSION,
                    content_key(TELEMETRY, TELEMETRY_VERSION, sha256, telemetry_identity),
                    {"replay_public_id": replay.public_id, "replay_sha256": sha256, "import_mode": mode},
                    replay.id,
                ),
            )
            self._jobs.ensure_dependency(session, telemetry.id, parse.id)

        branch_identity = {
            "import_observations_version": IMPORT_OBSERVATIONS_VERSION,
            "parser_version": self._parser_version,
            "telemetry_acquirer_version": self._telemetry_acquirer_version if use_telemetry else None,
        }
        import_job = self._future_job(
            session,
            replay,
            IMPORT_OBSERVATIONS,
            IMPORT_OBSERVATIONS_VERSION,
            branch_identity,
        )
        derive = self._future_job(
            session,
            replay,
            DERIVE_FEATURES,
            DERIVE_FEATURES_VERSION,
            {"derive_features_version": DERIVE_FEATURES_VERSION, "observations": branch_identity},
        )
        assess = self._future_job(
            session,
            replay,
            ASSESS_STRATEGIES,
            ASSESS_STRATEGIES_VERSION,
            {"assess_strategies_version": ASSESS_STRATEGIES_VERSION, "observations": branch_identity},
        )
        llm = self._future_job(
            session,
            replay,
            ANALYZE_LLM,
            ANALYZE_LLM_VERSION,
            {"analyze_llm_version": ANALYZE_LLM_VERSION, "observations": branch_identity},
        )
        report = self._future_job(
            session,
            replay,
            RENDER_REPORT,
            RENDER_REPORT_VERSION,
            {"render_report_version": RENDER_REPORT_VERSION, "observations": branch_identity},
        )
        self._jobs.ensure_dependency(session, import_job.id, parse.id)
        if telemetry is not None:
            self._jobs.ensure_dependency(session, import_job.id, telemetry.id)
        self._jobs.ensure_dependency(session, derive.id, import_job.id)
        self._jobs.ensure_dependency(session, assess.id, derive.id)
        self._jobs.ensure_dependency(session, llm.id, assess.id)
        self._jobs.ensure_dependency(session, report.id, assess.id)

    def _future_job(
        self,
        session: Session,
        replay: Replay,
        stage: str,
        version: str,
        identity: Mapping[str, Any],
    ) -> Job:
        return self._jobs.ensure_job(
            session,
            JobSpec(
                stage,
                version,
                content_key(stage, version, replay.sha256, identity),
                {"replay_public_id": replay.public_id, "replay_sha256": replay.sha256},
                replay.id,
            ),
        )

    def _manage_copy(self, claimed: ClaimedJob) -> Mapping[str, Any]:
        sha256 = cast(str, claimed.input_json["replay_sha256"])
        mode = cast(str, claimed.input_json["import_mode"])
        if mode == "reference":
            return {"copied": False, "sha256": sha256}
        source_path = self._source_path_for_replay(sha256, mode)
        if source_path is None:
            raise StageFailure("source_missing", "no ordinary source remains for managed copy", retryable=False)
        try:
            stored = self._replay_store.store_file(source_path, expected_sha256=sha256)
        except ContentStorageError as error:
            raise StageFailure("managed_copy_failed", str(error), retryable=True) from error
        with self._session_factory.begin() as session:
            asset = self._register_asset(session, stored, "replay")
            replay = session.scalar(select(Replay).where(Replay.sha256 == sha256))
            if replay is None:
                raise StageFailure("replay_missing", "replay identity disappeared", retryable=False)
            if replay.managed_asset_id not in (None, asset.id):
                raise StageFailure("managed_asset_conflict", "replay is linked to different managed bytes", retryable=False)
            replay.managed_asset_id = asset.id
            asset_public_id = asset.public_id
        return {
            "copied": True,
            "sha256": stored.sha256,
            "size_bytes": stored.size,
            "asset_public_id": asset_public_id,
        }

    def _parse(self, claimed: ClaimedJob) -> Mapping[str, Any]:
        sha256 = cast(str, claimed.input_json["replay_sha256"])
        mode = cast(str, claimed.input_json["import_mode"])
        replay_path = self._immutable_replay_path(sha256, mode)
        try:
            parsed = self._parser(replay_path)
        except OSError as error:
            raise StageFailure(
                "parser_failed",
                str(error),
                retryable=True,
                details={"exception_type": type(error).__name__},
            ) from error
        except Exception as error:
            raise StageFailure(
                "parser_failed",
                str(error),
                retryable=False,
                details={"exception_type": type(error).__name__},
            ) from error
        warning_codes = sorted({cast(str, warning.code) for warning in parsed.warnings})
        return {
            "parser_version": self._parser_version,
            "content_sha256": sha256,
            "completion_status": parsed.completion_status,
            "command_count": len(parsed.commands),
            "warning_codes": warning_codes,
            "command_stream_offset": parsed.command_stream_offset,
            "end_offset": parsed.end_offset,
        }

    def _telemetry(self, claimed: ClaimedJob) -> Mapping[str, Any]:
        acquirer = self._telemetry_acquirer
        assert acquirer is not None
        sha256 = cast(str, claimed.input_json["replay_sha256"])
        mode = cast(str, claimed.input_json["import_mode"])
        replay_path = self._immutable_replay_path(sha256, mode)
        try:
            artifact = acquirer.acquire(replay_path, sha256)
        except OSError as error:
            raise StageFailure("telemetry_acquisition_failed", str(error), retryable=True) from error
        try:
            paths = _validated_artifact_paths(artifact)
        except ValueError as error:
            raise StageFailure("invalid_telemetry_artifact", str(error), retryable=False) from error

        stored_artifacts: list[tuple[str, StoredContent]] = []
        try:
            for kind, path in paths:
                stored_artifacts.append((kind, self._artifact_store.store_file(path)))
        except ContentStorageError as error:
            raise StageFailure("artifact_copy_failed", str(error), retryable=True) from error
        manifest: list[dict[str, Any]] = []
        with self._session_factory.begin() as session:
            for kind, stored in stored_artifacts:
                asset = self._register_asset(session, stored, kind)
                manifest.append(
                    {
                        "asset_public_id": asset.public_id,
                        "kind": kind,
                        "sha256": stored.sha256,
                        "size_bytes": stored.size,
                    }
                )
        manifest.sort(key=lambda item: (cast(str, item["kind"]), cast(str, item["sha256"])))
        output: dict[str, Any] = {
            "run_id": artifact.run_id,
            "runner_status": artifact.runner_status,
            "replay_quality": artifact.replay_quality,
            "strategy_analysis_scope": artifact.strategy_analysis_scope,
            "exit_code": artifact.exit_code,
            "engine_build": artifact.engine_build,
            "engine_executable_sha256": artifact.engine_executable_sha256,
            "diagnostics": [
                {"code": diagnostic.code, "message": diagnostic.message} for diagnostic in artifact.diagnostics
            ],
            "artifacts": manifest,
        }
        if artifact.runner_status != "success":
            retryable = artifact.runner_status in {"timeout", "launch_failure", "interrupted"}
            raise StageFailure("exporter_failure", "telemetry acquisition did not succeed", retryable, output)
        return output

    def _register_asset(self, session: Session, stored: StoredContent, kind: str) -> ManagedAsset:
        asset = session.scalar(select(ManagedAsset).where(ManagedAsset.sha256 == stored.sha256))
        if asset is not None:
            return asset
        relative_path = stored.path.relative_to(self._settings.data_root).as_posix()
        try:
            with session.begin_nested():
                asset = ManagedAsset(
                    public_id=str(uuid4()),
                    sha256=stored.sha256,
                    kind=kind,
                    relative_path=relative_path,
                    size_bytes=stored.size,
                    media_type=None,
                )
                session.add(asset)
                session.flush()
        except IntegrityError:
            asset = session.scalar(select(ManagedAsset).where(ManagedAsset.sha256 == stored.sha256))
            if asset is None:
                raise
        return asset

    def _source_path_for_replay(self, sha256: str, mode: str) -> Path | None:
        with self._session_factory() as session:
            sources = [
                source
                for source in self._matching_sources(session, sha256)
                if cast(Mapping[str, Any], source.provenance_json).get("import_mode") == mode
            ]
            return _first_regular_path(source.original_locator for source in sources)

    def _immutable_replay_path(self, sha256: str, mode: str) -> Path:
        with self._session_factory() as session:
            replay = session.scalar(select(Replay).where(Replay.sha256 == sha256))
            if replay is None:
                raise StageFailure("replay_missing", "replay identity disappeared", retryable=False)
            if replay.managed_asset_id is not None:
                asset = session.get(ManagedAsset, replay.managed_asset_id)
                if asset is None:
                    raise StageFailure("managed_asset_missing", "managed replay metadata disappeared", retryable=False)
                try:
                    return self._replay_store.verify(sha256).path
                except ContentStorageError as error:
                    raise StageFailure("managed_asset_invalid", str(error), retryable=False) from error
        source_path = self._source_path_for_replay(sha256, mode)
        if source_path is None:
            raise StageFailure("source_missing", "reference source is no longer an ordinary file", retryable=False)
        return source_path


def _absolute_without_following(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def _validate_submission_path(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError(f"import path is unavailable: {error}") from error
    if path.is_symlink() or _is_reparse(info):
        raise ValueError("import path cannot be a symlink or reparse point")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError("import path must be a regular replay file or directory")
    if stat.S_ISREG(info.st_mode) and path.suffix.casefold() != ".rep":
        raise ValueError("import file must use the .rep extension")


def _snapshot_replays(root: Path, recursive: bool) -> tuple[list[Path], list[dict[str, str]]]:
    try:
        root_info = root.lstat()
    except OSError as error:
        return [], [_diagnostic("source_unavailable", root, str(error))]
    if stat.S_ISREG(root_info.st_mode):
        return [root], []
    accepted: list[Path] = []
    diagnostics: list[dict[str, str]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            diagnostics.append(_diagnostic("directory_unreadable", directory, str(error)))
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or _is_reparse(info):
                    diagnostics.append(_diagnostic("symlink_rejected", path, "symlink or reparse entry"))
                elif stat.S_ISDIR(info.st_mode):
                    if recursive:
                        pending.append(path)
                elif not stat.S_ISREG(info.st_mode):
                    diagnostics.append(_diagnostic("non_file_rejected", path, "entry is not a regular file"))
                elif path.suffix.casefold() != ".rep":
                    diagnostics.append(_diagnostic("extension_rejected", path, "entry is not a replay"))
                else:
                    accepted.append(path)
            except OSError as error:
                diagnostics.append(_diagnostic("entry_unreadable", path, str(error)))
    accepted.sort(key=lambda path: os.path.normcase(str(_absolute_without_following(path))).casefold())
    return accepted, diagnostics


def _diagnostic(code: str, path: Path, message: str) -> dict[str, str]:
    return {"code": code, "entry_name": path.name, "message": message}


def _first_regular_path(locators: Iterable[str]) -> Path | None:
    for locator in locators:
        path = Path(locator)
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and not path.is_symlink() and not _is_reparse(info):
            return path
    return None


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _validated_artifact_paths(artifact: TelemetryArtifact) -> list[tuple[str, Path]]:
    try:
        parsed_uuid = UUID(artifact.run_id)
    except (ValueError, AttributeError) as error:
        raise ValueError("telemetry run_id must be a lowercase hyphenated UUID") from error
    if str(parsed_uuid) != artifact.run_id:
        raise ValueError("telemetry run_id must be a lowercase hyphenated UUID")
    if artifact.exit_code is not None and (
        not isinstance(artifact.exit_code, int) or isinstance(artifact.exit_code, bool) or artifact.exit_code < 0
    ):
        raise ValueError("telemetry exit_code must be a nonnegative integer")
    engine_hash = artifact.engine_executable_sha256
    if engine_hash is not None and (
        len(engine_hash) != 64
        or engine_hash != engine_hash.lower()
        or any(character not in "0123456789abcdef" for character in engine_hash)
    ):
        raise ValueError("engine executable SHA-256 must be lowercase hexadecimal")
    values: list[tuple[str, Path | None]] = [
        ("telemetry_trace", artifact.trace_path),
        ("telemetry_catalog", artifact.catalog_path),
        ("telemetry_outcome", artifact.outcome_path),
        ("telemetry_stdout", artifact.stdout_path),
        ("telemetry_stderr", artifact.stderr_path),
    ]
    values.extend(("telemetry_map_asset", path) for path in artifact.map_asset_paths)
    retained: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for kind, possible_path in values:
        if possible_path is None:
            continue
        path = possible_path.resolve(strict=False)
        if path in seen:
            raise ValueError("telemetry artifact paths must be unique")
        seen.add(path)
        try:
            info = path.lstat()
        except OSError as error:
            raise ValueError(f"telemetry artifact is unavailable: {path.name}") from error
        if not stat.S_ISREG(info.st_mode) or path.is_symlink() or _is_reparse(info):
            raise ValueError("telemetry artifacts must be ordinary non-symlink files")
        retained.append((kind, path))
    if artifact.runner_status == "success" and artifact.trace_path is None:
        raise ValueError("successful telemetry acquisition requires a trace")
    return retained
