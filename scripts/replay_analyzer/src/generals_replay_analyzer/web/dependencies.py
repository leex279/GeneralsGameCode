"""Request-scoped web application port dependencies."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Annotated, Any, Protocol, Self, cast

from fastapi import Depends, Request

from generals_replay_analyzer.web.errors import PublicProblem
from generals_replay_analyzer.web.ports import (
    ApplySettingsCommandDTO,
    AvailabilityDTO,
    CancelJobCommandDTO,
    ComparisonDTO,
    ComparisonResolutionDTO,
    ComparisonSelectionDTO,
    ComponentIdentityDTO,
    DashboardDTO,
    DiagnosticCommandDTO,
    DiagnosticDTO,
    DiagnosticResultDTO,
    EvidenceDetailDTO,
    EvidenceQueryDTO,
    ExecuteIdentityChangeDTO,
    FixedComparisonQueryDTO,
    FixedReportQueryDTO,
    IdentityAuditPageDTO,
    IdentityDraftDTO,
    IdentityLandingDTO,
    IdentityMutationReceiptDTO,
    IdentityPreviewDTO,
    ImportRootDTO,
    ImportSubmissionDTO,
    InvalidationJobReferenceDTO,
    JobDetailDTO,
    JobLogChunkDTO,
    JobLogQueryDTO,
    JobMutationDTO,
    JobPageDTO,
    JobQueryDTO,
    LatestReportQueryDTO,
    MapRasterQueryDTO,
    MapRasterResourceDTO,
    MapSceneDTO,
    MapSceneIndexPageDTO,
    MapSceneIndexQueryDTO,
    MapSceneQueryDTO,
    PlayerIndexPageDTO,
    PlayerIndexQueryDTO,
    PlayerProfileDTO,
    PlayerProfileQueryDTO,
    PlayerProfileResolutionDTO,
    PlayerProfileSelectionDTO,
    ReadinessDTO,
    ReplayLibraryPageDTO,
    ReplayLibraryQueryDTO,
    ReplayReportDTO,
    ReportResolutionDTO,
    RetryJobCommandDTO,
    RootImportCommandDTO,
    SettingsImpactDTO,
    SettingsMutationDTO,
    SettingsPreviewCommandDTO,
    SettingsSnapshotDTO,
    TimelineChartDTO,
    TimelineChartQueryDTO,
    UploadImportCommandDTO,
    WebApplicationPort,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from generals_replay_analyzer.config import AnalyzerSettings, RuntimeConfiguration


class WebApplicationPortFactory(Protocol):
    def __call__(self) -> AbstractContextManager[WebApplicationPort]: ...


@dataclass(frozen=True)
class RootImportForm:
    """Exactly the scalar public fields accepted by a configured-root selection."""

    root_public_id: str
    relative_path: str
    csrf_token: str | None


class ReadinessState(Protocol):
    def schema_revision(self) -> str | None: ...


class UnavailableWebApplicationPort:
    """Expose honest unavailable snapshots until an Analytics adapter is accepted."""

    def __init__(self, readiness: ReadinessState) -> None:
        self._readiness = readiness

    def readiness(self) -> ReadinessDTO:
        revision = self._readiness.schema_revision()
        if revision is None:
            return ReadinessDTO(
                ready=False,
                diagnostics=(DiagnosticDTO(code="bootstrap_incomplete", message="Bootstrap is incomplete"),),
            )
        return ReadinessDTO(ready=True, schema_revision=revision)

    def dashboard(self) -> DashboardDTO:
        return DashboardDTO(
            generated_at=datetime.now(UTC),
            availability=AvailabilityDTO(
                state="unavailable",
                reason_codes=("analytics_adapter_pending",),
            ),
        )

    def identity_landing(self) -> IdentityLandingDTO:
        return IdentityLandingDTO(
            generated_at=datetime.now(UTC),
            availability=AvailabilityDTO(
                state="unavailable",
                reason_codes=("identity_adapter_pending",),
            ),
        )

    # TheSuperHackers @feature Leex 22/08/2026 Keep the null library adapter immutable until Analytics accepts a real port. (#0)
    def list_replays(self, query: ReplayLibraryQueryDTO) -> ReplayLibraryPageDTO:
        return ReplayLibraryPageDTO(
            query=query,
            items=(),
            page=query.page,
            page_size=query.page_size,
            total_items=0,
            availability=AvailabilityDTO(state="unavailable", reason_codes=("replay_library_adapter_pending",)),
        )

    def import_roots(self) -> tuple[ImportRootDTO, ...]:
        return ()

    def submit_upload(self, _command: UploadImportCommandDTO) -> ImportSubmissionDTO:
        return ImportSubmissionDTO(
            submission_public_id="123e4567-e89b-42d3-a456-426614174098",
            availability=AvailabilityDTO(state="unavailable", reason_codes=("opaque_ingress_handoff_pending",)),
            problem_code="opaque_ingress_handoff_pending",
        )

    def submit_root_selection(self, _command: RootImportCommandDTO) -> ImportSubmissionDTO:
        return ImportSubmissionDTO(
            submission_public_id="123e4567-e89b-42d3-a456-426614174099",
            availability=AvailabilityDTO(state="unavailable", reason_codes=("import_adapter_pending",)),
            problem_code="dependency_unavailable",
        )

    def list_jobs(self, query: JobQueryDTO) -> JobPageDTO:
        from generals_replay_analyzer.web.ports import AvailabilityDTO

        return JobPageDTO(
            query=query,
            items=(),
            availability=AvailabilityDTO(state="unavailable", reason_codes=("job_adapter_pending",)),
        )

    def get_job(self, _job_public_id: str) -> JobDetailDTO:
        raise PublicProblem(status=503, code="job_adapter_pending", detail="Job operations are unavailable")

    def retry_job(self, _command: RetryJobCommandDTO) -> JobMutationDTO:
        raise PublicProblem(status=503, code="job_adapter_pending", detail="Job operations are unavailable")

    def cancel_job(self, _command: CancelJobCommandDTO) -> JobMutationDTO:
        raise PublicProblem(status=503, code="job_adapter_pending", detail="Job operations are unavailable")

    def read_job_log(self, _query: JobLogQueryDTO) -> JobLogChunkDTO:
        raise PublicProblem(status=503, code="job_adapter_pending", detail="Job operations are unavailable")


class UnavailablePortFactory:
    """Create one immutable null adapter for each request scope."""

    def __init__(self, readiness: ReadinessState) -> None:
        self._readiness = readiness

    @contextmanager
    def __call__(self) -> Iterator[WebApplicationPort]:
        yield UnavailableWebApplicationPort(self._readiness)


# TheSuperHackers @feature Leex 23/08/2026 Compose accepted report, map, player, settings, and job ports without exposing persistence. (#TBD)
class AnalyticsWebApplicationPort(UnavailableWebApplicationPort):
    """Combine honest placeholders with accepted production application ports."""

    def __init__(
        self,
        readiness: ReadinessState,
        jobs: object,
        reports: object,
        maps: object,
        *,
        players: object | None = None,
        settings: object | None = None,
    ) -> None:
        super().__init__(readiness)
        self._jobs = jobs
        self._reports = reports
        self._maps = maps
        self._players = players
        self._settings = settings

    def _player_port(self) -> object:
        if self._players is None:
            raise PublicProblem(status=503, code="player_adapter_pending", detail="Player analysis is unavailable")
        return self._players

    def _settings_port(self) -> object:
        if self._settings is None:
            raise PublicProblem(status=503, code="settings_adapter_pending", detail="Analyzer settings are unavailable")
        return self._settings

    def list_jobs(self, query: JobQueryDTO) -> JobPageDTO:
        return self._jobs.list_jobs(query)  # type: ignore[attr-defined,no-any-return]

    def get_job(self, job_public_id: str) -> JobDetailDTO:
        return self._jobs.get_job(job_public_id)  # type: ignore[attr-defined,no-any-return]

    def retry_job(self, command: RetryJobCommandDTO) -> JobMutationDTO:
        return self._jobs.retry_job(command)  # type: ignore[attr-defined,no-any-return]

    def cancel_job(self, command: CancelJobCommandDTO) -> JobMutationDTO:
        return self._jobs.cancel_job(command)  # type: ignore[attr-defined,no-any-return]

    def read_job_log(self, query: JobLogQueryDTO) -> JobLogChunkDTO:
        return self._jobs.read_job_log(query)  # type: ignore[attr-defined,no-any-return]

    def resolve_latest(self, query: LatestReportQueryDTO) -> ReportResolutionDTO:
        return self._reports.resolve_latest(query)  # type: ignore[attr-defined,no-any-return]

    def get_report(self, query: FixedReportQueryDTO) -> ReplayReportDTO:
        return self._reports.get_report(query)  # type: ignore[attr-defined,no-any-return]

    def timeline_chart(self, query: TimelineChartQueryDTO) -> TimelineChartDTO:
        return self._reports.timeline_chart(query)  # type: ignore[attr-defined,no-any-return]

    def get_evidence(self, query: EvidenceQueryDTO) -> EvidenceDetailDTO:
        return self._reports.get_evidence(query)  # type: ignore[attr-defined,no-any-return]

    def list_scenes(self, query: MapSceneIndexQueryDTO) -> MapSceneIndexPageDTO:
        return self._maps.list_scenes(query)  # type: ignore[attr-defined,no-any-return]

    def get_scene(self, query: MapSceneQueryDTO) -> MapSceneDTO:
        return self._maps.get_scene(query)  # type: ignore[attr-defined,no-any-return]

    def get_raster(self, query: MapRasterQueryDTO) -> MapRasterResourceDTO:
        return self._maps.get_raster(query)  # type: ignore[attr-defined,no-any-return]

    def list_players(self, query: PlayerIndexQueryDTO) -> PlayerIndexPageDTO:
        return self._player_port().list_players(query)  # type: ignore[attr-defined,no-any-return]

    def resolve_profile(self, selection: PlayerProfileSelectionDTO) -> PlayerProfileResolutionDTO:
        return self._player_port().resolve_profile(selection)  # type: ignore[attr-defined,no-any-return]

    def get_profile(self, query: PlayerProfileQueryDTO) -> PlayerProfileDTO:
        return self._player_port().get_profile(query)  # type: ignore[attr-defined,no-any-return]

    def audit(self, player_public_id: str, page: int, page_size: int) -> IdentityAuditPageDTO:
        return self._player_port().audit(player_public_id, page, page_size)  # type: ignore[attr-defined,no-any-return]

    def preview(self, draft: IdentityDraftDTO) -> IdentityPreviewDTO:
        return self._player_port().preview(draft)  # type: ignore[attr-defined,no-any-return]

    def execute(self, command: ExecuteIdentityChangeDTO) -> IdentityMutationReceiptDTO:
        return self._player_port().execute(command)  # type: ignore[attr-defined,no-any-return]

    def retry_invalidation(self, operation_public_id: str) -> tuple[InvalidationJobReferenceDTO, ...]:
        return self._player_port().retry_invalidation(operation_public_id)  # type: ignore[attr-defined,no-any-return]

    def resolve(self, selection: ComparisonSelectionDTO) -> ComparisonResolutionDTO:
        return self._player_port().resolve(selection)  # type: ignore[attr-defined,no-any-return]

    def compare(self, query: FixedComparisonQueryDTO) -> ComparisonDTO:
        return self._player_port().compare(query)  # type: ignore[attr-defined,no-any-return]

    def get_settings(self) -> SettingsSnapshotDTO:
        return self._settings_port().get_settings()  # type: ignore[attr-defined,no-any-return]

    def preview_settings(self, command: SettingsPreviewCommandDTO) -> SettingsImpactDTO:
        return self._settings_port().preview_settings(command)  # type: ignore[attr-defined,no-any-return]

    def apply_settings(self, command: ApplySettingsCommandDTO) -> SettingsMutationDTO:
        return self._settings_port().apply_settings(command)  # type: ignore[attr-defined,no-any-return]

    def run_diagnostic(self, command: DiagnosticCommandDTO) -> DiagnosticResultDTO:
        return self._settings_port().run_diagnostic(command)  # type: ignore[attr-defined,no-any-return]


# TheSuperHackers @fix Leex 22/08/2026 Own one atomic Analytics session through each complete Jobs response. (#TBD)
class _RequestSessionLease:
    """Hide lifecycle-local commit/close behind the owning Web request transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def commit(self) -> None:
        self._session.flush()

    def rollback(self) -> None:
        self._session.rollback()

    def close(self) -> None:
        return None


class _RequestSessionFactory:
    """Lazily lease one SQLAlchemy session to every operation in one request."""

    def __init__(self, base_factory: Callable[[], Session]) -> None:
        self._base_factory = base_factory
        self._session: Session | None = None
        self._lease: _RequestSessionLease | None = None

    def __call__(self) -> _RequestSessionLease:
        if self._session is None:
            self._session = self._base_factory()
            self._lease = _RequestSessionLease(self._session)
        assert self._lease is not None
        return self._lease

    def begin(self) -> _RequestSessionLease:
        """Expose the shared request lease to services expecting sessionmaker.begin()."""

        return self()

    def commit(self) -> None:
        if self._session is not None:
            self._session.commit()

    def rollback(self) -> None:
        if self._session is not None:
            self._session.rollback()

    def close(self) -> None:
        if self._session is not None:
            self._session.close()


# TheSuperHackers @feature Leex 23/08/2026 Share one request transaction while retaining one process-lifetime settings identity. (#TBD)
class AnalyticsPortFactory:
    """Open one short Analytics scope per request after web-owned bootstrap."""

    def __init__(
        self,
        settings: AnalyzerSettings | RuntimeConfiguration,
        readiness: ReadinessState,
        *,
        configuration_root: Path | None = None,
    ) -> None:
        from generals_replay_analyzer import __version__
        from generals_replay_analyzer.config import RuntimeConfiguration, load_runtime_configuration
        from generals_replay_analyzer.diagnostics import DiagnosticCoordinator
        from generals_replay_analyzer.web.adapters.settings import SettingsDiagnosticsAdapter

        if isinstance(settings, RuntimeConfiguration):
            runtime = settings
        else:
            explicit_safe_values: dict[str, object] = {
                field_name: getattr(settings, field_name)
                for field_name in (
                    "import_mode",
                    "minimum_longitudinal_sample_size",
                    "movement_sample_frames",
                    "ollama_model",
                    "ollama_url",
                )
                if field_name in settings.model_fields_set
            }
            runtime = load_runtime_configuration(
                configuration_root=configuration_root,
                values={
                    "data_root": settings.data_root,
                    "database_path": settings.database_path,
                    "managed_replay_directory": settings.managed_replay_directory,
                    "map_asset_directory": settings.map_asset_directory,
                    "cache_directory": settings.cache_directory,
                    "log_directory": settings.log_directory,
                    "engine_executable": settings.engine_executable,
                    "watched_folders": settings.watched_folders,
                    **explicit_safe_values,
                },
                version_identities=(("analyzer", __version__),),
            )
        self._settings = runtime.settings
        self._readiness = readiness
        settings_store = runtime.store
        self._settings_adapter = SettingsDiagnosticsAdapter(
            settings_store,
            DiagnosticCoordinator(settings_revision=lambda: settings_store.read().revision),
            startup_snapshot=runtime.snapshot,
            components=(
                ComponentIdentityDTO(
                    component="analyzer",
                    version=__version__,
                    availability=AvailabilityDTO(state="available"),
                ),
            ),
        )

    @property
    def runtime_settings(self) -> AnalyzerSettings:
        """Expose the frozen effective settings to composition verification only."""

        return self._settings

    @contextmanager
    def __call__(self) -> Iterator[WebApplicationPort]:
        from generals_replay_analyzer.analysis_pipeline import AnalysisPlanner
        from generals_replay_analyzer.comparison.service import ReplayComparisonService
        from generals_replay_analyzer.db import create_database_engine, create_session_factory
        from generals_replay_analyzer.identity.query import PlayerQueryService
        from generals_replay_analyzer.identity.service import PlayerIdentityService
        from generals_replay_analyzer.identity.workflow import PlayerIdentityWorkflowService
        from generals_replay_analyzer.importing import JobLifecycleService
        from generals_replay_analyzer.report.query import ReportQueryService
        from generals_replay_analyzer.spatial.query import MapSceneQueryService
        from generals_replay_analyzer.storage import ContentAddressedStore
        from generals_replay_analyzer.watching import FileWatchStatusStore
        from generals_replay_analyzer.web.adapters.analytics import AnalyticsJobsAdapter
        from generals_replay_analyzer.web.adapters.map import AnalyticsMapSceneAdapter
        from generals_replay_analyzer.web.adapters.players import AnalyticsPlayersAdapter
        from generals_replay_analyzer.web.adapters.report import AnalyticsReportAdapter

        engine = create_database_engine(self._settings.database_path)
        request_sessions = _RequestSessionFactory(create_session_factory(engine))
        try:
            lifecycle = JobLifecycleService(
                cast("sessionmaker[Session]", request_sessions),
                registered_stages=(),
                clock=lambda: datetime.now(UTC),
                log_store=ContentAddressedStore(self._settings.cache_directory / "artifacts"),
                log_data_root=self._settings.data_root,
                redaction_values=(str(self._settings.data_root), str(self._settings.database_path)),
            )
            report_service = ReportQueryService(
                cast("sessionmaker[Session]", request_sessions),
                settings=self._settings,
            )
            reports = AnalyticsReportAdapter(report_service)
            maps = AnalyticsMapSceneAdapter(
                MapSceneQueryService(
                    cast("sessionmaker[Session]", request_sessions),
                    report_service,
                )
            )
            session_factory = cast("sessionmaker[Session]", request_sessions)
            players = AnalyticsPlayersAdapter(
                PlayerQueryService(session_factory),
                PlayerIdentityWorkflowService(
                    session_factory,
                    PlayerIdentityService(session_factory),
                    AnalysisPlanner(session_factory, clock=lambda: datetime.now(UTC)),
                ),
                ReplayComparisonService(session_factory),
                minimum_sample_size=self._settings.minimum_longitudinal_sample_size,
            )
            yield AnalyticsWebApplicationPort(
                self._readiness,
                AnalyticsJobsAdapter(
                    lifecycle,
                    watch_status_reader=FileWatchStatusStore(self._settings.data_root).read,
                ),
                reports,
                maps,
                players=players,
                settings=self._settings_adapter,
            )
            request_sessions.commit()
        except BaseException:
            request_sessions.rollback()
            raise
        finally:
            request_sessions.close()
            engine.dispose()


# TheSuperHackers @feature Leex 22/08/2026 Close every application scope at the request boundary. (#TBD)
def application_port(request: Request) -> Iterator[WebApplicationPort]:
    factory: WebApplicationPortFactory = request.app.state.port_factory
    with factory() as port:
        yield port


# TheSuperHackers @feature Leex 22/08/2026 Validate the full configured-root form shape before any CSRF or port dependency. (#0)
async def validated_root_import_form(request: Request) -> RootImportForm:
    """Reject unknown or repeated form values before CSRF consumption and command scope construction."""
    form = await request.form()
    fields = tuple(form.multi_items())
    allowed = frozenset({"root_public_id", "relative_path", "_csrf"})
    if any(key not in allowed for key, _value in fields):
        raise PublicProblem(status=422, code="invalid_root_selection", detail="Replay selection is invalid")

    def exactly_one(name: str, *, required: bool) -> str | None:
        values = tuple(value for key, value in fields if key == name)
        if (required and len(values) != 1) or (not required and len(values) > 1):
            raise PublicProblem(status=422, code="invalid_root_selection", detail="Replay selection is invalid")
        if not values:
            return None
        value = values[0]
        if not isinstance(value, str):
            raise PublicProblem(status=422, code="invalid_root_selection", detail="Replay selection is invalid")
        return value

    root_public_id = exactly_one("root_public_id", required=True)
    relative_path = exactly_one("relative_path", required=True)
    csrf_token = exactly_one("_csrf", required=False)
    assert isinstance(root_public_id, str)
    assert isinstance(relative_path, str)
    return RootImportForm(root_public_id=root_public_id, relative_path=relative_path, csrf_token=csrf_token)


# TheSuperHackers @feature Leex 22/08/2026 Consume one native-form CSRF token before creating an import command port scope. (#0)
def native_form_csrf_guard(
    request: Request,
    root_import_form: Annotated[RootImportForm, Depends(validated_root_import_form)],
) -> None:
    """Reject a native form before any downstream command dependency can open a port."""
    if request.headers.get("x-csrf-token") is not None:
        return
    registry = request.app.state.form_csrf_token_registry
    if registry.consume(request.cookies.get("_csrf"), root_import_form.csrf_token):
        return
    raise PublicProblem(status=403, code="csrf_rejected", detail="The request CSRF token was rejected")


# TheSuperHackers @feature Leex 22/08/2026 Make native CSRF validation an explicit prerequisite of the command-port dependency DAG. (#0)
def csrf_guarded_application_port(
    request: Request,
    _csrf_guard: Annotated[None, Depends(native_form_csrf_guard)],
) -> Iterator[WebApplicationPort]:
    factory: WebApplicationPortFactory = request.app.state.port_factory
    with factory() as port:
        yield port
