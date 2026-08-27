"""Public package-contract tests."""

import re
import tomllib
from pathlib import Path

from generals_replay_analyzer import LOGIC_FRAMES_PER_SECOND, __version__


def test_public_version_is_a_non_empty_semantic_version() -> None:
    """Reject missing or malformed package versions exposed to consumers."""
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", __version__)


def test_logic_frame_rate_matches_the_replay_time_contract() -> None:
    """Keep replay timestamps aligned with the engine's fixed simulation rate."""
    assert LOGIC_FRAMES_PER_SECOND == 30


def test_package_declares_standalone_strata_resolver_runtime() -> None:
    configuration = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))

    assert configuration["project"]["scripts"]["strata-resolver"] == "generals_replay_analyzer.strata.cli:main"
    dependencies = configuration["project"]["dependencies"]
    assert any(value.startswith("beautifulsoup4") for value in dependencies)
    assert any(value.startswith("playwright") for value in dependencies)
