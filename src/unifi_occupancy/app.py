"""The service: poll Protect, follow its event stream, publish to MQTT."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import aiomqtt

from .config import Config
from .models import Camera, DetectionEvent
from .protect import ProtectAuthError, ProtectClient, ProtectError
from .publisher import AiomqttTransport, StatePublisher
from .recorder import EventRecorder
from .report import SheetWriter
from .state import DetectionTracker
from .store import EventStore
from .topics import PAYLOAD_UNAVAILABLE

logger = logging.getLogger(__name__)

#: Reconnect backoff for the Protect event websocket.
MIN_BACKOFF = 1.0
MAX_BACKOFF = 60.0


def mqtt_client(config: Config) -> aiomqtt.Client:
    """An aiomqtt client with a last-will so HA sees us go away."""
    return aiomqtt.Client(
        hostname=config.mqtt.host,
        port=config.mqtt.port,
        username=config.mqtt.username,
        password=config.mqtt.password,
        identifier=config.mqtt.client_id,
        keepalive=config.mqtt.keepalive,
        tls_context=ssl.create_default_context() if config.mqtt.tls else None,
        will=aiomqtt.Will(
            topic=config.mqtt.availability_topic,
            payload=PAYLOAD_UNAVAILABLE,
            qos=config.mqtt.qos,
            retain=config.mqtt.retain,
        ),
    )


class OccupancyService:
    """Owns the camera inventory, the detection tracker and the publish loops."""

    def __init__(
        self,
        config: Config,
        protect: ProtectClient,
        publisher: StatePublisher,
        clock: Callable[[], float] = time.monotonic,
        recorder: EventRecorder | None = None,
        meta: Mapping[str, Any] | None = None,
        console: Mapping[str, Any] | None = None,
    ) -> None:
        self._config = config
        self._protect = protect
        self._publisher = publisher
        self._clock = clock
        self._recorder = recorder
        self._meta = meta
        self._console = console
        self._tracker = DetectionTracker(hold_seconds=config.detection_hold_seconds)
        self._cameras: dict[str, Camera] = {}
        self._lock = asyncio.Lock()

    @property
    def cameras(self) -> dict[str, Camera]:
        return dict(self._cameras)

    async def refresh(self) -> None:
        """Poll the camera list and publish the state of every camera."""
        cameras = await self._protect.get_cameras()
        async with self._lock:
            seen = {camera.id: camera for camera in cameras}
            gone = [camera for cid, camera in self._cameras.items() if cid not in seen]
            if gone:
                self._tracker.forget(camera.id for camera in gone)
                if self._recorder is not None:
                    self._recorder.forget(frozenset(camera.id for camera in gone))
                await self._publisher.retract(gone)
            self._cameras = seen
            await self._publish_locked(cameras)
            await self._publisher.publish_service_info(
                meta=self._meta,
                console=self._console,
                camera_count=len(seen),
            )

    async def _publish_locked(self, cameras: list[Camera]) -> None:
        now = self._clock()
        for camera in cameras:
            state = self._tracker.snapshot(camera, now)
            if self._recorder is not None:
                self._recorder.observe(state)
            await self._publisher.publish_state(state)

    async def handle_events(self, events: AsyncIterator[DetectionEvent]) -> None:
        """Apply events as they arrive and republish just the affected camera."""
        async for event in events:
            async with self._lock:
                self._tracker.apply(event, self._clock())
                camera = self._cameras.get(event.camera_id)
                if camera is None:
                    logger.debug("event for unknown camera %s", event.camera_id)
                    continue
                await self._publish_locked([camera])

    async def poll_forever(self) -> None:
        while True:
            try:
                await self.refresh()
            except ProtectAuthError:
                raise
            except ProtectError as exc:
                logger.warning("poll failed: %s", exc)
            await asyncio.sleep(self._config.protect.poll_interval)

    async def watch_events_forever(self) -> None:
        """Follow the event websocket, reconnecting with exponential backoff."""
        backoff = MIN_BACKOFF
        while True:
            try:
                await self.handle_events(self._protect.events())
                logger.info("Protect event stream closed; reconnecting")
                backoff = MIN_BACKOFF
            except ProtectAuthError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any transport error is retryable
                logger.warning("event stream error: %s", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)


def _open_store(config: Config) -> EventStore | None:
    """The history database, or ``None`` when the sheets are switched off."""
    reports = config.reports
    if reports is None:
        logger.info("no %s; not recording occupancy history", config.config_file)
        return None
    if not reports.enabled:
        logger.info("occupancy sheets disabled in %s", config.config_file)
        return None
    store = EventStore(reports.database)
    store.close_open_events(config.detection_hold_seconds)
    logger.info(
        "recording occupancy history to %s, sheets in %s (%s)",
        reports.database,
        reports.output_dir,
        reports.timezone,
    )
    return store


async def run(config: Config) -> None:
    """Connect everything up and run until cancelled."""
    async with ProtectClient(config.protect) as protect:
        meta = await protect.get_meta()
        logger.info("connected to UniFi Protect %s", meta.get("applicationVersion", "(unknown)"))
        console = await _console_info(protect)

        store = _open_store(config)
        try:
            await _serve(config, protect, store, meta=meta, console=console)
        finally:
            if store is not None:
                store.close()


async def _console_info(protect: ProtectClient) -> dict[str, Any] | None:
    """The console's description, or ``None`` if it will not tell us.

    Published as metadata and nothing more, so failing to read it must never
    stop the service from starting.
    """
    try:
        return await protect.get_console()
    except ProtectAuthError:
        raise
    except ProtectError as exc:
        logger.debug("could not read console info: %s", exc)
        return None


async def _serve(
    config: Config,
    protect: ProtectClient,
    store: EventStore | None,
    *,
    meta: Mapping[str, Any] | None = None,
    console: Mapping[str, Any] | None = None,
) -> None:
    recorder = EventRecorder(store) if store is not None else None

    async with mqtt_client(config) as client:
        publisher = StatePublisher(config, AiomqttTransport(client))
        await publisher.publish_availability(True)
        service = OccupancyService(
            config,
            protect,
            publisher,
            recorder=recorder,
            meta=meta,
            console=console,
        )
        await publisher.publish_service_info(meta=meta, console=console)

        tasks = [asyncio.create_task(service.poll_forever(), name="poll")]
        if config.protect.use_websocket:
            tasks.append(asyncio.create_task(service.watch_events_forever(), name="events"))
        if store is not None and config.reports is not None:
            writer = SheetWriter(
                store,
                config.reports,
                cameras=lambda: list(service.cameras.values()),
            )
            tasks.append(asyncio.create_task(writer.run_forever(), name="sheets"))
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await publisher.publish_availability(False)
