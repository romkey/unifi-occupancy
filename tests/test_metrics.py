from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import camera_payload
from unifi_occupancy.config import Config, ReportConfig, TriggerConfig
from unifi_occupancy.metrics import (
    CONTENT_TYPE,
    NAMESPACE,
    Counter,
    Gauge,
    Histogram,
    Metrics,
    Registry,
    default_metrics,
    format_value,
)
from unifi_occupancy.models import MOTION, PERSON, parse_camera
from unifi_occupancy.state import CameraState, DetectionTracker


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def metrics() -> Metrics:
    return Metrics(clock=Clock(), wall=lambda: 1_700_000_000.0)


def samples(text: str, name: str) -> dict[str, float]:
    """Every sample line of one metric, keyed by its label text."""
    found: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        head, _, value = line.rpartition(" ")
        if head == name or head.startswith(f"{name}{{"):
            found[head[len(name) :]] = float(value)
    return found


# -- the primitives ---------------------------------------------------------


def test_names_are_namespaced() -> None:
    assert Counter("widgets_total", "x").name == f"{NAMESPACE}_widgets_total"
    assert Counter(f"{NAMESPACE}_widgets_total", "x").name == f"{NAMESPACE}_widgets_total"


def test_format_value_renders_what_prometheus_expects() -> None:
    assert format_value(3.0) == "3"
    assert format_value(0.5) == "0.5"
    assert format_value(math.inf) == "+Inf"
    assert format_value(-math.inf) == "-Inf"
    assert format_value(math.nan) == "NaN"


def test_counter_counts_per_label_set() -> None:
    counter = Counter("things_total", "Things.", ("kind",))
    counter.inc(kind="motion")
    counter.inc(2, kind="motion")
    counter.inc(kind="person")

    assert counter.value(kind="motion") == 3
    assert counter.value(kind="person") == 1
    assert counter.value(kind="siren") == 0


def test_counter_refuses_to_go_backwards() -> None:
    counter = Counter("things_total", "Things.")
    with pytest.raises(ValueError, match="cannot decrease"):
        counter.inc(-1)


def test_a_metric_insists_on_its_own_labels() -> None:
    counter = Counter("things_total", "Things.", ("kind",))
    with pytest.raises(ValueError, match="takes labels"):
        counter.inc(camera="front_door")


def test_priming_makes_a_series_exist_at_zero() -> None:
    counter = Counter("things_total", "Things.", ("kind",))
    counter.prime(kind="smoke")

    assert 'things_total{kind="smoke"} 0' in "\n".join(counter.expose())

    counter.inc(kind="smoke")
    counter.prime(kind="smoke")

    assert counter.value(kind="smoke") == 1


def test_gauge_moves_both_ways() -> None:
    gauge = Gauge("level", "Level.")
    gauge.set(5)
    gauge.inc(2)
    gauge.dec(3)

    assert gauge.value() == 4


def test_histogram_buckets_are_cumulative() -> None:
    histogram = Histogram("wait_seconds", "Waits.", buckets=(0.1, 1.0))
    histogram.observe(0.05)
    histogram.observe(0.5)
    histogram.observe(5.0)

    text = "\n".join(histogram.expose())
    counts = samples(text, f"{NAMESPACE}_wait_seconds_bucket")

    assert counts['{le="0.1"}'] == 1
    assert counts['{le="1"}'] == 2
    assert counts['{le="+Inf"}'] == 3
    assert histogram.count() == 3
    assert histogram.total() == pytest.approx(5.55)


def test_histogram_wants_at_least_one_bucket() -> None:
    with pytest.raises(ValueError, match="at least one bucket"):
        Histogram("wait_seconds", "Waits.", buckets=())


def test_label_values_and_help_are_escaped() -> None:
    gauge = Gauge("thing_info", 'A "thing"\nover two lines.', ("name",))
    gauge.set(1, name='say "hi"\\there')

    text = "\n".join(gauge.expose())

    assert '# HELP unifi_occupancy_thing_info A "thing"\\nover two lines.' in text
    assert r'name="say \"hi\"\\there"' in text


def test_drop_matching_forgets_one_camera() -> None:
    gauge = Gauge("detection", "Detections.", ("camera", "kind"))
    gauge.set(1, camera="front_door", kind=MOTION)
    gauge.set(1, camera="front_door", kind=PERSON)
    gauge.set(0, camera="back_yard", kind=MOTION)

    assert gauge.drop_matching(camera="front_door") == 2
    assert gauge.value(camera="front_door", kind=MOTION) is None
    assert gauge.value(camera="back_yard", kind=MOTION) == 0

    with pytest.raises(ValueError, match="no label"):
        gauge.drop_matching(nonsense="x")


def test_registry_renders_help_and_type_once_per_metric() -> None:
    registry = Registry()
    registry.counter("things_total", "Things.", ("kind",)).inc(kind="motion")

    text = registry.render()

    assert "# HELP unifi_occupancy_things_total Things." in text
    assert "# TYPE unifi_occupancy_things_total counter" in text
    assert text.endswith("\n")


def test_registry_refuses_a_duplicate_name() -> None:
    registry = Registry()
    registry.gauge("level", "Level.")
    with pytest.raises(ValueError, match="already registered"):
        registry.gauge("level", "Level.")


def test_render_runs_the_scrape_time_callbacks() -> None:
    registry = Registry()
    level = registry.gauge("level", "Level.")
    registry.before_render(lambda: level.set(7))

    assert "unifi_occupancy_level 7" in registry.render()


def test_a_broken_callback_does_not_break_the_scrape() -> None:
    registry = Registry()
    registry.gauge("level", "Level.").set(1)

    def explode() -> None:
        raise RuntimeError("no")

    registry.before_render(explode)

    assert "unifi_occupancy_level 1" in registry.render()


# -- the service's own numbers ----------------------------------------------


def test_build_info_and_uptime_are_there_from_the_start(metrics: Metrics) -> None:
    text = metrics.render()

    assert "unifi_occupancy_build_info{" in text
    assert "unifi_occupancy_uptime_seconds 0" in text
    assert "unifi_occupancy_start_time_seconds 1700000000" in text
    assert CONTENT_TYPE.startswith("text/plain; version=0.0.4")


def test_every_rendered_line_is_a_comment_or_a_sample(metrics: Metrics) -> None:
    metrics.record_protect_request("/cameras", "success", 0.2)
    metrics.observe_cameras([parse_camera(camera_payload(name='A "odd" name'))])

    for line in metrics.render().splitlines():
        if line.startswith("#"):
            assert line.split()[1] in {"HELP", "TYPE"}
            continue
        head, _, value = line.rpartition(" ")
        assert head.startswith(NAMESPACE)
        float(value)


def test_quiet_counters_still_report_zero(metrics: Metrics) -> None:
    text = metrics.render()

    assert samples(text, "unifi_occupancy_events_total")['{kind="motion",phase="start"}'] == 0
    assert samples(text, "unifi_occupancy_job_runs_total")['{job="poll",outcome="success"}'] == 0
    assert "unifi_occupancy_websocket_connected 0" in text


def test_protect_requests_are_counted_by_endpoint_and_outcome(metrics: Metrics) -> None:
    metrics.record_protect_request("/cameras", "success", 0.02)
    metrics.record_protect_request("/cameras", "auth_error", 0.01)

    text = metrics.render()
    requests = samples(text, "unifi_occupancy_protect_requests_total")

    assert requests['{endpoint="/cameras",outcome="success"}'] == 1
    assert requests['{endpoint="/cameras",outcome="auth_error"}'] == 1
    assert metrics.protect_request_duration.count(endpoint="/cameras") == 2


def test_websocket_connects_drops_and_failures(metrics: Metrics) -> None:
    metrics.record_websocket_connected()
    assert metrics.websocket_connected.value() == 1

    metrics.record_websocket_closed()
    assert metrics.websocket_connected.value() == 0

    metrics.record_websocket_failure("ConnectionClosedError")

    assert metrics.websocket_connects.value() == 1
    assert metrics.websocket_failures.value(cause="ConnectionClosedError") == 1


def test_event_frames_and_events(metrics: Metrics) -> None:
    metrics.record_event_frame(decoded=True)
    metrics.record_event_frame(decoded=False)
    metrics.record_event([MOTION, PERSON], start=True)
    metrics.record_event([MOTION], start=False)

    assert metrics.event_frames.value(outcome="detection") == 1
    assert metrics.event_frames.value(outcome="ignored") == 1
    assert metrics.events.value(kind=PERSON, phase="start") == 1
    assert metrics.events.value(kind=MOTION, phase="end") == 1


def test_jobs_record_duration_outcome_and_last_success(metrics: Metrics) -> None:
    metrics.start_job("poll")
    assert metrics.job_in_progress.value(job="poll") == 1

    metrics.finish_job("poll", "success", 1.5)

    assert metrics.job_in_progress.value(job="poll") == 0
    assert metrics.job_runs.value(job="poll", outcome="success") == 1
    assert metrics.job_duration.total(job="poll") == 1.5
    assert metrics.job_last_success.value(job="poll") == 1_700_000_000.0

    metrics.finish_job("poll", "error", 0.5)

    assert metrics.job_runs.value(job="poll", outcome="error") == 1
    assert metrics.job_last_success.value(job="poll") == 1_700_000_000.0


def test_cameras_are_counted_and_described(metrics: Metrics) -> None:
    cameras = [
        parse_camera(camera_payload()),
        parse_camera(camera_payload(id="cam-2", name="Back Yard", state="DISCONNECTED")),
    ]

    metrics.observe_cameras(cameras)
    text = metrics.render()

    assert "unifi_occupancy_cameras 2" in text
    assert "unifi_occupancy_cameras_connected 1" in text
    assert samples(text, "unifi_occupancy_camera_connected")['{camera="back_yard"}'] == 0
    info = samples(text, "unifi_occupancy_camera_info")
    assert any(
        'name="Front Door"' in key and 'hardware="UVC G4 Doorbell Pro"' in key for key in info
    )
    supported = samples(text, "unifi_occupancy_camera_detection_supported")
    assert supported['{camera="front_door",kind="person"}'] == 1


def test_renaming_a_camera_does_not_leave_two_info_series(metrics: Metrics) -> None:
    metrics.observe_cameras([parse_camera(camera_payload(name="Front Door"))])
    metrics.observe_cameras([parse_camera(camera_payload(name="Front  Door"))])

    info = samples(metrics.render(), "unifi_occupancy_camera_info")

    assert len([key for key in info if 'camera="front_door"' in key]) == 1


def test_detection_edges_and_seconds(metrics: Metrics) -> None:
    camera = parse_camera(camera_payload())
    tracker = DetectionTracker(hold_seconds=30.0)

    off = tracker.snapshot(camera, 0.0)
    metrics.observe_state(off, 0.0)

    assert metrics.detections.value(camera="front_door", kind=PERSON) == 0

    on = CameraState(camera=camera, detections={PERSON: True}, enabled={PERSON: True})
    metrics.observe_state(on, 10.0)

    assert metrics.detections.value(camera="front_door", kind=PERSON) == 1
    assert metrics.camera_detection_active.value(camera="front_door", kind=PERSON) == 1
    assert metrics.detection_seconds.value(camera="front_door", kind=PERSON) == 0

    metrics.observe_state(on, 15.0)
    assert metrics.detection_seconds.value(camera="front_door", kind=PERSON) == 5
    assert metrics.detections.value(camera="front_door", kind=PERSON) == 1

    metrics.observe_state(off, 20.0)

    assert metrics.detection_seconds.value(camera="front_door", kind=PERSON) == 10
    assert metrics.camera_detection_active.value(camera="front_door", kind=PERSON) == 0

    metrics.observe_state(on, 25.0)
    assert metrics.detections.value(camera="front_door", kind=PERSON) == 2


def test_a_camera_that_goes_away_takes_its_series_with_it(metrics: Metrics) -> None:
    camera = parse_camera(camera_payload())
    metrics.observe_cameras([camera])
    metrics.observe_state(
        CameraState(camera=camera, detections={MOTION: True}, enabled={MOTION: True}), 1.0
    )
    metrics.record_health_check(camera, "good", 0.5)

    metrics.forget_cameras(["front_door"])
    text = metrics.render()

    assert 'camera="front_door"' not in text
    assert metrics.camera_health.value(camera="front_door", status="good") is None


def test_publishes_bytes_and_suppressions(metrics: Metrics) -> None:
    metrics.record_publish("ON")
    metrics.record_publish(None)
    metrics.record_publish("boom", outcome="error")
    metrics.record_publish_suppressed()

    assert metrics.mqtt_publishes.value(outcome="success") == 2
    assert metrics.mqtt_publishes.value(outcome="error") == 1
    assert metrics.mqtt_published_bytes.value() == 2
    assert metrics.mqtt_suppressed.value() == 1
    assert metrics.mqtt_last_publish.value() == 1_700_000_000.0


def test_health_verdicts_are_one_hot(metrics: Metrics) -> None:
    camera = parse_camera(camera_payload())

    metrics.record_health_check(camera, "bad", 2.0)

    assert metrics.camera_health.value(camera="front_door", status="bad") == 1
    assert metrics.camera_health.value(camera="front_door", status="good") == 0

    metrics.record_health_check(camera, "good", 1.0)

    assert metrics.camera_health.value(camera="front_door", status="good") == 1
    assert metrics.camera_health.value(camera="front_door", status="bad") == 0
    assert metrics.health_checks.value(camera="front_door", verdict="bad") == 1
    assert metrics.health_check_duration.count() == 2
    assert metrics.health_last_check.value(camera="front_door") == 1_700_000_000.0


def test_history_and_sheets(metrics: Metrics) -> None:
    metrics.record_edge(MOTION, start=True)
    metrics.record_edge(MOTION, start=False)
    metrics.record_recorder_failure("event")
    metrics.record_sheets_written()
    metrics.record_sheets_written(2)

    assert metrics.recorded_edges.value(kind=MOTION, edge="start") == 1
    assert metrics.recorded_edges.value(kind=MOTION, edge="end") == 1
    assert metrics.recorder_failures.value(operation="event") == 1
    assert metrics.sheets_written.value() == 3


def test_http_requests_are_counted(metrics: Metrics) -> None:
    metrics.record_http_request("/metrics", 200, 0.01)
    metrics.record_http_request("other", 401, 0.0)

    requests = samples(metrics.render(), "unifi_occupancy_http_requests_total")

    assert requests['{path="/metrics",status="200"}'] == 1
    assert requests['{path="other",status="401"}'] == 1


def test_describe_config_reports_the_features_that_are_on(config: Config, metrics: Metrics) -> None:
    metrics.describe_config(config)
    text = metrics.render()
    features = samples(text, "unifi_occupancy_feature_enabled")

    assert features['{feature="websocket"}'] == 1
    assert features['{feature="camera_health"}'] == 0
    assert features['{feature="occupancy_sheets"}'] == 0
    assert features['{feature="metrics"}'] == 0
    settings = samples(text, "unifi_occupancy_configured_seconds")
    assert settings['{setting="detection_hold"}'] == 30
    assert settings['{setting="poll_interval"}'] == pytest.approx(0.01)


def test_describe_config_ignores_something_that_is_not_a_config(metrics: Metrics) -> None:
    metrics.describe_config(object())

    assert metrics.feature_enabled.value(feature="websocket") is None


def test_describe_protect_labels_the_controller(metrics: Metrics) -> None:
    metrics.describe_protect(
        host="protect.example",
        meta={"applicationVersion": "6.3.52"},
        console={"type": "UDMPRO", "version": "6.3.52", "hardwarePlatform": "al324"},
    )

    text = metrics.render()

    assert 'host="protect.example"' in text
    assert 'application_version="6.3.52"' in text
    assert 'console_hardware="al324"' in text


def test_describe_protect_copes_with_nothing_to_say(metrics: Metrics) -> None:
    metrics.describe_protect(host="protect.example")

    assert 'console_version=""' in metrics.render()


def test_default_metrics_is_one_instance() -> None:
    assert default_metrics() is default_metrics()


def _recording_config(config: Config, tmp_path: Path, **trigger: object) -> Config:
    return replace(
        config,
        reports=ReportConfig(output_dir=tmp_path, database=tmp_path / "h.sqlite3"),
        triggers=(TriggerConfig(name="Fire or smoke", prompt="?", **trigger),),  # type: ignore[arg-type]
    )


def test_a_recorded_trigger_reads_as_zero_edges_before_its_first_one(
    config: Config, metrics: Metrics, tmp_path: Path
) -> None:
    metrics.describe_config(_recording_config(config, tmp_path))
    text = metrics.render()

    edges = samples(text, "unifi_occupancy_recorded_edges_total")
    assert edges['{kind="trigger:fire_or_smoke",edge="start"}'] == 0
    assert edges['{kind="trigger:fire_or_smoke",edge="end"}'] == 0
    assert samples(text, "unifi_occupancy_feature_enabled")['{feature="trigger_history"}'] == 1


def test_a_trigger_kept_out_of_the_history_primes_no_edges(
    config: Config, metrics: Metrics, tmp_path: Path
) -> None:
    metrics.describe_config(_recording_config(config, tmp_path, record=False))
    text = metrics.render()

    assert '{kind="trigger:fire_or_smoke",edge="start"}' not in samples(
        text, "unifi_occupancy_recorded_edges_total"
    )
    assert samples(text, "unifi_occupancy_feature_enabled")['{feature="trigger_history"}'] == 0


def test_the_history_flag_is_off_without_the_sheets(config: Config, metrics: Metrics) -> None:
    """Nowhere to write to, so nothing is being written, whatever the trigger says."""
    metrics.describe_config(replace(config, triggers=(TriggerConfig(name="Fire", prompt="?"),)))

    assert metrics.feature_enabled.value(feature="trigger_history") == 0
