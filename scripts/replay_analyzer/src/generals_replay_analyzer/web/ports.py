"""Immutable, view-safe values accepted by replay-analyzer routes."""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Annotated, Literal, Protocol, Self, runtime_checkable
from urllib.parse import unquote, urlsplit
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.functional_validators import AfterValidator

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

    display_name: str = Field(min_length=1, max_length=256)
    slot: int = Field(ge=1, le=16)
    faction: str | None = Field(default=None, min_length=1, max_length=64)
    result: str | None = Field(default=None, min_length=1, max_length=64)


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
    analysis_status: Literal["discovered", "parsed", "engine_verified", "partial", "desynced", "unsupported", "failed"] | None = None
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
