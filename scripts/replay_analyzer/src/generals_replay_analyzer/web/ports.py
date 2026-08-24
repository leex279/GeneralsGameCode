"""Immutable, view-safe values accepted by replay-analyzer routes."""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Annotated, Literal, Protocol, Self, runtime_checkable
from urllib.parse import unquote, urlsplit
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pydantic.functional_validators import AfterValidator

from generals_replay_analyzer.configuration import (
    SettingsStoreError,
    normalize_ollama_endpoint,
    validate_ollama_model_name,
)
from generals_replay_analyzer.ingress_contract import validate_replay_relative_name, validate_root_public_id
from generals_replay_analyzer.report.model import freeze_report_value


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
    if remaining_length >= 3 and value[start].isalpha() and value[start + 1] == ":" and value[start + 2] in "\\/":
        return start + 3
    if value.startswith(("//", "\\\\"), start):
        if remaining_length <= 2:
            return None
        if value[start + 2] in "?.":
            return start + 4 if remaining_length > 3 and value[start + 3] in "\\/" else None
        return start + 2 if not value[start + 2].isspace() and value[start + 2] not in _CLOSING_PUNCTUATION else None
    if value[start] == "/":
        return (
            start + 1
            if remaining_length > 1 and not value[start + 1].isspace() and value[start + 1] not in _CLOSING_PUNCTUATION
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
    return "@" in decoded or _contains_local_locator(decoded) or any(marker in normalized for marker in _SECRET_MARKERS)


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
    frame_end: int | None = Field(default=None, ge=0)
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


class DashboardReplayDTO(WebDTO):
    """One adapter-supplied recent replay; never a template-derived match claim."""

    replay_public_id: PublicId
    report_public_id: PublicId | None = None
    label: str = Field(min_length=1, max_length=256)
    players: tuple[str, ...] = Field(min_length=1, max_length=16)
    result: str | None = Field(default=None, min_length=1, max_length=64)
    map_name: str | None = Field(default=None, min_length=1, max_length=256)
    analysis_state: str = Field(min_length=1, max_length=64)
    evidence_tier: Literal["observed", "derived", "inferred"] | None = None
    observed_at: AwareDatetime | None = None

    @field_validator("players")
    @classmethod
    def _validate_player_labels(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value) > 256 for value in values):
            raise ValueError("dashboard player labels must be nonempty and bounded")
        return values

    @field_validator("observed_at")
    @classmethod
    def _require_utc_observed_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() != timedelta(0):
            raise ValueError("dashboard observed time must use UTC")
        return value


class DashboardTrendDTO(WebDTO):
    """Text-first trend summary supplied by the accepted application adapter."""

    label: str = Field(min_length=1, max_length=128)
    period_label: str = Field(min_length=1, max_length=128)
    sample_count: int = Field(ge=0)
    summary: str = Field(min_length=1, max_length=512)
    availability: AvailabilityDTO
    filter_analysis_status: Literal["partial", "desynced", "failed"] | None = None


class DashboardNoticeDTO(WebDTO):
    """Deterministic review-queue entry with no template-side qualification."""

    code: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=512)
    replay_public_id: PublicId | None = None


# TheSuperHackers @feature Leex 23/08/2026 Keep command-center rows adapter-supplied and path-free. (#0)
class DashboardDTO(TimestampedWebDTO):
    availability: AvailabilityDTO
    pipeline_states: tuple[PipelineStateDTO, ...] = ()
    recent_replays: tuple[DashboardReplayDTO, ...] = Field(default=(), max_length=8)
    trends: tuple[DashboardTrendDTO, ...] = Field(default=(), max_length=2)
    notable_evidence: tuple[DashboardNoticeDTO, ...] = Field(default=(), max_length=8)


class IdentityLandingDTO(TimestampedWebDTO):
    availability: AvailabilityDTO


# TheSuperHackers @feature Leex 22/08/2026 Preserve replay presentation, provenance, and analysis filters as immutable public DTOs. (#0)
class ReplayPlayerDisplayDTO(WebDTO):
    """A replay-local player presentation value, not a canonical identity record."""

    replay_player_public_id: PublicId | None = None
    report_public_id: PublicId | None = None
    display_name: str = Field(min_length=1, max_length=256)
    slot: int = Field(ge=1, le=16)
    faction: str | None = Field(default=None, min_length=1, max_length=64)
    result: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _validate_report_link(self) -> Self:
        if (self.replay_player_public_id is None) != (self.report_public_id is None):
            raise ValueError("player report navigation requires both public identities")
        return self


class ReplayProvenanceDTO(WebDTO):
    """Explicitly labelled public provenance that cannot become player identity."""

    source_public_id: PublicId | None = None
    source_kind: str | None = Field(default=None, min_length=1, max_length=64)
    strata_match_token: str | None = Field(default=None, min_length=1, max_length=128)
    strata_user_token: str | None = Field(default=None, min_length=1, max_length=128)
    availability: AvailabilityDTO
    evidence_tier: Literal["observed", "derived", "inferred"] | None = None
    evidence_public_id: PublicId | None = None


class ReplayLibraryQueryDTO(WebDTO):
    """Frozen, deterministic filter state accepted by the replay-library port."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)
    sort: Literal["observed_desc", "observed_asc", "replay_asc", "status_asc"] = "observed_desc"
    search: str | None = Field(default=None, max_length=256)
    player_public_id: PublicId | None = None
    faction: str | None = Field(default=None, min_length=1, max_length=64)
    matchup: str | None = Field(default=None, min_length=1, max_length=128)
    map_public_id: PublicId | None = None
    result: str | None = Field(default=None, min_length=1, max_length=64)
    patch: str | None = Field(default=None, min_length=1, max_length=64)
    strategy_id: str | None = Field(default=None, min_length=1, max_length=128)
    analysis_status: (
        Literal["discovered", "parsed", "engine_verified", "partial", "desynced", "unsupported", "failed"] | None
    ) = None
    evidence_tier: Literal["observed", "derived", "inferred"] | None = None
    lifecycle_state: str | None = Field(default=None, min_length=1, max_length=64)
    source_kind: str | None = Field(default=None, min_length=1, max_length=64)
    date_from_utc: AwareDatetime | None = None
    date_to_utc: AwareDatetime | None = None

    @field_validator(
        "search",
        "faction",
        "matchup",
        "result",
        "patch",
        "strategy_id",
        "lifecycle_state",
        "source_kind",
    )
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("date_from_utc", "date_to_utc")
    @classmethod
    def _require_utc_filter_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() != timedelta(0):
            raise ValueError("library filter timestamps must use UTC")
        return value

    @model_validator(mode="after")
    def _require_ordered_dates(self) -> Self:
        if self.date_from_utc is not None and self.date_to_utc is not None and self.date_from_utc > self.date_to_utc:
            raise ValueError("date_from_utc must not follow date_to_utc")
        return self


class ReplayLibraryItemDTO(WebDTO):
    replay_public_id: PublicId
    report_public_id: PublicId | None = None
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    display_filename: str | None = Field(default=None, min_length=1, max_length=256)
    players: tuple[ReplayPlayerDisplayDTO, ...]
    map_public_id: PublicId | None = None
    map_display_name: str | None = Field(default=None, min_length=1, max_length=256)
    patch: str | None = Field(default=None, min_length=1, max_length=64)
    result: str | None = Field(default=None, min_length=1, max_length=64)
    lifecycle_state: str = Field(min_length=1, max_length=64)
    pipeline: PipelineStateDTO | None = None
    terminal_quality: TerminalQualityDTO | None = None
    availability: AvailabilityDTO
    provenance: ReplayProvenanceDTO
    observed_at_utc: AwareDatetime | None = None

    @field_validator("observed_at_utc")
    @classmethod
    def _require_utc_observation_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() != timedelta(0):
            raise ValueError("observed_at_utc must use UTC")
        return value


class ReplayLibraryPageDTO(WebDTO):
    query: ReplayLibraryQueryDTO
    items: tuple[ReplayLibraryItemDTO, ...]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total_items: int = Field(ge=0)
    availability: AvailabilityDTO


class ImportRootDTO(WebDTO):
    """An allow-listed root disclosure with no locator or browser-facing path."""

    root_public_id: PublicId
    label: str = Field(min_length=1, max_length=256)
    availability: AvailabilityDTO
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)


class UploadImportCommandDTO(WebDTO):
    """Opaque ingress metadata; web routes never receive the uploaded bytes or locator."""

    ingress_public_id: PublicId
    original_filename: str = Field(min_length=1, max_length=256)
    byte_count: int = Field(ge=1, le=64 * 1024 * 1024)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


# TheSuperHackers @feature Leex 22/08/2026 Keep configured-root commands as opaque identifiers plus a closed portable replay-name contract. (#0)
class RootImportCommandDTO(WebDTO):
    root_public_id: PublicId
    relative_path: str = Field(min_length=1, max_length=1024)

    @field_validator("root_public_id")
    @classmethod
    def _require_ingress_root_public_id(cls, value: str) -> str:
        return validate_root_public_id(value)

    @field_validator("relative_path")
    @classmethod
    def _require_safe_relative_posix_name(cls, value: str) -> str:
        validate_replay_relative_name(value)
        return value


class ImportSubmissionDTO(WebDTO):
    submission_public_id: PublicId
    replay_public_id: PublicId | None = None
    duplicate_of_replay_public_id: PublicId | None = None
    pipeline: PipelineStateDTO | None = None
    availability: AvailabilityDTO
    problem_code: str | None = Field(default=None, min_length=1, max_length=128)


JobState = Literal["pending", "running", "succeeded", "failed", "cancelled"]


class JobProgressDTO(WebDTO):
    completed: int = Field(ge=0)
    total: int = Field(ge=1)
    unit: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _bounded_progress(self) -> Self:
        if self.completed > self.total:
            raise ValueError("completed progress cannot exceed total")
        return self


class JobErrorSummaryDTO(WebDTO):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=512)
    retryable: bool


class JobLogReferenceDTO(WebDTO):
    log_public_id: PublicId
    label: Literal["stdout", "stderr", "supervisor"]
    byte_count: int = Field(ge=0)
    created_at_utc: AwareDatetime

    @field_validator("created_at_utc")
    @classmethod
    def _require_utc_created_at(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("created_at_utc must use UTC")
        return value


class JobSummaryDTO(WebDTO):
    job_public_id: PublicId
    replay_public_id: PublicId | None = None
    stage: str = Field(min_length=1, max_length=64)
    component_version: str = Field(min_length=1, max_length=255)
    state: JobState
    revision: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    retryable: bool
    cancel_requested: bool
    created_at_utc: AwareDatetime
    started_at_utc: AwareDatetime | None = None
    completed_at_utc: AwareDatetime | None = None
    progress: JobProgressDTO | None = None
    error: JobErrorSummaryDTO | None = None

    @field_validator("created_at_utc", "started_at_utc", "completed_at_utc")
    @classmethod
    def _require_utc_job_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() != timedelta(0):
            raise ValueError("job timestamps must use UTC")
        return value

    @model_validator(mode="after")
    def _bounded_attempts(self) -> Self:
        if self.attempt_count > self.max_attempts:
            raise ValueError("attempt_count cannot exceed max_attempts")
        return self


class JobDetailDTO(WebDTO):
    summary: JobSummaryDTO
    dependency_job_public_ids: tuple[PublicId, ...] = ()
    logs: tuple[JobLogReferenceDTO, ...] = ()
    availability: AvailabilityDTO


class JobQueryDTO(WebDTO):
    states: tuple[JobState, ...] = ()
    replay_public_id: PublicId | None = None
    stage: str | None = Field(default=None, min_length=1, max_length=64)
    limit: int = Field(default=50, ge=1, le=200)
    after_job_public_id: PublicId | None = None


class WatchFolderStatusDTO(WebDTO):
    root_public_id: PublicId
    label: str = Field(min_length=1, max_length=256)
    state: Literal["idle", "scanning", "degraded", "disabled"]
    last_scan_at_utc: AwareDatetime | None = None
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("last_scan_at_utc")
    @classmethod
    def _require_utc_scan_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() != timedelta(0):
            raise ValueError("last_scan_at_utc must use UTC")
        return value


class JobPageDTO(WebDTO):
    query: JobQueryDTO
    items: tuple[JobSummaryDTO, ...]
    next_job_public_id: PublicId | None = None
    watched_folders: tuple[WatchFolderStatusDTO, ...] = ()
    poll_after_seconds: int | None = Field(default=None, ge=1, le=300)
    availability: AvailabilityDTO


class RetryJobCommandDTO(WebDTO):
    job_public_id: PublicId
    expected_revision: int = Field(ge=0)


class CancelJobCommandDTO(WebDTO):
    job_public_id: PublicId
    expected_revision: int = Field(ge=0)


class JobMutationDTO(WebDTO):
    detail: JobDetailDTO
    result_code: Literal["retried", "cancelled", "cancel_requested"]


class JobLogQueryDTO(WebDTO):
    job_public_id: PublicId
    log_public_id: PublicId
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=65_536, ge=4, le=65_536)


class JobLogChunkDTO(BaseModel):
    """Pre-redacted bounded text; markup-like output must remain renderable as escaped log content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["available", "rotated", "unavailable"]
    text: str = Field(max_length=65_536)
    next_offset: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _bounded_utf8(self) -> Self:
        if len(self.text.encode("utf-8")) > 65_536:
            raise ValueError("job log chunk exceeds the public byte bound")
        return self


# TheSuperHackers @feature Leex 22/08/2026 Keep web routes isolated from ORM and mutable analytics state. (#0)
class WebApplicationPort(Protocol):
    def readiness(self) -> ReadinessDTO: ...

    def dashboard(self) -> DashboardDTO: ...

    def identity_landing(self) -> IdentityLandingDTO: ...

    # TheSuperHackers @feature Leex 22/08/2026 Expose replay-library snapshots and commands without persistence values. (#0)
    def list_replays(self, query: ReplayLibraryQueryDTO) -> ReplayLibraryPageDTO: ...

    def import_roots(self) -> tuple[ImportRootDTO, ...]: ...

    def submit_upload(self, command: UploadImportCommandDTO) -> ImportSubmissionDTO: ...

    def submit_root_selection(self, command: RootImportCommandDTO) -> ImportSubmissionDTO: ...

    # TheSuperHackers @feature Leex 22/08/2026 Expose durable job operations without persistence or worker capabilities. (#TBD)
    def list_jobs(self, query: JobQueryDTO) -> JobPageDTO: ...

    def get_job(self, job_public_id: str) -> JobDetailDTO: ...

    def retry_job(self, command: RetryJobCommandDTO) -> JobMutationDTO: ...

    def cancel_job(self, command: CancelJobCommandDTO) -> JobMutationDTO: ...

    def read_job_log(self, query: JobLogQueryDTO) -> JobLogChunkDTO: ...


# TheSuperHackers @feature Leex 23/08/2026 Resolve mutable latest-report navigation into an immutable fixed report identity. (#TBD)
class LatestReportQueryDTO(WebDTO):
    replay_public_id: PublicId
    replay_player_public_id: PublicId | None = None


class FixedReportQueryDTO(WebDTO):
    replay_public_id: PublicId
    report_public_id: PublicId


class ReportVersionDTO(WebDTO):
    report_public_id: PublicId
    report_version: str = Field(min_length=1, max_length=128)
    replay_player_public_id: PublicId | None = None


class ReportResolutionDTO(WebDTO):
    state: Literal["available", "not_generated", "pipeline_active", "pipeline_failed"]
    fixed_report: FixedReportQueryDTO | None = None
    version: ReportVersionDTO | None = None
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)
    pipeline: PipelineStateDTO | None = None

    @model_validator(mode="after")
    def _require_exact_resolution_shape(self) -> Self:
        if self.state == "available":
            if (
                self.fixed_report is None
                or self.version is None
                or self.reason_code is not None
                or self.pipeline is not None
                or self.fixed_report.report_public_id != self.version.report_public_id
            ):
                raise ValueError("available report resolution requires one matching fixed report and version")
            return self
        if self.fixed_report is not None or self.version is not None or self.reason_code is None:
            raise ValueError("unavailable report resolution requires one reason and no fixed report")
        if self.state == "not_generated" and self.pipeline is not None:
            raise ValueError("not-generated report resolution cannot claim pipeline state")
        if self.state in {"pipeline_active", "pipeline_failed"} and self.pipeline is None:
            raise ValueError("pipeline report resolution requires pipeline state")
        return self


ReportSectionKey = Literal[
    "overview",
    "players_results",
    "opening_build_order",
    "economy",
    "production_composition",
    "combat_engagements",
    "activity",
    "strategy_phases",
    "spatial_analysis",
    "longitudinal_context",
    "llm_interpretation",
]
ReportTier = Literal["observed", "derived", "inferred"]
ReportAvailability = Literal["available", "partial", "unavailable"]
_REPORT_SECTION_ORDER: tuple[ReportSectionKey, ...] = (
    "overview",
    "players_results",
    "opening_build_order",
    "economy",
    "production_composition",
    "combat_engagements",
    "activity",
    "strategy_phases",
    "spatial_analysis",
    "longitudinal_context",
    "llm_interpretation",
)


class ReportEvidenceReferenceDTO(WebDTO):
    public_id: PublicId
    tier: ReportTier


class ReportLifecycleDTO(WebDTO):
    lifecycle_state: str = Field(min_length=1, max_length=64)
    parser_completion_status: str | None = Field(default=None, min_length=1, max_length=64)
    telemetry_status: str | None = Field(default=None, min_length=1, max_length=64)
    telemetry_runner_status: str | None = Field(default=None, min_length=1, max_length=64)


class ReportClaimDTO(WebDTO):
    claim_id: str = Field(min_length=1, max_length=256)
    section: ReportSectionKey
    label: str = Field(min_length=1, max_length=256)
    raw_value: object | None
    display_value: str | None = Field(default=None, min_length=1, max_length=2048)
    unit: str | None = Field(default=None, min_length=1, max_length=64)
    availability: ReportAvailability
    unavailable_reason: str | None = Field(default=None, min_length=1, max_length=256)
    scope: object
    frame_window: tuple[int, int] | None = None
    confidence: float | None = None
    evidence: tuple[ReportEvidenceReferenceDTO, ...] = ()
    details: object

    @field_validator("raw_value", "scope", "details", mode="before")
    @classmethod
    def _freeze_canonical_values(cls, value: object) -> object:
        return freeze_report_value(value)

    @field_validator("confidence", mode="before")
    @classmethod
    def _require_bounded_confidence(cls, value: object) -> object:
        if value is None:
            return None
        if type(value) is not float or not 0.0 <= value <= 1.0:
            raise ValueError("report confidence must be a bounded float")
        return value

    @model_validator(mode="after")
    def _validate_claim_semantics(self) -> Self:
        if self.frame_window is not None and (
            type(self.frame_window[0]) is not int
            or type(self.frame_window[1]) is not int
            or self.frame_window[0] < 0
            or self.frame_window[1] < self.frame_window[0]
        ):
            raise ValueError("report frame window must be ordered and nonnegative")
        identities = tuple((item.public_id, item.tier) for item in self.evidence)
        if len(identities) != len(set(identities)):
            raise ValueError("report claim evidence must be unique")
        if self.availability == "available":
            if self.raw_value is None or self.display_value is None or self.unavailable_reason is not None:
                raise ValueError("available report claim requires raw and display values without a reason")
            if not self.evidence:
                raise ValueError("available report claim requires public evidence")
        elif self.availability == "partial":
            if self.raw_value is None or self.display_value is None or self.unavailable_reason is None:
                raise ValueError("partial report claim requires values and one reason")
            if not self.evidence:
                raise ValueError("partial report claim requires public evidence")
        elif self.raw_value is not None or self.display_value is not None:
            raise ValueError("unavailable report claim cannot expose a value")
        elif self.unavailable_reason is None:
            raise ValueError("unavailable report claim requires one reason")
        return self


class ReportSectionDTO(WebDTO):
    key: ReportSectionKey
    title: str = Field(min_length=1, max_length=128)
    availability: AvailabilityDTO
    claims: tuple[ReportClaimDTO, ...] = ()

    @model_validator(mode="after")
    def _validate_section_claims(self) -> Self:
        if any(claim.section != self.key for claim in self.claims):
            raise ValueError("report claim section must match its containing section")
        identities = tuple(claim.claim_id for claim in self.claims)
        if len(identities) != len(set(identities)):
            raise ValueError("report section claim IDs must be unique")
        return self


class OllamaReportStatusDTO(WebDTO):
    requested: bool
    status: Literal["not_requested", "disabled", "succeeded", "unavailable", "failed", "invalid", "cancelled"]
    analysis_run_id: PublicId | None = None
    provider: str | None = Field(default=None, min_length=1, max_length=128)
    model_name: str | None = Field(default=None, min_length=1, max_length=256)
    model_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    prompt_version: str | None = Field(default=None, min_length=1, max_length=128)
    response_schema_version: str | None = Field(default=None, min_length=1, max_length=128)
    diagnostic_codes: tuple[str, ...] = ()
    validated_prose: object | None = None

    @field_validator("validated_prose", mode="before")
    @classmethod
    def _freeze_validated_prose(cls, value: object) -> object:
        return freeze_report_value(value)

    @model_validator(mode="after")
    def _validate_ollama_state(self) -> Self:
        fields = (
            self.analysis_run_id,
            self.provider,
            self.model_name,
            self.model_digest,
            self.prompt_version,
            self.response_schema_version,
            self.validated_prose,
        )
        if not self.requested:
            if self.status != "not_requested" or any(item is not None for item in fields) or self.diagnostic_codes:
                raise ValueError("not-requested Ollama status cannot expose analysis data")
        elif self.status == "not_requested":
            raise ValueError("requested Ollama status cannot be not_requested")
        elif self.status == "succeeded":
            if any(item is None for item in fields):
                raise ValueError("succeeded Ollama status requires complete validated identity and prose")
        elif self.validated_prose is not None:
            raise ValueError("only succeeded Ollama status can expose validated prose")
        object.__setattr__(self, "diagnostic_codes", tuple(sorted(set(self.diagnostic_codes))))
        return self


# TheSuperHackers @feature Leex 23/08/2026 Keep every report section explicit and evidence-backed at the web boundary. (#TBD)
class ReplayReportDTO(TimestampedWebDTO):
    schema_version: Literal["web-replay-report-v1"]
    fixed_report: FixedReportQueryDTO
    version: ReportVersionDTO
    availability: AvailabilityDTO
    replay_label: str = Field(min_length=1, max_length=256)
    replay_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    players: tuple[ReplayPlayerDisplayDTO, ...] = Field(min_length=1, max_length=16)
    result: str | None = Field(default=None, min_length=1, max_length=128)
    map_name: str | None = Field(default=None, min_length=1, max_length=256)
    patch: str | None = Field(default=None, min_length=1, max_length=64)
    duration_frames: int | None = Field(default=None, ge=0)
    source_mode: Literal["deterministic_only", "deterministic_with_ollama"]
    lifecycle: ReportLifecycleDTO
    terminal_quality: TerminalQualityDTO
    sections: tuple[ReportSectionDTO, ...]
    ollama: OllamaReportStatusDTO
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_report_graph(self) -> Self:
        if self.fixed_report.report_public_id != self.version.report_public_id:
            raise ValueError("report version does not match its fixed report identity")
        by_key = {section.key: section for section in self.sections}
        if len(by_key) != len(self.sections) or set(by_key) != set(_REPORT_SECTION_ORDER):
            raise ValueError("report must contain exactly one of every report section")
        object.__setattr__(self, "sections", tuple(by_key[key] for key in _REPORT_SECTION_ORDER))
        object.__setattr__(self, "warnings", tuple(sorted(set(self.warnings))))
        return self


TimelineFamily = Literal[
    "build_order",
    "economy",
    "production",
    "combat",
    "activity",
    "strategy",
    "quality",
]


class TimelineChartQueryDTO(WebDTO):
    replay_public_id: PublicId
    report_public_id: PublicId
    players: tuple[PublicId, ...] = ()
    families: tuple[TimelineFamily, ...] = ()

    @model_validator(mode="after")
    def _normalize_filters(self) -> Self:
        object.__setattr__(self, "players", tuple(sorted(set(self.players))))
        object.__setattr__(self, "families", tuple(sorted(set(self.families))))
        return self


class TimelinePlayerOptionDTO(WebDTO):
    public_id: PublicId
    label: str = Field(min_length=1, max_length=256)


class TimelineFamilyOptionDTO(WebDTO):
    value: TimelineFamily
    label: str = Field(min_length=1, max_length=128)


class TimelinePointDTO(WebDTO):
    frame: int = Field(ge=0)
    value: int | float | str | None
    label: str = Field(min_length=1, max_length=256)
    evidence: tuple[ReportEvidenceReferenceDTO, ...] = Field(min_length=1)

    @field_validator("value", mode="before")
    @classmethod
    def _require_finite_value(cls, value: object) -> object:
        if value is None:
            return None
        if type(value) is int:
            return value
        if type(value) is float:
            if not math.isfinite(value) or value == 0.0 and math.copysign(1.0, value) < 0:
                raise ValueError("timeline point float must be finite and not negative zero")
            return value
        if type(value) is str:
            if not value or value != value.strip():
                raise ValueError("timeline point string must be nonempty and canonical")
            freeze_report_value(value)
            return value
        raise ValueError("timeline point value must use the closed scalar union")


class TimelineIntervalDTO(WebDTO):
    frame_start: int = Field(ge=0)
    frame_end: int = Field(ge=0)
    label: str = Field(min_length=1, max_length=256)
    evidence: tuple[ReportEvidenceReferenceDTO, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _require_ordered_interval(self) -> Self:
        if self.frame_end <= self.frame_start:
            raise ValueError("timeline interval must have positive frame width")
        return self


class TimelineSeriesDTO(WebDTO):
    series_id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=128)
    kind: Literal["marker", "line", "step", "band"]
    player_public_id: PublicId | None = None
    event_family: TimelineFamily
    unit: str | None = Field(default=None, min_length=1, max_length=64)
    availability: AvailabilityDTO
    points: tuple[TimelinePointDTO, ...] = ()
    intervals: tuple[TimelineIntervalDTO, ...] = ()

    @model_validator(mode="after")
    def _validate_series_shape(self) -> Self:
        points = tuple(sorted(self.points, key=lambda item: (item.frame, item.label)))
        intervals = tuple(sorted(self.intervals, key=lambda item: (item.frame_start, item.frame_end, item.label)))
        if self.availability.state == "unavailable":
            if points or intervals:
                raise ValueError("unavailable timeline series cannot expose data")
        elif self.kind == "band":
            if points or not intervals:
                raise ValueError("timeline series kind band requires intervals only")
        elif self.kind == "marker":
            if intervals or not points:
                raise ValueError("timeline series kind marker requires points only")
        elif intervals or not points or any(point.value is None for point in points):
            raise ValueError("timeline series kind line or step requires valued points only")
        object.__setattr__(self, "points", points)
        object.__setattr__(self, "intervals", intervals)
        return self


# TheSuperHackers @feature Leex 23/08/2026 Preserve authoritative frame time at a fixed 30 FPS chart boundary. (#TBD)
class TimelineChartDTO(WebDTO):
    schema_version: Literal["web-report-timeline-v1"]
    query: TimelineChartQueryDTO
    availability: AvailabilityDTO
    timebase_fps: Literal[30]
    available_players: tuple[TimelinePlayerOptionDTO, ...]
    available_families: tuple[TimelineFamilyOptionDTO, ...]
    series: tuple[TimelineSeriesDTO, ...]

    @model_validator(mode="after")
    def _canonicalize_chart(self) -> Self:
        players = {item.public_id: item for item in self.available_players}
        families = {item.value: item for item in self.available_families}
        series = {item.series_id: item for item in self.series}
        if (
            len(players) != len(self.available_players)
            or len(families) != len(self.available_families)
            or len(series) != len(self.series)
        ):
            raise ValueError("timeline options and series identities must be unique")
        if any(
            item.event_family not in families
            or (item.player_public_id is not None and item.player_public_id not in players)
            for item in series.values()
        ):
            raise ValueError("timeline series must use an available player and family")
        if self.availability.state == "unavailable" and any(
            item.availability.state != "unavailable" for item in series.values()
        ):
            raise ValueError("unavailable timeline chart cannot expose available geometry")
        if series and all(item.availability.state == "unavailable" for item in series.values()):
            object.__setattr__(
                self,
                "availability",
                AvailabilityDTO(
                    state="unavailable",
                    reason_codes=self.availability.reason_codes or ("timeline_series_unavailable",),
                    evidence_references=self.availability.evidence_references,
                ),
            )
        object.__setattr__(self, "available_players", tuple(players[key] for key in sorted(players)))
        object.__setattr__(self, "available_families", tuple(families[key] for key in sorted(families)))
        object.__setattr__(
            self,
            "series",
            tuple(
                sorted(
                    series.values(),
                    key=lambda item: (item.event_family, item.player_public_id or "", item.kind, item.series_id),
                )
            ),
        )
        return self


EvidenceRole = Literal["input", "supporting", "contradicting"]


class EvidenceQueryDTO(WebDTO):
    report_public_id: PublicId
    evidence_public_id: PublicId
    expected_tier: ReportTier


class EvidenceLinkDTO(WebDTO):
    public_id: PublicId
    tier: ReportTier
    role: EvidenceRole


def _ordered_evidence_links(values: tuple[EvidenceLinkDTO, ...]) -> tuple[EvidenceLinkDTO, ...]:
    identities = tuple((item.public_id, item.tier, item.role) for item in values)
    if len(identities) != len(set(identities)):
        raise ValueError("evidence links must be unique")
    return tuple(sorted(values, key=lambda item: (item.role, item.public_id, item.tier)))


def _validate_source_availability(
    availability: ReportAvailability,
    unavailable_reason: str | None,
    value: object | None,
) -> None:
    if availability == "available" and (unavailable_reason is not None or value is None):
        raise ValueError("available evidence requires a value without a reason")
    if availability == "partial" and (unavailable_reason is None or value is None):
        raise ValueError("partial evidence requires a value and reason")
    if availability == "unavailable" and (unavailable_reason is None or value is not None):
        raise ValueError("unavailable evidence requires a reason and no value")


class ObservedCommandEvidenceDTO(WebDTO):
    kind: Literal["replay_command"]
    parser_run_id: PublicId
    parser_version: str = Field(min_length=1, max_length=255)
    parser_schema_version: int = Field(ge=0)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=1)
    frame: int = Field(ge=0)
    message_type: int = Field(ge=0)
    message_name: str = Field(min_length=1, max_length=256)
    replay_player_public_id: PublicId | None = None
    arguments: object

    @field_validator("arguments", mode="before")
    @classmethod
    def _freeze_arguments(cls, value: object) -> object:
        return freeze_report_value(value)

    @model_validator(mode="after")
    def _validate_offsets(self) -> Self:
        if self.end_offset <= self.start_offset:
            raise ValueError("observed command offsets must be ordered")
        return self


class ObservedTelemetryEvidenceDTO(WebDTO):
    kind: Literal["telemetry_event"]
    telemetry_run_id: PublicId
    engine_build: str = Field(min_length=1, max_length=256)
    telemetry_schema_version: int = Field(ge=0)
    sequence: int = Field(ge=0)
    frame: int = Field(ge=0)
    event_type: str = Field(min_length=1, max_length=256)
    payload: object

    @field_validator("payload", mode="before")
    @classmethod
    def _freeze_payload(cls, value: object) -> object:
        return freeze_report_value(value)


class DerivedFeatureEvidenceDTO(WebDTO):
    kind: Literal["derived_feature"]
    feature_public_id: PublicId
    feature_set_public_id: PublicId
    feature_name: str = Field(min_length=1, max_length=256)
    extractor_name: str = Field(min_length=1, max_length=256)
    extractor_version: str = Field(min_length=1, max_length=128)
    raw_value: object | None
    unit: str | None = Field(default=None, min_length=1, max_length=64)
    scope: object
    frame_start: int = Field(ge=0)
    frame_end: int = Field(ge=0)
    availability: ReportAvailability
    unavailable_reason: str | None = Field(default=None, min_length=1, max_length=256)
    details: object
    inputs: tuple[EvidenceLinkDTO, ...]

    @field_validator("raw_value", "scope", "details", mode="before")
    @classmethod
    def _freeze_values(cls, value: object) -> object:
        return freeze_report_value(value)

    @model_validator(mode="after")
    def _validate_feature(self) -> Self:
        if self.frame_end < self.frame_start:
            raise ValueError("derived feature window must be ordered")
        _validate_source_availability(self.availability, self.unavailable_reason, self.raw_value)
        object.__setattr__(self, "inputs", _ordered_evidence_links(self.inputs))
        return self


class DerivedAssessmentEvidenceDTO(WebDTO):
    kind: Literal["derived_assessment"]
    assessment_public_id: PublicId
    strategy_label: str = Field(min_length=1, max_length=256)
    phase: str = Field(min_length=1, max_length=128)
    taxonomy_version: str = Field(min_length=1, max_length=128)
    rule_version: str = Field(min_length=1, max_length=128)
    frame_start: int = Field(ge=0)
    frame_end: int = Field(ge=0)
    score: float | None
    availability: ReportAvailability
    unavailable_reason: str | None = Field(default=None, min_length=1, max_length=256)
    details: object
    citations: tuple[EvidenceLinkDTO, ...]

    @field_validator("details", mode="before")
    @classmethod
    def _freeze_details(cls, value: object) -> object:
        return freeze_report_value(value)

    @field_validator("score", mode="before")
    @classmethod
    def _validate_score(cls, value: object) -> object:
        if value is None:
            return None
        if type(value) is not float or not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("derived assessment score must be a bounded float")
        return value

    @model_validator(mode="after")
    def _validate_assessment(self) -> Self:
        if self.frame_end < self.frame_start:
            raise ValueError("derived assessment window must be ordered")
        _validate_source_availability(self.availability, self.unavailable_reason, self.score)
        object.__setattr__(self, "citations", _ordered_evidence_links(self.citations))
        return self


class DerivedLongitudinalEvidenceDTO(WebDTO):
    kind: Literal["longitudinal_result"]
    result_public_id: PublicId
    longitudinal_run_id: PublicId
    analyzer_name: str = Field(min_length=1, max_length=256)
    analyzer_version: str = Field(min_length=1, max_length=128)
    result_name: str = Field(min_length=1, max_length=256)
    result_kind: str = Field(min_length=1, max_length=128)
    sample_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    availability: ReportAvailability
    unavailable_reason: str | None = Field(default=None, min_length=1, max_length=256)
    statistics: object | None
    members: tuple[EvidenceLinkDTO, ...]

    @field_validator("statistics", mode="before")
    @classmethod
    def _freeze_statistics(cls, value: object) -> object:
        return freeze_report_value(value)

    @model_validator(mode="after")
    def _validate_longitudinal(self) -> Self:
        _validate_source_availability(self.availability, self.unavailable_reason, self.statistics)
        object.__setattr__(self, "members", _ordered_evidence_links(self.members))
        return self


class InferredAssessmentEvidenceDTO(WebDTO):
    kind: Literal["inferred_assessment"]
    assessment_public_id: PublicId
    analysis_run_id: PublicId
    assessment_key: str = Field(min_length=1, max_length=256)
    strategy_label: str = Field(min_length=1, max_length=256)
    phase: str = Field(min_length=1, max_length=128)
    frame_start: int = Field(ge=0)
    frame_end: int = Field(ge=0)
    confidence: float
    provider: str = Field(min_length=1, max_length=128)
    model_name: str = Field(min_length=1, max_length=256)
    model_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_version: str = Field(min_length=1, max_length=128)
    response_schema_version: str = Field(min_length=1, max_length=128)
    assessment: object
    citations: tuple[EvidenceLinkDTO, ...]

    @field_validator("assessment", mode="before")
    @classmethod
    def _freeze_assessment(cls, value: object) -> object:
        return freeze_report_value(value)

    @field_validator("confidence", mode="before")
    @classmethod
    def _validate_confidence(cls, value: object) -> object:
        if type(value) is not float or not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("inferred confidence must be a bounded float")
        return value

    @model_validator(mode="after")
    def _validate_inference(self) -> Self:
        if self.frame_end < self.frame_start:
            raise ValueError("inferred assessment window must be ordered")
        object.__setattr__(self, "citations", _ordered_evidence_links(self.citations))
        return self


EvidenceSourceDTO = Annotated[
    ObservedCommandEvidenceDTO
    | ObservedTelemetryEvidenceDTO
    | DerivedFeatureEvidenceDTO
    | DerivedAssessmentEvidenceDTO
    | DerivedLongitudinalEvidenceDTO
    | InferredAssessmentEvidenceDTO,
    Field(discriminator="kind"),
]


# TheSuperHackers @feature Leex 23/08/2026 Preserve each accepted evidence family as a typed report-scoped web value. (#TBD)
class EvidenceDetailDTO(WebDTO):
    schema_version: Literal["web-evidence-inspector-v1"]
    query: EvidenceQueryDTO
    replay_public_id: PublicId
    source_kind: str = Field(min_length=1, max_length=128)
    source_schema_version: int = Field(ge=0)
    source: EvidenceSourceDTO

    @model_validator(mode="after")
    def _validate_source_identity(self) -> Self:
        if type(self.source) is ObservedCommandEvidenceDTO:
            expected = ("observed", "parser_command", self.source.parser_schema_version)
        elif type(self.source) is ObservedTelemetryEvidenceDTO:
            expected = ("observed", "telemetry_event", self.source.telemetry_schema_version)
        elif type(self.source) is DerivedFeatureEvidenceDTO:
            expected = ("derived", "feature", 1)
        elif type(self.source) is DerivedAssessmentEvidenceDTO:
            expected = ("derived", "strategy_rule", 1)
        elif type(self.source) is DerivedLongitudinalEvidenceDTO:
            expected = ("derived", "longitudinal_corpus", 1)
        else:
            expected = ("inferred", "llm", 1)
        if (self.query.expected_tier, self.source_kind, self.source_schema_version) != expected:
            raise ValueError("evidence tier, source kind, and schema version must match the typed source")
        return self


# TheSuperHackers @feature Leex 23/08/2026 Isolate report reads behind one immutable fakeable web capability. (#TBD)
@runtime_checkable
class ReportQueryPort(Protocol):
    def resolve_latest(self, query: LatestReportQueryDTO) -> ReportResolutionDTO: ...

    def get_report(self, query: FixedReportQueryDTO) -> ReplayReportDTO: ...

    def timeline_chart(self, query: TimelineChartQueryDTO) -> TimelineChartDTO: ...

    def get_evidence(self, query: EvidenceQueryDTO) -> EvidenceDetailDTO: ...


EvidenceTier = Literal["observed", "derived"]
CoordinateDisplay = Literal["raw", "map_normalized", "player_centric"]
LocomotorSurface = Literal["ground", "amphibious"]
MapEventFamily = Literal[
    "samples",
    "orders",
    "routes",
    "structures",
    "engagements",
    "casualties",
    "presence",
    "visibility",
    "engine_heuristics",
]
MapAvailabilityFilter = Literal["available", "partial", "unavailable"]
SampleReason = Literal["lifecycle_forced", "order_forced", "state_forced", "changed", "periodic_moving_heartbeat"]


def _finite_canonical_float(value: float) -> float:
    if not math.isfinite(value) or (value == 0.0 and math.copysign(1.0, value) < 0):
        raise ValueError("spatial values must be finite and cannot use negative zero")
    return value


FiniteSpatialFloat = Annotated[float, AfterValidator(_finite_canonical_float)]


class MapOptionDTO(WebDTO):
    public_id: PublicId
    label: str = Field(min_length=1, max_length=256)


class SpatialEvidenceReferenceDTO(WebDTO):
    evidence_public_id: PublicId
    tier: EvidenceTier


def _ordered_spatial_evidence(
    values: tuple[SpatialEvidenceReferenceDTO, ...],
) -> tuple[SpatialEvidenceReferenceDTO, ...]:
    identities = tuple((item.tier, item.evidence_public_id) for item in values)
    if len(identities) != len(set(identities)):
        raise ValueError("spatial evidence references must be unique")
    return tuple(sorted(values, key=lambda item: (item.tier, item.evidence_public_id)))


class FrameWindowDTO(WebDTO):
    frame_start: int = Field(ge=0)
    frame_end: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_window(self) -> Self:
        if self.frame_end < self.frame_start:
            raise ValueError("frame window must be ordered")
        return self


class MapSceneIndexQueryDTO(WebDTO):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)
    search: str | None = Field(default=None, min_length=1, max_length=256)
    availability: MapAvailabilityFilter | None = None


class MapSceneSummaryDTO(WebDTO):
    replay_public_id: PublicId
    report_public_id: PublicId
    map_public_id: PublicId
    map_display_name: str = Field(min_length=1, max_length=256)
    report_version: str = Field(min_length=1, max_length=128)
    frame_window: FrameWindowDTO
    players: tuple[MapOptionDTO, ...] = Field(max_length=16)
    availability: AvailabilityDTO

    @model_validator(mode="after")
    def _order_players(self) -> Self:
        identities = tuple(item.public_id for item in self.players)
        if len(identities) != len(set(identities)):
            raise ValueError("map player options must be unique")
        object.__setattr__(self, "players", tuple(sorted(self.players, key=lambda item: item.public_id)))
        return self


class MapSceneIndexPageDTO(WebDTO):
    query: MapSceneIndexQueryDTO
    items: tuple[MapSceneSummaryDTO, ...]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total_items: int = Field(ge=0)
    availability: AvailabilityDTO

    @model_validator(mode="after")
    def _validate_page(self) -> Self:
        if (self.page, self.page_size) != (self.query.page, self.query.page_size):
            raise ValueError("map page identity must match its normalized query")
        identities = tuple((item.replay_public_id, item.report_public_id) for item in self.items)
        if len(identities) != len(set(identities)):
            raise ValueError("map scene summaries must be unique")
        object.__setattr__(
            self,
            "items",
            tuple(sorted(self.items, key=lambda item: (item.replay_public_id, item.report_public_id))),
        )
        return self


class MapSceneQueryDTO(WebDTO):
    replay_public_id: PublicId
    report_public_id: PublicId
    frame_start: int = Field(ge=0)
    frame_end: int = Field(ge=0)
    replay_player_public_ids: tuple[PublicId, ...] = ()
    entity_public_ids: tuple[PublicId, ...] = ()
    event_families: tuple[MapEventFamily, ...] = ()
    locomotor_surface: LocomotorSurface | None = None
    coordinate_display: CoordinateDisplay = "raw"
    player_centric_subject_public_id: PublicId | None = None
    sample_budget: int = Field(default=5000, ge=100, le=20_000)
    include_engine_heuristics: bool = False

    @model_validator(mode="after")
    def _normalize_query(self) -> Self:
        if self.frame_end < self.frame_start:
            raise ValueError("map scene frame window must be ordered")
        players = tuple(sorted(set(self.replay_player_public_ids)))
        entities = tuple(sorted(set(self.entity_public_ids)))
        families = tuple(sorted(set(self.event_families)))
        if self.coordinate_display == "player_centric" and self.player_centric_subject_public_id is None:
            raise ValueError("player-centric display requires a subject")
        if (
            self.player_centric_subject_public_id is not None
            and players
            and self.player_centric_subject_public_id not in players
        ):
            raise ValueError("player-centric subject must be present in the player filter")
        object.__setattr__(self, "replay_player_public_ids", players)
        object.__setattr__(self, "entity_public_ids", entities)
        object.__setattr__(self, "event_families", families)
        return self


class RawPositionDTO(WebDTO):
    x: FiniteSpatialFloat
    y: FiniteSpatialFloat
    z: FiniteSpatialFloat


class NormalizedPositionDTO(WebDTO):
    u: FiniteSpatialFloat = Field(ge=0, le=1)
    v: FiniteSpatialFloat = Field(ge=0, le=1)


class PlayerCentricPositionDTO(WebDTO):
    forward: FiniteSpatialFloat
    left: FiniteSpatialFloat
    z: FiniteSpatialFloat


class SpatialPositionDTO(WebDTO):
    raw: RawPositionDTO
    map_normalized: NormalizedPositionDTO
    player_centric: PlayerCentricPositionDTO | None = None


class RawCoordinateSystemDTO(WebDTO):
    coordinate_version: Literal["engine-world-xyz-v1"]
    axes: tuple[Literal["engine_world_x"], Literal["engine_world_y"], Literal["engine_world_z"]]
    units: Literal["engine_world_unit"]
    minimum: RawPositionDTO
    maximum: RawPositionDTO
    minimum_inclusive: Literal[True]
    maximum_inclusive: Literal[True]


class MapNormalizedTransformDTO(WebDTO):
    transform_version: Literal["map-normalized-v1"]
    formula: Literal["u=(x-min_x)/(max_x-min_x);v=(y-min_y)/(max_y-min_y)"]
    availability: AvailabilityDTO


class PlayerCentricTransformDTO(WebDTO):
    transform_version: Literal["player-centric-v1"]
    subject_replay_player_public_id: PublicId
    own_start_public_id: PublicId
    reference_enemy_start_public_id: PublicId
    angle_radians: FiniteSpatialFloat
    availability: AvailabilityDTO
    evidence: tuple[SpatialEvidenceReferenceDTO, ...]

    @model_validator(mode="after")
    def _order_evidence(self) -> Self:
        object.__setattr__(self, "evidence", _ordered_spatial_evidence(self.evidence))
        return self


class CoordinateTransformsDTO(WebDTO):
    raw: RawCoordinateSystemDTO
    map_normalized: MapNormalizedTransformDTO
    player_centric: tuple[PlayerCentricTransformDTO, ...]

    @model_validator(mode="after")
    def _order_player_transforms(self) -> Self:
        identities = tuple(item.subject_replay_player_public_id for item in self.player_centric)
        if len(identities) != len(set(identities)):
            raise ValueError("player-centric transforms must have unique subjects")
        object.__setattr__(
            self,
            "player_centric",
            tuple(sorted(self.player_centric, key=lambda item: item.subject_replay_player_public_id)),
        )
        return self


class RasterPlacementDTO(WebDTO):
    raw_minimum_x: FiniteSpatialFloat
    raw_minimum_y: FiniteSpatialFloat
    raw_maximum_x: FiniteSpatialFloat
    raw_maximum_y: FiniteSpatialFloat
    grid_width: int = Field(ge=1)
    grid_height: int = Field(ge=1)
    source_storage_order: Literal["row_major_y_then_x_x_fastest"]
    source_row_zero: Literal["minimum_world_y"]
    png_row_zero: Literal["maximum_world_y"]
    display_interpolation: Literal["nearest"]

    @model_validator(mode="after")
    def _validate_bounds(self) -> Self:
        if self.raw_maximum_x <= self.raw_minimum_x or self.raw_maximum_y <= self.raw_minimum_y:
            raise ValueError("raster placement bounds must have positive extent")
        return self


class MapRasterDescriptorDTO(WebDTO):
    raster_public_id: PublicId
    kind: Literal["terrain_cell_type", "pathability"]
    locomotor_surface: LocomotorSurface | None
    rasterization_version: Literal["map-grid-raster-v1"]
    media_type: Literal["image/png"]
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    placement: RasterPlacementDTO
    availability: AvailabilityDTO

    @model_validator(mode="after")
    def _validate_raster_kind(self) -> Self:
        if (self.kind == "terrain_cell_type") != (self.locomotor_surface is None):
            raise ValueError("only pathability rasters have a locomotor surface")
        if (self.width, self.height) != (self.placement.grid_width, self.placement.grid_height):
            raise ValueError("raster dimensions must match its grid placement")
        return self


class MapRasterQueryDTO(WebDTO):
    map_public_id: PublicId
    raster_public_id: PublicId


class MapRasterResourceDTO(BaseModel):
    """Bounded opaque PNG bytes; the route derives its same-origin URL from public IDs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    map_public_id: PublicId
    raster: MapRasterDescriptorDTO
    content: bytes = Field(max_length=16 * 1024 * 1024)

    @model_validator(mode="after")
    def _validate_png(self) -> Self:
        from hashlib import sha256

        if not self.content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("map raster resource must contain PNG bytes")
        if sha256(self.content).hexdigest() != self.raster.content_sha256:
            raise ValueError("map raster resource digest must match its descriptor")
        return self


class MapStartDTO(WebDTO):
    start_public_id: PublicId
    name: str = Field(min_length=1, max_length=256)
    replay_player_public_ids: tuple[PublicId, ...]
    position: SpatialPositionDTO
    evidence: tuple[SpatialEvidenceReferenceDTO, ...]

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        object.__setattr__(self, "replay_player_public_ids", tuple(sorted(set(self.replay_player_public_ids))))
        object.__setattr__(self, "evidence", _ordered_spatial_evidence(self.evidence))
        return self


class MapResourceDTO(WebDTO):
    resource_public_id: PublicId
    resource_kind: Literal[
        "supply_source", "supply_warehouse", "capturable", "tech_building", "cash_generator", "oil_income"
    ]
    label: str = Field(min_length=1, max_length=256)
    position: SpatialPositionDTO
    amount: FiniteSpatialFloat | None = Field(default=None, ge=0)
    availability: AvailabilityDTO
    evidence: tuple[SpatialEvidenceReferenceDTO, ...]

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        object.__setattr__(self, "evidence", _ordered_spatial_evidence(self.evidence))
        return self


class MapStructureDTO(WebDTO):
    structure_public_id: PublicId
    source_kind: Literal["map_static", "construction_completed"]
    replay_player_public_id: PublicId | None = None
    template_name: str = Field(min_length=1, max_length=256)
    frame: int | None = Field(default=None, ge=0)
    position: SpatialPositionDTO
    availability: AvailabilityDTO
    evidence: tuple[SpatialEvidenceReferenceDTO, ...]

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        if (self.source_kind == "map_static") != (self.frame is None):
            raise ValueError("only completed construction structures have a frame")
        object.__setattr__(self, "evidence", _ordered_spatial_evidence(self.evidence))
        return self


class MapSampleDTO(WebDTO):
    sample_public_id: PublicId
    entity_public_id: PublicId
    replay_player_public_id: PublicId | None = None
    frame: int = Field(ge=0)
    position: SpatialPositionDTO
    orientation: FiniteSpatialFloat
    sample_reason: SampleReason
    locomotor_surface: LocomotorSurface | None = None
    evidence: tuple[SpatialEvidenceReferenceDTO, ...]

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        object.__setattr__(self, "evidence", _ordered_spatial_evidence(self.evidence))
        return self


class MapOrderDTO(WebDTO):
    order_public_id: PublicId
    replay_player_public_id: PublicId
    frame: int = Field(ge=0)
    target_kind: Literal["none", "object", "location"]
    target_position: SpatialPositionDTO | None = None
    selected_entity_public_ids: tuple[PublicId, ...]
    label: str = Field(min_length=1, max_length=256)
    evidence: tuple[SpatialEvidenceReferenceDTO, ...]

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        if (self.target_kind == "location") != (self.target_position is not None):
            raise ValueError("only location orders contain a public target position")
        object.__setattr__(self, "selected_entity_public_ids", tuple(sorted(set(self.selected_entity_public_ids))))
        object.__setattr__(self, "evidence", _ordered_spatial_evidence(self.evidence))
        return self


class MapRouteSegmentDTO(WebDTO):
    segment_public_id: PublicId
    entity_public_id: PublicId
    replay_player_public_id: PublicId | None = None
    frame_start: int = Field(ge=0)
    frame_end: int = Field(ge=0)
    locomotor_surface: LocomotorSurface
    route_algorithm_version: Literal["grid-route-v1"]
    points: tuple[SpatialPositionDTO, ...]
    availability: AvailabilityDTO
    evidence: tuple[SpatialEvidenceReferenceDTO, ...]

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        if self.frame_end < self.frame_start:
            raise ValueError("route frame window must be ordered")
        if self.availability.state == "available" and len(self.points) < 1:
            raise ValueError("available routes require validated grid points")
        if self.availability.state == "unavailable" and self.points:
            raise ValueError("unavailable route gaps cannot expose line geometry")
        object.__setattr__(self, "evidence", _ordered_spatial_evidence(self.evidence))
        return self


class MapEngagementDTO(WebDTO):
    engagement_public_id: PublicId
    frame_start: int = Field(ge=0)
    frame_end: int = Field(ge=0)
    centroid: SpatialPositionDTO
    participant_replay_player_public_ids: tuple[PublicId, ...]
    applied_damage_sum: FiniteSpatialFloat = Field(ge=0)
    killing_blow_count: int = Field(ge=0)
    engagement_algorithm_version: Literal["engagement-cluster-v1"]
    availability: AvailabilityDTO
    evidence: tuple[SpatialEvidenceReferenceDTO, ...]

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        if self.frame_end < self.frame_start:
            raise ValueError("engagement frame window must be ordered")
        object.__setattr__(
            self, "participant_replay_player_public_ids", tuple(sorted(set(self.participant_replay_player_public_ids)))
        )
        object.__setattr__(self, "evidence", _ordered_spatial_evidence(self.evidence))
        return self


class MapCasualtyDTO(WebDTO):
    casualty_public_id: PublicId
    frame: int = Field(ge=0)
    victim_replay_player_public_id: PublicId | None = None
    attacker_replay_player_public_id: PublicId | None = None
    position: SpatialPositionDTO
    evidence: tuple[SpatialEvidenceReferenceDTO, ...]

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        object.__setattr__(self, "evidence", _ordered_spatial_evidence(self.evidence))
        return self


class PlayerShareDTO(WebDTO):
    replay_player_public_id: PublicId
    observed_sample_count: int = Field(ge=0)
    share: FiniteSpatialFloat = Field(ge=0, le=1)
    evidence: tuple[SpatialEvidenceReferenceDTO, ...]

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        object.__setattr__(self, "evidence", _ordered_spatial_evidence(self.evidence))
        return self


class PresenceCellDTO(WebDTO):
    cell_x: int = Field(ge=0)
    cell_y: int = Field(ge=0)
    shares: tuple[PlayerShareDTO, ...]
    evidence: tuple[SpatialEvidenceReferenceDTO, ...]

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        identities = tuple(item.replay_player_public_id for item in self.shares)
        if len(identities) != len(set(identities)):
            raise ValueError("presence shares must have unique players")
        object.__setattr__(self, "shares", tuple(sorted(self.shares, key=lambda item: item.replay_player_public_id)))
        object.__setattr__(self, "evidence", _ordered_spatial_evidence(self.evidence))
        return self


class MapControlWindowDTO(WebDTO):
    control_window_public_id: PublicId
    frame_window: FrameWindowDTO
    metric_kind: Literal["observed_cell_presence_share"]
    algorithm_version: Literal["sample-count-presence-v1"]
    cells: tuple[PresenceCellDTO, ...]
    availability: AvailabilityDTO

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        cell_ids = tuple((item.cell_x, item.cell_y) for item in self.cells)
        if len(cell_ids) != len(set(cell_ids)):
            raise ValueError("presence cells must be unique")
        object.__setattr__(self, "cells", tuple(sorted(self.cells, key=lambda item: (item.cell_y, item.cell_x))))
        return self


# TheSuperHackers @feature Leex 24/08/2026 Publish engine-observed scouting transitions and sampled AI heuristics without reinterpreting them as map control. (#TBD)
class MapVisibilityTransitionDTO(WebDTO):
    visibility_public_id: PublicId
    replay_player_public_id: PublicId
    entity_public_id: PublicId
    frame: int = Field(ge=0)
    template_name: str = Field(min_length=1, max_length=256)
    previous_status: Literal["unseen", "clear", "fogged", "shrouded"]
    status: Literal["clear", "fogged", "shrouded"]
    first_observed_clear: bool
    position: SpatialPositionDTO
    sampling_cycle_id: int = Field(ge=0)
    evidence: tuple[SpatialEvidenceReferenceDTO, ...]

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        if self.previous_status == self.status:
            raise ValueError("visibility transition must change status")
        if self.first_observed_clear != (self.previous_status == "unseen" and self.status == "clear"):
            raise ValueError("first observed clear must identify the initial clear transition")
        object.__setattr__(self, "evidence", _ordered_spatial_evidence(self.evidence))
        return self


class MapVisibilitySamplingSummaryDTO(WebDTO):
    summary_public_id: PublicId
    frame: int = Field(ge=0)
    eligible_pair_count: int = Field(ge=0)
    sampled_pair_count: int = Field(ge=0, le=8192)
    maximum_pairs_per_pass: Literal[8192]
    sampling_cycle_id: int = Field(ge=0)
    cycle_complete: bool
    coverage_state: Literal["complete", "incomplete"]
    evidence: tuple[SpatialEvidenceReferenceDTO, ...]

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        if self.sampled_pair_count > self.eligible_pair_count:
            raise ValueError("sampled visibility pairs cannot exceed eligible pairs")
        if self.cycle_complete != (self.coverage_state == "complete"):
            raise ValueError("visibility coverage state must disclose incomplete cycles")
        object.__setattr__(self, "evidence", _ordered_spatial_evidence(self.evidence))
        return self


class MapEngineHeuristicCellDTO(WebDTO):
    cell_x: int = Field(ge=0)
    cell_y: int = Field(ge=0)
    position: SpatialPositionDTO
    shroud_status: Literal["clear", "fogged", "shrouded"]
    threat_value: int = Field(ge=0, le=4_294_967_295)
    cash_value: int = Field(ge=0, le=4_294_967_295)
    evidence: tuple[SpatialEvidenceReferenceDTO, ...]

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        object.__setattr__(self, "evidence", _ordered_spatial_evidence(self.evidence))
        return self


class MapEngineHeuristicOverlayDTO(WebDTO):
    overlay_public_id: PublicId
    replay_player_public_id: PublicId
    frame: int = Field(ge=0)
    sampling_scheme: Literal["uniform_partition_lattice_v1"]
    grid_complete: bool
    threat_label: Literal["Engine AI threat heuristic"]
    cash_label: Literal["Engine AI cash-value heuristic"]
    cells: tuple[MapEngineHeuristicCellDTO, ...] = Field(min_length=1, max_length=128)
    evidence: tuple[SpatialEvidenceReferenceDTO, ...]

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        coordinates = tuple((item.cell_x, item.cell_y) for item in self.cells)
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("engine heuristic overlay cells must be unique")
        object.__setattr__(self, "cells", tuple(sorted(self.cells, key=lambda item: (item.cell_y, item.cell_x))))
        object.__setattr__(self, "evidence", _ordered_spatial_evidence(self.evidence))
        return self


class DownsamplingDTO(WebDTO):
    algorithm_version: Literal["event-forced-stratified-v1"]
    requested_sample_budget: int = Field(ge=100, le=20_000)
    original_sample_count: int = Field(ge=0)
    mandatory_sample_count: int = Field(ge=0)
    returned_sample_count: int = Field(ge=0)
    budget_exceeded_by_mandatory: bool

    @model_validator(mode="after")
    def _validate_counts(self) -> Self:
        if not self.mandatory_sample_count <= self.returned_sample_count <= self.original_sample_count:
            raise ValueError("downsampling counts are inconsistent")
        if self.budget_exceeded_by_mandatory != (self.mandatory_sample_count > self.requested_sample_budget):
            raise ValueError("mandatory sample budget flag is inconsistent")
        if (
            self.mandatory_sample_count <= self.requested_sample_budget
            and self.returned_sample_count > self.requested_sample_budget
        ):
            raise ValueError("nonmandatory samples cannot exceed the requested budget")
        return self


# TheSuperHackers @feature Leex 23/08/2026 Expose one fixed authoritative spatial scene without storage capabilities. (#TBD)
class MapSceneDTO(WebDTO):
    schema_version: Literal["replay-map-scene-v2"]
    replay_public_id: PublicId
    report_public_id: PublicId
    report_version: str = Field(min_length=1, max_length=128)
    map_public_id: PublicId
    map_display_name: str = Field(min_length=1, max_length=256)
    map_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    map_schema_version: Literal[1, 2]
    engine_data_identity: str = Field(min_length=1, max_length=256)
    query: MapSceneQueryDTO
    available_frame_window: FrameWindowDTO
    transforms: CoordinateTransformsDTO
    rasters: tuple[MapRasterDescriptorDTO, ...]
    starts: tuple[MapStartDTO, ...]
    resources: tuple[MapResourceDTO, ...]
    structures: tuple[MapStructureDTO, ...]
    samples: tuple[MapSampleDTO, ...]
    orders: tuple[MapOrderDTO, ...]
    routes: tuple[MapRouteSegmentDTO, ...]
    engagements: tuple[MapEngagementDTO, ...]
    casualties: tuple[MapCasualtyDTO, ...]
    control_windows: tuple[MapControlWindowDTO, ...]
    visibility_transitions: tuple[MapVisibilityTransitionDTO, ...]
    visibility_sampling_summaries: tuple[MapVisibilitySamplingSummaryDTO, ...]
    engine_heuristic_overlays: tuple[MapEngineHeuristicOverlayDTO, ...]
    downsampling: DownsamplingDTO
    availability: AvailabilityDTO
    terminal_quality: TerminalQualityDTO

    @model_validator(mode="after")
    def _validate_scene(self) -> Self:
        if (self.query.replay_public_id, self.query.report_public_id) != (
            self.replay_public_id,
            self.report_public_id,
        ):
            raise ValueError("map scene identity must match its fixed query")
        if not (
            self.available_frame_window.frame_start <= self.query.frame_start
            and self.query.frame_end <= self.available_frame_window.frame_end
        ):
            raise ValueError("map query must stay inside the available frame window")
        if self.downsampling.returned_sample_count != len(self.samples):
            raise ValueError("returned sample count must equal the scene sample tuple")
        if not self.query.include_engine_heuristics and self.engine_heuristic_overlays:
            raise ValueError("engine heuristic overlays require explicit query opt-in")
        semantic_identities: tuple[tuple[str, tuple[object, ...]], ...] = (
            ("rasters", tuple(item.raster_public_id for item in self.rasters)),
            ("starts", tuple(item.start_public_id for item in self.starts)),
            ("resources", tuple(item.resource_public_id for item in self.resources)),
            ("structures", tuple(item.structure_public_id for item in self.structures)),
            ("samples", tuple(item.sample_public_id for item in self.samples)),
            ("orders", tuple(item.order_public_id for item in self.orders)),
            ("routes", tuple(item.segment_public_id for item in self.routes)),
            ("engagements", tuple(item.engagement_public_id for item in self.engagements)),
            ("casualties", tuple(item.casualty_public_id for item in self.casualties)),
            ("control_windows", tuple(item.control_window_public_id for item in self.control_windows)),
            ("visibility_transitions", tuple(item.visibility_public_id for item in self.visibility_transitions)),
            (
                "visibility_sampling_summaries",
                tuple(item.summary_public_id for item in self.visibility_sampling_summaries),
            ),
            (
                "engine_heuristic_overlays",
                tuple(item.overlay_public_id for item in self.engine_heuristic_overlays),
            ),
        )
        for field_name, identities in semantic_identities:
            if len(identities) != len(set(identities)):
                raise ValueError(f"{field_name} must contain unique semantic identities")
        object.__setattr__(self, "rasters", tuple(sorted(self.rasters, key=lambda item: item.raster_public_id)))
        object.__setattr__(self, "starts", tuple(sorted(self.starts, key=lambda item: item.start_public_id)))
        object.__setattr__(self, "resources", tuple(sorted(self.resources, key=lambda item: item.resource_public_id)))
        object.__setattr__(
            self, "structures", tuple(sorted(self.structures, key=lambda item: item.structure_public_id))
        )
        object.__setattr__(
            self,
            "samples",
            tuple(sorted(self.samples, key=lambda item: (item.frame, item.entity_public_id, item.sample_public_id))),
        )
        object.__setattr__(
            self, "orders", tuple(sorted(self.orders, key=lambda item: (item.frame, item.order_public_id)))
        )
        object.__setattr__(
            self,
            "routes",
            tuple(
                sorted(
                    self.routes,
                    key=lambda item: (item.frame_start, item.entity_public_id, item.segment_public_id),
                )
            ),
        )
        object.__setattr__(
            self,
            "engagements",
            tuple(sorted(self.engagements, key=lambda item: (item.frame_start, item.engagement_public_id))),
        )
        object.__setattr__(
            self,
            "casualties",
            tuple(sorted(self.casualties, key=lambda item: (item.frame, item.casualty_public_id))),
        )
        object.__setattr__(
            self,
            "control_windows",
            tuple(
                sorted(
                    self.control_windows,
                    key=lambda item: (item.frame_window.frame_start, item.control_window_public_id),
                )
            ),
        )
        object.__setattr__(
            self,
            "visibility_transitions",
            tuple(
                sorted(
                    self.visibility_transitions,
                    key=lambda item: (item.frame, item.replay_player_public_id, item.entity_public_id),
                )
            ),
        )
        object.__setattr__(
            self,
            "visibility_sampling_summaries",
            tuple(
                sorted(
                    self.visibility_sampling_summaries,
                    key=lambda item: (item.frame, item.sampling_cycle_id, item.summary_public_id),
                )
            ),
        )
        object.__setattr__(
            self,
            "engine_heuristic_overlays",
            tuple(
                sorted(
                    self.engine_heuristic_overlays,
                    key=lambda item: (item.frame, item.replay_player_public_id, item.overlay_public_id),
                )
            ),
        )
        return self


# TheSuperHackers @feature Leex 23/08/2026 Isolate fixed map-scene reads behind one immutable fakeable capability. (#TBD)
@runtime_checkable
class MapSceneQueryPort(Protocol):
    def list_scenes(self, query: MapSceneIndexQueryDTO) -> MapSceneIndexPageDTO: ...

    def get_scene(self, query: MapSceneQueryDTO) -> MapSceneDTO: ...

    def get_raster(self, query: MapRasterQueryDTO) -> MapRasterResourceDTO: ...


ComparisonState = Literal["comparable", "partial", "not_comparable", "unavailable"]
ComparisonKind = Literal["players", "matches", "openings", "strategies", "time_periods"]
IdentityOperationKind = Literal["merge_players", "split_alias", "inverse"]
# TheSuperHackers @fix Leex 23/08/2026 Keep the read-only audit vocabulary aligned with durable identity records. (#TBD)
IdentityAuditOperationKind = Literal["auto_link", "merge_players", "split_alias", "attach_external_alias", "inverse"]


def _require_utc_datetime(value: datetime | None, *, label: str) -> datetime | None:
    if value is not None and value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must use UTC")
    return value


def _freeze_optional_canonical_value(value: object) -> object:
    if value is None:
        return None
    return freeze_report_value(value)


class PublicEvidenceReferenceDTO(WebDTO):
    evidence_public_id: PublicId
    tier: ReportTier


class FixedReportReferenceDTO(WebDTO):
    replay_public_id: PublicId
    replay_player_public_id: PublicId
    report_public_id: PublicId
    document_schema_version: str = Field(min_length=1, max_length=128)
    report_version: str = Field(min_length=1, max_length=128)
    display_policy_version: str = Field(min_length=1, max_length=128)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class LongitudinalBindingDTO(WebDTO):
    run_id: PublicId
    player_public_id: PublicId
    identity_revision: int = Field(ge=0)
    analyzer_name: str = Field(min_length=1, max_length=256)
    analyzer_version: str = Field(min_length=1, max_length=128)
    segment_schema_version: Literal["longitudinal-segment-v1"]
    segment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    cache_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    statistics_algorithm_versions: tuple[str, ...] = Field(min_length=1)

    @field_validator("statistics_algorithm_versions")
    @classmethod
    def _canonical_algorithms(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value) > 128 for value in values):
            raise ValueError("statistics algorithms must be nonempty and bounded")
        return tuple(sorted(set(values)))


class DefinitionBindingDTO(WebDTO):
    definition_kind: Literal["feature", "opening", "strategy", "trend", "match_metric"]
    definition_id: str = Field(min_length=1, max_length=256)
    definition_version: str = Field(min_length=1, max_length=128)
    unit: str | None = Field(default=None, min_length=1, max_length=64)
    scope_type: str = Field(min_length=1, max_length=128)
    window_policy_version: str = Field(min_length=1, max_length=128)
    faction_comparability: Literal["same_faction_only", "declared_cross_faction"]
    taxonomy_version: str | None = Field(default=None, min_length=1, max_length=128)


# TheSuperHackers @feature Leex 23/08/2026 Freeze player history around exact identity, report, and longitudinal bindings. (#TBD)
class PlayerIndexQueryDTO(WebDTO):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)
    search: str | None = Field(default=None, max_length=256)
    faction: str | None = Field(default=None, max_length=64)
    opponent_faction: str | None = Field(default=None, max_length=64)
    map_public_id: PublicId | None = None
    patch: str | None = Field(default=None, max_length=64)
    active_only: bool = True
    sort: Literal["display_name", "recent_match", "match_count"] = "display_name"

    @field_validator("search", "faction", "opponent_faction", "patch")
    @classmethod
    def _normalize_player_filter(cls, value: str | None) -> str | None:
        return None if value is None or not value.strip() else value.strip()


class PlayerSummaryDTO(WebDTO):
    player_public_id: PublicId
    display_name: str = Field(min_length=1, max_length=256)
    identity_revision: int = Field(ge=0)
    state: Literal["active", "retired"]
    match_count: int = Field(ge=0)
    latest_match_at_utc: AwareDatetime | None = None
    availability: AvailabilityDTO

    @field_validator("latest_match_at_utc")
    @classmethod
    def _utc_latest_match(cls, value: datetime | None) -> datetime | None:
        return _require_utc_datetime(value, label="latest match")


class PlayerIndexPageDTO(WebDTO):
    query: PlayerIndexQueryDTO
    items: tuple[PlayerSummaryDTO, ...]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total_items: int = Field(ge=0)
    availability: AvailabilityDTO

    @model_validator(mode="after")
    def _match_player_page(self) -> Self:
        if (self.page, self.page_size) != (self.query.page, self.query.page_size):
            raise ValueError("player page must match its fixed query")
        return self


class PlayerProfileSelectionDTO(WebDTO):
    player_public_id: PublicId
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)
    faction: str | None = Field(default=None, min_length=1, max_length=64)
    opponent_faction: str | None = Field(default=None, min_length=1, max_length=64)
    opponent_player_public_id: PublicId | None = None
    map_public_id: PublicId | None = None
    patch: str | None = Field(default=None, min_length=1, max_length=64)
    result: str | None = Field(default=None, min_length=1, max_length=64)
    start_position: str | None = Field(default=None, min_length=1, max_length=64)
    date_from_utc: AwareDatetime | None = None
    date_to_utc: AwareDatetime | None = None
    quality_policy_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("date_from_utc", "date_to_utc")
    @classmethod
    def _utc_profile_filter(cls, value: datetime | None) -> datetime | None:
        return _require_utc_datetime(value, label="profile filter")

    @model_validator(mode="after")
    def _ordered_profile_dates(self) -> Self:
        if self.date_from_utc is not None and self.date_to_utc is not None and self.date_from_utc >= self.date_to_utc:
            raise ValueError("profile UTC interval must be nonempty")
        return self


class PlayerProfileQueryDTO(PlayerProfileSelectionDTO):
    expected_identity_revision: int = Field(ge=0)
    longitudinal_run_ids: tuple[PublicId, ...]
    report_public_ids: tuple[PublicId, ...]
    definition_binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _canonical_profile_bindings(self) -> Self:
        object.__setattr__(self, "longitudinal_run_ids", tuple(sorted(set(self.longitudinal_run_ids))))
        object.__setattr__(self, "report_public_ids", tuple(sorted(set(self.report_public_ids))))
        return self


class PlayerProfileResolutionDTO(WebDTO):
    state: Literal["resolved", "unavailable"]
    fixed_query: PlayerProfileQueryDTO | None = None
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid_profile_resolution(self) -> Self:
        if self.state == "resolved" and (self.fixed_query is None or self.reason_codes):
            raise ValueError("resolved profile requires only one fixed query")
        if self.state == "unavailable" and (self.fixed_query is not None or not self.reason_codes):
            raise ValueError("unavailable profile requires reasons and no query")
        return self


class EmbeddedAliasDTO(WebDTO):
    alias_public_id: PublicId
    namespace: Literal["embedded_replay_name"]
    original_name: str = Field(min_length=1, max_length=256)
    normalized_name: str = Field(min_length=1, max_length=256)


class ProviderIdentityDTO(WebDTO):
    alias_public_id: PublicId
    provider_namespace: str = Field(min_length=1, max_length=128)
    external_subject: str = Field(min_length=1, max_length=256)
    attachment_operation_public_id: PublicId
    label: Literal["manually_attached_provider_identity"]


class StrataProvenanceDTO(WebDTO):
    source_public_id: PublicId
    replay_public_id: PublicId
    strata_match_id: str | None = Field(default=None, min_length=1, max_length=128)
    strata_source_user_token: str | None = Field(default=None, min_length=1, max_length=128)
    label: Literal["provenance_not_identity"]
    availability: AvailabilityDTO


class ReplayHistoryItemDTO(WebDTO):
    replay_public_id: PublicId
    replay_player_public_id: PublicId
    observed_name: str = Field(min_length=1, max_length=256)
    faction: str | None = Field(default=None, min_length=1, max_length=64)
    opponent_factions: tuple[str, ...]
    opponent_player_public_ids: tuple[PublicId, ...]
    map_public_id: PublicId | None = None
    map_display_name: str | None = Field(default=None, min_length=1, max_length=256)
    patch: str | None = Field(default=None, min_length=1, max_length=64)
    start_position: str | None = Field(default=None, min_length=1, max_length=64)
    result: str | None = Field(default=None, min_length=1, max_length=64)
    started_at_utc: AwareDatetime | None = None
    terminal_quality: TerminalQualityDTO
    fixed_report: FixedReportReferenceDTO | None = None
    availability: AvailabilityDTO

    @field_validator("started_at_utc")
    @classmethod
    def _utc_history_start(cls, value: datetime | None) -> datetime | None:
        return _require_utc_datetime(value, label="replay start")


class DistributionIntervalDTO(WebDTO):
    lower: float
    upper: float
    confidence_level: float = Field(gt=0, lt=1)
    method: str = Field(min_length=1, max_length=128)
    algorithm_version: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _finite_ordered_interval(self) -> Self:
        if any(
            not math.isfinite(value) or value == 0.0 and math.copysign(1.0, value) < 0
            for value in (self.lower, self.upper, self.confidence_level)
        ):
            raise ValueError("distribution interval must be finite without negative zero")
        if self.lower > self.upper:
            raise ValueError("distribution interval must be ordered")
        return self


class PlayerInsightDTO(WebDTO):
    insight_kind: Literal[
        "recurring_opening",
        "timing_distribution",
        "transition_preference",
        "spatial_habit",
        "personal_baseline_deviation",
        "opponent_associated_difference",
        "trend",
        "change_point_candidate",
        "consistency",
    ]
    result_public_id: PublicId
    definition: DefinitionBindingDTO
    label: str = Field(min_length=1, max_length=256)
    raw_value: object | None
    unit: str | None = Field(default=None, min_length=1, max_length=64)
    frame_start: int | None = Field(default=None, ge=0)
    frame_end: int | None = Field(default=None, ge=0)
    sample_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    interval: DistributionIntervalDTO | None = None
    quality_exclusion_codes: tuple[str, ...] = ()
    availability: AvailabilityDTO
    evidence: tuple[PublicEvidenceReferenceDTO, ...] = ()

    @field_validator("raw_value", mode="before")
    @classmethod
    def _canonical_player_value(cls, value: object) -> object:
        return _freeze_optional_canonical_value(value)

    @model_validator(mode="after")
    def _valid_player_insight(self) -> Self:
        if (self.frame_start is None) != (self.frame_end is None):
            raise ValueError("insight frame window must be complete and ordered")
        if self.frame_start is not None and self.frame_end is not None and self.frame_end < self.frame_start:
            raise ValueError("insight frame window must be complete and ordered")
        if self.availability.state == "unavailable" and self.raw_value is not None:
            raise ValueError("unavailable insight cannot expose a value")
        object.__setattr__(self, "quality_exclusion_codes", tuple(sorted(set(self.quality_exclusion_codes))))
        object.__setattr__(
            self, "evidence", tuple(sorted(self.evidence, key=lambda item: (item.tier, item.evidence_public_id)))
        )
        return self


class PlayerProfileVersionDTO(WebDTO):
    schema_version: Literal["replay-player-profile-v1"]
    display_policy_version: Literal["replay-player-profile-display-v1"]
    profile_public_id: PublicId
    player_public_id: PublicId
    identity_revision: int = Field(ge=0)
    longitudinal: tuple[LongitudinalBindingDTO, ...]
    fixed_reports: tuple[FixedReportReferenceDTO, ...]
    definition_bindings: tuple[DefinitionBindingDTO, ...]
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PlayerProfileDTO(WebDTO):
    version: PlayerProfileVersionDTO
    query: PlayerProfileQueryDTO
    player: PlayerSummaryDTO
    embedded_aliases: tuple[EmbeddedAliasDTO, ...]
    provider_identities: tuple[ProviderIdentityDTO, ...]
    strata_provenance: tuple[StrataProvenanceDTO, ...]
    replay_history: tuple[ReplayHistoryItemDTO, ...]
    history_page: int = Field(ge=1)
    history_page_size: int = Field(ge=1, le=100)
    history_total_items: int = Field(ge=0)
    insights: tuple[PlayerInsightDTO, ...]
    availability: AvailabilityDTO

    @model_validator(mode="after")
    def _valid_profile_graph(self) -> Self:
        identity = (self.query.player_public_id, self.query.expected_identity_revision)
        if (
            identity != (self.version.player_public_id, self.version.identity_revision)
            or self.player.player_public_id != identity[0]
        ):
            raise ValueError("player profile identity must match its fixed query")
        if (self.history_page, self.history_page_size) != (self.query.page, self.query.page_size):
            raise ValueError("player history page must match its fixed query")
        if tuple(item.run_id for item in self.version.longitudinal) != self.query.longitudinal_run_ids:
            raise ValueError("profile longitudinal bindings must match its fixed query")
        if tuple(item.report_public_id for item in self.version.fixed_reports) != self.query.report_public_ids:
            raise ValueError("profile report bindings must match its fixed query")
        object.__setattr__(
            self,
            "embedded_aliases",
            tuple(
                sorted(
                    self.embedded_aliases,
                    key=lambda item: (item.normalized_name, item.original_name, item.alias_public_id),
                )
            ),
        )
        object.__setattr__(
            self,
            "provider_identities",
            tuple(
                sorted(
                    self.provider_identities,
                    key=lambda item: (item.provider_namespace, item.external_subject, item.alias_public_id),
                )
            ),
        )
        object.__setattr__(
            self,
            "strata_provenance",
            tuple(sorted(self.strata_provenance, key=lambda item: (item.replay_public_id, item.source_public_id))),
        )
        return self


@runtime_checkable
class PlayerHistoryPort(Protocol):
    def list_players(self, query: PlayerIndexQueryDTO) -> PlayerIndexPageDTO: ...
    def resolve_profile(self, selection: PlayerProfileSelectionDTO) -> PlayerProfileResolutionDTO: ...
    def get_profile(self, query: PlayerProfileQueryDTO) -> PlayerProfileDTO: ...


class RevisionPreconditionDTO(WebDTO):
    player_public_id: PublicId
    expected_revision: int = Field(ge=0)


class MergeIdentityDraftDTO(WebDTO):
    operation_kind: Literal["merge_players"]
    target_player_public_id: PublicId
    source_player_public_ids: tuple[PublicId, ...] = Field(min_length=1)
    expected_revisions: tuple[RevisionPreconditionDTO, ...] = Field(min_length=2)


class SplitIdentityDraftDTO(WebDTO):
    operation_kind: Literal["split_alias"]
    alias_public_id: PublicId
    replay_player_public_ids: tuple[PublicId, ...] = Field(min_length=1)
    new_display_name: str = Field(min_length=1, max_length=256)
    expected_revisions: tuple[RevisionPreconditionDTO, ...] = Field(min_length=1)


class InverseIdentityDraftDTO(WebDTO):
    operation_kind: Literal["inverse"]
    operation_public_id: PublicId
    expected_revisions: tuple[RevisionPreconditionDTO, ...] = Field(min_length=1)


IdentityDraftDTO = Annotated[
    MergeIdentityDraftDTO | SplitIdentityDraftDTO | InverseIdentityDraftDTO,
    Field(discriminator="operation_kind"),
]


class IdentityImpactDTO(WebDTO):
    canonical_player_count: int = Field(ge=0)
    alias_count: int = Field(ge=0)
    replay_player_count: int = Field(ge=0)
    replay_count: int = Field(ge=0)
    feature_set_count: int = Field(ge=0)
    longitudinal_run_count: int = Field(ge=0)
    longitudinal_result_count: int = Field(ge=0)
    report_count: int = Field(ge=0)
    invalidation_stage_counts: tuple[tuple[str, int], ...]


class IdentityPreviewDTO(WebDTO):
    schema_version: Literal["player-identity-preview-v1"]
    draft: IdentityDraftDTO
    before_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_after_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    impact: IdentityImpactDTO
    can_execute: bool
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid_preview_state(self) -> Self:
        if self.can_execute == bool(self.reason_codes):
            raise ValueError("identity preview execute state and reasons are inconsistent")
        return self


class ExecuteIdentityChangeDTO(WebDTO):
    draft: IdentityDraftDTO
    expected_before_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    operator_label: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1024)


class InvalidationJobReferenceDTO(WebDTO):
    job_public_id: PublicId
    stage: str = Field(min_length=1, max_length=128)
    state: Literal["pending", "already_queued", "durable_retry_required"]
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)


class IdentityOperationSummaryDTO(WebDTO):
    operation_public_id: PublicId
    operation_kind: IdentityAuditOperationKind
    inverse_of_operation_public_id: PublicId | None = None
    operator_label: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1024)
    created_at_utc: AwareDatetime
    affected_player_revisions: tuple[RevisionPreconditionDTO, ...]
    inverse_allowed: bool
    inverse_reason_code: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("created_at_utc")
    @classmethod
    def _utc_identity_operation(cls, value: datetime) -> datetime:
        result = _require_utc_datetime(value, label="identity operation time")
        assert result is not None
        return result


class IdentityMutationReceiptDTO(WebDTO):
    operation: IdentityOperationSummaryDTO
    invalidation_jobs: tuple[InvalidationJobReferenceDTO, ...] = Field(min_length=1)
    audit_public_id: PublicId


class IdentityAuditPageDTO(WebDTO):
    player_public_id: PublicId
    current_identity_revision: int = Field(ge=0)
    operations: tuple[IdentityOperationSummaryDTO, ...]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total_items: int = Field(ge=0)
    availability: AvailabilityDTO


@runtime_checkable
class PlayerIdentityWorkflowPort(Protocol):
    def audit(self, player_public_id: str, page: int, page_size: int) -> IdentityAuditPageDTO: ...
    def preview(self, draft: IdentityDraftDTO) -> IdentityPreviewDTO: ...
    def execute(self, command: ExecuteIdentityChangeDTO) -> IdentityMutationReceiptDTO: ...
    def retry_invalidation(self, operation_public_id: str) -> tuple[InvalidationJobReferenceDTO, ...]: ...


class PlayerCohortSubjectDTO(WebDTO):
    subject_kind: Literal["player_cohort"]
    player_public_id: PublicId
    identity_revision: int = Field(ge=0)
    longitudinal: LongitudinalBindingDTO

    @model_validator(mode="after")
    def _valid_longitudinal_identity(self) -> Self:
        if (self.player_public_id, self.identity_revision) != (
            self.longitudinal.player_public_id,
            self.longitudinal.identity_revision,
        ):
            raise ValueError("subject identity must match its longitudinal binding")
        return self


class SegmentBaselineSubjectDTO(WebDTO):
    subject_kind: Literal["segment_baseline"]
    baseline_public_id: PublicId
    longitudinal: LongitudinalBindingDTO
    population_definition_version: str = Field(min_length=1, max_length=128)


class MatchSubjectDTO(WebDTO):
    subject_kind: Literal["match"]
    report: FixedReportReferenceDTO
    feature_set_public_ids: tuple[PublicId, ...]


class OpeningSubjectDTO(WebDTO):
    subject_kind: Literal["opening"]
    player_public_id: PublicId
    identity_revision: int = Field(ge=0)
    longitudinal: LongitudinalBindingDTO
    result_public_id: PublicId
    opening_definition_id: str = Field(min_length=1, max_length=256)
    opening_definition_version: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _valid_longitudinal_identity(self) -> Self:
        if (self.player_public_id, self.identity_revision) != (
            self.longitudinal.player_public_id,
            self.longitudinal.identity_revision,
        ):
            raise ValueError("subject identity must match its longitudinal binding")
        return self


class StrategySubjectDTO(WebDTO):
    subject_kind: Literal["strategy"]
    player_public_id: PublicId
    identity_revision: int = Field(ge=0)
    longitudinal: LongitudinalBindingDTO
    result_public_id: PublicId
    strategy_id: str = Field(min_length=1, max_length=256)
    taxonomy_version: str = Field(min_length=1, max_length=128)
    rule_version: str = Field(min_length=1, max_length=128)
    method: Literal["rule"]

    @model_validator(mode="after")
    def _valid_longitudinal_identity(self) -> Self:
        if (self.player_public_id, self.identity_revision) != (
            self.longitudinal.player_public_id,
            self.longitudinal.identity_revision,
        ):
            raise ValueError("subject identity must match its longitudinal binding")
        return self


class TimePeriodSubjectDTO(WebDTO):
    subject_kind: Literal["time_period"]
    player_public_id: PublicId
    identity_revision: int = Field(ge=0)
    start_inclusive_utc: AwareDatetime
    end_exclusive_utc: AwareDatetime
    longitudinal: LongitudinalBindingDTO

    @model_validator(mode="after")
    def _valid_utc_period(self) -> Self:
        if (self.player_public_id, self.identity_revision) != (
            self.longitudinal.player_public_id,
            self.longitudinal.identity_revision,
        ):
            raise ValueError("subject identity must match its longitudinal binding")
        start = _require_utc_datetime(self.start_inclusive_utc, label="period start")
        end = _require_utc_datetime(self.end_exclusive_utc, label="period end")
        if start is None or end is None or start >= end:
            raise ValueError("time period must be a nonempty UTC interval")
        return self


ComparisonSubjectDTO = Annotated[
    PlayerCohortSubjectDTO
    | SegmentBaselineSubjectDTO
    | MatchSubjectDTO
    | OpeningSubjectDTO
    | StrategySubjectDTO
    | TimePeriodSubjectDTO,
    Field(discriminator="subject_kind"),
]


class FixedComparisonQueryDTO(WebDTO):
    schema_version: Literal["replay-comparison-query-v1"]
    kind: ComparisonKind
    left: ComparisonSubjectDTO
    right: ComparisonSubjectDTO
    metric_definition_ids: tuple[str, ...] = Field(min_length=1)
    definition_bindings: tuple[DefinitionBindingDTO, ...] = Field(min_length=1)
    minimum_sample_size: int = Field(ge=1)

    @model_validator(mode="after")
    def _valid_comparison_subjects(self) -> Self:
        valid = {
            "players": isinstance(self.left, PlayerCohortSubjectDTO)
            and isinstance(self.right, (PlayerCohortSubjectDTO, SegmentBaselineSubjectDTO)),
            "matches": isinstance(self.left, MatchSubjectDTO) and isinstance(self.right, MatchSubjectDTO),
            "openings": isinstance(self.left, OpeningSubjectDTO) and isinstance(self.right, OpeningSubjectDTO),
            "strategies": isinstance(self.left, StrategySubjectDTO) and isinstance(self.right, StrategySubjectDTO),
            "time_periods": isinstance(self.left, TimePeriodSubjectDTO)
            and isinstance(self.right, TimePeriodSubjectDTO),
        }[self.kind]
        if not valid:
            raise ValueError("comparison subjects do not match the selected kind")
        if isinstance(self.left, TimePeriodSubjectDTO) and isinstance(self.right, TimePeriodSubjectDTO):
            if (self.left.player_public_id, self.left.identity_revision) != (
                self.right.player_public_id,
                self.right.identity_revision,
            ):
                raise ValueError("time periods must bind one player revision")
            if not (
                self.left.end_exclusive_utc <= self.right.start_inclusive_utc
                or self.right.end_exclusive_utc <= self.left.start_inclusive_utc
            ):
                raise ValueError("time periods must be disjoint")
        object.__setattr__(self, "metric_definition_ids", tuple(sorted(set(self.metric_definition_ids))))
        return self


class ComparisonValueDTO(WebDTO):
    raw_value: object | None
    unit: str | None = Field(default=None, min_length=1, max_length=64)
    sample_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    interval: DistributionIntervalDTO | None = None
    quality_exclusion_codes: tuple[str, ...] = ()
    availability: AvailabilityDTO
    evidence: tuple[PublicEvidenceReferenceDTO, ...] = ()

    @field_validator("raw_value", mode="before")
    @classmethod
    def _canonical_comparison_value(cls, value: object) -> object:
        return _freeze_optional_canonical_value(value)

    @model_validator(mode="after")
    def _valid_comparison_value(self) -> Self:
        if self.availability.state == "unavailable" and self.raw_value is not None:
            raise ValueError("unavailable comparison value cannot expose raw data")
        object.__setattr__(self, "quality_exclusion_codes", tuple(sorted(set(self.quality_exclusion_codes))))
        object.__setattr__(
            self, "evidence", tuple(sorted(self.evidence, key=lambda item: (item.tier, item.evidence_public_id)))
        )
        return self


class ComparisonMetricDTO(WebDTO):
    section: Literal["overview", "openings", "timings", "transitions", "strategy", "spatial", "trend"]
    metric_id: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1, max_length=256)
    value_kind: Literal["scalar", "distribution", "categorical_share", "timing_band", "transition", "trend"]
    definition: DefinitionBindingDTO
    left: ComparisonValueDTO
    right: ComparisonValueDTO
    derived_difference: object | None
    difference_evidence: tuple[PublicEvidenceReferenceDTO, ...] = ()
    state: ComparisonState
    reason_codes: tuple[str, ...] = ()

    @field_validator("derived_difference", mode="before")
    @classmethod
    def _canonical_difference(cls, value: object) -> object:
        return _freeze_optional_canonical_value(value)

    @model_validator(mode="after")
    def _valid_metric_state(self) -> Self:
        if self.state in {"not_comparable", "unavailable"} and (
            self.derived_difference is not None or self.difference_evidence
        ):
            raise ValueError("unaligned comparison metric cannot expose a difference")
        object.__setattr__(
            self,
            "difference_evidence",
            tuple(sorted(self.difference_evidence, key=lambda item: (item.tier, item.evidence_public_id))),
        )
        return self


class ComparisonVersionDTO(WebDTO):
    schema_version: Literal["replay-comparison-v1"]
    comparison_definition_version: Literal["replay-comparison-definition-v1"]
    display_policy_version: Literal["replay-comparison-display-v1"]
    comparison_public_id: PublicId
    query_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_bindings: tuple[tuple[PublicId, int], ...]
    longitudinal_bindings: tuple[LongitudinalBindingDTO, ...]
    report_bindings: tuple[FixedReportReferenceDTO, ...]
    definition_bindings: tuple[DefinitionBindingDTO, ...]


class ComparisonDTO(WebDTO):
    version: ComparisonVersionDTO
    query: FixedComparisonQueryDTO
    state: ComparisonState
    reason_codes: tuple[str, ...]
    metrics: tuple[ComparisonMetricDTO, ...]
    availability: AvailabilityDTO


class ComparisonFiltersDTO(WebDTO):
    faction: str | None = Field(default=None, min_length=1, max_length=64)
    subfaction: str | None = Field(default=None, min_length=1, max_length=64)
    opponent_faction: str | None = Field(default=None, min_length=1, max_length=64)
    opponent_player_public_id: PublicId | None = None
    map_public_id: PublicId | None = None
    start_position: str | None = Field(default=None, min_length=1, max_length=64)
    patch: str | None = Field(default=None, min_length=1, max_length=64)
    date_from_utc: AwareDatetime | None = None
    date_to_utc: AwareDatetime | None = None
    quality_policy_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ComparisonSelectionDTO(WebDTO):
    kind: ComparisonKind
    left_public_id: PublicId | None
    right_public_id: PublicId | None
    baseline_requested: bool
    metric_definition_ids: tuple[str, ...]
    filters: ComparisonFiltersDTO


class ComparisonResolutionDTO(WebDTO):
    state: Literal["resolved", "unavailable"]
    fixed_query: FixedComparisonQueryDTO | None = None
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid_comparison_resolution(self) -> Self:
        if self.state == "resolved" and (self.fixed_query is None or self.reason_codes):
            raise ValueError("resolved comparison requires one fixed query")
        if self.state == "unavailable" and (self.fixed_query is not None or not self.reason_codes):
            raise ValueError("unavailable comparison requires reasons")
        return self


@runtime_checkable
class ComparisonQueryPort(Protocol):
    def resolve(self, selection: ComparisonSelectionDTO) -> ComparisonResolutionDTO: ...
    def compare(self, query: FixedComparisonQueryDTO) -> ComparisonDTO: ...


EditableSettingKey = Literal[
    "ollama_url",
    "ollama_model",
    "movement_sample_frames",
    "import_mode",
    "minimum_longitudinal_sample_size",
]
SettingsSource = Literal["default", "persisted", "environment"]
DiagnosticKind = Literal[
    "data_root_writable",
    "sqlite_integrity",
    "engine_launch_version",
    "ollama_model_available",
    "ollama_minimal_generation",
]
AffectedStageFamily = Literal[
    "future_imports",
    "telemetry",
    "spatial",
    "features",
    "strategy",
    "longitudinal",
    "ollama",
    "report",
]
_EDITABLE_SETTING_KEYS: tuple[EditableSettingKey, ...] = (
    "import_mode",
    "minimum_longitudinal_sample_size",
    "movement_sample_frames",
    "ollama_model",
    "ollama_url",
)
_AFFECTED_STAGE_ORDER: tuple[AffectedStageFamily, ...] = (
    "future_imports",
    "telemetry",
    "spatial",
    "features",
    "strategy",
    "longitudinal",
    "ollama",
    "report",
)


def _validated_setting_value(key: EditableSettingKey, value: StrictInt | str) -> StrictInt | str:
    if key == "movement_sample_frames":
        if type(value) is not int or not 1 <= value <= 3600:
            raise ValueError("movement_sample_frames must be a strict integer from 1 through 3600")
        return value
    if key == "minimum_longitudinal_sample_size":
        if type(value) is not int or not 1 <= value <= 100_000:
            raise ValueError("minimum_longitudinal_sample_size must be a strict integer from 1 through 100000")
        return value
    if key == "import_mode":
        if type(value) is not str or value not in {"copy", "reference"}:
            raise ValueError("import_mode must use the closed copy or reference policy")
        return value
    if key == "ollama_url":
        try:
            return normalize_ollama_endpoint(value)
        except SettingsStoreError:
            raise ValueError("ollama_url must be an explicit literal loopback endpoint") from None
    if key == "ollama_model":
        try:
            return validate_ollama_model_name(value)
        except SettingsStoreError:
            raise ValueError("ollama_model must use the closed local model identifier grammar") from None
    raise ValueError("unknown editable setting key")


class SettingValueDTO(WebDTO):
    key: EditableSettingKey
    value: StrictInt | str
    source: SettingsSource
    editable: bool
    unavailable_reason_code: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _valid_effective_setting(self) -> Self:
        object.__setattr__(self, "value", _validated_setting_value(self.key, self.value))
        if self.source == "environment" and (self.editable or self.unavailable_reason_code is None):
            raise ValueError("environment-owned settings must be read-only with a stable reason")
        if self.editable and self.unavailable_reason_code is not None:
            raise ValueError("editable settings cannot have an unavailable reason")
        return self


class RedactedLocationDTO(WebDTO):
    kind: Literal[
        "data_root",
        "database",
        "managed_replays",
        "map_cache",
        "cache",
        "logs",
        "engine_executable",
        "game_data",
        "watched_folder",
    ]
    public_id: PublicId | None
    label: str = Field(min_length=1, max_length=256)
    configured: bool
    location_class: Literal["platform_default", "custom_external", "not_configured"]
    basename: Literal["generalszh.exe", "replay-analyzer.sqlite3"] | None
    availability: AvailabilityDTO

    @model_validator(mode="after")
    def _valid_redacted_location(self) -> Self:
        if self.configured == (self.location_class == "not_configured"):
            raise ValueError("configured state and location class must agree")
        if self.kind == "watched_folder" and self.public_id is None:
            raise ValueError("watched folders require an opaque public identity")
        if self.kind != "watched_folder" and self.public_id is not None:
            raise ValueError("only repeated watched folders use a public identity")
        if self.basename == "generalszh.exe" and self.kind != "engine_executable":
            raise ValueError("engine basename can only describe the configured engine")
        if self.basename == "replay-analyzer.sqlite3" and self.kind != "database":
            raise ValueError("database basename can only describe the configured database")
        return self


class ComponentIdentityDTO(WebDTO):
    component: Literal[
        "analyzer",
        "parser",
        "telemetry_schema",
        "exporter",
        "message_catalog",
        "game_data_catalog",
        "map_asset",
        "database_schema",
    ]
    version: str | None = Field(default=None, min_length=1, max_length=255)
    content_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    build_identity: str | None = Field(default=None, min_length=1, max_length=255)
    availability: AvailabilityDTO


class ModelIdentityDTO(WebDTO):
    provider: Literal["ollama"]
    endpoint: str
    model_name: str
    configured_model_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    discovered_model_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    availability: AvailabilityDTO

    @model_validator(mode="after")
    def _valid_model_identity(self) -> Self:
        try:
            endpoint = normalize_ollama_endpoint(self.endpoint)
            model_name = validate_ollama_model_name(self.model_name)
        except SettingsStoreError:
            raise ValueError("model identity must use the closed local Ollama policy") from None
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "model_name", model_name)
        return self


class SettingsSnapshotDTO(WebDTO):
    schema_version: Literal[1]
    revision: int = Field(ge=0)
    effective_settings_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    values: tuple[SettingValueDTO, ...]
    locations: tuple[RedactedLocationDTO, ...]
    components: tuple[ComponentIdentityDTO, ...]
    model: ModelIdentityDTO
    restart_required: bool
    availability: AvailabilityDTO

    @model_validator(mode="after")
    def _canonical_settings_snapshot(self) -> Self:
        values = {value.key: value for value in self.values}
        if len(values) != len(self.values) or set(values) != set(_EDITABLE_SETTING_KEYS):
            raise ValueError("settings snapshot requires exactly one of every safe scalar")
        components = {component.component: component for component in self.components}
        if len(components) != len(self.components):
            raise ValueError("component identities must be unique")
        location_identities = tuple((location.kind, location.public_id or "") for location in self.locations)
        if len(location_identities) != len(set(location_identities)):
            raise ValueError("redacted location identities must be unique")
        object.__setattr__(self, "values", tuple(values[key] for key in _EDITABLE_SETTING_KEYS))
        object.__setattr__(self, "components", tuple(components[key] for key in sorted(components)))
        object.__setattr__(
            self,
            "locations",
            tuple(sorted(self.locations, key=lambda item: (item.kind, item.public_id or "", item.label))),
        )
        return self


class SettingChangeDTO(WebDTO):
    key: EditableSettingKey
    value: StrictInt | str

    @model_validator(mode="after")
    def _valid_setting_change(self) -> Self:
        object.__setattr__(self, "value", _validated_setting_value(self.key, self.value))
        return self


def _canonical_changes(changes: tuple[SettingChangeDTO, ...]) -> tuple[SettingChangeDTO, ...]:
    if not changes:
        raise ValueError("settings commands require at least one change")
    by_key = {change.key: change for change in changes}
    if len(by_key) != len(changes):
        raise ValueError("settings command keys must be unique")
    return tuple(by_key[key] for key in _EDITABLE_SETTING_KEYS if key in by_key)


class SettingsPreviewCommandDTO(WebDTO):
    expected_revision: int = Field(ge=0)
    changes: tuple[SettingChangeDTO, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def _canonical_preview_changes(self) -> Self:
        object.__setattr__(self, "changes", _canonical_changes(self.changes))
        return self


class SettingsImpactDTO(WebDTO):
    expected_revision: int = Field(ge=0)
    impact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_changes: tuple[SettingChangeDTO, ...] = Field(min_length=1, max_length=5)
    affected_stage_families: tuple[AffectedStageFamily, ...] = Field(min_length=1, max_length=8)
    invalidates_existing_results: bool
    requires_confirmation: bool
    restart_required: bool
    messages: tuple[DiagnosticDTO, ...]

    @model_validator(mode="after")
    def _canonical_impact(self) -> Self:
        object.__setattr__(self, "normalized_changes", _canonical_changes(self.normalized_changes))
        families = set(self.affected_stage_families)
        if len(families) != len(self.affected_stage_families):
            raise ValueError("affected stage families must be unique")
        object.__setattr__(
            self,
            "affected_stage_families",
            tuple(family for family in _AFFECTED_STAGE_ORDER if family in families),
        )
        return self


class ApplySettingsCommandDTO(WebDTO):
    expected_revision: int = Field(ge=0)
    changes: tuple[SettingChangeDTO, ...] = Field(min_length=1, max_length=5)
    expected_impact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm_invalidating_change: Literal[True]

    @model_validator(mode="after")
    def _canonical_apply_changes(self) -> Self:
        object.__setattr__(self, "changes", _canonical_changes(self.changes))
        return self


class SettingsMutationDTO(WebDTO):
    result_code: Literal["updated", "unchanged"]
    snapshot: SettingsSnapshotDTO
    impact: SettingsImpactDTO
    analysis_jobs_queued: Literal[False]


class DiagnosticCommandDTO(WebDTO):
    kind: DiagnosticKind
    expected_settings_revision: int = Field(ge=0)


class DiagnosticResultDTO(WebDTO):
    kind: DiagnosticKind
    state: Literal["passed", "failed", "unavailable"]
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=MAX_DIAGNOSTIC_MESSAGE_LENGTH)
    settings_revision: int = Field(ge=0)
    component: ComponentIdentityDTO | None
    model: ModelIdentityDTO | None
    duration_milliseconds: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _valid_diagnostic_identity_family(self) -> Self:
        if self.component is not None and self.model is not None:
            raise ValueError("diagnostic result cannot expose component and model identities together")
        if self.kind.startswith("ollama_") and self.component is not None:
            raise ValueError("Ollama diagnostics cannot expose component identity")
        if not self.kind.startswith("ollama_") and self.model is not None:
            raise ValueError("deterministic diagnostics cannot expose model identity")
        return self


# TheSuperHackers @feature Leex 23/08/2026 Isolate settings and active diagnostics behind immutable path-free web contracts. (#TBD)
@runtime_checkable
class SettingsQueryPort(Protocol):
    def get_settings(self) -> SettingsSnapshotDTO: ...
    def preview_settings(self, command: SettingsPreviewCommandDTO) -> SettingsImpactDTO: ...


@runtime_checkable
class SettingsCommandPort(Protocol):
    def apply_settings(self, command: ApplySettingsCommandDTO) -> SettingsMutationDTO: ...


@runtime_checkable
class DiagnosticsCommandPort(Protocol):
    def run_diagnostic(self, command: DiagnosticCommandDTO) -> DiagnosticResultDTO: ...
