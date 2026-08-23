"""Narrow database adapter for immutable deterministic rule assessments."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db.models import (
    AssessmentEvidence,
    Entity,
    EvidenceItem,
    Feature,
    FeatureEvidence,
    FeatureSet,
    ManagedAsset,
    ParserRun,
    Replay,
    ReplayPlayer,
    StrategyAssessment,
    TelemetryEvent,
    TelemetryRun,
)
from generals_replay_analyzer.features.base import (
    FeatureScope,
    FeatureValue,
    FeatureWindow,
    validate_feature_value,
)
from generals_replay_analyzer.features.evidence import (
    CanonicalValue,
    DerivedEvidence,
    EvidenceRef,
    ObservedEvidence,
    freeze_canonical,
    thaw_canonical,
)
from generals_replay_analyzer.features.registry import FeatureRegistry
from generals_replay_analyzer.strategy.candidates import (
    assess_candidates,
    strategy_cache_identity,
    strategy_definition_digests,
)
from generals_replay_analyzer.strategy.rules import CatalogProof, RuleAssessment, StrategyContext, StrategyFeature
from generals_replay_analyzer.strategy.taxonomy import StrategyTaxonomy, default_taxonomy, load_taxonomy


def _catalog_schema() -> object:
    filename = "game-data-catalog-v1.schema.json"
    packaged = files("generals_replay_analyzer").joinpath("data", filename)
    source = Path(__file__).resolve().parents[3] / "contracts" / filename
    resource = packaged if packaged.is_file() else source
    return json.loads(resource.read_text(encoding="utf-8"))


_CATALOG_SCHEMA = _catalog_schema()
_CATALOG_VALIDATOR = Draft202012Validator(_CATALOG_SCHEMA)


@dataclass(frozen=True)
class StrategyAssessmentReceipt:
    cache_key: str
    taxonomy_version: str
    taxonomy_sha256: str
    assessments: tuple[RuleAssessment, ...]


def _uuid(identity: str) -> str:
    return str(uuid5(NAMESPACE_URL, identity))


def _schema_label(row: EvidenceItem) -> str:
    return f"{row.source_kind}-v{row.schema_version}"


def _evidence_ref(row: EvidenceItem) -> EvidenceRef:
    return EvidenceRef(
        row.public_id,
        cast(object, row.tier),  # type: ignore[arg-type]
        row.source_kind,
        row.source_key,
        _schema_label(row),
    )


def _raw_value(row: Feature) -> CanonicalValue:
    return cast(
        CanonicalValue,
        {
            "integer": row.integer_value,
            "real": row.real_value,
            "text": row.text_value,
            "boolean": row.boolean_value,
            "json": row.json_value,
        }[row.value_type],
    )


def _assessment_source_key(
    replay_public_id: str,
    replay_player_public_id: str | None,
    cache_key: str,
    assessment: RuleAssessment,
) -> str:
    player = replay_player_public_id or "replay"
    return (
        f"strategy-rule:{replay_public_id}:{player}:{cache_key}:{assessment.strategy_id}:"
        f"{assessment.phase}:{assessment.window.frame_start}:{assessment.window.frame_end}"
    )


def _details_mapping(details: CanonicalValue) -> dict[str, object]:
    value = thaw_canonical(details)
    if not isinstance(value, dict):
        raise TypeError("assessment details must be a canonical object")
    return cast(dict[str, object], value)


def _mapping(value: object) -> dict[str, object] | None:
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _reject_nonstandard_constant(value: str) -> object:
    raise ValueError(f"non-standard numeric constant {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"nonfinite number {value}")
    return parsed


# TheSuperHackers @feature Leex 23/08/2026 Reverify managed catalog semantics before strategy use. (#TBD)
def _catalog_document(data_root: Path, asset: ManagedAsset) -> dict[str, object] | None:
    if asset.kind != "telemetry_catalog" or asset.media_type != "application/json":
        return None
    candidate = data_root.joinpath(*asset.relative_path.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
        payload = resolved.read_bytes()
    except OSError:
        return None
    if resolved != candidate or data_root not in resolved.parents:
        return None
    if len(payload) != asset.size_bytes or hashlib.sha256(payload).hexdigest() != asset.sha256:
        return None
    try:
        decoded = json.loads(
            payload,
            parse_constant=_reject_nonstandard_constant,
            parse_float=_parse_finite_float,
        )
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(decoded, dict) or next(_CATALOG_VALIDATOR.iter_errors(decoded), None) is not None:
        return None
    return cast(dict[str, object], decoded)


# TheSuperHackers @feature Leex 22/08/2026 Persist deterministic strategy results through one short immutable transaction. (#TBD)
class StrategyAssessmentService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        data_root: Path | None = None,
        taxonomy_resource: Traversable | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._data_root = None if data_root is None else data_root.resolve(strict=True)
        self._taxonomy_resource = taxonomy_resource

    def assess_rule_candidates(
        self,
        replay_public_id: str,
        replay_player_public_id: str | None,
        feature_set_public_ids: tuple[str, ...],
        registry: FeatureRegistry,
    ) -> StrategyAssessmentReceipt:
        self._validate_feature_set_selection(feature_set_public_ids)
        taxonomy = (
            default_taxonomy(registry)
            if self._taxonomy_resource is None
            else load_taxonomy(self._taxonomy_resource, registry)
        )
        context = self._build_context(
            replay_public_id,
            replay_player_public_id,
            feature_set_public_ids,
            registry,
        )
        input_digest, cache_key = strategy_cache_identity(context, taxonomy, registry)
        evaluated = assess_candidates(context, taxonomy, registry)
        assessments = self._with_persistence_identity(
            evaluated,
            input_digest,
            cache_key,
            feature_set_public_ids,
            taxonomy,
            registry,
        )
        cached = self._load_receipt(
            context,
            feature_set_public_ids,
            taxonomy,
            registry,
            input_digest,
            cache_key,
            assessments,
        )
        if cached is not None:
            return cached
        return self._persist(
            context,
            feature_set_public_ids,
            taxonomy,
            registry,
            input_digest,
            cache_key,
            assessments,
        )

    @staticmethod
    def _validate_feature_set_selection(feature_set_public_ids: tuple[str, ...]) -> None:
        if type(feature_set_public_ids) is not tuple or not feature_set_public_ids:
            raise ValueError("feature_set_public_ids must be a nonempty tuple")
        if any(type(public_id) is not str or not public_id.strip() for public_id in feature_set_public_ids):
            raise ValueError("feature set public IDs must be nonempty strings")
        if len(feature_set_public_ids) != len(set(feature_set_public_ids)):
            raise ValueError("feature set public IDs must be unique")
        if feature_set_public_ids != tuple(sorted(feature_set_public_ids)):
            raise ValueError("feature set public IDs must be sorted")

    @staticmethod
    def _telemetry_owner(session: Session, feature_set_ids: tuple[int, ...]) -> int | None:
        owners = tuple(
            value
            for value in session.scalars(
                select(EvidenceItem.telemetry_run_id)
                .join(FeatureEvidence, FeatureEvidence.evidence_item_id == EvidenceItem.id)
                .join(Feature, Feature.id == FeatureEvidence.feature_id)
                .where(
                    Feature.feature_set_id.in_(feature_set_ids),
                    EvidenceItem.telemetry_run_id.is_not(None),
                )
                .distinct()
            ).all()
            if value is not None
        )
        if len(owners) > 1:
            raise ValueError("strategy features have mixed authoritative telemetry owners")
        return owners[0] if owners else None

    # TheSuperHackers @feature Leex 23/08/2026 Bind named strategies to imported faction, map, and catalog proof. (#TBD)
    def _applicability_context(
        self,
        session: Session,
        replay: Replay,
        replay_player: ReplayPlayer | None,
        telemetry_run_id: int | None,
    ) -> tuple[
        str | None,
        EvidenceRef | None,
        str | None,
        EvidenceRef | None,
        str | None,
        EvidenceRef | None,
        CatalogProof | None,
    ]:
        unavailable = (None, None, None, None, None, None, None)
        if self._data_root is None or replay_player is None or telemetry_run_id is None:
            return unavailable
        telemetry = session.get(TelemetryRun, telemetry_run_id)
        if telemetry is None or telemetry.replay_id != replay.id or telemetry.status != "succeeded":
            return unavailable
        rows = tuple(
            session.execute(
                select(TelemetryEvent, EvidenceItem)
                .join(EvidenceItem, EvidenceItem.id == TelemetryEvent.evidence_item_id)
                .where(
                    TelemetryEvent.telemetry_run_id == telemetry.id,
                    TelemetryEvent.event_type.in_(("manifest", "players_initialized")),
                )
                .order_by(TelemetryEvent.sequence, EvidenceItem.public_id)
            ).all()
        )
        manifests = tuple(row for row in rows if row[0].event_type == "manifest")
        player_snapshots = tuple(row for row in rows if row[0].event_type == "players_initialized")
        if len(manifests) != 1 or len(player_snapshots) != 1:
            return unavailable
        manifest, manifest_evidence = manifests[0]
        players, players_evidence = player_snapshots[0]
        manifest_payload = _mapping(manifest.payload_json)
        players_payload = _mapping(players.payload_json)
        if manifest_payload is None or players_payload is None:
            return unavailable
        slots = players_payload.get("slots")
        if not isinstance(slots, list):
            return unavailable
        resolved_slots = tuple(
            cast(dict[str, object], slot)
            for slot in slots
            if isinstance(slot, dict)
            and slot.get("occupied") is True
            and slot.get("resolution_status") == "resolved"
            and type(slot.get("faction_template_name")) is str
        )
        selected_slots = tuple(slot for slot in resolved_slots if slot.get("slot_index") == replay_player.slot_index)
        opponent_slots = tuple(slot for slot in resolved_slots if slot.get("slot_index") != replay_player.slot_index)
        if len(selected_slots) != 1 or len(opponent_slots) != 1:
            return unavailable
        player_faction = cast(str, selected_slots[0]["faction_template_name"])
        opponent_faction = cast(str, opponent_slots[0]["faction_template_name"])
        map_identity = manifest_payload.get("map_identity")
        catalog_reference = _mapping(manifest_payload.get("game_data_catalog"))
        asset = None if telemetry.catalog_asset_id is None else session.get(ManagedAsset, telemetry.catalog_asset_id)
        if type(map_identity) is not str or catalog_reference is None or asset is None:
            return unavailable
        if (
            catalog_reference.get("sha256") != asset.sha256
            or catalog_reference.get("engine_data_identity") != telemetry.engine_build
        ):
            return unavailable
        catalog = _catalog_document(self._data_root, asset)
        if catalog is None or catalog.get("engine_data_identity") != telemetry.engine_build:
            return unavailable
        entries = catalog.get("thing_templates")
        if not isinstance(entries, list):
            return unavailable
        factions = tuple(
            (cast(str, entry["name"]), cast(str | None, entry["faction"]))
            for entry in entries
            if isinstance(entry, dict) and type(entry.get("name")) is str
        )
        categories = tuple(
            (cast(str, entry["name"]), tuple(cast(list[str], entry["category_tags"])))
            for entry in entries
            if isinstance(entry, dict)
            and type(entry.get("name")) is str
            and isinstance(entry.get("category_tags"), list)
        )
        manifest_ref = _evidence_ref(manifest_evidence)
        players_ref = _evidence_ref(players_evidence)
        proof = CatalogProof(
            catalog_identity=asset.sha256,
            evidence=ObservedEvidence(
                manifest_ref,
                manifest.frame,
                "catalog_manifest",
                freeze_canonical({"catalog_identity": asset.sha256}),
            ),
            factions_by_template=factions,
            category_tags_by_template=categories,
        )
        return (
            player_faction,
            players_ref,
            opponent_faction,
            players_ref,
            map_identity,
            manifest_ref,
            proof,
        )

    def _build_context(
        self,
        replay_public_id: str,
        replay_player_public_id: str | None,
        feature_set_public_ids: tuple[str, ...],
        registry: FeatureRegistry,
    ) -> StrategyContext:
        with self._session_factory() as session:
            replay = session.scalar(select(Replay).where(Replay.public_id == replay_public_id))
            if replay is None:
                raise LookupError("unknown replay public ID")
            replay_player = None
            if replay_player_public_id is not None:
                replay_player = session.scalar(
                    select(ReplayPlayer).where(
                        ReplayPlayer.public_id == replay_player_public_id,
                        ReplayPlayer.replay_id == replay.id,
                    )
                )
                if replay_player is None:
                    raise ValueError("requested replay player does not belong to replay")
            selected = tuple(
                session.scalars(
                    select(FeatureSet)
                    .where(FeatureSet.public_id.in_(feature_set_public_ids))
                    .order_by(FeatureSet.public_id)
                ).all()
            )
            if tuple(item.public_id for item in selected) != feature_set_public_ids:
                raise LookupError("unknown selected feature set public ID")
            if any(item.status != "succeeded" or item.completed_at is None for item in selected):
                raise ValueError("selected feature sets must be completed and succeeded")
            if any(item.replay_id != replay.id for item in selected):
                raise ValueError("selected feature set belongs to another replay")
            expected_player_id = None if replay_player is None else replay_player.id
            if any(item.replay_player_id != expected_player_id for item in selected):
                raise ValueError("selected feature set player scope does not match request")

            features: list[StrategyFeature] = []
            settings: list[dict[str, object]] = []
            for feature_set in selected:
                settings.append(
                    {
                        "cache_key": feature_set.cache_key,
                        "extractor_name": feature_set.extractor_name,
                        "extractor_version": feature_set.extractor_version,
                        "input_digest": feature_set.input_digest,
                        "public_id": feature_set.public_id,
                        "settings": feature_set.settings_json,
                    }
                )
                rows = tuple(
                    session.scalars(
                        select(Feature)
                        .where(Feature.feature_set_id == feature_set.id)
                        .order_by(
                            Feature.name,
                            Feature.scope_type,
                            Feature.scope_key,
                            Feature.frame_start,
                            Feature.frame_end,
                            Feature.public_id,
                        )
                    ).all()
                )
                features.extend(self._strategy_feature(session, replay, feature_set, row, registry) for row in rows)
            scopes = {item.value.scope for item in features}
            if len(scopes) > 1:
                raise ValueError("selected features must have one exact assessment scope")
            scope = (
                next(iter(scopes))
                if scopes
                else FeatureScope("replay", replay.public_id)
                if replay_player is None
                else FeatureScope("player", replay_player.public_id, replay_player.public_id)
            )
            if replay_player is not None and scope != FeatureScope(
                "player", replay_player.public_id, replay_player.public_id
            ):
                raise ValueError("selected feature scope does not match requested player")
            window = FeatureWindow(0, replay.frame_count)
            final_frame = replay.frame_count if replay.lifecycle_state == "engine_verified" else None
            context_settings = {
                "feature_set_public_ids": list(feature_set_public_ids),
                "feature_sets": settings,
            }
            telemetry_owner = self._telemetry_owner(session, tuple(item.id for item in selected))
            (
                player_faction,
                player_faction_evidence,
                opponent_faction,
                opponent_faction_evidence,
                map_identity,
                map_identity_evidence,
                catalog,
            ) = self._applicability_context(session, replay, replay_player, telemetry_owner)
            return StrategyContext(
                replay_public_id=replay.public_id,
                replay_sha256=replay.sha256,
                replay_player_public_id=None if replay_player is None else replay_player.public_id,
                scope=scope,
                window=window,
                final_frame=final_frame,
                player_faction_template_name=player_faction,
                player_faction_evidence=player_faction_evidence,
                opponent_faction_template_name=opponent_faction,
                opponent_faction_evidence=opponent_faction_evidence,
                map_identity=map_identity,
                map_identity_evidence=map_identity_evidence,
                catalog=catalog,
                features=tuple(features),
                settings=freeze_canonical(context_settings),
            )

    def _strategy_feature(
        self,
        session: Session,
        replay: Replay,
        feature_set: FeatureSet,
        row: Feature,
        registry: FeatureRegistry,
    ) -> StrategyFeature:
        derived_row = session.get(EvidenceItem, row.evidence_item_id)
        if derived_row is None or derived_row.tier != "derived":
            raise ValueError("feature evidence must be derived")
        if derived_row.replay_id != replay.id:
            raise ValueError("cross-replay feature evidence is forbidden")
        links = tuple(
            session.execute(
                select(FeatureEvidence.role, EvidenceItem)
                .join(EvidenceItem, EvidenceItem.id == FeatureEvidence.evidence_item_id)
                .where(FeatureEvidence.feature_id == row.id)
                .order_by(
                    EvidenceItem.source_kind, EvidenceItem.source_key, EvidenceItem.public_id, FeatureEvidence.role
                )
            ).all()
        )
        by_role: dict[str, list[EvidenceRef]] = {"input": [], "supporting": [], "contradicting": []}
        for role, evidence in links:
            if evidence.tier != "observed":
                raise ValueError("feature input evidence must be observed")
            if evidence.replay_id != replay.id:
                raise ValueError("cross-replay feature input evidence is forbidden")
            by_role[role].append(_evidence_ref(evidence))
        if row.quality != "unavailable" and not by_role["input"]:
            raise ValueError("feature requires direct observed input evidence")
        if {item.public_id for item in by_role["supporting"]} & {item.public_id for item in by_role["contradicting"]}:
            raise ValueError("feature evidence cannot be both supporting and contradicting")
        scope = self._feature_scope(session, row)
        value = validate_feature_value(
            FeatureValue(
                row.name,
                cast(object, row.value_type),  # type: ignore[arg-type]
                _raw_value(row),
                row.unit,
                scope,
                FeatureWindow(row.frame_start, row.frame_end),
                "complete" if row.quality == "available" else cast(object, row.quality),  # type: ignore[arg-type]
                row.quality_reason,
                tuple(by_role["input"]),
                tuple(by_role["supporting"]),
                tuple(by_role["contradicting"]),
                cast(CanonicalValue, row.details_json),
            ),
            registry,
        )
        derived = DerivedEvidence(
            _evidence_ref(derived_row),
            feature_set.extractor_name,
            feature_set.extractor_version,
            tuple(item.public_id for item in value.input_evidence),
        )
        return StrategyFeature(value, derived)

    @staticmethod
    def _feature_scope(session: Session, row: Feature) -> FeatureScope:
        replay_player_public_id = None
        entity_public_id = None
        if row.replay_player_id is not None:
            player = session.get(ReplayPlayer, row.replay_player_id)
            if player is None:
                raise ValueError("feature replay player is unavailable")
            replay_player_public_id = player.public_id
        if row.entity_id is not None:
            entity = session.get(Entity, row.entity_id)
            if entity is None:
                raise ValueError("feature entity is unavailable")
            entity_public_id = entity.public_id
        return FeatureScope(
            cast(object, row.scope_type),  # type: ignore[arg-type]
            row.scope_key,
            replay_player_public_id,
            row.team_id,
            entity_public_id,
        )

    @staticmethod
    def _with_persistence_identity(
        assessments: tuple[RuleAssessment, ...],
        input_digest: str,
        cache_key: str,
        feature_set_public_ids: tuple[str, ...],
        taxonomy: StrategyTaxonomy,
        registry: FeatureRegistry,
    ) -> tuple[RuleAssessment, ...]:
        registry_content_sha256, taxonomy_rules_sha256 = strategy_definition_digests(taxonomy, registry)
        result: list[RuleAssessment] = []
        for assessment in assessments:
            details = _details_mapping(assessment.details)
            details["cache_identity"] = {
                "cache_key": cache_key,
                "evaluator_name": "strategy-rule",
                "evaluator_version": "strategy-rule-v1",
                "feature_set_public_ids": list(feature_set_public_ids),
                "formula_version": "strategy-rule-score-v1",
                "input_digest": input_digest,
                "registry_content_sha256": registry_content_sha256,
                "registry_schema": registry.schema_version,
                "taxonomy_sha256": taxonomy.content_sha256,
                "taxonomy_rules_sha256": taxonomy_rules_sha256,
                "taxonomy_version": taxonomy.taxonomy_version,
            }
            result.append(
                RuleAssessment(
                    assessment.strategy_id,
                    assessment.phase,
                    assessment.window,
                    assessment.quality,
                    assessment.rule_score,
                    assessment.supporting_evidence,
                    assessment.contradicting_evidence,
                    cast(CanonicalValue, details),
                )
            )
        return tuple(result)

    def _load_receipt(
        self,
        context: StrategyContext,
        feature_set_public_ids: tuple[str, ...],
        taxonomy: StrategyTaxonomy,
        registry: FeatureRegistry,
        input_digest: str,
        cache_key: str,
        expected: tuple[RuleAssessment, ...],
    ) -> StrategyAssessmentReceipt | None:
        with self._session_factory() as session:
            return self._load_receipt_in_session(
                session,
                context,
                feature_set_public_ids,
                taxonomy,
                registry,
                input_digest,
                cache_key,
                expected,
            )

    def _load_receipt_in_session(
        self,
        session: Session,
        context: StrategyContext,
        feature_set_public_ids: tuple[str, ...],
        taxonomy: StrategyTaxonomy,
        registry: FeatureRegistry,
        input_digest: str,
        cache_key: str,
        expected: tuple[RuleAssessment, ...],
    ) -> StrategyAssessmentReceipt | None:
        replay = session.scalar(select(Replay).where(Replay.public_id == context.replay_public_id))
        if replay is None:
            raise ValueError("strategy cache replay ownership is unavailable")
        replay_player_id = None
        if context.replay_player_public_id is not None:
            replay_player = session.scalar(
                select(ReplayPlayer).where(
                    ReplayPlayer.public_id == context.replay_player_public_id,
                    ReplayPlayer.replay_id == replay.id,
                )
            )
            if replay_player is None:
                raise ValueError("strategy cache player ownership is unavailable")
            replay_player_id = replay_player.id
        source_keys = tuple(
            _assessment_source_key(context.replay_public_id, context.replay_player_public_id, cache_key, item)
            for item in expected
        )
        evidence_rows = tuple(
            session.scalars(
                select(EvidenceItem)
                .where(EvidenceItem.source_kind == "strategy_rule", EvidenceItem.source_key.in_(source_keys))
                .order_by(EvidenceItem.source_key)
            ).all()
        )
        if not evidence_rows:
            return None
        if len(evidence_rows) != len(source_keys) or {item.source_key for item in evidence_rows} != set(source_keys):
            raise ValueError("partial strategy cache graph exists")
        by_source = {item.source_key: item for item in evidence_rows}
        loaded: list[RuleAssessment] = []
        for expected_assessment, source_key in zip(expected, source_keys, strict=True):
            evidence = by_source[source_key]
            if evidence.tier != "derived" or evidence.replay_id != replay.id or evidence.schema_version != 1:
                raise ValueError("strategy cache result evidence tier or replay ownership mismatch")
            row = session.scalar(
                select(StrategyAssessment).where(
                    StrategyAssessment.evidence_item_id == evidence.id,
                    StrategyAssessment.method == "rule",
                )
            )
            if row is None:
                raise ValueError("strategy cache evidence has no rule assessment")
            expected_details = _details_mapping(expected_assessment.details)
            if (
                row.replay_id != replay.id
                or row.replay_player_id != replay_player_id
                or row.taxonomy_version != taxonomy.taxonomy_version
                or row.rule_version != self._rule_version(taxonomy, row.strategy_label)
                or row.model_version is not None
                or row.analysis_run_id is not None
                or row.frame_start != expected_assessment.window.frame_start
                or row.frame_end != expected_assessment.window.frame_end
                or row.quality != expected_assessment.quality
                or row.confidence != expected_assessment.rule_score
                or row.details_json != expected_details
            ):
                raise ValueError("strategy cache identity details do not match exact request")
            supporting, contradicting = self._assessment_links(session, row, replay.id)
            loaded.append(
                RuleAssessment(
                    row.strategy_label,
                    cast(object, row.phase),  # type: ignore[arg-type]
                    FeatureWindow(row.frame_start, row.frame_end),
                    cast(object, row.quality),  # type: ignore[arg-type]
                    row.confidence,
                    supporting,
                    contradicting,
                    freeze_canonical(row.details_json),
                )
            )
        receipt = StrategyAssessmentReceipt(
            cache_key, taxonomy.taxonomy_version, taxonomy.content_sha256, tuple(loaded)
        )
        if receipt.assessments != expected:
            raise ValueError("strategy cache evidence graph does not match exact request")
        return receipt

    @staticmethod
    def _assessment_links(
        session: Session, row: StrategyAssessment, replay_id: int
    ) -> tuple[tuple[EvidenceRef, ...], tuple[EvidenceRef, ...]]:
        links = tuple(
            session.execute(
                select(AssessmentEvidence.role, EvidenceItem)
                .join(EvidenceItem, EvidenceItem.id == AssessmentEvidence.evidence_item_id)
                .where(AssessmentEvidence.assessment_id == row.id)
                .order_by(EvidenceItem.source_kind, EvidenceItem.source_key, EvidenceItem.public_id)
            ).all()
        )
        if any(
            evidence.replay_id != replay_id or evidence.tier not in ("observed", "derived") for _, evidence in links
        ):
            raise ValueError("strategy cache linked evidence tier or replay ownership mismatch")
        supporting = tuple(_evidence_ref(evidence) for role, evidence in links if role == "supporting")
        contradicting = tuple(_evidence_ref(evidence) for role, evidence in links if role == "contradicting")
        return supporting, contradicting

    def _persist(
        self,
        context: StrategyContext,
        feature_set_public_ids: tuple[str, ...],
        taxonomy: StrategyTaxonomy,
        registry: FeatureRegistry,
        input_digest: str,
        cache_key: str,
        assessments: tuple[RuleAssessment, ...],
    ) -> StrategyAssessmentReceipt:
        session = self._session_factory()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            winner = self._load_receipt_in_session(
                session,
                context,
                feature_set_public_ids,
                taxonomy,
                registry,
                input_digest,
                cache_key,
                assessments,
            )
            if winner is not None:
                session.commit()
                return winner
            replay = session.scalar(select(Replay).where(Replay.public_id == context.replay_public_id))
            if replay is None:
                raise ValueError("replay disappeared before strategy persistence")
            replay_player = None
            if context.replay_player_public_id is not None:
                replay_player = session.scalar(
                    select(ReplayPlayer).where(
                        ReplayPlayer.public_id == context.replay_player_public_id,
                        ReplayPlayer.replay_id == replay.id,
                    )
                )
                if replay_player is None:
                    raise ValueError("replay player disappeared before strategy persistence")
            authorized = self._authorized_evidence(session, replay, context, assessments)
            parser_run_ids = {
                item.parser_run_id for item in authorized.values() if item.parser_run_id is not None
            }
            telemetry_run_ids = {
                item.telemetry_run_id
                for item in authorized.values()
                if item.telemetry_run_id is not None
            }
            if len(parser_run_ids) > 1 or len(telemetry_run_ids) > 1:
                raise ValueError("strategy evidence has mixed authoritative run owners")
            if not parser_run_ids and not telemetry_run_ids:
                raise ValueError("strategy evidence has no authoritative run owner")
            parser_run_id = next(iter(parser_run_ids), None)
            telemetry_run_id = next(iter(telemetry_run_ids), None)
            telemetry = None if telemetry_run_id is None else session.get(TelemetryRun, telemetry_run_id)
            if telemetry is not None:
                settings = telemetry.settings_json if isinstance(telemetry.settings_json, Mapping) else {}
                selected_parser = session.scalar(
                    select(ParserRun).where(
                        ParserRun.replay_id == replay.id,
                        ParserRun.run_id == settings.get("parser_run_id"),
                        ParserRun.status == "succeeded",
                        ParserRun.completion_status == "complete",
                    )
                )
                if (
                    telemetry.replay_id != replay.id
                    or telemetry.status != "succeeded"
                    or selected_parser is None
                    or (parser_run_id is not None and parser_run_id != selected_parser.id)
                ):
                    raise ValueError("strategy parser and telemetry evidence do not select one branch")
                parser_run_id = selected_parser.id
            elif parser_run_id is not None:
                selected_parser = session.get(ParserRun, parser_run_id)
                if (
                    selected_parser is None
                    or selected_parser.replay_id != replay.id
                    or selected_parser.status != "succeeded"
                    or selected_parser.completion_status != "complete"
                ):
                    raise ValueError("strategy parser evidence owner is not authoritative")
            for assessment in assessments:
                self._insert_assessment(
                    session,
                    replay,
                    replay_player,
                    assessment,
                    taxonomy,
                    cache_key,
                    authorized,
                    parser_run_id,
                    telemetry_run_id,
                )
            session.flush()
            self._after_graph_insert(session)
            session.commit()
            return StrategyAssessmentReceipt(cache_key, taxonomy.taxonomy_version, taxonomy.content_sha256, assessments)
        except IntegrityError as error:
            session.rollback()
            winner = self._load_receipt(
                context,
                feature_set_public_ids,
                taxonomy,
                registry,
                input_digest,
                cache_key,
                assessments,
            )
            if winner is not None:
                return winner
            raise ValueError("strategy persistence identity conflict") from error
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _authorized_evidence(
        session: Session,
        replay: Replay,
        context: StrategyContext,
        assessments: tuple[RuleAssessment, ...],
    ) -> Mapping[str, EvidenceItem]:
        all_context_refs = tuple(
            ref
            for feature in context.features
            for ref in (feature.derived_evidence.ref,) + feature.value.input_evidence
        ) + tuple(
            ref
            for ref in (
                None if context.catalog is None else context.catalog.evidence.ref,
                context.player_faction_evidence,
                context.opponent_faction_evidence,
                context.map_identity_evidence,
            )
            if ref is not None
        )
        context_refs: dict[str, EvidenceRef] = {}
        context_sources: dict[tuple[str, str], EvidenceRef] = {}
        for ref in all_context_refs:
            if (
                context_refs.get(ref.public_id, ref) != ref
                or context_sources.get((ref.source_kind, ref.source_key), ref) != ref
            ):
                raise ValueError("conflicting strategy context evidence identity")
            context_refs[ref.public_id] = ref
            context_sources[(ref.source_kind, ref.source_key)] = ref
        assessment_refs = {
            ref.public_id: ref
            for assessment in assessments
            for ref in assessment.supporting_evidence + assessment.contradicting_evidence
        }
        if any(context_refs.get(public_id) != ref for public_id, ref in assessment_refs.items()):
            raise ValueError("assessment cites evidence outside the exact strategy context")
        rows = {
            item.public_id: item
            for item in session.scalars(
                select(EvidenceItem).where(EvidenceItem.public_id.in_(tuple(context_refs)))
            ).all()
        }
        if set(rows) != set(context_refs):
            raise ValueError("unknown strategy context evidence public ID")
        for public_id, ref in context_refs.items():
            row = rows[public_id]
            if row.replay_id != replay.id:
                raise ValueError("cross-replay assessment evidence is forbidden")
            if _evidence_ref(row) != ref or row.tier not in ("observed", "derived"):
                raise ValueError("assessment evidence identity or tier mismatch")
        return rows

    def _insert_assessment(
        self,
        session: Session,
        replay: Replay,
        replay_player: ReplayPlayer | None,
        assessment: RuleAssessment,
        taxonomy: StrategyTaxonomy,
        cache_key: str,
        authorized: Mapping[str, EvidenceItem],
        parser_run_id: int | None,
        telemetry_run_id: int | None,
    ) -> None:
        supporting_ids = {ref.public_id for ref in assessment.supporting_evidence}
        contradicting_ids = {ref.public_id for ref in assessment.contradicting_evidence}
        if not supporting_ids.isdisjoint(contradicting_ids):
            raise ValueError("assessment evidence cannot be both supporting and contradicting")
        source_key = _assessment_source_key(
            replay.public_id,
            None if replay_player is None else replay_player.public_id,
            cache_key,
            assessment,
        )
        evidence = EvidenceItem(
            public_id=_uuid(f"evidence:{source_key}"),
            replay_id=replay.id,
            parser_run_id=parser_run_id,
            telemetry_run_id=telemetry_run_id,
            tier="derived",
            source_kind="strategy_rule",
            source_key=source_key,
            schema_version=1,
        )
        # TheSuperHackers @bugfix Leex 23/08/2026 Preserve authoritative predecessor runs on derived strategy evidence. (#TBD)
        session.add(evidence)
        session.flush()
        row = StrategyAssessment(
            public_id=_uuid(f"strategy-assessment:{source_key}"),
            evidence_item_id=evidence.id,
            replay_id=replay.id,
            replay_player_id=None if replay_player is None else replay_player.id,
            analysis_run_id=None,
            method="rule",
            strategy_label=assessment.strategy_id,
            phase=assessment.phase,
            taxonomy_version=taxonomy.taxonomy_version,
            rule_version=self._rule_version(taxonomy, assessment.strategy_id),
            model_version=None,
            frame_start=assessment.window.frame_start,
            frame_end=assessment.window.frame_end,
            quality=assessment.quality,
            confidence=assessment.rule_score,
            details_json=_details_mapping(assessment.details),
        )
        session.add(row)
        session.flush()
        for role, refs in (
            ("supporting", assessment.supporting_evidence),
            ("contradicting", assessment.contradicting_evidence),
        ):
            for ref in refs:
                session.add(
                    AssessmentEvidence(
                        assessment_id=row.id,
                        evidence_item_id=authorized[ref.public_id].id,
                        role=role,
                    )
                )

    @staticmethod
    def _rule_version(taxonomy: StrategyTaxonomy, strategy_id: str) -> str:
        for definition in taxonomy.strategies:
            if definition.strategy_id == strategy_id:
                return definition.rule_version
        raise ValueError("assessment strategy is absent from taxonomy")

    def _after_graph_insert(self, session: Session) -> None:
        """Test seam after the complete graph is pending but before commit."""
        del session
