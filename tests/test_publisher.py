from __future__ import annotations

import json

from conftest import FakeTransport, camera_payload
from unifi_occupancy.config import Config
from unifi_occupancy.models import MOTION, PERSON, SMOKE, DetectionEvent, parse_camera
from unifi_occupancy.publisher import StatePublisher
from unifi_occupancy.state import DetectionTracker


def snapshot(camera_overrides: dict | None = None, events: list[str] | None = None):
    camera = parse_camera(camera_payload(**(camera_overrides or {})))
    tracker = DetectionTracker(hold_seconds=30.0)
    for key in events or []:
        tracker.apply(DetectionEvent(camera.id, frozenset({key})), now=1.0)
    return tracker.snapshot(camera, now=1.0)


async def test_publishes_a_state_topic_per_detection(
    config: Config, transport: FakeTransport
) -> None:
    await StatePublisher(config, transport).publish_state(snapshot(events=[PERSON]))

    assert transport.payload_for("unifi-occupancy/front_door/person") == "ON"
    assert transport.payload_for("unifi-occupancy/front_door/motion") == "OFF"
    assert transport.payload_for("unifi-occupancy/front_door/siren") == "OFF"
    assert transport.payload_for("unifi-occupancy/front_door/smoke") == "OFF"


async def test_publishes_enabled_topics(config: Config, transport: FakeTransport) -> None:
    state = snapshot({"smartDetectSettings": {"objectTypes": ["person"], "audioTypes": []}})
    await StatePublisher(config, transport).publish_state(state)

    assert transport.payload_for("unifi-occupancy/front_door/person_enabled") == "ON"
    assert transport.payload_for("unifi-occupancy/front_door/smoke_enabled") == "OFF"
    assert transport.payload_for("unifi-occupancy/front_door/motion_enabled") == "ON"


async def test_attributes_carry_no_video_fields(config: Config, transport: FakeTransport) -> None:
    await StatePublisher(config, transport).publish_state(snapshot())

    payload = transport.payload_for("unifi-occupancy/front_door/attributes")
    assert payload is not None
    attributes = json.loads(payload)

    assert attributes["camera_name"] == "Front Door"
    assert attributes["connected"] is True
    assert attributes["supported"][SMOKE] is True
    assert not {"rtsp", "host", "streams", "snapshot"} & set(attributes)


async def test_unchanged_values_are_not_republished(
    config: Config, transport: FakeTransport
) -> None:
    publisher = StatePublisher(config, transport)
    state = snapshot()

    await publisher.publish_state(state)
    first = len(transport.messages)
    await publisher.publish_state(state)

    assert len(transport.messages) == first


async def test_changed_values_are_republished(config: Config, transport: FakeTransport) -> None:
    publisher = StatePublisher(config, transport)

    await publisher.publish_state(snapshot())
    await publisher.publish_state(snapshot(events=[MOTION]))

    assert transport.payload_for("unifi-occupancy/front_door/motion") == "ON"


async def test_discovery_is_sent_once_per_camera(config: Config, transport: FakeTransport) -> None:
    publisher = StatePublisher(config, transport)

    await publisher.publish_state(snapshot())
    discovery_topics = [t for t in transport.topics if t.startswith("homeassistant/")]
    announced = [
        topic
        for topic, payload, _, _ in transport.messages
        if topic.startswith("homeassistant/") and payload is not None
    ]
    cleared = [
        topic
        for topic, payload, _, _ in transport.messages
        if topic.startswith("homeassistant/") and payload is None
    ]
    await publisher.publish_state(snapshot(events=[MOTION]))

    still = [t for t in transport.topics if t.startswith("homeassistant/")]
    assert len(announced) == 8  # 4 detections x (state + enabled)
    # The two health entities are retracted rather than announced, because the
    # health check is off in this config and may have been on in a previous run.
    assert len(cleared) == 2
    assert still == discovery_topics


async def test_discovery_payload_shape(config: Config, transport: FakeTransport) -> None:
    await StatePublisher(config, transport).publish_state(snapshot())

    topic = "homeassistant/binary_sensor/unifi_occupancy_front_door/person/config"
    payload = transport.payload_for(topic)
    assert payload is not None
    body = json.loads(payload)

    assert body["device_class"] == "occupancy"
    assert body["state_topic"] == "unifi-occupancy/front_door/person"
    assert body["unique_id"] == "unifi_occupancy_cam-1_person"
    assert body["availability_topic"] == "unifi-occupancy/status"
    assert body["device"]["identifiers"] == ["unifi_occupancy_cam-1"]
    assert body["device"]["manufacturer"] == "Ubiquiti"


async def test_enabled_sensor_is_diagnostic(config: Config, transport: FakeTransport) -> None:
    await StatePublisher(config, transport).publish_state(snapshot())

    topic = "homeassistant/binary_sensor/unifi_occupancy_front_door/smoke_enabled/config"
    payload = transport.payload_for(topic)
    assert payload is not None
    body = json.loads(payload)

    assert body["entity_category"] == "diagnostic"
    assert body["state_topic"] == "unifi-occupancy/front_door/smoke_enabled"


async def test_discovery_can_be_disabled(config: Config, transport: FakeTransport) -> None:
    from dataclasses import replace

    disabled = replace(config, discovery=replace(config.discovery, enabled=False))
    await StatePublisher(disabled, transport).publish_state(snapshot())

    assert not any(topic.startswith("homeassistant/") for topic in transport.topics)


async def test_availability(config: Config, transport: FakeTransport) -> None:
    publisher = StatePublisher(config, transport)

    await publisher.publish_availability(True)
    await publisher.publish_availability(False)

    assert transport.messages[0][:2] == ("unifi-occupancy/status", "online")
    assert transport.messages[1][:2] == ("unifi-occupancy/status", "offline")


async def test_retract_clears_discovery_and_allows_reannounce(
    config: Config, transport: FakeTransport
) -> None:
    publisher = StatePublisher(config, transport)
    state = snapshot()
    await publisher.publish_state(state)

    await publisher.retract([state.camera])

    topic = "homeassistant/binary_sensor/unifi_occupancy_front_door/person/config"
    assert transport.payload_for(topic) is None

    await publisher.publish_state(state)
    assert transport.payload_for(topic) is not None
    assert transport.payload_for("unifi-occupancy/front_door/person") == "OFF"


async def test_retain_and_qos_come_from_config(config: Config, transport: FakeTransport) -> None:
    from dataclasses import replace

    tuned = replace(config, mqtt=replace(config.mqtt, retain=False, qos=1))
    await StatePublisher(tuned, transport).publish_state(snapshot())

    assert all(retain is False and qos == 1 for _, _, retain, qos in transport.messages)


async def test_topic_prefix_is_honoured(config: Config, transport: FakeTransport) -> None:
    from dataclasses import replace

    tuned = replace(config, mqtt=replace(config.mqtt, topic_prefix="sensors/unifi"))
    await StatePublisher(tuned, transport).publish_state(snapshot())

    assert "sensors/unifi/front_door/motion" in transport.topics
