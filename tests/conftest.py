from __future__ import annotations

from typing import Any

import pytest

from unifi_occupancy.config import Config, DiscoveryConfig, MqttConfig, ProtectConfig


@pytest.fixture
def config() -> Config:
    return Config(
        protect=ProtectConfig(host="protect.example", api_key="secret", poll_interval=0.01),
        mqtt=MqttConfig(host="broker.example", topic_prefix="unifi-occupancy"),
        discovery=DiscoveryConfig(),
        detection_hold_seconds=30.0,
    )


class FakeTransport:
    """Records everything published so tests can assert on the wire format."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str | None, bool, int]] = []

    async def publish(self, topic: str, payload: str | None, *, retain: bool, qos: int) -> None:
        self.messages.append((topic, payload, retain, qos))

    @property
    def topics(self) -> list[str]:
        return [topic for topic, _, _, _ in self.messages]

    def payload_for(self, topic: str) -> str | None:
        for candidate, payload, _, _ in reversed(self.messages):
            if candidate == topic:
                return payload
        raise KeyError(topic)

    def sent(self, topic: str) -> bool:
        return topic in self.topics


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


def camera_payload(**overrides: Any) -> dict[str, Any]:
    """A representative ``GET /cameras`` entry."""
    payload: dict[str, Any] = {
        "id": "cam-1",
        "name": "Front Door",
        # Protect answers `modelKey` with the kind of thing this is, which is
        # "camera" for every camera; `type` is the hardware. Spelled the way the
        # controller really spells it, because a fixture that flatters the code
        # is how the useless value reached Home Assistant in the first place.
        "modelKey": "camera",
        "type": "UVC G4 Doorbell Pro",
        "mac": "AABBCCDDEEFF",
        "firmwareVersion": "4.70.55",
        "state": "CONNECTED",
        "isMotionDetected": False,
        "lastMotion": 1_700_000_000_000,
        "recordingSettings": {"enableMotionDetection": True},
        "smartDetectSettings": {
            "objectTypes": ["person", "vehicle"],
            "audioTypes": ["alrmSmoke", "alrmSiren"],
        },
        "featureFlags": {
            "smartDetectTypes": ["person", "vehicle", "animal"],
            "smartDetectAudioTypes": ["alrmSmoke", "alrmSiren", "alrmCmonx"],
        },
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def camera_json() -> dict[str, Any]:
    return camera_payload()
