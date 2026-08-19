"""Public package-contract tests."""

import re

from generals_replay_analyzer import LOGIC_FRAMES_PER_SECOND, __version__


def test_public_version_is_a_non_empty_semantic_version() -> None:
    """Reject missing or malformed package versions exposed to consumers."""
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", __version__)


def test_logic_frame_rate_matches_the_replay_time_contract() -> None:
    """Keep replay timestamps aligned with the engine's fixed simulation rate."""
    assert LOGIC_FRAMES_PER_SECOND == 30
