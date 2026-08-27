"""Dependency-injection ports for deterministic Strata acquisition tests."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class MonotonicClock(Protocol):
    def __call__(self) -> float: ...


class Sleeper(Protocol):
    def __call__(self, seconds: float) -> None: ...


class StrataHttpPort(Protocol):
    def get_profile(self, player_id: int) -> str: ...

    def get_match(self, match_id: int) -> str: ...

    def download_replay(self, url: str) -> bytes: ...
