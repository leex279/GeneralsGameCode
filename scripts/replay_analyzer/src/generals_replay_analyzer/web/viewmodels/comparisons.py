"""Text-first comparison presentation values."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from generals_replay_analyzer.web.ports import ComparisonDTO

ComparisonKind = Literal["players", "matches", "openings", "strategies", "time_periods"]


class ComparisonModeOption(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: ComparisonKind
    label: str
    unavailable_reason_code: str


class ComparisonViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    comparison: ComparisonDTO
    json_url: str


# TheSuperHackers @feature Leex 23/08/2026 Keep every promised comparison mode visible without fabricated values. (#TBD)
def comparison_mode_options() -> tuple[ComparisonModeOption, ...]:
    return (
        ComparisonModeOption(
            value="players", label="Players", unavailable_reason_code="player_comparison_adapter_pending"
        ),
        ComparisonModeOption(
            value="matches", label="Matches", unavailable_reason_code="match_comparison_adapter_pending"
        ),
        ComparisonModeOption(
            value="openings", label="Openings", unavailable_reason_code="opening_comparison_adapter_pending"
        ),
        ComparisonModeOption(
            value="strategies", label="Strategies", unavailable_reason_code="strategy_comparison_adapter_pending"
        ),
        ComparisonModeOption(
            value="time_periods", label="Time Periods", unavailable_reason_code="period_comparison_adapter_pending"
        ),
    )


def comparison_view(comparison: ComparisonDTO, json_url: str) -> ComparisonViewModel:
    """Pair one immutable comparison with its exact typed data link."""
    return ComparisonViewModel(comparison=comparison, json_url=json_url)
