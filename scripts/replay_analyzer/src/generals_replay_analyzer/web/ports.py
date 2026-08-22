"""Immutable, view-safe values accepted by replay-analyzer routes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Annotated, Literal, Protocol, Self
from urllib.parse import unquote, urlsplit
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.functional_validators import AfterValidator

from generals_replay_analyzer.ingress_contract import validate_replay_relative_name, validate_root_public_id


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
