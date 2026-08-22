"""Strict closed codecs for direct analysis-stage outputs."""

from __future__ import annotations

import json

import pytest

from generals_replay_analyzer.analysis_pipeline.codecs import (
    PipelineCodecError,
    PlayerAssessmentSelection,
    PlayerFeatureSelection,
    PlayerLLMSelection,
    decode_assessment_output,
    decode_bundle,
    decode_feature_output,
    decode_llm_output,
    encode_assessment_output,
    encode_bundle,
    encode_feature_output,
    encode_llm_output,
)
from generals_replay_analyzer.llm.evidence_bundle import EvidenceClaim, build_evidence_bundle

REPLAY_ID = "00000000-0000-4000-8000-000000000201"
REPLAY_SHA = "a" * 64


def test_feature_codec_preserves_sorted_unique_public_player_selections() -> None:
    second = PlayerFeatureSelection(
        "00000000-0000-4000-8000-000000000202",
        None,
        ("00000000-0000-4000-8000-000000000212",),
    )
    first = PlayerFeatureSelection(
        "00000000-0000-4000-8000-000000000201",
        "00000000-0000-4000-8000-000000000221",
        ("00000000-0000-4000-8000-000000000211",),
    )

    encoded = encode_feature_output((second, first))

    assert decode_feature_output(encoded) == (first, second)


@pytest.mark.parametrize(
    "damage",
    [
        {"schema_version": "feature-output-v1", "players": [], "unknown": True},
        {
            "schema_version": "feature-output-v1",
            "players": [
                {
                    "replay_player_public_id": "00000000-0000-4000-8000-000000000201",
                    "canonical_player_public_id": None,
                    "feature_set_public_ids": [
                        "00000000-0000-4000-8000-000000000212",
                        "00000000-0000-4000-8000-000000000211",
                    ],
                }
            ],
        },
        {
            "schema_version": "feature-output-v1",
            "players": [
                {
                    "replay_player_public_id": "C:\\private\\player",
                    "canonical_player_public_id": None,
                    "feature_set_public_ids": [],
                }
            ],
        },
    ],
)
def test_feature_codec_rejects_unknown_unsorted_and_pathlike_data(
    damage: dict[str, object],
) -> None:
    with pytest.raises(PipelineCodecError):
        decode_feature_output(damage)


def test_assessment_codec_round_trips_exact_bundle_identity() -> None:
    bundle = build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=())
    selection = PlayerAssessmentSelection(
        None,
        None,
        ("00000000-0000-4000-8000-000000000231",),
        "b" * 64,
        "not_applicable",
        None,
        bundle,
    )

    assert decode_assessment_output(encode_assessment_output((selection,))) == (selection,)


def test_output_dtos_reject_noncanonical_values_at_construction() -> None:
    bundle = build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=())
    with pytest.raises(PipelineCodecError):
        PlayerAssessmentSelection(None, None, (), "not-a-digest", "not_applicable", None, bundle)
    with pytest.raises(PipelineCodecError):
        PlayerLLMSelection(None, "NOT-A-UUID", "unavailable", "model_unavailable")
    with pytest.raises(PipelineCodecError):
        PlayerLLMSelection(None, 123, "unavailable", "model_unavailable")  # type: ignore[arg-type]
    with pytest.raises(PipelineCodecError):
        PlayerLLMSelection(
            None,
            "AAAAAAAA-0000-4000-8000-000000000251",
            "unavailable",
            "model_unavailable",
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda bundle: PlayerFeatureSelection(None, None, ()),
        lambda bundle: PlayerAssessmentSelection(
            None,
            None,
            ("00000000-0000-4000-8000-000000000231",),
            "b" * 64,
            "invalid",  # type: ignore[arg-type]
            None,
            bundle,
        ),
        lambda bundle: PlayerAssessmentSelection(
            None,
            None,
            ("00000000-0000-4000-8000-000000000231",),
            "b" * 64,
            "not_applicable",
            "00000000-0000-4000-8000-000000000232",
            bundle,
        ),
        lambda bundle: PlayerAssessmentSelection(
            None,
            None,
            ("00000000-0000-4000-8000-000000000231",),
            "b" * 64,
            "not_applicable",
            None,
            object(),  # type: ignore[arg-type]
        ),
        lambda bundle: PlayerLLMSelection(None, "00000000-0000-4000-8000-000000000251", "unknown", "ok"),
        lambda bundle: PlayerLLMSelection(None, "00000000-0000-4000-8000-000000000251", "succeeded", "NOT-SAFE"),
    ],
)
def test_output_dtos_reject_each_closed_contract_violation(factory: object) -> None:
    bundle = build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=())
    with pytest.raises(PipelineCodecError):
        factory(bundle)  # type: ignore[operator]


def test_feature_codec_rejects_duplicate_encode_and_unsorted_players() -> None:
    first = PlayerFeatureSelection(
        "00000000-0000-4000-8000-000000000201",
        None,
        ("00000000-0000-4000-8000-000000000211",),
    )
    second = PlayerFeatureSelection(
        "00000000-0000-4000-8000-000000000202",
        None,
        ("00000000-0000-4000-8000-000000000212",),
    )
    with pytest.raises(PipelineCodecError):
        encode_feature_output((first, first))
    reversed_players = encode_feature_output((first, second))
    reversed_players["players"].reverse()  # type: ignore[union-attr]
    with pytest.raises(PipelineCodecError):
        decode_feature_output(reversed_players)


@pytest.mark.parametrize(
    "decoder,value",
    [
        (decode_feature_output, {"schema_version": "feature-output-v1", "players": "not-an-array"}),
        (decode_assessment_output, {"schema_version": "wrong", "players": []}),
        (decode_llm_output, {"schema_version": "wrong", "players": []}),
        (
            decode_llm_output,
            {
                "schema_version": "llm-output-v1",
                "players": [
                    {
                        "replay_player_public_id": None,
                        "analysis_run_id": "00000000-0000-4000-8000-000000000251",
                        "llm_status": "unknown",
                        "code": "ok",
                    }
                ],
            },
        ),
    ],
)
def test_codecs_reject_wrong_arrays_schemas_and_statuses(decoder: object, value: object) -> None:
    with pytest.raises(PipelineCodecError):
        decoder(value)  # type: ignore[operator]


def _bundle_with_text(text: str):
    claim = EvidenceClaim._create(
        "safe-claim",
        "replay_context",
        {"note": text},
        "complete",
        None,
        ("00000000-0000-4000-8000-000000000299",),
    )
    return build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=(claim,))


@pytest.mark.parametrize(
    "locator",
    [
        "/tmp/private/replay.rep",
        "source:/home/private/replay.rep",
        "source:%2Fhome%2Fprivate%2Freplay.rep",
        "s3://private-bucket/replay.rep",
        "s3%3A%2F%2Fprivate-bucket%2Freplay.rep",
        r"C:\private\replay.rep",
        r"\\server\share\replay.rep",
        "../private/replay.rep",
        "docs/../../private/replay.rep",
        "docs%2F..%2Fprivate%2Freplay.rep",
        "file:///tmp/private/replay.rep",
        "https://example.invalid/private/replay.rep",
    ],
)
def test_bundle_codec_rejects_locators_on_encode_and_decode(locator: str) -> None:
    bundle = _bundle_with_text(locator)
    unvalidated = json.loads(bundle.canonical_json)
    unvalidated["digest"] = bundle.digest

    with pytest.raises(PipelineCodecError, match="path-like"):
        encode_bundle(bundle)
    with pytest.raises(PipelineCodecError, match="path-like"):
        decode_bundle(unvalidated)


@pytest.mark.parametrize(
    "prose",
    [
        "Ordinary prose may mention a 1/2 ratio.",
        "The evidence group is analysis/features and drive C: is named.",
        "Ordinary prose: use / as the separator.",
    ],
)
def test_bundle_codec_preserves_benign_prose(prose: str) -> None:
    bundle = _bundle_with_text(prose)

    assert decode_bundle(encode_bundle(bundle)) == bundle


@pytest.mark.parametrize(
    "selection",
    [
        lambda bundle: PlayerFeatureSelection(
            None,
            "00000000-0000-4000-8000-000000000221",
            ("00000000-0000-4000-8000-000000000211",),
        ),
        lambda bundle: PlayerAssessmentSelection(
            None,
            "00000000-0000-4000-8000-000000000221",
            ("00000000-0000-4000-8000-000000000211",),
            "b" * 64,
            "not_applicable",
            None,
            bundle,
        ),
        lambda bundle: PlayerAssessmentSelection(
            "00000000-0000-4000-8000-000000000201",
            None,
            ("00000000-0000-4000-8000-000000000211",),
            "b" * 64,
            "not_applicable",
            None,
            bundle,
        ),
        lambda bundle: PlayerAssessmentSelection(
            "00000000-0000-4000-8000-000000000201",
            "00000000-0000-4000-8000-000000000221",
            ("00000000-0000-4000-8000-000000000211",),
            "b" * 64,
            "canonical_player_unresolved",
            None,
            bundle,
        ),
        lambda bundle: PlayerAssessmentSelection(
            None,
            None,
            ("00000000-0000-4000-8000-000000000211",),
            "b" * 64,
            "unavailable",
            None,
            bundle,
        ),
        lambda bundle: PlayerAssessmentSelection(
            "00000000-0000-4000-8000-000000000201",
            "00000000-0000-4000-8000-000000000221",
            ("00000000-0000-4000-8000-000000000211",),
            "b" * 64,
            "not_applicable",
            None,
            bundle,
        ),
    ],
)
def test_player_selections_reject_cross_field_identity_contradictions(selection: object) -> None:
    bundle = build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=())

    with pytest.raises(PipelineCodecError):
        selection(bundle)  # type: ignore[operator]


def test_assessment_selection_accepts_each_exact_player_status_shape() -> None:
    bundle = build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=())
    feature_sets = ("00000000-0000-4000-8000-000000000211",)
    replay_player = "00000000-0000-4000-8000-000000000201"
    canonical_player = "00000000-0000-4000-8000-000000000221"
    run_id = "00000000-0000-4000-8000-000000000231"

    assert PlayerAssessmentSelection(None, None, feature_sets, "b" * 64, "not_applicable", None, bundle)
    assert PlayerAssessmentSelection(
        replay_player,
        None,
        feature_sets,
        "b" * 64,
        "canonical_player_unresolved",
        None,
        bundle,
    )
    assert PlayerAssessmentSelection(
        replay_player,
        canonical_player,
        feature_sets,
        "b" * 64,
        "unavailable",
        None,
        bundle,
    )
    assert PlayerAssessmentSelection(
        replay_player,
        canonical_player,
        feature_sets,
        "b" * 64,
        "succeeded",
        run_id,
        bundle,
    )


def test_assessment_codec_rejects_duplicate_player_and_changed_bundle_digest() -> None:
    bundle = build_evidence_bundle(replay_public_id=REPLAY_ID, replay_sha256=REPLAY_SHA, claims=())
    selection = PlayerAssessmentSelection(
        None,
        None,
        ("00000000-0000-4000-8000-000000000231",),
        "b" * 64,
        "not_applicable",
        None,
        bundle,
    )
    duplicate = encode_assessment_output((selection, selection))
    damaged = encode_assessment_output((selection,))
    damaged["players"][0]["evidence_bundle"]["digest"] = "c" * 64  # type: ignore[index]

    with pytest.raises(PipelineCodecError):
        decode_assessment_output(duplicate)
    with pytest.raises(PipelineCodecError):
        decode_assessment_output(damaged)


def test_llm_codec_rejects_unsorted_duplicate_and_pathlike_data() -> None:
    second = PlayerLLMSelection(
        "00000000-0000-4000-8000-000000000242",
        "00000000-0000-4000-8000-000000000252",
        "unavailable",
        "model_unavailable",
    )
    first = PlayerLLMSelection(
        "00000000-0000-4000-8000-000000000241",
        "00000000-0000-4000-8000-000000000251",
        "succeeded",
        "ok",
    )
    encoded = encode_llm_output((second, first))
    assert decode_llm_output(encoded) == (first, second)

    duplicate = encode_llm_output((first, first))
    pathlike = encode_llm_output((first,))
    pathlike["players"][0]["code"] = "C:\\private"  # type: ignore[index]
    with pytest.raises(PipelineCodecError):
        decode_llm_output(duplicate)
    with pytest.raises(PipelineCodecError):
        decode_llm_output(pathlike)
