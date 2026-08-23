"""Compose one populated external product root through accepted application services."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, TypeVar, cast
from uuid import UUID

from map_asset_support import write_test_map_asset  # type: ignore[import-not-found]
from telemetry.test_economy_production_contract import (  # type: ignore[import-not-found]
    ENGINE_IDENTITY,
    _object_created,
    _production,
    _record,
    _upgrade,
)
from telemetry.test_economy_production_contract import (
    _base_records as economy_base_records,
)
from telemetry.test_economy_production_contract import (
    _finish as finish_economy_trace,
)

from generals_replay_analyzer.analysis_pipeline.composition import create_production_import_service
from generals_replay_analyzer.analysis_pipeline.planner import AnalysisPlanner
from generals_replay_analyzer.config import load_runtime_configuration
from generals_replay_analyzer.configuration import ConfigurationStore, SettingChange
from generals_replay_analyzer.db import create_database_engine, create_session_factory
from generals_replay_analyzer.identity.service import PlayerIdentityService
from generals_replay_analyzer.importing import (
    AcquisitionDiagnostic,
    JobDTO,
    TelemetryArtifact,
)
from generals_replay_analyzer.importing.parser_import import ParserObservationImporter
from generals_replay_analyzer.llm.provider import CancellationSignal, JSONValue, OllamaClientConfig, TransportResponse
from generals_replay_analyzer.parser import parse_replay
from generals_replay_analyzer.storage import ContentAddressedStore
from generals_replay_analyzer.telemetry import load_validated_telemetry_bundle
from generals_replay_analyzer.web.adapters.library import AnalyticsLibraryAdapter
from generals_replay_analyzer.web.bootstrap import BootstrapReadinessState, create_production_bootstrapper
from generals_replay_analyzer.web.dependencies import AnalyticsPortFactory, AnalyticsWebApplicationPort
from generals_replay_analyzer.web.ports import (
    ComparisonFiltersDTO,
    ComparisonSelectionDTO,
    JobQueryDTO,
    LatestReportQueryDTO,
    MapSceneIndexQueryDTO,
    MapSceneQueryDTO,
    PlayerIndexQueryDTO,
    PlayerProfileSelectionDTO,
    RootImportCommandDTO,
)
from generals_replay_analyzer.web.routes.comparisons import _fixed_url as comparison_fixed_url
from generals_replay_analyzer.web.routes.players import _fixed_profile_url

_SCHEMA_VERSION = "populated-browser-fixture-v1"
_MANIFEST_NAME = "populated-browser-fixture.json"
_PINNED_REPLAY = Path("tests/fixtures/zero_hour_1_04/leex279_vs_fox27.rep")
_NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
_ResultT = TypeVar("_ResultT")
_MAX_QUERY_SECONDS = 30.0
_MAX_TOTAL_QUERY_SECONDS = 120.0
_EXPECTED_QUERY_LABELS = (
    "resolve_latest:replay",
    "get_report:replay",
    "list_scenes",
    "get_scene",
    "list_players",
    "resolve_profile:leex279",
    "get_profile:leex279",
    "resolve_latest:leex279",
    "resolve_profile:FOX27",
    "get_profile:FOX27",
    "resolve_latest:FOX27",
    "resolve_comparison",
    "compare",
    "list_jobs",
    "get_settings",
)


def _canonical_uuid(value: str, label: str) -> str:
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f"{label} must be a canonical UUID") from None
    if str(parsed) != value:
        raise ValueError(f"{label} must be a canonical UUID")
    return value


@dataclass(frozen=True, slots=True)
class ReportBinding:
    report_public_id: str
    report_version: str
    canonical_player_public_id: str | None
    replay_player_public_id: str | None
    fixed_url: str


@dataclass(frozen=True, slots=True)
class EvidenceBinding:
    evidence_public_id: str
    tier: str
    fixed_url: str


@dataclass(frozen=True, slots=True)
class MapBinding:
    map_public_id: str
    replay_public_id: str
    report_public_id: str
    frame_start: int
    frame_end: int
    scene_version: str
    fixed_url: str
    api_url: str


@dataclass(frozen=True, slots=True)
class PlayerBinding:
    canonical_public_id: str
    display_name: str
    identity_revision: int
    replay_player_public_id: str
    alias_public_ids: tuple[str, ...]
    profile_query_json: str
    profile_url: str
    profile_api_url: str
    identity_url: str


@dataclass(frozen=True, slots=True)
class ComparisonBinding:
    comparison_public_id: str
    state: str
    metric_definition_id: str
    query_digest: str
    input_digest: str
    fixed_query_json: str
    fixed_url: str
    api_url: str


@dataclass(frozen=True, slots=True)
class JobBinding:
    job_public_id: str
    stage: str
    status: str
    revision: int
    fixed_url: str


@dataclass(frozen=True, slots=True)
class PopulatedFixtureManifest:
    """Canonical envelope payload frozen around one generated database identity."""

    schema_version: str
    fixed_now_utc: str
    input_replay_sha256: str
    telemetry_trace_sha256: str
    map_member_sha256s: tuple[str, ...]
    schema_head: str
    replay_public_id: str
    import_root_public_id: str
    import_relative_path: str
    replay_report: ReportBinding
    player_reports: tuple[ReportBinding, ...]
    evidence: EvidenceBinding
    map: MapBinding
    players: tuple[PlayerBinding, ...]
    comparison: ComparisonBinding
    pending_job: JobBinding
    settings_revision: int
    ollama_state: str

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION or self.ollama_state != "not_requested":
            raise ValueError("unsupported populated fixture manifest contract")
        _canonical_uuid(self.replay_public_id, "replay_public_id")
        _canonical_uuid(self.import_root_public_id, "import_root_public_id")
        if Path(self.import_relative_path).name != self.import_relative_path or not self.import_relative_path.endswith(
            ".rep"
        ):
            raise ValueError("fixture import path must be one safe replay filename")
        if self.map_member_sha256s != tuple(sorted(set(self.map_member_sha256s))):
            raise ValueError("map member digests must be unique and sorted")
        if tuple(player.display_name for player in self.players) != ("leex279", "FOX27"):
            raise ValueError("fixture players must retain pinned replay order")
        if len(self.player_reports) != 2 or self.settings_revision < 0:
            raise ValueError("fixture manifest is incomplete")

    def canonical_payload_bytes(self) -> bytes:
        """Return the exact canonical payload used for the outer digest."""
        return json.dumps(
            asdict(self),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

    @property
    def fixture_digest(self) -> str:
        return hashlib.sha256(self.canonical_payload_bytes()).hexdigest()

    def write(self, runtime_root: Path) -> Path:
        """Publish a digest-bound canonical manifest below the external runtime root."""
        root = runtime_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        envelope = {
            "schema_version": self.schema_version,
            "payload_sha256": self.fixture_digest,
            "payload": payload,
        }
        destination = root / _MANIFEST_NAME
        destination.write_bytes(
            json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
            + b"\n"
        )
        return destination


@dataclass(frozen=True, slots=True)
class PopulatedFixtureResult:
    manifest_path: Path
    runtime_root: Path
    environment: tuple[tuple[str, str], ...]
    manifest: PopulatedFixtureManifest
    query_timings: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        labels = tuple(label for label, _duration in self.query_timings)
        if labels != _EXPECTED_QUERY_LABELS:
            raise ValueError("query timing budget requires the complete ordered query contract")
        if any(
            not label
            or not math.isfinite(duration)
            or duration < 0
            or duration >= _MAX_QUERY_SECONDS
            for label, duration in self.query_timings
        ) or sum(duration for _label, duration in self.query_timings) >= _MAX_TOTAL_QUERY_SECONDS:
            timing_summary = ", ".join(f"{label}={duration:.3f}s" for label, duration in self.query_timings)
            raise ValueError(f"query timing budget was exceeded ({timing_summary})")

    @property
    def fixture_digest(self) -> str:
        return self.manifest.fixture_digest

    @property
    def replay_public_id(self) -> str:
        return self.manifest.replay_public_id


class _ForbiddenTransport:
    """Fail closed if an offline populated fixture accidentally requests Ollama."""

    client_config: OllamaClientConfig

    def __init__(self, config: OllamaClientConfig) -> None:
        del config
        raise AssertionError("populated browser fixture must not instantiate Ollama")

    async def request(
        self,
        method: str,
        path: str,
        payload: JSONValue | None,
        *,
        client_config: OllamaClientConfig,
        cancellation: CancellationSignal | None,
    ) -> TransportResponse:
        del method, path, payload, client_config, cancellation
        raise AssertionError("populated browser fixture must stay offline")

    async def aclose(self) -> None:
        return None


def _write_fixture_trace(trace_root: Path, pinned_replay: Path) -> Path:
    trace_root.mkdir(parents=True, exist_ok=True)
    parsed = parse_replay(pinned_replay)
    human_slots = tuple(slot for slot in parsed.header.slots if slot.kind == "human")
    if tuple((slot.index, slot.name) for slot in human_slots) != ((0, "leex279"), (1, "FOX27")):
        raise AssertionError("pinned replay human slots changed")
    records = economy_base_records(trace_root)
    manifest = cast(dict[str, Any], records[0]["payload"])
    map_reference = write_test_map_asset(
        trace_root,
        ENGINE_IDENTITY,
        parsed.header.map,
        start_positions=[
            {
                "bounds_policy": "pathfinder_xy_closed",
                "category_source": "GameSlot::getStartPos + TerrainLogic::getWaypointByName",
                "name": f"Player_{slot + 1}_Start",
                "position": {"x": float(slot), "y": 0.0, "z": 0.0},
                "slot_indices": [slot],
                "waypoint_id": slot + 1,
            }
            for slot in range(2)
        ],
    )
    manifest.update(
        {
            "map_identity": parsed.header.map,
            "initial_seed": parsed.header.seed,
            "replay_version": parsed.header.version_string,
            "map_asset": map_reference,
        }
    )
    player_record = next(record for record in records if record["event_type"] == "players_initialized")
    player_payload = cast(dict[str, Any], player_record["payload"])
    slots = player_payload["slots"]
    assert isinstance(slots, list)
    cast(dict[str, Any], slots[0])["replay_name"] = "leex279"
    slots[1] = {
        "slot_index": 1,
        "slot_state": "human",
        "occupied": True,
        "resolution_status": "resolved",
        "replay_name": "FOX27",
        "player_index": 1,
        "team_id": 1,
        "faction_template_name": "FactionAmerica",
        "color": 1,
        "start_position_status": "resolved",
        "start_position": {"x": 1.0, "y": 0.0, "z": 0.0},
        "controller": "human",
        "is_human": True,
        "is_header_local_slot": False,
        "is_resolved_local_player": False,
    }
    player_payload["engine_player_indices"] = [0, 1]
    owner_one = _object_created(50, "AmericaTankCrusader")
    owner_one["owner_player_index"] = 1
    owner_one["team_id"] = 1
    owner_one["position"] = {"x": 1.0, "y": 2.0, "z": 0.0}
    records.extend(
        [
            _record(6, "object_created", owner_one),
            _record(7, "production_queued", _production("queued"), frame=1),
            _record(
                8,
                "cash_changed",
                {
                    "player_index": 0,
                    "before": 5000,
                    "delta": -900,
                    "after": 4100,
                    "track_income": False,
                    "reason": "unit_cost",
                },
                frame=1,
            ),
            _record(9, "production_completed", _production("completed"), frame=20),
            _record(10, "upgrade_queued", _upgrade("queued"), frame=2),
            _record(11, "upgrade_cancelled", _upgrade("cancelled"), frame=12),
            _record(
                12,
                "cash_changed",
                {
                    "player_index": 1,
                    "before": 5000,
                    "delta": -500,
                    "after": 4500,
                    "track_income": False,
                    "reason": "unit_cost",
                },
                frame=2,
            ),
            _record(
                13,
                "supply_collected",
                {
                    "collector_object_id": 20,
                    "source_object_id": 30,
                    "source_status": "resolved",
                    "dropoff_object_id": 40,
                    "player_index": 0,
                    "amount": 600,
                    "location": {"x": 40.0, "y": 2.0, "z": 0.0},
                },
                frame=15,
            ),
            _record(
                14,
                "cash_changed",
                {
                    "player_index": 0,
                    "before": 4100,
                    "delta": 600,
                    "after": 4700,
                    "track_income": True,
                    "reason": "supply_income",
                },
                frame=15,
            ),
        ]
    )
    for sequence, record in enumerate(records):
        record["sequence"] = sequence
    trace = cast(
        Path,
        finish_economy_trace(
            trace_root / "browser-fixture.ndjson",
            records,
            [
                {"player_index": 0, "has_money": True, "balance": 4700},
                {"player_index": 1, "has_money": True, "balance": 4500},
            ],
        ),
    )
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    outcome = next(record for record in records if record["event_type"] == "match_outcome")
    complete = next(record for record in records if record["event_type"] == "complete")
    outcome["frame"] = 108
    outcome["logic_time_seconds"] = 108 / 30.0
    outcome["payload"].update(
        terminal_reason="crc_mismatch",
        crc_mismatch=True,
        crc_mismatch_frame=105,
        clean_shutdown=False,
    )
    complete["frame"] = 108
    complete["logic_time_seconds"] = 108 / 30.0
    complete["payload"].update(
        final_frame=108,
        command_count=16,
        terminal_reason="crc_mismatch",
        crc_mismatch=True,
        crc_mismatch_frame=105,
        replay_truncated=False,
        clean_shutdown=False,
    )
    prior = b"".join(
        json.dumps(record, separators=(",", ":")).encode() + b"\n"
        for record in records[:-1]
    )
    complete["payload"]["trace_sha256"] = hashlib.sha256(prior).hexdigest()
    trace.write_bytes(
        b"".join(json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records)
    )
    return trace


class _FixtureTelemetryAcquirer:
    def __init__(
        self,
        trace: Path,
        *,
        replay_quality: str = "partial",
        strategy_analysis_scope: str = "observed_boundary_only",
    ) -> None:
        self._trace = trace
        self._replay_quality = replay_quality
        self._strategy_analysis_scope = strategy_analysis_scope

    def acquire(self, replay: Path, replay_sha256: str) -> TelemetryArtifact:
        del replay, replay_sha256
        bundle = load_validated_telemetry_bundle(self._trace)
        catalog = bundle.manifest.payload.game_data_catalog
        if catalog is None:
            raise AssertionError("fixture telemetry is missing its catalog")
        catalog_path = self._trace.parent / catalog.path
        return TelemetryArtifact(
            str(bundle.manifest.run_id),
            "success",
            self._replay_quality,
            self._strategy_analysis_scope,
            self._trace,
            catalog_path,
            bundle.map_member_paths,
            None,
            None,
            None,
            0,
            bundle.manifest.payload.engine_build,
            "d" * 64,
            (AcquisitionDiagnostic("fixture", "deterministic offline browser evidence"),),
        )


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _model_json(value: Any) -> str:
    payload = value.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def build_populated_fixture(
    runtime_root: Path,
    environment: Mapping[str, str],
    *,
    project_root: Path,
    telemetry_trace: Path | None = None,
    telemetry_replay_quality: str = "partial",
    telemetry_strategy_analysis_scope: str = "observed_boundary_only",
) -> PopulatedFixtureResult:
    """Build one populated external database without an engine, Ollama, or direct ORM seeding."""
    root = runtime_root.resolve()
    configured_root = environment.get("GENERALS_REPLAY_ANALYZER_DATA_ROOT")
    if configured_root is None:
        raise ValueError("environment is missing GENERALS_REPLAY_ANALYZER_DATA_ROOT")
    data_root = Path(configured_root).resolve()
    if not _inside(data_root, root):
        raise ValueError("configured data root must be inside runtime_root")
    pinned_replay = (project_root.resolve() / _PINNED_REPLAY).resolve()
    if not pinned_replay.is_file():
        raise FileNotFoundError("pinned replay fixture is unavailable")
    source_root = root / "fixture-input"
    source_root.mkdir(parents=True, exist_ok=True)
    replay_input = source_root / pinned_replay.name
    shutil.copyfile(pinned_replay, replay_input)
    replay_input.chmod(0o444)
    runtime_environment = dict(environment)
    runtime_environment["GENERALS_REPLAY_ANALYZER_WATCHED_FOLDERS"] = json.dumps([str(source_root)])
    local_app_data = environment.get("LOCALAPPDATA")
    if local_app_data is None:
        raise ValueError("environment is missing LOCALAPPDATA")
    configuration_root = Path(local_app_data).resolve() / "GeneralsReplayAnalyzer"
    if not _inside(configuration_root, root):
        raise ValueError("platform configuration root must be inside runtime_root")
    configuration_store = ConfigurationStore(
        configuration_root=configuration_root,
        environment=runtime_environment,
    )
    configuration_snapshot = configuration_store.read()
    if configuration_snapshot.value("minimum_longitudinal_sample_size") != 1:
        configuration_store.apply(
            expected_revision=configuration_snapshot.revision,
            changes=(SettingChange("minimum_longitudinal_sample_size", 1),),
        )

    runtime = load_runtime_configuration(
        configuration_root=configuration_root,
        environment=runtime_environment,
        values={"watched_folders": (source_root,)},
    )
    if runtime.settings.data_root != data_root:
        raise AssertionError("runtime configuration did not preserve the fixture data root")
    readiness = BootstrapReadinessState()
    bootstrap = create_production_bootstrapper(readiness).prepare(runtime.settings)
    engine = create_database_engine(runtime.settings.database_path)
    query_timings: list[tuple[str, float]] = []

    def timed(label: str, operation: Callable[[], _ResultT]) -> _ResultT:
        started = time.perf_counter()
        try:
            return operation()
        finally:
            query_timings.append((label, time.perf_counter() - started))

    try:
        factory = create_session_factory(engine)
        trace = (
            _write_fixture_trace(root / "telemetry-source", pinned_replay)
            if telemetry_trace is None
            else telemetry_trace.resolve(strict=True)
        )
        bundle = load_validated_telemetry_bundle(trace)
        service = create_production_import_service(
            factory,
            runtime.settings,
            ContentAddressedStore(runtime.settings.managed_replay_directory),
            ContentAddressedStore(runtime.settings.cache_directory / "artifacts"),
            parser=parse_replay,
            telemetry_acquirer=_FixtureTelemetryAcquirer(
                trace,
                replay_quality=telemetry_replay_quality,
                strategy_analysis_scope=telemetry_strategy_analysis_scope,
            ),
            clock=lambda: _NOW,
            parser_version="browser-fixture-parser-v1",
            telemetry_acquirer_version="browser-fixture-telemetry-v1",
            transport_factory=_ForbiddenTransport,
        )
        library = AnalyticsLibraryAdapter(
            factory,
            settings=runtime.settings,
            import_service=service,
            request_telemetry=True,
            clock=lambda: _NOW,
        )
        import_roots = library.import_roots()
        if len(import_roots) != 1 or import_roots[0].availability.state != "available":
            raise AssertionError("fixture import root was not exposed through the public adapter")
        import_root_public_id = import_roots[0].root_public_id
        accepted = library.submit_root_selection(
            RootImportCommandDTO(
                root_public_id=import_root_public_id,
                relative_path=pinned_replay.name,
            )
        )
        if accepted.availability.state != "available":
            raise AssertionError("fixture watched-root selection was not accepted")
        imported_jobs: list[JobDTO] = []
        for _ in range(8):
            completed = service.run_available("browser-fixture-import", limit=1)
            if not completed:
                break
            imported_jobs.extend(completed)
            if completed[0].stage == "import_observations":
                break
        imported = tuple(imported_jobs)
        observation = next((job for job in imported if job.stage == "import_observations"), None)
        if observation is None or observation.status != "succeeded":
            details = tuple((job.stage, job.status, job.error_code) for job in imported)
            raise AssertionError(f"fixture observations did not import: {details!r}")
        replay_ids = {job.replay_public_id for job in imported if job.replay_public_id is not None}
        if len(replay_ids) != 1:
            raise AssertionError("fixture import did not produce exactly one replay")
        replay_public_id = replay_ids.pop()
        import_result = service.result_for_replay(replay_public_id)
        def reject_reparse(_path: Path) -> Any:
            raise AssertionError("fixture parser cache lookup attempted to reparse the watched replay")

        parser_result = ParserObservationImporter(
            factory,
            runtime.settings.data_root,
            parser=reject_reparse,
            parser_version="browser-fixture-parser-v1",
            schema_version=1,
            clock=lambda: _NOW,
        ).import_replay(import_result.sha256)
        identity_batch = PlayerIdentityService(factory, now_factory=lambda: _NOW).resolve_parser_run(
            replay_public_id=replay_public_id,
            parser_run_id=parser_result.run_id,
        )
        linked_names = {
            decision.normalized_name
            for decision in identity_batch.decisions
            if decision.player_public_id is not None
        }
        if linked_names != {"leex279", "fox27"}:
            raise AssertionError(f"fixture identities did not resolve: {linked_names!r}")
        plan = AnalysisPlanner(factory, clock=lambda: _NOW).ensure_analysis_plan(replay_public_id, False)
        if plan.status != "planned":
            raise AssertionError(f"fixture analysis plan was not accepted: {plan.status}")
        settled = service.run_available("browser-fixture-analysis", limit=64)
        report_job = next((job for job in settled if job.public_id == plan.report_job_public_id), None)
        if report_job is None or report_job.status != "succeeded":
            details = tuple((job.stage, job.status, job.error_code) for job in settled)
            with AnalyticsPortFactory(runtime, readiness)() as diagnostic_port:
                public_jobs = diagnostic_port.list_jobs(JobQueryDTO(limit=200))
                diagnostics = tuple(
                    (
                        job.stage,
                        job.state,
                        None if job.error is None else job.error.model_dump(mode="json"),
                    )
                    for job in public_jobs.items
                )
            raise AssertionError(f"fixture report did not settle: {details!r}; jobs={diagnostics!r}")

        pending_service = create_production_import_service(
            factory,
            runtime.settings,
            ContentAddressedStore(runtime.settings.managed_replay_directory),
            ContentAddressedStore(runtime.settings.cache_directory / "artifacts"),
            parser=parse_replay,
            telemetry_acquirer=None,
            clock=lambda: _NOW,
            parser_version="browser-fixture-parser-v1",
            telemetry_acquirer_version="none",
            transport_factory=_ForbiddenTransport,
        )
        pending_library = AnalyticsLibraryAdapter(
            factory,
            settings=runtime.settings,
            import_service=pending_service,
            request_telemetry=False,
            clock=lambda: _NOW,
        )
        pending_roots = pending_library.import_roots()
        if (
            len(pending_roots) != 1
            or pending_roots[0].root_public_id != import_root_public_id
            or pending_roots[0].availability.state != "available"
        ):
            raise AssertionError("fixture pending import root identity changed")
        pending_submission = pending_library.submit_root_selection(
            RootImportCommandDTO(
                root_public_id=import_root_public_id,
                relative_path=pinned_replay.name,
            )
        )
        pending_public_id = pending_submission.submission_public_id
        with AnalyticsPortFactory(runtime, readiness)() as application_port:
            port = cast(AnalyticsWebApplicationPort, application_port)
            resolution = timed(
                "resolve_latest:replay",
                lambda: port.resolve_latest(LatestReportQueryDTO(replay_public_id=replay_public_id)),
            )
            if resolution.state != "available" or resolution.fixed_report is None or resolution.version is None:
                raise AssertionError(f"fixture report query did not resolve: {resolution.state}")
            fixed_report = resolution.fixed_report
            report = timed("get_report:replay", lambda: port.get_report(fixed_report))
            evidence_refs = tuple(
                reference
                for section in report.sections
                for claim in section.claims
                for reference in claim.evidence
            )
            if not evidence_refs:
                raise AssertionError("fixture report exposed no standalone evidence")
            evidence_ref = min(evidence_refs, key=lambda item: (item.tier, item.public_id))

            map_page = timed(
                "list_scenes",
                lambda: port.list_scenes(MapSceneIndexQueryDTO(page=1, page_size=100)),
            )
            map_matches = tuple(
                item
                for item in map_page.items
                if (item.replay_public_id, item.report_public_id)
                == (replay_public_id, fixed_report.report_public_id)
            )
            if len(map_matches) != 1:
                raise AssertionError("fixture report did not resolve exactly one map")
            map_item = map_matches[0]
            scene_query = MapSceneQueryDTO(
                replay_public_id=replay_public_id,
                report_public_id=fixed_report.report_public_id,
                frame_start=map_item.frame_window.frame_start,
                frame_end=map_item.frame_window.frame_end,
            )
            scene = timed("get_scene", lambda: port.get_scene(scene_query))

            player_page = timed(
                "list_players",
                lambda: port.list_players(PlayerIndexQueryDTO(page=1, page_size=100, active_only=True)),
            )
            summaries = {item.display_name: item for item in player_page.items}
            if set(summaries) != {"leex279", "FOX27"}:
                raise AssertionError(f"fixture player queries are incomplete: {tuple(summaries)!r}")
            player_bindings: list[PlayerBinding] = []
            player_reports: list[ReportBinding] = []
            for display_name in ("leex279", "FOX27"):
                summary = summaries[display_name]
                profile_resolution = timed(
                    f"resolve_profile:{display_name}",
                    partial(
                        port.resolve_profile,
                        PlayerProfileSelectionDTO(
                            player_public_id=summary.player_public_id,
                            page=1,
                            page_size=25,
                        ),
                    ),
                )
                if profile_resolution.state != "resolved" or profile_resolution.fixed_query is None:
                    raise AssertionError(f"fixture profile did not resolve: {display_name}")
                fixed_profile_query = profile_resolution.fixed_query
                profile = timed(
                    f"get_profile:{display_name}",
                    partial(port.get_profile, fixed_profile_query),
                )
                history = tuple(
                    item for item in profile.replay_history if item.replay_public_id == replay_public_id
                )
                if len(history) != 1:
                    raise AssertionError(f"fixture profile has no exact replay member: {display_name}")
                replay_player_public_id = history[0].replay_player_public_id
                player_report_resolution = timed(
                    f"resolve_latest:{display_name}",
                    partial(
                        port.resolve_latest,
                        LatestReportQueryDTO(
                            replay_public_id=replay_public_id,
                            replay_player_public_id=replay_player_public_id,
                        ),
                    ),
                )
                if (
                    player_report_resolution.state != "available"
                    or player_report_resolution.fixed_report is None
                    or player_report_resolution.version is None
                ):
                    raise AssertionError(f"fixture player report did not resolve: {display_name}")
                player_reports.append(
                    ReportBinding(
                        player_report_resolution.fixed_report.report_public_id,
                        player_report_resolution.version.report_version,
                        summary.player_public_id,
                        replay_player_public_id,
                        (
                            f"/replays/{replay_public_id}/reports/"
                            f"{player_report_resolution.fixed_report.report_public_id}"
                        ),
                    )
                )
                profile_url = _fixed_profile_url(profile_resolution.fixed_query)
                player_bindings.append(
                    PlayerBinding(
                        summary.player_public_id,
                        display_name,
                        summary.identity_revision,
                        replay_player_public_id,
                        tuple(alias.alias_public_id for alias in profile.embedded_aliases),
                        _model_json(profile_resolution.fixed_query),
                        profile_url,
                        profile_url.replace(
                            f"/players/{summary.player_public_id}",
                            f"/api/players/{summary.player_public_id}/profile",
                            1,
                        ),
                        f"/players/{summary.player_public_id}/identity",
                    )
                )

            comparison_resolution = timed(
                "resolve_comparison",
                lambda: port.resolve(
                    ComparisonSelectionDTO(
                        kind="players",
                        left_public_id=player_bindings[0].canonical_public_id,
                        right_public_id=player_bindings[1].canonical_public_id,
                        baseline_requested=False,
                        metric_definition_ids=("economy.cash_change_total",),
                        filters=ComparisonFiltersDTO(),
                    )
                ),
            )
            if comparison_resolution.state != "resolved" or comparison_resolution.fixed_query is None:
                raise AssertionError(f"fixture comparison did not resolve: {comparison_resolution.reason_codes!r}")
            fixed_comparison_query = comparison_resolution.fixed_query
            comparison = timed(
                "compare",
                lambda: port.compare(fixed_comparison_query),
            )
            jobs = timed("list_jobs", lambda: port.list_jobs(JobQueryDTO(limit=200)))
            pending_jobs = tuple(item for item in jobs.items if item.job_public_id == pending_public_id)
            if len(pending_jobs) != 1 or pending_jobs[0].state != "pending":
                raise AssertionError("fixture pending job was not exposed through the public port")
            pending_job = pending_jobs[0]
            settings = timed("get_settings", port.get_settings)

        manifest = PopulatedFixtureManifest(
            schema_version=_SCHEMA_VERSION,
            fixed_now_utc=_NOW.isoformat().replace("+00:00", "Z"),
            input_replay_sha256=import_result.sha256,
            telemetry_trace_sha256=bundle.complete.payload.trace_sha256,
            map_member_sha256s=tuple(
                sorted(hashlib.sha256(path.read_bytes()).hexdigest() for path in bundle.map_member_paths)
            ),
            schema_head=bootstrap.schema_revision,
            replay_public_id=replay_public_id,
            import_root_public_id=import_root_public_id,
            import_relative_path=pinned_replay.name,
            replay_report=ReportBinding(
                fixed_report.report_public_id,
                resolution.version.report_version,
                None,
                None,
                f"/replays/{replay_public_id}/reports/{fixed_report.report_public_id}",
            ),
            player_reports=tuple(player_reports),
            evidence=EvidenceBinding(
                evidence_ref.public_id,
                evidence_ref.tier,
                (
                    f"/evidence/{evidence_ref.tier}/{evidence_ref.public_id}"
                    f"?report_id={fixed_report.report_public_id}"
                ),
            ),
            map=MapBinding(
                scene.map_public_id,
                replay_public_id,
                fixed_report.report_public_id,
                map_item.frame_window.frame_start,
                map_item.frame_window.frame_end,
                scene.schema_version,
                (
                    f"/replays/{replay_public_id}/reports/{fixed_report.report_public_id}/map"
                    f"?frame_start={map_item.frame_window.frame_start}&frame_end={map_item.frame_window.frame_end}"
                ),
                (
                    f"/api/replays/{replay_public_id}/reports/{fixed_report.report_public_id}/map/scene"
                    f"?frame_start={map_item.frame_window.frame_start}&frame_end={map_item.frame_window.frame_end}"
                ),
            ),
            players=tuple(player_bindings),
            comparison=ComparisonBinding(
                comparison.version.comparison_public_id,
                comparison.state,
                "economy.cash_change_total",
                comparison.version.query_digest,
                comparison.version.input_digest,
                _model_json(comparison_resolution.fixed_query),
                comparison_fixed_url(comparison_resolution.fixed_query),
                comparison_fixed_url(comparison_resolution.fixed_query, api=True),
            ),
            pending_job=JobBinding(
                pending_job.job_public_id,
                pending_job.stage,
                pending_job.state,
                pending_job.revision,
                f"/jobs/{pending_job.job_public_id}",
            ),
            settings_revision=settings.revision,
            ollama_state="not_requested",
        )
        manifest_path = manifest.write(root)
        return PopulatedFixtureResult(
            manifest_path,
            root,
            tuple(sorted(runtime_environment.items())),
            manifest,
            tuple(query_timings),
        )
    finally:
        engine.dispose()
