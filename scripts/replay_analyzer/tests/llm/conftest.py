"""Stable public identity fixtures for LLM boundary tests."""

from __future__ import annotations

from uuid import UUID

import pytest


@pytest.fixture
def public_ids() -> tuple[str, ...]:
    return tuple(str(UUID(int=value)) for value in range(1, 40))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
