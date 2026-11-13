from __future__ import annotations

import json
from dataclasses import replace

from conftest import FakeTransport, camera_payload
from unifi_occupancy import __version__
from unifi_occupancy.config import Config
from unifi_occupancy.info import camera_info_payload, console_info, service_info_payload
from unifi_occupancy.models import MOTION, PERSON, SMOKE, parse_camera
from unifi_occupancy.publisher import StatePublisher
from unifi_occupancy.state import DetectionTracker

META = {"applicationVersion": "6.3.52"}
NVR = {
    "id": "nvr-1",
    "name": "Dream Machine",
    "type": "UDMPRO",
    "version": "6.3.52",
    "firmwareVersion": "4.3.9",
    "hardwarePlatform": "al324",
    "timezone": "America/Los_Angeles",
    "uptime": 123456,
    # Fields we must not forward even though Protect hands them over.
    "wanIp": "203.0.113.7",
    "storageInfo": {"totalSize": 1},
    "ports": {"rtsp": 7447},
}


def test_console_info_is_an_allowlist() -> None:
    info = console_info(NVR)

    assert info["name"] == "Dream Machine"
    assert info["type"] == "UDMPRO"
    assert info["version"] == "6.3.52"
    assert info["firmware_version"] == "4.3.9"
    assert info["hardware_platform"] == "al324"
    assert info["uptime"] == 123456
    assert not {"wanIp", "storageInfo", "ports", "id"} & set(info)


def test_console_info_tolerates_nothing() -> None:
    assert console_info(None) == {}
    assert console_info({}) == {}


def test_service_info_reports_versions_and_features(config: Config) -> None:
    payload = service_info_payload(config, meta=META, console=NVR, camera_count=3)

    assert payload["service"] == "unifi-occupancy"
    assert payload["version"] == __version__
    assert payload["protect"]["application_version"] == "6.3.52"
    assert payload["protect"]["host"] == "protect.example"
    assert payload["protect"]["console"]["name"] == "Dream Machine"
    assert payload["mqtt"]["topic_prefix"] == "unifi-occupancy"
    assert payload["features"] == {
        "websocket": True,
        "ha_discovery": True,
        "occupancy_sheets": False,
        "camera_health": False,
    }
    assert payload["cameras"] == 3
    assert MOTION in payload["detections"]


def test_service_info_without_protect_metadata(config: Config) -> None:
    payload = service_info_payload(config)

    assert payload["protect"]["application_version"] is None
    assert "console" not in payload["protect"]
    assert "cameras" not in payload


def test_service_info_carries_no_credentials(config: Config) -> None:
    text = json.dumps(service_info_payload(config, meta=META, console=NVR))

    assert config.protect.api_key not in text


def test_camera_info_describes_the_device() -> None:
    camera = parse_camera(camera_payload(type="UVC G4 Doorbell Pro"))

    info = camera_info_payload(camera)

    assert info["camera_id"] == "cam-1"
    assert info["name"] == "Front Door"
    assert info["slug"] == "front_door"
    assert info["hardware"] == "UVC G4 Doorbell Pro"
    assert info["mac"] == "AABBCCDDEEFF"
    assert info["firmware"] == "4.70.55"
    assert info["connected"] is True
    assert info["detections"][PERSON] == {"supported": True, "enabled": True}
    assert info["detections"][SMOKE] == {"supported": True, "enabled": True}


def test_camera_info_carries_no_video_fields() -> None:
    info = camera_info_payload(parse_camera(camera_payload()))

    assert not {"host", "rtsp", "streams", "snapshot", "channels"} & set(info)


async def test_publisher_sends_the_service_info_topic(
    config: Config, transport: FakeTransport
) -> None:
    publisher = StatePublisher(config, transport)

    await publisher.publish_service_info(meta=META, console=NVR, camera_count=1)

    payload = transport.payload_for("unifi-occupancy/info")
    assert payload is not None
    assert json.loads(payload)["protect"]["application_version"] == "6.3.52"


async def test_publisher_sends_a_camera_info_subtopic(
    config: Config, transport: FakeTransport
) -> None:
    camera = parse_camera(camera_payload())
    state = DetectionTracker(hold_seconds=30.0).snapshot(camera, now=1.0)

    await StatePublisher(config, transport).publish_state(state)

    payload = transport.payload_for("unifi-occupancy/front_door/info")
    assert payload is not None
    assert json.loads(payload)["slug"] == "front_door"


async def test_service_info_is_not_republished_unchanged(
    config: Config, transport: FakeTransport
) -> None:
    publisher = StatePublisher(config, transport)

    await publisher.publish_service_info(meta=META, camera_count=1)
    before = len(transport.messages)
    await publisher.publish_service_info(meta=META, camera_count=1)
    assert len(transport.messages) == before

    await publisher.publish_service_info(meta=META, camera_count=2)
    assert len(transport.messages) == before + 1


async def test_retracting_a_camera_clears_its_retained_metadata(
    config: Config, transport: FakeTransport
) -> None:
    camera = parse_camera(camera_payload())
    publisher = StatePublisher(config, transport)
    state = DetectionTracker(hold_seconds=30.0).snapshot(camera, now=1.0)
    await publisher.publish_state(state)

    await publisher.retract([camera])

    # An empty retained payload is how a retained topic is taken off a broker;
    # otherwise the inventory would describe a camera that is no longer there.
    assert transport.payload_for("unifi-occupancy/front_door/info") is None
    assert transport.payload_for("unifi-occupancy/front_door/attributes") is None


SERVICE_VERSION_CONFIG = "homeassistant/sensor/unifi_occupancy/service_version/config"
PROTECT_VERSION_CONFIG = "homeassistant/sensor/unifi_occupancy/protect_version/config"
CAMERA_INFO_CONFIG = "homeassistant/sensor/unifi_occupancy_front_door/info/config"


async def test_the_service_gets_its_own_home_assistant_device(
    config: Config, transport: FakeTransport
) -> None:
    await StatePublisher(config, transport).publish_service_info(meta=META, console=NVR)

    payload = transport.payload_for(SERVICE_VERSION_CONFIG)
    assert payload is not None
    body = json.loads(payload)

    assert body["state_topic"] == "unifi-occupancy/info"
    assert body["json_attributes_topic"] == "unifi-occupancy/info"
    assert body["value_template"] == "{{ value_json.version }}"
    assert body["entity_category"] == "diagnostic"
    assert body["unique_id"] == "unifi_occupancy_service_version"
    assert body["availability_topic"] == "unifi-occupancy/status"
    # The bridge is its own device, not one of the cameras: a camera device is
    # identified by `<node>_<camera id>` and made by Ubiquiti.
    assert body["device"]["identifiers"] == ["unifi_occupancy"]
    assert body["device"]["manufacturer"] == "unifi-occupancy"
    assert body["device"]["sw_version"] == __version__


async def test_the_protect_version_entity_falls_back_to_unknown(
    config: Config, transport: FakeTransport
) -> None:
    await StatePublisher(config, transport).publish_service_info(meta={})

    payload = transport.payload_for(PROTECT_VERSION_CONFIG)
    assert payload is not None
    body = json.loads(payload)

    # Protect may answer /meta/info without a version. A bare value_template
    # would leave the entity unknown, which reads as a broken bridge.
    assert "default('unknown', true)" in body["value_template"]
    assert "json_attributes_topic" not in body


async def test_service_discovery_is_sent_once(config: Config, transport: FakeTransport) -> None:
    publisher = StatePublisher(config, transport)

    await publisher.publish_service_info(meta=META, camera_count=1)
    await publisher.publish_service_info(meta=META, camera_count=2)

    assert transport.topics.count(SERVICE_VERSION_CONFIG) == 1


async def test_no_service_discovery_when_discovery_is_off(
    config: Config, transport: FakeTransport
) -> None:
    off = replace(config, discovery=replace(config.discovery, enabled=False))
    await StatePublisher(off, transport).publish_service_info(meta=META)

    assert not any(topic.startswith("homeassistant/") for topic in transport.topics)
    assert transport.sent("unifi-occupancy/info")


async def test_each_camera_gets_a_diagnostic_info_entity(
    config: Config, transport: FakeTransport
) -> None:
    camera = parse_camera(camera_payload())
    state = DetectionTracker(hold_seconds=30.0).snapshot(camera, now=1.0)

    await StatePublisher(config, transport).publish_state(state)

    payload = transport.payload_for(CAMERA_INFO_CONFIG)
    assert payload is not None
    body = json.loads(payload)

    assert body["state_topic"] == "unifi-occupancy/front_door/info"
    assert body["json_attributes_topic"] == "unifi-occupancy/front_door/info"
    assert body["entity_category"] == "diagnostic"
    # It hangs off the camera's device, beside its detections.
    assert body["device"]["identifiers"] == ["unifi_occupancy_cam-1"]


async def test_retracting_a_camera_takes_its_info_entity_with_it(
    config: Config, transport: FakeTransport
) -> None:
    camera = parse_camera(camera_payload())
    publisher = StatePublisher(config, transport)
    await publisher.publish_state(DetectionTracker(hold_seconds=30.0).snapshot(camera, now=1.0))

    await publisher.retract([camera])

    assert transport.payload_for(CAMERA_INFO_CONFIG) is None
