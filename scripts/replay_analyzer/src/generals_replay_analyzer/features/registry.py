"""Frozen Task 6 feature registry and pure plugin composition seam."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

ValueType: TypeAlias = Literal["integer", "real", "text", "boolean", "json"]
ScopeType: TypeAlias = Literal["replay", "player", "team", "entity"]

REGISTRY_SCHEMA = "feature-registry-v1"
_UNITS = {
    "actions_per_minute",
    "count",
    "credits",
    "credits_per_minute",
    "damage",
    "engine_world_unit",
    "frames",
    "json",
    "none",
    "percent",
    "ratio",
    "seconds",
}
_VALUE_TYPES = {"integer", "real", "text", "boolean", "json"}
_SCOPES = {"replay", "player", "team", "entity"}
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    value_type: ValueType
    unit: str
    scope_types: tuple[ScopeType, ...]
    window_policy: str
    availability_policy: str
    owner_namespace: str

    def __post_init__(self) -> None:
        if not _NAME_PATTERN.fullmatch(self.name):
            raise ValueError("feature name must be a lower-case dot path")
        if self.value_type not in _VALUE_TYPES:
            raise ValueError("unsupported feature value type")
        if self.unit not in _UNITS:
            raise ValueError("unsupported feature unit")
        if not self.scope_types or tuple(sorted(set(self.scope_types))) != self.scope_types:
            raise ValueError("feature scope types must be sorted and unique")
        if not set(self.scope_types) <= _SCOPES:
            raise ValueError("unsupported feature scope")
        if self.window_policy != "inclusive":
            raise ValueError("unsupported feature window policy")
        if self.availability_policy != "observed":
            raise ValueError("unsupported feature availability policy")
        if self.name.split(".", 1)[0] != self.owner_namespace:
            raise ValueError("feature namespace does not match its owner")


class FeaturePlugin(Protocol):
    plugin_name: str
    plugin_version: str
    registry_schema: str
    owned_namespaces: tuple[str, ...]
    definitions: tuple[FeatureDefinition, ...]


# TheSuperHackers @feature Leex 22/08/2026 Freeze deterministic feature names while allowing pure later plugin fanout. (#TBD)
@dataclass(frozen=True)
class FeatureRegistry:
    schema_version: str
    definitions: tuple[FeatureDefinition, ...]

    def __post_init__(self) -> None:
        names = tuple(definition.name for definition in self.definitions)
        if names != tuple(sorted(names)) or len(set(names)) != len(names):
            raise ValueError("registry definitions must be sorted and unique")
        if self.schema_version != REGISTRY_SCHEMA:
            raise ValueError("unsupported registry schema")

    def definition(self, name: str) -> FeatureDefinition:
        for definition in self.definitions:
            if definition.name == name:
                return definition
        raise KeyError(name)

    def names(self) -> tuple[str, ...]:
        return tuple(definition.name for definition in self.definitions)

    # TheSuperHackers @fix Leex 22/08/2026 Bind multi-namespace plugins to one explicit closed ownership set. (#TBD)
    def with_plugin(self, plugin: FeaturePlugin) -> FeatureRegistry:
        if plugin.registry_schema != self.schema_version:
            raise ValueError("plugin registry schema mismatch")
        if type(plugin.plugin_name) is not str or not plugin.plugin_name.strip():
            raise ValueError("plugin name must be stable")
        if type(plugin.plugin_version) is not str or not plugin.plugin_version.strip():
            raise ValueError("plugin version must be stable")
        owned_namespaces = plugin.owned_namespaces
        if (
            type(owned_namespaces) is not tuple
            or owned_namespaces != tuple(sorted(set(owned_namespaces)))
            or any(type(namespace) is not str or not _NAMESPACE_PATTERN.fullmatch(namespace) for namespace in owned_namespaces)
        ):
            raise ValueError("plugin owned namespaces must be sorted unique lower-case names")
        plugin_names = tuple(definition.name for definition in plugin.definitions)
        if plugin_names != tuple(sorted(plugin_names)):
            raise ValueError("plugin definitions must be sorted")
        if len(plugin_names) != len(set(plugin_names)):
            raise ValueError("plugin definitions must be unique")
        existing = set(self.names())
        for definition in plugin.definitions:
            if definition.name in existing:
                raise ValueError("duplicate feature name")
            prefix = definition.name.split(".", 1)[0]
            if prefix not in owned_namespaces or definition.owner_namespace not in owned_namespaces:
                raise ValueError("illegal plugin namespace ownership")
        return FeatureRegistry(self.schema_version, tuple(sorted(self.definitions + plugin.definitions, key=lambda item: item.name)))


def _definition(name: str, value_type: ValueType, unit: str) -> FeatureDefinition:
    namespace = name.split(".", 1)[0]
    return FeatureDefinition(name, value_type, unit, ("player",), "inclusive", "observed", namespace)


_BASE_DEFINITIONS = (
    _definition("activity.effective_actions_per_minute", "real", "actions_per_minute"),
    _definition("activity.supported_order_action_count", "integer", "count"),
    _definition("activity.supported_order_coverage", "json", "json"),
    _definition("build.completed_count", "integer", "count"),
    _definition("build.completed_sequence", "json", "json"),
    _definition("build.first_completed_frame", "integer", "frames"),
    _definition("combat.applied_damage_dealt", "real", "damage"),
    _definition("combat.applied_damage_taken", "real", "damage"),
    _definition("combat.killing_blow_count", "integer", "count"),
    _definition("combat.observed_damage_trade_ratio", "real", "ratio"),
    _definition("economy.cash_balance_final", "real", "credits"),
    _definition("economy.cash_change_total", "real", "credits"),
    _definition("economy.cash_per_minute_latest", "integer", "credits_per_minute"),
    _definition("economy.cash_per_minute_peak", "integer", "credits_per_minute"),
    _definition("economy.cash_per_minute_reconciled_share", "real", "ratio"),
    _definition("economy.cash_per_minute_series", "json", "json"),
    _definition("economy.supply_collected_total", "real", "credits"),
    _definition("economy.supply_collection_rate", "real", "credits_per_minute"),
    _definition("economy.supply_source_resolved_share", "real", "ratio"),
    _definition("economy.tracked_income_total", "real", "credits"),
    _definition("production.cancelled_count", "integer", "count"),
    _definition("production.completed_composition", "json", "json"),
    _definition("production.completed_count", "integer", "count"),
    _definition("production.observed_duration_frames", "json", "frames"),
    _definition("production.queued_count", "integer", "count"),
    _definition("production.science_purchase_timing", "json", "json"),
    _definition("production.special_power_timing", "json", "json"),
    _definition("scorekeeper.event_reconciliation", "json", "json"),
    _definition("scorekeeper.terminal_snapshot", "json", "json"),
    _definition("state.entity_transition_count", "integer", "count"),
    _definition("state.final_result", "json", "json"),
)

BASE_REGISTRY = FeatureRegistry(REGISTRY_SCHEMA, _BASE_DEFINITIONS)
