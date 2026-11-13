from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from unifi_occupancy.config import ReportConfig, parse_report_config
from unifi_occupancy.models import MOTION, PERSON, SMOKE, Camera
from unifi_occupancy.report import SheetWriter, day_window, next_refresh
from unifi_occupancy.store import EventStore

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 20)
NOON = datetime(2026, 10, 20, 12, 0, tzinfo=NY)

CAMERAS = (
    Camera(id="cam-1", name="Front Door"),
    Camera(id="cam-2", name="Driveway"),
    Camera(id="cam-3", name="Side Gate"),
)

CONFIG = """
timezone: America/New_York
reports:
  bucket_minutes: 60
groups:
  - name: Front of house
    cameras: [Front Door, Driveway]
"""


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    with EventStore(tmp_path / "history.sqlite3") as opened:
        yield opened


@pytest.fixture
def reports(tmp_path: Path) -> ReportConfig:
    parsed = parse_report_config(CONFIG)
    return ReportConfig(
        **{
            **parsed.__dict__,
            "output_dir": tmp_path / "www",
            "database": tmp_path / "history.sqlite3",
        }
    )


@pytest.fixture
def writer(store: EventStore, reports: ReportConfig) -> SheetWriter:
    return SheetWriter(
        store,
        reports,
        cameras=lambda: CAMERAS,
        clock=lambda: NOON.astimezone(UTC),
    )


def record(store: EventStore, camera: Camera, kind: str, at: datetime, seconds: float) -> None:
    store.start_event(camera.id, camera.name, kind, at.astimezone(UTC))
    store.end_event(camera.id, kind, (at + timedelta(seconds=seconds)).astimezone(UTC))


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 20, hour, minute, tzinfo=NY)


# -- day boundaries ---------------------------------------------------------


def test_a_day_runs_from_local_midnight_to_local_midnight() -> None:
    start, end = day_window(DAY, NY)

    assert start.astimezone(NY).hour == 0
    assert (end - start) == timedelta(hours=24)


@pytest.mark.parametrize(
    ("day", "hours"),
    [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25), (date(2026, 6, 1), 24)],
)
def test_clock_changes_make_the_day_shorter_or_longer(day: date, hours: int) -> None:
    start, end = day_window(day, NY)

    assert (end - start).total_seconds() / 3600 == hours


# -- the refresh schedule ---------------------------------------------------


def test_an_hourly_refresh_lands_on_the_hour(reports: ReportConfig) -> None:
    wake = next_refresh(at(14, 37).astimezone(UTC), reports)

    assert wake.astimezone(NY) == at(15, 0)


def test_the_last_refresh_of_the_day_is_local_midnight(reports: ReportConfig) -> None:
    wake = next_refresh(at(23, 30).astimezone(UTC), reports)

    assert wake.astimezone(NY) == datetime(2026, 10, 21, 0, 0, tzinfo=NY)


def test_a_shorter_interval_subdivides_the_hour(reports: ReportConfig) -> None:
    half_hourly = ReportConfig(**{**reports.__dict__, "refresh_minutes": 30})

    assert next_refresh(at(14, 10).astimezone(UTC), half_hourly).astimezone(NY) == at(14, 30)
    assert next_refresh(at(14, 40).astimezone(UTC), half_hourly).astimezone(NY) == at(15, 0)


def test_the_next_refresh_is_always_in_the_future(reports: ReportConfig) -> None:
    on_the_hour = at(15, 0).astimezone(UTC)

    assert next_refresh(on_the_hour, reports) > on_the_hour


# -- grouping ---------------------------------------------------------------


def test_cameras_are_grouped_and_strays_stand_alone(writer: SheetWriter) -> None:
    _, sections, _, _ = writer._build(DAY)

    assert [section.name for section in sections] == ["Front of house", "Side Gate"]
    assert [row.label for row in sections[0].rows] == ["All cameras", "Front Door", "Driveway"]


def test_a_lone_camera_gets_no_redundant_label(writer: SheetWriter) -> None:
    _, sections, _, _ = writer._build(DAY)

    (row,) = sections[1].rows
    assert row.label == ""
    assert row.aggregate is False


def test_a_camera_can_be_named_by_slug_or_id(store: EventStore, reports: ReportConfig) -> None:
    by_slug = ReportConfig(
        **{
            **reports.__dict__,
            "groups": parse_report_config(
                "groups:\n  - name: Mixed\n    cameras: [front_door, cam-2]\n"
            ).groups,
        }
    )
    _, sections, _, _ = SheetWriter(
        store, by_slug, cameras=lambda: CAMERAS, clock=lambda: NOON.astimezone(UTC)
    )._build(DAY)

    assert [row.label for row in sections[0].rows] == ["All cameras", "Front Door", "Driveway"]


def test_an_unmatched_camera_name_is_reported_once(
    store: EventStore, reports: ReportConfig, caplog: pytest.LogCaptureFixture
) -> None:
    typo = ReportConfig(
        **{
            **reports.__dict__,
            "groups": parse_report_config(
                "groups:\n  - name: Front\n    cameras: [Frnot Door]\n"
            ).groups,
        }
    )
    sheet = SheetWriter(store, typo, cameras=lambda: CAMERAS, clock=lambda: NOON.astimezone(UTC))

    sheet._build(DAY)
    sheet._build(DAY)

    assert caplog.text.count("matches no camera") == 1
    assert "Frnot Door" in caplog.text


def test_a_camera_only_in_the_history_still_appears(
    store: EventStore, reports: ReportConfig
) -> None:
    """A camera removed from Protect keeps its place on the days it was there."""
    record(store, Camera(id="cam-9", name="Old Shed"), MOTION, at(9), 30)
    _, sections, _, _ = SheetWriter(
        store, reports, cameras=lambda: (), clock=lambda: NOON.astimezone(UTC)
    )._build(DAY)

    assert [section.name for section in sections] == ["Front of house", "Old Shed"]


# -- the arithmetic ---------------------------------------------------------


def test_events_are_counted_and_placed(writer: SheetWriter, store: EventStore) -> None:
    record(store, CAMERAS[0], MOTION, at(2, 30), 60)
    record(store, CAMERAS[0], MOTION, at(2, 45), 60)
    record(store, CAMERAS[0], MOTION, at(20), 60)

    _, sections, peak, _ = writer._build(DAY)
    track = next(t for t in sections[0].rows[1].tracks if t.kind == MOTION)

    assert track.events == 3
    assert track.seconds == 180.0
    assert track.buckets[2] == 2  # both of the 02:xx events, one hourly bucket
    assert track.buckets[20] == 1
    assert peak == 2


def test_a_group_does_not_count_overlapping_time_twice(
    writer: SheetWriter, store: EventStore
) -> None:
    record(store, CAMERAS[0], MOTION, at(9), 60)
    record(store, CAMERAS[1], MOTION, at(9), 60)

    _, sections, _, _ = writer._build(DAY)
    total = next(t for t in sections[0].rows[0].tracks if t.kind == MOTION)

    assert total.events == 2
    assert total.seconds == 60.0  # one minute of activity, seen by two cameras
    assert len(total.segments) == 1


def test_an_event_from_yesterday_contributes_time_but_not_a_count(
    writer: SheetWriter, store: EventStore
) -> None:
    store.start_event("cam-1", "Front Door", MOTION, at(0).astimezone(UTC) - timedelta(minutes=5))
    store.end_event("cam-1", MOTION, at(0).astimezone(UTC) + timedelta(minutes=5))

    _, sections, _, _ = writer._build(DAY)
    track = next(t for t in sections[0].rows[1].tracks if t.kind == MOTION)

    assert track.events == 0
    assert track.seconds == pytest.approx(300.0)
    assert track.segments[0].start == 0.0


def test_a_live_event_is_drawn_only_up_to_now(writer: SheetWriter, store: EventStore) -> None:
    store.start_event("cam-1", "Front Door", MOTION, at(11, 30).astimezone(UTC))

    _, sections, _, _ = writer._build(DAY)
    track = next(t for t in sections[0].rows[1].tracks if t.kind == MOTION)

    assert track.seconds == pytest.approx(1800.0)  # 11:30 to the 12:00 "now"
    assert track.segments[-1].end == pytest.approx(0.5)


def test_motion_and_person_always_get_a_track(writer: SheetWriter) -> None:
    _, sections, _, _ = writer._build(DAY)

    assert [track.kind for track in sections[0].rows[0].tracks] == [MOTION, PERSON]


def test_other_detections_appear_only_when_they_fire(
    writer: SheetWriter, store: EventStore
) -> None:
    record(store, CAMERAS[0], SMOKE, at(3), 120)

    _, sections, _, _ = writer._build(DAY)

    assert [track.kind for track in sections[0].rows[0].tracks] == [MOTION, PERSON, SMOKE]
    assert [track.kind for track in sections[1].rows[0].tracks] == [MOTION, PERSON]


# -- writing the files ------------------------------------------------------


def test_a_refresh_writes_today_and_the_index(
    writer: SheetWriter, reports: ReportConfig, store: EventStore
) -> None:
    record(store, CAMERAS[0], MOTION, at(9), 30)

    writer.refresh()

    assert (reports.output_dir / "2026-10-20.html").is_file()
    assert (reports.output_dir / "index.html").is_file()
    assert "Front of house" in (reports.output_dir / "index.html").read_text()


def test_a_refresh_after_midnight_finalises_the_day_that_ended(
    store: EventStore, reports: ReportConfig
) -> None:
    record(store, CAMERAS[0], MOTION, at(23, 30), 30)
    midnight = datetime(2026, 10, 21, 0, 0, tzinfo=NY)

    SheetWriter(
        store, reports, cameras=lambda: CAMERAS, clock=lambda: midnight.astimezone(UTC)
    ).refresh()

    assert (reports.output_dir / "2026-10-20.html").is_file()
    assert (reports.output_dir / "2026-10-21.html").is_file()
    assert store.summarised_days() == ["2026-10-21", "2026-10-20"]


def test_a_quiet_yesterday_is_not_conjured_up(writer: SheetWriter, reports: ReportConfig) -> None:
    writer.refresh()

    assert (reports.output_dir / "2026-10-20.html").is_file()
    assert not (reports.output_dir / "2026-10-19.html").exists()


def test_nothing_serves_a_half_written_page(writer: SheetWriter, reports: ReportConfig) -> None:
    writer.refresh()

    assert [path.name for path in reports.output_dir.iterdir() if path.name.startswith(".")] == []


def test_the_index_counts_every_group_for_every_day(
    writer: SheetWriter, store: EventStore, reports: ReportConfig
) -> None:
    record(store, CAMERAS[0], MOTION, at(9), 30)
    record(store, CAMERAS[1], MOTION, at(10), 30)
    record(store, CAMERAS[2], MOTION, at(11), 30)
    record(store, CAMERAS[0], PERSON, at(9), 20)

    writer.refresh()
    index = (reports.output_dir / "index.html").read_text()

    assert "<th>Front of house</th>" in index
    assert "<th>Side Gate</th>" in index
    assert "<td>2 / 1</td>" in index  # the group: two motion, one person
    assert '<a href="2026-10-20.html">' in index


def test_rebuilding_re_renders_every_recorded_day(store: EventStore, reports: ReportConfig) -> None:
    for offset in range(3):
        when = at(9) - timedelta(days=offset)
        store.start_event("cam-1", "Front Door", MOTION, when.astimezone(UTC))
        store.end_event("cam-1", MOTION, (when + timedelta(seconds=30)).astimezone(UTC))

    days = SheetWriter(
        store, reports, cameras=lambda: CAMERAS, clock=lambda: NOON.astimezone(UTC)
    ).rebuild()

    assert days == 3
    assert sorted(path.name for path in reports.output_dir.iterdir()) == [
        "2026-10-18.html",
        "2026-10-19.html",
        "2026-10-20.html",
        "index.html",
    ]


def test_retention_drops_old_events_but_keeps_their_counts(
    store: EventStore, reports: ReportConfig
) -> None:
    kept = ReportConfig(**{**reports.__dict__, "retention_days": 7})
    old = at(9) - timedelta(days=30)
    store.start_event("cam-1", "Front Door", MOTION, old.astimezone(UTC))
    store.end_event("cam-1", MOTION, (old + timedelta(seconds=30)).astimezone(UTC))
    sheet = SheetWriter(store, kept, cameras=lambda: CAMERAS, clock=lambda: NOON.astimezone(UTC))
    sheet.write_day(old.date())

    sheet.refresh()

    assert store.events_between(old - timedelta(days=1), old + timedelta(days=1)) == []
    assert "2026-09-20" in store.summarised_days()
    assert "2026-09-20" in (reports.output_dir / "index.html").read_text()


# ---------------------------------------------------------------------------
# Clock-change days, pruned days and neighbour links
# ---------------------------------------------------------------------------

FALL_BACK = date(2026, 11, 1)  # 25 hours in New York
SPRING_FORWARD = date(2026, 3, 8)  # 23 hours


def writer_for(store: EventStore, reports: ReportConfig, now: datetime) -> SheetWriter:
    return SheetWriter(store, reports, cameras=lambda: CAMERAS, clock=lambda: now.astimezone(UTC))


@pytest.mark.parametrize("day", [FALL_BACK, SPRING_FORWARD, date(2026, 6, 1)])
def test_shading_lines_up_with_the_bars_drawn_over_it(
    store: EventStore, reports: ReportConfig, day: date
) -> None:
    """A clock-change day is not 1440 minutes, so noon is not halfway through it."""
    for minute in (0, 360, 720, 1080):
        at = datetime.combine(day, time(minute // 60, 0), tzinfo=NY)
        store.start_event("cam-1", "Front Door", MOTION, at.astimezone(UTC))
        store.end_event("cam-1", MOTION, (at + timedelta(minutes=1)).astimezone(UTC))
    end_of_day = datetime.combine(day, time(23, 59), tzinfo=NY)

    window, sections, _, _ = writer_for(store, reports, end_of_day)._build(day)
    bands = writer_for(store, reports, end_of_day)._bands(window)
    bars = next(t for t in sections[0].rows[1].tracks if t.kind == MOTION).segments

    assert len(bars) == 4
    for band, bar in zip(bands, bars, strict=True):
        assert band.start == pytest.approx(bar.start, abs=1e-9)


def test_an_ordinary_day_still_shades_on_the_quarters(
    store: EventStore, reports: ReportConfig
) -> None:
    window, _, _, _ = writer_for(store, reports, NOON)._build(DAY)

    bands = writer_for(store, reports, NOON)._bands(window)

    assert [band.start for band in bands] == [0.0, 0.25, 0.5, 0.75]
    assert bands[-1].end == 1.0


def test_the_hour_axis_follows_the_same_stretched_day(
    store: EventStore, reports: ReportConfig
) -> None:
    end_of_day = datetime.combine(FALL_BACK, time(23, 59), tzinfo=NY)
    window, _, _, _ = writer_for(store, reports, end_of_day)._build(FALL_BACK)

    axis = writer_for(store, reports, end_of_day)._axis(window)

    assert axis[0].at == 0.0
    assert axis[-1].label == "24"
    assert axis[-1].at == 1.0
    # 12:00 on a 25-hour day falls 13 hours in, not 12.
    assert next(mark.at for mark in axis if mark.label == "12") == pytest.approx(13 / 25)


def test_periods_from_the_config_file_reach_the_bands(
    store: EventStore, reports: ReportConfig
) -> None:
    owlish = ReportConfig(
        **{
            **reports.__dict__,
            "periods": parse_report_config(
                'periods:\n  - {name: Owl hours, start: "00:00"}\n'
                '  - {name: Daylight, start: "07:00"}\n'
            ).periods,
        }
    )
    window, _, _, _ = writer_for(store, owlish, NOON)._build(DAY)

    bands = writer_for(store, owlish, NOON)._bands(window)

    assert [band.name for band in bands] == ["Owl hours", "Daylight"]
    assert bands[1].start == pytest.approx(7 / 24)


def test_bands_sharing_a_name_share_a_shade(store: EventStore, reports: ReportConfig) -> None:
    """Night wrapping past midnight is one period drawn twice, not two periods."""
    wrapping = ReportConfig(
        **{
            **reports.__dict__,
            "periods": parse_report_config(
                'periods:\n  - {name: Day, start: "06:00"}\n' '  - {name: Night, start: "22:00"}\n'
            ).periods,
        }
    )
    window, _, _, _ = writer_for(store, wrapping, NOON)._build(DAY)

    bands = writer_for(store, wrapping, NOON)._bands(window)

    assert [band.name for band in bands] == ["Night", "Day", "Night"]
    assert bands[0].shade == bands[2].shade
    assert bands[0].shade != bands[1].shade


@pytest.mark.parametrize("day", [FALL_BACK, SPRING_FORWARD])
def test_every_wall_clock_hour_gets_its_refresh_on_a_clock_change_day(
    reports: ReportConfig, day: date
) -> None:
    """23 or 25 refreshes, one per hour as the hallway clock reads it."""
    cursor = datetime.combine(day, time.min, tzinfo=NY).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=NY).astimezone(UTC)

    seen: list[str] = []
    while cursor < end:
        cursor = next_refresh(cursor, reports)
        if cursor >= end:
            break
        seen.append(f"{cursor.astimezone(NY):%H:%M}")

    # Every hour from 01:00 to 23:00; midnight ends the day and stops the walk.
    assert seen == [f"{hour:02d}:00" for hour in range(1, 24)]


def test_the_day_ends_on_local_midnight_however_long_it_was(reports: ReportConfig) -> None:
    for day in (FALL_BACK, SPRING_FORWARD, date(2026, 6, 1)):
        last = datetime.combine(day, time(23, 30), tzinfo=NY)

        wake = next_refresh(last.astimezone(UTC), reports).astimezone(NY)

        assert wake == datetime.combine(day + timedelta(days=1), time.min, tzinfo=NY)


def test_rebuilding_leaves_a_pruned_day_alone(store: EventStore, reports: ReportConfig) -> None:
    """retention_days promises the counts survive; rebuild must not zero them."""
    kept = ReportConfig(**{**reports.__dict__, "retention_days": 7})
    old = at(9) - timedelta(days=30)
    for n in range(5):
        when = old + timedelta(minutes=n * 10)
        store.start_event("cam-1", "Front Door", MOTION, when.astimezone(UTC))
        store.end_event("cam-1", MOTION, (when + timedelta(seconds=30)).astimezone(UTC))
    sheet = writer_for(store, kept, NOON)
    sheet.write_day(old.date())
    before = (reports.output_dir / f"{old.date()}.html").read_text()
    sheet.refresh()

    assert sheet.rebuild() >= 1
    stored = store.summaries([old.date().isoformat()])[old.date().isoformat()]
    summary = next(
        row for row in stored if row.group_key == "front_of_house" and row.kind == MOTION
    )
    assert summary.events == 5
    assert summary.seconds == 150.0
    assert (reports.output_dir / f"{old.date()}.html").read_text() == before


def test_write_day_declines_a_pruned_day(store: EventStore, reports: ReportConfig) -> None:
    kept = ReportConfig(**{**reports.__dict__, "retention_days": 7})
    old = at(9) - timedelta(days=30)
    store.start_event("cam-1", "Front Door", MOTION, old.astimezone(UTC))
    store.end_event("cam-1", MOTION, (old + timedelta(seconds=30)).astimezone(UTC))
    sheet = writer_for(store, kept, NOON)
    sheet.write_day(old.date())
    sheet.refresh()

    assert sheet.write_day(old.date()) is None


def test_a_quiet_day_that_was_never_pruned_is_still_rendered(
    store: EventStore, reports: ReportConfig
) -> None:
    """Absence of events is only "pruned" when counts were kept for that day."""
    sheet = writer_for(store, reports, NOON)

    assert sheet.write_day(DAY) is not None
    assert sheet.write_day(DAY) is not None  # and again, now that it is summarised


def test_the_earliest_sheet_does_not_link_to_a_page_that_was_never_written(
    store: EventStore, reports: ReportConfig
) -> None:
    record(store, CAMERAS[0], MOTION, at(9), 30)

    writer_for(store, reports, NOON).refresh()
    html = (reports.output_dir / "2026-10-20.html").read_text()

    assert "2026-10-19.html" not in html
    assert not (reports.output_dir / "2026-10-19.html").exists()


def test_neighbouring_days_do_link_to_each_other(store: EventStore, reports: ReportConfig) -> None:
    for offset in range(3):
        when = at(9) - timedelta(days=offset)
        store.start_event("cam-1", "Front Door", MOTION, when.astimezone(UTC))
        store.end_event("cam-1", MOTION, (when + timedelta(seconds=30)).astimezone(UTC))

    writer_for(store, reports, NOON).rebuild()
    middle = (reports.output_dir / "2026-10-19.html").read_text()

    assert '<a href="2026-10-18.html">' in middle
    assert '<a href="2026-10-20.html">' in middle
    earliest = (reports.output_dir / "2026-10-18.html").read_text()
    assert "2026-10-17.html" not in earliest


def test_rebuild_does_not_link_forward_to_a_day_it_never_writes(
    store: EventStore, reports: ReportConfig
) -> None:
    """Today only has a page once something writes one; rebuild may not assume it."""
    record(store, CAMERAS[0], MOTION, at(9) - timedelta(days=1), 30)

    writer_for(store, reports, NOON).rebuild()
    yesterday = (reports.output_dir / "2026-10-19.html").read_text()

    assert not (reports.output_dir / "2026-10-20.html").exists()
    assert "2026-10-20.html" not in yesterday


def test_rebuild_does_link_forward_when_today_has_a_page(
    store: EventStore, reports: ReportConfig
) -> None:
    record(store, CAMERAS[0], MOTION, at(9) - timedelta(days=1), 30)
    record(store, CAMERAS[0], MOTION, at(9), 30)

    writer_for(store, reports, NOON).rebuild()
    yesterday = (reports.output_dir / "2026-10-19.html").read_text()

    assert (reports.output_dir / "2026-10-20.html").is_file()
    assert '<a href="2026-10-20.html">' in yesterday


def test_a_first_refresh_links_back_to_the_day_it_is_about_to_write(
    store: EventStore, reports: ReportConfig
) -> None:
    """Yesterday has events but no summary yet; the back-link must not wait a pass."""
    record(store, CAMERAS[0], MOTION, at(9) - timedelta(days=1), 30)
    record(store, CAMERAS[0], MOTION, at(8), 30)

    writer_for(store, reports, NOON).refresh()
    today = (reports.output_dir / "2026-10-20.html").read_text()

    assert (reports.output_dir / "2026-10-19.html").is_file()
    assert '<a href="2026-10-19.html">' in today


def test_every_neighbour_link_on_every_page_resolves(
    store: EventStore, reports: ReportConfig
) -> None:
    """The property both of those are instances of."""
    for offset in (0, 1, 3, 4):  # a deliberate gap at two days ago
        when = at(9) - timedelta(days=offset)
        store.start_event("cam-1", "Front Door", MOTION, when.astimezone(UTC))
        store.end_event("cam-1", MOTION, (when + timedelta(seconds=30)).astimezone(UTC))
    sheet = writer_for(store, reports, NOON)
    sheet.rebuild()
    sheet.refresh()

    pages = sorted(reports.output_dir.glob("*.html"))
    assert pages
    for page in pages:
        for link in re.findall(r'href="([^"]+\.html)"', page.read_text()):
            assert (reports.output_dir / link).is_file(), f"{page.name} -> {link}"


# ---------------------------------------------------------------------------
# The live edge
# ---------------------------------------------------------------------------


def test_todays_sheet_is_marked_where_the_updating_stopped(
    store: EventStore, reports: ReportConfig
) -> None:
    writer_for(store, reports, NOON).write_day(DAY)

    html = (reports.output_dir / "2026-10-20.html").read_text()

    assert 'class="now" style="left:50%"' in html  # noon, halfway through the day
    assert "Updated to 12:00" in html


def test_a_day_that_is_over_carries_no_marker(store: EventStore, reports: ReportConfig) -> None:
    store.start_event("cam-1", "Front Door", MOTION, at(9).astimezone(UTC))
    store.end_event("cam-1", MOTION, at(9, 1).astimezone(UTC))
    writer_for(store, reports, NOON + timedelta(days=1)).write_day(DAY)

    html = (reports.output_dir / "2026-10-20.html").read_text()

    assert 'class="now"' not in html


def test_the_marker_follows_the_stretched_day_like_everything_else(
    store: EventStore, reports: ReportConfig
) -> None:
    """25 hours on the fall-back day, so noon is not halfway through it."""
    noon = datetime.combine(FALL_BACK, time(12, 0), tzinfo=NY)

    writer_for(store, reports, noon).write_day(FALL_BACK)
    html = (reports.output_dir / "2026-11-01.html").read_text()

    found = re.search(r'class="now" style="left:([\d.]+)%', html)
    assert found is not None
    assert float(found.group(1)) == pytest.approx(13 / 25 * 100, abs=1e-3)


# ---------------------------------------------------------------------------
# What the hover readout is told about the day
# ---------------------------------------------------------------------------


def test_an_ordinary_day_needs_no_correction(store: EventStore, reports: ReportConfig) -> None:
    hand = writer_for(store, reports, NOON)
    window, _, _, _ = hand._build(DAY)

    assert window.seconds == 86_400
    assert hand._shifts(window) == ()


def test_the_morning_the_clocks_go_back_is_corrected_once(
    store: EventStore, reports: ReportConfig
) -> None:
    end_of_day = datetime.combine(FALL_BACK, time(23, 59), tzinfo=NY)
    hand = writer_for(store, reports, end_of_day)
    window, _, _, _ = hand._build(FALL_BACK)

    shifts = hand._shifts(window)

    assert window.seconds == 90_000
    assert len(shifts) == 1
    at_fraction, delta = shifts[0]
    assert delta == -3600
    # 02:00 EDT is two hours into a 25-hour day.
    assert at_fraction == pytest.approx(2 / 25)


def test_the_morning_the_clocks_go_forward_is_corrected_once(
    store: EventStore, reports: ReportConfig
) -> None:
    end_of_day = datetime.combine(SPRING_FORWARD, time(23, 59), tzinfo=NY)
    hand = writer_for(store, reports, end_of_day)
    window, _, _, _ = hand._build(SPRING_FORWARD)

    shifts = hand._shifts(window)

    assert window.seconds == 82_800
    assert shifts == ((pytest.approx(2 / 23), 3600),)


@pytest.mark.parametrize("day", [DAY, FALL_BACK, SPRING_FORWARD])
def test_the_readout_reads_back_the_wall_clock(
    store: EventStore, reports: ReportConfig, day: date
) -> None:
    """The arithmetic the page's script does, done here against the real zone.

    Walked over instants rather than over labelled hours, because on the
    morning the clocks go forward there is no 02:30 to ask about. Every
    position on a lane must read back as the time the hallway clock showed at
    that moment -- that is the whole contract between the page and this.
    """
    end_of_day = datetime.combine(day, time(23, 59), tzinfo=NY)
    hand = writer_for(store, reports, end_of_day)
    window, _, _, _ = hand._build(day)
    shifts = hand._shifts(window)

    step = timedelta(minutes=30)
    moment = window.start
    while moment < window.end:
        fraction = window.fraction(moment)
        elapsed = fraction * window.seconds
        for at_fraction, delta in shifts:
            if fraction >= at_fraction:
                elapsed += delta
        local = moment.astimezone(NY)
        expected = local.hour * 3600 + local.minute * 60 + local.second
        assert round(elapsed) % 86_400 == expected, f"{day} {local:%H:%M}"
        moment += step


def test_the_day_written_out_tells_its_sheet_how_long_it_was(
    store: EventStore, reports: ReportConfig
) -> None:
    end_of_day = datetime.combine(FALL_BACK, time(23, 59), tzinfo=NY)
    record(store, CAMERAS[0], MOTION, datetime.combine(FALL_BACK, time(9), tzinfo=NY), 30)

    page = writer_for(store, reports, end_of_day).write_day(FALL_BACK)

    assert page is not None
    html = page.read_text(encoding="utf-8")
    assert 'data-seconds="90000"' in html
    assert 'data-shifts="[[0.080000,-3600]]"' in html


def test_a_quiet_camera_is_still_listed(store: EventStore, reports: ReportConfig) -> None:
    """Nothing happening is a result, not a reason to leave a camera off."""
    _, sections, _, _ = SheetWriter(
        store, reports, cameras=lambda: CAMERAS, clock=lambda: NOON.astimezone(UTC)
    )._build(DAY)

    assert [section.name for section in sections] == ["Front of house", "Side Gate"]
    assert [row.label for row in sections[0].rows] == ["All cameras", "Front Door", "Driveway"]
    assert all(
        track.events == 0 for section in sections for row in section.rows for track in row.tracks
    )


def test_the_stored_roster_carries_quiet_cameras_without_a_controller(
    store: EventStore, reports: ReportConfig
) -> None:
    """A rebuild has no live camera list, and the event log knows only the noisy ones."""
    for camera in CAMERAS:
        store.record_camera(camera.id, camera.name, NOON.astimezone(UTC))

    # No `cameras` callable at all: this is what `--rebuild-sheets` constructs.
    _, sections, _, _ = SheetWriter(store, reports, clock=lambda: NOON.astimezone(UTC))._build(DAY)

    assert [section.name for section in sections] == ["Front of house", "Side Gate"]
    assert [row.label for row in sections[0].rows] == ["All cameras", "Front Door", "Driveway"]


def test_a_camera_removed_from_protect_leaves_the_roster(
    store: EventStore, reports: ReportConfig
) -> None:
    for camera in CAMERAS:
        store.record_camera(camera.id, camera.name, NOON.astimezone(UTC))
    store.forget_cameras(["cam-3"])

    _, sections, _, _ = SheetWriter(store, reports, clock=lambda: NOON.astimezone(UTC))._build(DAY)

    assert [section.name for section in sections] == ["Front of house"]


def test_a_removed_camera_keeps_its_place_on_a_day_it_was_there(
    store: EventStore, reports: ReportConfig
) -> None:
    """Dropping it from the roster must not rewrite the history it is in."""
    record(store, Camera(id="cam-9", name="Old Shed"), MOTION, at(9), 30)
    store.record_camera("cam-9", "Old Shed", NOON.astimezone(UTC))
    store.forget_cameras(["cam-9"])

    _, sections, _, _ = SheetWriter(store, reports, clock=lambda: NOON.astimezone(UTC))._build(DAY)

    assert "Old Shed" in [section.name for section in sections]


def test_the_live_list_wins_over_a_stale_stored_name(
    store: EventStore, reports: ReportConfig
) -> None:
    store.record_camera("cam-3", "Side Gate (old)", NOON.astimezone(UTC))

    _, sections, _, _ = SheetWriter(
        store, reports, cameras=lambda: CAMERAS, clock=lambda: NOON.astimezone(UTC)
    )._build(DAY)

    assert [section.name for section in sections] == ["Front of house", "Side Gate"]
