"""Five-mode immutable replay comparison routes."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ValidationError
from starlette.responses import RedirectResponse, Response

from generals_replay_analyzer.web.dependencies import application_port
from generals_replay_analyzer.web.errors import PublicProblem, problem_response
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    ComparisonDTO,
    ComparisonFiltersDTO,
    ComparisonKind,
    ComparisonQueryPort,
    ComparisonResolutionDTO,
    ComparisonSelectionDTO,
    FixedComparisonQueryDTO,
    WebApplicationPort,
)
from generals_replay_analyzer.web.presentation.shell import (
    ShellContextDTO,
    accepts_html,
    feature_shell,
    template_response,
)
from generals_replay_analyzer.web.viewmodels.comparisons import comparison_mode_options, comparison_view

router = APIRouter(tags=["comparisons"])
_SELECTION_FIELDS = frozenset(
    {
        "kind",
        "left_public_id",
        "right_public_id",
        "baseline_requested",
        "metric_definition_id",
        "faction",
        "subfaction",
        "opponent_faction",
        "opponent_player_public_id",
        "map_public_id",
        "start_position",
        "patch",
        "date_from_utc",
        "date_to_utc",
        "quality_policy_digest",
    }
)
_FIXED_FIELD = re.compile(
    r"(?:schema_version|kind|minimum_sample_size|metric_definition_id|left_[a-z0-9_]+|right_[a-z0-9_]+|definition_[0-9]+_[a-z0-9_]+)\Z"
)


def _port(value: WebApplicationPort) -> ComparisonQueryPort | None:
    return cast(ComparisonQueryPort, value) if isinstance(value, ComparisonQueryPort) else None


def _shell(availability: AvailabilityDTO) -> ShellContextDTO:
    return feature_shell(
        page_title="Compare replay evidence | Generals Replay Analyzer",
        current_path="/compare",
        availability=availability,
    )


def _selector_context(resolution: ComparisonResolutionDTO | None, kind: ComparisonKind) -> dict[str, object]:
    reasons = {} if resolution is None else {kind: ", ".join(resolution.reason_codes)}
    return {
        "modes": comparison_mode_options(),
        "selected_kind": kind,
        "resolution": resolution,
        "mode_reasons": reasons,
    }


def _selection(request: Request) -> ComparisonSelectionDTO | None:
    if set(request.query_params).difference(_SELECTION_FIELDS):
        raise ValueError("invalid comparison selection")
    if any(
        len(request.query_params.getlist(name)) != 1 for name in request.query_params if name != "metric_definition_id"
    ):
        raise ValueError("invalid comparison selection")
    kind = cast(ComparisonKind, request.query_params.get("kind", "players"))
    left = request.query_params.get("left_public_id") or None
    right = request.query_params.get("right_public_id") or None
    baseline = request.query_params.get("baseline_requested", "false").casefold() == "true"
    if left is None or right is None and not baseline:
        return None
    filters = ComparisonFiltersDTO.model_validate(
        {
            name: request.query_params[name]
            for name in (
                "faction",
                "subfaction",
                "opponent_faction",
                "opponent_player_public_id",
                "map_public_id",
                "start_position",
                "patch",
                "date_from_utc",
                "date_to_utc",
                "quality_policy_digest",
            )
            if name in request.query_params
        }
    )
    return ComparisonSelectionDTO(
        kind=kind,
        left_public_id=left,
        right_public_id=right,
        baseline_requested=baseline,
        metric_definition_ids=tuple(sorted(set(request.query_params.getlist("metric_definition_id")))),
        filters=filters,
    )


def _longitudinal_pairs(prefix: str, binding: BaseModel) -> list[tuple[str, object]]:
    values = binding.model_dump(mode="json")
    pairs = [
        (f"{prefix}_longitudinal_{name}", value)
        for name, value in values.items()
        if name != "statistics_algorithm_versions"
    ]
    pairs.extend((f"{prefix}_statistics_algorithm_version", value) for value in values["statistics_algorithm_versions"])
    return pairs


def _subject_pairs(prefix: str, subject: BaseModel) -> list[tuple[str, object]]:
    values = subject.model_dump(mode="json")
    pairs: list[tuple[str, object]] = [(f"{prefix}_subject_kind", values["subject_kind"])]
    for name, value in values.items():
        if name in {"subject_kind", "longitudinal", "report", "feature_set_public_ids"}:
            continue
        pairs.append((f"{prefix}_{name}", value))
    if hasattr(subject, "longitudinal"):
        pairs.extend(_longitudinal_pairs(prefix, subject.longitudinal))
    if "report" in values:
        pairs.extend((f"{prefix}_report_{name}", value) for name, value in values["report"].items())
    pairs.extend((f"{prefix}_feature_set_public_id", value) for value in values.get("feature_set_public_ids", []))
    return pairs


def _fixed_url(query: FixedComparisonQueryDTO, *, api: bool = False) -> str:
    pairs: list[tuple[str, object]] = [
        ("schema_version", query.schema_version),
        ("kind", query.kind),
        ("minimum_sample_size", query.minimum_sample_size),
    ]
    pairs.extend(("metric_definition_id", value) for value in query.metric_definition_ids)
    pairs.extend(_subject_pairs("left", query.left))
    pairs.extend(_subject_pairs("right", query.right))
    for index, binding in enumerate(query.definition_bindings):
        pairs.extend(
            (f"definition_{index}_{name}", value)
            for name, value in binding.model_dump(mode="json", exclude_none=True).items()
        )
    return ("/api/comparisons?" if api else "/compare/result?") + urlencode(pairs)


def _longitudinal(prefix: str, request: Request) -> dict[str, object]:
    field = f"{prefix}_longitudinal_"
    names = (
        "run_id",
        "player_public_id",
        "identity_revision",
        "analyzer_name",
        "analyzer_version",
        "segment_schema_version",
        "segment_digest",
        "quality_policy_digest",
        "input_digest",
        "cache_key",
    )
    values: dict[str, object] = {name: request.query_params[f"{field}{name}"] for name in names}
    values["statistics_algorithm_versions"] = tuple(
        request.query_params.getlist(f"{prefix}_statistics_algorithm_version")
    )
    return values


def _subject(prefix: str, request: Request) -> dict[str, object]:
    kind = request.query_params[f"{prefix}_subject_kind"]
    value: dict[str, object] = {"subject_kind": kind}
    if kind == "player_cohort":
        value.update(
            player_public_id=request.query_params[f"{prefix}_player_public_id"],
            identity_revision=request.query_params[f"{prefix}_identity_revision"],
            longitudinal=_longitudinal(prefix, request),
        )
    elif kind == "segment_baseline":
        value.update(
            baseline_public_id=request.query_params[f"{prefix}_baseline_public_id"],
            population_definition_version=request.query_params[f"{prefix}_population_definition_version"],
            longitudinal=_longitudinal(prefix, request),
        )
    elif kind == "match":
        report_names = (
            "replay_public_id",
            "replay_player_public_id",
            "report_public_id",
            "document_schema_version",
            "report_version",
            "display_policy_version",
            "input_digest",
        )
        value.update(
            report={name: request.query_params[f"{prefix}_report_{name}"] for name in report_names},
            feature_set_public_ids=tuple(request.query_params.getlist(f"{prefix}_feature_set_public_id")),
        )
    elif kind == "opening":
        value.update(
            player_public_id=request.query_params[f"{prefix}_player_public_id"],
            identity_revision=request.query_params[f"{prefix}_identity_revision"],
            longitudinal=_longitudinal(prefix, request),
            result_public_id=request.query_params[f"{prefix}_result_public_id"],
            opening_definition_id=request.query_params[f"{prefix}_opening_definition_id"],
            opening_definition_version=request.query_params[f"{prefix}_opening_definition_version"],
        )
    elif kind == "strategy":
        value.update(
            player_public_id=request.query_params[f"{prefix}_player_public_id"],
            identity_revision=request.query_params[f"{prefix}_identity_revision"],
            longitudinal=_longitudinal(prefix, request),
            result_public_id=request.query_params[f"{prefix}_result_public_id"],
            strategy_id=request.query_params[f"{prefix}_strategy_id"],
            taxonomy_version=request.query_params[f"{prefix}_taxonomy_version"],
            rule_version=request.query_params[f"{prefix}_rule_version"],
            method=request.query_params[f"{prefix}_method"],
        )
    elif kind == "time_period":
        value.update(
            player_public_id=request.query_params[f"{prefix}_player_public_id"],
            identity_revision=request.query_params[f"{prefix}_identity_revision"],
            start_inclusive_utc=request.query_params[f"{prefix}_start_inclusive_utc"],
            end_exclusive_utc=request.query_params[f"{prefix}_end_exclusive_utc"],
            longitudinal=_longitudinal(prefix, request),
        )
    else:
        raise ValueError("invalid comparison subject")
    return value


def _definitions(request: Request) -> tuple[dict[str, str], ...]:
    indices = sorted(
        {
            int(match.group(1))
            for name in request.query_params
            if (match := re.fullmatch(r"definition_([0-9]+)_definition_id", name))
        }
    )
    result = []
    for index in indices:
        prefix = f"definition_{index}_"
        names = (
            "definition_kind",
            "definition_id",
            "definition_version",
            "scope_type",
            "window_policy_version",
            "faction_comparability",
        )
        value = {name: request.query_params[prefix + name] for name in names}
        for optional in ("unit", "taxonomy_version"):
            if prefix + optional in request.query_params:
                value[optional] = request.query_params[prefix + optional]
        result.append(value)
    return tuple(result)


def _fixed_query(request: Request) -> FixedComparisonQueryDTO:
    if any(_FIXED_FIELD.fullmatch(name) is None for name in request.query_params):
        raise ValueError("invalid fixed comparison field")
    repeated = {
        "metric_definition_id",
        "left_statistics_algorithm_version",
        "right_statistics_algorithm_version",
        "left_feature_set_public_id",
        "right_feature_set_public_id",
    }
    if any(len(request.query_params.getlist(name)) != 1 for name in request.query_params if name not in repeated):
        raise ValueError("invalid fixed comparison multiplicity")
    return FixedComparisonQueryDTO.model_validate(
        {
            "schema_version": request.query_params["schema_version"],
            "kind": request.query_params["kind"],
            "left": _subject("left", request),
            "right": _subject("right", request),
            "metric_definition_ids": tuple(request.query_params.getlist("metric_definition_id")),
            "definition_bindings": _definitions(request),
            "minimum_sample_size": request.query_params["minimum_sample_size"],
        }
    )


def _canonical_json(value: BaseModel) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


# TheSuperHackers @feature Leex 23/08/2026 Keep all five comparison modes visible and bind results to exact service-owned inputs. (#TBD)
@router.get("/compare", summary="Comparison selector")
def compare_selector(
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    if not accepts_html(request.headers.get("accept")):
        return problem_response(
            406, title="Not Acceptable", code="not_acceptable", detail="This route provides only text/html"
        )
    try:
        kind = cast(ComparisonKind, request.query_params.get("kind", "players"))
        selection = _selection(request)
    except (ValueError, ValidationError):
        return problem_response(
            422,
            title="Invalid comparison selection",
            code="invalid_comparison_selection",
            detail="Comparison selection is invalid",
        )
    if selection is None:
        availability = AvailabilityDTO(state="unavailable", reason_codes=("comparison_selection_incomplete",))
        return template_response(
            request, "compare/index.html", _shell(availability), context=_selector_context(None, kind)
        )
    comparison_port = _port(port)
    resolution = (
        comparison_port.resolve(selection)
        if comparison_port is not None
        else ComparisonResolutionDTO(state="unavailable", reason_codes=("comparison_adapter_pending",))
    )
    if resolution.state == "resolved":
        assert resolution.fixed_query is not None
        if resolution.fixed_query.kind != selection.kind:
            raise PublicProblem(
                status=409, code="comparison_scope_mismatch", detail="Comparison resolution is inconsistent"
            )
        return RedirectResponse(_fixed_url(resolution.fixed_query), status_code=303)
    availability = AvailabilityDTO(state="unavailable", reason_codes=resolution.reason_codes)
    return template_response(
        request, "compare/index.html", _shell(availability), context=_selector_context(resolution, kind)
    )


def _comparison(request: Request, port: WebApplicationPort) -> tuple[ComparisonDTO, str]:
    query = _fixed_query(request)
    comparison_port = _port(port)
    if comparison_port is None:
        raise PublicProblem(status=503, code="comparison_adapter_pending", detail="Fixed comparisons are unavailable")
    result = comparison_port.compare(query)
    if result.query != query:
        raise PublicProblem(status=409, code="comparison_scope_mismatch", detail="Comparison result is inconsistent")
    return result, _fixed_url(query, api=True)


@router.get("/compare/result", summary="Fixed comparison result")
def compare_result(
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    if not accepts_html(request.headers.get("accept")):
        return problem_response(
            406, title="Not Acceptable", code="not_acceptable", detail="This route provides only text/html"
        )
    try:
        result, json_url = _comparison(request, port)
    except (KeyError, ValueError, ValidationError):
        return problem_response(
            422, title="Invalid fixed comparison", code="invalid_fixed_comparison", detail="Fixed comparison is invalid"
        )
    return template_response(
        request,
        "compare/_result.html",
        _shell(result.availability),
        context={"view": comparison_view(result, json_url)},
    )


@router.get("/api/comparisons", summary="Fixed comparison data")
def comparison_json(
    request: Request,
    port: Annotated[WebApplicationPort, Depends(application_port, scope="function")],
) -> Response:
    try:
        result, _json_url = _comparison(request, port)
    except (KeyError, ValueError, ValidationError):
        return problem_response(
            422, title="Invalid fixed comparison", code="invalid_fixed_comparison", detail="Fixed comparison is invalid"
        )
    body = _canonical_json(result)
    etag = hashlib.sha256(body).hexdigest()
    if request.headers.get("if-none-match", "").strip('"') == etag:
        return Response(status_code=304, headers={"etag": f'"{etag}"'})
    return Response(body, media_type="application/json", headers={"etag": f'"{etag}"'})
