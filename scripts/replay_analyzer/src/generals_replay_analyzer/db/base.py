"""Declarative metadata and focused row mixins."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utc_now() -> datetime:
    """Return an aware UTC timestamp for application-created rows."""
    return datetime.now(UTC)


# TheSuperHackers @feature Leex 21/08/2026 Define stable database naming and row identity conventions. (#TBD)
class Base(DeclarativeBase):
    """Root for the migration-owned SQLAlchemy schema."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class IntegerPrimaryKeyMixin:
    """Internal row identity that never crosses service DTO boundaries."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)


class PublicIdMixin:
    """Stable external UUID stored in lowercase hyphenated form."""

    public_id: Mapped[str] = mapped_column(
        String(36),
        CheckConstraint(
            "length(public_id) = 36 AND public_id = lower(public_id) "
            "AND substr(public_id, 9, 1) = '-' AND substr(public_id, 14, 1) = '-' "
            "AND substr(public_id, 19, 1) = '-' AND substr(public_id, 24, 1) = '-' "
            "AND replace(public_id, '-', '') NOT GLOB '*[^0-9a-f]*'",
            name="public_id_lowercase_uuid",
        ),
        nullable=False,
        unique=True,
    )


class CreatedAtMixin:
    """UTC creation time shared by durable public records."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
