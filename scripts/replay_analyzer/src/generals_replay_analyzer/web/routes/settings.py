"""Safe scalar settings commands and explicit injected diagnostics."""

from __future__ import annotations

import re
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from starlette.responses import Response

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import PublicProblem, problem_response
from generals_replay_analyzer.web.ports import (
    ApplySettingsCommandDTO,
    DiagnosticCommandDTO,
    DiagnosticKind,
    DiagnosticsCommandPort,
    EditableSettingKey,
    SettingChangeDTO,
    SettingsCommandPort,
    SettingsPreviewCommandDTO,
    SettingsQueryPort,
    WebApplicationPort,
)
from generals_replay_analyzer.web.presentation.shell import accepts_html, template_response
from generals_replay_analyzer.web.viewmodels.settings import settings_shell, settings_view

router = APIRouter(tags=["settings"])
_INTEGER = re.compile(r"[0-9]+", re.ASCII)
_DIAGNOSTIC_KINDS = frozenset(
    {
        "data_root_writable",
        "sqlite_integrity",
        "engine_launch_version",
        "ollama_model_available",
        "ollama_minimal_generation",
    }
)


def _query_port(port: WebApplicationPort) -> SettingsQueryPort:
    if not isinstance(port, SettingsQueryPort):
        raise PublicProblem(status=503, code="settings_adapter_pending", detail="Analyzer settings are unavailable")
    return cast(SettingsQueryPort, port)


def _command_port(port: WebApplicationPort) -> SettingsCommandPort:
    if not isinstance(port, SettingsCommandPort):
        raise PublicProblem(status=503, code="settings_adapter_pending", detail="Analyzer settings are unavailable")
    return cast(SettingsCommandPort, port)


def _diagnostics_port(port: WebApplicationPort) -> DiagnosticsCommandPort:
    if not isinstance(port, DiagnosticsCommandPort):
        raise PublicProblem(status=503, code="diagnostics_adapter_pending", detail="Diagnostics are unavailable")
    return cast(DiagnosticsCommandPort, port)


def _issue_csrf(request: Request) -> tuple[str, str]:
    token = request.app.state.form_csrf_token_registry.issue()
    return token.hidden_value, token.cookie_value


def _set_csrf_cookie(response: Response, cookie: str) -> None:
    response.set_cookie("_csrf", cookie, max_age=600, httponly=True, samesite="strict", path="/")


async def _form(request: Request, allowed: frozenset[str]) -> dict[str, list[str]]:
    form = await request.form()
    if any(name not in allowed for name, _value in form.multi_items()):
        raise ValueError("unknown form field")
    values: dict[str, list[str]] = {}
    for name in allowed:
        if name not in form:
            continue
        items = form.getlist(name)
        if any(not isinstance(item, str) or len(item) > 512 for item in items):
            raise ValueError("invalid form value")
        values[name] = cast(list[str], items)
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


def _integer(value: str) -> int:
    if _INTEGER.fullmatch(value) is None:
        raise ValueError("integer")
    return int(value)


def _changes(values: dict[str, list[str]]) -> tuple[SettingChangeDTO, ...]:
    keys = values.get("setting_key", [])
    candidates = values.get("setting_value", [])
    if not keys or len(keys) != len(candidates) or len(keys) > 5:
        raise ValueError("changes")
    changes = []
    for raw_key, raw_value in zip(keys, candidates, strict=True):
        key = cast(EditableSettingKey, raw_key)
        value: int | str = _integer(raw_value) if raw_key in {
            "movement_sample_frames",
            "minimum_longitudinal_sample_size",
        } else raw_value
        changes.append(SettingChangeDTO(key=key, value=value))
    return tuple(changes)


def _html_or_problem(request: Request) -> Response | None:
    if accepts_html(request.headers.get("accept")):
        return None
    return problem_response(
        406,
        title="Not Acceptable",
        code="not_acceptable",
        detail="This route provides only text/html",
    )


@router.get("/settings", summary="Analyzer settings")
# TheSuperHackers @feature Leex 23/08/2026 Render immutable configuration identity without active diagnostic side effects. (#TBD)
def settings_page(
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    negotiation = _html_or_problem(request)
    if negotiation is not None:
        return negotiation
    snapshot = _query_port(port).get_settings()
    hidden, cookie = _issue_csrf(request)
    response = template_response(
        request,
        "settings/index.html",
        settings_shell(snapshot),
        context={"settings": settings_view(snapshot), "csrf_token": hidden, "mutation": None},
    )
    _set_csrf_cookie(response, cookie)
    return response


@router.post("/settings/preview", summary="Preview analyzer settings")
async def preview_settings(
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    negotiation = _html_or_problem(request)
    if negotiation is not None:
        return negotiation
    try:
        values = await _form(
            request,
            frozenset({"_csrf", "expected_revision", "setting_key", "setting_value"}),
        )
        command = SettingsPreviewCommandDTO(
            expected_revision=_integer(_one(values, "expected_revision")),
            changes=_changes(values),
        )
    except (ValueError, ValidationError):
        return problem_response(
            422,
            title="Invalid settings preview",
            code="invalid_settings_preview",
            detail="Settings preview is invalid",
        )
    impact = _query_port(port).preview_settings(command)
    hidden, cookie = _issue_csrf(request)
    response = template_response(
        request,
        "settings/_impact.html",
        settings_shell(_query_port(port).get_settings()),
        context={"impact": impact, "csrf_token": hidden},
    )
    _set_csrf_cookie(response, cookie)
    return response


@router.post("/settings/apply", summary="Apply confirmed analyzer settings")
async def apply_settings(
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    negotiation = _html_or_problem(request)
    if negotiation is not None:
        return negotiation
    try:
        values = await _form(
            request,
            frozenset(
                {
                    "_csrf",
                    "expected_revision",
                    "setting_key",
                    "setting_value",
                    "expected_impact_digest",
                    "confirm_invalidating_change",
                }
            ),
        )
        if _one(values, "confirm_invalidating_change") != "true":
            raise ValueError("confirmation")
        command = ApplySettingsCommandDTO(
            expected_revision=_integer(_one(values, "expected_revision")),
            changes=_changes(values),
            expected_impact_digest=_one(values, "expected_impact_digest"),
            confirm_invalidating_change=True,
        )
    except (ValueError, ValidationError):
        return problem_response(
            422,
            title="Invalid settings change",
            code="invalid_settings_change",
            detail="Settings change is invalid",
        )
    mutation = _command_port(port).apply_settings(command)
    hidden, cookie = _issue_csrf(request)
    response = template_response(
        request,
        "settings/index.html",
        settings_shell(mutation.snapshot),
        context={"settings": settings_view(mutation.snapshot), "csrf_token": hidden, "mutation": mutation},
    )
    _set_csrf_cookie(response, cookie)
    return response


@router.post("/settings/diagnostics/{kind}", summary="Run explicit analyzer diagnostic")
async def run_diagnostic(
    kind: str,
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    negotiation = _html_or_problem(request)
    if negotiation is not None:
        return negotiation
    if kind not in _DIAGNOSTIC_KINDS:
        return problem_response(
            422,
            title="Invalid diagnostic",
            code="invalid_diagnostic_kind",
            detail="Diagnostic action is invalid",
        )
    try:
        values = await _form(
            request,
            frozenset({"_csrf", "expected_settings_revision"}),
        )
        command = DiagnosticCommandDTO(
            kind=cast(DiagnosticKind, kind),
            expected_settings_revision=_integer(_one(values, "expected_settings_revision")),
        )
    except (ValueError, ValidationError):
        return problem_response(
            422,
            title="Invalid diagnostic",
            code="invalid_diagnostic_command",
            detail="Diagnostic action is invalid",
        )
    result = _diagnostics_port(port).run_diagnostic(command)
    snapshot = _query_port(port).get_settings()
    return template_response(
        request,
        "settings/_diagnostic.html",
        settings_shell(snapshot),
        context={"result": result},
    )
