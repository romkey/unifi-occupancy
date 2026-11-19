"""Turning published state back into events worth keeping.

:class:`~unifi_occupancy.state.DetectionTracker` already decides when a
detection is on and when it has decayed; this watches the same snapshots the
publisher sends and writes the edges -- off-to-on and on-to-off -- into the
store. Deriving history from exactly what was published means a sheet can never
disagree with what Home Assistant was told at the time.

Custom triggers go in the same log, through :meth:`EventRecorder.record_trigger`
and under a ``trigger:`` kind. They are a detection in every way that matters
here: a boolean that goes on and later goes off, on a camera, at a time. Giving
them a table of their own would have bought nothing and cost the sheets their
one way of reading history.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .metrics import Metrics, default_metrics
from .models import DETECTION_KEYS, Camera, trigger_kind
from .state import CameraState
from .store import EventStore

if TYPE_CHECKING:  # pragma: no cover - a type, not a dependency
    from .triggers import TriggerResult

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(tz=UTC)


class EventRecorder:
    """Writes detection edges to an :class:`EventStore`.

    Failures are logged and swallowed. A full disk should cost the occupancy
    sheets their newest hour, not take the MQTT bridge down with it.
    """

    def __init__(
        self,
        store: EventStore,
        kinds: tuple[str, ...] = DETECTION_KEYS,
        clock: Callable[[], datetime] = _now,
        metrics: Metrics | None = None,
    ) -> None:
        self._store = store
        self._kinds = kinds
        self._clock = clock
        self._metrics = metrics or default_metrics()
        self._active: set[tuple[str, str]] = set()
        #: Last name written to the roster per camera, so a poll that changed
        #: nothing costs no write.
        self._roster: dict[str, str] = {}
        self._complained = False
        #: Separate from ``_complained``: a roster write that fails must not
        #: swallow the first warning about the event log, or the other way
        #: round. They fail for the same reasons but they are not the same loss.
        self._roster_complained = False

    def observe(self, state: CameraState) -> None:
        camera = state.camera
        now = self._clock()
        self._note(camera.id, camera.name, now)
        for kind in self._kinds:
            if not self._edge(camera, kind, active=state.active(kind), now=now):
                return

    def record_trigger(self, camera: Camera, result: TriggerResult) -> None:
        """Write a custom trigger's edge, the same way a detection's is written.

        Only an answer worth publishing as a boolean is worth writing down.
        ``unknown`` -- no snapshot, no endpoint, a reply nobody could read --
        leaves the boolean exactly as it was, so it has to leave the history
        exactly as it was too: a sheet showing a trigger going clear at the
        moment the vision server fell over would be reporting an answer nobody
        ever gave.

        Nothing the model *said* is kept. The row carries the trigger, the
        camera and the two times, like every other row in the log; the
        explanation stays where it already was, in the attributes topic and in
        the service log, and is not written to disk.
        """
        if not result.known:
            return
        now = self._clock()
        self._note(camera.id, camera.name, now)
        self._edge(camera, trigger_kind(result.trigger), active=result.matched, now=now)

    def _edge(self, camera: Camera, kind: str, *, active: bool, now: datetime) -> bool:
        """Write one edge if this is one. False when the store would not take it.

        A failure is reported once and then swallowed; the caller stops only so
        that a sweep over every kind does not log the same dead disk four times
        over.
        """
        pair = (camera.id, kind)
        if active == (pair in self._active):
            return True
        try:
            if active:
                self._store.start_event(camera.id, camera.name, kind, now)
            else:
                self._store.end_event(camera.id, kind, now)
        except Exception as exc:  # noqa: BLE001 - history is not worth a crash
            self._metrics.record_recorder_failure("event")
            if not self._complained:
                logger.warning("could not record detection history: %s", exc)
                self._complained = True
            return False
        self._complained = False
        self._metrics.record_edge(kind, start=active)
        if active:
            self._active.add(pair)
        else:
            self._active.discard(pair)
        return True

    def _note(self, camera_id: str, camera_name: str, now: datetime) -> None:
        """Keep the roster current so quiet cameras still reach the sheets."""
        if self._roster.get(camera_id) == camera_name:
            return
        try:
            self._store.record_camera(camera_id, camera_name, now)
        except Exception as exc:  # noqa: BLE001 - history is not worth a crash
            self._metrics.record_recorder_failure("roster")
            if not self._roster_complained:
                logger.warning("could not record the camera roster: %s", exc)
                self._roster_complained = True
            return
        self._roster_complained = False
        self._roster[camera_id] = camera_name

    def forget(self, camera_ids: frozenset[str]) -> None:
        """Close out cameras that have gone away so they do not stay latched."""
        now = self._clock()
        try:
            self._store.forget_cameras(camera_ids)
        except Exception as exc:  # noqa: BLE001 - as above
            self._metrics.record_recorder_failure("forget")
            logger.warning("could not drop %d camera(s) from the roster: %s", len(camera_ids), exc)
        for camera_id in camera_ids:
            self._roster.pop(camera_id, None)
        for pair in [pair for pair in self._active if pair[0] in camera_ids]:
            try:
                self._store.end_event(pair[0], pair[1], now)
            except Exception as exc:  # noqa: BLE001 - as above
                self._metrics.record_recorder_failure("close")
                logger.warning("could not close history for %s: %s", pair[0], exc)
            self._active.discard(pair)
