"""Canonical strategy taxonomy contract tests."""

import copy
import dataclasses
import hashlib
import json
from collections.abc import Callable
from importlib.resources import files
from typing import Any

import pytest

from generals_replay_analyzer.features.context import canonical_json
from generals_replay_analyzer.features.registry import FeatureRegistry
from generals_replay_analyzer.strategy.taxonomy import TaxonomyValidationError, default_taxonomy, load_taxonomy

from .conftest import MemoryResource


@pytest.mark.parametrize(
    "resource_name",
    ("strategy-taxonomy-v1.json", "strategy-taxonomy-v1.schema.json"),
)
def test_package_taxonomy_resources_are_canonical_json(resource_name: str) -> None:
    payload = files("generals_replay_analyzer").joinpath("data", resource_name).read_bytes()

    assert payload == canonical_json(json.loads(payload)).encode("utf-8")


def test_default_taxonomy_exposes_only_the_conservative_fallback(registry: FeatureRegistry) -> None:
    taxonomy = default_taxonomy(registry)

    assert tuple(item.strategy_id for item in taxonomy.strategies) == ("unknown_or_mixed",)
    assert taxonomy.strategies[0].fallback is True
    assert taxonomy.strategies[0].phase == "cross_phase"
    assert taxonomy.strategies[0].applicability.faction_template_names == ("*",)
    assert "not established" in taxonomy.strategies[0].display_name
    assert len(taxonomy.content_sha256) == 64


def test_valid_taxonomy_loads_as_frozen_values_with_a_semantic_digest(
    registry: FeatureRegistry,
    taxonomy_document: dict[str, Any],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
) -> None:
    taxonomy = load_taxonomy(taxonomy_resource(), registry)

    expected_digest = hashlib.sha256(canonical_json(taxonomy_document).encode()).hexdigest()
    assert taxonomy.schema_version == "strategy-taxonomy-v1"
    assert taxonomy.taxonomy_version == "strategy-taxonomy-v1.0.0"
    assert taxonomy.content_sha256 == expected_digest
    assert tuple(item.strategy_id for item in taxonomy.strategies) == (
        "catalog_proven_pressure",
        "unknown_or_mixed",
    )
    assert taxonomy.strategies[0].required[0].weight == 3
    with pytest.raises(dataclasses.FrozenInstanceError):
        taxonomy.strategies[0].display_name = "mutable"  # type: ignore[misc]


def test_loader_rejects_missing_and_malformed_resources(registry: FeatureRegistry) -> None:
    class MissingResource:
        name = "missing.json"

        def read_bytes(self) -> bytes:
            raise FileNotFoundError(self.name)

    with pytest.raises(TaxonomyValidationError, match="resource"):
        load_taxonomy(MissingResource(), registry)  # type: ignore[arg-type]
    with pytest.raises(TaxonomyValidationError, match="JSON"):
        load_taxonomy(MemoryResource(b"{"), registry)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(extra=True), "schema"),
        (lambda value: value.update(schema_version="strategy-taxonomy-v2"), "schema"),
        (lambda value: value.update(taxonomy_version="INVALID VERSION"), "version"),
        (lambda value: value["strategies"].reverse(), "sorted"),
        (lambda value: value["strategies"].append(copy.deepcopy(value["strategies"][0])), "unique"),
        (lambda value: value["strategies"][1].update(fallback=False), "fallback"),
        (lambda value: value["strategies"][0].update(phase="rush"), "schema"),
        (lambda value: value["strategies"][0]["required"][0].update(operator="between"), "schema"),
        (lambda value: value["strategies"][0]["required"][0].update(feature_name="unknown.feature"), "registry"),
        (lambda value: value["strategies"][0]["required"][0].update(unit="count"), "unit"),
        (lambda value: value["strategies"][0]["required"][0].update(expected_value="100"), "value type"),
        (lambda value: value["strategies"][0]["required"][0].update(allowed_scope_types=["replay"]), "scope"),
        (
            lambda value: value["strategies"][0]["required"][0].update(
                feature_name="build.completed_sequence", unit="json"
            ),
            "JSON",
        ),
        (lambda value: value["strategies"][0]["required"][0].update(weight=0), "weight"),
        (lambda value: value["strategies"][0]["required"][0].update(weight=-1), "weight"),
        (lambda value: value["strategies"][0]["required"][0].update(expected_value=-0.0), "negative zero"),
        (lambda value: value["strategies"][0]["required"][0].update(predicate_id="INVALID"), "identifier"),
        (
            lambda value: value["strategies"][0]["required"].append(
                copy.deepcopy(value["strategies"][0]["required"][0])
            ),
            "unique",
        ),
        (lambda value: value["strategies"][0]["supporting"][0].update(predicate_id="supply_floor"), "predicate"),
        (
            lambda value: value["strategies"][0]["supporting"][0].update(
                feature_name="economy.supply_collected_total", unit="credits", expected_value=50.0
            ),
            "selector",
        ),
        (lambda value: value["strategies"][0]["applicability"].update(map_identities=["*"]), "wildcard"),
        (
            lambda value: value["strategies"][1]["required"].append(
                copy.deepcopy(value["strategies"][0]["required"][0])
            ),
            "fallback",
        ),
        (lambda value: value["strategies"][0].update(required=[]), "required"),
        (
            lambda value: value["strategies"][1]["applicability"].update(map_identities=["maps/test/map.ini"]),
            "fallback",
        ),
        (lambda value: value["strategies"][0].update(synonyms=["z", "a"]), "sorted"),
        (lambda value: value["strategies"][0].update(synonyms=["same", "same"]), "unique"),
        (
            lambda value: value["strategies"][0]["required"][0].update(
                feature_name="state.final_result", unit="json", expected_value="won", operator="contains"
            ),
            "JSON",
        ),
    ],
)
def test_semantically_invalid_taxonomies_are_rejected(
    registry: FeatureRegistry,
    taxonomy_document: dict[str, Any],
    taxonomy_resource: Callable[[dict[str, Any] | bytes | None], MemoryResource],
    mutation: Callable[[dict[str, Any]], object],
    message: str,
) -> None:
    invalid = copy.deepcopy(taxonomy_document)
    mutation(invalid)

    with pytest.raises(TaxonomyValidationError, match=message):
        load_taxonomy(taxonomy_resource(invalid), registry)


def test_nonfinite_threshold_is_rejected_before_json_schema(
    registry: FeatureRegistry,
    taxonomy_document: dict[str, Any],
) -> None:
    invalid = copy.deepcopy(taxonomy_document)
    invalid["strategies"][0]["required"][0]["expected_value"] = float("inf")
    payload = json.dumps(invalid, sort_keys=True, separators=(",", ":"), allow_nan=True).encode()

    with pytest.raises(TaxonomyValidationError, match="finite"):
        load_taxonomy(MemoryResource(payload), registry)  # type: ignore[arg-type]
