"""Checkout-independent access to package-owned web and migration files."""

from __future__ import annotations

from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath


class PackagedResourceError(LookupError):
    """A requested package resource is invalid or absent."""


# TheSuperHackers @feature Leex 22/08/2026 Resolve web resources only from installed package data. (#TBD)
def package_resource(relative_name: str) -> Traversable:
    """Return an existing package resource with no checkout-relative fallback."""
    if not relative_name or "\\" in relative_name:
        raise PackagedResourceError("invalid packaged resource name")
    relative = PurePosixPath(relative_name)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts or ":" in relative.parts[0]:
        raise PackagedResourceError("invalid packaged resource name")
    target = resources.files("generals_replay_analyzer").joinpath(*relative.parts)
    if not target.is_file() and not target.is_dir():
        raise PackagedResourceError("packaged resource is unavailable")
    return target
