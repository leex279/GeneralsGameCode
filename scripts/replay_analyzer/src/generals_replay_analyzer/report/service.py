"""Persisted-evidence report materialization and managed publication."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import (
    AnalysisRun,
    AssessmentEvidence,
    CombatEvent,
    EconomyEvent,
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


@dataclass(frozen=True)
class _SelectedEvidenceClaim:
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class _SelectedEvidenceBundle:
    """The structural citation view consumed by Task 10 domain validation."""

    claims: tuple[_SelectedEvidenceClaim, ...]


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
            telemetry = self._select_telemetry(session, replay, replay_player, parser)
            observed, parser_evidence, telemetry_evidence = self._observed_values(
                session, replay, replay_player, parser, telemetry
            )
            availability = self._availability_values(parser, telemetry, parser_evidence, telemetry_evidence)
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
                *self._feature_values(session, replay.id, replay_player, parser, telemetry),
                *self._strategy_values(session, replay.id, replay_player, parser, telemetry),
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
    ) -> TelemetryRun | None:
        feature_query = (
            select(Feature)
            .join(FeatureSet, Feature.feature_set_id == FeatureSet.id)
            .where(FeatureSet.replay_id == replay.id, FeatureSet.status == "succeeded")
        )
        if replay_player is None:
            feature_query = feature_query.where(FeatureSet.replay_player_id.is_(None))
        else:
            feature_query = feature_query.where(FeatureSet.replay_player_id == replay_player.id)
        telemetry_ids: set[int] = set()
        for feature in session.scalars(feature_query):
            own = session.get(EvidenceItem, feature.evidence_item_id)
            if own is not None and own.telemetry_run_id is not None:
                telemetry_ids.add(own.telemetry_run_id)
            for linked in session.scalars(
                select(EvidenceItem)
                .join(FeatureEvidence, FeatureEvidence.evidence_item_id == EvidenceItem.id)
                .where(FeatureEvidence.feature_id == feature.id)
            ):
                if linked.telemetry_run_id is not None:
                    telemetry_ids.add(linked.telemetry_run_id)
        if len(telemetry_ids) > 1:
            raise ReportContractError("successful feature graphs select multiple telemetry runs")
        if telemetry_ids:
            telemetry = session.get(TelemetryRun, next(iter(telemetry_ids)))
            if telemetry is None or telemetry.replay_id != replay.id or telemetry.status != "succeeded":
                raise ReportContractError("successful feature graph selects an invalid telemetry run")
            return telemetry
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
    def _observed_values(
        session: Session,
        replay: Replay,
        replay_player: ReplayPlayer | None,
        parser: ParserRun | None,
        telemetry: TelemetryRun | None,
    ) -> tuple[tuple[ReportValue, ...], tuple[ReportEvidenceRef, ...], tuple[ReportEvidenceRef, ...]]:
        values: list[ReportValue] = []
        parser_refs: list[ReportEvidenceRef] = []
        telemetry_refs: list[ReportEvidenceRef] = []
        if parser is not None:
            command_rows = session.execute(
                select(ReplayCommand, EvidenceItem)
                .join(EvidenceItem, EvidenceItem.id == ReplayCommand.evidence_item_id)
                .where(ReplayCommand.parser_run_id == parser.id, ReplayCommand.replay_id == replay.id)
                .order_by(ReplayCommand.frame, EvidenceItem.source_key, EvidenceItem.public_id)
            )
            for command, evidence in command_rows:
                if (
                    evidence.replay_id != replay.id
                    or evidence.tier != "observed"
                    or evidence.source_kind != "parser_command"
                    or evidence.parser_run_id != parser.id
                    or evidence.telemetry_run_id is not None
                ):
                    raise ReportContractError("parser command evidence does not match the selected graph")
                if replay_player is not None and command.replay_player_id != replay_player.id:
                    continue
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
            event_rows = session.execute(
                select(TelemetryEvent, EvidenceItem)
                .join(EvidenceItem, EvidenceItem.id == TelemetryEvent.evidence_item_id)
                .where(TelemetryEvent.telemetry_run_id == telemetry.id)
                .order_by(TelemetryEvent.frame, EvidenceItem.source_key, EvidenceItem.public_id)
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
                if replay_player is not None and not ReportService._telemetry_evidence_owned_by_player(
                    session, event, replay_player
                ):
                    continue
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
        return tuple(values), tuple(parser_refs), tuple(telemetry_refs)

    @staticmethod
    def _availability_values(
        parser: ParserRun | None,
        telemetry: TelemetryRun | None,
        parser_evidence: tuple[ReportEvidenceRef, ...],
        telemetry_evidence: tuple[ReportEvidenceRef, ...],
    ) -> tuple[ReportValue, ...]:
        values: list[ReportValue] = []
        for claim_id, label, selected, evidence, absent_reason, empty_reason in (
            (
                "availability:parser",
                "Parser evidence",
                parser is not None,
                parser_evidence,
                "parser_unavailable",
                "parser_observations_empty",
            ),
            (
                "availability:telemetry",
                "Telemetry evidence",
                telemetry is not None,
                telemetry_evidence,
                "telemetry_unavailable",
                "telemetry_observations_empty",
            ),
        ):
            if selected and evidence:
                values.append(
                    ReportValue(
                        claim_id,
                        "availability",
                        label,
                        True,
                        None,
                        "available",
                        None,
                        _canonical({}),
                        None,
                        evidence,
                        _canonical({}),
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
    ) -> tuple[ReportValue, ...]:
        query = (
            select(Feature)
            .join(FeatureSet, Feature.feature_set_id == FeatureSet.id)
            .where(FeatureSet.replay_id == replay_id, FeatureSet.status == "succeeded")
            .order_by(Feature.name, Feature.public_id)
        )
        if replay_player is not None:
            query = query.where(FeatureSet.replay_player_id == replay_player.id)
        else:
            query = query.where(FeatureSet.replay_player_id.is_(None))
        rows = tuple(session.scalars(query))
        output: list[ReportValue] = []
        for row in rows:
            own = session.get(EvidenceItem, row.evidence_item_id)
            feature_set = session.get(FeatureSet, row.feature_set_id)
            if (
                own is None
                or own.replay_id != replay_id
                or own.tier != "derived"
                or own.source_kind != "feature"
                or feature_set is None
                or (telemetry is not None and own.telemetry_run_id != telemetry.id)
            ):
                raise ReportContractError("successful feature is missing its derived public evidence")
            linked_ids = session.scalars(
                select(FeatureEvidence.evidence_item_id)
                .where(FeatureEvidence.feature_id == row.id)
                .order_by(FeatureEvidence.evidence_item_id)
            )
            evidence = {ReportEvidenceRef(own.public_id, "derived")}
            for evidence_id in linked_ids:
                item = session.get(EvidenceItem, evidence_id)
                if item is None:
                    raise ReportContractError("successful feature has missing predecessor evidence")
                ReportService._validate_predecessor(item, replay_id, parser, telemetry, set())
                if replay_player is not None:
                    ReportService._validate_player_predecessor(session, item, replay_player)
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
    ) -> tuple[ReportValue, ...]:
        query = (
            select(StrategyAssessment)
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
            .where(FeatureSet.replay_id == replay_id, FeatureSet.status == "succeeded")
        )
        if replay_player is None:
            feature_query = feature_query.where(FeatureSet.replay_player_id.is_(None))
        else:
            feature_query = feature_query.where(FeatureSet.replay_player_id == replay_player.id)
        allowed_derived = set(session.scalars(feature_query))
        output: list[ReportValue] = []
        for row in session.scalars(query):
            own = session.get(EvidenceItem, row.evidence_item_id)
            if (
                own is None
                or own.replay_id != replay_id
                or own.tier != "derived"
                or own.source_kind != "strategy_rule"
                or (telemetry is not None and own.telemetry_run_id != telemetry.id)
            ):
                raise ReportContractError("deterministic strategy assessment is missing derived public evidence")
            evidence = {ReportEvidenceRef(own.public_id, "derived")}
            for evidence_id in session.scalars(
                select(AssessmentEvidence.evidence_item_id)
                .where(AssessmentEvidence.assessment_id == row.id)
                .order_by(AssessmentEvidence.evidence_item_id)
            ):
                item = session.get(EvidenceItem, evidence_id)
                if item is None:
                    raise ReportContractError("deterministic assessment has missing predecessor evidence")
                ReportService._validate_predecessor(item, replay_id, parser, telemetry, allowed_derived)
                if replay_player is not None:
                    ReportService._validate_player_predecessor(session, item, replay_player)
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
    def _validate_player_predecessor(session: Session, item: EvidenceItem, replay_player: ReplayPlayer) -> None:
        if item.tier == "derived":
            feature = session.scalar(select(Feature).where(Feature.evidence_item_id == item.id))
            if feature is None or feature.replay_player_id != replay_player.id:
                raise ReportContractError("predecessor evidence is not owned by the requested player")
            return
        if item.telemetry_run_id is not None:
            event = session.scalar(select(TelemetryEvent).where(TelemetryEvent.evidence_item_id == item.id))
            if event is None or not ReportService._telemetry_evidence_owned_by_player(session, event, replay_player):
                raise ReportContractError("telemetry predecessor is not owned by the requested player")
            return
        command = session.scalar(select(ReplayCommand).where(ReplayCommand.evidence_item_id == item.id))
        if command is None or command.replay_player_id != replay_player.id:
            raise ReportContractError("parser predecessor is not owned by the requested player")

    @staticmethod
    def _telemetry_evidence_owned_by_player(
        session: Session, event: TelemetryEvent, replay_player: ReplayPlayer
    ) -> bool:
        payload = _mapping(event.payload_json, label="telemetry event payload")
        if payload.get("player_index") is None:
            return True
        if replay_player.player_index is not None:
            return payload["player_index"] == replay_player.player_index
        for model in (EconomyEvent, ProductionEvent):
            owner_id = session.scalar(select(model.replay_player_id).where(model.telemetry_event_id == event.id))
            if owner_id is not None:
                return owner_id == replay_player.id
        combat = session.scalar(select(CombatEvent).where(CombatEvent.telemetry_event_id == event.id))
        return combat is not None and replay_player.id in {
            combat.attacker_replay_player_id,
            combat.victim_replay_player_id,
        }

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
