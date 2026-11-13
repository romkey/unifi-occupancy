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
        },
        "detections": list(DETECTION_KEYS),
    }
    if camera_count is not None:
        payload["cameras"] = camera_count
    return payload


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
