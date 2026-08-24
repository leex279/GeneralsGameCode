"""Closed base registry and pure plugin composition tests."""

from dataclasses import dataclass

import pytest

from generals_replay_analyzer.features.registry import BASE_REGISTRY, FeatureDefinition

EXPECTED_NAMES = (
    "activity.effective_actions_per_minute",
    "activity.supported_order_action_count",
    "activity.supported_order_coverage",
    "build.completed_count",
    "build.completed_sequence",
    "build.first_completed_frame",
    "combat.applied_damage_dealt",
    "combat.applied_damage_taken",
    "combat.killing_blow_count",
    "combat.observed_damage_trade_ratio",
    "combat.observed_kill_timing",
    "combat.turning_point_timing",
    "economy.cash_balance_final",
    "economy.cash_change_total",
    "economy.cash_per_minute_latest",
    "economy.cash_per_minute_peak",
    "economy.cash_per_minute_reconciled_share",
    "economy.cash_per_minute_series",
    "economy.supply_collected_total",
    "economy.supply_collection_rate",
    "economy.supply_source_resolved_share",
    "economy.tracked_income_total",
    "production.cancelled_count",
    "production.completed_composition",
    "production.completed_count",
    "production.observed_duration_frames",
    "production.queued_count",
    "production.science_purchase_timing",
    "production.special_power_timing",
    "scorekeeper.event_reconciliation",
    "scorekeeper.terminal_snapshot",
    "scouting.first_observed_clear_timing",
    "scouting.visibility_transition_count",
    "state.entity_transition_count",
    "state.final_result",
)


@dataclass(frozen=True)
class Plugin:
    plugin_name: str
    plugin_version: str
    registry_schema: str
    owned_namespaces: tuple[str, ...]
    definitions: tuple[FeatureDefinition, ...]


def _spatial_definition(name: str = "spatial.hotspot_count") -> FeatureDefinition:
    return FeatureDefinition(name, "integer", "count", ("player",), "inclusive", "observed", "spatial")


def test_base_registry_is_exact_closed_and_immutable() -> None:
    assert BASE_REGISTRY.schema_version == "feature-registry-v1"
    assert BASE_REGISTRY.names() == EXPECTED_NAMES
    assert BASE_REGISTRY.definition("economy.supply_collection_rate").unit == "credits_per_minute"
    with pytest.raises(KeyError):
        BASE_REGISTRY.definition("economy.worker_efficiency")


def test_plugin_composition_is_pure_and_supports_later_spatial_fanout() -> None:
    definitions = (
        FeatureDefinition(
            "army_route.observed_reachable_distance",
            "real",
            "engine_world_unit",
            ("player",),
            "inclusive",
            "observed",
            "army_route",
        ),
        FeatureDefinition(
            "engagement.observed_cluster_count",
            "integer",
            "count",
            ("player",),
            "inclusive",
            "observed",
            "engagement",
        ),
    )
    plugin = Plugin("spatial", "v1", "feature-registry-v1", ("army_route", "engagement"), definitions)
    composed = BASE_REGISTRY.with_plugin(plugin)
    assert BASE_REGISTRY.names() == EXPECTED_NAMES
    assert composed.names() == tuple(sorted(EXPECTED_NAMES + tuple(definition.name for definition in definitions)))
    assert composed.definition("army_route.observed_reachable_distance").unit == "engine_world_unit"


@pytest.mark.parametrize(
    "plugin, message",
    [
        (Plugin("spatial", "v1", "wrong", ("spatial",), (_spatial_definition(),)), "schema"),
        (
            Plugin(
                "build",
                "v1",
                "feature-registry-v1",
                ("build",),
                (FeatureDefinition("build.completed_count", "integer", "count", ("player",), "inclusive", "observed", "build"),),
            ),
            "duplicate",
        ),
        (
            Plugin("spatial", "v1", "feature-registry-v1", ("other",), (_spatial_definition(),)),
            "namespace",
        ),
        (
            Plugin(
                "spatial",
                "v1",
                "feature-registry-v1",
                ("spatial",),
                (_spatial_definition("spatial.z"), _spatial_definition("spatial.a")),
            ),
            "sorted",
        ),
    ],
)
def test_plugin_rejects_schema_duplicates_namespace_theft_and_unsorted_definitions(plugin: Plugin, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        BASE_REGISTRY.with_plugin(plugin)


@pytest.mark.parametrize(
    "arguments, message",
    [
        (("Bad", "integer", "count", ("player",), "inclusive", "observed", "Bad"), "lower-case"),
        (("x.value", "invalid", "count", ("player",), "inclusive", "observed", "x"), "value type"),
        (("x.value", "integer", "invalid", ("player",), "inclusive", "observed", "x"), "unit"),
        (("x.value", "integer", "count", (), "inclusive", "observed", "x"), "scope types"),
        (("x.value", "integer", "count", ("bad",), "inclusive", "observed", "x"), "scope"),
        (("x.value", "integer", "count", ("player",), "point", "observed", "x"), "window"),
        (("x.value", "integer", "count", ("player",), "inclusive", "guessed", "x"), "availability"),
        (("x.value", "integer", "count", ("player",), "inclusive", "observed", "other"), "namespace"),
    ],
)
def test_feature_definition_rejects_every_unstable_contract(arguments: tuple[object, ...], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        FeatureDefinition(*arguments)  # type: ignore[arg-type]


def test_registry_rejects_unsorted_or_wrong_schema_and_plugin_metadata() -> None:
    with pytest.raises(ValueError, match="sorted"):
        type(BASE_REGISTRY)("feature-registry-v1", tuple(reversed(BASE_REGISTRY.definitions)))
    with pytest.raises(ValueError, match="schema"):
        type(BASE_REGISTRY)("wrong", BASE_REGISTRY.definitions)
    with pytest.raises(ValueError, match="plugin name"):
        BASE_REGISTRY.with_plugin(Plugin("", "v1", "feature-registry-v1", (), ()))
    with pytest.raises(ValueError, match="plugin version"):
        BASE_REGISTRY.with_plugin(Plugin("spatial", "", "feature-registry-v1", (), ()))


@pytest.mark.parametrize(
    "owned_namespaces",
    [
        ("engagement", "army_route"),
        ("army_route", "army_route"),
        ("ArmyRoute",),
        ("army.route",),
    ],
)
def test_plugin_rejects_noncanonical_owned_namespace_sets(owned_namespaces: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="owned namespaces"):
        BASE_REGISTRY.with_plugin(
            Plugin("spatial", "v1", "feature-registry-v1", owned_namespaces, (_spatial_definition(),))
        )
