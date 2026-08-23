"""Typed report timeline contracts and local chart routes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    ReportEvidenceReferenceDTO,
    TimelineChartDTO,
    TimelineChartQueryDTO,
    TimelineFamilyOptionDTO,
    TimelineIntervalDTO,
    TimelinePlayerOptionDTO,
    TimelinePointDTO,
    TimelineSeriesDTO,
)

from .test_report import _client, _report, _ReportPort

REPLAY_ID = "123e4567-e89b-42d3-a456-426614174100"
REPORT_ID = "123e4567-e89b-42d3-a456-426614174101"
PLAYER_A = "123e4567-e89b-42d3-a456-426614174102"
PLAYER_B = "123e4567-e89b-42d3-a456-426614174105"
EVIDENCE_ID = "123e4567-e89b-42d3-a456-426614174103"


def _reference() -> ReportEvidenceReferenceDTO:
    return ReportEvidenceReferenceDTO(public_id=EVIDENCE_ID, tier="observed")


def test_timeline_query_and_series_are_canonical_frame_only_values() -> None:
    """Catch request ordering, duplicate filters, or precomputed seconds entering chart identity."""
    query = TimelineChartQueryDTO(
        replay_public_id=REPLAY_ID,
        report_public_id=REPORT_ID,
        players=(PLAYER_B, PLAYER_A, PLAYER_B),
        families=("production", "activity", "production"),
    )
    marker = TimelineSeriesDTO(
        series_id="commands-a",
        label="Commands",
        kind="marker",
        player_public_id=PLAYER_A,
        event_family="activity",
        unit=None,
        availability=AvailabilityDTO(state="available"),
        points=(
            TimelinePointDTO(frame=60, value=None, label="Attack move", evidence=(_reference(),)),
            TimelinePointDTO(frame=30, value=None, label="Build", evidence=(_reference(),)),
        ),
        intervals=(),
    )
    band = TimelineSeriesDTO(
        series_id="phase-a",
        label="Strategy phase",
        kind="band",
        player_public_id=PLAYER_A,
        event_family="strategy",
        unit=None,
        availability=AvailabilityDTO(state="available"),
        points=(),
        intervals=(
            TimelineIntervalDTO(
                frame_start=0,
                frame_end=300,
                label="Opening",
                evidence=(_reference(),),
            ),
        ),
    )

    chart = TimelineChartDTO(
        schema_version="web-report-timeline-v1",
        query=query,
        availability=AvailabilityDTO(state="available"),
        timebase_fps=30,
        available_players=(
            TimelinePlayerOptionDTO(public_id=PLAYER_B, label="Fox27"),
            TimelinePlayerOptionDTO(public_id=PLAYER_A, label="Leex279"),
        ),
        available_families=(
            TimelineFamilyOptionDTO(value="strategy", label="Strategy"),
            TimelineFamilyOptionDTO(value="activity", label="Activity"),
        ),
        series=(band, marker),
    )

    assert chart.query.players == (PLAYER_A, PLAYER_B)
    assert chart.query.families == ("activity", "production")
    assert tuple(option.public_id for option in chart.available_players) == (PLAYER_A, PLAYER_B)
    assert tuple(option.value for option in chart.available_families) == ("activity", "strategy")
    assert tuple(series.series_id for series in chart.series) == ("commands-a", "phase-a")
    assert tuple(point.frame for point in chart.series[0].points) == (30, 60)
    assert "second" not in chart.model_dump_json()


@pytest.mark.parametrize(
    "series",
    [
        TimelineSeriesDTO.model_construct(
            series_id="bad-band",
            label="Bad band",
            kind="band",
            player_public_id=None,
            event_family="strategy",
            unit=None,
            availability=AvailabilityDTO(state="available"),
            points=(TimelinePointDTO(frame=0, value=None, label="Wrong", evidence=(_reference(),)),),
            intervals=(),
        ),
        TimelineSeriesDTO.model_construct(
            series_id="bad-line",
            label="Bad line",
            kind="line",
            player_public_id=None,
            event_family="economy",
            unit="credits",
            availability=AvailabilityDTO(state="available"),
            points=(TimelinePointDTO(frame=0, value=None, label="Cash", evidence=(_reference(),)),),
            intervals=(),
        ),
    ],
)
def test_timeline_series_rejects_shapes_that_do_not_match_their_kind(series: TimelineSeriesDTO) -> None:
    """Catch band/point semantics collapsing into one ambiguous chart payload."""
    with pytest.raises(ValidationError, match="timeline series kind"):
        TimelineSeriesDTO.model_validate(series.model_dump())


def test_timeline_timebase_is_frozen_at_thirty_frames_per_second() -> None:
    """Catch a chart endpoint inventing a replay-specific or browser-specific timebase."""
    query = TimelineChartQueryDTO(replay_public_id=REPLAY_ID, report_public_id=REPORT_ID)
    with pytest.raises(ValidationError):
        TimelineChartDTO(
            schema_version="web-report-timeline-v1",
            query=query,
            availability=AvailabilityDTO(state="unavailable", reason_codes=("telemetry_missing",)),
            timebase_fps=60,
            available_players=(),
            available_families=(),
            series=(),
        )


@pytest.mark.parametrize("family", ["commands", "terrain", "Activity"])
def test_timeline_rejects_unknown_event_families(family: str) -> None:
    """Catch arbitrary strings widening the frozen seven-family selector contract."""
    with pytest.raises(ValidationError):
        TimelineChartQueryDTO(
            replay_public_id=REPLAY_ID,
            report_public_id=REPORT_ID,
            families=(family,),  # type: ignore[arg-type]
        )

    with pytest.raises(ValidationError):
        TimelineFamilyOptionDTO(value=family, label="Unknown")  # type: ignore[arg-type]


def test_timeline_point_preserves_integer_and_string_scalars() -> None:
    """Catch the web contract coercing authoritative raw scalar types for chart display."""
    integer = TimelinePointDTO(frame=30, value=7, label="Count", evidence=(_reference(),))
    text = TimelinePointDTO(frame=60, value="phase-2", label="Phase", evidence=(_reference(),))

    assert integer.value == 7
    assert type(integer.value) is int
    assert text.value == "phase-2"
    assert type(text.value) is str


@pytest.mark.parametrize("value", [7, 1.5, "phase-2"])
def test_timeline_marker_preserves_optional_exact_frame_scalars(value: object) -> None:
    """Catch exact-frame scalar claims being rejected merely because they render as markers."""
    marker = TimelineSeriesDTO(
        series_id="exact-frame",
        label="Exact frame claim",
        kind="marker",
        player_public_id=PLAYER_A,
        event_family="activity",
        unit=None,
        availability=AvailabilityDTO(state="available"),
        points=(
            TimelinePointDTO(
                frame=30,
                value=value,  # type: ignore[arg-type]
                label="Event",
                evidence=(_reference(),),
            ),
        ),
        intervals=(),
    )

    assert marker.points[0].value == value
    assert type(marker.points[0].value) is type(value)


@pytest.mark.parametrize("value", [True, -0.0])
def test_timeline_point_rejects_bool_and_negative_zero(value: object) -> None:
    """Catch Python numeric edge cases becoming ambiguous report chart values."""
    with pytest.raises(ValidationError):
        TimelinePointDTO(
            frame=30,
            value=value,  # type: ignore[arg-type]
            label="Invalid",
            evidence=(_reference(),),
        )


def test_timeline_band_rejects_a_zero_width_interval() -> None:
    """Catch an exact-frame event being mislabeled as an interval band."""
    with pytest.raises(ValidationError, match="timeline interval"):
        TimelineIntervalDTO(
            frame_start=30,
            frame_end=30,
            label="Not a band",
            evidence=(_reference(),),
        )


def test_chart_canonicalizes_all_unavailable_series_to_unavailable() -> None:
    """Catch a chart claiming partial or available data when every series is unavailable."""
    query = TimelineChartQueryDTO(replay_public_id=REPLAY_ID, report_public_id=REPORT_ID)
    chart = TimelineChartDTO(
        schema_version="web-report-timeline-v1",
        query=query,
        availability=AvailabilityDTO(state="partial", reason_codes=("telemetry_partial",)),
        timebase_fps=30,
        available_players=(),
        available_families=(TimelineFamilyOptionDTO(value="economy", label="Economy"),),
        series=(
            TimelineSeriesDTO(
                series_id="cash",
                label="Cash",
                kind="line",
                player_public_id=None,
                event_family="economy",
                unit="credits",
                availability=AvailabilityDTO(state="unavailable", reason_codes=("telemetry_missing",)),
                points=(),
                intervals=(),
            ),
        ),
    )

    assert chart.availability.state == "unavailable"
    assert chart.availability.reason_codes == ("telemetry_partial",)


class _ChartPort(_ReportPort):
    def timeline_chart(self, query: TimelineChartQueryDTO) -> TimelineChartDTO:
        self.timeline_queries.append(query)
        return TimelineChartDTO(
            schema_version="web-report-timeline-v1",
            query=query,
            availability=AvailabilityDTO(state="available"),
            timebase_fps=30,
            available_players=(TimelinePlayerOptionDTO(public_id=PLAYER_A, label="Leex279"),),
            available_families=(TimelineFamilyOptionDTO(value="activity", label="Activity"),),
            series=(
                TimelineSeriesDTO(
                    series_id="commands-a",
                    label="Commands",
                    kind="marker",
                    player_public_id=PLAYER_A,
                    event_family="activity",
                    unit=None,
                    availability=AvailabilityDTO(state="available"),
                    points=(TimelinePointDTO(frame=30, value=7, label="Build", evidence=(_reference(),)),),
                    intervals=(),
                ),
            ),
        )


def test_timeline_endpoint_rejects_html_content_negotiation() -> None:
    """Catch the typed chart endpoint becoming an HTML or content-sniffed response."""
    port = _ChartPort(_report())
    with _client(port) as client:
        response = client.get(
            f"/api/replays/{REPLAY_ID}/reports/{REPORT_ID}/charts/timeline",
            headers={"host": "localhost", "accept": "text/html"},
        )

    assert response.status_code == 406
    assert response.json()["code"] == "not_acceptable"
    assert port.timeline_queries == []


@pytest.mark.parametrize(
    ("accept", "expected_status"),
    [
        ("application/json;q=0.5", 200),
        ("APPLICATION/JSON ; Q=1", 200),
        ("application/json;q=0", 406),
        ("application/json;q=0, */*;q=1", 406),
        ("application/json;q=0.5;q=1", 406),
        ("application/*;q=0, */*;q=1", 406),
        ("application/json;q=0.5, application/json;q=0", 406),
    ],
)
def test_timeline_endpoint_honors_json_accept_quality(accept: str, expected_status: int) -> None:
    """Catch valid nonzero Accept quality being confused with an explicit JSON refusal."""
    port = _ChartPort(_report())
    with _client(port) as client:
        response = client.get(
            f"/api/replays/{REPLAY_ID}/reports/{REPORT_ID}/charts/timeline",
            headers={"host": "localhost", "accept": accept},
        )

    assert response.status_code == expected_status
    assert len(port.timeline_queries) == (1 if expected_status == 200 else 0)


def test_timeline_endpoint_returns_only_the_exact_normalized_fixed_query() -> None:
    """Catch chart filters or a report identity being dropped before the fake query port."""
    port = _ChartPort(_report())
    with _client(port) as client:
        response = client.get(
            f"/api/replays/{REPLAY_ID}/reports/{REPORT_ID}/charts/timeline"
            f"?player={PLAYER_A}&player={PLAYER_A}&family=activity&family=activity",
            headers={"host": "localhost", "accept": "application/json"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert port.timeline_queries == [
        TimelineChartQueryDTO(
            replay_public_id=REPLAY_ID,
            report_public_id=REPORT_ID,
            players=(PLAYER_A,),
            families=("activity",),
        )
    ]
    payload = response.json()
    assert payload["timebase_fps"] == 30
    assert payload["series"][0]["points"][0]["frame"] == 30
    assert "second" not in response.text
