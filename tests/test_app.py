from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import FakeTransport, camera_payload
from unifi_occupancy.app import OccupancyService, _open_store
from unifi_occupancy.config import Config, ReportConfig
from unifi_occupancy.models import MOTION, PERSON, DetectionEvent, parse_cameras
from unifi_occupancy.protect import ProtectError
from unifi_occupancy.publisher import StatePublisher
from unifi_occupancy.recorder import EventRecorder
from unifi_occupancy.store import EventStore


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


async def test_refresh_publishes_the_service_info_topic(
    config: Config, transport: FakeTransport
) -> None:
    service, _ = service_for(config, transport, [[camera_payload()]])

    await service.refresh()

    payload = transport.payload_for("unifi-occupancy/info")
    assert payload is not None
    assert json.loads(payload)["cameras"] == 1
