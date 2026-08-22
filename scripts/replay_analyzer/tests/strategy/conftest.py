"""Pure immutable strategy fixtures."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from generals_replay_analyzer.features.base import FeatureScope, FeatureValue, FeatureWindow
from generals_replay_analyzer.features.evidence import DerivedEvidence, EvidenceRef
from generals_replay_analyzer.features.registry import BASE_REGISTRY, FeatureRegistry


@dataclass(frozen=True)
class MemoryResource:
    """Small read-only Traversable-shaped object for loader unit tests."""

    payload: bytes
    name: str = "strategy-taxonomy-v1.json"

    def read_bytes(self) -> bytes:
        return self.payload

    def read_text(self, encoding: str | None = None, errors: str | None = None) -> str:
        return self.payload.decode(encoding or "utf-8", errors or "strict")


@pytest.fixture
def taxonomy_document() -> dict[str, Any]:
    path = Path(__file__).parent / "fixtures" / "strategy-taxonomy-v1-valid.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def taxonomy_resource(taxonomy_document: dict[str, Any]) -> Callable[[dict[str, Any] | bytes | None], MemoryResource]:
    def build(value: dict[str, Any] | bytes | None = None) -> MemoryResource:
        payload = taxonomy_document if value is None else value
        if isinstance(payload, bytes):
            return MemoryResource(payload)
        return MemoryResource(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        )

    return build


@pytest.fixture
def registry() -> FeatureRegistry:
    return BASE_REGISTRY


@pytest.fixture
def evidence_ref() -> Callable[..., EvidenceRef]:
    def build(
        sequence: int,
        *,
        tier: str = "observed",
        source_kind: str | None = None,
        source_key: str | None = None,
    ) -> EvidenceRef:
        return EvidenceRef(
            public_id=f"00000000-0000-4000-8000-{sequence:012d}",
            tier=tier,  # type: ignore[arg-type]
            source_kind=source_kind or tier,
            source_key=source_key or f"{tier}:fixture:{sequence}",
            schema_version=f"{tier}-v1",
        )

    return build


@pytest.fixture
def strategy_feature(evidence_ref: Callable[..., EvidenceRef]) -> Callable[..., object]:
    def build(
        *,
        name: str = "economy.supply_collected_total",
        raw_value: object = 150.0,
        unit: str = "credits",
        quality: str = "complete",
        frame_start: int = 0,
        frame_end: int = 300,
        sequence: int = 1,
    ) -> object:
        from generals_replay_analyzer.strategy.rules import StrategyFeature

        observed = evidence_ref(sequence)
        derived_ref = evidence_ref(
            sequence + 100,
            tier="derived",
            source_kind="feature",
            source_key=f"feature:fixture:{sequence}",
        )
        definition = BASE_REGISTRY.definition(name)
        value = FeatureValue(
            name=name,
            value_type=definition.value_type,
            raw_value=raw_value,
            unit=unit,
            scope=FeatureScope(
                "player",
                "00000000-0000-4000-8000-000000000250",
                "00000000-0000-4000-8000-000000000250",
            ),
            window=FeatureWindow(frame_start, frame_end),
            quality=quality,  # type: ignore[arg-type]
            quality_reason=None if quality == "complete" else "fixture_partial",
            input_evidence=(observed,),
        )
        return StrategyFeature(
            value=value,
            derived_evidence=DerivedEvidence(
                ref=derived_ref,
                extractor_name="fixture",
                extractor_version="fixture-v1",
                input_evidence_ids=(observed.public_id,),
            ),
        )

    return build
