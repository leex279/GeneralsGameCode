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
    "economy.cash_balance_final",
    "economy.cash_change_total",
    "economy.supply_collected_total",
    "economy.supply_collection_rate",
    "economy.supply_source_resolved_share",
    "economy.tracked_income_total",
    "production.cancelled_count",
    "production.completed_composition",
    "production.completed_count",
    "production.observed_duration_frames",
    "production.queued_count",
    "state.entity_transition_count",
    "state.final_result",
)


@dataclass(frozen=True)
class Plugin:
    plugin_name: str
    plugin_version: str
    registry_schema: str
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
    plugin = Plugin("spatial", "v1", "feature-registry-v1", (_spatial_definition(),))
    composed = BASE_REGISTRY.with_plugin(plugin)
    assert BASE_REGISTRY.names() == EXPECTED_NAMES
    assert composed.names() == tuple(sorted(EXPECTED_NAMES + ("spatial.hotspot_count",)))
    assert composed.definition("spatial.hotspot_count").owner_namespace == "spatial"


@pytest.mark.parametrize(
    "plugin, message",
    [
        (Plugin("spatial", "v1", "wrong", (_spatial_definition(),)), "schema"),
        (
            Plugin(
                "build",
                "v1",
                "feature-registry-v1",
                (FeatureDefinition("build.completed_count", "integer", "count", ("player",), "inclusive", "observed", "build"),),
            ),
            "duplicate",
        ),
        (
            Plugin("other", "v1", "feature-registry-v1", (_spatial_definition(),)),
            "namespace",
        ),
        (
            Plugin(
                "spatial",
                "v1",
                "feature-registry-v1",
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
        BASE_REGISTRY.with_plugin(Plugin("", "v1", "feature-registry-v1", ()))
    with pytest.raises(ValueError, match="plugin version"):
        BASE_REGISTRY.with_plugin(Plugin("spatial", "", "feature-registry-v1", ()))
