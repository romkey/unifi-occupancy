from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from conftest import FakeTransport, camera_payload
from unifi_occupancy.app import OccupancyService
from unifi_occupancy.config import Config
from unifi_occupancy.models import MOTION, PERSON, DetectionEvent, parse_cameras
from unifi_occupancy.protect import ProtectError
from unifi_occupancy.publisher import StatePublisher


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
