"""Sole database boundary for deterministic feature extraction and caching."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db.models import (
    Entity,
    EvidenceItem,
    Feature,
    FeatureEvidence,
    FeatureSet,
    ManagedAsset,
    ParserRun,
    Replay,
    ReplayCommand,
    ReplayPlayer,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.features.activity import ActivityExtractor
from generals_replay_analyzer.features.base import (
    FeatureBundle,
    FeatureScope,
    FeatureValue,
    FeatureWindow,
    validate_feature_value,
)
from generals_replay_analyzer.features.build_order import BuildOrderExtractor
from generals_replay_analyzer.features.combat import CombatExtractor
from generals_replay_analyzer.features.context import FeatureContext, cache_key, input_digest
from generals_replay_analyzer.features.economy import EconomyExtractor
from generals_replay_analyzer.features.evidence import (
    CanonicalValue,
    EvidenceRef,
    ObservedEvidence,
    freeze_canonical,
    thaw_canonical,
)
from generals_replay_analyzer.features.production import ProductionExtractor
from generals_replay_analyzer.features.registry import BASE_REGISTRY, FeatureRegistry


class RegisteredExtractor(Protocol):
    name: str
    version: str
    feature_names: tuple[str, ...]

    def extract(self, context: FeatureContext) -> FeatureBundle: ...


class FeatureExtractionError(RuntimeError):
    """Typed feature request, invariant, or persistence failure."""


@dataclass(frozen=True)
class ExtractFeaturesRequest:
    replay_public_id: str
    replay_player_public_id: str | None
    extractor_names: tuple[str, ...]
    settings: CanonicalValue = ()

    def __post_init__(self) -> None:
        if not self.replay_public_id.strip():
            raise ValueError("replay_public_id must be nonempty")
        names = tuple(sorted(self.extractor_names))
        if not names or len(names) != len(set(names)):
            raise ValueError("extractor names must be nonempty and unique")
        object.__setattr__(self, "extractor_names", names)
        object.__setattr__(self, "settings", freeze_canonical(self.settings))


@dataclass(frozen=True)
class FeatureSetReceipt:
    feature_set_public_id: str
    extractor_name: str
    extractor_version: str
    input_digest: str
    cache_key: str
    cache_hit: bool
    features: tuple[FeatureValue, ...]


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid(identity: str) -> str:
    return str(uuid5(NAMESPACE_URL, identity))


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {key: item for key, item in value.items() if type(key) is str}


def _schema_label(source_kind: str, schema_version: int) -> str:
    return f"{source_kind}-v{schema_version}"


# TheSuperHackers @feature Leex 22/08/2026 Make one transactional service authoritative for feature cache persistence. (#TBD)
class FeatureExtractionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        extractors: tuple[RegisteredExtractor, ...] | None = None,
        registry: FeatureRegistry = BASE_REGISTRY,
    ) -> None:
        self._session_factory = session_factory
        configured = extractors if extractors is not None else cast(
            tuple[RegisteredExtractor, ...],
            (
                ActivityExtractor(),
                BuildOrderExtractor(),
                CombatExtractor(),
                EconomyExtractor(),
                ProductionExtractor(),
            ),
        )
        self._extractors = {extractor.name: extractor for extractor in configured}
        if len(self._extractors) != len(configured):
            raise ValueError("extractor names must be unique")
        self._registry = registry

    def extract(self, request: ExtractFeaturesRequest) -> tuple[FeatureSetReceipt, ...]:
        unknown = set(request.extractor_names) - set(self._extractors)
        if unknown:
            raise FeatureExtractionError(f"unknown extractor: {min(unknown)}")
        receipts: list[FeatureSetReceipt] = []
        for extractor_name in request.extractor_names:
            extractor = self._extractors[extractor_name]
            context = self._build_context(request)
            digest = input_digest(context)
            key = cache_key(context, extractor.name, extractor.version, registry_schema=self._registry.schema_version)
            hit = self._load_receipt(key, cache_hit=True)
            if hit is not None:
                receipts.append(hit)
                continue
            try:
                bundle = extractor.extract(context)
                values = self._validated_bundle(bundle, extractor, context)
                receipts.append(self._persist(context, extractor, digest, key, values))
            except FeatureExtractionError:
                raise
            except Exception as error:
                self._record_failure(context, extractor, digest, key, "feature_extraction_failed", str(error))
                raise FeatureExtractionError("feature extraction failed") from error
        return tuple(receipts)

    def _build_context(self, request: ExtractFeaturesRequest) -> FeatureContext:
        with self._session_factory() as session:
            replay = session.scalar(select(Replay).where(Replay.public_id == request.replay_public_id))
            if replay is None:
                raise FeatureExtractionError("unknown replay")
            replay_player: ReplayPlayer | None = None
            parser: ParserRun | None = None
            if request.replay_player_public_id is not None:
                replay_player = session.scalar(
                    select(ReplayPlayer).where(
                        ReplayPlayer.public_id == request.replay_player_public_id,
                        ReplayPlayer.replay_id == replay.id,
                    )
                )
                if replay_player is None:
                    raise FeatureExtractionError("replay player does not belong to replay")
                parser = session.scalar(
                    select(ParserRun).where(ParserRun.id == replay_player.parser_run_id, ParserRun.status == "succeeded")
                )
            else:
                parser = session.scalar(
                    select(ParserRun)
                    .where(ParserRun.replay_id == replay.id, ParserRun.status == "succeeded")
                    .order_by(ParserRun.run_id.desc())
                    .limit(1)
                )
            telemetry = session.scalar(
                select(TelemetryRun)
                .where(TelemetryRun.replay_id == replay.id, TelemetryRun.status == "succeeded")
                .order_by(TelemetryRun.run_id.desc())
                .limit(1)
            )
            scope = (
                FeatureScope("player", replay_player.public_id, replay_player.public_id)
                if replay_player is not None
                else FeatureScope("replay", replay.public_id)
            )
            observations: list[ObservedEvidence] = []
            versions: list[tuple[str, str]] = []
            if parser is not None:
                versions.append(("parser", f"{parser.schema_version}:{parser.run_id}"))
                observations.extend(self._parser_observations(session, replay, replay_player, parser))
            if telemetry is not None:
                versions.append(("telemetry", f"{telemetry.schema_version}:{telemetry.run_id}"))
                observations.extend(self._telemetry_observations(session, replay, replay_player, telemetry))
            catalog_identity = None
            if telemetry is not None and telemetry.catalog_asset_id is not None:
                catalog = session.get(ManagedAsset, telemetry.catalog_asset_id)
                catalog_identity = None if catalog is None else catalog.sha256
            return FeatureContext(
                cache_schema="feature-context-v1",
                replay_public_id=replay.public_id,
                replay_sha256=replay.sha256,
                replay_player_public_id=None if replay_player is None else replay_player.public_id,
                scope=scope,
                observation_schema_versions=tuple(versions),
                parser_completion_status=None if parser is None else parser.completion_status,
                telemetry_status=None if telemetry is None else telemetry.status,
                final_frame=None if telemetry is None else telemetry.final_frame,
                catalog_identity=catalog_identity,
                observed=tuple(observations),
                settings=request.settings,
            )

    def _parser_observations(
        self, session: Session, replay: Replay, replay_player: ReplayPlayer | None, parser: ParserRun
    ) -> tuple[ObservedEvidence, ...]:
        statement = (
            select(ReplayCommand, EvidenceItem)
            .join(EvidenceItem, EvidenceItem.id == ReplayCommand.evidence_item_id)
            .where(ReplayCommand.parser_run_id == parser.id, ReplayCommand.replay_id == replay.id)
            .order_by(ReplayCommand.frame, EvidenceItem.source_key, EvidenceItem.public_id)
        )
        observations = []
        for command, evidence in session.execute(statement):
            self._validate_observed_evidence(
                evidence,
                replay,
                parser_run_id=parser.id,
                telemetry_run_id=None,
            )
            if replay_player is not None and command.replay_player_id != replay_player.id:
                continue
            facts = {
                "arguments": command.arguments_json,
                "message_name": command.message_name,
                "message_type": command.message_type,
                "replay_player_public_id": None if replay_player is None else replay_player.public_id,
            }
            observations.append(
                ObservedEvidence(
                    EvidenceRef(
                        evidence.public_id,
                        "observed",
                        evidence.source_kind,
                        evidence.source_key,
                        _schema_label(evidence.source_kind, evidence.schema_version),
                    ),
                    command.frame,
                    "replay_command",
                    cast(CanonicalValue, facts),
                )
            )
        return tuple(observations)

    def _telemetry_observations(
        self, session: Session, replay: Replay, replay_player: ReplayPlayer | None, telemetry: TelemetryRun
    ) -> tuple[ObservedEvidence, ...]:
        players = {
            item.player_index: item.public_id
            for item in session.scalars(select(ReplayPlayer).where(ReplayPlayer.replay_id == replay.id)).all()
            if item.player_index is not None
        }
        statement = (
            select(TelemetryEvent, EvidenceItem)
            .join(EvidenceItem, EvidenceItem.id == TelemetryEvent.evidence_item_id)
            .where(TelemetryEvent.telemetry_run_id == telemetry.id)
            .order_by(TelemetryEvent.frame, EvidenceItem.source_key, EvidenceItem.public_id)
        )
        event_rows = tuple(session.execute(statement))
        for _, evidence in event_rows:
            self._validate_observed_evidence(
                evidence,
                replay,
                parser_run_id=None,
                telemetry_run_id=telemetry.id,
            )
        evidence_by_sequence = {event.sequence: evidence.public_id for event, evidence in event_rows}
        entities = {
            item.object_id: (
                item.template_name,
                players.get(item.initial_owner_player_index)
                if item.initial_owner_player_index is not None
                else None,
                evidence_by_sequence.get(item.creation_sequence) if item.creation_sequence is not None else None,
            )
            for item in session.scalars(select(Entity).where(Entity.telemetry_run_id == telemetry.id)).all()
        }
        observations = []
        for event, evidence in event_rows:
            facts = self._event_facts(event, players, entities, replay_player)
            if replay_player is not None and not self._belongs_to_player(event.event_type, facts, replay_player.public_id):
                continue
            observations.append(
                ObservedEvidence(
                    EvidenceRef(
                        evidence.public_id,
                        "observed",
                        evidence.source_kind,
                        evidence.source_key,
                        _schema_label(evidence.source_kind, evidence.schema_version),
                    ),
                    event.frame,
                    event.event_type,
                    cast(CanonicalValue, facts),
                )
            )
        return tuple(observations)

    def _validate_observed_evidence(
        self,
        evidence: EvidenceItem,
        replay: Replay,
        *,
        parser_run_id: int | None,
        telemetry_run_id: int | None,
    ) -> None:
        if (
            evidence.tier != "observed"
            or evidence.replay_id != replay.id
            or evidence.parser_run_id != parser_run_id
            or evidence.telemetry_run_id != telemetry_run_id
        ):
            raise FeatureExtractionError("malformed persisted evidence link")

    def _event_facts(
        self,
        event: TelemetryEvent,
        players: Mapping[int, str],
        entities: Mapping[int, tuple[str, str | None, str | None]],
        replay_player: ReplayPlayer | None,
    ) -> dict[str, object]:
        facts = _mapping(event.payload_json)
        event_type = event.event_type
        player_index = facts.get("player_index")
        if event_type.startswith("construction_"):
            owner = facts.get("responsible_player_index", facts.get("owner_player_index"))
            object_id = facts.get("object_id")
            entity = entities.get(object_id) if type(object_id) is int else None
            facts["replay_player_public_id"] = (
                players.get(owner) if type(owner) is int else None if entity is None else entity[1]
            )
            facts.pop("template_name", None)
            if entity is not None:
                facts["template_name"] = entity[0]
                facts["template_evidence_public_id"] = entity[2]
        elif event_type == "object_created":
            object_id = facts.get("object_id")
            entity = entities.get(object_id) if type(object_id) is int else None
            facts.pop("template_name", None)
            if entity is not None:
                facts["template_name"] = entity[0]
                facts["replay_player_public_id"] = entity[1]
        elif event_type.startswith(("production_", "upgrade_")):
            facts["replay_player_public_id"] = players.get(player_index) if type(player_index) is int else None
            if event_type.startswith("production_"):
                facts["item_kind"] = "unit"
                facts["item_name"] = facts.get("template_name")
                facts["production_identity"] = f"production:{facts.get('production_id')}"
            else:
                facts["item_kind"] = "upgrade"
                facts["item_name"] = facts.get("upgrade_name")
                facts["production_identity"] = f"upgrade:{facts.get('upgrade_queue_id', facts.get('upgrade_name'))}"
        elif event_type in ("cash_changed", "supply_collected"):
            facts["replay_player_public_id"] = players.get(player_index) if type(player_index) is int else None
        elif event_type == "damage_applied":
            source_indices = facts.get("source_player_indices")
            facts["source_replay_player_public_ids"] = [
                players[index]
                for index in cast(list[object], source_indices or [])
                if type(index) is int and index in players
            ]
            victim = facts.get("victim_player_index")
            facts["victim_replay_player_public_id"] = players.get(victim) if type(victim) is int else None
        elif event_type == "order_issued":
            source = facts.get("source_player_index")
            facts["source_replay_player_public_id"] = players.get(source) if type(source) is int else None
        elif event_type == "entity_state_changed":
            owner = facts.get("owner_player_index")
            facts["replay_player_public_id"] = players.get(owner) if type(owner) is int else None
        elif event_type == "manifest":
            exporter_settings = _mapping(facts.get("exporter_settings"))
            facts["order_coverage"] = exporter_settings.get("order_coverage")
        elif event_type == "complete" and replay_player is not None:
            facts["replay_player_public_id"] = replay_player.public_id
            balances = cast(list[object], facts.get("final_cash_balances") or [])
            for balance_value in balances:
                balance = _mapping(balance_value)
                if balance.get("player_index") == replay_player.player_index and balance.get("has_money") is True:
                    facts["final_cash_balance"] = balance.get("balance")
        elif event_type == "match_outcome" and replay_player is not None:
            winners = cast(list[object], facts.get("winner_player_indices") or [])
            losers = cast(list[object], facts.get("loser_player_indices") or [])
            result = "won" if replay_player.player_index in winners else "lost" if replay_player.player_index in losers else None
            if result is not None:
                facts["replay_player_public_id"] = replay_player.public_id
                facts["result_payload"] = {"result": result, "source": facts.get("source"), "status": facts.get("status")}
        return facts

    def _belongs_to_player(self, event_type: str, facts: Mapping[str, object], player_public_id: str) -> bool:
        if event_type in ("manifest", "complete"):
            return True
        if event_type == "damage_applied":
            sources = cast(list[object], facts.get("source_replay_player_public_ids") or [])
            return player_public_id in sources or facts.get("victim_replay_player_public_id") == player_public_id
        if event_type == "order_issued":
            return facts.get("source_replay_player_public_id") == player_public_id
        return facts.get("replay_player_public_id") == player_public_id

    def _validated_bundle(
        self, bundle: FeatureBundle, extractor: RegisteredExtractor, context: FeatureContext
    ) -> tuple[FeatureValue, ...]:
        if bundle.extractor_name != extractor.name or bundle.extractor_version != extractor.version:
            raise FeatureExtractionError("extractor bundle identity mismatch")
        if tuple(sorted(value.name for value in bundle.values)) != tuple(sorted(extractor.feature_names)):
            raise FeatureExtractionError("extractor must emit each owned feature exactly once")
        values = tuple(validate_feature_value(value, self._registry) for value in bundle.values)
        identities = {
            (value.name, value.scope.scope_type, value.scope.scope_key, value.window.frame_start, value.window.frame_end)
            for value in values
        }
        if len(identities) != len(values):
            raise FeatureExtractionError("duplicate logical feature identity")
        if any(value.scope != context.scope for value in values):
            raise FeatureExtractionError("feature scope does not match context")
        return tuple(sorted(values, key=lambda value: (value.name, value.window.frame_start, value.window.frame_end)))

    def _load_receipt(self, key: str, *, cache_hit: bool) -> FeatureSetReceipt | None:
        with self._session_factory() as session:
            feature_set = session.scalar(
                select(FeatureSet).where(FeatureSet.cache_key == key, FeatureSet.status == "succeeded")
            )
            if feature_set is None:
                return None
            values = self._receipt_values(session, feature_set)
            return FeatureSetReceipt(
                feature_set.public_id,
                feature_set.extractor_name,
                feature_set.extractor_version,
                feature_set.input_digest,
                feature_set.cache_key,
                cache_hit,
                values,
            )

    def _receipt_values(self, session: Session, feature_set: FeatureSet) -> tuple[FeatureValue, ...]:
        rows = session.scalars(
            select(Feature)
            .where(Feature.feature_set_id == feature_set.id)
            .order_by(Feature.name, Feature.scope_type, Feature.scope_key, Feature.frame_start, Feature.frame_end)
        ).all()
        values = []
        for row in rows:
            role_refs: dict[str, list[EvidenceRef]] = {"input": [], "supporting": [], "contradicting": []}
            links = session.execute(
                select(FeatureEvidence.role, EvidenceItem)
                .join(EvidenceItem, EvidenceItem.id == FeatureEvidence.evidence_item_id)
                .where(FeatureEvidence.feature_id == row.id)
                .order_by(EvidenceItem.source_kind, EvidenceItem.source_key, EvidenceItem.public_id)
            )
            for role, evidence in links:
                role_refs[role].append(
                    EvidenceRef(
                        evidence.public_id,
                        cast(object, evidence.tier),  # type: ignore[arg-type]
                        evidence.source_kind,
                        evidence.source_key,
                        _schema_label(evidence.source_kind, evidence.schema_version),
                    )
                )
            raw_value = self._row_raw_value(row)
            replay_player_public_id = None
            entity_public_id = None
            if row.replay_player_id is not None:
                replay_player = session.get(ReplayPlayer, row.replay_player_id)
                replay_player_public_id = None if replay_player is None else replay_player.public_id
            if row.entity_id is not None:
                entity = session.get(Entity, row.entity_id)
                entity_public_id = None if entity is None else entity.public_id
            scope = FeatureScope(
                cast(object, row.scope_type),  # type: ignore[arg-type]
                row.scope_key,
                replay_player_public_id,
                row.team_id,
                entity_public_id,
            )
            values.append(
                validate_feature_value(
                    FeatureValue(
                        row.name,
                        cast(object, row.value_type),  # type: ignore[arg-type]
                        raw_value,
                        row.unit,
                        scope,
                        FeatureWindow(row.frame_start, row.frame_end),
                        "complete" if row.quality == "available" else cast(object, row.quality),  # type: ignore[arg-type]
                        row.quality_reason,
                        tuple(role_refs["input"]),
                        tuple(role_refs["supporting"]),
                        tuple(role_refs["contradicting"]),
                        cast(CanonicalValue, row.details_json),
                    ),
                    self._registry,
                )
            )
        return tuple(values)

    def _row_raw_value(self, row: Feature) -> CanonicalValue:
        return cast(CanonicalValue, {
            "integer": row.integer_value,
            "real": row.real_value,
            "text": row.text_value,
            "boolean": row.boolean_value,
            "json": row.json_value,
        }[row.value_type])

    def _persist(
        self,
        context: FeatureContext,
        extractor: RegisteredExtractor,
        digest: str,
        key: str,
        values: tuple[FeatureValue, ...],
    ) -> FeatureSetReceipt:
        session = self._session_factory()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            winner = session.scalar(select(FeatureSet).where(FeatureSet.cache_key == key, FeatureSet.status == "succeeded"))
            if winner is not None:
                receipt = FeatureSetReceipt(
                    winner.public_id,
                    winner.extractor_name,
                    winner.extractor_version,
                    winner.input_digest,
                    winner.cache_key,
                    True,
                    self._receipt_values(session, winner),
                )
                session.commit()
                return receipt
            replay = session.scalar(select(Replay).where(Replay.public_id == context.replay_public_id))
            if replay is None:
                raise FeatureExtractionError("replay disappeared before persistence")
            replay_player = None
            if context.replay_player_public_id is not None:
                replay_player = session.scalar(
                    select(ReplayPlayer).where(
                        ReplayPlayer.public_id == context.replay_player_public_id,
                        ReplayPlayer.replay_id == replay.id,
                    )
                )
                if replay_player is None:
                    raise FeatureExtractionError("replay player disappeared before persistence")
            feature_set = FeatureSet(
                public_id=_uuid(f"feature-set:{key}"),
                replay_id=replay.id,
                replay_player_id=None if replay_player is None else replay_player.id,
                extractor_name=extractor.name,
                extractor_version=extractor.version,
                input_digest=digest,
                cache_key=key,
                status="pending",
                settings_json=thaw_canonical(context.settings),
                created_at=_now(),
            )
            session.add(feature_set)
            session.flush()
            self._persist_values(
                session,
                replay,
                replay_player,
                feature_set,
                values,
                {item.ref.public_id: item.ref for item in context.observed},
            )
            feature_set.status = "succeeded"
            feature_set.completed_at = _now()
            session.commit()
            return FeatureSetReceipt(
                feature_set.public_id,
                extractor.name,
                extractor.version,
                digest,
                key,
                False,
                values,
            )
        except FeatureExtractionError:
            session.rollback()
            raise
        except (IntegrityError, OperationalError, ValueError, TypeError) as error:
            session.rollback()
            self._record_failure(context, extractor, digest, key, "feature_persistence_failed", str(error))
            raise FeatureExtractionError("feature persistence failed") from error
        finally:
            session.close()

    def _persist_values(
        self,
        session: Session,
        replay: Replay,
        replay_player: ReplayPlayer | None,
        feature_set: FeatureSet,
        values: tuple[FeatureValue, ...],
        authorized_evidence: Mapping[str, EvidenceRef],
    ) -> None:
        refs = {
            ref.public_id: ref
            for value in values
            for ref in value.input_evidence + value.supporting_evidence + value.contradicting_evidence
        }
        if any(authorized_evidence.get(public_id) != ref for public_id, ref in refs.items()):
            raise ValueError("feature evidence is not authorized by exact feature context")
        evidence_rows = {
            item.public_id: item
            for item in session.scalars(select(EvidenceItem).where(EvidenceItem.public_id.in_(tuple(refs)))).all()
        }
        if set(evidence_rows) != set(refs):
            raise ValueError("feature evidence reference does not exist")
        for public_id, ref in refs.items():
            row = evidence_rows[public_id]
            if row.replay_id != replay.id:
                raise ValueError("cross-replay feature evidence is forbidden")
            if (
                row.tier != ref.tier
                or row.source_kind != ref.source_kind
                or row.source_key != ref.source_key
                or _schema_label(row.source_kind, row.schema_version) != ref.schema_version
            ):
                raise ValueError("feature evidence identity mismatch")
        for value in values:
            source_key = (
                f"feature:{feature_set.public_id}:{value.name}:{value.scope.scope_type}:{value.scope.scope_key}:"
                f"{value.window.frame_start}:{value.window.frame_end}"
            )
            derived = EvidenceItem(
                public_id=_uuid(f"evidence:{source_key}"),
                replay_id=replay.id,
                tier="derived",
                source_kind="feature",
                source_key=source_key,
                schema_version=1,
                created_at=_now(),
            )
            session.add(derived)
            session.flush()
            typed = self._typed_columns(value)
            feature = Feature(
                public_id=_uuid(f"feature-value:{source_key}"),
                feature_set_id=feature_set.id,
                evidence_item_id=derived.id,
                name=value.name,
                value_type=value.value_type,
                unit=value.unit,
                scope_type=value.scope.scope_type,
                scope_key=value.scope.scope_key,
                replay_player_id=None if replay_player is None else replay_player.id,
                team_id=value.scope.team_id,
                frame_start=value.window.frame_start,
                frame_end=value.window.frame_end,
                quality="available" if value.quality == "complete" else value.quality,
                quality_reason=value.quality_reason,
                details_json=thaw_canonical(value.details),
                **typed,
            )
            session.add(feature)
            session.flush()
            for role, role_refs in (
                ("input", value.input_evidence),
                ("supporting", value.supporting_evidence),
                ("contradicting", value.contradicting_evidence),
            ):
                for ref in role_refs:
                    session.add(FeatureEvidence(feature_id=feature.id, evidence_item_id=evidence_rows[ref.public_id].id, role=role))

    def _typed_columns(self, value: FeatureValue) -> dict[str, object]:
        columns: dict[str, object] = {
            "integer_value": None,
            "real_value": None,
            "text_value": None,
            "boolean_value": None,
            "json_value": None,
        }
        if value.quality == "unavailable":
            return columns
        key = {
            "integer": "integer_value",
            "real": "real_value",
            "text": "text_value",
            "boolean": "boolean_value",
            "json": "json_value",
        }[value.value_type]
        columns[key] = thaw_canonical(value.raw_value)
        return columns

    def _record_failure(
        self,
        context: FeatureContext,
        extractor: RegisteredExtractor,
        digest: str,
        key: str,
        code: str,
        message: str,
    ) -> None:
        session = self._session_factory()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            replay = session.scalar(select(Replay).where(Replay.public_id == context.replay_public_id))
            if replay is None:
                session.rollback()
                return
            replay_player_id = None
            if context.replay_player_public_id is not None:
                replay_player_id = session.scalar(
                    select(ReplayPlayer.id).where(
                        ReplayPlayer.public_id == context.replay_player_public_id,
                        ReplayPlayer.replay_id == replay.id,
                    )
                )
            session.add(
                FeatureSet(
                    public_id=str(uuid4()),
                    replay_id=replay.id,
                    replay_player_id=replay_player_id,
                    extractor_name=extractor.name,
                    extractor_version=extractor.version,
                    input_digest=digest,
                    cache_key=key,
                    status="failed",
                    settings_json=thaw_canonical(context.settings),
                    completed_at=_now(),
                    error_json={"code": code, "message": message},
                    created_at=_now(),
                )
            )
            session.commit()
        except (IntegrityError, OperationalError):
            session.rollback()
        finally:
            session.close()
