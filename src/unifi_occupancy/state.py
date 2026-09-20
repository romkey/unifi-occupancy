"""Turns polled camera data plus a stream of events into per-camera detection state.

Protect tells us a detection *started*; it is far less reliable about telling us
one stopped. So a detection latches on and decays after ``hold_seconds`` unless
something refreshes it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .models import DETECTION_KEYS, MOTION, Camera, DetectionEvent


@dataclass(frozen=True)
class CameraState:
    """What we publish for one camera at one moment."""

    camera: Camera
    detections: Mapping[str, bool]
    enabled: Mapping[str, bool]

    def active(self, key: str) -> bool:
        return bool(self.detections.get(key, False))


@dataclass
class DetectionTracker:
    """Latching detection state with a time-based decay.

    ``now`` is passed in rather than read from the clock so tests -- and the
    publish loop -- see a single consistent instant.
    """

    hold_seconds: float = 30.0
    #: (camera_id, detection key) -> monotonic timestamp of the last "started".
    _last_seen: dict[tuple[str, str], float] = field(default_factory=dict)
    #: Pairs explicitly ended by Protect, which we clear immediately.
    _cleared: set[tuple[str, str]] = field(default_factory=set)

    def apply(self, event: DetectionEvent, now: float) -> None:
        for key in event.keys:
            pair = (event.camera_id, key)
            if event.start:
                self._last_seen[pair] = now
                self._cleared.discard(pair)
            else:
                self._last_seen.pop(pair, None)
                self._cleared.add(pair)

    def is_active(self, camera_id: str, key: str, now: float) -> bool:
        seen = self._last_seen.get((camera_id, key))
        if seen is None:
            return False
        if now - seen >= self.hold_seconds:
            del self._last_seen[(camera_id, key)]
            return False
        return True

    def forget(self, camera_ids: Iterable[str]) -> None:
        """Drop state for cameras that are no longer present."""
        stale = set(camera_ids)
        if not stale:
            return
        for pair in [pair for pair in self._last_seen if pair[0] in stale]:
            del self._last_seen[pair]
        self._cleared -= {pair for pair in self._cleared if pair[0] in stale}

    def snapshot(self, camera: Camera, now: float) -> CameraState:
        """Combine tracked events with the camera's own polled state."""
        detections: dict[str, bool] = {}
        for key in DETECTION_KEYS:
            active = self.is_active(camera.id, key, now)
            if key == MOTION and camera.motion_detected:
                active = True
            # A detection that is switched off cannot be firing.
            detections[key] = active and camera.is_enabled(key)
        return CameraState(
            camera=camera,
            detections=detections,
            enabled={key: camera.is_enabled(key) for key in DETECTION_KEYS},
        )
