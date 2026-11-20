from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import FakeTransport, camera_payload
from unifi_occupancy.app import OccupancyService, _open_store
from unifi_occupancy.config import Config, ReportConfig, TriggerConfig
from unifi_occupancy.metrics import Metrics
from unifi_occupancy.models import MOTION, PERSON, DetectionEvent, parse_cameras, trigger_kind
from unifi_occupancy.protect import ProtectError
from unifi_occupancy.publisher import StatePublisher
from unifi_occupancy.recorder import EventRecorder
from unifi_occupancy.store import EventStore
from unifi_occupancy.triggers import MATCH, TriggerAnswer, TriggerWatcher


class FakeProtect:
    """Stands in for :class:`ProtectClient`."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def get_cameras(self):
        self.calls += 1
        response = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return parse_cameras(response)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def service_for(config: Config, transport: FakeTransport, responses: list):
    clock = Clock()
    publisher = StatePublisher(config, transport)
    return (
        OccupancyService(config, FakeProtect(responses), publisher, clock=clock),
        clock,
    )


async def events_from(*events: DetectionEvent) -> AsyncIterator[DetectionEvent]:
    for event in events:
        yield event


async def test_refresh_publishes_every_camera(config: Config, transport: FakeTransport) -> None:
    payload = [camera_payload(), camera_payload(id="cam-2", name="Back Yard")]
    service, _ = service_for(config, transport, [payload])

    await service.refresh()

    assert transport.payload_for("unifi-occupancy/front_door/motion") == "OFF"
    assert transport.payload_for("unifi-occupancy/back_yard/motion") == "OFF"
    assert set(service.cameras) == {"cam-1", "cam-2"}


async def test_event_turns_a_sensor_on(config: Config, transport: FakeTransport) -> None:
    service, _ = service_for(config, transport, [[camera_payload()]])
    await service.refresh()

    await service.handle_events(events_from(DetectionEvent("cam-1", frozenset({PERSON}))))

    assert transport.payload_for("unifi-occupancy/front_door/person") == "ON"


async def test_detection_expires_on_the_next_poll(config: Config, transport: FakeTransport) -> None:
    service, clock = service_for(config, transport, [[camera_payload()]])
    await service.refresh()
    await service.handle_events(events_from(DetectionEvent("cam-1", frozenset({PERSON}))))
    assert transport.payload_for("unifi-occupancy/front_door/person") == "ON"

    clock.now += config.detection_hold_seconds + 1
    await service.refresh()

    assert transport.payload_for("unifi-occupancy/front_door/person") == "OFF"


async def test_events_for_unknown_cameras_are_ignored(
    config: Config, transport: FakeTransport
) -> None:
    service, _ = service_for(config, transport, [[camera_payload()]])
    await service.refresh()
    before = len(transport.messages)

    await service.handle_events(events_from(DetectionEvent("ghost", frozenset({MOTION}))))

    assert len(transport.messages) == before


async def test_removed_cameras_are_retracted(config: Config, transport: FakeTransport) -> None:
    both = [camera_payload(), camera_payload(id="cam-2", name="Back Yard")]
    service, _ = service_for(config, transport, [both, [camera_payload()]])

    await service.refresh()
    await service.refresh()

    topic = "homeassistant/binary_sensor/unifi_occupancy_back_yard/person/config"
    assert transport.payload_for(topic) is None
    assert set(service.cameras) == {"cam-1"}


async def test_poll_errors_do_not_stop_the_loop(config: Config, transport: FakeTransport) -> None:
    service, _ = service_for(config, transport, [ProtectError("temporary"), [camera_payload()]])

    task = asyncio.create_task(service.poll_forever())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if transport.sent("unifi-occupancy/front_door/motion"):
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert transport.payload_for("unifi-occupancy/front_door/motion") == "OFF"


async def test_motion_reported_by_polling_is_published(
    config: Config, transport: FakeTransport
) -> None:
    service, _ = service_for(
        config, transport, [[camera_payload()], [camera_payload(isMotionDetected=True)]]
    )

    await service.refresh()
    await service.refresh()

    assert transport.payload_for("unifi-occupancy/front_door/motion") == "ON"


# ---------------------------------------------------------------------------
# Recording history for the occupancy sheets
# ---------------------------------------------------------------------------


def recording_service(
    config: Config, transport: FakeTransport, responses: list, store: EventStore
) -> tuple[OccupancyService, Clock]:
    clock = Clock()
    service = OccupancyService(
        config,
        FakeProtect(responses),
        StatePublisher(config, transport),
        clock=clock,
        recorder=EventRecorder(store),
    )
    return service, clock


async def test_detections_become_history(
    config: Config, transport: FakeTransport, camera_json: dict, tmp_path: Path
) -> None:
    with EventStore(tmp_path / "history.sqlite3") as store:
        service, _ = recording_service(config, transport, [[camera_json]], store)
        await service.refresh()
        await service.handle_events(events_from(DetectionEvent("cam-1", frozenset({PERSON}))))

        events = store.events_between(
            datetime.now(tz=UTC) - timedelta(hours=1), datetime.now(tz=UTC) + timedelta(hours=1)
        )

    assert [event.kind for event in events] == [PERSON]
    assert events[0].camera_name == "Front Door"
    assert events[0].ended_at is None


async def test_history_closes_when_a_detection_decays(
    config: Config, transport: FakeTransport, camera_json: dict, tmp_path: Path
) -> None:
    with EventStore(tmp_path / "history.sqlite3") as store:
        service, clock = recording_service(config, transport, [[camera_json], [camera_json]], store)
        await service.refresh()
        await service.handle_events(events_from(DetectionEvent("cam-1", frozenset({PERSON}))))
        clock.advance(config.detection_hold_seconds + 1)
        await service.refresh()

        (event,) = store.events_between(
            datetime.now(tz=UTC) - timedelta(hours=1), datetime.now(tz=UTC) + timedelta(hours=1)
        )

    assert event.ended_at is not None


async def test_a_removed_camera_has_its_history_closed(
    config: Config, transport: FakeTransport, camera_json: dict, tmp_path: Path
) -> None:
    with EventStore(tmp_path / "history.sqlite3") as store:
        service, _ = recording_service(config, transport, [[camera_json], []], store)
        await service.refresh()
        await service.handle_events(events_from(DetectionEvent("cam-1", frozenset({PERSON}))))
        await service.refresh()

        (event,) = store.events_between(
            datetime.now(tz=UTC) - timedelta(hours=1), datetime.now(tz=UTC) + timedelta(hours=1)
        )

    assert event.ended_at is not None


def test_without_a_config_file_nothing_is_recorded(config: Config) -> None:
    assert _open_store(config) is None


def test_sheets_can_be_switched_off_in_the_config_file(config: Config, tmp_path: Path) -> None:
    disabled = replace(
        config, reports=ReportConfig(enabled=False, database=tmp_path / "history.sqlite3")
    )

    assert _open_store(disabled) is None
    assert not (tmp_path / "history.sqlite3").exists()


def test_a_configured_run_opens_the_store_and_tidies_up_after_a_crash(
    config: Config, tmp_path: Path
) -> None:
    database = tmp_path / "history.sqlite3"
    with EventStore(database) as previous:
        previous.start_event("cam-1", "Front Door", PERSON, datetime.now(tz=UTC))

    store = _open_store(replace(config, reports=ReportConfig(database=database)))

    assert store is not None
    try:
        (event,) = store.events_between(
            datetime.now(tz=UTC) - timedelta(hours=1), datetime.now(tz=UTC) + timedelta(hours=1)
        )
        assert event.ended_at is not None
    finally:
        store.close()


def test_a_trigger_left_open_by_a_crash_is_cut_at_its_interval(
    config: Config, tmp_path: Path
) -> None:
    """Closed with the detection hold, an hour-long match would read as 30s."""
    database = tmp_path / "history.sqlite3"
    started = datetime.now(tz=UTC) - timedelta(hours=3)
    with EventStore(database) as previous:
        previous.start_event("cam-1", "Front Door", trigger_kind("fire_or_smoke"), started)

    store = _open_store(
        replace(
            config,
            reports=ReportConfig(database=database),
            triggers=(TriggerConfig(name="Fire or smoke", prompt="?", interval_minutes=15.0),),
        )
    )

    assert store is not None
    try:
        (event,) = store.events_between(started - timedelta(hours=1), datetime.now(tz=UTC))
        assert event.ended_at == started + timedelta(minutes=15)
    finally:
        store.close()


async def test_refresh_publishes_the_service_info_topic(
    config: Config, transport: FakeTransport
) -> None:
    service, _ = service_for(config, transport, [[camera_payload()]])

    await service.refresh()

    payload = transport.payload_for("unifi-occupancy/info")
    assert payload is not None
    assert json.loads(payload)["cameras"] == 1


# -- what the service tells the metrics -------------------------------------


def instrumented(config: Config, transport: FakeTransport, responses: list):
    metrics = Metrics()
    clock = Clock()
    service = OccupancyService(
        config,
        FakeProtect(responses),
        StatePublisher(config, transport, metrics=metrics),
        clock=clock,
        metrics=metrics,
    )
    return service, metrics, clock


async def test_a_poll_records_the_inventory(config: Config, transport: FakeTransport) -> None:
    payload = [camera_payload(), camera_payload(id="cam-2", name="Back Yard", state="OFFLINE")]
    service, metrics, _ = instrumented(config, transport, [payload])

    await service.refresh()

    assert metrics.cameras.value() == 2
    assert metrics.cameras_connected.value() == 1
    assert metrics.camera_detection_active.value(camera="front_door", kind=MOTION) == 0
    assert metrics.camera_detection_enabled.value(camera="front_door", kind=MOTION) == 1


async def test_a_camera_that_leaves_takes_its_series_with_it(
    config: Config, transport: FakeTransport
) -> None:
    both = [camera_payload(), camera_payload(id="cam-2", name="Back Yard")]
    service, metrics, _ = instrumented(config, transport, [both, [camera_payload()]])

    await service.refresh()
    await service.refresh()

    assert metrics.cameras.value() == 1
    assert 'camera="back_yard"' not in metrics.render()
    assert 'camera="front_door"' in metrics.render()


async def test_events_are_counted_by_kind_and_phase(
    config: Config, transport: FakeTransport
) -> None:
    service, metrics, _ = instrumented(config, transport, [[camera_payload()]])
    await service.refresh()

    await service.handle_events(
        events_from(
            DetectionEvent("cam-1", frozenset({PERSON})),
            DetectionEvent("cam-1", frozenset({PERSON}), start=False),
        )
    )

    assert metrics.events.value(kind=PERSON, phase="start") == 1
    assert metrics.events.value(kind=PERSON, phase="end") == 1


async def test_a_detection_accumulates_seconds(config: Config, transport: FakeTransport) -> None:
    service, metrics, clock = instrumented(config, transport, [[camera_payload()]])

    await service.handle_events(events_from(DetectionEvent("cam-1", frozenset({PERSON}))))
    await service.refresh()
    clock.advance(5.0)
    await service.refresh()

    assert metrics.detections.value(camera="front_door", kind=PERSON) == 1
    assert metrics.detection_seconds.value(camera="front_door", kind=PERSON) == 5


async def test_poll_forever_records_each_run(config: Config, transport: FakeTransport) -> None:
    service, metrics, _ = instrumented(config, transport, [[camera_payload()]])

    task = asyncio.create_task(service.poll_forever())
    while metrics.job_runs.value(job="poll", outcome="success") < 1:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert metrics.protect_up.value() == 1
    assert metrics.job_in_progress.value(job="poll") == 0
    assert metrics.job_duration.count(job="poll") >= 1


async def test_a_failing_poll_is_counted_and_lowers_protect_up(
    config: Config, transport: FakeTransport
) -> None:
    service, metrics, _ = instrumented(config, transport, [ProtectError("protect is away")])

    task = asyncio.create_task(service.poll_forever())
    while metrics.job_runs.value(job="poll", outcome="error") < 1:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert metrics.protect_up.value() == 0
    assert metrics.job_runs.value(job="poll", outcome="success") == 0


class FakeDemand:
    """Stands in for a :class:`TriggerWatcher` running on demand."""

    def __init__(self) -> None:
        self.offered: list[tuple[str, tuple[str, ...]]] = []
        self.forgotten: list[str] = []

    def demand(self, camera, keys) -> bool:
        self.offered.append((camera.name, tuple(sorted(keys))))
        return True

    def forget(self, camera_ids) -> None:
        self.forgotten += list(camera_ids)


async def test_a_detection_starting_is_offered_to_the_demand_watchers(
    config: Config, transport: FakeTransport
) -> None:
    service, _ = service_for(config, transport, [[camera_payload()]])
    watcher = FakeDemand()
    service.watch_on_demand(watcher)
    await service.refresh()

    await service.handle_events(events_from(DetectionEvent("cam-1", frozenset({MOTION, PERSON}))))

    assert watcher.offered == [("Front Door", (MOTION, PERSON))]


async def test_a_detection_ending_is_not_a_reason_to_look(
    config: Config, transport: FakeTransport
) -> None:
    """The question is about what is in the frame, and that is when there is least."""
    service, _ = service_for(config, transport, [[camera_payload()]])
    watcher = FakeDemand()
    service.watch_on_demand(watcher)
    await service.refresh()

    await service.handle_events(
        events_from(DetectionEvent("cam-1", frozenset({MOTION}), start=False))
    )

    assert watcher.offered == []


async def test_a_detection_for_a_camera_we_do_not_know_is_not_offered(
    config: Config, transport: FakeTransport
) -> None:
    service, _ = service_for(config, transport, [[camera_payload()]])
    watcher = FakeDemand()
    service.watch_on_demand(watcher)
    await service.refresh()

    await service.handle_events(events_from(DetectionEvent("ghost", frozenset({MOTION}))))

    assert watcher.offered == []


async def test_a_camera_that_goes_away_loses_its_cooldown(
    config: Config, transport: FakeTransport
) -> None:
    service, _ = service_for(
        config,
        transport,
        [[camera_payload(), camera_payload(id="cam-2", name="Back Yard")], [camera_payload()]],
    )
    watcher = FakeDemand()
    service.watch_on_demand(watcher)

    await service.refresh()
    assert watcher.forgotten == []
    await service.refresh()

    assert watcher.forgotten == ["cam-2"]


def test_a_demand_trigger_left_open_is_cut_at_its_cooldown(config: Config, tmp_path: Path) -> None:
    """Its own ruler: a trigger nothing triggered is a trigger nobody asked."""
    database = tmp_path / "history.sqlite3"
    started = datetime.now(tz=UTC) - timedelta(hours=3)
    with EventStore(database) as previous:
        previous.start_event("cam-1", "Front Door", trigger_kind("fire_or_smoke"), started)

    store = _open_store(
        replace(
            config,
            reports=ReportConfig(database=database),
            triggers=(
                TriggerConfig(
                    name="Fire or smoke",
                    prompt="?",
                    interval_minutes=15.0,
                    on_events=(MOTION,),
                    cooldown_minutes=2.0,
                ),
            ),
        )
    )

    assert store is not None
    try:
        (event,) = store.events_between(started - timedelta(hours=1), datetime.now(tz=UTC))
        assert event.ended_at == started + timedelta(minutes=2)
    finally:
        store.close()


class StillSnapshots:
    async def get_snapshot(self, camera_id: str, *, high_quality: bool = False) -> bytes:
        return b"\xff\xd8\xff\xe0 a frame"


class SayingYes:
    async def evaluate(self, image: bytes, **kwargs) -> TriggerAnswer:
        return TriggerAnswer(MATCH, "flames on the doormat")

    async def evaluate_batch(self, image: bytes, triggers, **kwargs):
        return {trigger.key: TriggerAnswer(MATCH) for trigger in triggers}


async def test_a_detection_carries_all_the_way_to_a_published_answer(
    config: Config, transport: FakeTransport
) -> None:
    """The whole path: a Protect event, a vision call, a boolean on the broker.

    The wiring in ``_serve`` that starts this loop is the one part no test
    reaches, so the contract either side of it is pinned here instead: the
    service offers the detection, and the watcher turns it into a publish.
    """
    clock = Clock()
    publisher = StatePublisher(config, transport)
    service = OccupancyService(config, FakeProtect([[camera_payload()]]), publisher, clock=clock)
    watcher = TriggerWatcher(
        TriggerConfig(name="Fire", prompt="?", on_events=(MOTION,), cooldown_minutes=5.0),
        StillSnapshots(),
        publisher,
        SayingYes(),
        clock=clock,
    )
    service.watch_on_demand(watcher)
    topic = "unifi-occupancy/front_door/trigger/fire"
    await service.refresh()
    # Nothing has looked yet, so the sensor has no state at all.
    assert not transport.sent(topic)

    await service.handle_events(events_from(DetectionEvent("cam-1", frozenset({MOTION}))))
    loop = asyncio.create_task(watcher.run_on_demand(lambda: list(service.cameras.values())))
    try:
        for _ in range(500):
            if transport.sent(topic):
                break
            await asyncio.sleep(0)

        assert transport.payload_for(topic) == "ON"

        # And the burst behind it costs nothing: one answer, not two.
        clock.advance(10.0)
        await service.handle_events(events_from(DetectionEvent("cam-1", frozenset({MOTION}))))
        for _ in range(100):
            await asyncio.sleep(0)
        assert transport.topics.count(topic) == 1
    finally:
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop
