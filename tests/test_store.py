from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from unifi_occupancy.models import MOTION
from unifi_occupancy.store import EventStore, Summary

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    with EventStore(tmp_path / "nested" / "history.sqlite3") as opened:
        yield opened


def test_a_detection_round_trips(store: EventStore) -> None:
    store.start_event("cam-1", "Front Door", "motion", T0)
    store.end_event("cam-1", "motion", T0 + timedelta(seconds=20))

    (event,) = store.events_between(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert event.camera_name == "Front Door"
    assert event.kind == "motion"
    assert event.ended_at is not None
    assert event.duration(T0) == 20.0


def test_a_second_start_does_not_open_a_second_event(store: EventStore) -> None:
    store.start_event("cam-1", "Front Door", "motion", T0)
    store.start_event("cam-1", "Front Door", "motion", T0 + timedelta(seconds=5))

    events = store.events_between(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert len(events) == 1
    assert events[0].started_at == T0


def test_kinds_and_cameras_are_tracked_separately(store: EventStore) -> None:
    store.start_event("cam-1", "Front Door", "motion", T0)
    store.start_event("cam-1", "Front Door", "person", T0)
    store.start_event("cam-2", "Driveway", "motion", T0)

    assert len(store.events_between(T0, T0 + timedelta(hours=1))) == 3


def test_a_live_event_has_no_end(store: EventStore) -> None:
    store.start_event("cam-1", "Front Door", "motion", T0)

    (event,) = store.events_between(T0, T0 + timedelta(hours=1))
    assert event.ended_at is None
    assert event.duration(T0 + timedelta(seconds=90)) == 90.0


def test_events_overlapping_the_window_are_included(store: EventStore) -> None:
    """A detection that began yesterday is part of today if it was still running."""
    store.start_event("cam-1", "Front Door", "motion", T0 - timedelta(hours=2))
    store.end_event("cam-1", "motion", T0 + timedelta(minutes=30))

    assert store.events_between(T0, T0 + timedelta(hours=1))
    assert not store.events_between(T0 + timedelta(hours=2), T0 + timedelta(hours=3))


def test_events_outside_the_window_are_excluded(store: EventStore) -> None:
    store.start_event("cam-1", "Front Door", "motion", T0)
    store.end_event("cam-1", "motion", T0 + timedelta(seconds=10))

    assert not store.events_between(T0 + timedelta(hours=1), T0 + timedelta(hours=2))
    assert not store.events_between(T0 - timedelta(hours=2), T0 - timedelta(hours=1))


def test_open_events_are_closed_at_the_hold_time(store: EventStore) -> None:
    store.start_event("cam-1", "Front Door", "motion", T0)

    assert store.close_open_events(30.0) == 1

    (event,) = store.events_between(T0, T0 + timedelta(hours=1))
    assert event.ended_at == T0 + timedelta(seconds=30)
    assert store.close_open_events(30.0) == 0


def test_a_trigger_left_open_is_cut_at_its_own_interval(store: EventStore) -> None:
    """The detection hold is the wrong ruler: it would store an hour as a blip."""
    store.start_event("cam-1", "Front Door", "motion", T0)
    store.start_event("cam-1", "Front Door", "trigger:fire_or_smoke", T0)

    assert store.close_open_events(30.0, {"trigger:fire_or_smoke": 900.0}) == 2

    ends = {
        event.kind: event.ended_at for event in store.events_between(T0, T0 + timedelta(hours=2))
    }
    assert ends["motion"] == T0 + timedelta(seconds=30)
    assert ends["trigger:fire_or_smoke"] == T0 + timedelta(minutes=15)


def test_a_kind_with_no_hold_of_its_own_falls_back_to_the_default(store: EventStore) -> None:
    """A trigger from a run that asked a question this one does not."""
    store.start_event("cam-1", "Front Door", "trigger:gone_away", T0)

    assert store.close_open_events(30.0, {"trigger:fire_or_smoke": 900.0}) == 1

    (event,) = store.events_between(T0, T0 + timedelta(hours=1))
    assert event.ended_at == T0 + timedelta(seconds=30)


def test_a_hold_reaching_past_now_is_cut_at_now(store: EventStore) -> None:
    """A row ending in the future would claim a time that has not happened."""
    kind = "trigger:fire_or_smoke"
    store.start_event("cam-1", "Front Door", kind, T0)
    restart = T0 + timedelta(minutes=2)

    assert store.close_open_events(30.0, {kind: 900.0}, now=restart) == 1

    (event,) = store.events_between(T0, T0 + timedelta(hours=1))
    assert event.ended_at == restart


def test_a_detection_is_not_future_dated_either(store: EventStore) -> None:
    store.start_event("cam-1", "Front Door", "motion", T0)
    restart = T0 + timedelta(seconds=5)

    store.close_open_events(30.0, now=restart)

    (event,) = store.events_between(T0, T0 + timedelta(hours=1))
    assert event.ended_at == restart


def test_a_restart_mid_match_leaves_two_bars_and_a_real_gap(store: EventStore) -> None:
    """The failure this guards: the first bar outlasting the one that replaced it."""
    kind = "trigger:fire_or_smoke"
    store.start_event("cam-1", "Front Door", kind, T0)
    restart = T0 + timedelta(minutes=2)
    store.close_open_events(30.0, {kind: 900.0}, now=restart)
    # The first check after startup still matches, so a second row opens.
    store.start_event("cam-1", "Front Door", kind, T0 + timedelta(minutes=3))
    store.end_event("cam-1", kind, T0 + timedelta(minutes=4))

    first, second = store.events_between(T0, T0 + timedelta(hours=1))
    assert first.ended_at == restart
    assert second.ended_at == T0 + timedelta(minutes=4)
    # Nothing is drawn over the minute nobody was looking, and nothing outlives
    # the clear that ended the condition.
    assert first.ended_at is not None and first.ended_at < second.started_at


def test_ending_an_event_that_never_started_is_harmless(store: EventStore) -> None:
    store.end_event("cam-1", "motion", T0)

    assert store.events_between(T0 - timedelta(days=1), T0 + timedelta(days=1)) == []


def test_summaries_replace_rather_than_accumulate(store: EventStore) -> None:
    store.save_summaries("2026-11-01", [Summary("front", "Front", "motion", 3, 60.0)])
    store.save_summaries("2026-11-01", [Summary("front", "Front", "motion", 5, 90.0)])

    (row,) = store.summaries()["2026-11-01"]
    assert (row.events, row.seconds) == (5, 90.0)


def test_summaries_can_be_fetched_by_day(store: EventStore) -> None:
    store.save_summaries("2026-11-01", [Summary("front", "Front", "motion", 1, 1.0)])
    store.save_summaries("2026-10-31", [Summary("front", "Front", "motion", 2, 2.0)])

    assert store.summarised_days() == ["2026-11-01", "2026-10-31"]
    assert list(store.summaries(["2026-10-31"])) == ["2026-10-31"]
    assert store.summaries([]) == {}


def test_pruning_drops_events_but_keeps_summaries(store: EventStore) -> None:
    store.start_event("cam-1", "Front Door", "motion", T0 - timedelta(days=10))
    store.end_event("cam-1", "motion", T0 - timedelta(days=10) + timedelta(seconds=5))
    store.start_event("cam-1", "Front Door", "motion", T0)
    store.save_summaries("2026-10-22", [Summary("front", "Front", "motion", 1, 5.0)])

    assert store.prune(T0 - timedelta(days=1)) == 1
    assert len(store.events_between(T0 - timedelta(days=30), T0 + timedelta(days=1))) == 1
    assert store.summarised_days() == ["2026-10-22"]


def test_span_is_none_until_something_happens(store: EventStore) -> None:
    assert store.span() is None

    store.start_event("cam-1", "Front Door", "motion", T0)
    store.start_event("cam-2", "Driveway", "motion", T0 + timedelta(hours=3))

    span = store.span()
    assert span == (T0, T0 + timedelta(hours=3))


def test_the_roster_survives_a_rename(store: EventStore) -> None:
    moment = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    store.record_camera("cam-1", "Front Door", moment)
    store.record_camera("cam-1", "Front Porch", moment + timedelta(minutes=1))

    assert store.roster() == [("cam-1", "Front Porch")]


def test_the_roster_is_sorted_by_name_regardless_of_case(store: EventStore) -> None:
    moment = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    store.record_camera("cam-2", "driveway", moment)
    store.record_camera("cam-1", "Front Door", moment)

    assert [name for _, name in store.roster()] == ["driveway", "Front Door"]


def test_forgetting_a_camera_keeps_its_events(store: EventStore) -> None:
    moment = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    store.record_camera("cam-1", "Front Door", moment)
    store.start_event("cam-1", "Front Door", MOTION, moment)

    store.forget_cameras(["cam-1"])

    assert store.roster() == []
    assert len(store.events_between(moment - timedelta(hours=1), moment + timedelta(hours=1))) == 1


def test_forgetting_nothing_is_allowed(store: EventStore) -> None:
    store.forget_cameras([])
    assert store.roster() == []
