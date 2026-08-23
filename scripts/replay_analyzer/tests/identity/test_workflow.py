"""Identity preview and durable invalidation coordinator tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db.models import Player
from generals_replay_analyzer.identity.service import PlayerIdentityService
from generals_replay_analyzer.identity.workflow import (
    IdentityExecutionCommand,
    InverseIdentityDraft,
    MergeIdentityDraft,
    PlayerIdentityWorkflowService,
    RevisionPrecondition,
    SplitIdentityDraft,
)

from .test_query import _seed

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _id(value: int) -> str:
    return str(UUID(int=value))


class _Ids:
    def __init__(self) -> None:
        self.value = 100

    def __call__(self) -> str:
        self.value += 1
        return _id(self.value)


class _Planner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def ensure_identity_invalidation_plan(self, operation_public_id: str) -> object:
        from generals_replay_analyzer.analysis_pipeline.planner import IdentityInvalidationPlanDTO

        self.calls.append(operation_public_id)
        return IdentityInvalidationPlanDTO(operation_public_id, ())


class _FlakyPlanner(_Planner):
    def ensure_identity_invalidation_plan(self, operation_public_id: str) -> object:
        self.calls.append(operation_public_id)
        if len(self.calls) == 1:
            raise ValueError("planner returned malformed state")
        from generals_replay_analyzer.analysis_pipeline.planner import IdentityInvalidationPlanDTO

        return IdentityInvalidationPlanDTO(operation_public_id, ())


def test_preview_is_read_only_and_execute_uses_committed_audit_for_idempotent_invalidation(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch preview mutation or best-effort route-level job creation after identity commit."""
    with identity_session_factory.begin() as session:
        session.add_all(
            (
                Player(public_id=_id(1), display_name="Target", identity_revision=2, updated_at=NOW, created_at=NOW),
                Player(public_id=_id(2), display_name="Source", identity_revision=4, updated_at=NOW, created_at=NOW),
            )
        )
    planner = _Planner()
    workflow = PlayerIdentityWorkflowService(
        identity_session_factory,
        PlayerIdentityService(identity_session_factory, public_id_factory=_Ids(), now_factory=lambda: NOW),
        planner,
        now_factory=lambda: NOW,
    )
    draft = MergeIdentityDraft(
        target_player_public_id=_id(1),
        source_player_public_ids=(_id(2),),
        expected_revisions=(RevisionPrecondition(_id(1), 2), RevisionPrecondition(_id(2), 4)),
    )
    preview = workflow.preview(draft)
    assert preview.can_execute is True
    assert preview.impact.canonical_player_count == 2
    with identity_session_factory() as session:
        assert [
            (row.public_id, row.identity_revision, row.retired_at)
            for row in session.query(Player).order_by(Player.public_id)
        ] == [(_id(1), 2, None), (_id(2), 4, None)]

    receipt = workflow.execute(
        IdentityExecutionCommand(draft, preview.before_snapshot_digest, "operator:leex", "same player")
    )
    assert receipt.operation.operation_kind == "merge_players"
    assert receipt.operation.affected_player_revisions == (
        RevisionPrecondition(_id(1), 3),
        RevisionPrecondition(_id(2), 5),
    )
    assert receipt.invalidation_jobs[0].state == "already_queued"
    assert planner.calls == [receipt.operation.operation_public_id]


def test_explicit_split_and_inverse_are_previewed_audited_and_revision_guarded(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch alias-wide implicit splits or inverse execution without a fresh exact preview."""
    player_id, replay_player_id = _seed(identity_session_factory)
    planner = _Planner()
    workflow = PlayerIdentityWorkflowService(
        identity_session_factory,
        PlayerIdentityService(identity_session_factory, public_id_factory=_Ids(), now_factory=lambda: NOW),
        planner,
        now_factory=lambda: NOW,
    )
    split = SplitIdentityDraft(
        alias_public_id=_id(7),
        replay_player_public_ids=(replay_player_id,),
        new_display_name="Leex279 Alt",
        expected_revisions=(RevisionPrecondition(player_id, 3),),
    )
    preview = workflow.preview(split)
    assert preview.can_execute is True
    receipt = workflow.execute(
        IdentityExecutionCommand(split, preview.before_snapshot_digest, "operator:leex", "separate account")
    )
    assert receipt.operation.operation_kind == "split_alias"
    inverse = InverseIdentityDraft(
        receipt.operation.operation_public_id,
        receipt.operation.affected_player_revisions,
    )
    inverse_preview = workflow.preview(inverse)
    assert inverse_preview.can_execute is True
    restored = workflow.execute(
        IdentityExecutionCommand(inverse, inverse_preview.before_snapshot_digest, "operator:leex", "undo split")
    )
    assert restored.operation.operation_kind == "inverse"
    assert restored.operation.inverse_of_operation_public_id == receipt.operation.operation_public_id
    audit = workflow.audit(player_id)
    assert sorted(item.operation_kind for item in audit.operations) == ["inverse", "split_alias"]


def test_invalid_and_unknown_identity_workflow_requests_are_explicit(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch malformed membership or unknown audit subjects being treated as executable empty changes."""
    with identity_session_factory.begin() as session:
        session.add(Player(public_id=_id(1), display_name="Only", identity_revision=2, updated_at=NOW, created_at=NOW))
    workflow = PlayerIdentityWorkflowService(
        identity_session_factory, PlayerIdentityService(identity_session_factory), _Planner(), now_factory=lambda: NOW
    )
    invalid = MergeIdentityDraft(_id(1), (_id(1),), (RevisionPrecondition(_id(1), 2),))
    preview = workflow.preview(invalid)
    assert preview.can_execute is False
    assert "invalid_merge_membership" in preview.reason_codes
    unknown = workflow.audit(_id(999))
    assert unknown.available is False
    assert unknown.reason_codes == ("player_not_found",)


def test_split_preview_rejects_replay_players_owned_by_another_alias_player(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch preview approving membership that execute must reject after the identity write begins."""
    player_id, _ = _seed(identity_session_factory)
    workflow = PlayerIdentityWorkflowService(
        identity_session_factory,
        PlayerIdentityService(identity_session_factory, public_id_factory=_Ids(), now_factory=lambda: NOW),
        _Planner(),
        now_factory=lambda: NOW,
    )
    preview = workflow.preview(
        SplitIdentityDraft(
            alias_public_id=_id(7),
            replay_player_public_ids=(_id(6),),
            new_display_name="Wrong owner",
            expected_revisions=(RevisionPrecondition(player_id, 3),),
        )
    )
    assert preview.can_execute is False
    assert preview.reason_codes == ("invalid_split_membership",)


def test_unexpected_planner_failure_returns_a_durable_receipt_and_can_be_retried(
    identity_session_factory: sessionmaker[Session],
) -> None:
    """Catch a committed identity mutation escaping without a durable invalidation retry path."""
    with identity_session_factory.begin() as session:
        session.add_all(
            (
                Player(public_id=_id(1), display_name="Target", identity_revision=2, updated_at=NOW, created_at=NOW),
                Player(public_id=_id(2), display_name="Source", identity_revision=4, updated_at=NOW, created_at=NOW),
            )
        )
    planner = _FlakyPlanner()
    workflow = PlayerIdentityWorkflowService(
        identity_session_factory,
        PlayerIdentityService(identity_session_factory, public_id_factory=_Ids(), now_factory=lambda: NOW),
        planner,
        now_factory=lambda: NOW,
    )
    draft = MergeIdentityDraft(
        _id(1),
        (_id(2),),
        (RevisionPrecondition(_id(1), 2), RevisionPrecondition(_id(2), 4)),
    )
    preview = workflow.preview(draft)
    receipt = workflow.execute(
        IdentityExecutionCommand(draft, preview.before_snapshot_digest, "operator:leex", "same player")
    )
    assert receipt.invalidation_jobs[0].state == "durable_retry_required"
    assert receipt.invalidation_jobs[0].reason_code == "identity_invalidation_planner_failure"
    retried = workflow.retry_invalidation(receipt.operation.operation_public_id)
    assert retried[0].state == "already_queued"
    assert planner.calls == [receipt.operation.operation_public_id, receipt.operation.operation_public_id]
