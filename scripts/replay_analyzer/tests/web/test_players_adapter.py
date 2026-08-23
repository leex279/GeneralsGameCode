"""Production player/identity/comparison adapter boundary tests."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.comparison.service import LongitudinalSubject, MatchSubject, ReplayComparisonService
from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.models import LongitudinalRun, Player
from generals_replay_analyzer.identity.query import PlayerQueryService
from generals_replay_analyzer.identity.service import PlayerIdentityService
from generals_replay_analyzer.identity.workflow import PlayerIdentityWorkflowService
from generals_replay_analyzer.web.adapters.players import AnalyticsPlayersAdapter
from generals_replay_analyzer.web.app import OneTimeFormTokenRegistry
from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import PublicProblem
from generals_replay_analyzer.web.ports import (
    ComparisonFiltersDTO,
    ComparisonSelectionDTO,
    ExecuteIdentityChangeDTO,
    InverseIdentityDraftDTO,
    MatchSubjectDTO,
    MergeIdentityDraftDTO,
    OpeningSubjectDTO,
    PlayerCohortSubjectDTO,
    PlayerIndexQueryDTO,
    PlayerProfileSelectionDTO,
    RevisionPreconditionDTO,
    SplitIdentityDraftDTO,
    StrategySubjectDTO,
    TimePeriodSubjectDTO,
)
from generals_replay_analyzer.web.routes.comparisons import router as comparison_router
from generals_replay_analyzer.web.routes.players import router as player_router

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _id(value: int) -> str:
    return str(UUID(int=value))


class _Planner:
    def ensure_identity_invalidation_plan(self, operation_public_id: str) -> object:
        from generals_replay_analyzer.analysis_pipeline.planner import IdentityInvalidationPlanDTO

        return IdentityInvalidationPlanDTO(operation_public_id, ())


def _adapter(path: Path) -> tuple[AnalyticsPlayersAdapter, sessionmaker[Session], object]:
    upgrade_database(path)
    engine = create_database_engine(path)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add_all(
            (
                Player(public_id=_id(1), display_name="Alpha", identity_revision=2, updated_at=NOW, created_at=NOW),
                Player(public_id=_id(2), display_name="Bravo", identity_revision=4, updated_at=NOW, created_at=NOW),
            )
        )
        session.flush()
        for index, player in enumerate(session.query(Player).order_by(Player.public_id), start=10):
            session.add(
                LongitudinalRun(
                    run_id=_id(index),
                    player_id=player.id,
                    identity_revision=player.identity_revision,
                    analyzer_name="longitudinal-player-analysis",
                    analyzer_version="v1",
                    segment_key_json={
                        "schema_version": "longitudinal-segment-v1",
                        "quality_policy": {"quality_floor": "partial"},
                    },
                    settings_json={
                        "definitions": [
                            {
                                "public_name": "cash",
                                "result_kind": "metric",
                                "definition_version": "cash-v1",
                                "unit": "credits",
                                "scope_types": ["player"],
                            }
                        ],
                        "settings": {"bootstrap_algorithm_version": "median-bootstrap-v1"},
                    },
                    input_digest=str(index)[-1] * 64,
                    cache_key=str(index + 2)[-1] * 64,
                    status="succeeded",
                    created_at=NOW,
                    completed_at=NOW,
                )
            )
    identity = PlayerIdentityService(factory, public_id_factory=lambda: _id(100), now_factory=lambda: NOW)
    workflow = PlayerIdentityWorkflowService(factory, identity, _Planner(), now_factory=lambda: NOW)
    return (
        AnalyticsPlayersAdapter(PlayerQueryService(factory), workflow, ReplayComparisonService(factory)),
        factory,
        engine,
    )


def test_adapter_returns_only_frozen_web_dtos_and_preserves_preview_without_mutation(tmp_path: Path) -> None:
    """Catch ORM rows/generic dictionaries crossing the route dependency surface."""
    adapter, factory, engine = _adapter(tmp_path / "players.sqlite3")
    try:
        page = adapter.list_players(PlayerIndexQueryDTO(active_only=False))
        assert [item.display_name for item in page.items] == ["Alpha", "Bravo"]
        profile_resolution = adapter.resolve_profile(PlayerProfileSelectionDTO(player_public_id=_id(1)))
        assert profile_resolution.fixed_query is not None
        profile = adapter.get_profile(profile_resolution.fixed_query)
        assert profile.player.display_name == "Alpha"
        assert profile.version.identity_revision == 2
        draft = MergeIdentityDraftDTO(
            operation_kind="merge_players",
            target_player_public_id=_id(1),
            source_player_public_ids=(_id(2),),
            expected_revisions=(
                RevisionPreconditionDTO(player_public_id=_id(1), expected_revision=2),
                RevisionPreconditionDTO(player_public_id=_id(2), expected_revision=4),
            ),
        )
        preview = adapter.preview(draft)
        assert preview.can_execute is True
        assert preview.impact.canonical_player_count == 2
        assert preview.model_dump(mode="json")["schema_version"] == "player-identity-preview-v1"
        with factory() as session:
            assert [
                (row.public_id, row.identity_revision) for row in session.query(Player).order_by(Player.public_id)
            ] == [(_id(1), 2), (_id(2), 4)]
        resolved = adapter.resolve(
            ComparisonSelectionDTO(
                kind="players",
                left_public_id=_id(1),
                right_public_id=_id(2),
                baseline_requested=False,
                metric_definition_ids=("cash",),
                filters=ComparisonFiltersDTO(),
            )
        )
        assert resolved.state == "resolved"
        assert resolved.fixed_query is not None
        assert resolved.fixed_query.left.longitudinal.segment_digest != "0" * 64  # type: ignore[union-attr]
        comparison = adapter.compare(resolved.fixed_query)
        assert comparison.state == "not_comparable"
        assert comparison.query == resolved.fixed_query
        assert comparison.metrics[0].derived_difference is None
        unavailable = adapter.resolve(
            ComparisonSelectionDTO(
                kind="openings",
                left_public_id=None,
                right_public_id=None,
                baseline_requested=False,
                metric_definition_ids=("opening",),
                filters=ComparisonFiltersDTO(),
            )
        )
        assert unavailable.state == "unavailable"
        stale = profile_resolution.fixed_query.model_copy(update={"longitudinal_run_ids": (_id(999),)})
        with pytest.raises(PublicProblem) as problem:
            adapter.get_profile(stale)
        assert problem.value.status == 409
        receipt = adapter.execute(
            ExecuteIdentityChangeDTO(
                draft=draft,
                expected_before_snapshot_digest=preview.before_snapshot_digest,
                operator_label="operator:leex",
                reason="same player",
            )
        )
        assert receipt.operation.operation_kind == "merge_players"
        assert receipt.invalidation_jobs[0].state == "already_queued"
        assert adapter.retry_invalidation(receipt.operation.operation_public_id)[0].state == "already_queued"
        assert adapter.retry_invalidation(receipt.operation.operation_public_id)[0].state == "already_queued"
        audit = adapter.audit(_id(1), 1, 25)
        assert audit.total_items == 1
        assert audit.operations[0].reason == "same player"
    finally:
        engine.dispose()  # type: ignore[attr-defined]


def test_real_player_selection_redirects_to_the_service_owned_fixed_query(tmp_path: Path) -> None:
    """Catch selector redirects using route-built or mutable-latest comparison bindings."""
    adapter, _factory, engine = _adapter(tmp_path / "redirect.sqlite3")
    app = FastAPI()
    app.include_router(comparison_router)
    app.dependency_overrides[application_port] = lambda: adapter
    try:
        with TestClient(app) as client:
            response = client.get(
                f"/compare?kind=players&left_public_id={_id(1)}&right_public_id={_id(2)}&metric_definition_id=cash",
                headers={"accept": "text/html"},
                follow_redirects=False,
            )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/compare/result?schema_version=replay-comparison-query-v1")
        assert "left_longitudinal_segment_digest=" in response.headers["location"]
        assert "definition_0_definition_version=cash-v1" in response.headers["location"]
    finally:
        engine.dispose()  # type: ignore[attr-defined]


def test_persisted_audit_recovers_a_lost_adapter_execute_response_through_the_web(tmp_path: Path) -> None:
    """Catch retry existing only on fakes instead of the real immutable-audit adapter boundary."""
    adapter, _factory, engine = _adapter(tmp_path / "lost-response.sqlite3")
    draft = MergeIdentityDraftDTO(
        operation_kind="merge_players",
        target_player_public_id=_id(1),
        source_player_public_ids=(_id(2),),
        expected_revisions=(
            RevisionPreconditionDTO(player_public_id=_id(1), expected_revision=2),
            RevisionPreconditionDTO(player_public_id=_id(2), expected_revision=4),
        ),
    )
    preview = adapter.preview(draft)
    receipt = adapter.execute(
        ExecuteIdentityChangeDTO(
            draft=draft,
            expected_before_snapshot_digest=preview.before_snapshot_digest,
            operator_label="operator:leex",
            reason="same player",
        )
    )
    app = FastAPI()
    app.state.form_csrf_token_registry = OneTimeFormTokenRegistry()
    app.include_router(player_router)
    app.dependency_overrides[application_port] = lambda: adapter
    try:
        with TestClient(app) as client:
            audit = client.get(f"/players/{_id(1)}/identity", headers={"accept": "text/html"})
            hidden = re.search(
                rf'action="/players/{_id(1)}/identity/invalidation/'
                rf'{receipt.operation.operation_public_id}/retry".*?name="_csrf" value="([^"]+)"',
                audit.text,
            )
            assert hidden is not None
            assert receipt.operation.operation_public_id in audit.text
            response = client.post(
                f"/players/{_id(1)}/identity/invalidation/{receipt.operation.operation_public_id}/retry",
                data={"_csrf": hidden.group(1)},
                follow_redirects=False,
            )
        assert response.status_code == 303
        assert "invalidation_state=already_queued" in response.headers["location"]
        assert adapter.audit(_id(1), 1, 25).operations[0].operation_public_id == receipt.operation.operation_public_id
    finally:
        engine.dispose()  # type: ignore[attr-defined]


def test_adapter_preserves_filters_and_rejects_forged_fixed_query_fields(tmp_path: Path) -> None:
    """Catch selector filters or fixed metric IDs being discarded at the Web/domain seam."""
    adapter, _factory, engine = _adapter(tmp_path / "fixed-query.sqlite3")
    quality_digest = hashlib.sha256(
        json.dumps(
            {"quality_floor": "partial"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    selection = ComparisonSelectionDTO(
        kind="players",
        left_public_id=_id(1),
        right_public_id=_id(2),
        baseline_requested=False,
        metric_definition_ids=("cash",),
        filters=ComparisonFiltersDTO(quality_policy_digest=quality_digest),
    )
    try:
        resolved = adapter.resolve(selection)
        assert resolved.state == "resolved"
        assert resolved.fixed_query is not None

        rejected_quality = adapter.resolve(
            selection.model_copy(update={"filters": ComparisonFiltersDTO(quality_policy_digest="f" * 64)})
        )
        assert rejected_quality.state == "unavailable"
        assert rejected_quality.reason_codes == ("comparison_filter_not_materialized",)

        rejected_date = adapter.resolve(
            selection.model_copy(update={"filters": ComparisonFiltersDTO(date_from_utc=NOW)})
        )
        assert rejected_date.state == "unavailable"
        assert rejected_date.reason_codes == ("comparison_filter_not_materialized",)

        forged = resolved.fixed_query.model_copy(update={"metric_definition_ids": ("forged",)})
        with pytest.raises(PublicProblem) as problem:
            adapter.compare(forged)
        assert problem.value.code == "comparison_binding_conflict"

        left = resolved.fixed_query.left
        assert isinstance(left, PlayerCohortSubjectDTO)
        forged_left = left.model_copy(update={"identity_revision": left.identity_revision + 1})
        forged_identity = resolved.fixed_query.model_copy(update={"left": forged_left})
        with pytest.raises(PublicProblem) as identity_problem:
            adapter.compare(forged_identity)
        assert identity_problem.value.code == "comparison_binding_conflict"
    finally:
        engine.dispose()  # type: ignore[attr-defined]


def test_adapter_projects_every_fixed_subject_without_generic_payloads() -> None:
    """Catch one comparison mode crossing the web seam as a dictionary or losing exact bindings."""
    base = {
        "run_id": _id(10),
        "player_public_id": _id(1),
        "identity_revision": 2,
        "analyzer_name": "longitudinal-player-analysis",
        "analyzer_version": "v1",
        "segment_digest": "a" * 64,
        "quality_policy_digest": "b" * 64,
        "input_digest": "c" * 64,
        "cache_key": "d" * 64,
        "statistics_algorithm_versions": ("median-bootstrap-v1",),
    }
    subjects = (
        LongitudinalSubject("player_cohort", **base),
        LongitudinalSubject(
            "opening",
            **base,
            result_public_id=_id(20),
            opening_definition_id="opening",
            opening_definition_version="v1",
        ),
        LongitudinalSubject(
            "strategy",
            **base,
            result_public_id=_id(21),
            strategy_id="fast_tech",
            taxonomy_version="taxonomy-v1",
            rule_version="rule-v1",
        ),
        LongitudinalSubject("time_period", **base, start_inclusive_utc=NOW, end_exclusive_utc=NOW.replace(hour=13)),
        MatchSubject("match", _id(30), _id(31), _id(32), "e" * 64, (_id(33),)),
    )
    projected = tuple(AnalyticsPlayersAdapter._web_subject(item) for item in subjects)
    assert tuple(type(item) for item in projected) == (
        PlayerCohortSubjectDTO,
        OpeningSubjectDTO,
        StrategySubjectDTO,
        TimePeriodSubjectDTO,
        MatchSubjectDTO,
    )
    for domain, item in zip(subjects, projected, strict=True):
        assert AnalyticsPlayersAdapter._subject(item) == domain

    split = SplitIdentityDraftDTO(
        operation_kind="split_alias",
        alias_public_id=_id(40),
        replay_player_public_ids=(_id(41),),
        new_display_name="Split",
        expected_revisions=(RevisionPreconditionDTO(player_public_id=_id(1), expected_revision=2),),
    )
    inverse = InverseIdentityDraftDTO(
        operation_kind="inverse",
        operation_public_id=_id(42),
        expected_revisions=(RevisionPreconditionDTO(player_public_id=_id(1), expected_revision=2),),
    )
    from generals_replay_analyzer.web.adapters.players import _domain_draft, _web_draft

    for draft in (split, inverse):
        assert _web_draft(_domain_draft(draft)) == draft


def test_player_bound_subjects_reject_outer_only_identity_forgery_at_both_boundaries() -> None:
    """Catch a fixed URL claiming an outer player/revision that its frozen run never analyzed."""
    values = {
        "run_id": _id(10),
        "player_public_id": _id(1),
        "identity_revision": 2,
        "analyzer_name": "longitudinal-player-analysis",
        "analyzer_version": "v1",
        "segment_digest": "a" * 64,
        "quality_policy_digest": "b" * 64,
        "input_digest": "c" * 64,
        "cache_key": "d" * 64,
        "statistics_algorithm_versions": ("median-bootstrap-v1",),
    }
    base = LongitudinalSubject("player_cohort", **values)
    cohort = AnalyticsPlayersAdapter._web_subject(base)
    assert isinstance(cohort, PlayerCohortSubjectDTO)
    subjects = (
        cohort,
        AnalyticsPlayersAdapter._web_subject(
            LongitudinalSubject(
                "opening",
                **values,
                result_public_id=_id(20),
                opening_definition_id="opening",
                opening_definition_version="v1",
            )
        ),
        AnalyticsPlayersAdapter._web_subject(
            LongitudinalSubject(
                "strategy",
                **values,
                result_public_id=_id(21),
                strategy_id="fast_tech",
                taxonomy_version="taxonomy-v1",
                rule_version="rule-v1",
            )
        ),
        AnalyticsPlayersAdapter._web_subject(
            LongitudinalSubject(
                "time_period",
                **values,
                start_inclusive_utc=NOW,
                end_exclusive_utc=NOW.replace(hour=13),
            )
        ),
    )
    for subject in subjects:
        payload = subject.model_dump(mode="python")
        payload["player_public_id"] = _id(999)
        with pytest.raises(ValidationError, match="longitudinal binding"):
            type(subject).model_validate(payload)
        forged = subject.model_copy(update={"identity_revision": subject.identity_revision + 1})
        with pytest.raises(ValueError, match="longitudinal binding"):
            AnalyticsPlayersAdapter._subject(forged)
