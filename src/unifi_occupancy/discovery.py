"""Home Assistant MQTT autodiscovery payloads.

One HA device per camera, carrying two binary sensors per detection kind: the
detection itself, and a diagnostic sensor saying whether that detection is
switched on in Protect, plus a diagnostic sensor holding the camera's ``info``.

One further device stands for the service itself -- a bridge, in HA's sense --
so the ``<prefix>/info`` payload has somewhere to land: what version is running,
which Protect it is talking to, and which optional features are on. Without it
that topic exists only for consumers outside Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import __version__
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
    health_topic,
    info_topic,
    service_info_topic,
)

MANUFACTURER = "Ubiquiti"

#: The service's own HA device. The cameras are Ubiquiti's; this one is ours.
SERVICE_NAME = "UniFi Occupancy"
SERVICE_MANUFACTURER = "unifi-occupancy"
SERVICE_MODEL = "MQTT bridge"


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
        # `type` first: Protect's `modelKey` is "camera" for every camera, so
        # it tells a reader nothing. `model` is kept as the fallback for a
        # controller version that does not send `type`.
        "model": camera.hardware or camera.model or "UniFi Protect Camera",
    }
    if camera.firmware:
        device["sw_version"] = camera.firmware
    if camera.mac:
        device["connections"] = [["mac", camera.mac]]
    return device


def service_device_block(config: Config) -> dict[str, Any]:
    """The HA device that stands for this service rather than for a camera."""
    return {
        "identifiers": [config.discovery.node_id],
        "name": SERVICE_NAME,
        "manufacturer": SERVICE_MANUFACTURER,
        "model": SERVICE_MODEL,
        "sw_version": __version__,
    }


def service_messages(config: Config) -> list[DiscoveryMessage]:
    """Discovery for the bridge device: what is running, and against what.

    Both entities read the one retained ``<prefix>/info`` payload. The service
    version is the state of the first and the whole payload hangs off it as
    attributes, so everything in that topic -- the console, the MQTT layout,
    which features are on -- is visible on one page in Home Assistant.
    """
    topic = service_info_topic(config.mqtt)
    node = config.discovery.node_id
    common: dict[str, Any] = {
        "device": service_device_block(config),
        "availability_topic": config.mqtt.availability_topic,
        "payload_available": PAYLOAD_AVAILABLE,
        "payload_not_available": PAYLOAD_UNAVAILABLE,
        "state_topic": topic,
        "entity_category": "diagnostic",
    }
    version = {
        **common,
        "name": "Service Version",
        "unique_id": f"{node}_service_version",
        "object_id": f"{node}_service_version",
        "value_template": "{{ value_json.version }}",
        "json_attributes_topic": topic,
    }
    protect = {
        **common,
        "name": "Protect Version",
        "unique_id": f"{node}_protect_version",
        "object_id": f"{node}_protect_version",
        # Protect is entitled to answer /meta/info without a version; an empty
        # state would make HA show the entity as unknown, which reads as "the
        # bridge is broken" rather than "Protect did not say".
        "value_template": (
            "{{ value_json.protect.application_version | default('unknown', true) }}"
        ),
    }
    return [
        DiscoveryMessage(_service_config_topic(config, "service_version"), version),
        DiscoveryMessage(_service_config_topic(config, "protect_version"), protect),
    ]


def _service_config_topic(config: Config, suffix: str) -> str:
    return f"{config.discovery.prefix}/sensor/{config.discovery.node_id}/{suffix}/config"


def _unique_id(config: Config, camera: Camera, suffix: str) -> str:
    return f"{config.discovery.node_id}_{camera.id}_{suffix}"


def _object_id(config: Config, camera: Camera, suffix: str) -> str:
    return f"{config.discovery.node_id}_{camera.slug}_{suffix}"


def _config_topic(
    config: Config, camera: Camera, suffix: str, component: str = "binary_sensor"
) -> str:
    return (
        f"{config.discovery.prefix}/{component}/"
        f"{config.discovery.node_id}_{camera.slug}/{suffix}/config"
    )


def _device(config: Config, camera: Camera) -> dict[str, Any]:
    return {
        "device": device_block(config, camera),
        "availability_topic": config.mqtt.availability_topic,
        "payload_available": PAYLOAD_AVAILABLE,
        "payload_not_available": PAYLOAD_UNAVAILABLE,
    }


def _common(config: Config, camera: Camera) -> dict[str, Any]:
    return {
        **_device(config, camera),
        "payload_on": PAYLOAD_ON,
        "payload_off": PAYLOAD_OFF,
        "json_attributes_topic": attributes_topic(config.mqtt, camera),
    }


def health_messages(config: Config, camera: Camera) -> list[DiscoveryMessage]:
    """The two health entities: the verdict itself, and "is there a problem".

    Only published when the health check is switched on -- there is no sense in
    giving Home Assistant an entity that will never have a state.
    """
    topic = health_topic(config.mqtt, camera)
    verdict = {
        **_device(config, camera),
        "name": "Camera Health",
        "unique_id": _unique_id(config, camera, "health"),
        "object_id": _object_id(config, camera, "health"),
        "state_topic": topic,
        "value_template": "{{ value_json.health }}",
        "json_attributes_topic": topic,
        "entity_category": "diagnostic",
    }
    problem = {
        **_common(config, camera),
        "name": "Camera Health Problem",
        "unique_id": _unique_id(config, camera, "health_problem"),
        "object_id": _object_id(config, camera, "health_problem"),
        "state_topic": topic,
        # Only an explicit "bad" is a problem; an unknown verdict means the
        # check could not be made, which is not the same as a blocked camera.
        "value_template": ("{{ 'ON' if value_json.health == 'bad' else 'OFF' }}"),
        "json_attributes_topic": topic,
        "device_class": "problem",
        "entity_category": "diagnostic",
    }
    return [
        DiscoveryMessage(_config_topic(config, camera, "health", "sensor"), verdict),
        DiscoveryMessage(_config_topic(config, camera, "health_problem"), problem),
    ]


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
    messages.append(
        DiscoveryMessage(
            _config_topic(config, camera, "info", "sensor"),
            {
                **_device(config, camera),
                "name": "Camera Info",
                "unique_id": _unique_id(config, camera, "info"),
                "object_id": _object_id(config, camera, "info"),
                "state_topic": info_topic(config.mqtt, camera),
                # The hardware model is the one field short and stable enough
                # to be a state; everything else in `info` rides along as
                # attributes. Not `model`, which Protect answers with the bare
                # word "camera".
                "value_template": ("{{ value_json.hardware or value_json.model or 'unknown' }}"),
                "json_attributes_topic": info_topic(config.mqtt, camera),
                "entity_category": "diagnostic",
            },
        )
    )
    if config.health.enabled:
        messages.extend(health_messages(config, camera))
    return messages


def removal_messages(config: Config, camera: Camera) -> list[DiscoveryMessage]:
    """Empty-payload messages that retract a camera's entities from HA.

    The health entities are retracted whether or not the check is switched on:
    emptying a topic that was never published costs one message, while leaving a
    retained config behind after someone switches the check off leaves Home
    Assistant holding an entity nothing will ever update again.
    """
    messages = list(discovery_messages(config, camera))
    if not config.health.enabled:
        messages.extend(health_messages(config, camera))
    return [DiscoveryMessage(message.topic, None) for message in messages]


def attributes_payload(camera: Camera) -> dict[str, Any]:
    """Extra context HA shows on each entity. No video-adjacent fields."""
    return {
        "camera_id": camera.id,
        "camera_name": camera.name,
        "model": camera.model,
        # Alongside `model` rather than instead of it: `model` is what Protect
        # calls the kind of device ("camera"), and anything already reading it
        # should keep getting the same answer.
        "hardware": camera.hardware,
        "connected": camera.connected,
        "last_motion": camera.last_motion.isoformat() if camera.last_motion else None,
        "supported": {kind_for(key).key: camera.is_supported(key) for key in camera.supported},
    }
