"""Deterministic comparisons for paired VC6 and modern per-frame CRC logs."""

from pathlib import Path

import pytest

from generals_replay_analyzer.engine.crc_diff import CRCDifference, CRCDiffError, first_crc_difference


def _write_frame(directory: Path, frame: int, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"DebugFrame_{frame:06d}.txt").write_bytes(text.encode("utf-8"))


# TheSuperHackers @feature Leex 24/08/2026 Pin exact VC6-versus-modern CRC line comparison semantics. (#TBD)
def test_first_crc_difference_reports_the_earliest_component_and_exact_lines(tmp_path: Path) -> None:
    retail = tmp_path / "retail"
    modern = tmp_path / "modern"
    common_99 = "99:00000 CRC for frame 99 is 0x11111111\r\n"
    _write_frame(retail, 99, common_99)
    _write_frame(modern, 99, common_99.replace("\r\n", "\n"))
    _write_frame(
        retail,
        100,
        "100:00000 CRC at start of frame 100 is 0x00000000\r\n"
        "100:00001 CRC after objects for frame 100 is 0xAAAAAAAA\r\n"
        "100:00002 RandomSeed: 123\r\n",
    )
    _write_frame(
        modern,
        100,
        "100:00000 CRC at start of frame 100 is 0x00000000\n"
        "100:00001 CRC after objects for frame 100 is 0xBBBBBBBB\n"
        "100:00002 RandomSeed: 999\n",
    )

    assert first_crc_difference(retail, modern) == CRCDifference(
        frame=100,
        line_number=2,
        component="objects",
        reason="line_mismatch",
        retail_line="100:00001 CRC after objects for frame 100 is 0xAAAAAAAA",
        modern_line="100:00001 CRC after objects for frame 100 is 0xBBBBBBBB",
    )


def test_first_crc_difference_reports_the_first_missing_line(tmp_path: Path) -> None:
    retail = tmp_path / "retail"
    modern = tmp_path / "modern"
    _write_frame(
        retail,
        100,
        "100:00000 CRC at start of frame 100 is 0x00000000\n"
        "100:00001 RandomSeed: 123\n",
    )
    _write_frame(modern, 100, "100:00000 CRC at start of frame 100 is 0x00000000\n")

    assert first_crc_difference(retail, modern) == CRCDifference(
        frame=100,
        line_number=2,
        component="random_seed",
        reason="modern_line_missing",
        retail_line="100:00001 RandomSeed: 123",
        modern_line=None,
    )


def test_first_crc_difference_returns_none_for_line_ending_equivalent_logs(tmp_path: Path) -> None:
    retail = tmp_path / "retail"
    modern = tmp_path / "modern"
    _write_frame(retail, 100, "100:00000 CRC for frame 100 is 0x12345678\r\n")
    _write_frame(modern, 100, "100:00000 CRC for frame 100 is 0x12345678\n")

    assert first_crc_difference(retail, modern) is None


def test_first_crc_difference_reports_a_missing_earliest_frame_deterministically(tmp_path: Path) -> None:
    retail = tmp_path / "retail"
    modern = tmp_path / "modern"
    _write_frame(retail, 101, "101:00000 CRC for frame 101 is 0x12345678\n")
    _write_frame(modern, 100, "100:00000 CRC for frame 100 is 0x87654321\n")

    assert first_crc_difference(retail, modern) == CRCDifference(
        frame=100,
        line_number=1,
        component="game_logic_final",
        reason="retail_frame_missing",
        retail_line=None,
        modern_line="100:00000 CRC for frame 100 is 0x87654321",
    )


@pytest.mark.parametrize("missing_side", ["retail", "modern"])
def test_first_crc_difference_rejects_a_side_without_per_frame_logs(tmp_path: Path, missing_side: str) -> None:
    retail = tmp_path / "retail"
    modern = tmp_path / "modern"
    populated = modern if missing_side == "retail" else retail
    empty = retail if missing_side == "retail" else modern
    empty.mkdir()
    _write_frame(populated, 100, "100:00000 CRC for frame 100 is 0x12345678\n")

    with pytest.raises(CRCDiffError, match=rf"{missing_side}.*contains no DebugFrame logs"):
        first_crc_difference(retail, modern)
