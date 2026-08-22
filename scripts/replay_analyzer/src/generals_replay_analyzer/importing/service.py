"""Transactional replay intake with source provenance and injected acquisition."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias, TypeVar, cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..config import AnalyzerSettings
from ..db.models import Job, JobDependency, ManagedAsset, Replay, Source
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
    STAGES,
    TELEMETRY,
    TELEMETRY_VERSION,
    canonical_json,
    content_key,
    input_digest,
)

FrozenJSONValue: TypeAlias = (
    str | int | float | bool | None | tuple["FrozenJSONValue", ...] | Mapping[str, "FrozenJSONValue"]
)

_STAGE_VERSIONS = MappingProxyType(
    {
        DISCOVER: DISCOVER_VERSION,
        HASH: HASH_VERSION,
        MANAGE_COPY: MANAGE_COPY_VERSION,
        PARSE: PARSE_VERSION,
        TELEMETRY: TELEMETRY_VERSION,
        IMPORT_OBSERVATIONS: IMPORT_OBSERVATIONS_VERSION,
        DERIVE_FEATURES: DERIVE_FEATURES_VERSION,
        ASSESS_STRATEGIES: ASSESS_STRATEGIES_VERSION,
        ANALYZE_LLM: ANALYZE_LLM_VERSION,
        RENDER_REPORT: RENDER_REPORT_VERSION,
    }
)
_STAGE_ORDER = {stage: index for index, stage in enumerate(_STAGE_VERSIONS)}
_BUILT_IN_STAGES = frozenset({DISCOVER, HASH, MANAGE_COPY, PARSE, TELEMETRY})
_DIRECT_DEPENDENCY_STAGES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        IMPORT_OBSERVATIONS: frozenset({PARSE, TELEMETRY}),
        DERIVE_FEATURES: frozenset({IMPORT_OBSERVATIONS}),
        ASSESS_STRATEGIES: frozenset({DERIVE_FEATURES}),
        ANALYZE_LLM: frozenset({ASSESS_STRATEGIES}),
        RENDER_REPORT: frozenset({ASSESS_STRATEGIES}),
    }
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


# TheSuperHackers @feature Leex 22/08/2026 Expose immutable dependency evidence to injected future-stage handlers. (#TBD)
@dataclass(frozen=True)
class StageDependencyOutput:
    """Immutable public snapshot of one selected direct dependency."""

    job_public_id: str
    stage: str
    component_version: str
    output: Mapping[str, FrozenJSONValue] | None
    status: str = "succeeded"
    error_code: str | None = None
    error_message: str | None = None
    error_details: Mapping[str, FrozenJSONValue] | None = None


@dataclass(frozen=True)
class StageExecutionContext:
    """Immutable public input for a registered future-stage handler."""

    job_public_id: str
    idempotency_key: str
    replay_public_id: str
    replay_sha256: str
    stage: str
    component_version: str
    input: Mapping[str, FrozenJSONValue]
    dependencies: tuple[StageDependencyOutput, ...]


class StageHandler(Protocol):
    def __call__(self, context: StageExecutionContext) -> Mapping[str, Any]: ...


# TheSuperHackers @feature Leex 22/08/2026 Declare explicit failed direct dependencies that remain importable evidence. (#TBD)
@dataclass(frozen=True)
class TerminalDependencyPolicy:
    """Closed opt-in for direct dependency stages whose failed state is evidence."""

    failed_stages: frozenset[str] = frozenset()


@dataclass(frozen=True)
class StageHandlerRegistration:
    """Construction-time binding of one closed future stage to its handler."""

    stage: str
    component_version: str
    handler: StageHandler
    terminal_dependency_policy: TerminalDependencyPolicy = TerminalDependencyPolicy()


@dataclass(frozen=True)
class _ReplayInput:
    path: Path
    revalidate_reference: bool


# TheSuperHackers @bugfix Leex 22/08/2026 Preserve safe bundle topology without persisting engine run paths. (#TBD)
@dataclass(frozen=True)
class _ArtifactDescriptor:
    kind: str
    path: Path
    logical_path: str


_TELEMETRY_FAILURE_ENVELOPE_TYPE = "telemetry_artifact_failure"
_TELEMETRY_FAILURE_ENVELOPE_VERSION = 1


_ResultT = TypeVar("_ResultT")


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
        stage_handlers: Iterable[StageHandlerRegistration] = (),
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
        handlers: dict[str, Callable[[ClaimedJob], Mapping[str, Any]]] = {
            DISCOVER: self._discover,
            HASH: self._hash,
            MANAGE_COPY: self._manage_copy,
            PARSE: self._parse,
        }
        if telemetry_acquirer is not None:
            handlers[TELEMETRY] = self._telemetry
        seen: set[str] = set()
        terminal_failure_stages: dict[str, frozenset[str]] = {}
        for registration in tuple(stage_handlers):
            if registration.stage in seen:
                raise ValueError(f"duplicate_stage: {registration.stage}")
            seen.add(registration.stage)
            if registration.stage not in STAGES:
                raise ValueError(f"unknown_stage: {registration.stage}")
            if registration.stage in _BUILT_IN_STAGES:
                raise ValueError(f"built_in_stage: {registration.stage}")
            expected_version = _STAGE_VERSIONS[registration.stage]
            if registration.component_version != expected_version:
                raise ValueError(
                    f"version_mismatch: {registration.stage} requires component version {expected_version}"
                )
            policy = registration.terminal_dependency_policy
            if not isinstance(policy, TerminalDependencyPolicy) or not isinstance(policy.failed_stages, frozenset):
                raise TypeError("invalid_terminal_dependency_policy: policy must use the closed typed contract")
            if any(not isinstance(stage, str) for stage in policy.failed_stages):
                raise ValueError("invalid_terminal_dependency_policy: failed dependency stages must be strings")
            unknown = sorted(policy.failed_stages.difference(STAGES))
            if unknown:
                raise ValueError(f"unknown_terminal_dependency_stage: {unknown[0]}")
            direct_stages = _DIRECT_DEPENDENCY_STAGES.get(registration.stage, frozenset())
            non_direct = sorted(policy.failed_stages.difference(direct_stages))
            if non_direct:
                raise ValueError(
                    f"non_direct_terminal_dependency_stage: {non_direct[0]} is not a direct dependency of "
                    f"{registration.stage}"
                )
            terminal_failure_stages[registration.stage] = frozenset(policy.failed_stages)
            handlers[registration.stage] = self._adapt_stage_handler(registration.handler)
        self._registered_future_stages = frozenset(seen)
        self._terminal_failure_stages: Mapping[str, frozenset[str]] = MappingProxyType(
            terminal_failure_stages
        )
        self._handlers: Mapping[str, Callable[[ClaimedJob], Mapping[str, Any]]] = MappingProxyType(handlers)

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
            if IMPORT_OBSERVATIONS in self._registered_future_stages:
                self._materialize_ready_observation_jobs()
            claimed = self._jobs.claim(
                worker_id,
                self._handlers,
                terminal_failure_stages=self._terminal_failure_stages,
            )
            if claimed is None:
                break
            handler = self._handlers[claimed.stage]
            try:
                output = _canonical_output(handler(claimed))
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

    def _adapt_stage_handler(
        self, handler: StageHandler
    ) -> Callable[[ClaimedJob], Mapping[str, Any]]:
        def execute(claimed: ClaimedJob) -> Mapping[str, Any]:
            return handler(self._stage_execution_context(claimed))

        return execute

    def _stage_execution_context(self, claimed: ClaimedJob) -> StageExecutionContext:
        replay_public_id = claimed.replay_public_id
        replay_sha256 = claimed.input_json.get("replay_sha256")
        if replay_public_id is None or not isinstance(replay_sha256, str):
            raise StageFailure(
                "invalid_stage_context",
                "registered stage requires public replay identity and content SHA-256",
                retryable=False,
            )
        with self._session_factory() as session:
            dependencies = list(
                session.scalars(
                    select(Job)
                    .join(JobDependency, Job.id == JobDependency.depends_on_job_id)
                    .where(JobDependency.job_id == claimed.internal_id)
                )
            )
            tolerated_failures = self._terminal_failure_stages.get(claimed.stage, frozenset())
            if any(
                dependency.status != "succeeded"
                and not (
                    dependency.status == "failed"
                    and not dependency.retryable
                    and dependency.stage in tolerated_failures
                )
                for dependency in dependencies
            ):
                raise StageFailure(
                    "dependency_unavailable",
                    "registered stage dependency is not terminal evidence",
                    retryable=True,
                )
            dependencies.sort(
                key=lambda dependency: (
                    _STAGE_ORDER.get(dependency.stage, len(_STAGE_ORDER)),
                    dependency.component_version,
                    dependency.public_id,
                )
            )
            snapshots = tuple(_dependency_output(dependency) for dependency in dependencies)
        return StageExecutionContext(
            job_public_id=claimed.public_id,
            idempotency_key=claimed.idempotency_key,
            replay_public_id=replay_public_id,
            replay_sha256=replay_sha256,
            stage=claimed.stage,
            component_version=claimed.component_version,
            input=_freeze_mapping(claimed.input_json),
            dependencies=snapshots,
        )

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
        telemetry_identity: Mapping[str, Any] | None = None
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

        # TheSuperHackers @bugfix Leex 22/08/2026 Isolate downstream copy, reference, parser, and telemetry branches. (#TBD)
        branch_identity = {
            "import_observations_version": IMPORT_OBSERVATIONS_VERSION,
            "import_mode": mode,
            "parse": parse_identity,
            "telemetry": telemetry_identity,
        }
        import_job = self._future_job(
            session,
            replay,
            IMPORT_OBSERVATIONS,
            IMPORT_OBSERVATIONS_VERSION,
            branch_identity,
            dependency_bound=True,
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
        *,
        dependency_bound: bool = False,
    ) -> Job:
        provisional_key = content_key(stage, version, replay.sha256, identity)
        input_json: dict[str, Any] = {
            "replay_public_id": replay.public_id,
            "replay_sha256": replay.sha256,
        }
        if dependency_bound:
            input_json.update(
                {
                    "branch_recipe": dict(identity),
                    "dependency_identity_bound": False,
                    "provisional_idempotency_key": provisional_key,
                }
            )
            for existing in session.scalars(
                select(Job).where(Job.replay_id == replay.id, Job.stage == stage).order_by(Job.id)
            ):
                existing_input = cast(Mapping[str, Any], existing.input_json)
                if existing_input.get("provisional_idempotency_key") == provisional_key:
                    return existing
        return self._jobs.ensure_job(
            session,
            JobSpec(
                stage,
                version,
                provisional_key,
                input_json,
                replay.id,
            ),
        )

    # TheSuperHackers @feature Leex 22/08/2026 Bind observation imports to immutable selected dependency evidence before claim. (#TBD)
    def _materialize_ready_observation_jobs(self) -> None:
        with self._session_factory.begin() as session:
            candidates = list(
                session.scalars(
                    select(Job)
                    .where(
                        Job.stage == IMPORT_OBSERVATIONS,
                        Job.component_version == IMPORT_OBSERVATIONS_VERSION,
                        Job.status == "pending",
                        Job.lease_owner.is_(None),
                        Job.lease_expires_at.is_(None),
                    )
                    .order_by(Job.id)
                )
            )
            for candidate in candidates:
                candidate_input = cast(Mapping[str, Any], candidate.input_json)
                provisional_key = candidate_input.get("provisional_idempotency_key")
                if (
                    candidate_input.get("dependency_identity_bound") is not False
                    or not isinstance(provisional_key, str)
                ):
                    continue
                dependencies = list(
                    session.scalars(
                        select(Job)
                        .join(JobDependency, Job.id == JobDependency.depends_on_job_id)
                        .where(JobDependency.job_id == candidate.id)
                    )
                )
                tolerated_failures = self._terminal_failure_stages.get(
                    IMPORT_OBSERVATIONS,
                    frozenset(),
                )
                if not dependencies or any(
                    dependency.status != "succeeded"
                    and not (
                        dependency.status == "failed"
                        and not dependency.retryable
                        and dependency.stage in tolerated_failures
                    )
                    for dependency in dependencies
                ):
                    continue
                dependencies.sort(
                    key=lambda dependency: (
                        _STAGE_ORDER.get(dependency.stage, len(_STAGE_ORDER)),
                        dependency.component_version,
                        dependency.public_id,
                    )
                )
                replay_sha256 = candidate_input.get("replay_sha256")
                branch_recipe = candidate_input.get("branch_recipe")
                if not isinstance(replay_sha256, str) or not isinstance(branch_recipe, Mapping):
                    raise TypeError("provisional observation job has invalid semantic input")
                selected_identity = {
                    "replay_sha256": replay_sha256,
                    "branch_recipe": dict(branch_recipe),
                    "dependencies": [
                        _dependency_identity(dependency)
                        for dependency in dependencies
                    ],
                }
                selected_digest = input_digest(selected_identity)
                materialized_key = content_key(
                    IMPORT_OBSERVATIONS,
                    IMPORT_OBSERVATIONS_VERSION,
                    replay_sha256,
                    selected_identity,
                )
                materialized_input = dict(candidate_input)
                materialized_input["dependency_identity_bound"] = True
                materialized_input["selected_dependency_digest"] = selected_digest
                current_key = candidate.idempotency_key
                try:
                    with session.begin_nested():
                        result = session.execute(
                            update(Job)
                            .where(
                                Job.id == candidate.id,
                                Job.idempotency_key == current_key,
                                Job.status == "pending",
                                Job.lease_owner.is_(None),
                                Job.lease_expires_at.is_(None),
                            )
                            .values(
                                idempotency_key=materialized_key,
                                input_json=materialized_input,
                            )
                            .execution_options(synchronize_session=False)
                        )
                        if getattr(result, "rowcount", 0) != 1:
                            continue
                except IntegrityError:
                    winner = session.scalar(select(Job).where(Job.idempotency_key == materialized_key))
                    if winner is None:
                        raise
                    self._coalesce_provisional_job(session, candidate, winner)

    def _coalesce_provisional_job(self, session: Session, loser: Job, winner: Job) -> None:
        if loser.id == winner.id:
            return
        if loser.status != "pending" or loser.lease_owner is not None or loser.lease_expires_at is not None:
            raise RuntimeError("claimed observation jobs cannot be coalesced")
        incoming = list(session.scalars(select(JobDependency).where(JobDependency.job_id == loser.id)))
        outgoing = list(
            session.scalars(select(JobDependency).where(JobDependency.depends_on_job_id == loser.id))
        )
        for edge in incoming:
            if edge.depends_on_job_id != winner.id and session.get(
                JobDependency, (winner.id, edge.depends_on_job_id)
            ) is None:
                session.add(
                    JobDependency(
                        job_id=winner.id,
                        depends_on_job_id=edge.depends_on_job_id,
                        created_at=edge.created_at,
                    )
                )
        for edge in outgoing:
            if edge.job_id != winner.id and session.get(JobDependency, (edge.job_id, winner.id)) is None:
                session.add(
                    JobDependency(
                        job_id=edge.job_id,
                        depends_on_job_id=winner.id,
                        created_at=edge.created_at,
                    )
                )
        session.delete(loser)
        session.flush()

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
        replay_input = self._replay_input(sha256, mode)
        try:
            parsed = _consume_replay(replay_input, sha256, self._parser)
        except StageFailure:
            raise
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
        warning_codes = sorted({warning.code for warning in parsed.warnings})
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
        replay_input = self._replay_input(sha256, mode)
        try:
            artifact = _consume_replay(replay_input, sha256, lambda path: acquirer.acquire(path, sha256))
        except StageFailure:
            raise
        except OSError as error:
            raise StageFailure(
                "telemetry_acquisition_failed",
                _redacted_diagnostic_message(str(error), replay_input.path, ()),
                retryable=True,
            ) from error
        try:
            paths = _validated_artifact_paths(artifact)
        except Exception as error:
            raise StageFailure(
                "invalid_telemetry_artifact",
                "telemetry artifact validation failed",
                retryable=False,
                details=_telemetry_failure_details(
                    artifact,
                    replay_input.path,
                    failure_code="invalid_telemetry_artifact",
                    failure_message="telemetry artifact validation failed",
                    quality_issue_code="invalid_trace",
                    descriptors=(),
                    manifest=(),
                ),
            ) from error

        manifest: list[dict[str, Any]] = []
        for descriptor in paths:
            try:
                stored = self._artifact_store.store_file(descriptor.path)
            except ContentStorageError as error:
                raise StageFailure(
                    "artifact_copy_failed",
                    "telemetry artifact copy failed",
                    retryable=True,
                    details=_telemetry_failure_details(
                        artifact,
                        replay_input.path,
                        failure_code="artifact_copy_failed",
                        failure_message="telemetry artifact copy failed",
                        quality_issue_code="invalid_trace",
                        descriptors=tuple(paths),
                        manifest=tuple(manifest),
                    ),
                ) from error
            # TheSuperHackers @bugfix Leex 22/08/2026 Retain each verified artifact before a later bundle copy can fail. (#TBD)
            try:
                with self._session_factory.begin() as session:
                    asset = self._register_asset(session, stored, descriptor.kind)
                    registered = {
                        "asset_public_id": asset.public_id,
                        "kind": descriptor.kind,
                        "logical_path": descriptor.logical_path,
                        "sha256": stored.sha256,
                        "size_bytes": stored.size,
                    }
                manifest.append(registered)
            except Exception as error:
                raise StageFailure(
                    "artifact_registration_failed",
                    "telemetry artifact registration failed",
                    retryable=False,
                    details=_telemetry_failure_details(
                        artifact,
                        replay_input.path,
                        failure_code="artifact_registration_failed",
                        failure_message="telemetry artifact registration failed",
                        quality_issue_code="asset_invalid",
                        descriptors=tuple(paths),
                        manifest=tuple(manifest),
                    ),
                ) from error
        try:
            output = _telemetry_attempt_facts(
                artifact,
                replay_input.path,
                tuple(paths),
                tuple(manifest),
            )
        except Exception as error:
            raise StageFailure(
                "invalid_telemetry_metadata",
                "telemetry metadata validation failed",
                retryable=False,
                details=_telemetry_failure_details(
                    artifact,
                    replay_input.path,
                    failure_code="invalid_telemetry_metadata",
                    failure_message="telemetry metadata validation failed",
                    quality_issue_code="asset_invalid",
                    descriptors=tuple(paths),
                    manifest=tuple(manifest),
                ),
            ) from error
        if artifact.runner_status != "success":
            retryable = artifact.runner_status in {"timeout", "launch_failure", "interrupted"}
            raise StageFailure("exporter_failure", "telemetry acquisition did not succeed", retryable, output)
        return output

    def _register_asset(self, session: Session, stored: StoredContent, kind: str) -> ManagedAsset:
        asset = session.scalar(select(ManagedAsset).where(ManagedAsset.sha256 == stored.sha256))
        if asset is not None:
            if asset.kind != kind or asset.size_bytes != stored.size:
                raise ValueError("managed content is already registered for another telemetry role")
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
            if asset.kind != kind or asset.size_bytes != stored.size:
                raise ValueError("managed content is already registered for another telemetry role")
        return asset

    def _source_path_for_replay(self, sha256: str, mode: str) -> Path | None:
        with self._session_factory() as session:
            sources = [
                source
                for source in self._matching_sources(session, sha256)
                if cast(Mapping[str, Any], source.provenance_json).get("import_mode") == mode
            ]
            return _first_regular_path(source.original_locator for source in sources)

    def _replay_input(self, sha256: str, mode: str) -> _ReplayInput:
        with self._session_factory() as session:
            replay = session.scalar(select(Replay).where(Replay.sha256 == sha256))
            if replay is None:
                raise StageFailure("replay_missing", "replay identity disappeared", retryable=False)
            if replay.managed_asset_id is not None:
                asset = session.get(ManagedAsset, replay.managed_asset_id)
                if asset is None:
                    raise StageFailure("managed_asset_missing", "managed replay metadata disappeared", retryable=False)
                try:
                    return _ReplayInput(self._replay_store.verify(sha256).path, revalidate_reference=False)
                except ContentStorageError as error:
                    raise StageFailure("managed_asset_invalid", str(error), retryable=False) from error
        source_path = self._source_path_for_replay(sha256, mode)
        if source_path is None:
            raise StageFailure("source_missing", "reference source is no longer an ordinary file", retryable=False)
        return _ReplayInput(source_path, revalidate_reference=True)


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


def _freeze_json(value: Any) -> FrozenJSONValue:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"stage context contains non-JSON value: {type(value).__name__}")


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, FrozenJSONValue]:
    if any(not isinstance(key, str) for key in value):
        raise TypeError("stage context mapping keys must be strings")
    frozen: dict[str, FrozenJSONValue] = {}
    for key in sorted(value):
        frozen[key] = _freeze_json(value[key])
    return MappingProxyType(frozen)


def _dependency_output(row: Job) -> StageDependencyOutput:
    if row.status == "succeeded":
        output = row.output_json
        if not isinstance(output, Mapping):
            raise StageFailure(
                "dependency_output_invalid",
                "succeeded dependency has no mapping output",
                retryable=False,
                details={"dependency_public_id": row.public_id, "dependency_stage": row.stage},
            )
        return StageDependencyOutput(
            job_public_id=row.public_id,
            stage=row.stage,
            component_version=row.component_version,
            output=_freeze_mapping(output),
            status="succeeded",
        )
    if row.status == "failed" and not row.retryable:
        code, message, details = _dependency_failure_evidence(row)
        return StageDependencyOutput(
            job_public_id=row.public_id,
            stage=row.stage,
            component_version=row.component_version,
            output=None,
            status="failed",
            error_code=code,
            error_message=message,
            error_details=_freeze_mapping(details),
        )
    raise StageFailure(
        "dependency_unavailable",
        "dependency is not terminal",
        retryable=True,
        details={"dependency_public_id": row.public_id, "dependency_stage": row.stage},
    )


_PRIVATE_FAILURE_KEYS = frozenset(
    {
        "available_at",
        "completed_at",
        "created_at",
        "id",
        "job_id",
        "lease_expires_at",
        "lease_owner",
        "managed_asset_id",
        "map_asset_paths",
        "replay_id",
        "runner_config",
        "runner_result",
        "started_at",
    }
)


def _sanitize_failure_text(value: str) -> str:
    return re.sub(
        r"(?i)(?:file:(?:/{2,3}|\\{2})[^\s\"']*|(?:[a-z]:[\\/]|\\\\(?:[?.]\\)?|/)[^\s\"']*)",
        "[redacted-path]",
        value,
    )


def _contains_pathlike_text(value: str) -> bool:
    return _sanitize_failure_text(value) != value


def _sanitize_failure_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError("dependency failure details keys must be strings")
            if (
                key.startswith("_")
                or key in _PRIVATE_FAILURE_KEYS
                or key.endswith("_at")
                or (key.endswith("_path") and key != "logical_path")
            ):
                continue
            sanitized[key] = _sanitize_failure_json(value[key])
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_sanitize_failure_json(item) for item in value]
    if isinstance(value, str):
        return _sanitize_failure_text(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    raise TypeError(f"dependency failure details contain non-JSON value: {type(value).__name__}")


def _dependency_failure_evidence(row: Job) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(row.error_code, str) or not row.error_code:
        raise StageFailure(
            "dependency_error_invalid",
            "failed dependency has no stable error code",
            retryable=False,
            details={"dependency_public_id": row.public_id, "dependency_stage": row.stage},
        )
    message = row.error_message if isinstance(row.error_message, str) else "dependency failed"
    raw_details = row.error_details_json if isinstance(row.error_details_json, Mapping) else {}
    details = _sanitize_failure_json(raw_details)
    if not isinstance(details, dict):
        raise TypeError("dependency failure details must canonicalize to an object")
    return row.error_code, _sanitize_failure_text(message), details


_NON_SEMANTIC_IDENTITY_KEYS = frozenset(
    {
        "asset_public_id",
        "dependency_public_id",
        "job_public_id",
        "replay_public_id",
    }
)


def _semantic_identity_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _semantic_identity_json(value[key])
            for key in sorted(value)
            if key not in _NON_SEMANTIC_IDENTITY_KEYS and not key.endswith("_at")
        }
    if isinstance(value, (list, tuple)):
        return [_semantic_identity_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"dependency identity contains non-JSON value: {type(value).__name__}")


def _dependency_identity(row: Job) -> dict[str, Any]:
    input_json = row.input_json
    if not isinstance(input_json, Mapping):
        raise TypeError("dependency has invalid canonical input")
    identity: dict[str, Any] = {
        "stage": row.stage,
        "component_version": row.component_version,
        "status": row.status,
        "input": _semantic_identity_json(input_json),
    }
    if row.status == "succeeded":
        output = row.output_json
        if not isinstance(output, Mapping):
            raise TypeError("succeeded dependency has invalid canonical output")
        identity["output"] = _semantic_identity_json(output)
        return identity
    if row.status == "failed" and not row.retryable:
        code, message, details = _dependency_failure_evidence(row)
        identity["error"] = {
            "code": code,
            "message": message,
            "details": _semantic_identity_json(details),
        }
        return identity
    raise TypeError("dependency identity requires a terminal status")


def _canonical_output(output: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(output, Mapping):
        raise TypeError("stage handler output must be a mapping")
    canonical = json.loads(canonical_json(dict(output)))
    if not isinstance(canonical, dict):
        raise TypeError("stage handler output must canonicalize to an object")
    return cast(dict[str, Any], canonical)


# TheSuperHackers @bugfix Leex 22/08/2026 Reject reference bytes that change while a parser or acquirer consumes them. (#TBD)
def _consume_replay(
    replay_input: _ReplayInput,
    expected_sha256: str,
    consumer: Callable[[Path], _ResultT],
) -> _ResultT:
    if replay_input.revalidate_reference:
        _validate_reference_digest(replay_input.path, expected_sha256)
    try:
        return consumer(replay_input.path)
    finally:
        if replay_input.revalidate_reference:
            _validate_reference_digest(replay_input.path, expected_sha256)


def _validate_reference_digest(path: Path, expected_sha256: str) -> None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink() or _is_reparse(info):
            raise OSError("reference is no longer an ordinary file")
        actual_sha256, _size = _hash_file(path)
    except OSError as error:
        raise StageFailure(
            "source_changed",
            "reference source changed while it was being consumed",
            retryable=False,
            details={"reason": type(error).__name__},
        ) from error
    if actual_sha256 != expected_sha256:
        raise StageFailure(
            "source_changed",
            "reference source changed while it was being consumed",
            retryable=False,
            details={"expected_sha256": expected_sha256, "actual_sha256": actual_sha256},
        )


def _validated_logical_path(logical_path: str, seen_casefolded: set[str]) -> str:
    if (
        not logical_path
        or logical_path.startswith("/")
        or logical_path.endswith("/")
        or "\\" in logical_path
        or ":" in logical_path
        or "\x00" in logical_path
    ):
        raise ValueError("telemetry artifact logical path is unsafe")
    segments = logical_path.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("telemetry artifact logical path is unsafe")
    casefolded = logical_path.casefold()
    if casefolded in seen_casefolded:
        raise ValueError("telemetry artifact logical paths must be Windows-case-insensitively unique")
    seen_casefolded.add(casefolded)
    return logical_path


def _safe_metadata_text(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value or not value.isprintable():
        return None
    return value


def _safe_run_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return value if str(parsed) == value else None


def _safe_sha256(value: object) -> str | None:
    if value is None:
        return None
    if (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    ):
        return value
    return None


def _artifact_candidate_paths(artifact: TelemetryArtifact) -> tuple[Path, ...]:
    candidates: list[Path] = []

    def append(value: object) -> None:
        if isinstance(value, Path):
            candidates.append(value)
        elif isinstance(value, str) and value:
            try:
                candidates.append(Path(value))
            except (TypeError, ValueError):
                return

    for path_value in (
        artifact.trace_path,
        artifact.catalog_path,
        artifact.outcome_path,
        artifact.stdout_path,
        artifact.stderr_path,
    ):
        append(path_value)
    if isinstance(artifact.map_asset_paths, (list, tuple)):
        for value in artifact.map_asset_paths:
            append(value)
    return tuple(candidates)


def _redacted_artifact_message(
    message: str,
    replay_path: Path,
    artifact: TelemetryArtifact,
    descriptors: Iterable[_ArtifactDescriptor],
    *,
    reject_residual_path: bool = True,
) -> str:
    replacements: dict[str, str] = {}

    def register(path: Path, replacement: str) -> None:
        replacements.setdefault(str(path), replacement)
        replacements.setdefault(path.as_posix(), replacement)

    register(replay_path, "[replay]")
    register(replay_path.parent, "[replay-root]")
    for path in _artifact_candidate_paths(artifact):
        register(path, "[artifact]")
        register(path.parent, "[artifact-root]")
    for descriptor in descriptors:
        register(descriptor.path, "[artifact]")
        register(descriptor.path.parent, "[artifact-root]")
    redacted = message
    for source_text, replacement in sorted(
        replacements.items(),
        key=lambda item: (-len(item[0]), item[0].casefold(), item[1]),
    ):
        if source_text:
            redacted = re.sub(re.escape(source_text), replacement, redacted, flags=re.IGNORECASE)
    if reject_residual_path and _contains_pathlike_text(redacted):
        raise ValueError("telemetry metadata contains unregistered path provenance")
    return _sanitize_failure_text(redacted)


def _sorted_artifact_manifest(
    manifest: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    values = [dict(item) for item in manifest]
    values.sort(
        key=lambda item: (
            cast(str, item["logical_path"]).casefold(),
            cast(str, item["kind"]),
            cast(str, item["sha256"]),
        )
    )
    return values


def _telemetry_attempt_facts(
    artifact: TelemetryArtifact,
    replay_path: Path,
    descriptors: tuple[_ArtifactDescriptor, ...],
    manifest: tuple[Mapping[str, Any], ...],
    *,
    reject_residual_paths: bool = True,
) -> dict[str, Any]:
    """Return only validated scalar facts and already registered managed descriptors."""
    facts: dict[str, Any] = {"artifacts": _sorted_artifact_manifest(manifest)}
    run_id = _safe_run_id(artifact.run_id)
    if run_id is not None:
        facts["run_id"] = run_id
    for name, value in (
        ("runner_status", artifact.runner_status),
        ("replay_quality", artifact.replay_quality),
        ("strategy_analysis_scope", artifact.strategy_analysis_scope),
    ):
        safe = _safe_metadata_text(value)
        if safe is not None:
            facts[name] = _redacted_artifact_message(
                safe,
                replay_path,
                artifact,
                descriptors,
                reject_residual_path=reject_residual_paths,
            )
    if artifact.exit_code is None or (
        isinstance(artifact.exit_code, int)
        and not isinstance(artifact.exit_code, bool)
        and artifact.exit_code >= 0
    ):
        facts["exit_code"] = artifact.exit_code
    if artifact.engine_build is None:
        facts["engine_build"] = None
    else:
        engine_build = _safe_metadata_text(artifact.engine_build)
        if engine_build is not None:
            facts["engine_build"] = _redacted_artifact_message(
                engine_build,
                replay_path,
                artifact,
                descriptors,
                reject_residual_path=reject_residual_paths,
            )
    if artifact.engine_executable_sha256 is None:
        facts["engine_executable_sha256"] = None
    else:
        engine_hash = _safe_sha256(artifact.engine_executable_sha256)
        if engine_hash is not None:
            facts["engine_executable_sha256"] = engine_hash
    diagnostics: list[dict[str, str]] = []
    if isinstance(artifact.diagnostics, tuple):
        for diagnostic in artifact.diagnostics:
            if not isinstance(diagnostic, AcquisitionDiagnostic):
                continue
            code = _safe_metadata_text(diagnostic.code)
            message = _safe_metadata_text(diagnostic.message)
            if code is None or message is None:
                continue
            diagnostics.append(
                {
                    "code": _redacted_artifact_message(
                        code,
                        replay_path,
                        artifact,
                        descriptors,
                        reject_residual_path=reject_residual_paths,
                    ),
                    "message": _redacted_artifact_message(
                        message,
                        replay_path,
                        artifact,
                        descriptors,
                        reject_residual_path=reject_residual_paths,
                    ),
                }
            )
    facts["diagnostics"] = diagnostics
    return _canonical_output(facts)


def _telemetry_failure_details(
    artifact: TelemetryArtifact,
    replay_path: Path,
    *,
    failure_code: str,
    failure_message: str,
    quality_issue_code: str,
    descriptors: tuple[_ArtifactDescriptor, ...],
    manifest: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    # TheSuperHackers @bugfix Leex 22/08/2026 Carry a typed path-free attempt across post-acquisition failures. (#TBD)
    return _canonical_output(
        {
            "failure_envelope": {
                "type": _TELEMETRY_FAILURE_ENVELOPE_TYPE,
                "version": _TELEMETRY_FAILURE_ENVELOPE_VERSION,
                "failure_code": failure_code,
                "failure_message": failure_message,
                "quality_issue_code": quality_issue_code,
                "attempt": _telemetry_attempt_facts(
                    artifact,
                    replay_path,
                    descriptors,
                    manifest,
                    reject_residual_paths=False,
                ),
            }
        }
    )


def _validated_artifact_paths(artifact: TelemetryArtifact) -> list[_ArtifactDescriptor]:
    if not isinstance(artifact.map_asset_paths, tuple) or any(
        not isinstance(path, Path) for path in artifact.map_asset_paths
    ):
        raise ValueError("telemetry map artifact paths must be an immutable path tuple")
    if not isinstance(artifact.diagnostics, tuple) or any(
        not isinstance(diagnostic, AcquisitionDiagnostic)
        for diagnostic in artifact.diagnostics
    ):
        raise ValueError("telemetry diagnostics must be an immutable typed tuple")
    for path_value in (
        artifact.trace_path,
        artifact.catalog_path,
        artifact.outcome_path,
        artifact.stdout_path,
        artifact.stderr_path,
    ):
        if path_value is not None and not isinstance(path_value, Path):
            raise ValueError("telemetry artifact paths must be pathlib paths")
    for metadata_value in (
        artifact.runner_status,
        artifact.replay_quality,
        artifact.strategy_analysis_scope,
    ):
        if _safe_metadata_text(metadata_value) is None:
            raise ValueError("telemetry status, quality, and scope must be safe nonempty text")
    if artifact.engine_build is not None and _safe_metadata_text(artifact.engine_build) is None:
        raise ValueError("telemetry engine build must be safe nonempty text")
    try:
        parsed_uuid = UUID(artifact.run_id)
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("telemetry run_id must be a lowercase hyphenated UUID") from error
    if str(parsed_uuid) != artifact.run_id:
        raise ValueError("telemetry run_id must be a lowercase hyphenated UUID")
    if artifact.exit_code is not None and (
        not isinstance(artifact.exit_code, int) or isinstance(artifact.exit_code, bool) or artifact.exit_code < 0
    ):
        raise ValueError("telemetry exit_code must be a nonnegative integer")
    engine_hash = artifact.engine_executable_sha256
    if engine_hash is not None and (
        not isinstance(engine_hash, str)
        or len(engine_hash) != 64
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
    if artifact.runner_status == "success" and artifact.trace_path is None:
        raise ValueError("successful telemetry acquisition requires a trace")

    retained_paths: list[tuple[str, Path, Path]] = []
    seen_paths: set[str] = set()
    for kind, possible_path in values:
        if possible_path is None:
            continue
        # TheSuperHackers @bugfix Leex 22/08/2026 Reject supplied aliases before resolving artifact uniqueness. (#TBD)
        try:
            info = possible_path.lstat()
        except OSError as error:
            raise ValueError(f"telemetry artifact is unavailable: {possible_path.name}") from error
        if not stat.S_ISREG(info.st_mode) or possible_path.is_symlink() or _is_reparse(info):
            raise ValueError("telemetry artifacts must be ordinary non-symlink files")
        path = possible_path.resolve(strict=False)
        path_key = os.path.normcase(str(path)).casefold()
        if path_key in seen_paths:
            raise ValueError("telemetry artifact paths must be unique")
        seen_paths.add(path_key)
        retained_paths.append((kind, possible_path, path))

    descriptors: list[_ArtifactDescriptor] = []
    seen_logical_paths: set[str] = set()
    if artifact.trace_path is not None:
        lexical_root = artifact.trace_path.parent
        resolved_root = lexical_root.resolve(strict=False)
        for kind, supplied_path, path in retained_paths:
            try:
                relative = supplied_path.relative_to(lexical_root)
            except ValueError as error:
                raise ValueError("telemetry artifacts must remain within the trace bundle root") from error
            logical_path = _validated_logical_path(relative.as_posix(), seen_logical_paths)
            expected_path = resolved_root.joinpath(*logical_path.split("/")).resolve(strict=False)
            try:
                path.relative_to(resolved_root)
            except ValueError as error:
                raise ValueError("telemetry artifacts must remain within the trace bundle root") from error
            if path != expected_path:
                raise ValueError("telemetry artifact topology cannot contain aliases or traversal")
            if kind == "telemetry_catalog":
                _validate_catalog_logical_path(logical_path)
            elif kind == "telemetry_map_asset":
                _validate_map_logical_path(logical_path)
            descriptors.append(_ArtifactDescriptor(kind, path, logical_path))
        return descriptors

    role_names = {
        "telemetry_catalog": "catalog.json",
        "telemetry_outcome": "outcome.json",
        "telemetry_stdout": "stdout.log",
        "telemetry_stderr": "stderr.log",
    }
    for kind, _supplied_path, path in retained_paths:
        if kind == "telemetry_map_asset":
            try:
                sha256, _size = _hash_file(path)
            except OSError as error:
                raise ValueError("telemetry map artifact is unavailable") from error
            logical_path = f"map-assets/{sha256}.asset"
        else:
            logical_path = role_names[kind]
        descriptors.append(
            _ArtifactDescriptor(kind, path, _validated_logical_path(logical_path, seen_logical_paths))
        )
    return descriptors


def _validate_catalog_logical_path(logical_path: str) -> None:
    prefix = "game-data-catalog-v1-"
    suffix = ".json"
    if "/" in logical_path or not logical_path.startswith(prefix) or not logical_path.endswith(suffix):
        raise ValueError("telemetry catalog must use its validated bundle basename")
    digest = logical_path[len(prefix) : -len(suffix)]
    if len(digest) != 64 or digest != digest.lower() or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError("telemetry catalog basename must contain a lowercase SHA-256")


def _validate_map_logical_path(logical_path: str) -> None:
    segments = logical_path.split("/")
    if len(segments) < 3 or segments[0] not in {"map-assets-v1", "map-assets-v2"}:
        raise ValueError("telemetry map artifacts must preserve validated bundle topology")
    content_hash = segments[1]
    if len(content_hash) != 64 or content_hash != content_hash.lower() or any(
        character not in "0123456789abcdef" for character in content_hash
    ):
        raise ValueError("telemetry map bundle identity must be a lowercase SHA-256")


def _redacted_diagnostic_message(
    message: str,
    replay_path: Path,
    descriptors: Iterable[_ArtifactDescriptor],
) -> str:
    replacements: dict[str, str] = {}

    def register(path: Path, replacement: str) -> None:
        replacements.setdefault(str(path), replacement)
        replacements.setdefault(path.as_posix(), replacement)

    for path_value, replacement in (
        (replay_path, "[replay]"),
        (replay_path.parent, "[replay-root]"),
    ):
        register(path_value, replacement)
    for descriptor in descriptors:
        register(descriptor.path, "[artifact]")
        register(descriptor.path.parent, "[artifact-root]")
    redacted = message
    for source_text, replacement in sorted(
        replacements.items(),
        key=lambda item: (-len(item[0]), item[0].casefold(), item[1]),
    ):
        if source_text:
            redacted = re.sub(re.escape(source_text), replacement, redacted, flags=re.IGNORECASE)
    return redacted
