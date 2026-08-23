"""Preview and execute revision-guarded identity changes with durable invalidation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.analysis_pipeline.planner import (
    AnalysisPlanningError,
    IdentityInvalidationPlanDTO,
)
from generals_replay_analyzer.db.models import (
    FeatureSet,
    LongitudinalResult,
    LongitudinalRun,
    Player,
    PlayerAlias,
    PlayerIdentityOperation,
    ReplayPlayer,
    Report,
)
from generals_replay_analyzer.identity.service import (
    IdentityConflictError,
    IdentityInvariantError,
    IdentityNotFoundError,
    PlayerIdentityService,
)


class IdentityInvalidationPlanner(Protocol):
    def ensure_identity_invalidation_plan(self, operation_public_id: str) -> IdentityInvalidationPlanDTO: ...


@dataclass(frozen=True, slots=True, order=True)
class RevisionPrecondition:
    player_public_id: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class MergeIdentityDraft:
    target_player_public_id: str
    source_player_public_ids: tuple[str, ...]
    expected_revisions: tuple[RevisionPrecondition, ...]
    operation_kind: Literal["merge_players"] = "merge_players"


@dataclass(frozen=True, slots=True)
class SplitIdentityDraft:
    alias_public_id: str
    replay_player_public_ids: tuple[str, ...]
    new_display_name: str
    expected_revisions: tuple[RevisionPrecondition, ...]
    operation_kind: Literal["split_alias"] = "split_alias"


@dataclass(frozen=True, slots=True)
class InverseIdentityDraft:
    operation_public_id: str
    expected_revisions: tuple[RevisionPrecondition, ...]
    operation_kind: Literal["inverse"] = "inverse"


IdentityDraft = MergeIdentityDraft | SplitIdentityDraft | InverseIdentityDraft


@dataclass(frozen=True, slots=True)
class IdentityImpact:
    canonical_player_count: int
    alias_count: int
    replay_player_count: int
    replay_count: int
    feature_set_count: int
    longitudinal_run_count: int
    longitudinal_result_count: int
    report_count: int
    invalidation_stage_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class IdentityPreview:
    draft: IdentityDraft
    before_snapshot_digest: str
    expected_after_snapshot_digest: str
    impact: IdentityImpact
    can_execute: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IdentityExecutionCommand:
    draft: IdentityDraft
    expected_before_snapshot_digest: str
    operator_label: str
    reason: str


@dataclass(frozen=True, slots=True)
class InvalidationJobReference:
    job_public_id: str
    stage: str
    state: Literal["pending", "already_queued", "durable_retry_required"]
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class IdentityOperationSummary:
    operation_public_id: str
    # TheSuperHackers @fix Leex 23/08/2026 Preserve automatic and provider-attachment records in read-only audit history. (#TBD)
    operation_kind: Literal["auto_link", "merge_players", "split_alias", "attach_external_alias", "inverse"]
    inverse_of_operation_public_id: str | None
    operator_label: str
    reason: str
    created_at_utc: datetime
    affected_player_revisions: tuple[RevisionPrecondition, ...]
    inverse_allowed: bool
    inverse_reason_code: str | None


@dataclass(frozen=True, slots=True)
class IdentityMutationReceipt:
    operation: IdentityOperationSummary
    invalidation_jobs: tuple[InvalidationJobReference, ...]
    audit_public_id: str


@dataclass(frozen=True, slots=True)
class IdentityAuditPage:
    player_public_id: str
    current_identity_revision: int
    operations: tuple[IdentityOperationSummary, ...]
    page: int
    page_size: int
    total_items: int
    available: bool
    reason_codes: tuple[str, ...] = ()


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def _mapping(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


# TheSuperHackers @feature Leex 23/08/2026 Coordinate reviewed identity mutations with idempotent durable analysis invalidation. (#TBD)
class PlayerIdentityWorkflowService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        identity_service: PlayerIdentityService,
        invalidation_planner: IdentityInvalidationPlanner,
        *,
        now_factory: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._identity = identity_service
        self._planner = invalidation_planner
        self._now_factory = now_factory

    def preview(self, draft: IdentityDraft) -> IdentityPreview:
        with self._session_factory() as session:
            try:
                player_ids = self._affected_player_public_ids(session, draft)
                expected = {item.player_public_id: item.expected_revision for item in draft.expected_revisions}
                players = tuple(session.scalars(select(Player).where(Player.public_id.in_(player_ids))))
                reasons: list[str] = []
                if set(player_ids) != {item.public_id for item in players}:
                    reasons.append("identity_subject_not_found")
                if set(expected) != set(player_ids) or any(
                    expected.get(item.public_id) != item.identity_revision for item in players
                ):
                    reasons.append("identity_revision_conflict")
                if isinstance(draft, MergeIdentityDraft) and (
                    not draft.source_player_public_ids
                    or draft.target_player_public_id in draft.source_player_public_ids
                    or len(set(draft.source_player_public_ids)) != len(draft.source_player_public_ids)
                ):
                    reasons.append("invalid_merge_membership")
                if isinstance(draft, SplitIdentityDraft):
                    selected = tuple(
                        session.scalars(
                            select(ReplayPlayer).where(ReplayPlayer.public_id.in_(draft.replay_player_public_ids))
                        )
                    )
                    alias = session.scalar(select(PlayerAlias).where(PlayerAlias.public_id == draft.alias_public_id))
                    if alias is None or alias.namespace != "embedded_replay_name":
                        reasons.append("invalid_split_alias")
                    if (
                        {item.public_id for item in selected} != set(draft.replay_player_public_ids)
                        or alias is None
                        or any(item.player_id != alias.player_id for item in selected)
                    ):
                        reasons.append("invalid_split_membership")
                snapshot = self._snapshot(session, draft, player_ids)
                impact = self._impact(session, player_ids)
            except (IdentityNotFoundError, IdentityInvariantError):
                snapshot = {"operation_kind": draft.operation_kind, "unavailable": True}
                impact = IdentityImpact(0, 0, 0, 0, 0, 0, 0, 0, ())
                reasons = ["identity_subject_not_found"]
        before = _canonical_digest(snapshot)
        after = _canonical_digest(
            {"before": before, "draft": self._draft_value(draft), "schema": "identity-preview-after-v1"}
        )
        return IdentityPreview(draft, before, after, impact, not reasons, tuple(sorted(set(reasons))))

    def execute(self, command: IdentityExecutionCommand) -> IdentityMutationReceipt:
        if not command.operator_label.strip() or not command.reason.strip():
            raise IdentityInvariantError("operator label and reason are required")
        preview = self.preview(command.draft)
        if preview.before_snapshot_digest != command.expected_before_snapshot_digest:
            raise IdentityConflictError("identity preview snapshot changed")
        if not preview.can_execute:
            raise IdentityConflictError("identity preview is no longer executable")
        expected = {item.player_public_id: item.expected_revision for item in command.draft.expected_revisions}
        if isinstance(command.draft, MergeIdentityDraft):
            receipt = self._identity.merge_players(
                target_player_public_id=command.draft.target_player_public_id,
                source_player_public_ids=command.draft.source_player_public_ids,
                expected_revisions=expected,
                actor=command.operator_label,
                reason=command.reason,
            )
        elif isinstance(command.draft, SplitIdentityDraft):
            receipt = self._identity.split_alias(
                alias_public_id=command.draft.alias_public_id,
                replay_player_public_ids=command.draft.replay_player_public_ids,
                new_display_name=command.draft.new_display_name,
                expected_revisions=expected,
                actor=command.operator_label,
                reason=command.reason,
            )
        else:
            receipt = self._identity.inverse_operation(
                operation_public_id=command.draft.operation_public_id,
                expected_revisions=expected,
                actor=command.operator_label,
                reason=command.reason,
            )
        jobs = self._schedule(receipt.operation_public_id)
        with self._session_factory() as session:
            operation = session.scalar(
                select(PlayerIdentityOperation).where(PlayerIdentityOperation.public_id == receipt.operation_public_id)
            )
            if operation is None:
                raise IdentityInvariantError("committed audit operation disappeared")
            summary = self._operation_summary(session, operation)
        return IdentityMutationReceipt(summary, jobs, receipt.operation_public_id)

    def _schedule(self, operation_public_id: str) -> tuple[InvalidationJobReference, ...]:
        try:
            plan = self._planner.ensure_identity_invalidation_plan(operation_public_id)
        except AnalysisPlanningError as error:
            return (
                InvalidationJobReference(
                    operation_public_id,
                    "identity_invalidation",
                    "durable_retry_required",
                    error.code,
                ),
            )
        # TheSuperHackers @fix Leex 23/08/2026 Preserve the committed audit as a retry obligation for any ordinary planner failure. (#TBD)
        except Exception:  # noqa: BLE001
            return (
                InvalidationJobReference(
                    operation_public_id,
                    "identity_invalidation",
                    "durable_retry_required",
                    "identity_invalidation_planner_failure",
                ),
            )
        jobs: list[InvalidationJobReference] = []
        for replay in plan.replays:
            for stage, public_id in (
                ("derive", replay.derive_job_public_id),
                ("assess", replay.assess_job_public_id),
                ("report", replay.report_job_public_id),
            ):
                if public_id is not None:
                    jobs.append(InvalidationJobReference(public_id, stage, "already_queued"))
            if replay.status == "awaiting_observations":
                jobs.append(
                    InvalidationJobReference(
                        operation_public_id,
                        "observations",
                        "durable_retry_required",
                        "awaiting_observations",
                    )
                )
        if not jobs:
            jobs.append(InvalidationJobReference(operation_public_id, "identity_invalidation", "already_queued"))
        return tuple(sorted(jobs, key=lambda item: (item.stage, item.job_public_id)))

    def retry_invalidation(self, operation_public_id: str) -> tuple[InvalidationJobReference, ...]:
        with self._session_factory() as session:
            operation = session.scalar(
                select(PlayerIdentityOperation).where(PlayerIdentityOperation.public_id == operation_public_id)
            )
            if operation is None:
                raise IdentityNotFoundError("unknown identity operation")
        return self._schedule(operation_public_id)

    def audit(self, player_public_id: str, page: int = 1, page_size: int = 25) -> IdentityAuditPage:
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError("invalid audit page")
        with self._session_factory() as session:
            player = session.scalar(select(Player).where(Player.public_id == player_public_id))
            if player is None:
                return IdentityAuditPage(player_public_id, 0, (), page, page_size, 0, False, ("player_not_found",))
            rows = tuple(
                operation
                for operation in session.scalars(
                    select(PlayerIdentityOperation).order_by(
                        PlayerIdentityOperation.created_at.desc(), PlayerIdentityOperation.public_id
                    )
                )
                if player_public_id in self._operation_player_ids(operation)
            )
            start = (page - 1) * page_size
            summaries = tuple(self._operation_summary(session, item) for item in rows[start : start + page_size])
            return IdentityAuditPage(
                player_public_id, player.identity_revision, summaries, page, page_size, len(rows), True
            )

    def _affected_player_public_ids(self, session: Session, draft: IdentityDraft) -> tuple[str, ...]:
        if isinstance(draft, MergeIdentityDraft):
            return tuple(sorted({draft.target_player_public_id, *draft.source_player_public_ids}))
        if isinstance(draft, SplitIdentityDraft):
            alias = session.scalar(select(PlayerAlias).where(PlayerAlias.public_id == draft.alias_public_id))
            if alias is None:
                raise IdentityNotFoundError("unknown alias")
            player = session.get(Player, alias.player_id)
            if player is None:
                raise IdentityNotFoundError("unknown alias player")
            return (player.public_id,)
        operation = session.scalar(
            select(PlayerIdentityOperation).where(PlayerIdentityOperation.public_id == draft.operation_public_id)
        )
        if operation is None:
            raise IdentityNotFoundError("unknown identity operation")
        return self._operation_player_ids(operation)

    def _snapshot(self, session: Session, draft: IdentityDraft, player_ids: tuple[str, ...]) -> dict[str, object]:
        players = tuple(
            session.scalars(select(Player).where(Player.public_id.in_(player_ids)).order_by(Player.public_id))
        )
        internal = tuple(player.id for player in players)
        aliases = (
            tuple(
                session.scalars(
                    select(PlayerAlias).where(PlayerAlias.player_id.in_(internal)).order_by(PlayerAlias.public_id)
                )
            )
            if internal
            else ()
        )
        replay_players = (
            tuple(
                session.scalars(
                    select(ReplayPlayer).where(ReplayPlayer.player_id.in_(internal)).order_by(ReplayPlayer.public_id)
                )
            )
            if internal
            else ()
        )
        return {
            "schema": "identity-workflow-snapshot-v1",
            "draft": self._draft_value(draft),
            "players": [
                {
                    "public_id": item.public_id,
                    "revision": item.identity_revision,
                    "retired": item.retired_at is not None,
                }
                for item in players
            ],
            "aliases": [
                {
                    "public_id": item.public_id,
                    "player_public_id": next(player.public_id for player in players if player.id == item.player_id),
                    "namespace": item.namespace,
                }
                for item in aliases
            ],
            "replay_players": [
                {
                    "public_id": item.public_id,
                    "player_public_id": next(player.public_id for player in players if player.id == item.player_id),
                }
                for item in replay_players
            ],
        }

    def _impact(self, session: Session, player_ids: tuple[str, ...]) -> IdentityImpact:
        internal = tuple(session.scalars(select(Player.id).where(Player.public_id.in_(player_ids))))
        replay_player_ids = (
            tuple(session.scalars(select(ReplayPlayer.id).where(ReplayPlayer.player_id.in_(internal))))
            if internal
            else ()
        )
        replay_ids = (
            tuple(set(session.scalars(select(ReplayPlayer.replay_id).where(ReplayPlayer.id.in_(replay_player_ids)))))
            if replay_player_ids
            else ()
        )
        run_ids = (
            tuple(session.scalars(select(LongitudinalRun.id).where(LongitudinalRun.player_id.in_(internal))))
            if internal
            else ()
        )
        result_ids = (
            tuple(
                session.scalars(
                    select(LongitudinalResult.id).where(LongitudinalResult.longitudinal_run_id.in_(run_ids))
                )
            )
            if run_ids
            else ()
        )
        feature_sets = (
            session.scalar(
                select(func.count()).select_from(FeatureSet).where(FeatureSet.replay_player_id.in_(replay_player_ids))
            )
            if replay_player_ids
            else 0
        )
        reports = (
            session.scalar(
                select(func.count()).select_from(Report).where(Report.replay_player_id.in_(replay_player_ids))
            )
            if replay_player_ids
            else 0
        )
        counts = (
            ("derive", len(replay_ids)),
            ("assess", len(replay_ids)),
            ("report", len(replay_ids)),
        )
        return IdentityImpact(
            len(internal),
            len(tuple(session.scalars(select(PlayerAlias.id).where(PlayerAlias.player_id.in_(internal)))))
            if internal
            else 0,
            len(replay_player_ids),
            len(replay_ids),
            int(feature_sets or 0),
            len(run_ids),
            len(result_ids),
            int(reports or 0),
            counts,
        )

    @staticmethod
    def _draft_value(draft: IdentityDraft) -> dict[str, object]:
        values = {name: getattr(draft, name) for name in draft.__dataclass_fields__}
        values["expected_revisions"] = [
            {"player_public_id": item.player_public_id, "expected_revision": item.expected_revision}
            for item in draft.expected_revisions
        ]
        return values

    @staticmethod
    def _operation_player_ids(operation: PlayerIdentityOperation) -> tuple[str, ...]:
        affected = _mapping(operation.affected_revisions_json)
        players = affected.get("players")
        if not isinstance(players, list):
            return ()
        return tuple(sorted(str(_mapping(item).get("player_public_id")) for item in players))

    def _operation_summary(self, session: Session, operation: PlayerIdentityOperation) -> IdentityOperationSummary:
        inverse = (
            session.get(PlayerIdentityOperation, operation.inverse_of_operation_id)
            if operation.inverse_of_operation_id
            else None
        )
        already_inversed = bool(
            session.scalar(
                select(func.count())
                .select_from(PlayerIdentityOperation)
                .where(PlayerIdentityOperation.inverse_of_operation_id == operation.id)
            )
        )
        reversible = operation.operation_kind in {"merge_players", "split_alias"} and not already_inversed
        kind = cast(
            Literal["auto_link", "merge_players", "split_alias", "attach_external_alias", "inverse"],
            operation.operation_kind,
        )
        revisions = tuple(
            RevisionPrecondition(
                str(_mapping(item).get("player_public_id")), int(cast(int, _mapping(item).get("identity_revision")))
            )
            for item in cast(list[object], _mapping(operation.affected_revisions_json).get("players", []))
        )
        created = operation.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        else:
            created = created.astimezone(UTC)
        return IdentityOperationSummary(
            operation.public_id,
            kind,
            None if inverse is None else inverse.public_id,
            operation.actor,
            operation.reason,
            created,
            tuple(sorted(revisions)),
            reversible,
            None if reversible else "operation_not_reversible",
        )
