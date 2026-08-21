"""Strict loader for the telemetry-independent replay outcome side channel."""

import json
import math
import os
import stat
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, model_validator

MAX_OUTCOME_BYTES = 64 * 1024
UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1


class ReplayOutcomeValidationError(ValueError):
    """Reject an incomplete, unsafe, or incoherent independent outcome."""


def _is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        getattr(info, "st_file_attributes", 0),
    )


def _duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate field {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard numeric constant {value}")


BoundedFrame = Annotated[StrictInt, Field(ge=0, le=UINT32_MAX)]
BoundedCommandCount = Annotated[StrictInt, Field(ge=0, le=UINT64_MAX)]


# TheSuperHackers @feature Leex 21/08/2026 Validate the independent replay outcome as a closed evidence contract. (#TBD)
class ReplayOutcome(BaseModel):
    """Closed version-one facts independently published by ReplayOutcome.cpp."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    playback_started: StrictBool
    final_frame: BoundedFrame
    command_count: BoundedCommandCount
    terminal_reason: Literal[
        "input_unavailable",
        "invalid_replay_header",
        "truncated_input",
        "clean_completion",
        "crc_mismatch",
        "replay_truncated",
        "interrupted",
    ]
    crc_mismatch: StrictBool
    crc_mismatch_frame: BoundedFrame | None

    @model_validator(mode="after")
    def _require_source_coherence(self) -> "ReplayOutcome":
        startup = {"input_unavailable", "invalid_replay_header", "truncated_input"}
        if self.terminal_reason in startup:
            if self.playback_started or self.final_frame != 0 or self.command_count != 0:
                raise ValueError("startup outcome must contain zero facts and playback_started=false")
        elif not self.playback_started:
            raise ValueError("playback terminal reason requires playback_started=true")
        if self.terminal_reason == "crc_mismatch":
            if not self.crc_mismatch or self.crc_mismatch_frame is None:
                raise ValueError("CRC terminal reason requires exact mismatch facts")
            if self.crc_mismatch_frame > self.final_frame:
                raise ValueError("CRC mismatch frame cannot exceed final playback frame")
        elif self.crc_mismatch or self.crc_mismatch_frame is not None:
            raise ValueError("non-CRC terminal reason cannot claim CRC mismatch facts")
        return self


def load_replay_outcome(path: Path) -> ReplayOutcome:
    """Read and validate one complete independent outcome without partial exposure."""
    try:
        info = path.lstat()
        if path.is_symlink() or _is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ReplayOutcomeValidationError("outcome must be one ordinary single-link file")
        if info.st_size <= 0 or info.st_size > MAX_OUTCOME_BYTES:
            raise ReplayOutcomeValidationError("outcome size is outside the closed bound")
        with path.open("rb") as outcome_file:
            opened = os.fstat(outcome_file.fileno())
            if _identity(opened) != _identity(info):
                raise ReplayOutcomeValidationError("outcome identity changed before it was opened")
            source = outcome_file.read(MAX_OUTCOME_BYTES + 1)
            if _identity(os.fstat(outcome_file.fileno())) != _identity(opened):
                raise ReplayOutcomeValidationError("outcome changed while it was read")
        if _identity(path.lstat()) != _identity(opened):
            raise ReplayOutcomeValidationError("outcome path identity changed while it was read")
    except ReplayOutcomeValidationError:
        raise
    except OSError as error:
        raise ReplayOutcomeValidationError(f"cannot read outcome: {error}") from error
    if len(source) != info.st_size:
        raise ReplayOutcomeValidationError("outcome changed while it was read")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReplayOutcomeValidationError(f"outcome contains invalid UTF-8: {error}") from error
    if not text.endswith("\n") or "\n" in text[:-1] or "\r" in text:
        raise ReplayOutcomeValidationError("outcome must be exactly one newline-terminated JSON record")
    try:
        decoded = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_duplicate_object)
    except (json.JSONDecodeError, ValueError) as error:
        raise ReplayOutcomeValidationError(f"outcome contains invalid JSON: {error}") from error
    if not isinstance(decoded, dict):
        raise ReplayOutcomeValidationError("outcome must be a JSON object")
    if any(isinstance(value, float) and not math.isfinite(value) for value in decoded.values()):
        raise ReplayOutcomeValidationError("outcome contains a non-finite number")
    try:
        return ReplayOutcome.model_validate(decoded)
    except ValidationError as error:
        first = error.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "root"
        raise ReplayOutcomeValidationError(f"outcome schema path {location}: {first['msg']}") from error
