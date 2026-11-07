from __future__ import annotations

import re
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
    now: float | None = None,
    now_label: str = "",
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
        now=now,
        now_label=now_label,
    )


TRACK = Track(
    kind=MOTION,
    segments=(Segment(0.25, 0.26, "06:00:00 \u2013 06:14:24 (864s)"),),
    buckets=(0, 2, 1),
    events=3,
    seconds=864.0,
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
    clean = render_day(sheet(section()), REPORTS)
    hostile = render_day(sheet(section(name='<script>alert("x")</script>')), REPORTS)

    # The page has scripts of its own, so count them rather than forbid them.
    assert hostile.count("<script>") == clean.count("<script>")
    assert "&lt;script&gt;" in hostile


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


# ---------------------------------------------------------------------------
# Light and dark
# ---------------------------------------------------------------------------


def test_the_page_follows_the_browser_by_default() -> None:
    html = render_day(sheet(section()), REPORTS)

    assert "@media (prefers-color-scheme: dark)" in html
    assert "color-scheme: light dark" in html


def test_a_chosen_theme_overrides_what_the_browser_asks_for() -> None:
    html = render_day(sheet(section()), REPORTS)

    # The browser's preference applies only while the reader has not overridden it.
    assert ':root:not([data-theme="light"])' in html
    assert ':root[data-theme="dark"]' in html


def test_both_dark_rules_carry_the_same_palette() -> None:
    """One source of truth, spliced twice -- the two must never drift."""
    html = render_day(sheet(section()), REPORTS)

    assert html.count("--bg: #15161a") == 2


def test_the_theme_control_is_offered_on_every_page() -> None:
    day = render_day(sheet(section()), REPORTS)
    index = render_index([], [], REPORTS, NOW)

    for html in (day, index):
        assert 'id="theme"' in html
        for choice in ("auto", "light", "dark"):
            assert f'data-theme="{choice}"' in html


def test_the_control_is_not_dressed_as_a_dead_end_nav_label() -> None:
    """It sits in nav.days beside the disabled earlier/later labels.

    Those are styled by element, and `nav.days span` outranks `.theme`, so an
    unscoped rule renders an interactive control as a greyed-out dead end.
    """
    html = render_day(sheet(section()), REPORTS)

    assert "nav.days span:not(.theme)" in html
    assert "nav.days span {" not in html


def test_the_control_stays_hidden_until_a_script_can_work_it() -> None:
    html = render_day(sheet(section()), REPORTS)

    assert 'id="theme" role="group" aria-label="Colour theme" hidden' in html


def test_the_chosen_theme_is_applied_before_the_first_paint() -> None:
    """Reading storage after <body> would flash the wrong theme."""
    html = render_day(sheet(section()), REPORTS)

    assert html.index("localStorage.getItem") < html.index("<body>")


def test_storage_access_is_guarded() -> None:
    """A private window makes localStorage throw; the sheet must still render."""
    html = render_day(sheet(section()), REPORTS)

    assert html.count("try {") == html.count("catch (e) {}") >= 2
    for match in re.finditer("localStorage", html):
        prefix = html[: match.start()]
        opened = prefix.rfind("try {")
        assert opened != -1, "a storage access sits outside any try"
        assert prefix.rfind("catch (e) {}") < opened, "its try block was already closed"


# ---------------------------------------------------------------------------
# The live edge
# ---------------------------------------------------------------------------


def test_a_day_still_being_written_to_is_marked_where_it_stops() -> None:
    html = render_day(sheet(section(), now=0.604166, now_label="14:30"), REPORTS)

    assert 'class="now" style="left:60.4166%"' in html
    assert "updated to here" in html
    assert 'title="Updated to 14:30"' in html


def test_the_marker_is_one_line_across_a_whole_group() -> None:
    """One overlay per section, not one per lane: it has to read as a single cut."""
    rows = (Row(label="Front Door", tracks=(TRACK,)), Row(label="Drive", tracks=(TRACK,)))
    group = Section(key="front", name="Front", rows=rows, totals={MOTION: 3})

    html = render_day(sheet(group, now=0.5), REPORTS)

    assert html.count('class="nowwrap"') == 1
    assert html.count('class="plot"') == 1
    # Drawn after the rows it crosses, so nothing in a lane paints over it.
    assert html.index('class="nowwrap"') > html.index('class="row"')


def test_a_finished_day_has_no_marker() -> None:
    html = render_day(sheet(section()), REPORTS)

    assert 'class="now"' not in html
    assert "updated to here" not in html


# ---------------------------------------------------------------------------
# The hover readout
# ---------------------------------------------------------------------------


def test_every_group_carries_a_readout_over_its_lanes_and_its_axis() -> None:
    """One chart per section, with the rule and its label inside it."""
    html = render_day(sheet(section("Front"), section("Back")), REPORTS)

    assert html.count('class="chart"') == 2
    assert html.count('class="hoverwrap"') == 2
    assert html.count('class="hover"') == 2
    assert html.count('class="hovertip"') == 2
    # Inside the chart, after the lanes and the axis it is drawn across.
    chart = html[html.index('class="chart"') :]
    assert chart.index('class="hoverwrap"') > chart.index('class="axis"')


def test_the_readout_is_told_how_long_the_day_was() -> None:
    """A 24-hour day and no clock changes: the mapping is a plain multiply."""
    html = render_day(sheet(section()), REPORTS)

    assert 'data-seconds="86400"' in html
    assert 'data-shifts="[]"' in html


def test_a_clock_change_travels_to_the_readout() -> None:
    page = DaySheet(
        day=DAY,
        sections=(section(),),
        generated_at=NOW,
        axis=AXIS,
        day_seconds=90_000.0,
        shifts=((0.08, -3600.0),),
    )

    html = render_day(page, REPORTS)

    assert 'data-seconds="90000"' in html
    assert 'data-shifts="[[0.080000,-3600]]"' in html


def test_the_readout_never_swallows_a_segments_own_tooltip() -> None:
    html = render_day(sheet(section()), REPORTS)

    assert "pointer-events: none" in html


def test_the_readout_is_hidden_until_a_pointer_finds_it() -> None:
    """Nothing to see on a sheet opened without a pointer, or without script."""
    html = render_day(sheet(section()), REPORTS)

    assert ".hoverwrap { position: absolute; inset: 0; pointer-events: none;" in html
    assert "visibility: hidden" in html
    assert ".chart.hot .hoverwrap { visibility: visible; }" in html


# ---------------------------------------------------------------------------
# The cards at the top
# ---------------------------------------------------------------------------


def test_each_card_links_down_to_the_group_it_counts() -> None:
    html = render_day(sheet(section()), REPORTS)

    assert '<a class="card" href="#front">' in html
    assert '<section class="group" id="front">' in html


def test_a_card_link_survives_a_group_name_that_needs_escaping() -> None:
    odd = Section(
        key='front"&',
        name="Front",
        rows=(Row(label="", tracks=(TRACK,)),),
        totals={MOTION: 3},
    )

    html = render_day(sheet(odd), REPORTS)

    assert 'href="#front&quot;&amp;"' in html
    assert 'id="front&quot;&amp;"' in html


def test_the_index_has_no_cards_to_link_from() -> None:
    """Only a day page has groups to jump to; the index is a table of days."""
    entry = IndexEntry(
        day=DAY,
        href=day_filename(DAY),
        cells={"front": IndexCell(group_key="front", group_name="Front", counts={MOTION: 3})},
    )

    html = render_index((entry,), (("front", "Front"),), REPORTS, NOW)

    assert 'class="card"' not in html
