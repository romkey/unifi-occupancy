"""Numbers about this process, in Prometheus' own text format.

The MQTT topics say what the cameras are doing. Nothing, until now, said what
*this* said about them: how long a poll takes, how often Protect refuses the API
key, whether the event websocket has been flapping all afternoon, how many
snapshots the health check paid for, whether the sheets are still being written.
That is the gap :mod:`unifi_occupancy.exporter` fills, and this is where the
counters live.

Deliberately hand-rolled rather than ``prometheus_client``. The exposition
format is a dozen lines of text, this service has one event loop and no
threads, and the dependency list is short on purpose -- every entry in it is
something that has to keep working on an arm64 Raspberry Pi for years. What is
here is the subset that is actually used: counters, gauges, histograms, labels,
and a registry that renders them.

Cardinality is kept in hand: per-camera series are the only ones that grow, a
camera that goes away has its series dropped (:meth:`Metrics.forget_cameras`),
and nothing is ever labelled with an event id, a timestamp or a Protect object
id.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import platform
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Final

from . import __version__
from .models import DETECTION_KEYS, Camera, trigger_kind
from .state import CameraState

try:  # pragma: no cover - absent only on Windows, which this does not target
    import resource
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: Every metric this service publishes starts with this.
NAMESPACE: Final = "unifi_occupancy"

#: What a Prometheus scrape expects back. Version 0.0.4 is the text format
#: every Prometheus and every Prometheus-compatible agent reads.
CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"

#: Buckets for things that should take milliseconds: one HTTP request to
#: Protect, one publish to the broker.
REQUEST_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

#: Buckets for things that take as long as they take: a poll, a health sweep
#: across twenty cameras, an hour's worth of sheet rendering.
JOB_BUCKETS: Final[tuple[float, ...]] = (
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    300.0,
    900.0,
)

#: The recurring background jobs, named once so the metric is self-describing
#: even before a job has run for the first time.
JOBS: Final[tuple[str, ...]] = ("poll", "events", "health_sweep", "sheets")

OUTCOME_SUCCESS: Final = "success"
#: A batched vision reply that was not an answer per trigger.
OUTCOME_UNREADABLE: Final = "unreadable"
OUTCOME_ERROR: Final = "error"
OUTCOME_AUTH_ERROR: Final = "auth_error"


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _payload_bytes(payload: str | bytes | None) -> int:
    """How much went over the wire. Bytes stay bytes; text is encoded first."""
    if not payload:
        return 0
    if isinstance(payload, bytes):
        return len(payload)
    return len(payload.encode("utf-8"))


def format_value(value: float) -> str:
    """A number the way the exposition format wants it."""
    if math.isnan(value):
        return "NaN"
    if value == math.inf:
        return "+Inf"
    if value == -math.inf:
        return "-Inf"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


class Metric:
    """Shared bookkeeping: a name, a help string and a set of label names."""

    type_name = "untyped"

    def __init__(self, name: str, documentation: str, labelnames: Sequence[str] = ()) -> None:
        self.name = name if name.startswith(f"{NAMESPACE}_") else f"{NAMESPACE}_{name}"
        self.documentation = documentation
        self.labelnames: tuple[str, ...] = tuple(labelnames)

    def _key(self, labels: Mapping[str, str]) -> tuple[str, ...]:
        if set(labels) != set(self.labelnames):
            raise ValueError(f"{self.name} takes labels {self.labelnames!r}, got {tuple(labels)!r}")
        return tuple(str(labels[name]) for name in self.labelnames)

    def _labels_text(self, key: Sequence[str], extra: str = "") -> str:
        pairs = [
            f'{name}="{_escape_label(value)}"'
            for name, value in zip(self.labelnames, key, strict=True)
        ]
        if extra:
            pairs.append(extra)
        if not pairs:
            return ""
        return "{" + ",".join(pairs) + "}"

    def _series(self) -> Iterable[tuple[str, ...]]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _drop(self, key: tuple[str, ...]) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def drop_matching(self, **labels: str) -> int:
        """Forget every series whose labels include these values.

        Used when a camera leaves the controller: its gauges would otherwise sit
        at whatever they last were for as long as the process runs, and a
        detection that is no longer being watched must not keep reporting that
        it is off.
        """
        unknown = set(labels) - set(self.labelnames)
        if unknown:
            raise ValueError(f"{self.name} has no label(s) {sorted(unknown)}")
        wanted = [(self.labelnames.index(name), value) for name, value in labels.items()]
        doomed = [
            key
            for key in list(self._series())
            if all(key[index] == value for index, value in wanted)
        ]
        for key in doomed:
            self._drop(key)
        return len(doomed)

    def expose(self) -> Iterator[str]:
        """The metric's HELP, TYPE and sample lines."""
        yield f"# HELP {self.name} {_escape_help(self.documentation)}"
        yield f"# TYPE {self.name} {self.type_name}"
        yield from self.samples()

    def samples(self) -> Iterator[str]:  # pragma: no cover - overridden
        raise NotImplementedError


class Counter(Metric):
    """A number that only ever goes up."""

    type_name = "counter"

    def __init__(self, name: str, documentation: str, labelnames: Sequence[str] = ()) -> None:
        super().__init__(name, documentation, labelnames)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise ValueError(f"{self.name} cannot decrease")
        key = self._key(labels)
        self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0.0)

    def prime(self, **labels: str) -> None:
        """Make a series exist at zero.

        A counter nobody has incremented yet is absent from a scrape, which
        reads in a dashboard exactly like a counter that is stuck. Priming the
        combinations we know up front -- the four detection kinds, the four jobs
        -- means a quiet service still answers the question.
        """
        self._values.setdefault(self._key(labels), 0.0)

    def _series(self) -> Iterable[tuple[str, ...]]:
        return list(self._values)

    def _drop(self, key: tuple[str, ...]) -> None:
        self._values.pop(key, None)

    def samples(self) -> Iterator[str]:
        for key, value in self._values.items():
            yield f"{self.name}{self._labels_text(key)} {format_value(value)}"


class Gauge(Metric):
    """A number that goes up and down."""

    type_name = "gauge"

    def __init__(self, name: str, documentation: str, labelnames: Sequence[str] = ()) -> None:
        super().__init__(name, documentation, labelnames)
        self._values: dict[tuple[str, ...], float] = {}

    def set(self, value: float, **labels: str) -> None:
        self._values[self._key(labels)] = float(value)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.inc(-amount, **labels)

    def set_now(self, clock: Callable[[], float] = time.time, **labels: str) -> None:
        self.set(clock(), **labels)

    def value(self, **labels: str) -> float | None:
        return self._values.get(self._key(labels))

    def _series(self) -> Iterable[tuple[str, ...]]:
        return list(self._values)

    def _drop(self, key: tuple[str, ...]) -> None:
        self._values.pop(key, None)

    def samples(self) -> Iterator[str]:
        for key, value in self._values.items():
            yield f"{self.name}{self._labels_text(key)} {format_value(value)}"


class Histogram(Metric):
    """Cumulative buckets, a sum and a count: how long something took."""

    type_name = "histogram"

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str] = (),
        buckets: Sequence[float] = REQUEST_BUCKETS,
    ) -> None:
        super().__init__(name, documentation, labelnames)
        bounds = tuple(sorted(float(bound) for bound in buckets))
        if not bounds:
            raise ValueError(f"{self.name} needs at least one bucket")
        self.buckets: tuple[float, ...] = (*bounds, math.inf)
        self._counts: dict[tuple[str, ...], list[float]] = {}
        self._sums: dict[tuple[str, ...], float] = {}

    def observe(self, seconds: float, **labels: str) -> None:
        key = self._key(labels)
        counts = self._counts.setdefault(key, [0.0] * len(self.buckets))
        for index, bound in enumerate(self.buckets):
            if seconds <= bound:
                counts[index] += 1.0
        self._sums[key] = self._sums.get(key, 0.0) + seconds

    def count(self, **labels: str) -> float:
        counts = self._counts.get(self._key(labels))
        return counts[-1] if counts else 0.0

    def total(self, **labels: str) -> float:
        return self._sums.get(self._key(labels), 0.0)

    def _series(self) -> Iterable[tuple[str, ...]]:
        return list(self._counts)

    def _drop(self, key: tuple[str, ...]) -> None:
        self._counts.pop(key, None)
        self._sums.pop(key, None)

    def samples(self) -> Iterator[str]:
        for key, counts in self._counts.items():
            for bound, count in zip(self.buckets, counts, strict=True):
                le = f'le="{format_value(bound)}"'
                yield f"{self.name}_bucket{self._labels_text(key, le)} {format_value(count)}"
            total = format_value(self._sums.get(key, 0.0))
            yield f"{self.name}_sum{self._labels_text(key)} {total}"
            yield f"{self.name}_count{self._labels_text(key)} {format_value(counts[-1])}"


class Registry:
    """Everything a scrape should see, in the order it was declared."""

    def __init__(self) -> None:
        self._metrics: list[Metric] = []
        self._callbacks: list[Callable[[], None]] = []

    def register(self, metric: Metric) -> Metric:
        if any(existing.name == metric.name for existing in self._metrics):
            raise ValueError(f"{metric.name} is already registered")
        self._metrics.append(metric)
        return metric

    def counter(self, name: str, documentation: str, labelnames: Sequence[str] = ()) -> Counter:
        metric = Counter(name, documentation, labelnames)
        self.register(metric)
        return metric

    def gauge(self, name: str, documentation: str, labelnames: Sequence[str] = ()) -> Gauge:
        metric = Gauge(name, documentation, labelnames)
        self.register(metric)
        return metric

    def histogram(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str] = (),
        buckets: Sequence[float] = REQUEST_BUCKETS,
    ) -> Histogram:
        metric = Histogram(name, documentation, labelnames, buckets)
        self.register(metric)
        return metric

    def before_render(self, callback: Callable[[], None]) -> None:
        """Register something to run at scrape time.

        For the handful of values that are cheaper to read when asked than to
        keep updated -- uptime, CPU time, how many tasks the loop is running.
        """
        self._callbacks.append(callback)

    @property
    def metrics(self) -> tuple[Metric, ...]:
        return tuple(self._metrics)

    def render(self) -> str:
        """The whole registry as one exposition-format document."""
        for callback in self._callbacks:
            try:
                callback()
            except Exception as exc:  # noqa: BLE001 - a scrape is not worth a crash
                logger.warning("could not refresh metrics: %s", exc)
        lines: list[str] = []
        for metric in self._metrics:
            lines.extend(metric.expose())
        return "\n".join(lines) + "\n"


class Metrics:
    """Every number this service reports, and the methods that move them.

    One instance per process, created in :func:`unifi_occupancy.app.run` and
    handed to the pieces that have something to count. Components take it as an
    optional argument and fall back to :func:`default_metrics`, so a test that
    does not care about metrics does not have to mention them, and a test that
    does can pass its own instance and read the counters back.
    """

    def __init__(
        self,
        registry: Registry | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.registry = registry or Registry()
        self._clock = clock
        self._wall = wall
        self._started_monotonic = clock()
        #: (camera slug, detection key) -> when it most recently went active,
        #: so ``detection_seconds_total`` can be accumulated on each poll.
        self._active_since: dict[tuple[str, str], float] = {}
        self._cpu_seconds = 0.0

        registry = self.registry

        # -- what this is -------------------------------------------------
        self.build_info = registry.gauge(
            "build_info",
            "Always 1; the labels carry this build's version and runtime.",
            ("version", "python", "platform"),
        )
        self.build_info.set(
            1,
            version=__version__,
            python=platform.python_version(),
            platform=platform.machine(),
        )
        self.start_time_seconds = registry.gauge(
            "start_time_seconds", "Unix time at which this process started."
        )
        self.start_time_seconds.set(wall())
        self.uptime_seconds = registry.gauge(
            "uptime_seconds", "Seconds since this process started."
        )
        self.feature_enabled = registry.gauge(
            "feature_enabled",
            "1 for each optional feature that is switched on, 0 for each that is not.",
            ("feature",),
        )
        self.configured_seconds = registry.gauge(
            "configured_seconds",
            "Configured intervals and holds, in seconds.",
            ("setting",),
        )
        self.protect_info = registry.gauge(
            "protect_info",
            "Always 1; the labels describe the controller and console being read.",
            ("host", "application_version", "console_type", "console_version", "console_hardware"),
        )

        # -- the process itself -------------------------------------------
        self.process_cpu_seconds = registry.counter(
            "process_cpu_seconds_total", "CPU seconds this process has used, user plus system."
        )
        self.process_max_resident_bytes = registry.gauge(
            "process_max_resident_bytes", "Peak resident set size of this process, in bytes."
        )
        self.asyncio_tasks = registry.gauge(
            "asyncio_tasks", "Tasks currently alive on the event loop."
        )

        # -- talking to Protect -------------------------------------------
        self.protect_requests = registry.counter(
            "protect_requests_total",
            "Requests made to the Protect integration API, by endpoint and outcome.",
            ("endpoint", "outcome"),
        )
        self.protect_request_duration = registry.histogram(
            "protect_request_duration_seconds",
            "How long Protect took to answer, by endpoint.",
            ("endpoint",),
        )
        self.protect_up = registry.gauge(
            "protect_up", "1 when the most recent camera poll succeeded, 0 when it did not."
        )
        self.protect_cameras_polled = registry.gauge(
            "protect_cameras_polled", "Cameras returned by the most recent poll."
        )
        self.websocket_connected = registry.gauge(
            "websocket_connected", "1 while the Protect event websocket is subscribed."
        )
        self.websocket_connects = registry.counter(
            "websocket_connects_total", "Times the event websocket has been subscribed to."
        )
        self.websocket_failures = registry.counter(
            "websocket_failures_total",
            "Times the event websocket dropped or refused to connect, by cause.",
            ("cause",),
        )
        self.event_frames = registry.counter(
            "event_frames_total",
            "Frames read from the event websocket, by whether they held a detection.",
            ("outcome",),
        )
        self.events = registry.counter(
            "events_total",
            "Detection events applied, by detection kind and whether they started or ended.",
            ("kind", "phase"),
        )

        # -- the cameras ---------------------------------------------------
        self.cameras = registry.gauge("cameras", "Cameras currently known to this service.")
        self.cameras_connected = registry.gauge(
            "cameras_connected", "Cameras the controller currently reports as connected."
        )
        self.camera_info = registry.gauge(
            "camera_info",
            "Always 1; the labels describe one camera.",
            ("camera", "name", "model", "hardware", "firmware"),
        )
        self.camera_connected = registry.gauge(
            "camera_connected",
            "1 when the controller reports this camera as connected.",
            ("camera",),
        )
        self.camera_detection_active = registry.gauge(
            "camera_detection_active",
            "1 while this detection is firing on this camera.",
            ("camera", "kind"),
        )
        self.camera_detection_enabled = registry.gauge(
            "camera_detection_enabled",
            "1 when this detection is switched on for this camera.",
            ("camera", "kind"),
        )
        self.camera_detection_supported = registry.gauge(
            "camera_detection_supported",
            "1 when this camera's hardware can do this detection at all.",
            ("camera", "kind"),
        )
        self.detections = registry.counter(
            "detections_total",
            "Times a detection has gone from off to on, by camera and kind.",
            ("camera", "kind"),
        )
        self.detection_seconds = registry.counter(
            "detection_seconds_total",
            "Seconds a detection has spent on, by camera and kind.",
            ("camera", "kind"),
        )

        # -- the broker ----------------------------------------------------
        self.mqtt_publishes = registry.counter(
            "mqtt_publishes_total", "Messages handed to the broker, by outcome.", ("outcome",)
        )
        self.mqtt_published_bytes = registry.counter(
            "mqtt_published_bytes_total", "Payload bytes published, excluding topics."
        )
        self.mqtt_suppressed = registry.counter(
            "mqtt_publishes_suppressed_total",
            "Publishes skipped because the payload had not changed.",
        )
        self.mqtt_last_publish = registry.gauge(
            "mqtt_last_publish_timestamp_seconds", "Unix time of the most recent publish."
        )

        # -- the background jobs -------------------------------------------
        self.job_runs = registry.counter(
            "job_runs_total",
            "Completed runs of each background job, by outcome.",
            ("job", "outcome"),
        )
        self.job_duration = registry.histogram(
            "job_duration_seconds",
            "How long each background job takes.",
            ("job",),
            buckets=JOB_BUCKETS,
        )
        self.job_in_progress = registry.gauge(
            "job_in_progress", "1 while a background job is running.", ("job",)
        )
        self.job_last_success = registry.gauge(
            "job_last_success_timestamp_seconds",
            "Unix time at which each background job last finished without error.",
            ("job",),
        )

        # -- the camera health check ----------------------------------------
        self.health_checks = registry.counter(
            "health_checks_total",
            "Camera health checks completed, by camera and verdict.",
            ("camera", "verdict"),
        )
        self.health_check_duration = registry.histogram(
            "health_check_duration_seconds",
            "How long one camera health check takes, snapshot and model together.",
            buckets=JOB_BUCKETS,
        )
        self.camera_health = registry.gauge(
            "camera_health",
            "1 for the camera's most recent health verdict, 0 for the others.",
            ("camera", "status"),
        )
        self.health_last_check = registry.gauge(
            "health_last_check_timestamp_seconds",
            "Unix time at which each camera was last checked.",
            ("camera",),
        )

        # -- custom triggers --------------------------------------------------
        self.trigger_checks = registry.counter(
            "trigger_checks_total",
            "Custom trigger checks completed, by trigger, camera and answer.",
            ("trigger", "camera", "answer"),
        )
        self.trigger_check_duration = registry.histogram(
            "trigger_check_duration_seconds",
            "How long one custom trigger check takes, snapshot and model together.",
            ("trigger",),
            buckets=JOB_BUCKETS,
        )
        self.trigger_state = registry.gauge(
            "trigger_state",
            "1 for each trigger's most recent answer about a camera, 0 for the others.",
            ("trigger", "camera", "state"),
        )
        self.trigger_last_check = registry.gauge(
            "trigger_last_check_timestamp_seconds",
            "Unix time at which each trigger last checked each camera.",
            ("trigger", "camera"),
        )
        self.trigger_batches = registry.counter(
            "trigger_batch_calls_total",
            "Vision calls that asked several triggers at once, by outcome. "
            "`unreadable` fell back to asking each trigger separately.",
            ("outcome",),
        )
        self.trigger_snapshots = registry.counter(
            "trigger_snapshots_total",
            "Matching frames published, by trigger and camera. Only triggers "
            "with publish_snapshot on can ever increment this.",
            ("trigger", "camera"),
        )

        # -- the occupancy history and sheets --------------------------------
        self.recorded_edges = registry.counter(
            "recorded_edges_total",
            "Detection and trigger edges written to the history database, by kind and edge.",
            ("kind", "edge"),
        )
        self.recorder_failures = registry.counter(
            "recorder_failures_total",
            "Writes to the history database that failed, by operation.",
            ("operation",),
        )
        self.sheets_written = registry.counter(
            "sheets_written_total", "Daily sheet files written, index included."
        )

        # -- this endpoint ---------------------------------------------------
        self.http_requests = registry.counter(
            "http_requests_total",
            "Requests to the metrics endpoint, by path and response status.",
            ("path", "status"),
        )
        self.http_request_duration = registry.histogram(
            "http_request_duration_seconds", "How long this endpoint took to answer."
        )

        self._prime()
        registry.before_render(self._refresh)

    # -- setting the scene ---------------------------------------------------

    def _prime(self) -> None:
        """Make the fixed label combinations exist before anything happens."""
        for kind in DETECTION_KEYS:
            for phase in ("start", "end"):
                self.events.prime(kind=kind, phase=phase)
            for edge in ("start", "end"):
                self.recorded_edges.prime(kind=kind, edge=edge)
        for job in JOBS:
            self.job_in_progress.set(0, job=job)
            for outcome in (OUTCOME_SUCCESS, OUTCOME_ERROR):
                self.job_runs.prime(job=job, outcome=outcome)
        for outcome in (OUTCOME_SUCCESS, OUTCOME_ERROR):
            self.mqtt_publishes.prime(outcome=outcome)
        for outcome in ("detection", "ignored"):
            self.event_frames.prime(outcome=outcome)
        self.websocket_connected.set(0)

    def describe_config(self, config: object) -> None:
        """Record which features are on and how the intervals are set.

        Typed loosely on purpose: this is the one place that wants to read the
        whole :class:`~unifi_occupancy.config.Config`, and importing it here
        would make ``config`` depend on ``metrics`` depend on ``config``.
        """
        from .config import Config

        if not isinstance(config, Config):  # pragma: no cover - defensive
            return
        reports = config.reports
        self.feature_enabled.set(float(config.protect.use_websocket), feature="websocket")
        self.feature_enabled.set(float(config.discovery.enabled), feature="ha_discovery")
        self.feature_enabled.set(
            float(reports is not None and reports.enabled), feature="occupancy_sheets"
        )
        self.feature_enabled.set(float(config.health.enabled), feature="camera_health")
        self.feature_enabled.set(float(bool(config.active_triggers)), feature="custom_triggers")
        # Worth its own series: this is the one setting that makes the service
        # publish an image, so "is anything sending frames to the broker right
        # now" should be answerable without reading config.yaml.
        self.feature_enabled.set(
            float(any(trigger.publish_snapshot for trigger in config.active_triggers)),
            feature="trigger_snapshots",
        )
        self.feature_enabled.set(
            float(
                reports is not None
                and reports.enabled
                and any(trigger.record for trigger in config.active_triggers)
            ),
            feature="trigger_history",
        )
        self.feature_enabled.set(float(config.metrics.enabled), feature="metrics")
        self.configured_seconds.set(config.protect.poll_interval, setting="poll_interval")
        self.configured_seconds.set(config.detection_hold_seconds, setting="detection_hold")
        self.configured_seconds.set(config.health.interval_seconds, setting="health_interval")
        if reports is not None:
            self.configured_seconds.set(
                reports.refresh_minutes * 60.0, setting="sheets_refresh_interval"
            )
        for group in config.trigger_groups:
            if len(group) > 1:
                # So the series exists before the first sweep, the way JOBS
                # does: no batched calls yet should read as zero, not absent.
                for outcome in (OUTCOME_SUCCESS, OUTCOME_UNREADABLE, OUTCOME_ERROR):
                    self.trigger_batches.inc(0, outcome=outcome)
        for trigger in config.active_triggers:
            # Make each trigger's job series exist before its first sweep, the
            # way JOBS does for the fixed ones: a trigger that has never
            # managed to run should read as zero, not as absent.
            job = f"trigger:{trigger.key}"
            self.job_in_progress.set(0, job=job)
            for outcome in (OUTCOME_SUCCESS, OUTCOME_ERROR):
                self.job_runs.prime(job=job, outcome=outcome)
            self.configured_seconds.set(
                trigger.interval_seconds, setting=f"trigger_{trigger.key}_interval"
            )
            if trigger.record:
                # So a trigger that is being written to the history reads as
                # zero edges before its first one, not as absent -- the same
                # reason the detection kinds are primed.
                for edge in ("start", "end"):
                    self.recorded_edges.prime(kind=trigger_kind(trigger.key), edge=edge)

    def describe_protect(
        self,
        *,
        host: str,
        meta: Mapping[str, object] | None = None,
        console: Mapping[str, object] | None = None,
    ) -> None:
        """Record what we are talking to, as labels on a constant gauge."""

        def text(source: Mapping[str, object] | None, key: str) -> str:
            value = (source or {}).get(key)
            return value if isinstance(value, str) else ""

        self.protect_info.set(
            1,
            host=host,
            application_version=text(meta, "applicationVersion"),
            console_type=text(console, "type"),
            console_version=text(console, "version"),
            console_hardware=text(console, "hardwarePlatform"),
        )

    # -- scrape time ---------------------------------------------------------

    def _refresh(self) -> None:
        """Update the handful of values that are read rather than counted."""
        self.uptime_seconds.set(self._clock() - self._started_monotonic)
        if resource is not None:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            cpu = usage.ru_utime + usage.ru_stime
            if cpu > self._cpu_seconds:
                self.process_cpu_seconds.inc(cpu - self._cpu_seconds)
                self._cpu_seconds = cpu
            # ru_maxrss is kilobytes on Linux and bytes on macOS.
            scale = 1 if platform.system() == "Darwin" else 1024
            self.process_max_resident_bytes.set(usage.ru_maxrss * scale)
        # No running loop means a synchronous caller -- a test, or ``--rebuild-sheets``.
        with contextlib.suppress(RuntimeError):
            self.asyncio_tasks.set(len(asyncio.all_tasks()))

    def render(self) -> str:
        return self.registry.render()

    # -- Protect -------------------------------------------------------------

    def record_protect_request(self, endpoint: str, outcome: str, seconds: float) -> None:
        self.protect_requests.inc(endpoint=endpoint, outcome=outcome)
        self.protect_request_duration.observe(seconds, endpoint=endpoint)

    def record_websocket_connected(self) -> None:
        self.websocket_connected.set(1)
        self.websocket_connects.inc()

    def record_websocket_closed(self) -> None:
        self.websocket_connected.set(0)

    def record_websocket_failure(self, cause: str) -> None:
        self.websocket_connected.set(0)
        self.websocket_failures.inc(cause=cause)

    def record_event_frame(self, *, decoded: bool) -> None:
        self.event_frames.inc(outcome="detection" if decoded else "ignored")

    def record_event(self, kinds: Iterable[str], *, start: bool) -> None:
        phase = "start" if start else "end"
        for kind in kinds:
            self.events.inc(kind=kind, phase=phase)

    # -- jobs ----------------------------------------------------------------

    def start_job(self, job: str) -> None:
        self.job_in_progress.set(1, job=job)

    def finish_job(self, job: str, outcome: str, seconds: float) -> None:
        self.job_in_progress.set(0, job=job)
        self.job_runs.inc(job=job, outcome=outcome)
        self.job_duration.observe(seconds, job=job)
        if outcome == OUTCOME_SUCCESS:
            self.job_last_success.set(self._wall(), job=job)

    # -- cameras -------------------------------------------------------------

    def observe_cameras(self, cameras: Sequence[Camera]) -> None:
        """Record the shape of the inventory after a poll."""
        self.cameras.set(len(cameras))
        self.cameras_connected.set(sum(1 for camera in cameras if camera.connected))
        self.protect_cameras_polled.set(len(cameras))
        for camera in cameras:
            slug = camera.slug
            self.camera_info.drop_matching(camera=slug)
            self.camera_info.set(
                1,
                camera=slug,
                name=camera.name,
                model=camera.model or "",
                hardware=camera.hardware or "",
                firmware=camera.firmware or "",
            )
            self.camera_connected.set(float(camera.connected), camera=slug)
            for kind in DETECTION_KEYS:
                self.camera_detection_supported.set(
                    float(camera.is_supported(kind)), camera=slug, kind=kind
                )

    def observe_state(self, state: CameraState, now: float | None = None) -> None:
        """Record one camera's published state, and how long it has held.

        ``now`` is the same monotonic instant the publisher used, so the seconds
        counted here are exactly the seconds the broker was told about.
        """
        moment = self._clock() if now is None else now
        slug = state.camera.slug
        for kind in DETECTION_KEYS:
            active = state.active(kind)
            self.camera_detection_active.set(float(active), camera=slug, kind=kind)
            self.camera_detection_enabled.set(
                float(bool(state.enabled.get(kind, False))), camera=slug, kind=kind
            )
            pair = (slug, kind)
            since = self._active_since.get(pair)
            if active:
                if since is None:
                    self.detections.inc(camera=slug, kind=kind)
                else:
                    self.detection_seconds.inc(max(0.0, moment - since), camera=slug, kind=kind)
                self._active_since[pair] = moment
            elif since is not None:
                self.detection_seconds.inc(max(0.0, moment - since), camera=slug, kind=kind)
                del self._active_since[pair]
            else:
                # Make the counters exist even for a camera that never fires.
                self.detections.prime(camera=slug, kind=kind)
                self.detection_seconds.prime(camera=slug, kind=kind)

    def forget_cameras(self, slugs: Iterable[str]) -> None:
        """Drop every series belonging to cameras that have gone away."""
        per_camera: tuple[Metric, ...] = (
            self.camera_info,
            self.camera_connected,
            self.camera_detection_active,
            self.camera_detection_enabled,
            self.camera_detection_supported,
            self.detections,
            self.detection_seconds,
            self.health_checks,
            self.camera_health,
            self.health_last_check,
            self.trigger_checks,
            self.trigger_state,
            self.trigger_last_check,
            self.trigger_snapshots,
        )
        for slug in slugs:
            for metric in per_camera:
                metric.drop_matching(camera=slug)
            for pair in [pair for pair in self._active_since if pair[0] == slug]:
                del self._active_since[pair]

    # -- MQTT ----------------------------------------------------------------

    def record_publish(
        self, payload: str | bytes | None, *, outcome: str = OUTCOME_SUCCESS
    ) -> None:
        self.mqtt_publishes.inc(outcome=outcome)
        if outcome == OUTCOME_SUCCESS:
            self.mqtt_published_bytes.inc(_payload_bytes(payload))
            self.mqtt_last_publish.set(self._wall())

    def record_publish_suppressed(self) -> None:
        self.mqtt_suppressed.inc()

    # -- camera health -------------------------------------------------------

    def record_health_check(self, camera: Camera, verdict: str, seconds: float) -> None:
        slug = camera.slug
        self.health_checks.inc(camera=slug, verdict=verdict)
        self.health_check_duration.observe(seconds)
        self.health_last_check.set(self._wall(), camera=slug)
        for status in ("good", "bad", "unknown"):
            self.camera_health.set(float(status == verdict), camera=slug, status=status)

    # -- custom triggers -----------------------------------------------------

    def record_trigger_check(
        self, trigger: str, camera: Camera, answer: str, seconds: float
    ) -> None:
        slug = camera.slug
        self.trigger_checks.inc(trigger=trigger, camera=slug, answer=answer)
        self.trigger_check_duration.observe(seconds, trigger=trigger)
        self.trigger_last_check.set(self._wall(), trigger=trigger, camera=slug)
        for state in ("match", "clear", "unknown"):
            self.trigger_state.set(
                float(state == answer), trigger=trigger, camera=slug, state=state
            )

    def record_trigger_batch(self, outcome: str) -> None:
        """One call that asked several triggers at once."""
        self.trigger_batches.inc(outcome=outcome)

    def record_trigger_snapshot(self, trigger: str, camera: Camera) -> None:
        """One matching frame went to the broker."""
        self.trigger_snapshots.inc(trigger=trigger, camera=camera.slug)

    # -- history and sheets --------------------------------------------------

    def record_edge(self, kind: str, *, start: bool) -> None:
        self.recorded_edges.inc(kind=kind, edge="start" if start else "end")

    def record_recorder_failure(self, operation: str) -> None:
        self.recorder_failures.inc(operation=operation)

    def record_sheets_written(self, count: int = 1) -> None:
        self.sheets_written.inc(count)

    # -- this endpoint -------------------------------------------------------

    def record_http_request(self, path: str, status: int, seconds: float) -> None:
        self.http_requests.inc(path=path, status=str(status))
        self.http_request_duration.observe(seconds)


_DEFAULT: Metrics | None = None


def default_metrics() -> Metrics:
    """The process-wide :class:`Metrics`, created on first use."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Metrics()
    return _DEFAULT
