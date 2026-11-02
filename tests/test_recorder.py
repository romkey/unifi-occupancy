from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from unifi_occupancy.models import MOTION, PERSON, Camera
from unifi_occupancy.recorder import EventRecorder
from unifi_occupancy.state import CameraState
from unifi_occupancy.store import EventStore

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

    recorder = EventRecorder(Broken(), clock=Clock())  # type: ignore[arg-type]

    recorder.observe(state(motion=True))
    recorder.observe(state(motion=True))

    assert "could not record detection history" in caplog.text
    assert caplog.text.count("could not record detection history") == 1
