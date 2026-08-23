"""Version-bound player history and audited identity workflows."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ValidationError
from starlette.responses import RedirectResponse, Response

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import PublicProblem, problem_response
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    ExecuteIdentityChangeDTO,
    IdentityDraftDTO,
    IdentityLandingDTO,
    IdentityOperationKind,
    InvalidationJobReferenceDTO,
    InverseIdentityDraftDTO,
    MergeIdentityDraftDTO,
    PlayerHistoryPort,
    PlayerIdentityWorkflowPort,
    PlayerIndexQueryDTO,
    PlayerProfileQueryDTO,
    PlayerProfileSelectionDTO,
    RevisionPreconditionDTO,
    SplitIdentityDraftDTO,
    WebApplicationPort,
)
from generals_replay_analyzer.web.presentation.shell import (
    ShellContextDTO,
    accepts_html,
    identity_shell,
    template_response,
)
from generals_replay_analyzer.web.viewmodels.players import player_index_view, player_profile_view

router = APIRouter(tags=["players"])
_INDEX_FIELDS = frozenset(
    {"page", "page_size", "search", "faction", "opponent_faction", "map_public_id", "patch", "active_only", "sort"}
)
_PROFILE_FILTER_FIELDS = frozenset(
    {
        "page",
        "page_size",
        "faction",
        "opponent_faction",
        "opponent_player_public_id",
        "map_public_id",
        "patch",
        "result",
        "start_position",
        "date_from_utc",
        "date_to_utc",
        "quality_policy_digest",
    }
)
_PROFILE_BINDING_FIELDS = frozenset(
    {
        "expected_identity_revision",
        "longitudinal_run_id",
        "report_public_id",
        "definition_binding_digest",
        "profile_input_digest",
    }
)


def _public_id(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("invalid public ID")
    return value


def _history_port(port: WebApplicationPort) -> PlayerHistoryPort:
    if not isinstance(port, PlayerHistoryPort):
        raise PublicProblem(status=503, code="player_history_adapter_pending", detail="Player history is unavailable")
    return cast(PlayerHistoryPort, port)


def _identity_port(port: WebApplicationPort) -> PlayerIdentityWorkflowPort:
    if not isinstance(port, PlayerIdentityWorkflowPort):
        raise PublicProblem(
            status=503, code="identity_workflow_adapter_pending", detail="Identity operations are unavailable"
        )
    return cast(PlayerIdentityWorkflowPort, port)


def _shell(availability: AvailabilityDTO) -> ShellContextDTO:
    return identity_shell(IdentityLandingDTO(generated_at=datetime.now(UTC), availability=availability))


def _html_only(request: Request) -> Response | None:
    if accepts_html(request.headers.get("accept")):
        return None
    return problem_response(
        406, title="Not Acceptable", code="not_acceptable", detail="This route provides only text/html"
    )


def _single_values(request: Request, allowed: frozenset[str]) -> dict[str, str]:
    if set(request.query_params).difference(allowed) or any(
        len(request.query_params.getlist(name)) != 1 for name in request.query_params
    ):
        raise ValueError("invalid query shape")
    return dict(request.query_params)


def _profile_values(request: Request) -> dict[str, object]:
    allowed = _PROFILE_FILTER_FIELDS | _PROFILE_BINDING_FIELDS
    if set(request.query_params).difference(allowed):
        raise ValueError("invalid profile query")
    repeated = {"longitudinal_run_id", "report_public_id"}
    if any(len(request.query_params.getlist(name)) != 1 for name in request.query_params if name not in repeated):
        raise ValueError("invalid profile query")
    values: dict[str, object] = {
        name: request.query_params[name] for name in request.query_params if name not in repeated
    }
    values["longitudinal_run_ids"] = tuple(sorted(set(request.query_params.getlist("longitudinal_run_id"))))
    values["report_public_ids"] = tuple(sorted(set(request.query_params.getlist("report_public_id"))))
    return values


def _fixed_profile_url(query: PlayerProfileQueryDTO) -> str:
    values = query.model_dump(mode="json", exclude_none=True)
    pairs: list[tuple[str, object]] = []
    order = (
        "page",
        "page_size",
        "faction",
        "opponent_faction",
        "opponent_player_public_id",
        "map_public_id",
        "patch",
        "result",
        "start_position",
        "date_from_utc",
        "date_to_utc",
        "quality_policy_digest",
        "expected_identity_revision",
        "longitudinal_run_ids",
        "report_public_ids",
        "definition_binding_digest",
        "profile_input_digest",
    )
    for name in order:
        value = values.get(name)
        if value is None:
            continue
        if name in {"longitudinal_run_ids", "report_public_ids"}:
            singular = "longitudinal_run_id" if name == "longitudinal_run_ids" else "report_public_id"
            pairs.extend((singular, item) for item in value)
        else:
            pairs.append((name, value))
    return f"/players/{query.player_public_id}?{urlencode(pairs)}"


def _canonical_json(value: BaseModel) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


# TheSuperHackers @feature Leex 23/08/2026 Bind player pages to immutable public profile queries. (#TBD)
@router.get("/players", summary="Player directory")
def players_index(
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    negotiated = _html_only(request)
    if negotiated is not None:
        return negotiated
    try:
        query = PlayerIndexQueryDTO.model_validate(_single_values(request, _INDEX_FIELDS))
    except (ValueError, ValidationError):
        return problem_response(
            422, title="Invalid player query", code="invalid_player_query", detail="Player query is invalid"
        )
    page = _history_port(port).list_players(query)
    if page.query != query:
        raise PublicProblem(
            status=409, code="player_query_mismatch", detail="Player result is outside the requested scope"
        )
    return template_response(
        request, "players/index.html", _shell(page.availability), context={"view": player_index_view(page)}
    )


@router.get("/players/{player_public_id}", summary="Fixed player profile")
def player_profile(
    player_public_id: str,
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    negotiated = _html_only(request)
    if negotiated is not None:
        return negotiated
    try:
        player_id = _public_id(player_public_id)
        values = _profile_values(request)
        is_fixed = all(
            name in values
            for name in ("expected_identity_revision", "definition_binding_digest", "profile_input_digest")
        )
        if is_fixed:
            query: PlayerProfileQueryDTO | PlayerProfileSelectionDTO = PlayerProfileQueryDTO.model_validate(
                {"player_public_id": player_id, **values}
            )
        else:
            if set(values).intersection(
                {
                    "longitudinal_run_ids",
                    "report_public_ids",
                    "expected_identity_revision",
                    "definition_binding_digest",
                    "profile_input_digest",
                }
            ) and any(
                values.get(name)
                for name in (
                    "longitudinal_run_ids",
                    "report_public_ids",
                    "expected_identity_revision",
                    "definition_binding_digest",
                    "profile_input_digest",
                )
            ):
                raise ValueError("partial fixed binding")
            query = PlayerProfileSelectionDTO.model_validate(
                {
                    "player_public_id": player_id,
                    **{key: value for key, value in values.items() if key in _PROFILE_FILTER_FIELDS},
                }
            )
    except (ValueError, ValidationError):
        return problem_response(
            422,
            title="Invalid player profile query",
            code="invalid_player_profile_query",
            detail="Player profile query is invalid",
        )
    history = _history_port(port)
    if not is_fixed:
        resolution = history.resolve_profile(query)
        if resolution.state == "resolved":
            assert resolution.fixed_query is not None
            if resolution.fixed_query.player_public_id != player_id:
                raise PublicProblem(
                    status=409,
                    code="player_profile_identity_mismatch",
                    detail="Player profile identity is inconsistent",
                )
            return RedirectResponse(_fixed_profile_url(resolution.fixed_query), status_code=303)
        return problem_response(
            503,
            title="Player profile unavailable",
            code=resolution.reason_codes[0] if resolution.reason_codes else "player_profile_unavailable",
            detail="Player profile is unavailable",
        )
    assert isinstance(query, PlayerProfileQueryDTO)
    profile = history.get_profile(query)
    if profile.query != query or profile.player.player_public_id != player_id:
        raise PublicProblem(
            status=409, code="player_profile_identity_mismatch", detail="Player profile identity is inconsistent"
        )
    return template_response(
        request, "players/detail.html", _shell(profile.availability), context={"view": player_profile_view(profile)}
    )


@router.get("/api/players/{player_public_id}/profile", summary="Fixed player profile data")
def player_profile_json(
    player_public_id: str,
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    try:
        query = PlayerProfileQueryDTO.model_validate(
            {"player_public_id": _public_id(player_public_id), **_profile_values(request)}
        )
    except (ValueError, ValidationError):
        return problem_response(
            422,
            title="Invalid player profile query",
            code="invalid_player_profile_query",
            detail="Player profile query is invalid",
        )
    profile = _history_port(port).get_profile(query)
    if profile.query != query or profile.player.player_public_id != query.player_public_id:
        raise PublicProblem(
            status=409, code="player_profile_identity_mismatch", detail="Player profile identity is inconsistent"
        )
    body = _canonical_json(profile)
    etag = hashlib.sha256(body).hexdigest()
    if request.headers.get("if-none-match", "").strip('"') == etag:
        return Response(status_code=304, headers={"etag": f'"{etag}"'})
    return Response(body, media_type="application/json", headers={"etag": f'"{etag}"'})


def _issue_csrf(request: Request) -> tuple[str, str]:
    token = request.app.state.form_csrf_token_registry.issue()
    return token.hidden_value, token.cookie_value


def _set_csrf_cookie(response: Response, cookie: str) -> None:
    response.set_cookie("_csrf", cookie, max_age=600, httponly=True, samesite="strict", path="/")


def _invalidation_location(
    player_public_id: str,
    operation_public_id: str,
    jobs: tuple[InvalidationJobReferenceDTO, ...],
) -> str:
    if not jobs:
        raise PublicProblem(
            status=503, code="identity_invalidation_not_durable", detail="Durable invalidation is unavailable"
        )
    priority = {"already_queued": 0, "pending": 1, "durable_retry_required": 2}
    state = max((item.state for item in jobs), key=priority.__getitem__)
    params = [
        ("invalidation_operation_public_id", operation_public_id),
        ("invalidation_state", state),
    ]
    reason_code = next(
        (item.reason_code for item in jobs if item.state == state and item.reason_code is not None),
        None,
    )
    if reason_code is not None:
        params.append(("invalidation_reason_code", reason_code))
    return f"/players/{player_public_id}/identity?{urlencode(params)}"


@router.get("/players/{player_public_id}/identity", summary="Player identity audit")
def player_identity(
    player_public_id: str,
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
    page: int = 1,
    page_size: int = 25,
    invalidation_operation_public_id: str | None = None,
    invalidation_state: Literal["pending", "already_queued", "durable_retry_required"] | None = None,
    invalidation_reason_code: str | None = None,
) -> Response:
    try:
        player_id = _public_id(player_public_id)
    except ValueError:
        return problem_response(404, title="Player not found", code="player_not_found", detail="Player was not found")
    audit = _identity_port(port).audit(player_id, page, page_size)
    if audit.player_public_id != player_id:
        raise PublicProblem(status=409, code="identity_audit_mismatch", detail="Identity audit is inconsistent")
    invalidation_status: tuple[str, str, str | None] | None = None
    if invalidation_operation_public_id is not None or invalidation_state is not None:
        try:
            operation_id = _public_id(invalidation_operation_public_id or "")
        except ValueError:
            return problem_response(
                422,
                title="Invalid invalidation state",
                code="invalid_identity_invalidation_state",
                detail="Identity invalidation state is invalid",
            )
        if invalidation_state is None:
            return problem_response(
                422,
                title="Invalid invalidation state",
                code="invalid_identity_invalidation_state",
                detail="Identity invalidation state is invalid",
            )
        invalidation_status = (operation_id, invalidation_state, invalidation_reason_code)
    hidden, cookie = _issue_csrf(request)
    response = template_response(
        request,
        "players/identity.html",
        _shell(audit.availability),
        context={"audit": audit, "csrf_token": hidden, "invalidation_status": invalidation_status},
    )
    _set_csrf_cookie(response, cookie)
    return response


async def _identity_form(request: Request) -> dict[str, list[str]]:
    form = await request.form()
    allowed = {
        "_csrf",
        "target_player_public_id",
        "source_player_public_id",
        "expected_revision",
        "source_expected_revision",
        "player_public_id",
        "alias_public_id",
        "replay_player_public_id",
        "new_display_name",
        "operation_public_id",
        "expected_before_snapshot_digest",
        "operator_label",
        "reason",
    }
    if any(name not in allowed for name, _value in form.multi_items()):
        raise PublicProblem(status=422, code="invalid_identity_form", detail="Identity form is invalid")
    values = {name: [str(value) for value in form.getlist(name)] for name in allowed if name in form}
    if request.headers.get("x-csrf-token") is None:
        tokens = values.get("_csrf", [])
        if len(tokens) != 1 or not request.app.state.form_csrf_token_registry.consume(
            request.cookies.get("_csrf"), tokens[0]
        ):
            raise PublicProblem(status=403, code="csrf_rejected", detail="The request CSRF token was rejected")
    return values


def _one(values: dict[str, list[str]], name: str) -> str:
    items = values.get(name, [])
    if len(items) != 1 or not items[0].strip():
        raise ValueError(name)
    return items[0].strip()


def _revisions(values: dict[str, list[str]]) -> tuple[RevisionPreconditionDTO, ...]:
    players = values.get("player_public_id", [])
    revisions = values.get("expected_revision", [])
    if len(players) != len(revisions) or not players:
        raise ValueError("expected revisions")
    return tuple(
        RevisionPreconditionDTO(player_public_id=_public_id(player), expected_revision=int(revision))
        for player, revision in zip(players, revisions, strict=True)
    )


def _draft(kind: IdentityOperationKind, values: dict[str, list[str]]) -> IdentityDraftDTO:
    if kind == "merge_players":
        target = _public_id(_one(values, "target_player_public_id"))
        source = _public_id(_one(values, "source_player_public_id"))
        return MergeIdentityDraftDTO(
            operation_kind=kind,
            target_player_public_id=target,
            source_player_public_ids=(source,),
            expected_revisions=(
                RevisionPreconditionDTO(
                    player_public_id=target, expected_revision=int(_one(values, "expected_revision"))
                ),
                RevisionPreconditionDTO(
                    player_public_id=source, expected_revision=int(_one(values, "source_expected_revision"))
                ),
            ),
        )
    if kind == "split_alias":
        return SplitIdentityDraftDTO(
            operation_kind=kind,
            alias_public_id=_public_id(_one(values, "alias_public_id")),
            replay_player_public_ids=tuple(
                sorted({_public_id(value) for value in values.get("replay_player_public_id", [])})
            ),
            new_display_name=_one(values, "new_display_name"),
            expected_revisions=_revisions(values),
        )
    return InverseIdentityDraftDTO(
        operation_kind=kind,
        operation_public_id=_public_id(_one(values, "operation_public_id")),
        expected_revisions=_revisions(values),
    )


def _draft_fields(draft: IdentityDraftDTO) -> tuple[tuple[str, str], ...]:
    values = draft.model_dump(mode="json")
    fields: list[tuple[str, str]] = []
    for name, value in values.items():
        if name == "operation_kind":
            continue
        if name == "expected_revisions":
            for revision in value:
                fields.extend(
                    (
                        ("player_public_id", revision["player_public_id"]),
                        ("expected_revision", str(revision["expected_revision"])),
                    )
                )
        elif isinstance(value, list):
            singular = name.removesuffix("s")
            fields.extend((singular, str(item)) for item in value)
        else:
            fields.append((name, str(value)))
    return tuple(fields)


def _affected_player(draft: IdentityDraftDTO) -> str:
    if isinstance(draft, MergeIdentityDraftDTO):
        return draft.target_player_public_id
    return draft.expected_revisions[0].player_public_id


@router.post("/players/identity/previews/{operation}", summary="Preview identity change")
async def preview_identity(
    operation: str,
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    operation_map = {"merge": "merge_players", "split": "split_alias", "inverse": "inverse"}
    try:
        kind = cast(IdentityOperationKind, operation_map[operation])
        values = await _identity_form(request)
        draft = _draft(kind, values)
    except (KeyError, ValueError, ValidationError):
        return problem_response(
            422, title="Invalid identity preview", code="invalid_identity_preview", detail="Identity preview is invalid"
        )
    preview = _identity_port(port).preview(draft)
    hidden, cookie = _issue_csrf(request)
    response = template_response(
        request,
        "players/_identity_confirmation.html",
        _shell(AvailabilityDTO(state="available")),
        context={
            "preview": preview,
            "csrf_token": hidden,
            "execute_path": operation,
            "draft_fields": _draft_fields(draft),
            "affected_player_public_id": _affected_player(draft),
        },
    )
    _set_csrf_cookie(response, cookie)
    return response


@router.post("/players/identity/{operation}", summary="Execute confirmed identity change")
async def execute_identity(
    operation: str,
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    operation_map = {"merge": "merge_players", "split": "split_alias", "inverse": "inverse"}
    try:
        kind = cast(IdentityOperationKind, operation_map[operation])
        values = await _identity_form(request)
        draft = _draft(kind, values)
        command = ExecuteIdentityChangeDTO(
            draft=draft,
            expected_before_snapshot_digest=_one(values, "expected_before_snapshot_digest"),
            operator_label=_one(values, "operator_label"),
            reason=_one(values, "reason"),
        )
    except (KeyError, ValueError, ValidationError):
        return problem_response(
            422,
            title="Invalid identity execution",
            code="invalid_identity_execution",
            detail="Identity execution is invalid",
        )
    receipt = _identity_port(port).execute(command)
    location = _invalidation_location(
        _affected_player(draft),
        receipt.operation.operation_public_id,
        receipt.invalidation_jobs,
    )
    return RedirectResponse(location, status_code=303)


@router.post(
    "/players/{player_public_id}/identity/invalidation/{operation_public_id}/retry",
    summary="Ensure identity invalidation",
)
async def retry_identity_invalidation(
    player_public_id: str,
    operation_public_id: str,
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    try:
        player_id = _public_id(player_public_id)
        operation_id = _public_id(operation_public_id)
        await _identity_form(request)
    except (ValueError, ValidationError):
        return problem_response(
            422,
            title="Invalid identity invalidation retry",
            code="invalid_identity_invalidation_retry",
            detail="Identity invalidation retry is invalid",
        )
    jobs = _identity_port(port).retry_invalidation(operation_id)
    return RedirectResponse(_invalidation_location(player_id, operation_id, jobs), status_code=303)
