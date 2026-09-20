from __future__ import annotations

from conftest import camera_payload
from unifi_occupancy.models import MOTION, PERSON, SMOKE, DetectionEvent, parse_camera
from unifi_occupancy.state import DetectionTracker


def make_tracker(hold: float = 30.0) -> DetectionTracker:
    return DetectionTracker(hold_seconds=hold)


def test_event_turns_a_detection_on() -> None:
    tracker = make_tracker()
    tracker.apply(DetectionEvent("cam-1", frozenset({PERSON})), now=100.0)

    assert tracker.is_active("cam-1", PERSON, now=100.0) is True
    assert tracker.is_active("cam-1", SMOKE, now=100.0) is False
    assert tracker.is_active("cam-2", PERSON, now=100.0) is False


def test_detection_decays_after_the_hold_window() -> None:
    tracker = make_tracker(hold=30.0)
    tracker.apply(DetectionEvent("cam-1", frozenset({PERSON})), now=100.0)

    assert tracker.is_active("cam-1", PERSON, now=129.9) is True
    assert tracker.is_active("cam-1", PERSON, now=130.0) is False


def test_a_later_event_refreshes_the_hold() -> None:
    tracker = make_tracker(hold=30.0)
    tracker.apply(DetectionEvent("cam-1", frozenset({PERSON})), now=100.0)
    tracker.apply(DetectionEvent("cam-1", frozenset({PERSON})), now=125.0)

    assert tracker.is_active("cam-1", PERSON, now=150.0) is True


def test_end_event_clears_immediately() -> None:
    tracker = make_tracker()
    tracker.apply(DetectionEvent("cam-1", frozenset({SMOKE})), now=100.0)
    tracker.apply(DetectionEvent("cam-1", frozenset({SMOKE}), start=False), now=101.0)

    assert tracker.is_active("cam-1", SMOKE, now=101.0) is False


def test_forget_drops_state_for_removed_cameras() -> None:
    tracker = make_tracker()
    tracker.apply(DetectionEvent("cam-1", frozenset({PERSON})), now=100.0)
    tracker.apply(DetectionEvent("cam-2", frozenset({PERSON})), now=100.0)

    tracker.forget(["cam-1"])

    assert tracker.is_active("cam-1", PERSON, now=100.0) is False
    assert tracker.is_active("cam-2", PERSON, now=100.0) is True


def test_snapshot_reports_polled_motion_even_without_events() -> None:
    camera = parse_camera(camera_payload(isMotionDetected=True))
    state = make_tracker().snapshot(camera, now=1.0)

    assert state.active(MOTION) is True
    assert state.active(PERSON) is False


def test_snapshot_reports_enabled_flags() -> None:
    camera = parse_camera(
        camera_payload(smartDetectSettings={"objectTypes": [], "audioTypes": ["alrmSmoke"]})
    )
    state = make_tracker().snapshot(camera, now=1.0)

    assert state.enabled[SMOKE] is True
    assert state.enabled[PERSON] is False
    assert state.enabled[MOTION] is True


def test_disabled_detection_never_reports_active() -> None:
    camera = parse_camera(camera_payload(smartDetectSettings={"objectTypes": [], "audioTypes": []}))
    tracker = make_tracker()
    tracker.apply(DetectionEvent(camera.id, frozenset({PERSON})), now=1.0)

    state = tracker.snapshot(camera, now=1.0)

    assert state.active(PERSON) is False


def test_snapshot_combines_event_and_poll_state() -> None:
    camera = parse_camera(camera_payload(isMotionDetected=False))
    tracker = make_tracker()
    tracker.apply(DetectionEvent(camera.id, frozenset({MOTION, PERSON})), now=5.0)

    state = tracker.snapshot(camera, now=5.0)

    assert state.active(MOTION) is True
    assert state.active(PERSON) is True
