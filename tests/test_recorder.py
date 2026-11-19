from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from unifi_occupancy.metrics import Metrics
from unifi_occupancy.models import MOTION, PERSON, Camera
from unifi_occupancy.recorder import EventRecorder
from unifi_occupancy.state import CameraState
from unifi_occupancy.store import EventStore
from unifi_occupancy.triggers import CLEAR, MATCH, UNKNOWN, TriggerResult

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
CAMERA = Camera(id="cam-1", name="Front Door")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def state(**detections: bool) -> CameraState:
    return CameraState(
        camera=CAMERA,
        detections={MOTION: False, PERSON: False, **detections},
        enabled={MOTION: True, PERSON: True},
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    with EventStore(tmp_path / "history.sqlite3") as opened:
        yield opened


def test_an_edge_opens_and_closes_one_event(store: EventStore) -> None:
    clock = Clock()
    recorder = EventRecorder(store, clock=clock)

    recorder.observe(state(motion=True))
    clock.advance(45)
    recorder.observe(state(motion=False))

    (event,) = store.events_between(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert event.kind == MOTION
    assert event.started_at == T0
    assert event.duration(clock.now) == 45.0


def test_repeated_snapshots_do_not_pile_up_events(store: EventStore) -> None:
    clock = Clock()
    recorder = EventRecorder(store, clock=clock)

    for _ in range(5):
        recorder.observe(state(motion=True))
        clock.advance(5)
    recorder.observe(state(motion=False))

    events = store.events_between(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert len(events) == 1


def test_detections_are_recorded_independently(store: EventStore) -> None:
    clock = Clock()
    recorder = EventRecorder(store, clock=clock)

    recorder.observe(state(motion=True, person=True))
    clock.advance(10)
    recorder.observe(state(motion=True, person=False))

    kinds = {
        event.kind: event
        for event in store.events_between(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    }
    assert kinds[PERSON].ended_at == T0 + timedelta(seconds=10)
    assert kinds[MOTION].ended_at is None


def test_a_departed_camera_has_its_events_closed(store: EventStore) -> None:
    clock = Clock()
    recorder = EventRecorder(store, clock=clock)

    recorder.observe(state(motion=True))
    clock.advance(30)
    recorder.forget(frozenset({CAMERA.id}))

    (event,) = store.events_between(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert event.ended_at == T0 + timedelta(seconds=30)


def test_a_failing_store_does_not_propagate(caplog: pytest.LogCaptureFixture) -> None:
    class Broken:
        def start_event(self, *args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        def record_camera(self, *args: object, **kwargs: object) -> None:
            raise OSError("disk full")

    recorder = EventRecorder(Broken(), clock=Clock())  # type: ignore[arg-type]

    recorder.observe(state(motion=True))
    recorder.observe(state(motion=True))

    assert "could not record detection history" in caplog.text
    assert caplog.text.count("could not record detection history") == 1
    # The roster and the event log are complained about once each: neither
    # failing is allowed to swallow the other's one warning.
    assert caplog.text.count("could not record the camera roster") == 1


def test_edges_and_failures_reach_the_metrics(store: EventStore) -> None:
    metrics = Metrics()
    recorder = EventRecorder(store, clock=Clock(), metrics=metrics)

    recorder.observe(state(**{PERSON: True}))
    recorder.observe(state(**{PERSON: False}))

    assert metrics.recorded_edges.value(kind=PERSON, edge="start") == 1
    assert metrics.recorded_edges.value(kind=PERSON, edge="end") == 1
    assert metrics.recorder_failures.value(operation="event") == 0

    store.close()
    recorder.observe(state(**{MOTION: True}))

    assert metrics.recorder_failures.value(operation="event") == 1


def result(status: str, trigger: str = "fire_or_smoke") -> TriggerResult:
    return TriggerResult(trigger=trigger, name="Fire or smoke", status=status)


def test_a_trigger_match_opens_an_event_and_a_clear_closes_it(store: EventStore) -> None:
    clock = Clock()
    recorder = EventRecorder(store, clock=clock)

    recorder.record_trigger(CAMERA, result(MATCH))
    clock.advance(600)
    recorder.record_trigger(CAMERA, result(CLEAR))

    (event,) = store.events_between(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert event.kind == "trigger:fire_or_smoke"
    assert event.camera_id == CAMERA.id
    assert event.started_at == T0
    assert event.duration(clock.now) == 600.0


def test_an_unknown_answer_leaves_the_history_alone(store: EventStore) -> None:
    """The same rule the boolean follows: no answer is not an all-clear."""
    clock = Clock()
    recorder = EventRecorder(store, clock=clock)

    recorder.record_trigger(CAMERA, result(MATCH))
    clock.advance(60)
    recorder.record_trigger(CAMERA, result(UNKNOWN))

    (event,) = store.events_between(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert event.ended_at is None

    clock.advance(60)
    recorder.record_trigger(CAMERA, result(CLEAR))
    (event,) = store.events_between(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert event.duration(clock.now) == 120.0


def test_an_unknown_answer_does_not_open_an_event_either(store: EventStore) -> None:
    recorder = EventRecorder(store, clock=Clock())

    recorder.record_trigger(CAMERA, result(UNKNOWN))

    assert store.events_between(T0 - timedelta(hours=1), T0 + timedelta(hours=1)) == []


def test_repeated_matches_do_not_pile_up_trigger_events(store: EventStore) -> None:
    clock = Clock()
    recorder = EventRecorder(store, clock=clock)

    for _ in range(4):
        recorder.record_trigger(CAMERA, result(MATCH))
        clock.advance(300)

    (event,) = store.events_between(T0 - timedelta(hours=1), T0 + timedelta(hours=2))
    assert event.started_at == T0


def test_two_triggers_on_one_camera_get_a_row_each(store: EventStore) -> None:
    recorder = EventRecorder(store, clock=Clock())

    recorder.record_trigger(CAMERA, result(MATCH, trigger="fire_or_smoke"))
    recorder.record_trigger(CAMERA, result(MATCH, trigger="gate_open"))

    kinds = {
        event.kind
        for event in store.events_between(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    }
    assert kinds == {"trigger:fire_or_smoke", "trigger:gate_open"}


def test_a_trigger_match_keeps_its_camera_on_the_roster(store: EventStore) -> None:
    """A camera only a trigger ever reports on still belongs on the sheet."""
    recorder = EventRecorder(store, clock=Clock())

    recorder.record_trigger(CAMERA, result(MATCH))

    assert store.roster() == [(CAMERA.id, CAMERA.name)]


def test_a_trigger_edge_is_counted_under_its_own_kind(store: EventStore) -> None:
    metrics = Metrics()
    recorder = EventRecorder(store, clock=Clock(), metrics=metrics)

    recorder.record_trigger(CAMERA, result(MATCH))

    assert metrics.recorded_edges.value(kind="trigger:fire_or_smoke", edge="start") == 1.0


def test_a_broken_store_does_not_break_a_sweep(store: EventStore) -> None:
    class Broken:
        def record_camera(self, *args: object, **kwargs: object) -> None:
            raise OSError("no space left on device")

        def start_event(self, *args: object, **kwargs: object) -> None:
            raise OSError("no space left on device")

    recorder = EventRecorder(Broken(), clock=Clock())  # type: ignore[arg-type]

    recorder.record_trigger(CAMERA, result(MATCH))
