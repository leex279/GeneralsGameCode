"""Public invariants for server-owned deterministic map downsampling."""

import pytest
from pydantic import ValidationError

from generals_replay_analyzer.web.ports import DownsamplingDTO


def test_nonmandatory_results_cannot_exceed_the_requested_budget() -> None:
    """A service returning excess optional samples would violate the declared deterministic cap."""
    with pytest.raises(ValidationError):
        DownsamplingDTO(
            algorithm_version="event-forced-stratified-v1",
            requested_sample_budget=100,
            original_sample_count=150,
            mandatory_sample_count=20,
            returned_sample_count=101,
            budget_exceeded_by_mandatory=False,
        )


def test_mandatory_evidence_may_truthfully_exceed_the_soft_budget() -> None:
    downsampling = DownsamplingDTO(
        algorithm_version="event-forced-stratified-v1",
        requested_sample_budget=100,
        original_sample_count=120,
        mandatory_sample_count=105,
        returned_sample_count=105,
        budget_exceeded_by_mandatory=True,
    )

    assert downsampling.returned_sample_count == 105


@pytest.mark.parametrize(
    "mandatory, returned, original",
    [(2, 1, 3), (1, 4, 3)],
)
def test_downsampling_count_order_is_closed(mandatory: int, returned: int, original: int) -> None:
    with pytest.raises(ValidationError):
        DownsamplingDTO(
            algorithm_version="event-forced-stratified-v1",
            requested_sample_budget=100,
            original_sample_count=original,
            mandatory_sample_count=mandatory,
            returned_sample_count=returned,
            budget_exceeded_by_mandatory=False,
        )
