from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import pytest

from generals_replay_analyzer.features.evidence import thaw_canonical
from generals_replay_analyzer.report.model import (
    OllamaReportStatus,
    ReportDocument,
    ReportEvidenceRef,
    ReportLifecycle,
    ReportValue,
)
from generals_replay_analyzer.report.read_model import (
    PublishedReportAssetDTO,
    PublishedReportDTO,
    PublishedReportGraphDTO,
    ReportPlayerIdentityDTO,
    ReportReplayIdentityDTO,
)
from generals_replay_analyzer.spatial.query import MapSceneReadModel
from generals_replay_analyzer.video.camera import CameraPlanContractError, CameraPlanService, _citations
from generals_replay_analyzer.video.commentary import CommentaryPlanService
from generals_replay_analyzer.video.contracts import CameraPlanAuthorityV1, EvidenceHorizonV1
from generals_replay_analyzer.web.ports import MapSceneDTO

REPLAY_ID = "10000000-0000-4000-8000-000000000001"
REPORT_ID = "20000000-0000-4000-8000-000000000001"
PLAYER_REPORT_ID = "20000000-0000-4000-8000-000000000002"
RUN_ID = "30000000-0000-4000-8000-000000000001"
MAP_ID = "40000000-0000-4000-8000-000000000001"
PLAYER_ID = "70000000-0000-4000-8000-000000000001"
EVIDENCE_START = "50000000-0000-4000-8000-000000000001"
EVIDENCE_FIGHT = "50000000-0000-4000-8000-000000000002"
EVIDENCE_STRUCTURE_CREATED = "50000000-0000-4000-8000-000000000003"
EVIDENCE_STRUCTURE_COMPLETED = "50000000-0000-4000-8000-000000000004"
EVIDENCE_ATTACKER_POSITION = "50000000-0000-4000-8000-000000000005"
EVIDENCE_LATE_DAMAGE = "50000000-0000-4000-8000-000000000006"


def _authority(**updates: object) -> CameraPlanAuthorityV1:
    values: dict[str, object] = {
        "replay_public_id": REPLAY_ID,
        "replay_sha256": "a" * 64,
        "report_public_id": REPORT_ID,
        "telemetry_run_public_id": RUN_ID,
        "telemetry_trace_sha256": "b" * 64,
        "map_public_id": MAP_ID,
        "map_content_sha256": "c" * 64,
        "evidence_horizon": EvidenceHorizonV1(frame_start=0, frame_end=300),
        "logic_frames_per_second": 30,
    }
    values.update(updates)
    return CameraPlanAuthorityV1.model_validate(values)


def _report() -> PublishedReportGraphDTO:
    evidence = (
        ReportValue(
            claim_id="map.start",
            section="map",
            label="Map start",
            raw_value={"known": True},
            unit=None,
            availability="available",
            unavailable_reason=None,
            scope={},
            frame_window=(0, 0),
            evidence=(ReportEvidenceRef(EVIDENCE_START, "observed"),),
            details={},
        ),
        ReportValue(
            claim_id="combat.engagement",
            section="combat",
            label="Major engagement",
            raw_value={"damage": 500},
            unit=None,
            availability="available",
            unavailable_reason=None,
            scope={},
            frame_window=(120, 150),
            evidence=(ReportEvidenceRef(EVIDENCE_FIGHT, "observed"),),
            details={},
        ),
    )
    document = ReportDocument(
        schema_version="replay-report-v1",
        report_public_id=REPORT_ID,
        report_version="replay-report-v1",
        input_digest="d" * 64,
        cache_key="e" * 64,
        replay_public_id=REPLAY_ID,
        replay_sha256="a" * 64,
        replay_player_public_id=None,
        lifecycle=ReportLifecycle("complete", "complete", "complete", "complete"),
        evidence_availability=(),
        quality_issues=(),
        observed=evidence,
        derived=(),
        inferred=(),
        ollama=OllamaReportStatus.not_requested(),
        warnings=(),
    )
    asset = PublishedReportAssetDTO(
        public_id="80000000-0000-4000-8000-000000000001",
        sha256="f" * 64,
        kind="report_structured_json",
        media_type="application/json",
        size_bytes=2,
    )
    presentation = PublishedReportAssetDTO(
        public_id="80000000-0000-4000-8000-000000000002",
        sha256="1" * 64,
        kind="report_presentation_bundle",
        media_type="application/json",
        size_bytes=2,
    )
    published = PublishedReportDTO(document, asset, presentation, "<p>report</p>", "report", datetime.now(UTC))
    return PublishedReportGraphDTO(
        "replay-report-read-model-v1",
        "report-output-v1",
        REPLAY_ID,
        REPORT_ID,
        ReportReplayIdentityDTO(
            "Replay", "Tournament Desert", "1.04", 301,
            (ReportPlayerIdentityDTO(PLAYER_ID, "Player", 1, "USA", "won"),),
        ),
        published,
        (),
    )


def _player_selected_report() -> PublishedReportGraphDTO:
    graph = _report()
    player_document = replace(
        graph.replay_wide.document,
        report_public_id=PLAYER_REPORT_ID,
        replay_player_public_id=PLAYER_ID,
        observed=tuple(
            value for value in graph.replay_wide.document.observed if value.claim_id != "map.start"
        ),
    )
    player_report = PublishedReportDTO(
        player_document,
        graph.replay_wide.structured_asset,
        graph.replay_wide.presentation_asset,
        graph.replay_wide.html,
        graph.replay_wide.text,
        graph.replay_wide.created_at_utc,
    )
    return PublishedReportGraphDTO(
        graph.schema_version,
        graph.output_schema_version,
        graph.replay_public_id,
        PLAYER_REPORT_ID,
        graph.identity,
        graph.replay_wide,
        (player_report,),
    )


def _position(x: float, y: float, z: float = 0.0) -> dict[str, object]:
    return {
        "raw": {"x": x, "y": y, "z": z},
        "map_normalized": {"u": x / 1000.0, "v": y / 1000.0},
        "player_centric": None,
    }


def _availability(state: str = "available") -> dict[str, object]:
    return {"state": state, "reason_codes": [], "evidence_references": [EVIDENCE_START]}


def _scene(**updates: object) -> MapSceneReadModel:
    payload: dict[str, object] = {
        "schema_version": "replay-map-scene-v2",
        "replay_public_id": REPLAY_ID,
        "report_public_id": REPORT_ID,
        "report_version": "replay-report-v1",
        "telemetry_run_public_id": RUN_ID,
        "telemetry_trace_sha256": "b" * 64,
        "map_public_id": MAP_ID,
        "map_display_name": "Tournament Desert",
        "map_content_sha256": "c" * 64,
        "map_schema_version": 2,
        "engine_data_identity": "zero-hour-1.04",
        "query": {
            "replay_public_id": REPLAY_ID,
            "report_public_id": REPORT_ID,
            "frame_start": 0,
            "frame_end": 300,
            "replay_player_public_ids": [],
            "entity_public_ids": [],
            "event_families": [],
            "locomotor_surface": None,
            "coordinate_display": "raw",
            "player_centric_subject_public_id": None,
            "sample_budget": 5000,
            "include_engine_heuristics": False,
        },
        "available_frame_window": {"frame_start": 0, "frame_end": 300},
        "transforms": {"raw": {
            "coordinate_version": "engine-world-xyz-v1",
            "axes": ["engine_world_x", "engine_world_y", "engine_world_z"],
            "units": "engine_world_unit",
            "minimum": {"x": 0.0, "y": 0.0, "z": -100.0},
            "maximum": {"x": 1000.0, "y": 1000.0, "z": 500.0},
            "minimum_inclusive": True,
            "maximum_inclusive": True,
        }, "map_normalized": {
            "transform_version": "map-normalized-v1",
            "formula": "u=(x-min_x)/(max_x-min_x);v=(y-min_y)/(max_y-min_y)",
            "availability": _availability(),
        }, "player_centric": []},
        "rasters": [],
        "starts": [{
            "start_public_id": "90000000-0000-4000-8000-000000000001",
            "name": "Player 1",
            "replay_player_public_ids": [PLAYER_ID],
            "position": _position(100.0, 100.0),
            "evidence": [{"evidence_public_id": EVIDENCE_START, "tier": "observed"}],
        }],
        "engagements": [{
            "engagement_public_id": "90000000-0000-4000-8000-000000000002",
            "frame_start": 120,
            "frame_end": 150,
            "centroid": _position(700.0, 650.0),
            "participant_replay_player_public_ids": [PLAYER_ID],
            "applied_damage_sum": 500.0,
            "killing_blow_count": 1,
            "engagement_algorithm_version": "engagement-cluster-v1",
            "availability": _availability(),
            "evidence": [{"evidence_public_id": EVIDENCE_FIGHT, "tier": "observed"}],
        }],
        "samples": [],
        "orders": [],
        "casualties": [],
        "structures": [{
            "structure_public_id": "90000000-0000-4000-8000-000000000003",
            "source_kind": "map_static",
            "replay_player_public_id": None,
            "template_name": "TechOilDerrick",
            "frame": None,
            "position": _position(500.0, 500.0),
            "availability": _availability(),
            "evidence": [{"evidence_public_id": EVIDENCE_START, "tier": "observed"}],
        }],
        "resources": [{
            "resource_public_id": "90000000-0000-4000-8000-000000000004",
            "resource_kind": "oil_income",
            "label": "TechOilDerrick",
            "position": _position(550.0, 500.0),
            "amount": None,
            "availability": _availability(),
            "evidence": [{"evidence_public_id": EVIDENCE_START, "tier": "observed"}],
        }],
        "routes": [],
        "control_windows": [],
        "visibility_transitions": [],
        "visibility_sampling_summaries": [],
        "engine_heuristic_overlays": [],
        "downsampling": {
            "algorithm_version": "event-forced-stratified-v1",
            "requested_sample_budget": 5000,
            "original_sample_count": 0,
            "mandatory_sample_count": 0,
            "returned_sample_count": 0,
            "budget_exceeded_by_mandatory": False,
        },
        "availability": _availability("partial"),
        "terminal_quality": {"lifecycle": "complete", "issues": []},
    }
    payload.update(updates)
    return MapSceneReadModel(payload)


def _milestone_inputs(*, position_frame: int = 30) -> tuple[PublishedReportGraphDTO, MapSceneReadModel]:
    graph = _report()
    created = ReportValue(
        claim_id="build.created",
        section="build_order",
        label="Structure placement",
        raw_value={"template_name": "GLASupplyStash"},
        unit=None,
        availability="available",
        unavailable_reason=None,
        scope={},
        frame_window=(position_frame, position_frame),
        evidence=(ReportEvidenceRef(EVIDENCE_STRUCTURE_CREATED, "observed"),),
        details={},
    )
    completed = replace(
        created,
        claim_id="build.completed",
        label="Structure completion",
        frame_window=(60, 60),
        evidence=(ReportEvidenceRef(EVIDENCE_STRUCTURE_COMPLETED, "observed"),),
    )
    document = replace(
        graph.replay_wide.document,
        observed=(*graph.replay_wide.document.observed, created, completed),
    )
    graph = replace(graph, replay_wide=replace(graph.replay_wide, document=document))
    payload = dict(_scene().payload)
    payload["structures"] = [
        *payload["structures"],
        {
            "structure_public_id": "90000000-0000-4000-8000-000000000005",
            "source_kind": "construction_completed",
            "replay_player_public_id": PLAYER_ID,
            "template_name": "GLASupplyStash",
            "frame": 60,
            "position": _position(250.0, 300.0),
            "availability": {
                "state": "available",
                "reason_codes": [],
                "evidence_references": [
                    EVIDENCE_STRUCTURE_CREATED,
                    EVIDENCE_STRUCTURE_COMPLETED,
                ],
            },
            "evidence": [
                {
                    "evidence_public_id": EVIDENCE_STRUCTURE_CREATED,
                    "tier": "observed",
                    "support_role": "position",
                    "observed_frame": position_frame,
                },
                {
                    "evidence_public_id": EVIDENCE_STRUCTURE_COMPLETED,
                    "tier": "observed",
                    "support_role": "event_timing",
                    "observed_frame": 60,
                },
            ],
        },
    ]
    return graph, MapSceneReadModel(payload)


def test_camera_plan_uses_cited_positions_and_is_byte_deterministic() -> None:
    service = CameraPlanService()
    first = service.create(_authority(), _report(), _scene())
    second = service.create(_authority(), _report(), _scene())

    assert first.canonical_json() == second.canonical_json()
    assert first.segments[0].start_frame == 0
    assert first.segments[-1].end_frame == 300
    assert [item.focus_kind for item in first.segments] == ["base_context", "engagement"]
    assert first.segments[1].target_x == 700.0
    assert first.segments[1].evidence[0].evidence_public_id == EVIDENCE_FIGHT


def test_camera_milestone_preserves_distinct_timing_and_position_provenance() -> None:
    graph, scene = _milestone_inputs()

    plan = CameraPlanService().create(_authority(), graph, scene)

    milestone = next(item for item in plan.segments if item.focus_kind == "milestone")
    assert (milestone.start_frame, milestone.target_x, milestone.target_y) == (60, 250.0, 300.0)
    assert [
        (item.evidence_public_id, item.support_role, item.observed_frame)
        for item in milestone.evidence
    ] == [
        (EVIDENCE_STRUCTURE_CREATED, "position", 30),
        (EVIDENCE_STRUCTURE_COMPLETED, "event_timing", 60),
    ]


def test_camera_rejects_milestone_position_observed_after_completion() -> None:
    graph, scene = _milestone_inputs(position_frame=70)

    with pytest.raises(CameraPlanContractError, match="position.*after|future position"):
        CameraPlanService().create(_authority(), graph, scene)


def test_legacy_camera_segment_identity_keeps_the_original_evidence_seed() -> None:
    plan = CameraPlanService().create(_authority(), _report(), _scene())
    namespace = uuid5(NAMESPACE_URL, "generals-replay-analyzer:camera-plan-v1")
    expected = str(
        uuid5(
            namespace,
            f"{REPLAY_ID}:{'b' * 64}:0:119:base_context:{EVIDENCE_START}",
        )
    )

    assert plan.segments[0].segment_id == expected


def test_legacy_map_scene_evidence_serialization_omits_role_metadata_nulls() -> None:
    payload = thaw_canonical(_scene().payload)
    serialized = MapSceneDTO.model_validate(payload).model_dump_json()

    assert '"support_role"' not in serialized
    assert '"observed_frame"' not in serialized


def test_camera_plan_prefers_meaningful_focus_over_repeated_damage_samples() -> None:
    scene = _scene()
    payload = dict(scene.payload)
    engagement = dict(payload["engagements"][0])
    payload["engagements"] = [
        dict(
            engagement,
            engagement_public_id=f"90000000-0000-4000-8000-00000000001{index + 5}",
            frame_start=frame,
            frame_end=frame + 20,
        )
        for index, frame in enumerate((120, 125, 130))
    ]

    plan = CameraPlanService().create(_authority(), _report(), MapSceneReadModel(payload))

    assert [item.focus_kind for item in plan.segments] == ["base_context", "engagement"]
    assert plan.segments[1].target_x == 700.0


def test_camera_damage_focus_frames_both_sides_and_zooms_out_for_their_spread() -> None:
    # Break caught: casualty shots centered only the victim and hid the attacking force off-screen.
    graph = _report()
    attacker_position = ReportValue(
        claim_id="combat.attacker_position",
        section="combat",
        label="Attacker position",
        raw_value={"x": 500.0, "y": 550.0},
        unit=None,
        availability="available",
        unavailable_reason=None,
        scope={},
        frame_window=(115, 115),
        evidence=(ReportEvidenceRef(EVIDENCE_ATTACKER_POSITION, "observed"),),
        details={},
    )
    document = replace(
        graph.replay_wide.document,
        observed=(*graph.replay_wide.document.observed, attacker_position),
    )
    graph = replace(graph, replay_wide=replace(graph.replay_wide, document=document))
    payload = dict(_scene().payload)
    payload["engagements"] = []
    payload["casualties"] = [
        {
            "casualty_public_id": "90000000-0000-4000-8000-000000000006",
            "frame": 120,
            "victim_replay_player_public_id": PLAYER_ID,
            "attacker_replay_player_public_id": None,
            "position": _position(700.0, 650.0, 20.0),
            "opposing_position": _position(500.0, 550.0, 10.0),
            "evidence": [
                {
                    "evidence_public_id": EVIDENCE_ATTACKER_POSITION,
                    "tier": "observed",
                    "support_role": "position",
                    "observed_frame": 115,
                },
                {
                    "evidence_public_id": EVIDENCE_FIGHT,
                    "tier": "observed",
                    "support_role": "event_timing",
                    "observed_frame": 120,
                },
            ],
        }
    ]

    plan = CameraPlanService().create(_authority(), graph, MapSceneReadModel(payload))

    damage = plan.segments[1]
    assert damage.focus_kind == "damage"
    assert (damage.target_x, damage.target_y, damage.target_z) == (600.0, 600.0, 15.0)
    # A 224-unit split needs a 31% wider view: the prior 8% margin left the
    # opposing force and nearby context at the edge of a 45-degree cast shot.
    assert damage.zoom == pytest.approx(1.3118033989)
    assert damage.zoom >= 1.25


@pytest.mark.parametrize(
    ("position_frame", "timing_frame"),
    ((121, 120), (115, 119)),
)
def test_camera_rejects_opposing_damage_position_with_noncausal_provenance(
    position_frame: int,
    timing_frame: int,
) -> None:
    graph = _report()
    attacker_position = ReportValue(
        claim_id="combat.attacker_position",
        section="combat",
        label="Attacker position",
        raw_value={"x": 500.0, "y": 550.0},
        unit=None,
        availability="available",
        unavailable_reason=None,
        scope={},
        frame_window=(position_frame, position_frame),
        evidence=(ReportEvidenceRef(EVIDENCE_ATTACKER_POSITION, "observed"),),
        details={},
    )
    document = replace(
        graph.replay_wide.document,
        observed=(*graph.replay_wide.document.observed, attacker_position),
    )
    graph = replace(graph, replay_wide=replace(graph.replay_wide, document=document))
    payload = dict(_scene().payload)
    payload["engagements"] = []
    payload["casualties"] = [
        {
            "casualty_public_id": "90000000-0000-4000-8000-000000000007",
            "frame": 120,
            "position": _position(700.0, 650.0),
            "opposing_position": _position(500.0, 550.0),
            "evidence": [
                {
                    "evidence_public_id": EVIDENCE_ATTACKER_POSITION,
                    "tier": "observed",
                    "support_role": "position",
                    "observed_frame": position_frame,
                },
                {
                    "evidence_public_id": EVIDENCE_FIGHT,
                    "tier": "observed",
                    "support_role": "event_timing",
                    "observed_frame": timing_frame,
                },
            ],
        }
    ]

    with pytest.raises(CameraPlanContractError, match="replay-map-scene-v2|provenance"):
        CameraPlanService().create(_authority(), graph, MapSceneReadModel(payload))


def test_camera_prefers_paired_damage_over_an_earlier_one_sided_kill() -> None:
    graph = _report()
    attacker_position = ReportValue(
        claim_id="combat.paired_attacker_position",
        section="combat",
        label="Paired attacker position",
        raw_value={"x": 500.0, "y": 550.0},
        unit=None,
        availability="available",
        unavailable_reason=None,
        scope={},
        frame_window=(145, 145),
        evidence=(ReportEvidenceRef(EVIDENCE_ATTACKER_POSITION, "observed"),),
        details={},
    )
    document = replace(
        graph.replay_wide.document,
        observed=(*graph.replay_wide.document.observed, attacker_position),
    )
    graph = replace(graph, replay_wide=replace(graph.replay_wide, document=document))
    payload = dict(_scene().payload)
    payload["engagements"] = []
    payload["casualties"] = [
        {
            "casualty_public_id": "90000000-0000-4000-8000-000000000008",
            "frame": 120,
            "position": _position(300.0, 300.0),
            "evidence": [
                {"evidence_public_id": EVIDENCE_FIGHT, "tier": "observed"}
            ],
        },
        {
            "casualty_public_id": "90000000-0000-4000-8000-000000000009",
            "frame": 150,
            "position": _position(700.0, 650.0),
            "opposing_position": _position(500.0, 550.0),
            "evidence": [
                {
                    "evidence_public_id": EVIDENCE_ATTACKER_POSITION,
                    "tier": "observed",
                    "support_role": "position",
                    "observed_frame": 145,
                },
                {
                    "evidence_public_id": EVIDENCE_FIGHT,
                    "tier": "observed",
                    "support_role": "event_timing",
                    "observed_frame": 150,
                },
            ],
        },
    ]

    plan = CameraPlanService().create(_authority(), graph, MapSceneReadModel(payload))

    damage = next(segment for segment in plan.segments if segment.focus_kind == "damage")
    assert damage.start_frame == 150
    assert (damage.target_x, damage.target_y) == (600.0, 600.0)


def test_camera_keeps_one_sided_damage_after_a_paired_damage_cooldown() -> None:
    graph = _report()
    attacker_position = ReportValue(
        claim_id="combat.paired_attacker_position",
        section="combat",
        label="Paired attacker position",
        raw_value={"x": 500.0, "y": 550.0},
        unit=None,
        availability="available",
        unavailable_reason=None,
        scope={},
        frame_window=(145, 145),
        evidence=(ReportEvidenceRef(EVIDENCE_ATTACKER_POSITION, "observed"),),
        details={},
    )
    late_damage = ReportValue(
        claim_id="combat.late_damage",
        section="combat",
        label="Late damage",
        raw_value={"damage": 100},
        unit=None,
        availability="available",
        unavailable_reason=None,
        scope={},
        frame_window=(650, 650),
        evidence=(ReportEvidenceRef(EVIDENCE_LATE_DAMAGE, "observed"),),
        details={},
    )
    document = replace(
        graph.replay_wide.document,
        observed=(
            *graph.replay_wide.document.observed,
            attacker_position,
            late_damage,
        ),
    )
    graph = replace(graph, replay_wide=replace(graph.replay_wide, document=document))
    payload = dict(_scene().payload)
    payload["available_frame_window"] = {"frame_start": 0, "frame_end": 900}
    query = dict(payload["query"])
    query["frame_end"] = 900
    payload["query"] = query
    payload["engagements"] = []
    payload["casualties"] = [
        {
            "casualty_public_id": "90000000-0000-4000-8000-000000000009",
            "frame": 150,
            "position": _position(700.0, 650.0),
            "opposing_position": _position(500.0, 550.0),
            "evidence": [
                {
                    "evidence_public_id": EVIDENCE_ATTACKER_POSITION,
                    "tier": "observed",
                    "support_role": "position",
                    "observed_frame": 145,
                },
                {
                    "evidence_public_id": EVIDENCE_FIGHT,
                    "tier": "observed",
                    "support_role": "event_timing",
                    "observed_frame": 150,
                },
            ],
        },
        {
            "casualty_public_id": "90000000-0000-4000-8000-000000000010",
            "frame": 650,
            "position": _position(350.0, 300.0),
            "evidence": [
                {"evidence_public_id": EVIDENCE_LATE_DAMAGE, "tier": "observed"}
            ],
        },
    ]

    plan = CameraPlanService().create(
        _authority(evidence_horizon=EvidenceHorizonV1(frame_start=0, frame_end=900)),
        graph,
        MapSceneReadModel(payload),
    )

    damage = [segment for segment in plan.segments if segment.focus_kind == "damage"]
    assert [(item.start_frame, item.target_x) for item in damage] == [
        (150, 600.0),
        (650, 350.0),
    ]


def test_camera_paired_damage_replaces_a_nearby_milestone() -> None:
    graph, scene = _milestone_inputs()
    attacker_position = ReportValue(
        claim_id="combat.paired_attacker_position",
        section="combat",
        label="Paired attacker position",
        raw_value={"x": 500.0, "y": 550.0},
        unit=None,
        availability="available",
        unavailable_reason=None,
        scope={},
        frame_window=(145, 145),
        evidence=(ReportEvidenceRef(EVIDENCE_ATTACKER_POSITION, "observed"),),
        details={},
    )
    document = replace(
        graph.replay_wide.document,
        observed=(*graph.replay_wide.document.observed, attacker_position),
    )
    graph = replace(graph, replay_wide=replace(graph.replay_wide, document=document))
    payload = dict(scene.payload)
    payload["engagements"] = []
    payload["casualties"] = [
        {
            "casualty_public_id": "90000000-0000-4000-8000-000000000011",
            "frame": 150,
            "position": _position(700.0, 650.0),
            "opposing_position": _position(500.0, 550.0),
            "evidence": [
                {
                    "evidence_public_id": EVIDENCE_ATTACKER_POSITION,
                    "tier": "observed",
                    "support_role": "position",
                    "observed_frame": 145,
                },
                {
                    "evidence_public_id": EVIDENCE_FIGHT,
                    "tier": "observed",
                    "support_role": "event_timing",
                    "observed_frame": 150,
                },
            ],
        }
    ]

    plan = CameraPlanService().create(_authority(), graph, MapSceneReadModel(payload))

    assert not any(segment.focus_kind == "milestone" for segment in plan.segments)
    damage = next(segment for segment in plan.segments if segment.focus_kind == "damage")
    assert (damage.start_frame, damage.target_x, damage.target_y) == (150, 600.0, 600.0)


def test_player_selected_camera_uses_only_shared_frame_zero_context_before_commentary() -> None:
    report = _player_selected_report()
    scene = _scene()
    payload = dict(scene.payload)
    query = dict(payload["query"])
    payload["report_public_id"] = PLAYER_REPORT_ID
    query["report_public_id"] = PLAYER_REPORT_ID
    payload["query"] = query

    camera = CameraPlanService().create(
        _authority(report_public_id=PLAYER_REPORT_ID),
        report,
        MapSceneReadModel(payload),
    )
    commentary = CommentaryPlanService().create(report, camera)

    assert camera.authority.report_public_id == PLAYER_REPORT_ID
    assert [item.focus_kind for item in camera.segments] == ["base_context", "engagement"]
    assert camera.segments[0].evidence[0].evidence_public_id == EVIDENCE_START
    assert camera.segments[1].evidence[0].evidence_public_id == EVIDENCE_FIGHT
    assert commentary.report_public_id == PLAYER_REPORT_ID


@pytest.mark.parametrize(
    "authority_updates, scene_updates",
    [
        ({"replay_public_id": "10000000-0000-4000-8000-000000000099"}, {}),
        ({"map_content_sha256": "9" * 64}, {}),
        ({"telemetry_trace_sha256": "8" * 64}, {}),
        ({}, {"schema_version": "replay-map-scene-v1"}),
        ({}, {"map_public_id": "40000000-0000-4000-8000-000000000099"}),
    ],
)
def test_camera_plan_rejects_cross_authority_or_stale_scene(
    authority_updates: dict[str, object], scene_updates: dict[str, object]
) -> None:
    with pytest.raises(CameraPlanContractError):
        CameraPlanService().create(_authority(**authority_updates), _report(), _scene(**scene_updates))


def test_camera_plan_rejects_filtered_or_player_centric_scene() -> None:
    scene = _scene()
    payload = dict(scene.payload)
    query = dict(payload["query"])
    query["replay_player_public_ids"] = [PLAYER_ID]
    payload["query"] = query
    with pytest.raises(CameraPlanContractError, match="unfiltered"):
        CameraPlanService().create(_authority(), _report(), MapSceneReadModel(payload))


def test_camera_plan_requires_the_complete_map_scene_v2_contract() -> None:
    payload = dict(_scene().payload)
    payload.pop("terminal_quality")

    with pytest.raises(CameraPlanContractError, match="replay-map-scene-v2"):
        CameraPlanService().create(_authority(), _report(), MapSceneReadModel(payload))


@pytest.mark.parametrize("damage", ["unknown_evidence", "wrong_tier", "wrong_frame"])
def test_camera_plan_rejects_broken_or_time_mismatched_evidence(damage: str) -> None:
    payload = dict(_scene().payload)
    engagements = [dict(item) for item in payload["engagements"]]
    if damage == "unknown_evidence":
        engagements[0]["evidence"] = [{
            "evidence_public_id": "50000000-0000-4000-8000-000000000099",
            "tier": "observed",
        }]
    elif damage == "wrong_tier":
        engagements[0]["evidence"] = [{"evidence_public_id": EVIDENCE_FIGHT, "tier": "derived"}]
    else:
        engagements[0]["frame_start"] = 5
        engagements[0]["frame_end"] = 15
    payload["engagements"] = engagements

    with pytest.raises(CameraPlanContractError, match="evidence|cited"):
        CameraPlanService().create(_authority(), _report(), MapSceneReadModel(payload))


def test_camera_citation_clips_post_update_report_boundary_to_presentable_horizon() -> None:
    citations = _citations(
        {"evidence": [{"evidence_public_id": EVIDENCE_START, "tier": "observed"}]},
        {EVIDENCE_START: ("observed", ((0, 303),))},
        300,
        0,
        0,
    )

    assert citations[0].frame_start == 0
    assert citations[0].frame_end == 300


def test_camera_plan_rejects_out_of_bounds_cited_position() -> None:
    scene = _scene()
    payload = dict(scene.payload)
    engagements = [dict(item) for item in payload["engagements"]]
    engagements[0]["centroid"] = _position(1200.0, 650.0)
    payload["engagements"] = engagements
    with pytest.raises(CameraPlanContractError, match="replay-map-scene-v2|bounds"):
        CameraPlanService().create(_authority(), _report(), MapSceneReadModel(payload))


def test_camera_plan_accepts_engine_validated_airspace_above_terrain_bounds() -> None:
    scene = _scene()
    payload = dict(scene.payload)
    transforms = dict(payload["transforms"])
    raw = dict(transforms["raw"])
    raw["maximum"] = {"x": 1000.0, "y": 2000.0, "z": 159.375}
    transforms["raw"] = raw
    payload["transforms"] = transforms
    engagements = [dict(item) for item in payload["engagements"]]
    high_airspace = _position(543.0, 1598.0, 295.0)
    high_airspace["map_normalized"] = {"u": 0.543, "v": 0.799}
    engagements[0]["centroid"] = high_airspace
    payload["engagements"] = engagements

    plan = CameraPlanService().create(_authority(), _report(), MapSceneReadModel(payload))

    assert plan.segments[1].target_x == 543.0
    assert plan.segments[1].target_y == 1598.0
    assert plan.segments[1].target_z == 295.0
