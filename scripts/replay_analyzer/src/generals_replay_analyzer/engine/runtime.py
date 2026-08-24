"""Fail-closed binding of an analyzer executable into an installed game runtime."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from generals_replay_analyzer.engine.config import (
    EngineRunConfigurationError,
    require_no_reparse_components,
    require_plain_directory_input,
    require_regular_input,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _ordinary_file(path: Path, label: str) -> Path:
    try:
        inspected = require_regular_input(path, label)
        info = inspected.lstat()
    except (EngineRunConfigurationError, OSError) as error:
        raise EngineRunConfigurationError(f"{label} is not an ordinary non-reparse file") from error
    if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
        raise EngineRunConfigurationError(f"{label} is not an ordinary non-reparse file")
    return inspected


@dataclass(frozen=True)
class RuntimeExecutableBinding:
    """Launch identity, deliberately separate from configured executable provenance."""

    configured_executable: Path
    launch_executable: Path
    staged: bool


def _safe_same_file(left: Path, right: Path, expected_sha256: str) -> bool:
    try:
        left = _ordinary_file(left, "configured engine executable")
        right = _ordinary_file(right, "staged engine executable")
        return os.path.samefile(left, right) and _sha256(left) == expected_sha256 and _sha256(right) == expected_sha256
    except (EngineRunConfigurationError, OSError):
        return False


# TheSuperHackers @feature Leex 24/08/2026 Bind each analyzer launch into the installed Zero Hour runtime without replacing retail files. (#TBD)
@contextmanager
def bind_runtime_executable(executable: Path, runtime_directory: Path) -> Iterator[RuntimeExecutableBinding]:
    """Expose a unique hardlink beside runtime data, deleting only the unchanged link we own."""
    source = _ordinary_file(executable, "configured engine executable")
    runtime = require_plain_directory_input(runtime_directory, "engine runtime directory")
    if source.parent == runtime:
        yield RuntimeExecutableBinding(source, source, False)
        return
    source_info = source.stat()
    runtime_info = runtime.stat()
    if source_info.st_dev != runtime_info.st_dev:
        raise EngineRunConfigurationError(
            "engine executable and engine runtime directory are on different volumes; safe hardlink staging is unavailable"
        )
    expected_sha256 = _sha256(source)
    destination = runtime / f"generalszh_replay_analyzer_{uuid4()}.exe"
    require_no_reparse_components(destination.parent, "engine runtime directory")
    try:
        os.link(source, destination)
    except FileExistsError as error:
        raise EngineRunConfigurationError("unique runtime executable binding unexpectedly already exists") from error
    except OSError as error:
        raise EngineRunConfigurationError(f"could not create exclusive runtime executable binding: {error}") from error
    bound = False
    body_failed = False
    try:
        if not _safe_same_file(source, destination, expected_sha256):
            raise EngineRunConfigurationError("runtime executable binding did not retain configured executable identity")
        bound = True
        yield RuntimeExecutableBinding(source, destination, True)
    except BaseException:
        body_failed = True
        raise
    finally:
        # A name alone is never authority to delete: retain a replaced/tampered path for investigation.
        if bound:
            if not _safe_same_file(source, destination, expected_sha256):
                if not body_failed:
                    raise EngineRunConfigurationError("runtime executable binding changed; refusing to delete an unowned path")
            else:
                try:
                    destination.unlink()
                except OSError as error:
                    raise EngineRunConfigurationError(f"could not remove owned runtime executable binding: {error}") from error
