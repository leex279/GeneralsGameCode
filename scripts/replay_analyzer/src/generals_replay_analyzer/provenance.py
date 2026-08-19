"""External source provenance for replay files."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_SOURCE_FILENAME_PATTERN = re.compile(r"^match_(\d+)_user_([0-9a-fA-F]+)_replay\.rep$")


# TheSuperHackers @feature Leex 19/08/2026 Keep source-only identifiers separate from embedded player metadata.
@dataclass(frozen=True)
class SourceProvenance:
    """Checksum-bound source metadata derived without parsing replay contents."""

    original_filename: str
    strata_match_id: str | None
    strata_source_user_token: str | None
    sha256: str


def sha256_file(path: Path) -> str:
    """Return the uppercase SHA-256 digest for a file's raw bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        chunk = source_file.read(1024 * 1024)
        while chunk:
            digest.update(chunk)
            chunk = source_file.read(1024 * 1024)
    return digest.hexdigest().upper()


def extract_source_provenance(path: Path) -> SourceProvenance:
    """Return filename-derived external provenance and a checksum for ``path``."""
    filename_match = _SOURCE_FILENAME_PATTERN.fullmatch(path.name)
    strata_match_id = filename_match.group(1) if filename_match else None
    strata_source_user_token = filename_match.group(2) if filename_match else None

    return SourceProvenance(
        original_filename=path.name,
        strata_match_id=strata_match_id,
        strata_source_user_token=strata_source_user_token,
        sha256=sha256_file(path),
    )
