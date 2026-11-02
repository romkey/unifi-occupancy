from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from unifi_occupancy.config import ReportConfig
from unifi_occupancy.models import MOTION, PERSON
from unifi_occupancy.render import (
    AxisMark,
    BandSpan,
    DaySheet,
    IndexCell,
    IndexEntry,
    Row,
    Section,
    Segment,
    Track,
    _duration,
    day_filename,
    render_day,
    render_index,
)

REPORTS = ReportConfig(title="Occupancy", timezone="UTC")
NOW = datetime(2026, 10, 20, 14, 30, tzinfo=UTC)
DAY = date(2026, 10, 20)


BANDS = (
    BandSpan("Night", 0.0, 0.25, 0),
    BandSpan("Morning", 0.25, 0.5, 1),
    BandSpan("Afternoon", 0.5, 0.75, 2),
    BandSpan("Evening", 0.75, 1.0, 3),
)
AXIS = tuple(AxisMark(f"{hour:02d}", hour / 24) for hour in range(0, 25, 3))


def sheet(
    *sections: Section,
    peak: int = 2,
    bands: tuple[BandSpan, ...] = BANDS,
    previous_day: date | None = date(2026, 10, 19),
) -> DaySheet:
    return DaySheet(
        day=DAY,
        sections=sections,
        generated_at=NOW,
        peak_bucket=peak,
        previous_day=previous_day,
        next_day=None,
        bands=bands,
        axis=AXIS,
    )


def section(name: str = "Front of house", **kwargs: object) -> Section:
    track = Track(
        kind=MOTION,
        segments=(Segment(0.25, 0.26, "06:00:00 \u2013 06:14:24 (864s)"),),
        buckets=(0, 2, 1),
        events=3,
        seconds=864.0,
    )
    return Section(
        key="front",
        name=name,
        rows=(Row(label="Front Door", tracks=(track,)),),
        totals={MOTION: 3, PERSON: 0},
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0s"),
        (-5, "0s"),
        (1, "1s"),
        (59, "59s"),
        (60, "1m 00s"),
        (90, "1m 30s"),
        (3600, "1h 00m"),
        (4_980, "1h 23m"),
        (86_400, "24h 00m"),
    ],
)
def test_durations_read_naturally(seconds: float, text: str) -> None:
    assert _duration(seconds) == text


def test_a_day_renders_a_whole_document() -> None:
    html = render_day(sheet(section()), REPORTS)

    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    assert "Front of house" in html
    assert "3 events" in html
    assert '<div class="lane">' in html


def test_the_day_links_to_its_neighbours_and_the_index() -> None:
    html = render_day(sheet(section()), REPORTS)

    assert '<a href="2026-10-19.html">' in html
    assert '<a href="index.html">' in html
    assert "2026-10-21.html" not in html  # no sheet for a day that has not happened


def test_the_periods_are_drawn_and_named() -> None:
    html = render_day(sheet(section()), REPORTS)

    for band in BANDS:
        assert band.name in html
    assert html.count('class="band') == len(BANDS) * 2  # one per lane


def test_bands_are_drawn_where_the_sheet_says_and_not_on_a_1440_grid() -> None:
    """A clock-change day hands over uneven bands; they must be used verbatim."""
    uneven = (BandSpan("Night", 0.0, 0.28, 0), BandSpan("Day", 0.28, 1.0, 1))

    html = render_day(sheet(section(), bands=uneven), REPORTS)

    assert "left:0%;width:28%" in html
    assert "left:28%;width:72%" in html


def test_the_axis_is_placed_where_the_sheet_says() -> None:
    sparse = (AxisMark("00", 0.0), AxisMark("12", 0.52), AxisMark("24", 1.0))

    html = render_day(
        DaySheet(day=DAY, sections=(section(),), generated_at=NOW, peak_bucket=2, axis=sparse),
        REPORTS,
    )

    assert 'left:52%">12</span>' in html


def test_a_day_with_no_cameras_says_so() -> None:
    html = render_day(sheet(), REPORTS)

    assert "No cameras to report on yet" in html


def test_an_empty_track_is_labelled_rather_than_left_blank() -> None:
    quiet = Section(
        key="k", name="Shed", rows=(Row(label="", tracks=(Track(kind=MOTION),)),), totals={}
    )

    html = render_day(sheet(quiet), REPORTS)

    assert "no events" in html


def test_density_is_not_drawn_before_there_is_anything_to_scale_against() -> None:
    html = render_day(sheet(section(), peak=0), REPORTS)

    assert 'class="bk"' not in html


def test_camera_names_cannot_inject_markup() -> None:
    html = render_day(sheet(section(name='<script>alert("x")</script>')), REPORTS)

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_day_with_no_earlier_neighbour_shows_a_dead_end_not_a_link() -> None:
    html = render_day(sheet(section(), previous_day=None), REPORTS)

    assert "2026-10-19.html" not in html
    assert "earlier" in html


def test_the_index_lists_days_newest_first() -> None:
    entries = [
        IndexEntry(
            day=date(2026, 10, 20),
            href="2026-10-20.html",
            cells={"front": IndexCell("front", "Front", {MOTION: 7, PERSON: 2})},
        ),
        IndexEntry(
            day=date(2026, 10, 19),
            href="2026-10-19.html",
            cells={"front": IndexCell("front", "Front", {MOTION: 3, PERSON: 1})},
        ),
    ]

    html = render_index(entries, [("front", "Front")], REPORTS, NOW)

    assert html.index("2026-10-20.html") < html.index("2026-10-19.html")
    assert "<td>7 / 2</td>" in html
    assert "2 days recorded" in html


def test_a_group_with_nothing_that_day_still_gets_a_cell() -> None:
    entry = IndexEntry(day=DAY, href="x.html", cells={})

    html = render_index([entry], [("front", "Front"), ("back", "Back")], REPORTS, NOW)

    assert html.count('<td class="z">0 / 0</td>') == 2
    assert '<td class="tot">0 / 0</td>' in html


def test_an_index_with_no_days_explains_itself() -> None:
    html = render_index([], [], REPORTS, NOW)

    assert "No days have been recorded yet" in html


def test_group_names_cannot_inject_markup_into_the_index() -> None:
    html = render_index([], [("k", "<b>bold</b>")], REPORTS, NOW)

    assert "<b>bold</b>" not in html


def test_the_filename_is_the_day() -> None:
    assert day_filename(DAY) == "2026-10-20.html"
