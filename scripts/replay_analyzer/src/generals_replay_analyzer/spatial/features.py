"""Pure spatial plugin definitions and source-grounded feature extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from generals_replay_analyzer.features.base import (
    FeatureBundle,
    FeatureValue,
    FeatureWindow,
    complete_value,
    unavailable_value,
    validate_feature_value,
)
from generals_replay_analyzer.features.context import FeatureContext, input_digest
from generals_replay_analyzer.features.evidence import (
    CanonicalValue,
    EvidenceRef,
    ObservedEvidence,
    evidence_sort_key,
    fact,
    thaw_canonical,
)
from generals_replay_analyzer.features.registry import (
    BASE_REGISTRY,
    REGISTRY_SCHEMA,
    FeatureDefinition,
    ScopeType,
    ValueType,
)
from generals_replay_analyzer.spatial.assets import (
    GridSpec,
    Position3,
    SpatialCombatObservation,
    SpatialMapProjection,
    SpatialSample,
    SpatialUnavailable,
    StartPosition,
    StaticObjectCategory,
    StaticObjectFeature,
    WorldBounds,
    validate_map_projection,
)
from generals_replay_analyzer.spatial.coordinates import (
    NormalizedXY,
    PlayerTransform,
    world_to_map_normalized,
)
from generals_replay_analyzer.spatial.engagements import (
    EngagementCluster,
    EngagementClusters,
    cluster_engagements,
)
from generals_replay_analyzer.spatial.navigation import MovementSegment, route_sample_segments
from generals_replay_analyzer.spatial.statistics import (
    NUMPY_VERSION,
    PCG64_BOOTSTRAP_VERSION,
    SCIPY_VERSION,
    CellPresenceShares,
    SpatialAlgorithmSettings,
    bootstrap_sample_count_heatmap_interval,
    build_cell_presence_shares,
    build_sample_count_heatmap,
    derive_bootstrap_seed,
)

SPATIAL_PLUGIN_VERSION = "spatial-features-v1"
SPATIAL_FEATURE_NAMES = (
    "army_route.observed_reachable_distance",
    "army_route.observed_route_segment_count",
    "engagement.observed_cluster_count",
    "engagement.zone_clusters",
    "expansion.completed_structure_positions",
    "expansion.forward_completed_structure_count",
    "map_control.observed_cell_presence_share",
    "movement_density.sample_count_heatmap",
    "movement_density.sample_count_heatmap_bootstrap_interval",
    "resource_control.observed_supply_collected_amount",
    "resource_control.observed_supply_collection_share",
)
_PLAYER_FEATURE_NAMES = tuple(
    name for name in SPATIAL_FEATURE_NAMES if name.split(".", 1)[0] not in {"engagement", "map_control"}
)
_REPLAY_FEATURE_NAMES = tuple(name for name in SPATIAL_FEATURE_NAMES if name not in _PLAYER_FEATURE_NAMES)


def _definition(name: str, value_type: ValueType, unit: str, scope: ScopeType) -> FeatureDefinition:
    namespace = name.split(".", 1)[0]
    return FeatureDefinition(name, value_type, unit, (scope,), "inclusive", "observed", namespace)


_DEFINITIONS = tuple(
    sorted(
        (
            _definition("army_route.observed_reachable_distance", "real", "engine_world_unit", "player"),
            _definition("army_route.observed_route_segment_count", "integer", "count", "player"),
            _definition("engagement.observed_cluster_count", "integer", "count", "replay"),
            _definition("engagement.zone_clusters", "json", "json", "replay"),
            _definition("expansion.completed_structure_positions", "json", "json", "player"),
            _definition("expansion.forward_completed_structure_count", "integer", "count", "player"),
            _definition("map_control.observed_cell_presence_share", "json", "json", "replay"),
            _definition("movement_density.sample_count_heatmap", "json", "json", "player"),
            _definition("movement_density.sample_count_heatmap_bootstrap_interval", "json", "json", "player"),
            _definition("resource_control.observed_supply_collected_amount", "real", "credits", "player"),
            _definition("resource_control.observed_supply_collection_share", "real", "ratio", "player"),
        ),
        key=lambda definition: definition.name,
    )
)


@dataclass
class SpatialFeaturePlugin:
    plugin_name: str = "spatial"
    plugin_version: str = SPATIAL_PLUGIN_VERSION
    registry_schema: str = REGISTRY_SCHEMA
    owned_namespaces: tuple[str, ...] = (
        "army_route",
        "engagement",
        "expansion",
        "map_control",
        "movement_density",
        "resource_control",
    )
    definitions: tuple[FeatureDefinition, ...] = _DEFINITIONS


SPATIAL_REGISTRY = BASE_REGISTRY.with_plugin(SpatialFeaturePlugin())


def _mapping(value: object) -> dict[str, object] | None:
    return value if type(value) is dict else None


def _list(value: object) -> list[object] | None:
    return value if type(value) is list else None


def _position(value: object) -> Position3:
    mapping = _mapping(value)
    if mapping is None or set(mapping) != {"x", "y", "z"}:
        raise ValueError("position differs from the closed semantic projection")
    return Position3(cast(float, mapping["x"]), cast(float, mapping["y"]), cast(float, mapping["z"]))


def _parse_grid(value: object) -> GridSpec:
    grid = _mapping(value)
    if grid is None or set(grid) != {
        "bounds",
        "cell_size",
        "dimension_source",
        "height",
        "index_origin",
        "sample_point",
        "storage_order",
        "width",
    }:
        raise ValueError("pathing grid differs from the closed semantic projection")
    bounds = _mapping(grid["bounds"])
    cell_size = _mapping(grid["cell_size"])
    origin = _mapping(grid["index_origin"])
    if (
        bounds is None
        or set(bounds) != {"maximum_exclusive", "minimum_inclusive"}
        or cell_size is None
        or set(cell_size) != {"x", "y"}
        or origin is None
        or set(origin) != {"x", "y"}
        or grid["sample_point"] != "cell_center"
        or grid["storage_order"] != "row_major_y_then_x_x_fastest"
        or type(grid["dimension_source"]) is not str
    ):
        raise ValueError("pathing metadata differs from the validated grid contract")
    minimum = _mapping(bounds["minimum_inclusive"])
    maximum = _mapping(bounds["maximum_exclusive"])
    if minimum is None or maximum is None or set(minimum) != {"x", "y"} or set(maximum) != {"x", "y"}:
        raise ValueError("pathing bounds differ from the validated grid contract")
    return GridSpec(
        width=cast(int, grid["width"]),
        height=cast(int, grid["height"]),
        index_origin_x=cast(int, origin["x"]),
        index_origin_y=cast(int, origin["y"]),
        cell_size_x=cast(float, cell_size["x"]),
        cell_size_y=cast(float, cell_size["y"]),
        minimum_x=cast(float, minimum["x"]),
        minimum_y=cast(float, minimum["y"]),
        maximum_x=cast(float, maximum["x"]),
        maximum_y=cast(float, maximum["y"]),
    )


def _parse_world_bounds(value: object) -> WorldBounds:
    bounds = _mapping(value)
    if (
        bounds is None
        or set(bounds) != {"maximum", "maximum_inclusive", "minimum", "minimum_inclusive"}
        or bounds["maximum_inclusive"] is not True
        or bounds["minimum_inclusive"] is not True
    ):
        raise ValueError("world bounds differ from the closed semantic projection")
    return WorldBounds(_position(bounds["minimum"]), _position(bounds["maximum"]))


def _parse_projection(manifest: ObservedEvidence) -> SpatialMapProjection | SpatialUnavailable:
    raw = thaw_canonical(fact(manifest, "validated_spatial_projection"))
    projection = _mapping(raw)
    if projection is None:
        return SpatialUnavailable("missing_validated_map_asset")
    required = {
        "amphibious_passable",
        "content_sha256",
        "engine_data_identity",
        "ground_passable",
        "map_identity",
        "pathing",
        "schema_version",
        "start_positions",
        "static_objects",
        "world_bounds",
        "zone_ids",
    }
    if set(projection) != required:
        return SpatialUnavailable("invalid_validated_map_projection")
    try:
        starts_raw = _list(projection["start_positions"])
        objects_raw = _list(projection["static_objects"])
        ground = _list(projection["ground_passable"])
        amphibious = _list(projection["amphibious_passable"])
        zones = _list(projection["zone_ids"])
        if None in (starts_raw, objects_raw, ground, amphibious, zones):
            raise ValueError("projection sequences must be exact JSON arrays")
        starts = []
        for value in cast(list[object], starts_raw):
            start = _mapping(value)
            if start is None or set(start) != {
                "bounds_policy",
                "category_source",
                "name",
                "position",
                "slot_indices",
                "waypoint_id",
            }:
                raise ValueError("start position differs from the closed projection")
            slots = _list(start["slot_indices"])
            if (
                slots is None
                or start["bounds_policy"] != "pathfinder_xy_closed"
                or start["category_source"] != "GameSlot::getStartPos + TerrainLogic::getWaypointByName"
            ):
                raise ValueError("start position provenance differs from the closed projection")
            starts.append(
                StartPosition(
                    cast(str, start["name"]),
                    cast(int, start["waypoint_id"]),
                    tuple(cast(list[int], slots)),
                    _position(start["position"]),
                    manifest.ref,
                )
            )
        objects = []
        for value in cast(list[object], objects_raw):
            item = _mapping(value)
            if item is None or set(item) != {
                "bounds_policy",
                "categories",
                "creation_source",
                "object_id",
                "orientation",
                "position",
                "snapshot_scope",
                "template_name",
            }:
                raise ValueError("static object differs from the closed projection")
            categories_raw = _list(item["categories"])
            if (
                categories_raw is None
                or item["bounds_policy"] != "pathfinder_xy_closed"
                or item["creation_source"] != "map_loaded"
                or item["snapshot_scope"] != "post_map_initialization"
                or type(item["orientation"]) not in (int, float)
            ):
                raise ValueError("static object provenance differs from the closed projection")
            categories = []
            for category_value in categories_raw:
                category = _mapping(category_value)
                if category is None or set(category) != {"name", "source"}:
                    raise ValueError("static category differs from the closed projection")
                categories.append(StaticObjectCategory(cast(str, category["name"]), cast(str, category["source"])))
            objects.append(
                StaticObjectFeature(
                    cast(int, item["object_id"]),
                    cast(str, item["template_name"]),
                    _position(item["position"]),
                    tuple(categories),
                    manifest.ref,
                )
            )
        result = SpatialMapProjection(
            schema_version=cast(int, projection["schema_version"]),
            content_sha256=cast(str, projection["content_sha256"]),
            map_identity=cast(str, projection["map_identity"]),
            engine_data_identity=cast(str, projection["engine_data_identity"]),
            pathing=_parse_grid(projection["pathing"]),
            world_bounds=_parse_world_bounds(projection["world_bounds"]),
            ground_passable=tuple(cast(list[bool], ground)),
            amphibious_passable=tuple(cast(list[bool], amphibious)),
            zone_ids=tuple(cast(list[int], zones)),
            start_positions=tuple(starts),
            static_objects=tuple(objects),
        )
    except (TypeError, ValueError):
        return SpatialUnavailable("invalid_validated_map_projection")
    checked = validate_map_projection(result)
    if isinstance(checked, SpatialUnavailable):
        return checked
    catalog = _mapping(thaw_canonical(fact(manifest, "game_data_catalog")))
    map_asset = _mapping(thaw_canonical(fact(manifest, "map_asset")))
    if (
        catalog is None
        or map_asset is None
        or catalog.get("engine_data_identity") != result.engine_data_identity
        or map_asset.get("content_sha256") != result.content_sha256
        or map_asset.get("schema_version") != result.schema_version
        or map_asset.get("engine_data_identity") != result.engine_data_identity
        or map_asset.get("map_identity") != result.map_identity
    ):
        return SpatialUnavailable("map_identity_mismatch")
    return result


def _settings(context: FeatureContext) -> SpatialAlgorithmSettings | SpatialUnavailable:
    raw_settings = _mapping(thaw_canonical(context.settings))
    spatial = None if raw_settings is None else _mapping(raw_settings.get("spatial"))
    if spatial is None:
        return SpatialUnavailable("invalid_spatial_settings")
    try:
        return SpatialAlgorithmSettings.from_mapping(spatial)
    except ValueError:
        return SpatialUnavailable("invalid_spatial_settings")


def _observed_facts(item: ObservedEvidence) -> dict[str, object]:
    value = thaw_canonical(item.facts)
    return value if type(value) is dict else {}


def _references(*groups: tuple[EvidenceRef, ...]) -> tuple[EvidenceRef, ...]:
    by_id: dict[str, EvidenceRef] = {}
    for reference in (item for group in groups for item in group):
        existing = by_id.get(reference.public_id)
        if existing is not None and existing != reference:
            raise ValueError("conflicting evidence identity")
        by_id[reference.public_id] = reference
    return tuple(sorted(by_id.values(), key=evidence_sort_key))


def _base_details(
    projection: SpatialMapProjection | None, settings: SpatialAlgorithmSettings | None, reason: str | None = None
) -> dict[str, object]:
    return {
        "algorithm_settings": None if settings is None else dict(settings.canonical_items()),
        "engine_data_identity": None if projection is None else projection.engine_data_identity,
        "map_content_sha256": None if projection is None else projection.content_sha256,
        "map_schema_version": None if projection is None else projection.schema_version,
        "omission_reason": reason,
        "plugin_version": SPATIAL_PLUGIN_VERSION,
    }


def _partial_value(
    name: str,
    raw_value: object,
    context: FeatureContext,
    window: FeatureWindow,
    reason: str,
    evidence: tuple[EvidenceRef, ...],
    details: object,
) -> FeatureValue:
    definition = SPATIAL_REGISTRY.definition(name)
    return validate_feature_value(
        FeatureValue(
            name,
            definition.value_type,
            cast(CanonicalValue, raw_value),
            definition.unit,
            context.scope,
            window,
            "partial",
            reason,
            evidence,
            details=cast(CanonicalValue, details),
        ),
        SPATIAL_REGISTRY,
    )


def _sample(item: ObservedEvidence) -> SpatialSample | None:
    values = _observed_facts(item)
    try:
        object_id = values.get("object_id")
        object_key = values.get("object_key", f"object:{object_id}")
        owner = values.get("owner_scope_key", values.get("replay_player_public_id"))
        path_goal_value = values.get("path_goal")
        return SpatialSample(
            evidence=item.ref,
            frame=item.frame if item.frame is not None else -1,
            object_key=cast(str, object_key),
            owner_scope_key=cast(str | None, owner),
            position=_position(values.get("position")),
            position_bounds_policy=cast(str, values.get("position_bounds_policy")),
            is_mobile=cast(bool, values.get("is_mobile")),
            is_structure=cast(bool, values.get("is_structure")),
            is_disabled=cast(bool, values.get("is_disabled")),
            is_engine_moving=cast(bool, values.get("is_engine_moving")),
            locomotor_surface=cast(Literal["ground", "amphibious"] | None, values.get("locomotor_surface")),
            path_goal=None if path_goal_value is None else _position(path_goal_value),
        )
    except (TypeError, ValueError):
        return None


def _combat(item: ObservedEvidence) -> SpatialCombatObservation | None:
    values = _observed_facts(item)
    sources = _list(values.get("source_replay_player_public_ids"))
    if sources is None or any(type(source) is not str or not source for source in sources):
        attackers: tuple[str, ...] = ()
    else:
        attackers = tuple(sorted(set(cast(list[str], sources))))
    attacker = attackers[0] if len(attackers) == 1 else None
    try:
        return SpatialCombatObservation(
            item.ref,
            item.frame if item.frame is not None else -1,
            _position(values.get("location")),
            attacker,
            cast(str | None, values.get("victim_replay_player_public_id")),
            cast(float, values.get("applied_amount")),
            cast(bool, values.get("killing_blow")),
            attackers,
        )
    except (TypeError, ValueError):
        return None


def _normalized(point: NormalizedXY) -> dict[str, float]:
    return {"u": _canonical_float(point.u), "v": _canonical_float(point.v)}


def _canonical_float(value: float) -> float:
    return 0.0 if value == 0.0 else value


def _transform_record(transform: PlayerTransform, position: Position3) -> dict[str, object]:
    transformed = transform.apply(position)
    return {
        "enemy_start_evidence_public_id": transform.enemy_start.evidence.public_id,
        "enemy_start_name": transform.enemy_start.name,
        "own_start_evidence_public_id": transform.own_start.evidence.public_id,
        "own_start_name": transform.own_start.name,
        "position": {
            "x": _canonical_float(transformed.x),
            "y": _canonical_float(transformed.y),
            "z": _canonical_float(transformed.z),
        },
        "rotation_radians": _canonical_float(transform.angle_radians),
        "transform_version": transform.transform_version,
    }


# TheSuperHackers @feature Leex 22/08/2026 Emit only source-backed spatial values and explicit unavailable omissions. (#0)
class SpatialFeatureExtractor:
    name = "spatial"
    version = SPATIAL_PLUGIN_VERSION
    feature_names = SPATIAL_FEATURE_NAMES
    observation_policy: Literal["replay_wide_telemetry"] = "replay_wide_telemetry"

    def extract(self, context: FeatureContext) -> FeatureBundle:
        if context.scope.scope_type not in ("player", "replay"):
            raise ValueError("spatial extractor supports only player and replay scopes")
        scoped_names = _PLAYER_FEATURE_NAMES if context.scope.scope_type == "player" else _REPLAY_FEATURE_NAMES
        window = FeatureWindow(0, context.final_frame or 0)
        settings = _settings(context)
        if isinstance(settings, SpatialUnavailable):
            return self._all_unavailable(context, scoped_names, window, settings.reason, None, None, ())
        manifests = tuple(item for item in context.observed if item.event_type == "manifest")
        if len(manifests) != 1:
            return self._all_unavailable(
                context, scoped_names, window, "missing_validated_map_asset", None, settings, ()
            )
        manifest = manifests[0]
        projection = _parse_projection(manifest)
        if isinstance(projection, SpatialUnavailable):
            return self._all_unavailable(
                context, scoped_names, window, projection.reason, None, settings, (manifest.ref,)
            )
        values = (
            self._player_values(context, window, projection, settings, manifest)
            if context.scope.scope_type == "player"
            else self._replay_values(context, window, projection, settings, manifest)
        )
        return FeatureBundle(self.name, self.version, tuple(sorted(values, key=lambda value: value.name)))

    def _all_unavailable(
        self,
        context: FeatureContext,
        names: tuple[str, ...],
        window: FeatureWindow,
        reason: str,
        projection: SpatialMapProjection | None,
        settings: SpatialAlgorithmSettings | None,
        evidence: tuple[EvidenceRef, ...],
    ) -> FeatureBundle:
        details = _base_details(projection, settings, reason)
        return FeatureBundle(
            self.name,
            self.version,
            tuple(
                unavailable_value(
                    name,
                    context.scope,
                    window,
                    reason,
                    SPATIAL_REGISTRY,
                    input_evidence=evidence,
                    details=details,
                )
                for name in names
            ),
        )

    def _player_values(
        self,
        context: FeatureContext,
        window: FeatureWindow,
        projection: SpatialMapProjection,
        settings: SpatialAlgorithmSettings,
        manifest: ObservedEvidence,
    ) -> tuple[FeatureValue, ...]:
        samples = tuple(
            sample
            for item in context.observed
            if item.event_type == "entity_sample" and (sample := _sample(item)) is not None
        )
        base = _base_details(projection, settings)
        values: list[FeatureValue] = []
        values.extend(self._resource_values(context, window, projection, manifest, base))
        values.extend(self._expansion_values(context, window, projection, manifest, samples, base))
        values.extend(self._route_values(context, window, projection, settings, manifest, samples, base))
        values.extend(self._density_values(context, window, projection, settings, manifest, samples, base))
        return tuple(values)

    def _resource_values(
        self,
        context: FeatureContext,
        window: FeatureWindow,
        projection: SpatialMapProjection,
        manifest: ObservedEvidence,
        base: dict[str, object],
    ) -> tuple[FeatureValue, ...]:
        sources = {
            item.object_id
            for item in projection.static_objects
            if "supply_source" in {category.name for category in item.categories}
        }
        eligible: list[tuple[ObservedEvidence, str, int, float]] = []
        omissions: list[tuple[ObservedEvidence, object, str]] = []
        for item in context.observed:
            if item.event_type != "supply_collected":
                continue
            values = _observed_facts(item)
            owner = values.get("replay_player_public_id")
            source = values.get("source_object_id")
            amount = values.get("amount")
            if type(source) is not int or source not in sources:
                omissions.append((item, owner, "unresolved_supply_source"))
            elif type(owner) is not str or not owner or type(amount) not in (int, float) or cast(int | float, amount) < 0:
                omissions.append((item, owner, "invalid_supply_observation"))
            else:
                typed_amount = cast(int | float, amount)
                eligible.append((item, owner, source, float(typed_amount)))
        player = context.replay_player_public_id
        owned = [entry for entry in eligible if entry[1] == player]
        amount_omissions = [entry for entry in omissions if entry[1] in (None, player)]
        omission_effect = "excluded_from_resolved_supply_amount_and_share"
        amount_omission_details = [
            {
                "effect": omission_effect,
                "evidence_public_id": item.ref.public_id,
                "reason": reason,
            }
            for item, _, reason in sorted(amount_omissions, key=lambda entry: _observation_key(entry[0]))
        ]
        share_omission_details = [
            {
                "effect": omission_effect,
                "evidence_public_id": item.ref.public_id,
                "reason": reason,
            }
            for item, _, reason in sorted(omissions, key=lambda entry: _observation_key(entry[0]))
        ]
        if not owned:
            stable_amount_omissions = sorted(amount_omissions, key=lambda entry: _observation_key(entry[0]))
            stable_share_omissions = sorted(omissions, key=lambda entry: _observation_key(entry[0]))
            amount_reason = (
                stable_amount_omissions[0][2] if stable_amount_omissions else "missing_resolved_supply_source"
            )
            amount_evidence = _references(
                (manifest.ref,), tuple(entry[0].ref for entry in stable_amount_omissions)
            )
            amount_details = {
                **base,
                "eligible_source_object_ids": sorted(sources),
                "omissions": amount_omission_details,
            }
            share_reason = (
                stable_share_omissions[0][2] if stable_share_omissions else "missing_resolved_supply_source"
            )
            share_evidence = _references(
                (manifest.ref,),
                tuple(entry[0].ref for entry in eligible),
                tuple(entry[0].ref for entry in stable_share_omissions),
            )
            share_details = {
                **base,
                "denominator": sum(entry[3] for entry in eligible),
                "eligible_source_object_ids": sorted(sources),
                "omissions": share_omission_details,
            }
            return (
                unavailable_value(
                    "resource_control.observed_supply_collected_amount",
                    context.scope,
                    window,
                    amount_reason,
                    SPATIAL_REGISTRY,
                    input_evidence=amount_evidence,
                    details=amount_details,
                ),
                unavailable_value(
                    "resource_control.observed_supply_collection_share",
                    context.scope,
                    window,
                    share_reason,
                    SPATIAL_REGISTRY,
                    input_evidence=share_evidence,
                    details=share_details,
                ),
            )
        owned_refs = _references(
            (manifest.ref,),
            tuple(entry[0].ref for entry in owned),
            tuple(entry[0].ref for entry in amount_omissions),
        )
        total = sum(entry[3] for entry in owned)
        by_source = [
            {
                "amount": sum(entry[3] for entry in owned if entry[2] == source),
                "evidence_public_ids": sorted(entry[0].ref.public_id for entry in owned if entry[2] == source),
                "source_object_id": source,
            }
            for source in sorted({entry[2] for entry in owned})
        ]
        amount_details = {**base, "omissions": amount_omission_details, "per_source": by_source}
        amount_value = (
            _partial_value(
                "resource_control.observed_supply_collected_amount",
                total,
                context,
                window,
                amount_omissions[0][2],
                owned_refs,
                amount_details,
            )
            if amount_omissions
            else complete_value(
                "resource_control.observed_supply_collected_amount",
                total,
                context.scope,
                window,
                owned_refs,
                SPATIAL_REGISTRY,
                details=amount_details,
            )
        )
        denominator = sum(entry[3] for entry in eligible)
        all_refs = _references(
            (manifest.ref,),
            tuple(entry[0].ref for entry in eligible),
            tuple(entry[0].ref for entry in omissions),
        )
        per_source_shares = []
        for source in sorted({entry[2] for entry in eligible}):
            source_entries = [entry for entry in eligible if entry[2] == source]
            player_entries = [entry for entry in source_entries if entry[1] == player]
            source_denominator = sum(entry[3] for entry in source_entries)
            player_amount = sum(entry[3] for entry in player_entries)
            per_source_shares.append(
                {
                    "denominator": source_denominator,
                    "denominator_evidence_public_ids": sorted(entry[0].ref.public_id for entry in source_entries),
                    "player_amount": player_amount,
                    "player_evidence_public_ids": sorted(entry[0].ref.public_id for entry in player_entries),
                    "share": None if source_denominator == 0 else player_amount / source_denominator,
                    "source_object_id": source,
                }
            )
        if denominator == 0:
            share_value = unavailable_value(
                "resource_control.observed_supply_collection_share",
                context.scope,
                window,
                "zero_supply_share_denominator",
                SPATIAL_REGISTRY,
                input_evidence=all_refs,
                details={
                    **base,
                    "denominator": denominator,
                    "omissions": share_omission_details,
                    "per_source": per_source_shares,
                },
            )
        else:
            share_details = {
                    **base,
                    "denominator": denominator,
                    "omissions": share_omission_details,
                    "per_source": per_source_shares,
                    "player_amount": total,
            }
            share_value = (
                _partial_value(
                    "resource_control.observed_supply_collection_share",
                    total / denominator,
                    context,
                    window,
                    omissions[0][2],
                    all_refs,
                    share_details,
                )
                if omissions
                else complete_value(
                    "resource_control.observed_supply_collection_share",
                    total / denominator,
                    context.scope,
                    window,
                    all_refs,
                    SPATIAL_REGISTRY,
                    details=share_details,
                )
            )
        return (amount_value, share_value)

    def _player_transform(
        self, context: FeatureContext, projection: SpatialMapProjection
    ) -> tuple[PlayerTransform | SpatialUnavailable, tuple[EvidenceRef, ...]]:
        initializations = tuple(item for item in context.observed if item.event_type == "players_initialized")
        initialization_refs = tuple(item.ref for item in initializations)
        if len(initializations) != 1:
            return SpatialUnavailable("unresolved_player_transform"), initialization_refs
        initialization = initializations[0]
        values = _observed_facts(initialization)
        raw_slots = _list(values.get("slots")) if set(values) == {"slots"} else None
        if raw_slots is None or not raw_slots:
            return SpatialUnavailable("unresolved_player_transform"), (initialization.ref,)
        slots: list[tuple[int, int, str]] = []
        seen_slot_indices: set[int] = set()
        seen_player_indices: set[int] = set()
        seen_players: set[str] = set()
        for raw_slot in raw_slots:
            slot_values = _mapping(raw_slot)
            if slot_values is None or set(slot_values) != {
                "owner_scope_key",
                "player_index",
                "replay_player_public_id",
                "resolution_status",
                "slot_index",
            }:
                return SpatialUnavailable("unresolved_player_transform"), (initialization.ref,)
            slot_index = slot_values["slot_index"]
            player_index = slot_values["player_index"]
            owner = slot_values["owner_scope_key"]
            replay_player = slot_values["replay_player_public_id"]
            if (
                slot_values["resolution_status"] != "resolved"
                or type(slot_index) is not int
                or not 0 <= slot_index <= 7
                or type(player_index) is not int
                or player_index < 0
                or type(owner) is not str
                or not owner
                or type(replay_player) is not str
                or replay_player != owner
                or slot_index in seen_slot_indices
                or player_index in seen_player_indices
                or owner in seen_players
            ):
                return SpatialUnavailable("unresolved_player_transform"), (initialization.ref,)
            seen_slot_indices.add(slot_index)
            seen_player_indices.add(player_index)
            seen_players.add(owner)
            slots.append((slot_index, player_index, owner))
        assignments = [item for item in slots if item[2] == context.replay_player_public_id]
        if len(assignments) != 1:
            return SpatialUnavailable("unresolved_player_transform"), (initialization.ref,)
        slot = assignments[0][0]
        own = [start for start in projection.start_positions if slot in start.slot_indices]
        if len(own) != 1:
            return SpatialUnavailable("unresolved_player_transform"), (initialization.ref,)
        peer_slots = tuple(item[0] for item in slots if item[2] != context.replay_player_public_id)
        if not peer_slots:
            return SpatialUnavailable("unresolved_player_transform"), (initialization.ref,)
        for peer_slot in peer_slots:
            assigned_starts = [start for start in projection.start_positions if peer_slot in start.slot_indices]
            if len(assigned_starts) != 1:
                return SpatialUnavailable("unresolved_player_transform"), (initialization.ref,)
        # The accepted context proves slot ownership but carries no team/opponent relation.
        return SpatialUnavailable("unresolved_player_transform"), (initialization.ref,)

    def _expansion_values(
        self,
        context: FeatureContext,
        window: FeatureWindow,
        projection: SpatialMapProjection,
        manifest: ObservedEvidence,
        samples: tuple[SpatialSample, ...],
        base: dict[str, object],
    ) -> tuple[FeatureValue, ...]:
        completions = []
        for item in context.observed:
            if item.event_type != "construction_completed":
                continue
            values = _observed_facts(item)
            if (
                values.get("replay_player_public_id") == context.replay_player_public_id
                and item.frame is not None
                and type(values.get("object_id")) is int
                and type(values.get("template_name")) is str
            ):
                completions.append((item, cast(int, values["object_id"]), cast(str, values["template_name"])))
        transform, transform_refs = self._player_transform(context, projection)
        records = []
        record_refs: list[EvidenceRef] = [manifest.ref, *transform_refs]
        omissions: list[dict[str, object]] = []
        forward = 0
        for completion, object_id, template_name in sorted(completions, key=lambda item: _observation_key(item[0])):
            candidates = sorted(
                (
                    sample
                    for sample in samples
                    if sample.object_key == f"object:{object_id}"
                    and sample.is_structure
                    and sample.frame >= cast(int, completion.frame)
                    and sample.owner_scope_key == context.replay_player_public_id
                ),
                key=lambda sample: (sample.frame, evidence_sort_key(sample.evidence)),
            )
            matches = sorted(
                (
                    sample for sample in candidates if sample.position_bounds_policy == "pathfinder_xy_closed"
                ),
                key=lambda sample: (sample.frame, evidence_sort_key(sample.evidence)),
            )
            if not matches:
                omitted_refs = _references((completion.ref,), tuple(sample.evidence for sample in candidates))
                reason = (
                    "position_exempt_spatial_sample"
                    if any(sample.position_bounds_policy != "pathfinder_xy_closed" for sample in candidates)
                    else "missing_bounded_matching_structure_sample"
                )
                omissions.append(
                    {
                        "effect": "excluded_from_completed_structure_positions_and_forward_count",
                        "evidence_public_ids": [reference.public_id for reference in omitted_refs],
                        "object_id": object_id,
                        "reason": reason,
                    }
                )
                record_refs.extend(omitted_refs)
                continue
            sample = matches[0]
            normalized = world_to_map_normalized(sample.position, projection.world_bounds)
            if isinstance(normalized, SpatialUnavailable):
                omitted_refs = _references((completion.ref, sample.evidence))
                omissions.append(
                    {
                        "effect": "excluded_from_completed_structure_positions_and_forward_count",
                        "evidence_public_ids": [reference.public_id for reference in omitted_refs],
                        "object_id": object_id,
                        "reason": normalized.reason,
                    }
                )
                record_refs.extend(omitted_refs)
                continue
            record = {
                "completion_frame": completion.frame,
                "evidence_public_ids": sorted((completion.ref.public_id, sample.evidence.public_id)),
                "map_normalized": _normalized(normalized),
                "object_id": object_id,
                "player_centric": None if isinstance(transform, SpatialUnavailable) else _transform_record(transform, sample.position),
                "raw_position": {"x": sample.position.x, "y": sample.position.y, "z": sample.position.z},
                "template_name": template_name,
            }
            records.append(record)
            record_refs.extend((completion.ref, sample.evidence))
            if not isinstance(transform, SpatialUnavailable):
                own = transform.own_start.position
                enemy = transform.enemy_start.position
                if (sample.position.x - own.x) * (enemy.x - own.x) + (sample.position.y - own.y) * (enemy.y - own.y) > 0:
                    forward += 1
        if not records:
            evidence = _references(tuple(record_refs))
            reason = cast(str, omissions[0]["reason"]) if omissions else "missing_bounded_entity_samples"
            details = {**base, "omissions": omissions, "record_count": 0}
            return tuple(
                unavailable_value(
                    name,
                    context.scope,
                    window,
                    reason,
                    SPATIAL_REGISTRY,
                    input_evidence=evidence,
                    details=details,
                )
                for name in (
                    "expansion.completed_structure_positions",
                    "expansion.forward_completed_structure_count",
                )
            )
        evidence = _references(tuple(record_refs))
        common_details = {**base, "omissions": omissions, "record_count": len(records)}
        if isinstance(transform, SpatialUnavailable):
            positions = _partial_value(
                "expansion.completed_structure_positions",
                records,
                context,
                window,
                cast(str, omissions[0]["reason"]) if omissions else transform.reason,
                evidence,
                {**common_details, "transform": None},
            )
            forward_value = unavailable_value(
                "expansion.forward_completed_structure_count",
                context.scope,
                window,
                transform.reason,
                SPATIAL_REGISTRY,
                input_evidence=evidence,
                details=common_details,
            )
        elif omissions:
            reason = cast(str, omissions[0]["reason"])
            positions = _partial_value(
                "expansion.completed_structure_positions",
                records,
                context,
                window,
                reason,
                evidence,
                {**common_details, "transform_version": transform.transform_version},
            )
            forward_value = _partial_value(
                "expansion.forward_completed_structure_count",
                forward,
                context,
                window,
                reason,
                evidence,
                {**common_details, "predicate": "strictly_positive_raw_dot_product"},
            )
        else:
            positions = complete_value(
                "expansion.completed_structure_positions",
                records,
                context.scope,
                window,
                evidence,
                SPATIAL_REGISTRY,
                details={**common_details, "transform_version": transform.transform_version},
            )
            forward_value = complete_value(
                "expansion.forward_completed_structure_count",
                forward,
                context.scope,
                window,
                evidence,
                SPATIAL_REGISTRY,
                details={**common_details, "predicate": "strictly_positive_raw_dot_product"},
            )
        return (positions, forward_value)

    def _route_values(
        self,
        context: FeatureContext,
        window: FeatureWindow,
        projection: SpatialMapProjection,
        settings: SpatialAlgorithmSettings,
        manifest: ObservedEvidence,
        samples: tuple[SpatialSample, ...],
        base: dict[str, object],
    ) -> tuple[FeatureValue, ...]:
        owned = tuple(sample for sample in samples if sample.owner_scope_key == context.replay_player_public_id)
        routes = route_sample_segments(
            projection, owned, max_sample_gap_frames=settings.max_sample_gap_frames
        )
        evidence = _references(
            (manifest.ref,),
            tuple(reference for segment in routes.segments for reference in segment.evidence),
            tuple(reference for omission in routes.omissions for reference in omission.evidence),
        )
        details = {
            **base,
            "algorithm_version": settings.route_algorithm_version,
            "omissions": [
                {
                    "end_frame": item.end_frame,
                    "effect": "excluded_from_observed_route_segments",
                    "evidence_public_ids": [reference.public_id for reference in item.evidence],
                    "object_key": item.object_key,
                    "reason": item.reason,
                    "start_frame": item.start_frame,
                }
                for item in routes.omissions
            ],
            "segments": [_segment_record(item) for item in routes.segments],
        }
        if not routes.segments:
            return tuple(
                unavailable_value(
                    name,
                    context.scope,
                    window,
                    cast(str, routes.reason),
                    SPATIAL_REGISTRY,
                    input_evidence=evidence,
                    details=details,
                )
                for name in (
                    "army_route.observed_reachable_distance",
                    "army_route.observed_route_segment_count",
                )
            )
        raw_distance = sum(segment.route.distance_world for segment in routes.segments)
        maker = _partial_value if routes.quality == "partial" else None
        if maker is not None:
            reason = cast(str, routes.reason)
            return (
                maker("army_route.observed_reachable_distance", raw_distance, context, window, reason, evidence, details),
                maker("army_route.observed_route_segment_count", len(routes.segments), context, window, reason, evidence, details),
            )
        return (
            complete_value(
                "army_route.observed_reachable_distance",
                raw_distance,
                context.scope,
                window,
                evidence,
                SPATIAL_REGISTRY,
                details=details,
            ),
            complete_value(
                "army_route.observed_route_segment_count",
                len(routes.segments),
                context.scope,
                window,
                evidence,
                SPATIAL_REGISTRY,
                details=details,
            ),
        )

    def _density_values(
        self,
        context: FeatureContext,
        window: FeatureWindow,
        projection: SpatialMapProjection,
        settings: SpatialAlgorithmSettings,
        manifest: ObservedEvidence,
        samples: tuple[SpatialSample, ...],
        base: dict[str, object],
    ) -> tuple[FeatureValue, ...]:
        heatmap = build_sample_count_heatmap(
            projection, samples, owner_scope_key=context.replay_player_public_id
        )
        evidence = _references((manifest.ref,), heatmap.input_evidence, heatmap.omitted_evidence)
        details = {
            **base,
            "algorithm_version": heatmap.algorithm_version,
            "omission_effect": "excluded_from_bounded_sample_count_statistics" if heatmap.omitted_evidence else None,
            "omitted_evidence_public_ids": [reference.public_id for reference in heatmap.omitted_evidence],
        }
        if heatmap.quality == "unavailable":
            density = unavailable_value(
                "movement_density.sample_count_heatmap",
                context.scope,
                window,
                cast(str, heatmap.reason),
                SPATIAL_REGISTRY,
                input_evidence=evidence,
                details=details,
            )
        else:
            raw = {
                "cells": [
                    {"cell_x": cell.cell.x, "cell_y": cell.cell.y, "count": cell.count}
                    for cell in heatmap.cells
                ],
                "sample_count": heatmap.sample_count,
            }
            density = (
                _partial_value(
                    "movement_density.sample_count_heatmap",
                    raw,
                    context,
                    window,
                    cast(str, heatmap.reason),
                    evidence,
                    details,
                )
                if heatmap.quality == "partial"
                else complete_value(
                    "movement_density.sample_count_heatmap",
                    raw,
                    context.scope,
                    window,
                    evidence,
                    SPATIAL_REGISTRY,
                    details=details,
                )
            )
        digest = input_digest(context)
        interval = bootstrap_sample_count_heatmap_interval(
            projection,
            samples,
            input_digest=digest,
            settings=settings,
            owner_scope_key=context.replay_player_public_id,
        )
        interval_details = {
            **base,
            "algorithm_version": PCG64_BOOTSTRAP_VERSION,
            "bit_generator": "PCG64",
            "input_evidence_public_ids": sorted(reference.public_id for reference in heatmap.input_evidence),
            "numpy_version": NUMPY_VERSION,
            "omission_effect": "excluded_from_bounded_sample_count_statistics"
            if heatmap.omitted_evidence
            else None,
            "omitted_evidence_public_ids": sorted(
                reference.public_id for reference in heatmap.omitted_evidence
            ),
            "quantile_policy": "scipy-scoreatpercentile-fraction-v1:p025-p975",
            "resample_count": settings.bootstrap_resamples,
            "scipy_version": SCIPY_VERSION,
            "seed_hex": derive_bootstrap_seed(digest),
            "statistic_name": "occupied_cell_sample_proportion",
        }
        if isinstance(interval, SpatialUnavailable):
            bootstrap = unavailable_value(
                "movement_density.sample_count_heatmap_bootstrap_interval",
                context.scope,
                window,
                interval.reason,
                SPATIAL_REGISTRY,
                input_evidence=evidence,
                details=interval_details,
            )
        else:
            interval_evidence = _references((manifest.ref,), interval.input_evidence, interval.omitted_evidence)
            raw_interval = {
                "cells": [
                    {
                        "cell_x": cell.cell.x,
                        "cell_y": cell.cell.y,
                        "lower": cell.lower,
                        "observed_proportion": cell.observed_proportion,
                        "upper": cell.upper,
                    }
                    for cell in interval.intervals
                ],
                "sample_count": len(interval.input_evidence),
            }
            bootstrap = (
                _partial_value(
                    "movement_density.sample_count_heatmap_bootstrap_interval",
                    raw_interval,
                    context,
                    window,
                    cast(str, interval.reason),
                    interval_evidence,
                    interval_details,
                )
                if interval.quality == "partial"
                else complete_value(
                    "movement_density.sample_count_heatmap_bootstrap_interval",
                    raw_interval,
                    context.scope,
                    window,
                    interval_evidence,
                    SPATIAL_REGISTRY,
                    details=interval_details,
                )
            )
        return (density, bootstrap)

    def _replay_values(
        self,
        context: FeatureContext,
        window: FeatureWindow,
        projection: SpatialMapProjection,
        settings: SpatialAlgorithmSettings,
        manifest: ObservedEvidence,
    ) -> tuple[FeatureValue, ...]:
        samples = tuple(
            sample
            for item in context.observed
            if item.event_type == "entity_sample" and (sample := _sample(item)) is not None
        )
        combats = tuple(
            combat
            for item in context.observed
            if item.event_type == "damage_applied" and (combat := _combat(item)) is not None
        )
        base = _base_details(projection, settings)
        engagements = cluster_engagements(
            projection,
            combats,
            gap_frames=settings.engagement_gap_frames,
            reachable_radius_cells=settings.engagement_reachable_radius_cells,
        )
        engagement_values = self._engagement_values(context, window, manifest, engagements, base)
        presence = build_cell_presence_shares(projection, samples)
        presence_value = self._presence_value(context, window, manifest, presence, base)
        return (*engagement_values, presence_value)

    def _engagement_values(
        self,
        context: FeatureContext,
        window: FeatureWindow,
        manifest: ObservedEvidence,
        result: EngagementClusters,
        base: dict[str, object],
    ) -> tuple[FeatureValue, FeatureValue]:
        evidence = _references(
            (manifest.ref,),
            tuple(reference for cluster in result.clusters for reference in cluster.evidence),
            result.omitted_evidence,
        )
        details = {
            **base,
            "algorithm_version": result.algorithm_version,
            "omissions": [
                {"evidence_public_id": item.evidence.public_id, "reason": item.reason} for item in result.omissions
            ],
        }
        if result.quality == "unavailable":
            return (
                unavailable_value(
                    "engagement.zone_clusters", context.scope, window, cast(str, result.reason), SPATIAL_REGISTRY,
                    input_evidence=evidence, details=details,
                ),
                unavailable_value(
                    "engagement.observed_cluster_count", context.scope, window, cast(str, result.reason), SPATIAL_REGISTRY,
                    input_evidence=evidence, details=details,
                ),
            )
        raw = [_cluster_record(cluster) for cluster in result.clusters]
        if result.quality == "partial":
            reason = cast(str, result.reason)
            return (
                _partial_value("engagement.zone_clusters", raw, context, window, reason, evidence, details),
                _partial_value("engagement.observed_cluster_count", len(raw), context, window, reason, evidence, details),
            )
        return (
            complete_value("engagement.zone_clusters", raw, context.scope, window, evidence, SPATIAL_REGISTRY, details=details),
            complete_value(
                "engagement.observed_cluster_count", len(raw), context.scope, window, evidence, SPATIAL_REGISTRY, details=details
            ),
        )

    def _presence_value(
        self,
        context: FeatureContext,
        window: FeatureWindow,
        manifest: ObservedEvidence,
        result: CellPresenceShares,
        base: dict[str, object],
    ) -> FeatureValue:
        evidence = _references((manifest.ref,), result.input_evidence, result.omitted_evidence)
        details = {
            **base,
            "algorithm_version": result.algorithm_version,
            "omission_effect": "excluded_from_shared_cell_presence_statistics"
            if result.omitted_evidence
            else None,
            "omitted_evidence_public_ids": [reference.public_id for reference in result.omitted_evidence],
        }
        if result.quality == "unavailable":
            return unavailable_value(
                "map_control.observed_cell_presence_share",
                context.scope,
                window,
                cast(str, result.reason),
                SPATIAL_REGISTRY,
                input_evidence=evidence,
                details=details,
            )
        raw = [
            {
                "cell_x": cell.cell.x,
                "cell_y": cell.cell.y,
                "evidence_public_ids": [reference.public_id for reference in cell.evidence],
                "shares": [
                    {"count": share.count, "scope_key": share.scope_key, "share": share.share}
                    for share in cell.shares
                ],
                "total_count": cell.total_count,
            }
            for cell in result.cells
        ]
        if result.quality == "partial":
            return _partial_value(
                "map_control.observed_cell_presence_share",
                raw,
                context,
                window,
                cast(str, result.reason),
                evidence,
                details,
            )
        return complete_value(
            "map_control.observed_cell_presence_share",
            raw,
            context.scope,
            window,
            evidence,
            SPATIAL_REGISTRY,
            details=details,
        )


def _observation_key(item: ObservedEvidence) -> tuple[int, str, str, str]:
    return (
        item.frame if item.frame is not None else -1,
        item.ref.source_kind,
        item.ref.source_key,
        item.ref.public_id,
    )


def _segment_record(segment: MovementSegment) -> dict[str, object]:
    return {
        "algorithm_version": segment.route.algorithm_version,
        "cells": [{"x": cell.x, "y": cell.y} for cell in segment.route.cells],
        "distance_cells": segment.route.distance_cells,
        "distance_world": _canonical_float(segment.route.distance_world),
        "end_frame": segment.end_frame,
        "evidence_public_ids": [reference.public_id for reference in segment.evidence],
        "object_key": segment.object_key,
        "start_frame": segment.start_frame,
        "surface": segment.route.surface,
    }


def _cluster_record(cluster: EngagementCluster) -> dict[str, object]:
    return {
        "applied_damage_sum": _canonical_float(cluster.applied_damage_sum),
        "centroid": {
            "x": _canonical_float(cluster.centroid.x),
            "y": _canonical_float(cluster.centroid.y),
            "z": _canonical_float(cluster.centroid.z),
        },
        "cluster_key": cluster.cluster_key,
        "evidence_public_ids": [reference.public_id for reference in cluster.evidence],
        "first_frame": cluster.first_frame,
        "killing_blow_count": cluster.killing_blow_count,
        "last_frame": cluster.last_frame,
        "map_normalized_centroid": _normalized(cluster.normalized_centroid),
        "participant_scopes": list(cluster.participant_scopes),
        "transition_surfaces": list(cluster.transition_surfaces),
        "validated_zone_id": cluster.zone_id,
    }
