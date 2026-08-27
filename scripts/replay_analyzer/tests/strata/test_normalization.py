from __future__ import annotations

import pytest

from generals_replay_analyzer.strata.normalization import InvalidQueryNameError, normalize_query_name


def test_query_normalization_removes_only_terminal_nul_padding() -> None:
    query = normalize_query_name(" [TAG]Fish! \x00\x00")

    assert query.raw == " [TAG]Fish! "
    assert query.nfc == " [TAG]Fish! "
    assert query.casefold == " [tag]fish! "


def test_query_normalization_preserves_case_and_canonically_composes_unicode() -> None:
    query = normalize_query_name("Fi\u0301sh")

    assert query.raw == "Fi\u0301sh"
    assert query.nfc == "F\u00edsh"
    assert query.casefold == "f\u00edsh"


@pytest.mark.parametrize(
    "value",
    [
        "\x00\x00",
        "fi\x00sh",
        "\ud800",
        "x" * 256,
        42,
    ],
)
def test_invalid_query_names_have_a_stable_public_error(value: object) -> None:
    with pytest.raises(InvalidQueryNameError) as raised:
        normalize_query_name(value)  # type: ignore[arg-type]

    assert raised.value.code == "invalid_query_name"
    assert str(raised.value)
