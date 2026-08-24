"""Canonical semantic context and cache identity tests."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from generals_replay_analyzer.features.base import FeatureScope
from generals_replay_analyzer.features.context import FeatureContext, cache_key, canonical_json, input_digest
from generals_replay_analyzer.features.evidence import EvidenceRef, ObservedEvidence


def _observation(public_id: str, source_key: str, frame: int, facts: object) -> ObservedEvidence:
    return ObservedEvidence(
        EvidenceRef(public_id, "observed", "telemetry", source_key, "telemetry-v2"), frame, "fixture", facts
    )


def _context(*, observed: tuple[ObservedEvidence, ...], settings: object = ()) -> FeatureContext:
    player = "00000000-0000-4000-8000-000000000231"
    return FeatureContext(
        cache_schema="feature-context-v1",
        replay_public_id="00000000-0000-4000-8000-000000000230",
        replay_sha256="a" * 64,
        replay_player_public_id=player,
        scope=FeatureScope("player", player, player),
        observation_schema_versions=(("telemetry", "telemetry-v2"),),
        parser_completion_status="complete",
        telemetry_status="succeeded",
        final_frame=300,
        logic_frames_per_second=30,
        catalog_identity="catalog-v1:abc",
        observed=observed,
        settings=settings,
    )


def test_context_sorts_observations_and_canonical_json_ignores_semantic_insertion_order() -> None:
    first = _observation("00000000-0000-4000-8000-000000000232", "telemetry:z", 10, {"b": 2, "a": 1})
    second = _observation("00000000-0000-4000-8000-000000000233", "telemetry:a", 10, {"x": [2, 1]})
    left = _context(observed=(first, second), settings={"z": 1, "a": {"b": 2, "a": 1}})
    right = _context(observed=(second, first), settings={"a": {"a": 1, "b": 2}, "z": 1})
    assert [item.ref.source_key for item in left.observed] == ["telemetry:a", "telemetry:z"]
    assert canonical_json(left) == canonical_json(right)
    assert input_digest(left) == input_digest(right)
    assert cache_key(left, "fixture", "v1") == cache_key(right, "fixture", "v1")


def test_cache_identity_changes_for_every_semantic_input_and_version() -> None:
    item = _observation("00000000-0000-4000-8000-000000000234", "telemetry:1", 10, {"amount": 1.25})
    base = _context(observed=(item,))
    base_key = cache_key(base, "fixture", "v1")
    variants = (
        replace(base, replay_sha256="b" * 64),
        replace(base, final_frame=301),
        replace(base, logic_frames_per_second=60),
        replace(base, catalog_identity="catalog-v1:def"),
        replace(base, settings=(("policy", "v2"),)),
        replace(base, observed=(replace(item, facts={"amount": 1.2500000000001}),)),
        replace(base, observation_schema_versions=(("telemetry", "telemetry-v3"),)),
    )
    assert all(cache_key(item, "fixture", "v1") != base_key for item in variants)
    assert cache_key(base, "fixture", "v2") != base_key
    assert cache_key(base, "fixture", "v1", registry_schema="feature-registry-v2") != base_key


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), -0.0, b"bytes", Path("fixture.rep"), datetime.now(UTC), {"database_id": 1}],
)
def test_canonical_json_rejects_nonsemantic_or_ambiguous_values(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_json(value)


def test_context_rejects_bad_digest_duplicates_nonobserved_inputs_and_invalid_frames() -> None:
    item = _observation("00000000-0000-4000-8000-000000000235", "telemetry:1", 10, {})
    with pytest.raises(ValueError, match="sha256"):
        replace(_context(observed=(item,)), replay_sha256="A" * 64)
    with pytest.raises(ValueError, match="duplicate public"):
        _context(observed=(item, replace(item, ref=replace(item.ref, source_key="telemetry:2"))))
    with pytest.raises(ValueError, match="duplicate source"):
        _context(
            observed=(item, replace(item, ref=replace(item.ref, public_id="00000000-0000-4000-8000-000000000236")))
        )
    with pytest.raises(ValueError, match="observed"):
        _context(observed=(replace(item, ref=replace(item.ref, tier="inferred")),))
    with pytest.raises(ValueError, match="frame"):
        _context(observed=(replace(item, frame=-1),))


def test_context_rejects_scope_schema_and_terminal_shape_and_canonicalizes_sets() -> None:
    item = _observation("00000000-0000-4000-8000-000000000237", "telemetry:3", 10, {})
    base = _context(observed=(item,))
    with pytest.raises(ValueError, match="context schema"):
        replace(base, cache_schema="wrong")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="final_frame"):
        replace(base, final_frame=-1)
    with pytest.raises(ValueError, match="logic_frames_per_second"):
        replace(base, logic_frames_per_second=45)
    with pytest.raises(ValueError, match="schema family"):
        replace(base, observation_schema_versions=(("telemetry", "v2"), ("telemetry", "v2")))
    assert canonical_json({"values": {3, 1, 2}}) == '{"values":[1,2,3]}'
    with pytest.raises(TypeError, match="mapping keys"):
        canonical_json({1: "bad"})

    class FakeOrm:
        _sa_instance_state = object()

    with pytest.raises(TypeError, match="ORM"):
        canonical_json(FakeOrm())
