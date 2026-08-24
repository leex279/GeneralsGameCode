"""Adapter from validated engine runs to import-stage telemetry artifacts."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import AnalyzerSettings
from ..engine.config import EngineRunConfig
from ..engine.result import EngineRunResult, EngineRunStatus
from ..engine.runner import export_telemetry
from .service import AcquisitionDiagnostic, TelemetryArtifact

_HASH_BLOCK_BYTES = 1024 * 1024
_PARTIAL_TRACE_STATUSES = frozenset(
    {
        EngineRunStatus.VALID_CRC_MISMATCH,
        EngineRunStatus.REPLAY_TRUNCATED,
        EngineRunStatus.INTERRUPTED,
    }
)
_PATHLIKE_TEXT = re.compile(r"(?i)(?:[a-z]:[\\/][^\s]+|\\\\[^\s]+|/(?:[^\s]+))")

EngineTelemetryExporter = Callable[[Path, EngineRunConfig], EngineRunResult]


def _sha256_file(path: Path) -> str:
    """Hash one input with bounded reads so replay size does not affect memory use."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(_HASH_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _replace_path_variants(message: str, path: Path, replacement: str) -> str:
    redacted = message
    for source_text in (str(path), path.as_posix()):
        if source_text:
            redacted = re.sub(re.escape(source_text), replacement, redacted, flags=re.IGNORECASE)
    return redacted


def _redact_diagnostic_message(message: str, replay: Path, result: EngineRunResult) -> str:
    """Retain diagnostic meaning while removing input and runner filesystem provenance."""
    redacted = _replace_path_variants(message, replay, "[replay]")
    redacted = _replace_path_variants(redacted, replay.parent, "[replay-root]")
    paths = (
        result.trace_path,
        result.catalog_path,
        result.outcome_path,
        result.stdout_path,
        result.stderr_path,
        *result.map_assets,
    )
    for path in paths:
        if path is not None:
            redacted = _replace_path_variants(redacted, path, "[engine-artifact]")
            redacted = _replace_path_variants(redacted, path.parent, "[engine-artifact-root]")
    redacted = _replace_path_variants(redacted, result.run_dir, "[engine-run]")
    redacted = _replace_path_variants(redacted, result.run_dir.parent, "[engine-run-root]")
    return _PATHLIKE_TEXT.sub("[redacted-path]", redacted)


def _diagnostics(replay: Path, result: EngineRunResult) -> tuple[AcquisitionDiagnostic, ...]:
    return tuple(
        AcquisitionDiagnostic(
            code=diagnostic.code,
            message=_redact_diagnostic_message(diagnostic.message, replay, result),
        )
        for diagnostic in result.diagnostics
    )


# TheSuperHackers @feature Leex 23/08/2026 Adapt validated engine telemetry runs into import-safe acquisition artifacts. (#TBD)
@dataclass(frozen=True, slots=True)
class EngineTelemetryAcquirer:
    """Bind managed replay content to one validated engine telemetry attempt."""

    settings: AnalyzerSettings
    exporter: EngineTelemetryExporter = export_telemetry

    def acquire(self, replay: Path, replay_sha256: str) -> TelemetryArtifact:
        """Run a content-bound replay and translate its closed runner outcome."""
        if _sha256_file(replay) != replay_sha256:
            raise ValueError("replay SHA-256 does not match the imported replay content")
        executable = self.settings.engine_executable
        if executable is None:
            raise ValueError("engine telemetry acquisition requires a configured engine executable")
        config = EngineRunConfig(
            executable=executable,
            runtime_directory=self.settings.engine_runtime_directory,
            data_root=self.settings.data_root,
            movement_sample_frames=self.settings.movement_sample_frames,
        )
        executable_sha256 = _sha256_file(config.executable)
        result = self.exporter(replay, config)
        diagnostics = _diagnostics(replay, result)
        if result.status is EngineRunStatus.SUCCESS:
            runner_status = "success"
            replay_quality = "complete"
            strategy_analysis_scope = "full"
        elif result.status in _PARTIAL_TRACE_STATUSES:
            runner_status = "success"
            replay_quality = "partial"
            strategy_analysis_scope = "observed_boundary_only"
            diagnostics += (AcquisitionDiagnostic("engine_terminal_status", result.status.value),)
        else:
            runner_status = result.status.value
            replay_quality = "failed"
            strategy_analysis_scope = "none"
        return TelemetryArtifact(
            run_id=result.run_id,
            runner_status=runner_status,
            replay_quality=replay_quality,
            strategy_analysis_scope=strategy_analysis_scope,
            trace_path=result.trace_path,
            catalog_path=result.catalog_path,
            map_asset_paths=result.map_assets,
            outcome_path=result.outcome_path,
            stdout_path=result.stdout_path,
            stderr_path=result.stderr_path,
            exit_code=result.exit_code,
            engine_build=None,
            engine_executable_sha256=executable_sha256,
            diagnostics=diagnostics,
        )
