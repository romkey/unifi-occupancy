"""The ``info`` payloads: what this service is, and what each camera is.

Two topics, both JSON, both retained and both static enough to be read once:

* ``<prefix>/info`` -- this build, the Protect version it is talking to and the
  console behind it, plus which optional features are switched on.
* ``<prefix>/<camera>/info`` -- one camera's identity and capabilities.

``attributes`` already carries the handful of fields Home Assistant shows on an
entity, and keeps changing as ``last_motion`` moves. ``info`` is the other half:
everything that describes the *thing* rather than its current state -- MAC,
firmware, hardware model, which detections the hardware has and which of them
are switched on -- so a non-Home-Assistant consumer can inventory a controller
from the broker alone.

Nothing here is video-adjacent: no stream URLs, no snapshot endpoints, no
credentials. The console's fields are an explicit allowlist rather than
whatever Protect happens to return.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import __version__
from .config import Config
from .models import DETECTION_KEYS, Camera

#: ``/nvrs`` carries far more than we want on a broker (storage layout, WAN
#: addresses, backup settings). These are the fields that describe the console
#: as a device, under our own names.
CONSOLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "name"),
    ("type", "type"),
    ("version", "version"),
    ("firmwareVersion", "firmware_version"),
    ("hardwarePlatform", "hardware_platform"),
    ("hardwareRevision", "hardware_revision"),
    ("timezone", "timezone"),
    ("uptime", "uptime"),
)


def console_info(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Pick the console fields worth publishing out of a ``/nvrs`` entry."""
    if not isinstance(payload, Mapping):
        return {}
    info: dict[str, Any] = {}
    for source, name in CONSOLE_FIELDS:
        value = payload.get(source)
        if isinstance(value, str | int | float | bool):
            info[name] = value
    return info


def service_info_payload(
    config: Config,
    *,
    meta: Mapping[str, Any] | None = None,
    console: Mapping[str, Any] | None = None,
    camera_count: int | None = None,
) -> dict[str, Any]:
    """Metadata about this service, Protect and the console it is reading."""
    meta = meta or {}
    application_version = meta.get("applicationVersion")

    protect: dict[str, Any] = {
        "host": config.protect.host,
        "port": config.protect.port,
        "api": "integration/v1",
        "application_version": (
            application_version if isinstance(application_version, str) else None
        ),
    }
    resolved_console = console_info(console)
    if resolved_console:
        protect["console"] = resolved_console

    payload: dict[str, Any] = {
        "service": "unifi-occupancy",
        "version": __version__,
        "protect": protect,
        "mqtt": {
            "topic_prefix": config.mqtt.topic_prefix,
            "retain": config.mqtt.retain,
            "qos": config.mqtt.qos,
        },
        "features": {
            "websocket": config.protect.use_websocket,
            "ha_discovery": config.discovery.enabled,
            "occupancy_sheets": config.reports is not None and config.reports.enabled,
            "camera_health": config.health.enabled,
            "custom_triggers": bool(config.active_triggers),
            # Its own flag because it is the one setting that makes this
            # service publish a picture. Anyone auditing what reaches the
            # broker should be able to read it off the info topic.
            "trigger_snapshots": any(
                trigger.publish_snapshot for trigger in config.active_triggers
            ),
            # Whether any trigger answer is being written to disk, which is
            # the other thing about a trigger that outlives the moment.
            "trigger_history": bool(_recorded(config)),
            # Whether anything is asking its question on detections rather
            # than on a clock, which is what makes the call count on the
            # metrics unpredictable from the intervals alone.
            "demand_triggers": any(trigger.on_demand for trigger in config.active_triggers),
            "metrics": config.metrics.enabled,
        },
        "detections": list(DETECTION_KEYS),
    }
    if config.health.enabled:
        payload["camera_health"] = {
            "model": config.health.model,
            "endpoint": config.health.base_url,
            "interval_minutes": config.health.interval_minutes,
        }
    if config.active_triggers:
        payload["triggers"] = [
            {
                "name": trigger.name,
                "key": trigger.key,
                "cameras": "all" if trigger.all_cameras else list(trigger.cameras),
                "interval_minutes": trigger.interval_minutes,
                # Empty means the interval above is the whole schedule. With
                # detections listed, the interval is only what the cooldown
                # falls back to and the trigger is asked on those instead.
                "on_events": list(trigger.on_events),
                "cooldown_minutes": trigger.cooldown_seconds / 60.0 if trigger.on_demand else None,
                "model": trigger.model,
                "publish_snapshot": trigger.publish_snapshot,
                "batch": trigger.batch,
                "record": trigger.record,
            }
            for trigger in config.active_triggers
        ]
    if config.active_triggers:
        # Which triggers share a call, so the request count is explicable from
        # the info topic rather than only from the metrics.
        payload["trigger_groups"] = [
            [trigger.key for trigger in group] for group in config.trigger_groups if len(group) > 1
        ]
    if camera_count is not None:
        payload["cameras"] = camera_count
    return payload


def _recorded(config: Config) -> tuple[str, ...]:
    """Triggers whose answers reach the history, if the history is on at all."""
    if config.reports is None or not config.reports.enabled:
        return ()
    return tuple(trigger.key for trigger in config.active_triggers if trigger.record)


def camera_info_payload(camera: Camera) -> dict[str, Any]:
    """One camera's identity and detection capabilities."""
    return {
        "camera_id": camera.id,
        "name": camera.name,
        "slug": camera.slug,
        "model": camera.model,
        "hardware": camera.hardware,
        "mac": camera.mac,
        "firmware": camera.firmware,
        "connected": camera.connected,
        "detections": {
            key: {
                "supported": camera.is_supported(key),
                "enabled": camera.is_enabled(key),
            }
            for key in DETECTION_KEYS
        },
    }
