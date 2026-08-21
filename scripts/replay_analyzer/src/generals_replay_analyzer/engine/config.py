"""Strict immutable configuration for one isolated engine telemetry run."""

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 86_400
MIN_MOVEMENT_SAMPLE_FRAMES = 1
MAX_MOVEMENT_SAMPLE_FRAMES = 3_600
_WINDOWS_FORBIDDEN_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class EngineRunConfigurationError(ValueError):
    """Reject an unsafe or unsupported runner request before engine launch."""


def default_data_root() -> Path:
    """Return the product-owned Windows runtime root without touching it."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return (Path(local_app_data) / "GeneralsReplayAnalyzer").resolve()
    return (Path.home() / "AppData" / "Local" / "GeneralsReplayAnalyzer").resolve()


def _is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _existing_path_chain(path: Path) -> tuple[Path, ...]:
    current = Path(path.anchor)
    chain: list[Path] = [current]
    for part in path.parts[1:]:
        current /= part
        if not current.exists() and not current.is_symlink():
            break
        chain.append(current)
    return tuple(chain)


def require_no_reparse_components(path: Path, label: str) -> None:
    """Reject symlink/junction/reparse aliases in every existing component."""
    for component in _existing_path_chain(path):
        try:
            info = component.lstat()
        except OSError as error:
            raise EngineRunConfigurationError(f"{label} path cannot be inspected: {component}: {error}") from error
        if component.is_symlink() or _is_reparse(info):
            raise EngineRunConfigurationError(f"{label} path contains a reparse or symlink component: {component}")


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _require_safe_windows_components(path: Path, label: str) -> None:
    """Reject Win32 normalization/device aliases before any filesystem operation."""
    source = str(path)
    if source.startswith(("\\\\?\\", "\\\\.\\")):
        raise EngineRunConfigurationError(f"{label} path uses an unsafe Windows device namespace: {path}")
    for component in path.parts[1:]:
        stem = component.split(".", 1)[0].upper()
        if (
            component.endswith((" ", "."))
            or any(ord(character) < 32 or character in _WINDOWS_FORBIDDEN_CHARACTERS for character in component)
            or stem in _WINDOWS_RESERVED_NAMES
        ):
            raise EngineRunConfigurationError(f"{label} path contains an unsafe Windows path component: {component}")


def require_absolute_resolved_path(path: Path, label: str, *, must_exist: bool) -> Path:
    """Require an explicit canonical path rather than resolving caller ambiguity silently."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise EngineRunConfigurationError(f"{label} path must be an absolute resolved Path")
    _require_safe_windows_components(path, label)
    require_no_reparse_components(path, label)
    try:
        resolved = path.resolve(strict=must_exist)
    except OSError as error:
        raise EngineRunConfigurationError(f"{label} path cannot be resolved: {path}: {error}") from error
    if not _same_path(path, resolved):
        raise EngineRunConfigurationError(f"{label} path must be absolute and already resolved: {path}")
    return resolved


def require_regular_input(path: Path, label: str) -> Path:
    """Validate one immutable ordinary input file while permitting the runtime hardlink fixture."""
    resolved = require_absolute_resolved_path(path, label, must_exist=True)
    try:
        info = resolved.lstat()
    except OSError as error:
        raise EngineRunConfigurationError(f"{label} cannot be inspected: {resolved}: {error}") from error
    if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
        raise EngineRunConfigurationError(f"{label} must be an ordinary non-reparse file: {resolved}")
    return resolved


# TheSuperHackers @feature Leex 21/08/2026 Define one strict isolated replay-engine invocation contract. (#TBD)
@dataclass(frozen=True)
class EngineRunConfig:
    """Validated product settings that cannot be changed during an engine run."""

    executable: Path
    timeout_seconds: int = 900
    movement_sample_frames: int = 15
    data_root: Path = field(default_factory=default_data_root)

    def __post_init__(self) -> None:
        executable = require_regular_input(self.executable, "engine executable")
        if type(self.timeout_seconds) is not int or not MIN_TIMEOUT_SECONDS <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise EngineRunConfigurationError(
                f"timeout_seconds must be an integer from {MIN_TIMEOUT_SECONDS} through {MAX_TIMEOUT_SECONDS}"
            )
        if (
            type(self.movement_sample_frames) is not int
            or not MIN_MOVEMENT_SAMPLE_FRAMES <= self.movement_sample_frames <= MAX_MOVEMENT_SAMPLE_FRAMES
        ):
            raise EngineRunConfigurationError(
                "movement_sample_frames must be an integer from "
                f"{MIN_MOVEMENT_SAMPLE_FRAMES} through {MAX_MOVEMENT_SAMPLE_FRAMES}"
            )
        data_root = require_absolute_resolved_path(self.data_root, "data root", must_exist=False)
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "data_root", data_root)
