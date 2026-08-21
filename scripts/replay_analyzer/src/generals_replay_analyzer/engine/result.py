"""Closed public result contract for one isolated engine telemetry attempt."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


# TheSuperHackers @feature Leex 21/08/2026 Keep engine-run status and analysis eligibility explicit and closed. (#TBD)
class EngineRunStatus(str, Enum):
    """Mutually exclusive runner outcomes with evidence-aware precedence."""

    SUCCESS = "success"
    VALID_CRC_MISMATCH = "valid_crc_mismatch"
    INPUT_UNAVAILABLE = "input_unavailable"
    INVALID_REPLAY_HEADER = "invalid_replay_header"
    TRUNCATED_INPUT = "truncated_input"
    REPLAY_TRUNCATED = "replay_truncated"
    INTERRUPTED = "interrupted"
    TIMEOUT = "timeout"
    ENGINE_FAILURE = "nonzero_engine_failure"
    MISSING_TRACE = "missing_trace"
    INVALID_TRACE = "invalid_trace"
    WRITER_ERROR = "writer_error"
    MISSING_OUTCOME = "missing_outcome"
    INVALID_OUTCOME = "invalid_outcome"
    OUTCOME_MISMATCH = "outcome_mismatch"
    ASSET_INVALID = "asset_invalid"
    LAUNCH_FAILURE = "launch_failure"
    INPUT_CHANGED = "input_changed"
    UNSAFE_OUTPUT = "unsafe_output"


class ReplayQuality(str, Enum):
    """User-facing evidence quality independent of process mechanics."""

    ENGINE_VERIFIED = "engine_verified"
    PARTIAL = "partial"
    FAILED = "failed"


class StrategyAnalysisScope(str, Enum):
    """Maximum deterministic/inferred analysis scope authorized by this evidence."""

    FULL_MATCH = "full_match"
    OBSERVED_BOUNDARY_ONLY = "observed_boundary_only"
    NONE = "none"


@dataclass(frozen=True)
class RunDiagnostic:
    """Stable typed diagnostic retained in result metadata and CLI JSON."""

    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class EngineRunResult:
    """Validated public paths plus diagnostics for one never-reused run directory."""

    run_id: str
    run_dir: Path
    trace_path: Path | None
    catalog_path: Path | None
    map_assets: tuple[Path, ...]
    outcome_path: Path | None
    stdout_path: Path | None
    stderr_path: Path | None
    exit_code: int | None
    status: EngineRunStatus
    duration_seconds: float
    replay_quality: ReplayQuality
    strategy_analysis_scope: StrategyAnalysisScope
    diagnostics: tuple[RunDiagnostic, ...]

    def to_public_dict(self) -> dict[str, object]:
        """Serialize deterministic CLI JSON without leaking unvalidated evidence paths."""
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "status": self.status.value,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "replay_quality": self.replay_quality.value,
            "strategy_analysis_scope": self.strategy_analysis_scope.value,
            "trace_path": str(self.trace_path) if self.trace_path is not None else None,
            "catalog_path": str(self.catalog_path) if self.catalog_path is not None else None,
            "map_assets": [str(path) for path in self.map_assets],
            "outcome_path": str(self.outcome_path) if self.outcome_path is not None else None,
            "stdout_path": str(self.stdout_path) if self.stdout_path is not None else None,
            "stderr_path": str(self.stderr_path) if self.stderr_path is not None else None,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }
