"""Behavior tests for transactional replay intake and public DTO boundaries."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased, sessionmaker

from generals_replay_analyzer.cli import main
from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db import create_database_engine, create_session_factory
from generals_replay_analyzer.db.models import (
    Job,
    JobDependency,
    ManagedAsset,
    ParserRun,
    Player,
    PlayerAlias,
    Replay,
    ReplayCommand,
    ReplayPlayer,
    ReplayQualityIssue,
    Source,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.importing import (
    AcquisitionDiagnostic,
    ImportRequest,
    ImportService,
    TelemetryArtifact,
)
from generals_replay_analyzer.storage import ContentAddressedStore

from .conftest import PINNED_REPLAY, MutableClock


def _drain(service: ImportService, *, worker: str = "worker-a", maximum: int = 20) -> None:
    for _ in range(maximum):
        if not service.run_available(worker):
            return
    raise AssertionError("registered import stages did not drain")


def _service(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    clock: MutableClock,
    *,
    parser: object | None = None,
    acquirer: object | None = None,
) -> ImportService:
    if parser is None:
        parser = _successful_parser
    return ImportService(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        parser=parser,
        telemetry_acquirer=acquirer,
        clock=clock,
        parser_version="test-parser-1",
        telemetry_acquirer_version="test-acquirer-1",
    )


def _successful_parser(path: Path) -> SimpleNamespace:
    assert path.is_file()
    return SimpleNamespace(
        commands=(object(), object()),
        warnings=(SimpleNamespace(code="fixture_warning"),),
        command_stream_offset=32,
        end_offset=96,
        completion_status="complete",
    )


def _rows(factory: sessionmaker[Session], model: type[object]) -> list[object]:
    with factory() as session:
        return list(session.scalars(select(model).order_by(model.id)))  # type: ignore[attr-defined]


def test_single_file_creates_provenance_lowercase_replay_and_expected_dag(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    service = _service(session_factory, settings, replay_store, artifact_store, clock)

    submission = service.submit(ImportRequest(replay_file))
    assert submission.discovery_job.stage == "discover"
    assert submission.discovery_job.status == "pending"
    _drain(service)

    expected_sha = hashlib.sha256(replay_file.read_bytes()).hexdigest()
    with session_factory() as session:
        source = session.scalar(select(Source))
        replay = session.scalar(select(Replay))
        assert source is not None and replay is not None
        assert source.replay_id == replay.id
        assert source.strata_match_id == "3133811"
        assert source.strata_source_user_token == "ABCDEF"
        assert replay.sha256 == expected_sha == expected_sha.lower()
        assert replay.lifecycle_state == "discovered"
        stages = set(session.scalars(select(Job.stage)))
        assert stages == {
            "discover",
            "hash",
            "manage_copy",
            "parse",
            "import_observations",
            "derive_features",
            "assess_strategies",
            "analyze_llm",
            "render_report",
        }
        dependent_job = aliased(Job)
        dependency_job = aliased(Job)
        edge_stages = {
            (job.stage, dependency.stage)
            for job, dependency in session.execute(
                select(dependent_job, dependency_job)
                .join(JobDependency, JobDependency.job_id == dependent_job.id)
                .join(dependency_job, JobDependency.depends_on_job_id == dependency_job.id)
            )
        }
        assert ("parse", "manage_copy") in edge_stages
        assert ("import_observations", "parse") in edge_stages
        assert ("render_report", "assess_strategies") in edge_stages
        assert ("render_report", "analyze_llm") not in edge_stages

    result = service.result_for_replay(replay.public_id)
    assert result.sha256 == expected_sha
    assert result.source_public_ids == (source.public_id,)
    assert all("\\" not in job.public_id and "/" not in job.public_id for job in result.jobs)


def test_folder_snapshot_filters_sorts_and_respects_recursion(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    folder = tmp_path / "snapshot"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    shutil.copyfile(PINNED_REPLAY, folder / "b.REP")
    shutil.copyfile(PINNED_REPLAY, folder / "A.rep")
    shutil.copyfile(PINNED_REPLAY, nested / "c.rep")
    (folder / "notes.txt").write_text("not a replay", encoding="utf-8")
    symlink = folder / "linked.rep"
    symlink_created = True
    try:
        symlink.symlink_to(folder / "A.rep")
    except OSError:
        symlink_created = False

    first = _service(session_factory, settings, replay_store, artifact_store, clock)
    first.submit(ImportRequest(folder))
    first.run_available("discovery-only")
    with session_factory() as session:
        names = list(session.scalars(select(Source.original_filename).order_by(Source.id)))
        discovery = session.scalar(select(Job).where(Job.stage == "discover"))
        assert discovery is not None
        assert names == ["A.rep", "b.REP"]
        assert discovery.output_json["accepted_path_count"] == 2
        assert discovery.output_json["rejected_path_count"] >= 1 + int(symlink_created)

    second = _service(session_factory, settings, replay_store, artifact_store, clock)
    second.submit(ImportRequest(folder, recursive=True))
    _drain(second, worker="recursive-discovery")
    with session_factory() as session:
        names = list(session.scalars(select(Source.original_filename).order_by(Source.id)))
        assert names[-3:] == ["A.rep", "b.REP", "c.rep"]


def test_duplicate_bytes_keep_distinct_sources_and_coalesce_content_jobs(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    first_path = tmp_path / "first.rep"
    second_path = tmp_path / "second.rep"
    shutil.copyfile(PINNED_REPLAY, first_path)
    shutil.copyfile(PINNED_REPLAY, second_path)
    service = _service(session_factory, settings, replay_store, artifact_store, clock)

    service.submit(ImportRequest(first_path))
    _drain(service)
    service.submit(ImportRequest(second_path))
    _drain(service)

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Source)) == 2
        assert session.scalar(select(func.count()).select_from(Replay)) == 1
        assert session.scalar(select(func.count()).select_from(Job).where(Job.stage == "hash")) == 1
        assert session.scalar(select(func.count()).select_from(Job).where(Job.stage == "parse")) == 1
        assert len(set(session.scalars(select(Source.replay_id)))) == 1


def test_copy_survives_source_deletion_but_reference_mode_reports_source_missing(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    copied = tmp_path / "copied.rep"
    shutil.copyfile(PINNED_REPLAY, copied)
    copy_service = _service(session_factory, settings, replay_store, artifact_store, clock)
    copy_service.submit(ImportRequest(copied))
    copy_service.run_available("copy")  # discover
    copy_service.run_available("copy")  # hash
    copy_service.run_available("copy")  # manage_copy
    copied.unlink()
    parsed = copy_service.run_available("copy")
    assert parsed[0].stage == "parse" and parsed[0].status == "succeeded"
    with session_factory() as session:
        replay = session.scalar(select(Replay))
        asset = session.scalar(select(ManagedAsset).where(ManagedAsset.kind == "replay"))
        assert replay is not None and asset is not None
        assert replay.managed_asset_id == asset.id
        assert not Path(asset.relative_path).is_absolute()
        assert "\\" not in asset.relative_path

    referenced = tmp_path / "referenced.rep"
    referenced.write_bytes(b"distinct-reference-replay")
    reference_service = _service(session_factory, settings, replay_store, artifact_store, clock)
    reference_service.submit(ImportRequest(referenced, reference_only=True))
    reference_service.run_available("reference")
    reference_service.run_available("reference")
    reference_service.run_available("reference")
    referenced.unlink()
    failed = reference_service.run_available("reference")
    assert failed[0].stage == "parse"
    assert failed[0].status == "failed"
    assert failed[0].error_code == "source_missing"
    with session_factory() as session:
        reference_replay = session.scalar(select(Replay).where(Replay.sha256 == hashlib.sha256(b"distinct-reference-replay").hexdigest()))
        assert reference_replay is not None and reference_replay.managed_asset_id is None


def test_parser_failure_retains_job_diagnostics_and_zero_observations(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    def broken_parser(path: Path) -> object:
        raise OSError(f"fixture parser cannot read {path.name}")

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        parser=broken_parser,
    )
    service.submit(ImportRequest(replay_file))
    for _ in range(4):
        jobs = service.run_available("parser-worker")
    assert jobs[0].stage == "parse"
    assert jobs[0].status == "pending"
    assert jobs[0].retryable is True
    assert jobs[0].error_code == "parser_failed"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ParserRun)) == 0
        assert session.scalar(select(func.count()).select_from(ReplayCommand)) == 0
        assert session.scalar(select(func.count()).select_from(ReplayPlayer)) == 0


class _FakeAcquirer:
    def __init__(self, artifact: TelemetryArtifact) -> None:
        self.artifact = artifact

    def acquire(self, replay: Path, replay_sha256: str) -> TelemetryArtifact:
        assert replay.is_file()
        assert replay_sha256 == replay_sha256.lower()
        return self.artifact


class _MutatingAcquirer:
    def __init__(self, artifact: TelemetryArtifact) -> None:
        self.artifact = artifact

    def acquire(self, replay: Path, replay_sha256: str) -> TelemetryArtifact:
        replay.write_bytes(replay.read_bytes() + b"telemetry-mutation")
        return self.artifact


def _artifact(tmp_path: Path, *, status: str = "success") -> TelemetryArtifact:
    trace = tmp_path / "trace.ndjson"
    stdout = tmp_path / "stdout.log"
    trace.write_text('{"type":"fixture"}\n', encoding="utf-8")
    stdout.write_text("fixture output", encoding="utf-8")
    return TelemetryArtifact(
        run_id=str(uuid4()),
        runner_status=status,
        replay_quality="complete" if status == "success" else "failed",
        strategy_analysis_scope="full" if status == "success" else "none",
        trace_path=trace if status == "success" else None,
        catalog_path=None,
        map_asset_paths=(),
        outcome_path=None,
        stdout_path=stdout,
        stderr_path=None,
        exit_code=0 if status == "success" else 7,
        engine_build="fixture-build",
        engine_executable_sha256="a" * 64,
        diagnostics=(AcquisitionDiagnostic("fixture", "diagnostic retained"),),
    )


@pytest.mark.parametrize("runner_status", ["success", "nonzero_engine_failure"])
def test_telemetry_artifacts_and_failure_diagnostics_are_retained_without_observations(
    runner_status: str,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    artifact = _artifact(tmp_path, status=runner_status)
    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_FakeAcquirer(artifact),
    )
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    _drain(service)

    with session_factory() as session:
        telemetry_job = session.scalar(select(Job).where(Job.stage == "telemetry"))
        assert telemetry_job is not None
        payload = telemetry_job.output_json if runner_status == "success" else telemetry_job.error_details_json
        assert payload["runner_status"] == runner_status
        assert payload["diagnostics"] == [{"code": "fixture", "message": "diagnostic retained"}]
        assert any(asset["kind"] == "telemetry_stdout" for asset in payload["artifacts"])
        assert "run_path" not in json.dumps(payload)
        assert session.scalar(select(func.count()).select_from(TelemetryRun)) == 0
        assert session.scalar(select(func.count()).select_from(TelemetryEvent)) == 0


def test_source_provenance_never_creates_identity_or_missing_telemetry_issue(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    parser_only = _service(session_factory, settings, replay_store, artifact_store, clock)
    parser_only.submit(ImportRequest(replay_file))
    _drain(parser_only)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Player)) == 0
        assert session.scalar(select(func.count()).select_from(PlayerAlias)) == 0
        assert session.scalar(select(func.count()).select_from(ReplayPlayer)) == 0
        assert session.scalar(select(func.count()).select_from(ReplayQualityIssue)) == 0
        assert session.scalar(select(func.count()).select_from(Job).where(Job.stage == "telemetry")) == 0
        import_job = session.scalar(select(Job).where(Job.stage == "import_observations"))
        parse_job = session.scalar(select(Job).where(Job.stage == "parse"))
        assert import_job is not None and parse_job is not None
        dependencies = set(
            session.scalars(
                select(JobDependency.depends_on_job_id).where(JobDependency.job_id == import_job.id)
            )
        )
        assert dependencies == {parse_job.id}


def test_reference_parser_revalidates_bytes_after_consumption(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    def mutating_parser(path: Path) -> SimpleNamespace:
        path.write_bytes(path.read_bytes() + b"parser-mutation")
        return _successful_parser(path)

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        parser=mutating_parser,
    )
    service.submit(ImportRequest(replay_file, reference_only=True))
    for _ in range(4):
        result = service.run_available("reference-parser")
    assert result[0].stage == "parse"
    assert result[0].status == "failed"
    assert result[0].error_code == "source_changed"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ParserRun)) == 0


def test_reference_telemetry_revalidates_bytes_before_copying_artifacts(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_MutatingAcquirer(_artifact(tmp_path)),
    )
    service.submit(ImportRequest(replay_file, reference_only=True, request_telemetry=True))
    for _ in range(5):
        result = service.run_available("reference-telemetry")
    assert result[0].stage == "telemetry"
    assert result[0].status == "failed"
    assert result[0].error_code == "source_changed"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ManagedAsset)) == 0
        assert session.scalar(select(func.count()).select_from(TelemetryEvent)) == 0


def test_failed_reference_branch_cannot_block_later_successful_copy_branch(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    original_bytes = b"same-replay-content"
    reference = tmp_path / "stale-reference.rep"
    copied = tmp_path / "fresh-copy.rep"
    reference.write_bytes(original_bytes)
    copied.write_bytes(original_bytes)
    parser_calls = 0

    def first_parser_fails_after_mutation(path: Path) -> SimpleNamespace:
        nonlocal parser_calls
        parser_calls += 1
        if parser_calls == 1:
            path.write_bytes(path.read_bytes() + b"stale")
            raise ValueError("reference parser observed unstable bytes")
        return _successful_parser(path)

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        parser=first_parser_fails_after_mutation,
    )
    service.submit(ImportRequest(reference, reference_only=True))
    for _ in range(4):
        reference_result = service.run_available("reference")
    assert reference_result[0].error_code == "source_changed"
    assert service.run_available("dependency-projector") == ()

    service.submit(ImportRequest(copied))
    _drain(service, worker="copy")
    with session_factory() as session:
        parse_jobs = list(session.scalars(select(Job).where(Job.stage == "parse").order_by(Job.id)))
        assert [(job.input_json["import_mode"], job.status) for job in parse_jobs] == [
            ("reference", "failed"),
            ("copy", "succeeded"),
        ]
        import_jobs = list(
            session.scalars(select(Job).where(Job.stage == "import_observations").order_by(Job.id))
        )
        assert len(import_jobs) == 2
        assert [job.status for job in import_jobs] == ["failed", "pending"]
        copy_dependencies = set(
            session.scalars(
                select(JobDependency.depends_on_job_id).where(JobDependency.job_id == import_jobs[1].id)
            )
        )
        assert copy_dependencies == {parse_jobs[1].id}


def test_artifact_port_rejects_duplicate_or_unsafe_paths_without_observations(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    artifact = _artifact(tmp_path)
    malformed = replace(artifact, map_asset_paths=(artifact.stdout_path,))
    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_FakeAcquirer(malformed),
    )
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    _drain(service)
    with session_factory() as session:
        job = session.scalar(select(Job).where(Job.stage == "telemetry"))
        assert job is not None and job.status == "failed"
        assert job.error_code == "invalid_telemetry_artifact"
        assert session.scalar(select(func.count()).select_from(TelemetryEvent)) == 0


def test_artifact_port_rejects_supplied_symlink_before_path_canonicalization(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    artifact = _artifact(tmp_path)
    assert artifact.stdout_path is not None
    linked_stdout = tmp_path / "linked-stdout.log"
    try:
        linked_stdout.symlink_to(artifact.stdout_path)
    except OSError as error:
        pytest.skip(f"symlink/reparse creation is unavailable: {error}")
    assert linked_stdout.is_symlink()
    malformed = replace(artifact, stdout_path=linked_stdout)
    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_FakeAcquirer(malformed),
    )
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    _drain(service)
    with session_factory() as session:
        job = session.scalar(select(Job).where(Job.stage == "telemetry"))
        assert job is not None and job.status == "failed"
        assert job.error_code == "invalid_telemetry_artifact"
        assert session.scalar(select(func.count()).select_from(TelemetryEvent)) == 0


def test_artifact_port_inspects_supplied_alias_before_resolving_target(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact(tmp_path)
    assert artifact.stdout_path is not None
    target = artifact.stdout_path
    supplied_alias = tmp_path / "synthetic-reparse.log"
    original_resolve = Path.resolve
    original_lstat = Path.lstat
    original_is_symlink = Path.is_symlink
    resolved_alias = False

    def resolve_alias(path: Path, *args: object, **kwargs: object) -> Path:
        nonlocal resolved_alias
        if path == supplied_alias:
            resolved_alias = True
            return target
        return original_resolve(path, *args, **kwargs)  # type: ignore[arg-type]

    def lstat_alias(path: Path) -> object:
        if path == supplied_alias:
            return SimpleNamespace(st_mode=target.lstat().st_mode, st_file_attributes=0x400)
        return original_lstat(path)

    def identify_alias(path: Path) -> bool:
        if path == supplied_alias:
            return False
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "resolve", resolve_alias)
    monkeypatch.setattr(Path, "lstat", lstat_alias)
    monkeypatch.setattr(Path, "is_symlink", identify_alias)
    malformed = replace(artifact, stdout_path=supplied_alias)
    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_FakeAcquirer(malformed),
    )
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    _drain(service)
    with session_factory() as session:
        job = session.scalar(select(Job).where(Job.stage == "telemetry"))
        assert job is not None and job.status == "failed"
        assert job.error_code == "invalid_telemetry_artifact"
    assert resolved_alias is False


@pytest.mark.parametrize(
    "malformation",
    ["invalid_uuid", "uppercase_uuid", "negative_exit", "uppercase_hash", "missing_trace", "missing_file", "directory"],
)
def test_artifact_port_rejects_each_malformed_public_field(
    malformation: str,
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    artifact = _artifact(tmp_path)
    if malformation == "invalid_uuid":
        artifact = replace(artifact, run_id="not-a-uuid")
    elif malformation == "uppercase_uuid":
        artifact = replace(artifact, run_id=artifact.run_id.upper())
    elif malformation == "negative_exit":
        artifact = replace(artifact, exit_code=-1)
    elif malformation == "uppercase_hash":
        artifact = replace(artifact, engine_executable_sha256="A" * 64)
    elif malformation == "missing_trace":
        artifact = replace(artifact, trace_path=None)
    elif malformation == "missing_file":
        assert artifact.stdout_path is not None
        artifact.stdout_path.unlink()
    else:
        artifact = replace(artifact, stdout_path=tmp_path)

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        acquirer=_FakeAcquirer(artifact),
    )
    service.submit(ImportRequest(replay_file, request_telemetry=True))
    _drain(service)
    with session_factory() as session:
        job = session.scalar(select(Job).where(Job.stage == "telemetry"))
        assert job is not None and job.error_code == "invalid_telemetry_artifact"
        assert session.scalar(select(func.count()).select_from(TelemetryEvent)) == 0


def test_import_boundary_rejects_missing_nonreplay_and_symlink_paths(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    with pytest.raises(ValueError, match="unavailable"):
        service.submit(ImportRequest(tmp_path / "missing.rep"))
    nonreplay = tmp_path / "notes.txt"
    nonreplay.write_text("not replay", encoding="utf-8")
    with pytest.raises(ValueError, match="extension"):
        service.submit(ImportRequest(nonreplay))
    symlink = tmp_path / "link.rep"
    try:
        symlink.symlink_to(nonreplay)
    except OSError:
        return
    with pytest.raises(ValueError, match="symlink"):
        service.submit(ImportRequest(symlink))


def test_changed_or_missing_copy_source_fails_before_managed_publication(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    changed = tmp_path / "changed.rep"
    changed.write_bytes(b"first")
    service = _service(session_factory, settings, replay_store, artifact_store, clock)
    service.submit(ImportRequest(changed))
    service.run_available("changed")
    changed.write_bytes(b"second")
    hash_failure = service.run_available("changed")
    assert hash_failure[0].status == "failed" and hash_failure[0].error_code == "source_changed"

    missing = tmp_path / "missing-copy.rep"
    missing.write_bytes(b"copy-source")
    service.submit(ImportRequest(missing))
    service.run_available("missing")
    service.run_available("missing")
    missing.unlink()
    copy_failure = service.run_available("missing")
    assert copy_failure[0].stage == "manage_copy"
    assert copy_failure[0].status == "failed" and copy_failure[0].error_code == "source_missing"


def test_non_io_parser_failure_is_terminal_and_limit_must_be_positive(
    session_factory: sessionmaker[Session],
    settings: AnalyzerSettings,
    replay_store: ContentAddressedStore,
    artifact_store: ContentAddressedStore,
    replay_file: Path,
    clock: MutableClock,
) -> None:
    def invalid_parser(path: Path) -> object:
        raise ValueError(path.name)

    service = _service(
        session_factory,
        settings,
        replay_store,
        artifact_store,
        clock,
        parser=invalid_parser,
    )
    with pytest.raises(ValueError, match="limit"):
        service.run_available("worker", limit=0)
    service.submit(ImportRequest(replay_file))
    for _ in range(4):
        result = service.run_available("worker")
    assert result[0].status == "failed"
    assert result[0].retryable is False and result[0].error_code == "parser_failed"


def test_cli_import_json_reference_only_and_jobs_retry_use_external_product_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_root = tmp_path / "cli-product-data"
    replay = tmp_path / "cli.rep"
    shutil.copyfile(PINNED_REPLAY, replay)
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_DATA_ROOT", str(data_root))

    assert main(["import", str(replay), "--reference-only", "--json"]) == 0
    import_document = json.loads(capsys.readouterr().out)
    assert import_document["accepted_path_count"] == 0
    assert import_document["discovery_job"]["stage"] == "discover"
    job_id = import_document["discovery_job"]["public_id"]
    assert str(data_root) not in json.dumps(import_document)
    assert str(replay) not in json.dumps(import_document)

    assert main(["jobs", "retry", job_id]) == 0
    retry_document = json.loads(capsys.readouterr().out)
    assert retry_document["public_id"] == job_id
    assert retry_document["status"] == "pending"

    engine = create_database_engine(data_root / "replay-analyzer.sqlite3")
    try:
        factory = create_session_factory(engine)
        with factory() as session:
            job = session.scalar(select(Job).where(Job.public_id == job_id))
            assert job is not None and job.input_json["import_mode"] == "reference"
    finally:
        engine.dispose()
