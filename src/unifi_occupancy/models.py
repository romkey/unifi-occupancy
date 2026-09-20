"""Domain model: the handful of facts we are willing to expose about a camera.

Deliberately narrow. Nothing here carries a stream URL, a snapshot, or anything
else that would let a consumer look at what the camera sees -- only whether a
kind of detection is currently firing and whether it is switched on at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

MOTION = "motion"
PERSON = "person"
SIREN = "siren"
SMOKE = "smoke"


@dataclass(frozen=True)
class DetectionKind:
    """Static description of one thing we report per camera."""

    key: str
    name: str
    #: Home Assistant ``device_class`` for the "is it happening" sensor.
    device_class: str | None
    #: Values UniFi Protect may use for this detection in ``smartDetectTypes``
    #: and in event payloads. Matched case-insensitively.
    aliases: frozenset[str]
    #: True when Protect models this as an audio detection rather than motion
    #: or a visual object.
    audio: bool = False

    def matches(self, value: str) -> bool:
        return value.strip().lower() in self.aliases


DETECTION_KINDS: tuple[DetectionKind, ...] = (
    DetectionKind(
        key=MOTION,
        name="Motion",
        device_class="motion",
        aliases=frozenset({"motion"}),
    ),
    DetectionKind(
        key=PERSON,
        name="Person",
        device_class="occupancy",
        aliases=frozenset({"person", "people"}),
    ),
    DetectionKind(
        key=SIREN,
        name="Siren",
        device_class="sound",
        aliases=frozenset({"siren", "alrmsiren"}),
        audio=True,
    ),
    DetectionKind(
        key=SMOKE,
        name="Smoke Alarm",
        device_class="smoke",
        aliases=frozenset({"smoke", "alrmsmoke", "smoke_cmonx", "alrmcmonx", "smoke_alarm"}),
        audio=True,
    ),
)

DETECTION_KEYS: tuple[str, ...] = tuple(kind.key for kind in DETECTION_KINDS)
_KIND_BY_KEY: Mapping[str, DetectionKind] = {kind.key: kind for kind in DETECTION_KINDS}


def kind_for(key: str) -> DetectionKind:
    return _KIND_BY_KEY[key]


def classify(value: str) -> str | None:
    """Map a Protect detection type string onto one of our keys, if we track it."""
    for kind in DETECTION_KINDS:
        if kind.matches(value):
            return kind.key
    return None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Turn a camera name into something safe for a topic and an entity id."""
    slug = _SLUG_RE.sub("_", value.strip().lower()).strip("_")
    return slug or "camera"


@dataclass(frozen=True)
class Camera:
    """A camera as reported by the Protect integration API."""

    id: str
    name: str
    model: str | None = None
    mac: str | None = None
    firmware: str | None = None
    connected: bool = True
    #: Whether Protect currently reports motion, independent of our event stream.
    motion_detected: bool = False
    last_motion: datetime | None = None
    #: Detection key -> whether the detection is enabled on the camera.
    enabled: Mapping[str, bool] = field(default_factory=dict)
    #: Detection key -> whether the camera hardware supports it at all.
    supported: Mapping[str, bool] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return slugify(self.name)

    def is_enabled(self, key: str) -> bool:
        return bool(self.enabled.get(key, False))

    def is_supported(self, key: str) -> bool:
        return bool(self.supported.get(key, False))


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(item) for item in value if isinstance(item, str | int | float)]
    return []


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_timestamp(value: Any) -> datetime | None:
    """Protect hands out epoch milliseconds, sometimes ISO-8601 strings."""
    if value in (None, "", 0):
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000.0, tz=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _motion_enabled(payload: Mapping[str, Any]) -> bool:
    """Protect has expressed this three different ways across versions."""
    recording = _as_mapping(payload.get("recordingSettings"))
    for key in ("enableMotionDetection", "motionDetectionEnabled"):
        if isinstance(recording.get(key), bool):
            return bool(recording[key])
    if isinstance(payload.get("isMotionDetectionEnabled"), bool):
        return bool(payload["isMotionDetectionEnabled"])

    zones = payload.get("motionZones")
    if isinstance(zones, list) and zones:
        return any(
            float(_as_mapping(zone).get("sensitivity", 0) or 0) > 0
            for zone in zones
            if isinstance(zone, Mapping)
        )
    # Every Protect camera does motion; absent any signal, assume it is on.
    return True


def parse_camera(payload: Mapping[str, Any]) -> Camera:
    """Build a :class:`Camera` from one entry of ``GET /cameras``.

    Unknown or missing fields degrade to a safe default rather than raising:
    Protect's payload varies by controller version and camera model.
    """
    smart = _as_mapping(payload.get("smartDetectSettings"))
    features = _as_mapping(payload.get("featureFlags"))

    configured = {value.lower() for value in _as_list(smart.get("objectTypes"))}
    configured |= {value.lower() for value in _as_list(smart.get("audioTypes"))}

    available = {value.lower() for value in _as_list(features.get("smartDetectTypes"))}
    available |= {value.lower() for value in _as_list(features.get("smartDetectAudioTypes"))}

    enabled: dict[str, bool] = {}
    supported: dict[str, bool] = {}
    for kind in DETECTION_KINDS:
        if kind.key == MOTION:
            supported[kind.key] = True
            enabled[kind.key] = _motion_enabled(payload)
            continue
        supported[kind.key] = any(kind.matches(value) for value in available)
        enabled[kind.key] = any(kind.matches(value) for value in configured)

    state = str(payload.get("state") or "").upper()
    connected = state in {"", "CONNECTED"} and payload.get("isConnected", True) is not False

    return Camera(
        id=str(payload.get("id") or payload.get("mac") or ""),
        name=str(payload.get("name") or payload.get("id") or "camera"),
        model=payload.get("modelKey") if isinstance(payload.get("modelKey"), str) else None,
        mac=payload.get("mac") if isinstance(payload.get("mac"), str) else None,
        firmware=(
            payload.get("firmwareVersion")
            if isinstance(payload.get("firmwareVersion"), str)
            else None
        ),
        connected=connected,
        motion_detected=bool(payload.get("isMotionDetected", False)),
        last_motion=_parse_timestamp(payload.get("lastMotion")),
        enabled=enabled,
        supported=supported,
    )


def parse_cameras(payload: Any) -> list[Camera]:
    """Parse a ``GET /cameras`` response, skipping entries without an id."""
    if not isinstance(payload, list):
        return []
    cameras = [parse_camera(item) for item in payload if isinstance(item, Mapping)]
    return [camera for camera in cameras if camera.id]


@dataclass(frozen=True)
class DetectionEvent:
    """A detection reported over the Protect event websocket."""

    camera_id: str
    keys: frozenset[str]
    start: bool = True


def parse_event(payload: Any) -> DetectionEvent | None:
    """Extract the detections we care about from one websocket event frame.

    Returns ``None`` for the many frames that are about something else.
    """
    if not isinstance(payload, Mapping):
        return None

    item = _as_mapping(payload.get("item")) or payload
    camera_id = item.get("camera") or item.get("cameraId") or item.get("device")
    if not isinstance(camera_id, str) or not camera_id:
        return None

    raw_types = [
        *_as_list(item.get("smartDetectTypes")),
        *_as_list(item.get("smartDetectEvents")),
    ]
    event_type = item.get("type")
    if isinstance(event_type, str):
        raw_types.append(event_type)

    keys = {key for key in (classify(value) for value in raw_types) if key is not None}
    if not keys:
        return None

    # Protect marks a finished event by setting ``end``; until then it is live.
    start = item.get("end") in (None, 0, "")
    return DetectionEvent(camera_id=camera_id, keys=frozenset(keys), start=start)
