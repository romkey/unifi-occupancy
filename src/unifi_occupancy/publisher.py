"""Publishing camera state to MQTT.

:class:`StatePublisher` holds the logic worth testing -- what to publish, when
to re-publish, and how to retract a camera that has gone away -- behind a tiny
transport protocol so tests never need a broker.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

from .config import Config
from .discovery import (
    attributes_payload,
    discovery_messages,
    health_messages,
    removal_messages,
    service_messages,
)
from .health import HealthVerdict
from .info import camera_info_payload, service_info_payload
from .models import DETECTION_KEYS, Camera
from .state import CameraState
from .topics import (
    PAYLOAD_AVAILABLE,
    PAYLOAD_UNAVAILABLE,
    attributes_topic,
    detection_topic,
    enabled_topic,
    health_topic,
    info_topic,
    payload_for,
    service_info_topic,
)

logger = logging.getLogger(__name__)


class Transport(Protocol):
    """Anything that can put a payload on a topic."""

    async def publish(self, topic: str, payload: str | None, *, retain: bool, qos: int) -> None: ...


class StatePublisher:
    """Publishes detection state, skipping values that have not changed."""

    def __init__(self, config: Config, transport: Transport) -> None:
        self._config = config
        self._transport = transport
        self._published: dict[str, str | None] = {}
        self._announced: set[str] = set()
        #: Cameras whose stale health topics we have already cleaned up.
        self._health_cleared: set[str] = set()
        #: Whether the bridge device's own discovery has gone out this process.
        self._service_announced = False

    async def _send(self, topic: str, payload: str | None, *, force: bool = False) -> None:
        if not force and self._published.get(topic, object()) == payload:
            return
        await self._transport.publish(
            topic,
            payload,
            retain=self._config.mqtt.retain,
            qos=self._config.mqtt.qos,
        )
        self._published[topic] = payload

    async def publish_availability(self, online: bool) -> None:
        await self._send(
            self._config.mqtt.availability_topic,
            PAYLOAD_AVAILABLE if online else PAYLOAD_UNAVAILABLE,
            force=True,
        )

    async def publish_service_info(
        self,
        *,
        meta: Mapping[str, Any] | None = None,
        console: Mapping[str, Any] | None = None,
        camera_count: int | None = None,
    ) -> None:
        """Publish ``<prefix>/info``: this build, Protect, and the console."""
        await self.announce_service()
        payload = service_info_payload(
            self._config,
            meta=meta,
            console=console,
            camera_count=camera_count,
        )
        await self._send(
            service_info_topic(self._config.mqtt),
            json.dumps(payload, sort_keys=True),
        )

    async def announce_service(self) -> None:
        """Send HA discovery for the bridge device, once per process.

        Ordered before the payload it describes only for tidiness: both topics
        are retained, so Home Assistant gets the same result either way.
        """
        if not self._config.discovery.enabled or self._service_announced:
            return
        for message in service_messages(self._config):
            await self._send(message.topic, json.dumps(message.payload), force=True)
        self._service_announced = True
        logger.info("announced the service to Home Assistant")

    async def announce(self, camera: Camera) -> None:
        """Send HA discovery for a camera we have not announced yet."""
        if not self._config.discovery.enabled or camera.id in self._announced:
            return
        for message in discovery_messages(self._config, camera):
            await self._send(message.topic, json.dumps(message.payload), force=True)
        self._announced.add(camera.id)
        logger.info("announced camera %s (%s) to Home Assistant", camera.name, camera.id)

    async def _clear_disabled_health(self, camera: Camera) -> None:
        """Take down health topics left behind by a previous run.

        Switching ``CAMERA_HEALTH_ENABLED`` back off would otherwise leave a
        retained verdict -- possibly a ``bad`` one -- and its discovery configs
        on the broker for good, so the camera would look permanently broken by a
        check that is no longer running. Done once per camera per process.
        """
        if self._config.health.enabled or camera.id in self._health_cleared:
            return
        self._health_cleared.add(camera.id)
        if self._config.discovery.enabled:
            for message in health_messages(self._config, camera):
                await self._send(message.topic, None, force=True)
        await self._send(health_topic(self._config.mqtt, camera), None, force=True)

    async def retract(self, cameras: Iterable[Camera]) -> None:
        """Remove cameras from HA and forget their published state."""
        for camera in cameras:
            if self._config.discovery.enabled:
                for message in removal_messages(self._config, camera):
                    await self._send(message.topic, None, force=True)
            # Clear the retained metadata too. These two describe a camera that
            # exists; left on the broker they would outlive it, and `info` is
            # meant to be an inventory of what is there now.
            for topic in (
                info_topic(self._config.mqtt, camera),
                attributes_topic(self._config.mqtt, camera),
                health_topic(self._config.mqtt, camera),
            ):
                await self._send(topic, None, force=True)
            self._announced.discard(camera.id)
            self._health_cleared.discard(camera.id)
            prefix = f"{self._config.mqtt.topic_prefix}/{camera.slug}/"
            for topic in [key for key in self._published if key.startswith(prefix)]:
                del self._published[topic]
            logger.info("retracted camera %s (%s)", camera.name, camera.id)

    async def publish_state(self, state: CameraState) -> None:
        camera = state.camera
        await self.announce(camera)
        await self._clear_disabled_health(camera)
        for key in DETECTION_KEYS:
            await self._send(
                detection_topic(self._config.mqtt, camera, key),
                payload_for(state.active(key)),
            )
            await self._send(
                enabled_topic(self._config.mqtt, camera, key),
                payload_for(bool(state.enabled.get(key, False))),
            )
        await self._send(
            attributes_topic(self._config.mqtt, camera),
            json.dumps(attributes_payload(camera), sort_keys=True),
        )
        await self._send(
            info_topic(self._config.mqtt, camera),
            json.dumps(camera_info_payload(camera), sort_keys=True),
        )

    async def publish_health(self, camera: Camera, verdict: HealthVerdict) -> None:
        """Publish one camera's health verdict.

        Forced rather than change-suppressed: an hourly "still good" is how a
        consumer tells a healthy camera from one this process stopped checking,
        and ``checked_at`` would defeat the suppression anyway.
        """
        await self._send(
            health_topic(self._config.mqtt, camera),
            json.dumps(verdict.payload(), sort_keys=True),
            force=True,
        )

    async def publish_all(self, states: Sequence[CameraState]) -> None:
        for state in states:
            await self.publish_state(state)


class AiomqttTransport:
    """:class:`Transport` backed by an ``aiomqtt`` client."""

    def __init__(self, client: object) -> None:
        self._client = client

    async def publish(self, topic: str, payload: str | None, *, retain: bool, qos: int) -> None:
        await self._client.publish(  # type: ignore[attr-defined]
            topic,
            payload=payload if payload is not None else b"",
            retain=retain,
            qos=qos,
        )
