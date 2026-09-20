"""Home Assistant MQTT autodiscovery payloads.

One HA device per camera, carrying two binary sensors per detection kind: the
detection itself, and a diagnostic sensor saying whether that detection is
switched on in Protect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Config
from .models import DETECTION_KINDS, Camera, kind_for
from .topics import (
    PAYLOAD_AVAILABLE,
    PAYLOAD_OFF,
    PAYLOAD_ON,
    PAYLOAD_UNAVAILABLE,
    attributes_topic,
    detection_topic,
    enabled_topic,
)

MANUFACTURER = "Ubiquiti"


@dataclass(frozen=True)
class DiscoveryMessage:
    topic: str
    payload: dict[str, Any] | None


def device_block(config: Config, camera: Camera) -> dict[str, Any]:
    node = config.discovery.node_id
    device: dict[str, Any] = {
        "identifiers": [f"{node}_{camera.id}"],
        "name": camera.name,
        "manufacturer": MANUFACTURER,
        "model": camera.model or "UniFi Protect Camera",
    }
    if camera.firmware:
        device["sw_version"] = camera.firmware
    if camera.mac:
        device["connections"] = [["mac", camera.mac]]
    return device


def _unique_id(config: Config, camera: Camera, suffix: str) -> str:
    return f"{config.discovery.node_id}_{camera.id}_{suffix}"


def _object_id(config: Config, camera: Camera, suffix: str) -> str:
    return f"{config.discovery.node_id}_{camera.slug}_{suffix}"


def _config_topic(config: Config, camera: Camera, suffix: str) -> str:
    return (
        f"{config.discovery.prefix}/binary_sensor/"
        f"{config.discovery.node_id}_{camera.slug}/{suffix}/config"
    )


def _common(config: Config, camera: Camera) -> dict[str, Any]:
    return {
        "device": device_block(config, camera),
        "availability_topic": config.mqtt.availability_topic,
        "payload_available": PAYLOAD_AVAILABLE,
        "payload_not_available": PAYLOAD_UNAVAILABLE,
        "payload_on": PAYLOAD_ON,
        "payload_off": PAYLOAD_OFF,
        "json_attributes_topic": attributes_topic(config.mqtt, camera),
    }


def discovery_messages(config: Config, camera: Camera) -> list[DiscoveryMessage]:
    """Every discovery config message for one camera."""
    messages: list[DiscoveryMessage] = []
    for kind in DETECTION_KINDS:
        detection: dict[str, Any] = {
            **_common(config, camera),
            "name": kind.name,
            "unique_id": _unique_id(config, camera, kind.key),
            "object_id": _object_id(config, camera, kind.key),
            "state_topic": detection_topic(config.mqtt, camera, kind.key),
        }
        if kind.device_class:
            detection["device_class"] = kind.device_class
        messages.append(DiscoveryMessage(_config_topic(config, camera, kind.key), detection))

        enabled_suffix = f"{kind.key}_enabled"
        messages.append(
            DiscoveryMessage(
                _config_topic(config, camera, enabled_suffix),
                {
                    **_common(config, camera),
                    "name": f"{kind.name} Detection Enabled",
                    "unique_id": _unique_id(config, camera, enabled_suffix),
                    "object_id": _object_id(config, camera, enabled_suffix),
                    "state_topic": enabled_topic(config.mqtt, camera, kind.key),
                    "entity_category": "diagnostic",
                },
            )
        )
    return messages


def removal_messages(config: Config, camera: Camera) -> list[DiscoveryMessage]:
    """Empty-payload messages that retract a camera's entities from HA."""
    return [DiscoveryMessage(message.topic, None) for message in discovery_messages(config, camera)]


def attributes_payload(camera: Camera) -> dict[str, Any]:
    """Extra context HA shows on each entity. No video-adjacent fields."""
    return {
        "camera_id": camera.id,
        "camera_name": camera.name,
        "model": camera.model,
        "connected": camera.connected,
        "last_motion": camera.last_motion.isoformat() if camera.last_motion else None,
        "supported": {kind_for(key).key: camera.is_supported(key) for key in camera.supported},
    }
