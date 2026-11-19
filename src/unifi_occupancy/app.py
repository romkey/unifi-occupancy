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
from .exporter import MetricsServer
from .health import CameraHealthMonitor, VisionJudge
from .metrics import OUTCOME_AUTH_ERROR, OUTCOME_ERROR, OUTCOME_SUCCESS, Metrics, default_metrics
from .models import Camera, DetectionEvent
from .protect import ProtectAuthError, ProtectClient, ProtectError
from .publisher import AiomqttTransport, StatePublisher
from .recorder import EventRecorder
from .report import SheetWriter
from .state import DetectionTracker
from .store import EventStore
from .topics import PAYLOAD_UNAVAILABLE
from .triggers import TriggerWatcher, VisionTriggerJudge

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
        metrics: Metrics | None = None,
    ) -> None:
        self._config = config
        self._protect = protect
        self._publisher = publisher
        self._clock = clock
        self._recorder = recorder
        self._meta = meta
        self._console = console
        self._metrics = metrics or default_metrics()
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
                self._metrics.forget_cameras(camera.slug for camera in gone)
            self._cameras = seen
            self._metrics.observe_cameras(cameras)
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
            self._metrics.observe_state(state, now)
            await self._publisher.publish_state(state)

    async def handle_events(self, events: AsyncIterator[DetectionEvent]) -> None:
        """Apply events as they arrive and republish just the affected camera."""
        async for event in events:
            async with self._lock:
                self._metrics.record_event(event.keys, start=event.start)
                self._tracker.apply(event, self._clock())
                camera = self._cameras.get(event.camera_id)
                if camera is None:
                    logger.debug("event for unknown camera %s", event.camera_id)
                    continue
                await self._publish_locked([camera])

    async def poll_forever(self) -> None:
        while True:
            started = self._clock()
            self._metrics.start_job("poll")
            try:
                await self.refresh()
            except asyncio.CancelledError:
                self._metrics.job_in_progress.set(0, job="poll")
                raise
            except ProtectAuthError:
                self._metrics.protect_up.set(0)
                self._metrics.finish_job("poll", OUTCOME_AUTH_ERROR, self._since(started))
                raise
            except ProtectError as exc:
                self._metrics.protect_up.set(0)
                self._metrics.finish_job("poll", OUTCOME_ERROR, self._since(started))
                logger.warning("poll failed: %s", exc)
            else:
                self._metrics.protect_up.set(1)
                self._metrics.finish_job("poll", OUTCOME_SUCCESS, self._since(started))
            await asyncio.sleep(self._config.protect.poll_interval)

    def _since(self, started: float) -> float:
        return max(0.0, self._clock() - started)

    async def watch_events_forever(self) -> None:
        """Follow the event websocket, reconnecting with exponential backoff."""
        backoff = MIN_BACKOFF
        while True:
            started = self._clock()
            self._metrics.start_job("events")
            try:
                await self.handle_events(self._protect.events())
                logger.info("Protect event stream closed; reconnecting")
                self._metrics.finish_job("events", OUTCOME_SUCCESS, self._since(started))
                backoff = MIN_BACKOFF
            except ProtectAuthError:
                self._metrics.finish_job("events", OUTCOME_AUTH_ERROR, self._since(started))
                raise
            except asyncio.CancelledError:
                self._metrics.job_in_progress.set(0, job="events")
                raise
            except Exception as exc:  # noqa: BLE001 - any transport error is retryable
                self._metrics.finish_job("events", OUTCOME_ERROR, self._since(started))
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
    metrics = default_metrics()
    metrics.describe_config(config)
    async with ProtectClient(config.protect, metrics=metrics) as protect:
        meta = await protect.get_meta()
        logger.info("connected to UniFi Protect %s", meta.get("applicationVersion", "(unknown)"))
        console = await _console_info(protect)
        metrics.describe_protect(host=config.protect.host, meta=meta, console=console)

        store = _open_store(config)
        try:
            await _serve(config, protect, store, meta=meta, console=console, metrics=metrics)
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
    metrics: Metrics | None = None,
) -> None:
    metrics = metrics or default_metrics()
    recorder = EventRecorder(store, metrics=metrics) if store is not None else None

    async with mqtt_client(config) as client:
        publisher = StatePublisher(config, AiomqttTransport(client), metrics=metrics)
        await publisher.publish_availability(True)
        service = OccupancyService(
            config,
            protect,
            publisher,
            recorder=recorder,
            meta=meta,
            console=console,
            metrics=metrics,
        )
        await publisher.publish_service_info(meta=meta, console=console)

        tasks = [asyncio.create_task(service.poll_forever(), name="poll")]
        exporter: MetricsServer | None = None
        if config.metrics.enabled:
            exporter = MetricsServer(config.metrics, metrics)
            await exporter.start()
            tasks.append(asyncio.create_task(exporter.serve_forever(), name="metrics"))
        judge: VisionJudge | None = None
        if config.health.enabled:
            judge = VisionJudge(config.health)
            monitor = CameraHealthMonitor(
                config.health,
                protect,
                publisher,
                judge,
                is_current=lambda camera_id: camera_id in service.cameras,
                metrics=metrics,
            )
            tasks.append(
                asyncio.create_task(
                    monitor.run_forever(lambda: list(service.cameras.values())),
                    name="health",
                )
            )
            logger.info(
                "checking camera health every %g minute(s) with %s at %s",
                config.health.interval_minutes,
                config.health.model,
                config.health.base_url,
            )
        trigger_judges: list[VisionTriggerJudge] = []
        for group in config.trigger_groups:
            lead = group[0]
            trigger_judge = VisionTriggerJudge(lead)
            trigger_judges.append(trigger_judge)
            watcher = TriggerWatcher(
                group,
                protect,
                publisher,
                trigger_judge,
                is_current=lambda camera_id: camera_id in service.cameras,
                metrics=metrics,
            )
            tasks.append(
                asyncio.create_task(
                    watcher.run_forever(lambda: list(service.cameras.values())),
                    name=watcher.job,
                )
            )
            if len(group) > 1:
                logger.info(
                    "asking %s together every %g minute(s) with %s at %s -- "
                    "one frame and one request covers all %d",
                    ", ".join(repr(trigger.name) for trigger in group),
                    lead.interval_minutes,
                    lead.model,
                    lead.base_url,
                    len(group),
                )
            for trigger in group:
                logger.info(
                    "watching %s for %r every %g minute(s) with %s at %s",
                    "every camera" if trigger.all_cameras else ", ".join(trigger.cameras),
                    trigger.name,
                    trigger.interval_minutes,
                    trigger.model,
                    trigger.base_url,
                )
                if trigger.publish_snapshot:
                    # Said out loud at startup, on purpose: this is the one
                    # setting that puts a picture on the broker, and whoever
                    # reads the log should not have to go and find config.yaml.
                    logger.warning(
                        "%r will publish the frame that matched to "
                        "%s/<camera>/trigger/%s/snapshot (unretained)",
                        trigger.name,
                        config.mqtt.topic_prefix,
                        trigger.key,
                    )
        if config.protect.use_websocket:
            tasks.append(asyncio.create_task(service.watch_events_forever(), name="events"))
        if store is not None and config.reports is not None:
            writer = SheetWriter(
                store,
                config.reports,
                cameras=lambda: list(service.cameras.values()),
                metrics=metrics,
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
            if judge is not None:
                with contextlib.suppress(Exception):
                    await judge.aclose()
            for trigger_judge in trigger_judges:
                with contextlib.suppress(Exception):
                    await trigger_judge.aclose()
            if exporter is not None:
                with contextlib.suppress(Exception):
                    await exporter.aclose()
            with contextlib.suppress(Exception):
                await publisher.publish_availability(False)
