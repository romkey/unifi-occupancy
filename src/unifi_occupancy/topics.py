"""Topic layout, in one place so the publisher and discovery agree."""

from __future__ import annotations

from .config import MqttConfig
from .models import Camera

PAYLOAD_ON = "ON"
PAYLOAD_OFF = "OFF"
PAYLOAD_AVAILABLE = "online"
PAYLOAD_UNAVAILABLE = "offline"


def camera_base(mqtt: MqttConfig, camera: Camera) -> str:
    return f"{mqtt.topic_prefix}/{camera.slug}"


def detection_topic(mqtt: MqttConfig, camera: Camera, key: str) -> str:
    return f"{camera_base(mqtt, camera)}/{key}"


def enabled_topic(mqtt: MqttConfig, camera: Camera, key: str) -> str:
    return f"{camera_base(mqtt, camera)}/{key}_enabled"


def attributes_topic(mqtt: MqttConfig, camera: Camera) -> str:
    return f"{camera_base(mqtt, camera)}/attributes"


def payload_for(value: bool) -> str:
    return PAYLOAD_ON if value else PAYLOAD_OFF
