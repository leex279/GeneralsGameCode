"""Immutable, view-safe values accepted by replay-analyzer routes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Annotated, Literal, Protocol, Self
from urllib.parse import unquote, urlsplit
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.functional_validators import AfterValidator


def _lowercase_uuid(value: str) -> str:
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("public ID must be a lowercase hyphenated UUID")
    return value


PublicId = Annotated[str, AfterValidator(_lowercase_uuid)]
MAX_DIAGNOSTIC_MESSAGE_LENGTH = 2048
_LOCATOR_BOUNDARIES = frozenset("'\"([{<:=,;")
_CLOSING_PUNCTUATION = frozenset("'\")]}>,;!?")
_URI_TERMINATORS = frozenset("'\"(){}<>,;")
_SAFE_DIAGNOSTIC_URI_SCHEMES = frozenset({"http", "https"})
_SECRET_MARKERS = ("api_key", "apikey", "credential", "password", "private_key", "secret", "token")
_LOCAL_FILE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_MAX_PERCENT_DECODE_PASSES = 3
DiagnosticCandidate = tuple[Literal["uri", "locator", "traversal"], int, int]


def _is_candidate_boundary(value: str, index: int) -> bool:
    return index == 0 or value[index - 1].isspace() or value[index - 1] in _LOCATOR_BOUNDARIES


def _uri_candidate_end(value: str, start: int) -> int | None:
    if not value[start].isalpha():
        return None
    scheme_end = start + 1
    while scheme_end < len(value) and (value[scheme_end].isalnum() or value[scheme_end] in "+-."):
        scheme_end += 1
    if value[scheme_end : scheme_end + 3] != "://":
        return None
    end = scheme_end + 3
    while end < len(value) and not value[end].isspace() and value[end] not in _URI_TERMINATORS:
        end += 1
    while end > scheme_end + 3 and value[end - 1] in ".!?":
        end -= 1
    return end


def _is_traversal_at(value: str, index: int) -> bool:
    if value[index : index + 2] != "..":
        return False
    before = value[index - 1] if index > 0 else ""
    after = value[index + 2] if index + 2 < len(value) else ""
    boundary_before = index == 0 or before.isspace() or before in _LOCATOR_BOUNDARIES or before in "\\/"
    boundary_after = not after or after in _CLOSING_PUNCTUATION or after in "\\/"
    return boundary_before and boundary_after and (before in "\\/" or after in "\\/")


def _locator_candidate_end(value: str, start: int) -> int | None:
    if not _is_candidate_boundary(value, start):
        return None
    remaining_length = len(value) - start
    if (
        remaining_length >= 3
        and value[start].isalpha()
        and value[start + 1] == ":"
        and value[start + 2] in "\\/"
    ):
        return start + 3
    if value.startswith(("//", "\\\\"), start):
        if remaining_length <= 2:
            return None
        if value[start + 2] in "?.":
            return start + 4 if remaining_length > 3 and value[start + 3] in "\\/" else None
        return (
            start + 2
            if not value[start + 2].isspace() and value[start + 2] not in _CLOSING_PUNCTUATION
            else None
        )
    if value[start] == "/":
        return (
            start + 1
            if remaining_length > 1
            and not value[start + 1].isspace()
            and value[start + 1] not in _CLOSING_PUNCTUATION
            else None
        )
    return None


def iter_diagnostic_candidates(value: str) -> Iterator[DiagnosticCandidate]:
    index = 0
    while index < len(value):
        if _is_candidate_boundary(value, index):
            uri_end = _uri_candidate_end(value, index)
            if uri_end is not None:
                yield ("uri", index, uri_end)
                index = uri_end
                continue
        if _is_traversal_at(value, index):
            yield ("traversal", index, index + 2)
            index += 2
            continue
        locator_end = _locator_candidate_end(value, index)
        if locator_end is not None:
            yield ("locator", index, locator_end)
            index = locator_end
            continue
        index += 1


def _has_valid_percent_encoding(value: str) -> bool:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value) or value[index + 1] not in _HEX_DIGITS or value[index + 2] not in _HEX_DIGITS:
            return False
        index += 3
    return True


def _has_malformed_decoded_escape(value: str) -> bool:
    for index, character in enumerate(value):
        if character != "%" or index + 2 >= len(value):
            continue
        first, second = value[index + 1], value[index + 2]
        if first in _HEX_DIGITS and second in _HEX_DIGITS:
            continue
        if first in _HEX_DIGITS or (first.isalnum() and second.isalnum()):
            return True
    return False


def _bounded_unquote(value: str) -> str | None:
    if not _has_valid_percent_encoding(value):
        return None
    current = value
    for _pass in range(_MAX_PERCENT_DECODE_PASSES):
        try:
            decoded = unquote(current, errors="strict")
        except UnicodeDecodeError:
            return None
        if decoded == current:
            return None if _has_malformed_decoded_escape(current) else current
        current = decoded
    try:
        return (
            current
            if unquote(current, errors="strict") == current and not _has_malformed_decoded_escape(current)
            else None
        )
    except UnicodeDecodeError:
        return None


def _contains_local_locator(value: str) -> bool:
    return any(True for _candidate in iter_diagnostic_candidates(value))


def _is_unsafe_uri_component(value: str) -> bool:
    decoded = _bounded_unquote(value)
    if decoded is None:
        return True
    normalized = decoded.casefold().replace("-", "_")
    return (
        "@" in decoded
        or _contains_local_locator(decoded)
        or any(marker in normalized for marker in _SECRET_MARKERS)
    )


def _uri_data_fields(component: str) -> tuple[str, ...]:
    fields: list[str] = []
    for token in component.split("&"):
        key, separator, value = token.partition("=")
        fields.append(key)
        if separator:
            fields.append(value)
    return tuple(fields)


def _is_unsafe_uri_route_path(value: str) -> bool:
    decoded = _bounded_unquote(value)
    if decoded is None:
        return True
    normalized = decoded.replace("\\", "/")
    path_without_root = normalized.lstrip("/")
    has_drive = (
        len(path_without_root) >= 3
        and path_without_root[0].isalpha()
        and path_without_root[1] == ":"
        and path_without_root[2] == "/"
    )
    return bool(
        "\\" in decoded
        or normalized.startswith("//")
        or has_drive
        or any(candidate[0] == "traversal" for candidate in iter_diagnostic_candidates(normalized))
        or normalized.casefold().endswith(_LOCAL_FILE_SUFFIXES)
    )


def _is_safe_diagnostic_uri(candidate: str) -> bool:
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return False
    data_components = (
        parsed.hostname or "",
        *_uri_data_fields(parsed.query),
        *_uri_data_fields(parsed.fragment),
    )
    return bool(
        parsed.scheme.lower() in _SAFE_DIAGNOSTIC_URI_SCHEMES
        and parsed.netloc
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not _is_unsafe_uri_route_path(parsed.path)
        and not any(_is_unsafe_uri_component(component) for component in data_components)
        and (port is None or 0 < port <= 65535)
    )


def _contains_filesystem_locator(value: str) -> bool:
    for kind, start, end in iter_diagnostic_candidates(value):
        if kind == "uri":
            if not _is_safe_diagnostic_uri(value[start:end]):
                return True
            continue
        return True
    return False


def _string_values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, BaseModel):
        return tuple(text for field in type(value).model_fields for text in _string_values(getattr(value, field)))
    if isinstance(value, tuple):
        return tuple(text for item in value for text in _string_values(item))
    return ()


class WebDTO(BaseModel):
    """Common immutable contract that rejects accidental persistence fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="after")
    def _reject_absolute_paths(self) -> Self:
        if any(_contains_filesystem_locator(value) for value in _string_values(self)):
            raise ValueError("public DTOs cannot contain absolute filesystem paths")
        return self


class DiagnosticDTO(WebDTO):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=MAX_DIAGNOSTIC_MESSAGE_LENGTH)


class ReadinessDTO(WebDTO):
    ready: bool
    schema_revision: str | None = None
    diagnostics: tuple[DiagnosticDTO, ...] = ()


class AvailabilityDTO(WebDTO):
    state: Literal["available", "partial", "unavailable"]
    reason_codes: tuple[str, ...] = ()
    evidence_references: tuple[str, ...] = ()


class PipelineStateDTO(WebDTO):
    stage: str = Field(min_length=1)
    state: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    progress: float | None = Field(default=None, ge=0, le=1)
    job_public_id: PublicId | None = None
    replay_public_id: PublicId | None = None


class QualityIssueDTO(WebDTO):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    evidence_references: tuple[str, ...] = ()


class TerminalQualityDTO(WebDTO):
    lifecycle: str = Field(min_length=1)
    issues: tuple[QualityIssueDTO, ...] = ()
    engine_run_status: str | None = None
    strategy_analysis_scope: str | None = None


class TimestampedWebDTO(WebDTO):
    generated_at: AwareDatetime

    @field_validator("generated_at")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("generated_at must use UTC")
        return value


class DashboardDTO(TimestampedWebDTO):
    availability: AvailabilityDTO
    pipeline_states: tuple[PipelineStateDTO, ...] = ()


class IdentityLandingDTO(TimestampedWebDTO):
    availability: AvailabilityDTO


# TheSuperHackers @feature Leex 22/08/2026 Keep web routes isolated from ORM and mutable analytics state. (#TBD)
class WebApplicationPort(Protocol):
    def readiness(self) -> ReadinessDTO: ...

    def dashboard(self) -> DashboardDTO: ...

    def identity_landing(self) -> IdentityLandingDTO: ...
