"""Canonical storage types and reusable database constraints."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import CheckConstraint, Text
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


# TheSuperHackers @feature Leex 21/08/2026 Make structured persistence byte-stable for cache and evidence identities. (#TBD)
class CanonicalJSON(TypeDecorator[Any]):
    """Persist JSON as deterministic UTF-8-compatible text and return Python values."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect | None) -> str | None:
        if value is None:
            return None
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    def process_result_value(self, value: str | None, dialect: Dialect | None) -> Any:
        if value is None:
            return None
        return json.loads(value)


def lowercase_sha256_check(column_name: str) -> CheckConstraint:
    """Require exactly 64 lowercase hexadecimal characters in a named column."""
    return CheckConstraint(
        f"length({column_name}) = 64 AND {column_name} = lower({column_name}) AND {column_name} NOT GLOB '*[^0-9a-f]*'",
        name=f"{column_name}_lowercase_sha256",
    )
