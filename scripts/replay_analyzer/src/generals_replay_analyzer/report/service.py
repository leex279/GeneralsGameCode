"""Persisted-evidence report materialization and managed publication."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeVar, cast
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased, sessionmaker

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import (
    AnalysisRun,
    AssessmentEvidence,
    CombatEvent,
    EconomyEvent,
    Entity,
    EntitySample,
    EvidenceItem,
    Feature,
    FeatureEvidence,
    FeatureSet,
    LongitudinalMember,
    LongitudinalResult,
    LongitudinalRun,
    ManagedAsset,
    ParserRun,
    Player,
    ProductionEvent,
    Replay,
    ReplayCommand,
    ReplayPlayer,
    ReplayQualityIssue,
    Report,
    StrategyAssessment,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.llm.evidence_bundle import EvidenceBundle
from generals_replay_analyzer.llm.schema import (
    PROMPT_SHA256,
    PROMPT_VERSION,
    RESPONSE_SCHEMA_SHA256,
    RESPONSE_SCHEMA_VERSION,
    ResponseValidationError,
    validate_response,
)
from generals_replay_analyzer.longitudinal.segments import LongitudinalMemberDTO
from generals_replay_analyzer.report.assembly import DISPLAY_POLICY_VERSION, REPORT_VERSION, assemble_report
from generals_replay_analyzer.report.model import (
    CanonicalValue,
    OllamaReportStatus,
    OllamaStatus,
    ReportAssemblyInput,
    ReportAssetDTO,
    ReportAvailability,
    ReportDocument,
    ReportEvidenceRef,
    ReportEvidenceTier,
    ReportLifecycle,
    ReportQualityIssue,
    ReportReceipt,
    ReportRequest,
    ReportValue,
    document_to_mapping,
    freeze_report_value,
)
from generals_replay_analyzer.report.render_html import render_html
from generals_replay_analyzer.report.render_json import render_json
from generals_replay_analyzer.report.render_text import render_text
from generals_replay_analyzer.report.resources import ReportResources, load_report_resources
from generals_replay_analyzer.storage import ContentAddressedStore, ContentStorageError, StoredContent

_ASSET_NAMESPACE = uuid5(NAMESPACE_URL, "replay-report-managed-asset-v1")
_MAX_PARSER_OBSERVATIONS = 256
_MAX_TELEMETRY_OBSERVATIONS = 512
_MAX_NOISY_TELEMETRY_OBSERVATIONS = 32
_MAX_CAMERA_COMBAT_ANCHORS = 256
_CRITICAL_TELEMETRY_EVENT_TYPES = frozenset(
    {
        # TheSuperHackers @fix Leex 25/08/2026 Reserve engine map/timebase authority inside bounded reports used by camera planning. (#TBD)
        "manifest",
        "construction_completed",
        "construction_started",
        "damage",
        "object_created",
        "object_destroyed",
        "order",
        "production_completed",
        "production_queued",
        "science_purchased",
        "special_power_used",
    }
)
_NOISY_TELEMETRY_EVENT_TYPES = frozenset({"entity_sample", "entity_state_changed"})
# TheSuperHackers @bugfix Leex 24/08/2026 Keep bulk engine grid snapshots in telemetry storage rather than bounded public report values. (#TBD)
_REPORT_EXCLUDED_TELEMETRY_EVENT_TYPES = frozenset({"partition_engine_grid_sample"})
_T = TypeVar("_T")


@dataclass(frozen=True)
class _SelectedEvidenceClaim:
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class _SelectedEvidenceBundle:
    """The structural citation view consumed by Task 10 domain validation."""

    claims: tuple[_SelectedEvidenceClaim, ...]


@dataclass(frozen=True, slots=True)
class _CameraCombatAnchor:
    combat_evidence_public_id: str
    combat_frame: int
    attacker_sample_evidence_public_id: str
    attacker_sample_frame: int


# TheSuperHackers @feature Leex 25/08/2026 Bound exact two-sided combat anchors by deterministic broadcast-time buckets. (#TBD)
def _bucket_camera_combat_anchors(
    anchors: Sequence[_CameraCombatAnchor],
    *,
    final_frame: int,
    logic_frames_per_second: int,
) -> tuple[_CameraCombatAnchor, ...]:
    if final_frame < 0 or logic_frames_per_second not in (30, 60):
        raise ValueError("camera combat anchor timebase is invalid")
    bucket_width = max(
        logic_frames_per_second * 15,
        (final_frame + 1 + _MAX_CAMERA_COMBAT_ANCHORS - 1)
        // _MAX_CAMERA_COMBAT_ANCHORS,
    )
    selected: list[_CameraCombatAnchor] = []
    used_buckets: set[int] = set()
    used_samples: set[str] = set()
    for anchor in sorted(
        anchors,
        key=lambda item: (
            item.combat_frame,
            item.combat_evidence_public_id,
            item.attacker_sample_frame,
            item.attacker_sample_evidence_public_id,
        ),
    ):
        bucket = anchor.combat_frame // bucket_width
        if bucket in used_buckets or anchor.attacker_sample_evidence_public_id in used_samples:
            continue
        used_buckets.add(bucket)
        used_samples.add(anchor.attacker_sample_evidence_public_id)
        selected.append(anchor)
    return tuple(selected[:_MAX_CAMERA_COMBAT_ANCHORS])


class ReportServiceError(RuntimeError):
    """Base path-free report service failure."""


class ReportNotFoundError(ReportServiceError):
    """A requested public replay/player identity is inaccessible."""


class ReportContractError(ReportServiceError):
    """Persisted evidence or report cache violates the accepted contract."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _canonical(value: object) -> CanonicalValue:
    return freeze_report_value(value)


def _mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ReportContractError(f"{label} must be a canonical mapping")
    return {str(key): item for key, item in value.items()}


# TheSuperHackers @performance Leex 23/08/2026 Keep full-match report timelines deterministic and bounded across the match. (#TBD)
def _evenly_sample(rows: Sequence[_T], limit: int) -> tuple[_T, ...]:
    if limit < 0:
        raise ValueError("sample limit must be nonnegative")
    if len(rows) <= limit:
        return tuple(rows)
    if limit == 0:
        return ()
    if limit == 1:
        return (rows[0],)
    last = len(rows) - 1
    return tuple(rows[(index * last) // (limit - 1)] for index in range(limit))


def _diagnostic_codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ("analysis_diagnostics_invalid",)
    codes = {
        str(item["code"])
        for item in value
        if isinstance(item, Mapping) and type(item.get("code")) is str and item["code"]
    }
    return tuple(sorted(codes))


def _feature_raw(feature: Feature) -> object | None:
    return {
        "integer": feature.integer_value,
        "real": feature.real_value,
        "text": feature.text_value,
        "boolean": feature.boolean_value,
        "json": feature.json_value,
    }[feature.value_type]


class ReportService:
    """Project accepted immutable ORM rows into stable public reports."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: AnalyzerSettings,
        store: ContentAddressedStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._store = store or ContentAddressedStore(settings.cache_directory / "reports")

    # TheSuperHackers @feature Leex 22/08/2026 Assemble and publish reports from persisted evidence without runtime engines or LLM calls. (#TBD)
    def create(self, request: ReportRequest) -> ReportReceipt:
        """Create or reuse one deterministic report, optionally publishing two managed assets."""
        if type(request) is not ReportRequest:
            raise TypeError("request must be a ReportRequest")
        source, replay_id, replay_player_id, analysis_run_id = self._load_source(request)
        loaded = load_report_resources()
        document = assemble_report(
            source,
            html_template_sha256=loaded.html_template_sha256,
            document_schema_sha256=loaded.document_schema_sha256,
        )
        structured_bytes, bundle_bytes = self._render_bytes(document, loaded)
        existing = self._existing(
            replay_id, replay_player_id, analysis_run_id, document, structured_bytes, bundle_bytes
        )
        if existing is not None:
            return self._receipt_from_existing(
                existing, document, structured_bytes, bundle_bytes, publish=request.publish
            )
        if not request.publish:
            return ReportReceipt(document, False, None, None)

        structured = self._store.store_bytes(structured_bytes)
        bundle = self._store.store_bytes(bundle_bytes)
        return self._persist(
            replay_id,
            replay_player_id,
            analysis_run_id,
            document,
            structured,
            bundle,
            structured_bytes,
            bundle_bytes,
        )

    @staticmethod
    def _render_bytes(document: ReportDocument, loaded: ReportResources) -> tuple[bytes, bytes]:
        structured_bytes = render_json(document)
        html = render_html(document)
        text = render_text(document)
        bundle_bytes = _canonical_bytes(
            {
                "schema_version": "report-presentation-bundle-v1",
                "report_version": REPORT_VERSION,
                "display_policy_version": DISPLAY_POLICY_VERSION,
                "html_template_sha256": loaded.html_template_sha256,
                "document_schema_sha256": loaded.document_schema_sha256,
                "html": {"sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(), "text": html},
                "text": {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text},
            }
        )
        return structured_bytes, bundle_bytes

    def _load_source(self, request: ReportRequest) -> tuple[ReportAssemblyInput, int, int | None, int | None]:
        with self._session_factory() as session:
            replay = session.scalar(select(Replay).where(Replay.public_id == request.replay_public_id))
            if replay is None:
                raise ReportNotFoundError("requested replay public ID was not found")
            replay_player: ReplayPlayer | None = None
            if request.replay_player_public_id is not None:
                replay_player = session.scalar(
                    select(ReplayPlayer).where(
                        ReplayPlayer.replay_id == replay.id,
                        ReplayPlayer.public_id == request.replay_player_public_id,
                    )
                )
                if replay_player is None:
                    raise ReportNotFoundError("requested replay player public ID was not found")

            parser = self._select_parser(session, replay, replay_player)
            telemetry = self._select_telemetry(
                session,
                replay,
                replay_player,
                parser,
                request.feature_set_public_ids,
            )
            player_authority = self._player_evidence_authority(
                session, replay_player, parser, telemetry
            )
            (
                observed,
                parser_evidence,
                telemetry_evidence,
                parser_total,
                telemetry_total,
                camera_combat_anchors,
            ) = self._observed_values(
                session, replay, replay_player, parser, telemetry, player_authority
            )
            availability = (
                *self._availability_values(
                    parser,
                    telemetry,
                    parser_evidence,
                    telemetry_evidence,
                    parser_total,
                    telemetry_total,
                ),
                *((camera_combat_anchors,) if camera_combat_anchors is not None else ()),
            )
            issues = tuple(
                ReportQualityIssue(
                    row.public_id,
                    row.stage,
                    row.issue_code,
                    row.severity,
                    _canonical(row.details_json),
                    row.resolved_at is not None,
                )
                for row in session.scalars(
                    select(ReplayQualityIssue)
                    .where(ReplayQualityIssue.replay_id == replay.id)
                    .order_by(ReplayQualityIssue.stage, ReplayQualityIssue.issue_code, ReplayQualityIssue.public_id)
                )
            )
            derived = (
                *self._feature_values(
                    session,
                    replay.id,
                    replay_player,
                    parser,
                    telemetry,
                    player_authority,
                    request.feature_set_public_ids,
                ),
                *self._strategy_values(
                    session, replay.id, replay_player, parser, telemetry, player_authority
                ),
                *self._longitudinal_values(session, replay.id, replay_player),
            )
            ollama, inferred, analysis_id = self._ollama_values(
                session,
                replay.id,
                replay_player,
                request.include_validated_ollama,
                derived,
                observed,
                request.analysis_run_id,
            )
            component_identity: dict[str, object] = {
                "parser": None
                if parser is None
                else {
                    "run_id": parser.run_id,
                    "parser_version": parser.parser_version,
                    "schema_version": parser.schema_version,
                    "result_sha256": parser.result_sha256,
                    "status": parser.status,
                    "completion_status": parser.completion_status,
                },
                "telemetry": None
                if telemetry is None
                else {
                    "run_id": telemetry.run_id,
                    "schema_version": telemetry.schema_version,
                    "engine_build": telemetry.engine_build,
                    "trace_sha256": telemetry.trace_sha256,
                    "status": telemetry.status,
                    "runner_status": telemetry.runner_status,
                },
            }
            source = ReportAssemblyInput(
                replay_public_id=replay.public_id,
                replay_sha256=replay.sha256,
                replay_player_public_id=None if replay_player is None else replay_player.public_id,
                header_identity=_canonical(
                    {
                        "version_string": replay.version_string,
                        "version_number": replay.version_number,
                        "frame_count": replay.frame_count,
                        "map_name": replay.map_name,
                        "header": replay.header_json,
                    }
                ),
                component_identity=_canonical(component_identity),
                lifecycle=ReportLifecycle(
                    replay.lifecycle_state,
                    None if parser is None else parser.completion_status,
                    None if telemetry is None else telemetry.status,
                    None if telemetry is None else telemetry.runner_status,
                ),
                evidence_availability=availability,
                quality_issues=issues,
                observed=observed,
                derived=derived,
                inferred=inferred,
                ollama=ollama,
                warnings=tuple(
                    sorted(
                        {
                            str(value)
                            for value in (
                                ()
                                if parser is None or not isinstance(parser.warnings_json, list)
                                else parser.warnings_json
                            )
                        }
                    )
                ),
            )
            return source, replay.id, None if replay_player is None else replay_player.id, analysis_id

    @staticmethod
    def _select_parser(session: Session, replay: Replay, replay_player: ReplayPlayer | None) -> ParserRun | None:
        if replay_player is not None:
            parser = session.get(ParserRun, replay_player.parser_run_id)
            if parser is None or parser.replay_id != replay.id or parser.status != "succeeded":
                raise ReportContractError("requested player has no exact successful parser graph")
            return parser
        rows = tuple(
            session.scalars(select(ParserRun).where(ParserRun.replay_id == replay.id, ParserRun.status == "succeeded"))
        )
        if len(rows) > 1:
            raise ReportContractError("replay-wide parser graph is ambiguous")
        return None if not rows else rows[0]

    @staticmethod
    def _select_telemetry(
        session: Session,
        replay: Replay,
        replay_player: ReplayPlayer | None,
        parser: ParserRun | None,
        feature_set_public_ids: tuple[str, ...] = (),
    ) -> TelemetryRun | None:
        own_query = (
            select(EvidenceItem.telemetry_run_id)
            .join(Feature, Feature.evidence_item_id == EvidenceItem.id)
            .join(FeatureSet, Feature.feature_set_id == FeatureSet.id)
            .where(FeatureSet.replay_id == replay.id, FeatureSet.status == "succeeded")
        )
        linked_query = (
            select(EvidenceItem.telemetry_run_id)
            .join(FeatureEvidence, FeatureEvidence.evidence_item_id == EvidenceItem.id)
            .join(Feature, Feature.id == FeatureEvidence.feature_id)
            .join(FeatureSet, Feature.feature_set_id == FeatureSet.id)
            .where(FeatureSet.replay_id == replay.id, FeatureSet.status == "succeeded")
        )
        if replay_player is None:
            own_query = own_query.where(FeatureSet.replay_player_id.is_(None))
            linked_query = linked_query.where(FeatureSet.replay_player_id.is_(None))
        else:
            own_query = own_query.where(FeatureSet.replay_player_id == replay_player.id)
            linked_query = linked_query.where(FeatureSet.replay_player_id == replay_player.id)
        if feature_set_public_ids:
            own_query = own_query.where(FeatureSet.public_id.in_(feature_set_public_ids))
            linked_query = linked_query.where(FeatureSet.public_id.in_(feature_set_public_ids))
        telemetry_ids = {
            telemetry_id
            for telemetry_id in (*session.scalars(own_query), *session.scalars(linked_query))
            if telemetry_id is not None
        }
        if len(telemetry_ids) > 1:
            raise ReportContractError("successful feature graphs select multiple telemetry runs")
        if telemetry_ids:
            telemetry = session.get(TelemetryRun, next(iter(telemetry_ids)))
            if (
                telemetry is None
                or telemetry.replay_id != replay.id
                or telemetry.status != "succeeded"
                or parser is None
                or not isinstance(telemetry.settings_json, Mapping)
                or telemetry.settings_json.get("parser_run_id") != parser.run_id
            ):
                raise ReportContractError(
                    "successful feature graph telemetry does not match its authoritative parser"
                )
            return telemetry
        # TheSuperHackers @bugfix Leex 26/08/2026 Keep a report bound to its exact parser-only feature selection instead of falling back to historical telemetry. (#TBD)
        if feature_set_public_ids:
            return None
        candidates = tuple(
            session.scalars(
                select(TelemetryRun).where(TelemetryRun.replay_id == replay.id, TelemetryRun.status == "succeeded")
            )
        )
        if parser is not None:
            candidates = tuple(
                row
                for row in candidates
                if isinstance(row.settings_json, Mapping) and row.settings_json.get("parser_run_id") == parser.run_id
            )
        if len(candidates) > 1:
            raise ReportContractError("successful telemetry graph is ambiguous")
        return None if not candidates else candidates[0]

    @staticmethod
    def _telemetry_sampling_contract(
        all_event_rows: tuple[tuple[TelemetryEvent, EvidenceItem], ...],
        telemetry: TelemetryRun,
    ) -> tuple[int, int] | None:
        manifests = tuple(
            event for event, _ in all_event_rows if event.event_type == "manifest"
        )
        if len(manifests) != 1:
            return None
        payload = _mapping(manifests[0].payload_json, label="manifest payload")
        exporter = _mapping(
            payload.get("exporter_settings"), label="manifest exporter settings"
        )
        movement_sample_frames = exporter.get("movement_sample_frames")
        settings = _mapping(telemetry.settings_json, label="telemetry settings")
        logic_frames_per_second = settings.get(
            "logic_frames_per_second", payload.get("logic_frames_per_second")
        )
        if (
            type(movement_sample_frames) is not int
            or not 1 <= movement_sample_frames <= 3600
            or logic_frames_per_second not in (30, 60)
        ):
            return None
        return movement_sample_frames, logic_frames_per_second

    @staticmethod
    def _camera_combat_anchor_value(
        session: Session,
        replay: Replay,
        telemetry: TelemetryRun | None,
        all_event_rows: tuple[tuple[TelemetryEvent, EvidenceItem], ...],
    ) -> ReportValue | None:
        if telemetry is None or telemetry.final_frame is None:
            return None
        sampling = ReportService._telemetry_sampling_contract(
            all_event_rows, telemetry
        )
        if sampling is None:
            return None
        movement_sample_frames, logic_frames_per_second = sampling
        event_rows_by_id = {event.id: (event, evidence) for event, evidence in all_event_rows}
        bucket_width = max(
            logic_frames_per_second * 15,
            (telemetry.final_frame + 1 + _MAX_CAMERA_COMBAT_ANCHORS - 1)
            // _MAX_CAMERA_COMBAT_ANCHORS,
        )
        latest_sample = aliased(EntitySample)
        latest_sample_id = (
            select(latest_sample.id)
            .where(
                latest_sample.telemetry_run_id == telemetry.id,
                latest_sample.entity_id == CombatEvent.attacker_entity_id,
                latest_sample.frame <= CombatEvent.frame,
            )
            .order_by(latest_sample.frame.desc(), latest_sample.sequence.desc())
            .limit(1)
            .correlate(CombatEvent)
            .scalar_subquery()
        )
        # TheSuperHackers @performance Leex 25/08/2026 Rank valid causal combat anchors in SQL before hydrating at most one exact pair per bounded camera bucket. (#TBD)
        valid_pairs = (
            select(
                CombatEvent.id.label("combat_id"),
                EntitySample.id.label("sample_id"),
                (CombatEvent.frame // bucket_width).label("combat_bucket"),
                CombatEvent.frame.label("combat_frame"),
            )
            .join(Entity, Entity.id == CombatEvent.attacker_entity_id)
            .join(EntitySample, EntitySample.id == latest_sample_id)
            .where(
                CombatEvent.telemetry_run_id == telemetry.id,
                CombatEvent.killing_blow.is_(True),
                CombatEvent.attacker_entity_id.is_not(None),
                Entity.telemetry_run_id == telemetry.id,
                or_(
                    Entity.destruction_frame.is_(None),
                    Entity.destruction_frame >= CombatEvent.frame,
                ),
                or_(
                    func.json_extract(
                        EntitySample.payload_json, "$.is_engine_moving"
                    )
                    == 0,
                    CombatEvent.frame - EntitySample.frame <= movement_sample_frames,
                ),
            )
            .subquery()
        )
        sample_ranked_pairs = (
            select(
                valid_pairs.c.combat_id,
                valid_pairs.c.sample_id,
                valid_pairs.c.combat_bucket,
                valid_pairs.c.combat_frame,
                func.row_number()
                .over(
                    partition_by=valid_pairs.c.sample_id,
                    order_by=(valid_pairs.c.combat_frame, valid_pairs.c.combat_id),
                )
                .label("sample_rank"),
            )
            .subquery()
        )
        ranked_pairs = (
            select(
                sample_ranked_pairs.c.combat_id,
                sample_ranked_pairs.c.sample_id,
                sample_ranked_pairs.c.combat_frame,
                func.row_number()
                .over(
                    partition_by=sample_ranked_pairs.c.combat_bucket,
                    order_by=(
                        sample_ranked_pairs.c.combat_frame,
                        sample_ranked_pairs.c.combat_id,
                    ),
                )
                .label("bucket_rank"),
            )
            .where(sample_ranked_pairs.c.sample_rank == 1)
            .subquery()
        )
        combat_sample_rows = tuple(
            session.execute(
                select(CombatEvent, EntitySample, Entity)
                .join(ranked_pairs, ranked_pairs.c.combat_id == CombatEvent.id)
                .join(EntitySample, EntitySample.id == ranked_pairs.c.sample_id)
                .join(Entity, Entity.id == CombatEvent.attacker_entity_id)
                .where(ranked_pairs.c.bucket_rank == 1)
                .order_by(ranked_pairs.c.combat_frame, ranked_pairs.c.combat_id)
                .limit(_MAX_CAMERA_COMBAT_ANCHORS)
            )
        )
        candidates: list[_CameraCombatAnchor] = []
        for combat, sample, entity in combat_sample_rows:
            combat_row = event_rows_by_id.get(combat.telemetry_event_id)
            if (
                combat_row is None
                or (
                    entity.destruction_frame is not None
                    and entity.destruction_frame < combat.frame
                )
            ):
                continue
            sample_row = event_rows_by_id.get(sample.telemetry_event_id)
            if sample_row is None:
                continue
            combat_event, combat_evidence = combat_row
            sample_event, sample_evidence = sample_row
            sample_payload = _mapping(
                sample.payload_json, label="attacker sample payload"
            )
            is_engine_moving = sample_payload.get("is_engine_moving")
            if is_engine_moving is not False and (
                combat.frame - sample.frame > movement_sample_frames
            ):
                continue
            if (
                combat_event.frame != combat.frame
                or sample_event.frame != sample.frame
                or combat_evidence.replay_id != replay.id
                or sample_evidence.replay_id != replay.id
                or combat_evidence.telemetry_run_id != telemetry.id
                or sample_evidence.telemetry_run_id != telemetry.id
                or combat_evidence.tier != "observed"
                or sample_evidence.tier != "observed"
                or combat_evidence.source_kind != "telemetry_event"
                or sample_evidence.source_kind != "telemetry_event"
            ):
                raise ReportContractError(
                    "camera combat anchor evidence does not match telemetry authority"
                )
            candidates.append(
                _CameraCombatAnchor(
                    combat_evidence.public_id,
                    combat.frame,
                    sample_evidence.public_id,
                    sample.frame,
                )
            )
        selected = _bucket_camera_combat_anchors(
            candidates,
            final_frame=telemetry.final_frame,
            logic_frames_per_second=logic_frames_per_second,
        )
        if not selected:
            return None
        evidence = tuple(
            ReportEvidenceRef(public_id, "observed")
            for public_id in sorted(
                {
                    public_id
                    for anchor in selected
                    for public_id in (
                        anchor.combat_evidence_public_id,
                        anchor.attacker_sample_evidence_public_id,
                    )
                }
            )
        )
        return ReportValue(
            "availability:camera_combat_anchors",
            "availability",
            "Camera combat anchors",
            _canonical(
                {
                    "schema_version": "camera-combat-anchors-v1",
                    "bucket_width_frames": bucket_width,
                    "pairs": [
                        {
                            "combat_evidence_public_id": anchor.combat_evidence_public_id,
                            "combat_frame": anchor.combat_frame,
                            "attacker_sample_evidence_public_id": anchor.attacker_sample_evidence_public_id,
                            "attacker_sample_frame": anchor.attacker_sample_frame,
                        }
                        for anchor in selected
                    ],
                }
            ),
            None,
            "available",
            None,
            _canonical({"scope_type": "replay"}),
            (
                min(anchor.attacker_sample_frame for anchor in selected),
                max(anchor.combat_frame for anchor in selected),
            ),
            evidence,
            _canonical(
                {
                    "maximum_pair_count": _MAX_CAMERA_COMBAT_ANCHORS,
                    "selected_pair_count": len(selected),
                }
            ),
        )

    @staticmethod
    def _observed_values(
        session: Session,
        replay: Replay,
        replay_player: ReplayPlayer | None,
        parser: ParserRun | None,
        telemetry: TelemetryRun | None,
        player_authority: frozenset[int] | None,
    ) -> tuple[
        tuple[ReportValue, ...],
        tuple[ReportEvidenceRef, ...],
        tuple[ReportEvidenceRef, ...],
        int,
        int,
        ReportValue | None,
    ]:
        values: list[ReportValue] = []
        parser_refs: list[ReportEvidenceRef] = []
        telemetry_refs: list[ReportEvidenceRef] = []
        parser_total = 0
        telemetry_total = 0
        camera_combat_anchors: ReportValue | None = None
        if parser is not None:
            all_command_rows = tuple(
                session.execute(
                    select(ReplayCommand, EvidenceItem)
                    .join(EvidenceItem, EvidenceItem.id == ReplayCommand.evidence_item_id)
                    .where(ReplayCommand.parser_run_id == parser.id, ReplayCommand.replay_id == replay.id)
                    .order_by(ReplayCommand.frame, EvidenceItem.source_key, EvidenceItem.public_id)
                )
            )
            if replay_player is not None:
                all_command_rows = tuple(
                    row for row in all_command_rows if row[0].replay_player_id == replay_player.id
                )
            parser_total = len(all_command_rows)
            command_rows = _evenly_sample(all_command_rows, _MAX_PARSER_OBSERVATIONS)
            for command, evidence in command_rows:
                if (
                    evidence.replay_id != replay.id
                    or evidence.tier != "observed"
                    or evidence.source_kind != "parser_command"
                    or evidence.parser_run_id != parser.id
                    or evidence.telemetry_run_id is not None
                ):
                    raise ReportContractError("parser command evidence does not match the selected graph")
                ref = ReportEvidenceRef(evidence.public_id, "observed")
                parser_refs.append(ref)
                values.append(
                    ReportValue(
                        f"observed:parser_command:{evidence.public_id}",
                        "timeline",
                        command.message_name or f"message_{command.message_type}",
                        _canonical(
                            {
                                "arguments": command.arguments_json,
                                "message_name": command.message_name,
                                "message_type": command.message_type,
                            }
                        ),
                        None,
                        "available",
                        None,
                        _canonical(
                            {
                                "scope_type": "player" if command.replay_player_id is not None else "replay",
                                "public_id": None if replay_player is None else replay_player.public_id,
                            }
                        ),
                        (command.frame, command.frame),
                        (ref,),
                        _canonical({"source_kind": evidence.source_kind, "schema_version": evidence.schema_version}),
                    )
                )
        if telemetry is not None:
            all_event_rows = tuple(
                (row[0], row[1])
                for row in session.execute(
                    select(TelemetryEvent, EvidenceItem)
                    .join(EvidenceItem, EvidenceItem.id == TelemetryEvent.evidence_item_id)
                    .where(TelemetryEvent.telemetry_run_id == telemetry.id)
                    .order_by(TelemetryEvent.frame, EvidenceItem.source_key, EvidenceItem.public_id)
                )
            )
            if replay_player is not None:
                all_event_rows = tuple(
                    row
                    for row in all_event_rows
                    if player_authority is not None and row[1].id in player_authority
                )
            # TheSuperHackers @fix Leex 25/08/2026 Keep opposing-side camera anchors replay-wide because player reports cannot authorize an opponent's position evidence. (#TBD)
            if replay_player is None:
                camera_combat_anchors = ReportService._camera_combat_anchor_value(
                    session, replay, telemetry, all_event_rows
                )
            telemetry_total = len(all_event_rows)
            reportable_event_rows = tuple(
                row
                for row in all_event_rows
                if row[0].event_type not in _REPORT_EXCLUDED_TELEMETRY_EVENT_TYPES
            )
            critical = tuple(
                row for row in reportable_event_rows if row[0].event_type in _CRITICAL_TELEMETRY_EVENT_TYPES
            )
            routine = tuple(
                row
                for row in reportable_event_rows
                if row[0].event_type not in _CRITICAL_TELEMETRY_EVENT_TYPES
                and row[0].event_type not in _NOISY_TELEMETRY_EVENT_TYPES
            )
            noisy = tuple(
                row for row in reportable_event_rows if row[0].event_type in _NOISY_TELEMETRY_EVENT_TYPES
            )
            semantic_limit = _MAX_TELEMETRY_OBSERVATIONS - _MAX_NOISY_TELEMETRY_OBSERVATIONS
            selected_critical = _evenly_sample(critical, semantic_limit)
            selected_routine = _evenly_sample(routine, semantic_limit - len(selected_critical))
            selected_noisy = _evenly_sample(noisy, _MAX_NOISY_TELEMETRY_OBSERVATIONS)
            event_rows = tuple(
                sorted(
                    (*selected_critical, *selected_routine, *selected_noisy),
                    key=lambda row: (row[0].frame, row[1].source_key, row[1].public_id),
                )
            )
            for event, evidence in event_rows:
                if (
                    evidence.replay_id != replay.id
                    or evidence.tier != "observed"
                    or evidence.source_kind != "telemetry_event"
                    or evidence.telemetry_run_id != telemetry.id
                    or evidence.parser_run_id is not None
                ):
                    raise ReportContractError("telemetry event evidence does not match the selected graph")
                payload = _mapping(event.payload_json, label="telemetry event payload")
                player_index = payload.get("player_index")
                ref = ReportEvidenceRef(evidence.public_id, "observed")
                telemetry_refs.append(ref)
                values.append(
                    ReportValue(
                        f"observed:telemetry_event:{evidence.public_id}",
                        "timeline",
                        event.event_type,
                        _canonical(payload),
                        None,
                        "available",
                        None,
                        _canonical(
                            {
                                "scope_type": "player" if player_index is not None else "replay",
                                "public_id": None if replay_player is None else replay_player.public_id,
                            }
                        ),
                        (event.frame, event.frame),
                        (ref,),
                        _canonical({"source_kind": evidence.source_kind, "schema_version": evidence.schema_version}),
                    )
                )
        return (
            tuple(values),
            tuple(parser_refs),
            tuple(telemetry_refs),
            parser_total,
            telemetry_total,
            camera_combat_anchors,
        )

    @staticmethod
    def _availability_values(
        parser: ParserRun | None,
        telemetry: TelemetryRun | None,
        parser_evidence: tuple[ReportEvidenceRef, ...],
        telemetry_evidence: tuple[ReportEvidenceRef, ...],
        parser_total: int,
        telemetry_total: int,
    ) -> tuple[ReportValue, ...]:
        values: list[ReportValue] = []
        for claim_id, label, selected, evidence, total, absent_reason, empty_reason in (
            (
                "availability:parser",
                "Parser evidence",
                parser is not None,
                parser_evidence,
                parser_total,
                "parser_unavailable",
                "parser_observations_empty",
            ),
            (
                "availability:telemetry",
                "Telemetry evidence",
                telemetry is not None,
                telemetry_evidence,
                telemetry_total,
                "telemetry_unavailable",
                "telemetry_observations_empty",
            ),
        ):
            if selected and evidence:
                bounded = len(evidence) < total
                values.append(
                    ReportValue(
                        claim_id,
                        "availability",
                        label,
                        True,
                        None,
                        "partial" if bounded else "available",
                        "report_observations_bounded" if bounded else None,
                        _canonical({}),
                        None,
                        evidence,
                        _canonical({"selected_count": len(evidence), "total_count": total}),
                    )
                )
            else:
                values.append(
                    ReportValue(
                        claim_id,
                        "availability",
                        label,
                        None,
                        None,
                        "unavailable",
                        empty_reason if selected else absent_reason,
                        _canonical({}),
                        None,
                        (),
                        _canonical({}),
                    )
                )
        return tuple(values)

    @staticmethod
    def _feature_values(
        session: Session,
        replay_id: int,
        replay_player: ReplayPlayer | None,
        parser: ParserRun | None,
        telemetry: TelemetryRun | None,
        player_authority: frozenset[int] | None,
        feature_set_public_ids: tuple[str, ...] = (),
    ) -> tuple[ReportValue, ...]:
        query = (
            select(Feature, FeatureSet, EvidenceItem)
            .join(FeatureSet, Feature.feature_set_id == FeatureSet.id)
            .join(EvidenceItem, Feature.evidence_item_id == EvidenceItem.id)
            .where(FeatureSet.replay_id == replay_id, FeatureSet.status == "succeeded")
            .order_by(Feature.name, Feature.public_id)
        )
        if replay_player is not None:
            query = query.where(FeatureSet.replay_player_id == replay_player.id)
        else:
            query = query.where(FeatureSet.replay_player_id.is_(None))
        if feature_set_public_ids:
            query = query.where(FeatureSet.public_id.in_(feature_set_public_ids))
        # TheSuperHackers @bugfix Leex 25/08/2026 Select features from the report's current telemetry authority instead of stale parser-only generations. (#TBD)
        if telemetry is None:
            query = query.where(EvidenceItem.telemetry_run_id.is_(None))
        else:
            query = query.where(EvidenceItem.telemetry_run_id == telemetry.id)
        rows = tuple(session.execute(query))
        feature_ids = tuple(row.id for row, _feature_set, _own in rows)
        linked_by_feature: dict[int, list[EvidenceItem]] = {feature_id: [] for feature_id in feature_ids}
        if feature_ids:
            for feature_id, item in session.execute(
                select(FeatureEvidence.feature_id, EvidenceItem)
                .join(EvidenceItem, FeatureEvidence.evidence_item_id == EvidenceItem.id)
                .where(FeatureEvidence.feature_id.in_(feature_ids))
                .order_by(FeatureEvidence.feature_id, FeatureEvidence.evidence_item_id)
            ):
                linked_by_feature[feature_id].append(item)
        output: list[ReportValue] = []
        for row, feature_set, own in rows:
            if (
                own.replay_id != replay_id
                or own.tier != "derived"
                or own.source_kind != "feature"
                or (telemetry is not None and own.telemetry_run_id != telemetry.id)
            ):
                raise ReportContractError("successful feature is missing its derived public evidence")
            evidence = {ReportEvidenceRef(own.public_id, "derived")}
            for item in linked_by_feature[row.id]:
                ReportService._validate_predecessor(item, replay_id, parser, telemetry, set())
                if replay_player is not None:
                    ReportService._validate_player_predecessor(item, player_authority)
                evidence.add(ReportEvidenceRef(item.public_id, cast(ReportEvidenceTier, item.tier)))
            raw = _feature_raw(row)
            output.append(
                ReportValue(
                    f"feature:{row.name}:{row.public_id}",
                    "features",
                    row.name,
                    _canonical(raw),
                    row.unit,
                    cast(ReportAvailability, row.quality),
                    row.quality_reason,
                    _canonical({"scope_type": row.scope_type, "scope_key": row.scope_key}),
                    (row.frame_start, row.frame_end),
                    tuple(evidence) if raw is not None else (),
                    _canonical(
                        {
                            "extractor": {
                                "name": feature_set.extractor_name,
                                "version": feature_set.extractor_version,
                                "input_digest": feature_set.input_digest,
                            },
                            "feature": row.details_json,
                        }
                    ),
                )
            )
        return tuple(output)

    @staticmethod
    def _strategy_values(
        session: Session,
        replay_id: int,
        replay_player: ReplayPlayer | None,
        parser: ParserRun | None,
        telemetry: TelemetryRun | None,
        player_authority: frozenset[int] | None,
    ) -> tuple[ReportValue, ...]:
        query = (
            select(StrategyAssessment, EvidenceItem)
            .join(EvidenceItem, StrategyAssessment.evidence_item_id == EvidenceItem.id)
            .where(StrategyAssessment.replay_id == replay_id, StrategyAssessment.method == "rule")
            .order_by(StrategyAssessment.strategy_label, StrategyAssessment.public_id)
        )
        if replay_player is not None:
            query = query.where(StrategyAssessment.replay_player_id == replay_player.id)
        else:
            query = query.where(StrategyAssessment.replay_player_id.is_(None))
        feature_query = (
            select(Feature.evidence_item_id)
            .join(FeatureSet, Feature.feature_set_id == FeatureSet.id)
            .join(EvidenceItem, Feature.evidence_item_id == EvidenceItem.id)
            .where(FeatureSet.replay_id == replay_id, FeatureSet.status == "succeeded")
        )
        if replay_player is None:
            feature_query = feature_query.where(FeatureSet.replay_player_id.is_(None))
        else:
            feature_query = feature_query.where(FeatureSet.replay_player_id == replay_player.id)
        # TheSuperHackers @bugfix Leex 25/08/2026 Keep strategies and their feature predecessors on the report's selected telemetry branch. (#TBD)
        if telemetry is None:
            query = query.where(EvidenceItem.telemetry_run_id.is_(None))
            feature_query = feature_query.where(EvidenceItem.telemetry_run_id.is_(None))
        else:
            query = query.where(EvidenceItem.telemetry_run_id == telemetry.id)
            feature_query = feature_query.where(EvidenceItem.telemetry_run_id == telemetry.id)
        allowed_derived = set(session.scalars(feature_query))
        rows = tuple(session.execute(query))
        assessment_ids = tuple(row.id for row, _own in rows)
        linked_by_assessment: dict[int, list[EvidenceItem]] = {
            assessment_id: [] for assessment_id in assessment_ids
        }
        if assessment_ids:
            for assessment_id, item in session.execute(
                select(AssessmentEvidence.assessment_id, EvidenceItem)
                .join(EvidenceItem, AssessmentEvidence.evidence_item_id == EvidenceItem.id)
                .where(AssessmentEvidence.assessment_id.in_(assessment_ids))
                .order_by(AssessmentEvidence.assessment_id, AssessmentEvidence.evidence_item_id)
            ):
                linked_by_assessment[assessment_id].append(item)
        output: list[ReportValue] = []
        for row, own in rows:
            if (
                own.replay_id != replay_id
                or own.tier != "derived"
                or own.source_kind != "strategy_rule"
                or (telemetry is not None and own.telemetry_run_id != telemetry.id)
            ):
                raise ReportContractError("deterministic strategy assessment is missing derived public evidence")
            evidence = {ReportEvidenceRef(own.public_id, "derived")}
            for item in linked_by_assessment[row.id]:
                ReportService._validate_predecessor(item, replay_id, parser, telemetry, allowed_derived)
                if replay_player is not None:
                    ReportService._validate_player_predecessor(item, player_authority)
                evidence.add(ReportEvidenceRef(item.public_id, cast(ReportEvidenceTier, item.tier)))
            raw: object | None = (
                None
                if row.quality == "unavailable"
                else {
                    "strategy_label": row.strategy_label,
                    "phase": row.phase,
                    "confidence": row.confidence,
                }
            )
            details = _mapping(row.details_json, label="strategy details")
            reason_value = details.get("reason")
            if row.quality == "available":
                reason = None
            elif type(reason_value) is str and reason_value:
                reason = reason_value
            else:
                raise ReportContractError("non-available Task 8 assessment is missing its exact persisted reason")
            output.append(
                ReportValue(
                    f"strategy:{row.strategy_label}:{row.public_id}",
                    "strategy",
                    row.strategy_label,
                    _canonical(raw),
                    None,
                    cast(ReportAvailability, row.quality),
                    reason,
                    _canonical({"scope_type": "player" if row.replay_player_id is not None else "replay"}),
                    (row.frame_start, row.frame_end),
                    tuple(evidence) if raw is not None else (),
                    _canonical(
                        {
                            "method": row.method,
                            "taxonomy_version": row.taxonomy_version,
                            "rule_version": row.rule_version,
                            "model_version": row.model_version,
                            "assessment": details,
                        }
                    ),
                )
            )
        return tuple(output)

    @staticmethod
    def _longitudinal_values(
        session: Session,
        replay_id: int,
        replay_player: ReplayPlayer | None,
    ) -> tuple[ReportValue, ...]:
        if replay_player is None or replay_player.player_id is None:
            return ()
        canonical_player = session.get(Player, replay_player.player_id)
        if canonical_player is None or canonical_player.retired_at is not None:
            return ()
        query = (
            select(LongitudinalResult)
            .join(LongitudinalMember, LongitudinalMember.longitudinal_result_id == LongitudinalResult.id)
            .join(LongitudinalRun, LongitudinalResult.longitudinal_run_id == LongitudinalRun.id)
            .where(
                LongitudinalMember.replay_id == replay_id,
                LongitudinalMember.replay_player_id == replay_player.id,
                LongitudinalRun.player_id == canonical_player.id,
                LongitudinalRun.identity_revision == canonical_player.identity_revision,
                LongitudinalRun.status == "succeeded",
            )
            .order_by(LongitudinalResult.result_name, LongitudinalResult.public_id)
        )
        rows = tuple(dict.fromkeys(session.scalars(query)))
        output: list[ReportValue] = []
        for row in rows:
            evidence = session.get(EvidenceItem, row.evidence_item_id)
            run = session.get(LongitudinalRun, row.longitudinal_run_id)
            if evidence is None or run is None:
                raise ReportContractError("successful longitudinal result is missing its accepted Task 9 graph")
            statistics = _mapping(row.statistics_json, label="longitudinal statistics")
            public_statistics = statistics.get("public_statistics")
            member_snapshots = statistics.get("member_snapshots")
            anchor = statistics.get("evidence_anchor")
            if (
                statistics.get("storage_schema") != "longitudinal-result-storage-v1"
                or not isinstance(public_statistics, Mapping)
                or not isinstance(member_snapshots, list)
                or not isinstance(anchor, Mapping)
            ):
                raise ReportContractError("longitudinal result storage is outside the accepted Task 9 contract")
            try:
                member_dtos = tuple(
                    LongitudinalMemberDTO.from_mapping(cast(Mapping[str, object], item)) for item in member_snapshots
                )
            except (KeyError, TypeError, ValueError):
                raise ReportContractError("longitudinal member snapshots are invalid") from None
            if _canonical_bytes(member_snapshots) != _canonical_bytes(
                [member.as_canonical() for member in member_dtos]
            ):
                raise ReportContractError("longitudinal member snapshots are noncanonical")
            members = tuple(
                session.scalars(select(LongitudinalMember).where(LongitudinalMember.longitudinal_result_id == row.id))
            )
            if len(members) != len(member_dtos):
                raise ReportContractError("longitudinal member graph does not match its persisted snapshots")
            members_by_evidence: dict[str, LongitudinalMember] = {}
            for member in members:
                member_evidence = session.get(EvidenceItem, member.evidence_item_id)
                if member_evidence is None or member_evidence.public_id in members_by_evidence:
                    raise ReportContractError("longitudinal member evidence graph is ambiguous")
                members_by_evidence[member_evidence.public_id] = member
            for member_dto in member_dtos:
                selected_member = members_by_evidence.get(member_dto.evidence_public_id)
                if selected_member is None:
                    raise ReportContractError("longitudinal member evidence graph does not match its snapshot")
                member_replay = session.get(Replay, selected_member.replay_id)
                member_player = session.get(ReplayPlayer, selected_member.replay_player_id)
                feature_set = session.get(FeatureSet, selected_member.feature_set_id)
                feature = (
                    None if selected_member.feature_id is None else session.get(Feature, selected_member.feature_id)
                )
                member_evidence = session.get(EvidenceItem, selected_member.evidence_item_id)
                direct_rows: tuple[EvidenceItem, ...] = ()
                if feature is not None:
                    direct_rows = tuple(
                        session.execute(
                            select(EvidenceItem)
                            .join(FeatureEvidence, FeatureEvidence.evidence_item_id == EvidenceItem.id)
                            .where(FeatureEvidence.feature_id == feature.id, FeatureEvidence.role == "input")
                            .order_by(EvidenceItem.source_kind, EvidenceItem.source_key, EvidenceItem.public_id)
                        ).scalars()
                    )
                expected_direct = tuple(
                    (
                        item.public_id,
                        item.tier,
                        item.source_kind,
                        item.source_key,
                        item.schema_version,
                    )
                    for item in member_dto.direct_evidence
                )
                actual_direct = tuple(
                    (item.public_id, item.tier, item.source_kind, item.source_key, item.schema_version)
                    for item in direct_rows
                )
                if (
                    member_replay is None
                    or member_player is None
                    or feature_set is None
                    or feature is None
                    or selected_member.strategy_assessment_id is not None
                    or member_evidence is None
                    or member_player.player_id != run.player_id
                    or member_player.replay_id != member_replay.id
                    or feature_set.replay_id != member_replay.id
                    or feature_set.replay_player_id != member_player.id
                    or feature.feature_set_id != feature_set.id
                    or feature.replay_player_id != member_player.id
                    or feature.evidence_item_id != member_evidence.id
                    or member_replay.public_id != member_dto.replay_public_id
                    or member_replay.sha256 != member_dto.replay_sha256
                    or member_player.public_id != member_dto.replay_player_public_id
                    or feature_set.public_id != member_dto.feature_set_public_id
                    or feature.public_id != member_dto.feature_public_id
                    or member_dto.strategy_assessment_public_id is not None
                    or member_evidence.public_id != member_dto.evidence_public_id
                    or member_evidence.tier != "derived"
                    or member_evidence.replay_id != member_replay.id
                    or member_evidence.source_kind != member_dto.derived_evidence_source_kind
                    or member_evidence.source_key != member_dto.derived_evidence_source_key
                    or member_evidence.schema_version != member_dto.derived_evidence_schema_version
                    or actual_direct != expected_direct
                    or any(item.replay_id != member_replay.id or item.tier != "observed" for item in direct_rows)
                ):
                    raise ReportContractError("longitudinal member graph is outside the accepted Task 9 graph")
            frame_start = min((member.frame_start for member in member_dtos), default=0)
            frame_end = max((member.frame_end for member in member_dtos), default=0)
            source_key = (
                f"longitudinal:{run.run_id}:{run.cache_key}:{row.result_name}:{row.result_kind}:"
                f"{frame_start}:{frame_end}"
            )
            anchor_replay = session.scalar(select(Replay).where(Replay.public_id == anchor.get("replay_public_id")))
            anchor_player = session.scalar(
                select(ReplayPlayer).where(ReplayPlayer.public_id == anchor.get("replay_player_public_id"))
            )
            if (
                row.public_id
                != str(uuid5(NAMESPACE_URL, f"{run.run_id}:{run.cache_key}:{row.result_kind}:{row.result_name}"))
                or evidence.public_id != str(uuid5(NAMESPACE_URL, source_key))
                or evidence.tier != "derived"
                or evidence.source_kind != "longitudinal_corpus"
                or evidence.source_key != source_key
                or evidence.schema_version != 1
                or evidence.parser_run_id is not None
                or evidence.telemetry_run_id is not None
                or anchor.get("role") != "schema_required_corpus_anchor"
                or anchor_replay is None
                or anchor_player is None
                or anchor_replay.sha256 != anchor.get("replay_sha256")
                or anchor_player.replay_id != anchor_replay.id
                or anchor_player.player_id != run.player_id
                or evidence.replay_id != anchor_replay.id
            ):
                raise ReportContractError("longitudinal result evidence is outside the accepted Task 9 graph")
            raw = None if row.quality == "unavailable" else dict(public_statistics)
            output.append(
                ReportValue(
                    f"longitudinal:{row.result_name}:{row.public_id}",
                    "longitudinal",
                    row.result_name,
                    _canonical(raw),
                    None,
                    cast(ReportAvailability, row.quality),
                    row.quality_reason,
                    _canonical({"scope_type": "player_corpus"}),
                    None,
                    () if raw is None else (ReportEvidenceRef(evidence.public_id, "derived"),),
                    _canonical(
                        {
                            "result_kind": row.result_kind,
                            "sample_count": row.sample_count,
                            "missing_count": row.missing_count,
                            "analyzer": {"name": run.analyzer_name, "version": run.analyzer_version},
                        }
                    ),
                )
            )
        return tuple(output)

    @staticmethod
    def _validate_predecessor(
        item: EvidenceItem,
        replay_id: int,
        parser: ParserRun | None,
        telemetry: TelemetryRun | None,
        allowed_derived: set[int],
    ) -> None:
        if item.replay_id != replay_id:
            raise ReportContractError("cross-replay predecessor evidence is forbidden")
        if item.tier == "observed":
            parser_match = parser is not None and item.parser_run_id == parser.id and item.telemetry_run_id is None
            telemetry_match = (
                telemetry is not None and item.telemetry_run_id == telemetry.id and item.parser_run_id is None
            )
            if not (parser_match or telemetry_match):
                raise ReportContractError("predecessor evidence is outside the selected successful graph")
            return
        if item.tier == "derived" and item.id in allowed_derived:
            return
        raise ReportContractError("predecessor evidence tier is not authorized")

    @staticmethod
    def _validate_player_predecessor(
        item: EvidenceItem,
        player_authority: frozenset[int] | None,
    ) -> None:
        if player_authority is None or item.id not in player_authority:
            raise ReportContractError("predecessor evidence is not owned by the requested player")

    @staticmethod
    def _player_evidence_authority(
        session: Session,
        replay_player: ReplayPlayer | None,
        parser: ParserRun | None,
        telemetry: TelemetryRun | None,
    ) -> frozenset[int] | None:
        if replay_player is None:
            return None
        owned = (
            set(
                session.scalars(
                    select(ReplayCommand.evidence_item_id).where(
                        ReplayCommand.parser_run_id == parser.id,
                        ReplayCommand.replay_player_id == replay_player.id,
                    )
                )
            )
            if parser is not None
            else set()
        )
        owned.update(
            session.scalars(
                select(Feature.evidence_item_id).where(Feature.replay_player_id == replay_player.id)
            )
        )
        if telemetry is None:
            return frozenset(owned)
        events = tuple(
            session.scalars(
                select(TelemetryEvent)
                .where(TelemetryEvent.telemetry_run_id == telemetry.id)
                .order_by(TelemetryEvent.sequence, TelemetryEvent.id)
            )
        )
        mapped_player_index = (
            None
            if not events
            else ReportService._resolved_telemetry_player_index(session, events[0], replay_player)
        )
        has_initialization = any(event.event_type == "players_initialized" for event in events)
        event_ids = tuple(event.id for event in events)
        economy_owners: dict[int, int | None] = {}
        production_owners: dict[int, int | None] = {}
        combat_owners: dict[int, tuple[int | None, int | None]] = {}
        if event_ids:
            # TheSuperHackers @fix Leex 23/08/2026 Resolve full-match typed owners by telemetry run without exceeding SQLite bind limits. (#TBD)
            economy_owners = {
                event_id: owner_id
                for event_id, owner_id in session.execute(
                    select(EconomyEvent.telemetry_event_id, EconomyEvent.replay_player_id)
                    .join(TelemetryEvent, TelemetryEvent.id == EconomyEvent.telemetry_event_id)
                    .where(TelemetryEvent.telemetry_run_id == telemetry.id)
                )
            }
            production_owners = {
                event_id: owner_id
                for event_id, owner_id in session.execute(
                    select(ProductionEvent.telemetry_event_id, ProductionEvent.replay_player_id)
                    .join(TelemetryEvent, TelemetryEvent.id == ProductionEvent.telemetry_event_id)
                    .where(TelemetryEvent.telemetry_run_id == telemetry.id)
                )
            }
            combat_owners = {
                event_id: (attacker_id, victim_id)
                for event_id, attacker_id, victim_id in session.execute(
                    select(
                        CombatEvent.telemetry_event_id,
                        CombatEvent.attacker_replay_player_id,
                        CombatEvent.victim_replay_player_id,
                    )
                    .join(TelemetryEvent, TelemetryEvent.id == CombatEvent.telemetry_event_id)
                    .where(TelemetryEvent.telemetry_run_id == telemetry.id)
                )
            }
        for event in events:
            payload = _mapping(event.payload_json, label="telemetry event payload")
            payload_index = payload.get("player_index")
            if payload_index is None:
                owned.add(event.evidence_item_id)
                continue
            mapped = (
                mapped_player_index is not None and payload_index == mapped_player_index
                if has_initialization
                else True
            )
            economy_owner = economy_owners.get(event.id)
            production_owner = production_owners.get(event.id)
            combat_owner = combat_owners.get(event.id)
            if economy_owner is not None:
                if economy_owner == replay_player.id and mapped:
                    owned.add(event.evidence_item_id)
            elif production_owner is not None:
                if production_owner == replay_player.id and mapped:
                    owned.add(event.evidence_item_id)
            elif combat_owner is not None:
                if replay_player.id in combat_owner and mapped:
                    owned.add(event.evidence_item_id)
            elif mapped_player_index is not None:
                if payload_index == mapped_player_index:
                    owned.add(event.evidence_item_id)
            elif not has_initialization and replay_player.player_index == payload_index:
                owned.add(event.evidence_item_id)
        return frozenset(owned)

    # TheSuperHackers @bugfix Leex 23/08/2026 Reuse the accepted telemetry slot mapping for report ownership. (#TBD)
    @staticmethod
    def _resolved_telemetry_player_index(
        session: Session, event: TelemetryEvent, replay_player: ReplayPlayer
    ) -> int | None:
        telemetry = session.get(TelemetryRun, event.telemetry_run_id)
        if telemetry is None or telemetry.replay_id != replay_player.replay_id or telemetry.status != "succeeded":
            return None
        parser_run_id = _mapping(telemetry.settings_json, label="telemetry settings").get("parser_run_id")
        if type(parser_run_id) is not str or not parser_run_id:
            return None
        parser = session.scalar(
            select(ParserRun).where(
                ParserRun.id == replay_player.parser_run_id,
                ParserRun.replay_id == replay_player.replay_id,
                ParserRun.run_id == parser_run_id,
                ParserRun.status == "succeeded",
            )
        )
        if parser is None:
            return None
        players = tuple(
            session.scalars(
                select(ReplayPlayer).where(
                    ReplayPlayer.parser_run_id == parser.id,
                    ReplayPlayer.replay_id == replay_player.replay_id,
                )
            )
        )
        by_slot = {player.slot_index: player for player in players}
        if len(by_slot) != len(players):
            return None
        snapshots = tuple(
            session.scalars(
                select(TelemetryEvent).where(
                    TelemetryEvent.telemetry_run_id == telemetry.id,
                    TelemetryEvent.event_type == "players_initialized",
                )
            )
        )
        if len(snapshots) != 1:
            return None
        slots = _mapping(snapshots[0].payload_json, label="players initialized payload").get("slots")
        if type(slots) is not list:
            return None
        resolved: dict[int, int] = {}
        resolved_player_indices: set[int] = set()
        for raw_slot in slots:
            if not isinstance(raw_slot, Mapping) or raw_slot.get("resolution_status") != "resolved":
                continue
            slot_index = raw_slot.get("slot_index")
            player_index = raw_slot.get("player_index")
            if (
                type(slot_index) is not int
                or type(player_index) is not int
                or slot_index not in by_slot
                or slot_index in resolved
                or player_index in resolved_player_indices
            ):
                return None
            resolved[slot_index] = player_index
            resolved_player_indices.add(player_index)
        return resolved.get(replay_player.slot_index)

    @staticmethod
    def _ollama_values(
        session: Session,
        replay_id: int,
        replay_player: ReplayPlayer | None,
        requested: bool,
        derived: tuple[ReportValue, ...],
        observed: tuple[ReportValue, ...],
        analysis_run_id: str | None = None,
    ) -> tuple[OllamaReportStatus, tuple[ReportValue, ...], int | None]:
        if not requested:
            return OllamaReportStatus.not_requested(), (), None
        query = select(AnalysisRun).where(AnalysisRun.replay_id == replay_id)
        if replay_player is not None:
            query = query.where(AnalysisRun.replay_player_id == replay_player.id)
        else:
            query = query.where(AnalysisRun.replay_player_id.is_(None))
        if analysis_run_id is not None:
            query = query.where(AnalysisRun.run_id == analysis_run_id)
        runs = tuple(session.scalars(query))
        if analysis_run_id is not None and not runs:
            raise ReportContractError("selected analysis run was not found in the requested replay graph")
        successful = tuple(run for run in runs if run.status == "succeeded")
        if len(successful) > 1:
            raise ReportContractError("successful Task 10 analysis graph is ambiguous")
        if successful:
            run = successful[0]
        elif len(runs) == 1:
            run = runs[0]
        elif not runs:
            return (
                OllamaReportStatus(
                    True, "unavailable", None, None, None, None, None, None, ("analysis_not_found",), None
                ),
                (),
                None,
            )
        else:
            return (
                OllamaReportStatus(
                    True,
                    "unavailable",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    ("analysis_attempt_ambiguous",),
                    None,
                ),
                (),
                None,
            )
        codes = _diagnostic_codes(run.diagnostics_json)
        base = (
            run.run_id,
            run.provider,
            run.model_name,
            run.model_digest,
            run.prompt_version,
            run.response_schema_version,
        )
        if run.status != "succeeded" or run.validated_response_json is None:
            status = run.status if run.status in ("failed", "invalid", "unavailable") else "unavailable"
            return OllamaReportStatus(True, cast(OllamaStatus, status), *base, codes, None), (), run.id

        if (
            run.prompt_version != PROMPT_VERSION
            or run.prompt_digest != PROMPT_SHA256
            or run.response_schema_version != RESPONSE_SCHEMA_VERSION
            or run.response_schema_digest != RESPONSE_SCHEMA_SHA256
        ):
            return ReportService._invalid_analysis(run, base, codes, "analysis_resource_mismatch")
        selected_ids = {ref.public_id for value in (*observed, *derived) for ref in value.evidence}
        citation_bundle = _SelectedEvidenceBundle(
            tuple(_SelectedEvidenceClaim((public_id,)) for public_id in sorted(selected_ids))
        )
        try:
            # Task 10's validator intentionally consumes only bundle.claims/evidence_ids for citation authority.
            # The selected report graph above supplies exactly that structural view without recreating domain rules.
            validated = validate_response(
                cast(Mapping[str, object], run.validated_response_json),
                cast(EvidenceBundle, citation_bundle),
            )
        except ResponseValidationError as exc:
            return ReportService._invalid_analysis(run, base, codes, f"analysis_{exc.code}")
        prose = validated.document.as_plain()
        cited_ids: set[str] = set()
        for section in (
            "phase_assessments",
            "strategy_assessments",
            "comparative_observations",
            "strengths",
            "vulnerabilities",
            "uncertainty_notes",
        ):
            for claim in cast(list[dict[str, object]], prose[section]):
                cited_ids.update(cast(list[str], claim["evidence_ids"]))
        if not cited_ids.issubset(selected_ids):
            return ReportService._invalid_analysis(run, base, codes, "citation_not_selected")

        strategy_rows = tuple(
            session.scalars(
                select(StrategyAssessment)
                .where(
                    StrategyAssessment.replay_id == replay_id,
                    StrategyAssessment.analysis_run_id == run.id,
                    StrategyAssessment.method == "llm",
                )
                .order_by(StrategyAssessment.strategy_label, StrategyAssessment.public_id)
            )
        )
        claims = cast(list[dict[str, object]], prose["strategy_assessments"])
        if len(strategy_rows) != len(claims):
            return ReportService._invalid_analysis(run, base, codes, "analysis_graph_mismatch")
        rows_by_source: dict[str, tuple[StrategyAssessment, EvidenceItem]] = {}
        for row in strategy_rows:
            own = session.get(EvidenceItem, row.evidence_item_id)
            if own is None or own.source_key in rows_by_source:
                return ReportService._invalid_analysis(run, base, codes, "analysis_graph_mismatch")
            rows_by_source[own.source_key] = (row, own)
        value_by_evidence: dict[str, ReportValue] = {}
        tier_by_evidence: dict[str, ReportEvidenceTier] = {}
        for value in (*observed, *derived):
            for ref in value.evidence:
                tier_by_evidence[ref.public_id] = ref.tier
                current = value_by_evidence.get(ref.public_id)
                if (
                    current is None
                    or {"available": 2, "partial": 1, "unavailable": 0}[value.availability]
                    < {"available": 2, "partial": 1, "unavailable": 0}[current.availability]
                ):
                    value_by_evidence[ref.public_id] = value
        inferred: list[ReportValue] = []
        for claim in claims:
            claim_id = cast(str, claim["claim_id"])
            source_key = f"analysis-run:{run.run_id}:{claim_id}"
            pair = rows_by_source.get(source_key)
            if pair is None:
                return ReportService._invalid_analysis(run, base, codes, "analysis_graph_mismatch")
            row, own = pair
            evidence_ids = cast(list[str], claim["evidence_ids"])
            cited_values = [value_by_evidence[public_id] for public_id in evidence_ids]
            minimum = min(
                cited_values, key=lambda value: {"available": 2, "partial": 1, "unavailable": 0}[value.availability]
            )
            quality = minimum.availability
            reasons = tuple(
                sorted({value.unavailable_reason for value in cited_values if value.unavailable_reason is not None})
            )
            expected_details = {
                "assessment": claim["assessment"],
                "claim_id": claim_id,
                "cited_quality_reasons": list(reasons),
                "minimum_cited_quality": {"available": "complete", "partial": "partial", "unavailable": "unavailable"}[
                    quality
                ],
                "schema_version": "llm-strategy-assessment-v1",
            }
            window = cast(dict[str, int], claim["window"])
            expected_public = str(uuid5(NAMESPACE_URL, f"strategy-assessment:{source_key}"))
            expected_evidence_public = str(uuid5(NAMESPACE_URL, f"evidence:{source_key}"))
            links = tuple(
                session.execute(
                    select(AssessmentEvidence.role, EvidenceItem.public_id)
                    .join(EvidenceItem, EvidenceItem.id == AssessmentEvidence.evidence_item_id)
                    .where(AssessmentEvidence.assessment_id == row.id)
                    .order_by(EvidenceItem.public_id)
                )
            )
            if (
                own.replay_id != replay_id
                or own.tier != "inferred"
                or own.source_kind != "llm"
                or own.parser_run_id is not None
                or own.telemetry_run_id is not None
                or own.schema_version != 1
                or own.public_id != expected_evidence_public
                or row.public_id != expected_public
                or row.replay_player_id != run.replay_player_id
                or row.method != "llm"
                or row.strategy_label != claim["strategy_label"]
                or row.phase != claim["phase"]
                or row.taxonomy_version is not None
                or row.rule_version is not None
                or row.model_version != run.model_digest
                or row.frame_start != window["frame_start"]
                or row.frame_end != window["frame_end"]
                or row.quality != quality
                or row.confidence != claim["confidence"]
                or row.details_json != expected_details
                or links != tuple(("supporting", public_id) for public_id in sorted(evidence_ids))
            ):
                return ReportService._invalid_analysis(run, base, codes, "analysis_graph_mismatch")
            evidence = {ReportEvidenceRef(own.public_id, "inferred")}
            evidence.update(ReportEvidenceRef(public_id, tier_by_evidence[public_id]) for public_id in evidence_ids)
            raw: object | None = (
                None
                if quality == "unavailable"
                else {
                    "strategy_label": row.strategy_label,
                    "phase": row.phase,
                    "confidence": row.confidence,
                }
            )
            inferred.append(
                ReportValue(
                    f"strategy:{row.strategy_label}:{row.public_id}",
                    "strategy",
                    row.strategy_label,
                    _canonical(raw),
                    None,
                    quality,
                    None if quality == "available" else (reasons[0] if reasons else "cited_evidence_unavailable"),
                    _canonical({"scope_type": "player" if row.replay_player_id is not None else "replay"}),
                    (row.frame_start, row.frame_end),
                    tuple(evidence) if raw is not None else (),
                    _canonical(expected_details),
                )
            )
        return OllamaReportStatus(True, "succeeded", *base, codes, _canonical(prose)), tuple(inferred), run.id

    @staticmethod
    def _invalid_analysis(
        run: AnalysisRun,
        base: tuple[str, str, str, str, str, str],
        codes: tuple[str, ...],
        code: str,
    ) -> tuple[OllamaReportStatus, tuple[ReportValue, ...], int]:
        return (
            OllamaReportStatus(True, "invalid", *base, tuple(sorted((*codes, code))), None),
            (),
            run.id,
        )

    def _existing(
        self,
        replay_id: int,
        replay_player_id: int | None,
        analysis_run_id: int | None,
        document: ReportDocument,
        structured_bytes: bytes,
        bundle_bytes: bytes,
    ) -> Report | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(Report).where(
                    Report.replay_id == replay_id,
                    Report.report_version == document.report_version,
                    Report.input_digest == document.input_digest,
                )
            )
            if row is not None:
                self._verify_existing(
                    session,
                    row,
                    replay_player_id,
                    analysis_run_id,
                    document,
                    structured_bytes,
                    bundle_bytes,
                )
            return row

    def _verify_existing(
        self,
        session: Session,
        row: Report,
        replay_player_id: int | None,
        analysis_run_id: int | None,
        document: ReportDocument,
        structured_bytes: bytes,
        bundle_bytes: bytes,
    ) -> None:
        if row.replay_player_id != replay_player_id:
            raise ReportContractError("existing report player link drift")
        if row.analysis_run_id != analysis_run_id:
            raise ReportContractError("existing report analysis link drift")
        if (
            row.public_id != document.report_public_id
            or row.cache_key != document.cache_key
            or row.report_json != document_to_mapping(document)
        ):
            raise ReportContractError("existing report identity has version or resource drift")
        if row.structured_asset_id is None or row.rendered_asset_id is None:
            raise ReportContractError("existing report is missing exact asset links")
        structured = session.get(ManagedAsset, row.structured_asset_id)
        bundle = session.get(ManagedAsset, row.rendered_asset_id)
        if structured is None or bundle is None or structured.id == bundle.id:
            raise ReportContractError("existing report asset links are invalid")
        self._verify_asset(structured, "report_structured_json", structured_bytes)
        self._verify_asset(bundle, "report_presentation_bundle", bundle_bytes)

    def _verify_asset(self, row: ManagedAsset, kind: str, expected_bytes: bytes) -> None:
        expected_sha = hashlib.sha256(expected_bytes).hexdigest()
        expected_public_id = str(uuid5(_ASSET_NAMESPACE, f"{kind}:{expected_sha}"))
        try:
            stored = self._store.verify(expected_sha)
            relative_path = stored.path.relative_to(self._settings.data_root).as_posix()
            actual_bytes = stored.path.read_bytes()
        except (ContentStorageError, OSError, ValueError) as exc:
            raise ReportContractError("managed report asset content is unavailable") from exc
        if (
            row.public_id != expected_public_id
            or row.sha256 != expected_sha
            or row.kind != kind
            or row.relative_path != relative_path
            or row.media_type != "application/json"
            or row.size_bytes != len(expected_bytes)
            or stored.size != len(expected_bytes)
            or actual_bytes != expected_bytes
        ):
            raise ReportContractError("managed report asset identity or bytes drift")

    def _receipt_from_existing(
        self,
        row: Report,
        document: ReportDocument,
        structured_bytes: bytes,
        bundle_bytes: bytes,
        *,
        publish: bool,
    ) -> ReportReceipt:
        if not publish:
            return ReportReceipt(document, True, None, None)
        if row.structured_asset_id is None or row.rendered_asset_id is None:
            raise ReportContractError("published report is missing managed asset links")
        with self._session_factory() as session:
            structured = session.get(ManagedAsset, row.structured_asset_id)
            bundle = session.get(ManagedAsset, row.rendered_asset_id)
            if structured is None or bundle is None:
                raise ReportContractError("published report managed asset metadata is unavailable")
            self._verify_asset(structured, "report_structured_json", structured_bytes)
            self._verify_asset(bundle, "report_presentation_bundle", bundle_bytes)
            return ReportReceipt(document, True, self._asset_dto(structured), self._asset_dto(bundle))

    def _persist(
        self,
        replay_id: int,
        replay_player_id: int | None,
        analysis_run_id: int | None,
        document: ReportDocument,
        structured: StoredContent,
        bundle: StoredContent,
        structured_bytes: bytes,
        bundle_bytes: bytes,
    ) -> ReportReceipt:
        try:
            with self._session_factory() as session:
                existing = session.scalar(
                    select(Report).where(
                        Report.replay_id == replay_id,
                        Report.report_version == document.report_version,
                        Report.input_digest == document.input_digest,
                    )
                )
                if existing is not None:
                    self._verify_existing(
                        session,
                        existing,
                        replay_player_id,
                        analysis_run_id,
                        document,
                        structured_bytes,
                        bundle_bytes,
                    )
                    session.rollback()
                    return self._receipt_from_existing(existing, document, structured_bytes, bundle_bytes, publish=True)
                structured_row = self._register_asset(session, structured, "report_structured_json", "application/json")
                bundle_row = self._register_asset(session, bundle, "report_presentation_bundle", "application/json")
                row = Report(
                    public_id=document.report_public_id,
                    replay_id=replay_id,
                    replay_player_id=replay_player_id,
                    analysis_run_id=analysis_run_id,
                    report_version=document.report_version,
                    input_digest=document.input_digest,
                    cache_key=document.cache_key,
                    report_json=document_to_mapping(document),
                    structured_asset_id=structured_row.id,
                    rendered_asset_id=bundle_row.id,
                )
                session.add(row)
                session.commit()
                return ReportReceipt(
                    document,
                    False,
                    self._asset_dto(structured_row),
                    self._asset_dto(bundle_row),
                )
        except IntegrityError:
            winner = self._existing(
                replay_id,
                replay_player_id,
                analysis_run_id,
                document,
                structured_bytes,
                bundle_bytes,
            )
            if winner is None:
                raise ReportContractError("report persistence failed without a concurrent winner") from None
            return self._receipt_from_existing(winner, document, structured_bytes, bundle_bytes, publish=True)

    def _register_asset(self, session: Session, stored: StoredContent, kind: str, media_type: str) -> ManagedAsset:
        verified = self._store.verify(stored.sha256)
        expected_public_id = str(uuid5(_ASSET_NAMESPACE, f"{kind}:{verified.sha256}"))
        try:
            relative_path = verified.path.relative_to(self._settings.data_root).as_posix()
        except ValueError as exc:
            raise ReportContractError("report store is outside the configured product root") from exc
        existing = session.scalar(select(ManagedAsset).where(ManagedAsset.sha256 == verified.sha256))
        if existing is not None:
            if (
                existing.public_id != expected_public_id
                or existing.kind != kind
                or existing.relative_path != relative_path
                or existing.size_bytes != verified.size
                or existing.media_type != media_type
            ):
                raise ReportContractError("managed report asset identity drift")
            return existing
        row = ManagedAsset(
            public_id=expected_public_id,
            sha256=verified.sha256,
            kind=kind,
            relative_path=relative_path,
            size_bytes=verified.size,
            media_type=media_type,
        )
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def _asset_dto(row: ManagedAsset) -> ReportAssetDTO:
        return ReportAssetDTO(row.public_id, row.sha256, row.kind, row.size_bytes)
