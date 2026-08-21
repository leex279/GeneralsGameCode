"""Alembic environment for package-owned online file migrations."""

from __future__ import annotations

from pathlib import Path

from alembic import context

from generals_replay_analyzer.db import Base, create_database_engine


def run_migrations_online() -> None:
    """Run migrations through the same SQLite policy used by the application."""
    if context.is_offline_mode():
        raise RuntimeError("offline and in-memory database migrations are not supported")
    configured_path = context.config.attributes.get("database_path")
    if not isinstance(configured_path, Path):
        raise TypeError("a resolved file database_path is required")
    engine = create_database_engine(configured_path)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=Base.metadata,
                compare_type=True,
                render_as_batch=True,
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations_online()
