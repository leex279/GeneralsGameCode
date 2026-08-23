"""Audited identity preview and execution web contracts."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import Response

from generals_replay_analyzer.web.app import OneTimeFormTokenRegistry
from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import PublicProblem, problem_response
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    ExecuteIdentityChangeDTO,
    IdentityAuditPageDTO,
    IdentityImpactDTO,
    IdentityMutationReceiptDTO,
    IdentityOperationSummaryDTO,
    IdentityPreviewDTO,
    InvalidationJobReferenceDTO,
    MergeIdentityDraftDTO,
    RevisionPreconditionDTO,
)
from generals_replay_analyzer.web.routes.players import router

TARGET_ID = "123e4567-e89b-42d3-a456-426614174320"
SOURCE_ID = "123e4567-e89b-42d3-a456-426614174321"
DIGEST = "d" * 64
ALIAS_ID = "123e4567-e89b-42d3-a456-426614174322"
REPLAY_PLAYER_ID = "123e4567-e89b-42d3-a456-426614174323"
OPERATION_ID = "123e4567-e89b-42d3-a456-426614174324"
AUDIT_ID = "123e4567-e89b-42d3-a456-426614174325"
JOB_ID = "123e4567-e89b-42d3-a456-426614174326"


async def _public_problem(_request: Request, error: Exception) -> Response:
    assert isinstance(error, PublicProblem)
    return problem_response(
        error.status,
        title="Request rejected",
        code=error.code,
        detail=error.detail,
    )


class _IdentityPort:
    def __init__(self) -> None:
        self.previews: list[object] = []
        self.executions: list[ExecuteIdentityChangeDTO] = []
        self.retry_calls: list[str] = []

    def audit(self, player_public_id: str, page: int, page_size: int) -> IdentityAuditPageDTO:
        raise AssertionError((player_public_id, page, page_size))

    def preview(self, draft: object) -> IdentityPreviewDTO:
        assert isinstance(draft, MergeIdentityDraftDTO)
        self.previews.append(draft)
        return IdentityPreviewDTO(
            schema_version="player-identity-preview-v1",
            draft=draft,
            before_snapshot_digest=DIGEST,
            expected_after_snapshot_digest="e" * 64,
            impact=IdentityImpactDTO(
                canonical_player_count=2,
                alias_count=3,
                replay_player_count=10,
                replay_count=8,
                feature_set_count=6,
                longitudinal_run_count=2,
                longitudinal_result_count=7,
                report_count=4,
                invalidation_stage_counts=(("derive_features", 6), ("render_report", 4)),
            ),
            can_execute=True,
        )

    def execute(self, command: ExecuteIdentityChangeDTO) -> IdentityMutationReceiptDTO:
        self.executions.append(command)
        raise AssertionError("preview must not execute")

    def retry_invalidation(self, operation_public_id: str) -> tuple[InvalidationJobReferenceDTO, ...]:
        self.retry_calls.append(operation_public_id)
        raise AssertionError("retry must not run")


def _operation() -> IdentityOperationSummaryDTO:
    return IdentityOperationSummaryDTO(
        operation_public_id=OPERATION_ID,
        operation_kind="merge_players",
        operator_label="Local reviewer",
        reason="Correct exact replay membership",
        created_at_utc=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
        affected_player_revisions=(RevisionPreconditionDTO(player_public_id=TARGET_ID, expected_revision=5),),
        inverse_allowed=True,
    )


class _WorkflowPort(_IdentityPort):
    def audit(self, player_public_id: str, page: int, page_size: int) -> IdentityAuditPageDTO:
        return IdentityAuditPageDTO(
            player_public_id=player_public_id,
            current_identity_revision=5,
            operations=(_operation(),),
            page=page,
            page_size=page_size,
            total_items=1,
            availability=AvailabilityDTO(state="available"),
        )

    def preview(self, draft: object) -> IdentityPreviewDTO:
        self.previews.append(draft)
        return IdentityPreviewDTO(
            schema_version="player-identity-preview-v1",
            draft=draft,
            before_snapshot_digest=DIGEST,
            expected_after_snapshot_digest="e" * 64,
            impact=IdentityImpactDTO(
                canonical_player_count=1,
                alias_count=1,
                replay_player_count=1,
                replay_count=1,
                feature_set_count=1,
                longitudinal_run_count=1,
                longitudinal_result_count=1,
                report_count=1,
                invalidation_stage_counts=(("derive_features", 1),),
            ),
            can_execute=True,
        )

    def execute(self, command: ExecuteIdentityChangeDTO) -> IdentityMutationReceiptDTO:
        self.executions.append(command)
        return IdentityMutationReceiptDTO(
            operation=_operation(),
            invalidation_jobs=(
                InvalidationJobReferenceDTO(
                    job_public_id=JOB_ID,
                    stage="derive_features",
                    state="already_queued",
                ),
            ),
            audit_public_id=AUDIT_ID,
        )

    def retry_invalidation(self, operation_public_id: str) -> tuple[InvalidationJobReferenceDTO, ...]:
        self.retry_calls.append(operation_public_id)
        return (
            InvalidationJobReferenceDTO(
                job_public_id=JOB_ID,
                stage="derive_features",
                state="already_queued",
            ),
        )


def test_merge_preview_is_read_only_and_shows_exact_impact_before_confirmation() -> None:
    """Catch preview mutating identity or hiding revisions, consequences, and invalidation work."""
    port = _IdentityPort()
    registry = OneTimeFormTokenRegistry()
    issued = registry.issue()
    app = FastAPI()
    app.add_exception_handler(PublicProblem, _public_problem)
    app.state.form_csrf_token_registry = registry
    app.include_router(router)
    app.dependency_overrides[application_port] = lambda: port

    with TestClient(app) as client:
        client.cookies.set("_csrf", issued.cookie_value)
        response = client.post(
            "/players/identity/previews/merge",
            data={
                "_csrf": issued.hidden_value,
                "target_player_public_id": TARGET_ID,
                "source_player_public_id": SOURCE_ID,
                "expected_revision": "4",
                "source_expected_revision": "2",
            },
        )

    assert response.status_code == 200
    assert len(port.previews) == 1 and port.executions == []
    assert "Confirm Identity Change" in response.text
    assert "This preview does not mutate identity" in response.text
    assert "operator label" in response.text.lower() and "not authentication" in response.text.lower()
    assert "Replay Players Affected" in response.text and ">10<" in response.text
    assert "render_report: 4 durable work items" in response.text
    assert f'name="target_player_public_id" value="{TARGET_ID}"' in response.text


def test_identity_preview_rejects_missing_csrf_before_port_invocation() -> None:
    """Catch identity impact work running before the one-time native-form guard."""
    port = _IdentityPort()
    app = FastAPI()
    app.add_exception_handler(PublicProblem, _public_problem)
    app.state.form_csrf_token_registry = OneTimeFormTokenRegistry()
    app.include_router(router)
    app.dependency_overrides[application_port] = lambda: port

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/players/identity/previews/merge",
            data={
                "target_player_public_id": TARGET_ID,
                "source_player_public_id": SOURCE_ID,
                "expected_revision": "4",
                "source_expected_revision": "2",
            },
        )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_rejected"
    assert port.previews == [] and port.executions == []


def test_identity_audit_issues_confirmation_controls_and_keeps_inverse_explicit() -> None:
    """Catch append-only audit history losing revision, reason, or keyboard-reachable inverse controls."""
    port = _WorkflowPort()
    registry = OneTimeFormTokenRegistry()
    app = FastAPI()
    app.state.form_csrf_token_registry = registry
    app.include_router(router)
    app.dependency_overrides[application_port] = lambda: port

    with TestClient(app) as client:
        response = client.get(f"/players/{TARGET_ID}/identity", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert "Current Revision 5" in response.text
    assert "Local reviewer" in response.text and "Correct exact replay membership" in response.text
    assert f"Preview Inverse for {OPERATION_ID}" in response.text
    assert f'action="/players/{TARGET_ID}/identity/invalidation/{OPERATION_ID}/retry"' in response.text
    assert "Ensure/retry invalidation" in response.text
    assert "operator label is audit text, not authentication" in response.text.lower()
    assert response.cookies.get("_csrf")


def test_split_and_inverse_previews_preserve_explicit_membership_and_revision() -> None:
    """Catch split/inverse preview inferring membership or accepting fuzzy/provenance selectors."""
    port = _WorkflowPort()
    registry = OneTimeFormTokenRegistry()
    app = FastAPI()
    app.state.form_csrf_token_registry = registry
    app.include_router(router)
    app.dependency_overrides[application_port] = lambda: port

    with TestClient(app) as client:
        split_token = registry.issue()
        client.cookies.set("_csrf", split_token.cookie_value)
        split = client.post(
            "/players/identity/previews/split",
            data={
                "_csrf": split_token.hidden_value,
                "player_public_id": TARGET_ID,
                "expected_revision": "5",
                "alias_public_id": ALIAS_ID,
                "replay_player_public_id": REPLAY_PLAYER_ID,
                "new_display_name": "Leex tournament account",
            },
        )
        inverse_token = registry.issue()
        client.cookies.clear()
        client.cookies.set("_csrf", inverse_token.cookie_value)
        inverse = client.post(
            "/players/identity/previews/inverse",
            data={
                "_csrf": inverse_token.hidden_value,
                "player_public_id": TARGET_ID,
                "expected_revision": "5",
                "operation_public_id": OPERATION_ID,
            },
        )

    assert split.status_code == inverse.status_code == 200
    assert port.previews[0].operation_kind == "split_alias"
    assert port.previews[0].replay_player_public_ids == (REPLAY_PLAYER_ID,)
    assert port.previews[1].operation_kind == "inverse"
    assert port.previews[1].operation_public_id == OPERATION_ID
    assert "Leex tournament account" in split.text and OPERATION_ID in inverse.text


def test_confirmed_merge_executes_once_and_requires_durable_invalidation_receipt() -> None:
    """Catch the route mutating identity without one coordinator-owned durable invalidation receipt."""
    port = _WorkflowPort()
    registry = OneTimeFormTokenRegistry()
    issued = registry.issue()
    app = FastAPI()
    app.state.form_csrf_token_registry = registry
    app.include_router(router)
    app.dependency_overrides[application_port] = lambda: port

    with TestClient(app) as client:
        client.cookies.set("_csrf", issued.cookie_value)
        response = client.post(
            "/players/identity/merge",
            data={
                "_csrf": issued.hidden_value,
                "target_player_public_id": TARGET_ID,
                "source_player_public_id": SOURCE_ID,
                "expected_revision": "4",
                "source_expected_revision": "2",
                "expected_before_snapshot_digest": DIGEST,
                "operator_label": "Local reviewer",
                "reason": "Exact embedded-name evidence",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/players/{TARGET_ID}/identity?invalidation_operation_public_id={OPERATION_ID}"
        "&invalidation_state=already_queued"
    )
    assert len(port.executions) == 1
    assert port.executions[0].expected_before_snapshot_digest == DIGEST
    assert port.executions[0].operator_label == "Local reviewer"


def test_audit_page_recovers_a_lost_execute_response_with_csrf_and_idempotent_retry() -> None:
    """Catch a committed audit operation becoming unrecoverable when its execute response is lost."""
    port = _WorkflowPort()
    registry = OneTimeFormTokenRegistry()
    app = FastAPI()
    app.state.form_csrf_token_registry = registry
    app.include_router(router)
    app.dependency_overrides[application_port] = lambda: port

    with TestClient(app) as client:
        audit = client.get(f"/players/{TARGET_ID}/identity", headers={"accept": "text/html"})
        hidden = re.search(r'name="_csrf" value="([^"]+)"', audit.text)
        assert hidden is not None
        first = client.post(
            f"/players/{TARGET_ID}/identity/invalidation/{OPERATION_ID}/retry",
            data={"_csrf": hidden.group(1)},
            follow_redirects=False,
        )
        refreshed = client.get(first.headers["location"], headers={"accept": "text/html"})
        assert "Invalidation already_queued" in refreshed.text
        assert OPERATION_ID in refreshed.text
        second_hidden = re.search(r'name="_csrf" value="([^"]+)"', refreshed.text)
        assert second_hidden is not None
        second = client.post(
            f"/players/{TARGET_ID}/identity/invalidation/{OPERATION_ID}/retry",
            data={"_csrf": second_hidden.group(1)},
            follow_redirects=False,
        )

    expected = (
        f"/players/{TARGET_ID}/identity?invalidation_operation_public_id={OPERATION_ID}"
        "&invalidation_state=already_queued"
    )
    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == second.headers["location"] == expected
    assert port.retry_calls == [OPERATION_ID, OPERATION_ID]


def test_retry_invalidation_rejects_missing_csrf_before_port_invocation() -> None:
    """Catch the durable audit retry endpoint being exposed as an unguarded mutation."""
    port = _WorkflowPort()
    app = FastAPI()
    app.add_exception_handler(PublicProblem, _public_problem)
    app.state.form_csrf_token_registry = OneTimeFormTokenRegistry()
    app.include_router(router)
    app.dependency_overrides[application_port] = lambda: port

    with TestClient(app) as client:
        response = client.post(
            f"/players/{TARGET_ID}/identity/invalidation/{OPERATION_ID}/retry",
            data={},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_rejected"
    assert port.retry_calls == []


def test_execute_redirect_surfaces_durable_retry_required_with_reason() -> None:
    """Catch a committed mutation silently redirecting while planner recovery is still required."""

    class _RetryRequiredPort(_WorkflowPort):
        def execute(self, command: ExecuteIdentityChangeDTO) -> IdentityMutationReceiptDTO:
            receipt = super().execute(command)
            return receipt.model_copy(
                update={
                    "invalidation_jobs": (
                        InvalidationJobReferenceDTO(
                            job_public_id=OPERATION_ID,
                            stage="identity_invalidation",
                            state="durable_retry_required",
                            reason_code="identity_invalidation_planner_failure",
                        ),
                    )
                }
            )

    port = _RetryRequiredPort()
    registry = OneTimeFormTokenRegistry()
    issued = registry.issue()
    app = FastAPI()
    app.state.form_csrf_token_registry = registry
    app.include_router(router)
    app.dependency_overrides[application_port] = lambda: port

    with TestClient(app) as client:
        client.cookies.set("_csrf", issued.cookie_value)
        response = client.post(
            "/players/identity/merge",
            data={
                "_csrf": issued.hidden_value,
                "target_player_public_id": TARGET_ID,
                "source_player_public_id": SOURCE_ID,
                "expected_revision": "4",
                "source_expected_revision": "2",
                "expected_before_snapshot_digest": DIGEST,
                "operator_label": "Local reviewer",
                "reason": "Exact embedded-name evidence",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].endswith(
        "invalidation_state=durable_retry_required&invalidation_reason_code=identity_invalidation_planner_failure"
    )


def test_identity_routes_fail_closed_for_invalid_ids_operations_and_missing_capability() -> None:
    """Catch unknown operations or absent workflow capability reaching mutation code."""
    registry = OneTimeFormTokenRegistry()
    app = FastAPI()
    app.add_exception_handler(PublicProblem, _public_problem)
    app.state.form_csrf_token_registry = registry
    app.include_router(router)
    app.dependency_overrides[application_port] = object

    with TestClient(app) as client:
        invalid_id = client.get("/players/not-a-uuid/identity")
        missing = client.get(f"/players/{TARGET_ID}/identity")
        issued = registry.issue()
        client.cookies.set("_csrf", issued.cookie_value)
        invalid_operation = client.post(
            "/players/identity/previews/fuzzy",
            data={"_csrf": issued.hidden_value},
        )

    assert invalid_id.status_code == 404
    assert missing.status_code == 503 and missing.json()["code"] == "identity_workflow_adapter_pending"
    assert invalid_operation.status_code == 422


@pytest.mark.parametrize(
    ("status", "code"),
    ((409, "identity_revision_changed"), (422, "invalid_identity_membership"), (503, "identity_dependency_busy")),
)
def test_identity_execution_preserves_typed_conflict_validation_and_busy_status(status: int, code: str) -> None:
    """Catch workflow failures becoming redirects, generic 500s, or best-effort job calls."""

    class _ErrorPort(_WorkflowPort):
        def execute(self, command: ExecuteIdentityChangeDTO) -> IdentityMutationReceiptDTO:
            self.executions.append(command)
            raise PublicProblem(status=status, code=code, detail="Identity workflow rejected the command")

    port = _ErrorPort()
    registry = OneTimeFormTokenRegistry()
    issued = registry.issue()
    app = FastAPI()
    app.add_exception_handler(PublicProblem, _public_problem)
    app.state.form_csrf_token_registry = registry
    app.include_router(router)
    app.dependency_overrides[application_port] = lambda: port

    with TestClient(app) as client:
        client.cookies.set("_csrf", issued.cookie_value)
        response = client.post(
            "/players/identity/merge",
            data={
                "_csrf": issued.hidden_value,
                "target_player_public_id": TARGET_ID,
                "source_player_public_id": SOURCE_ID,
                "expected_revision": "4",
                "source_expected_revision": "2",
                "expected_before_snapshot_digest": DIGEST,
                "operator_label": "Local reviewer",
                "reason": "Exact embedded-name evidence",
            },
        )

    assert response.status_code == status
    assert response.json()["code"] == code
    assert len(port.executions) == 1
