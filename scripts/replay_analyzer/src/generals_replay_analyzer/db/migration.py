"""Programmatic access to package-owned Alembic resources."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


# TheSuperHackers @feature Leex 21/08/2026 Run analyzer migrations without checkout-bound Alembic configuration. (#TBD)
def make_alembic_config(database_path: Path) -> Config:
    """Build an in-memory Alembic configuration for a file database."""
    resolved_path = Path(database_path).resolve(strict=False)
    config = Config()
    config.set_main_option("script_location", "generals_replay_analyzer.db:migrations")
    config.attributes["database_path"] = resolved_path
    return config


def upgrade_database(database_path: Path, revision: str = "head") -> None:
    """Upgrade a file database through the package-owned migration graph."""
    command.upgrade(make_alembic_config(database_path), revision)


def downgrade_database(database_path: Path, revision: str = "base") -> None:
    """Downgrade a file database through the package-owned migration graph."""
    command.downgrade(make_alembic_config(database_path), revision)
