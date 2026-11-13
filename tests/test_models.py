from __future__ import annotations

from datetime import UTC

import pytest

from conftest import camera_payload
from unifi_occupancy.models import (
    MOTION,
    PERSON,
    SIREN,
    SMOKE,
    classify,
    parse_camera,
    parse_cameras,
    parse_event,
    slugify,
)


def test_parse_camera_reads_identity() -> None:
    camera = parse_camera(camera_payload())

    assert camera.id == "cam-1"
    assert camera.name == "Front Door"
    assert camera.slug == "front_door"
    assert camera.model == "camera"
    assert camera.hardware == "UVC G4 Doorbell Pro"
    assert camera.mac == "AABBCCDDEEFF"
    assert camera.connected is True


def test_parse_camera_maps_enabled_detections() -> None:
    camera = parse_camera(camera_payload())

    assert camera.is_enabled(MOTION) is True
    assert camera.is_enabled(PERSON) is True
    assert camera.is_enabled(SMOKE) is True
    assert camera.is_enabled(SIREN) is True


def test_detection_disabled_when_not_configured() -> None:
    camera = parse_camera(
        camera_payload(smartDetectSettings={"objectTypes": ["vehicle"], "audioTypes": []})
    )

    assert camera.is_enabled(PERSON) is False
    assert camera.is_enabled(SMOKE) is False
    assert camera.is_enabled(SIREN) is False
    # Still supported by the hardware, just switched off.
    assert camera.is_supported(PERSON) is True
    assert camera.is_supported(SMOKE) is True


def test_unsupported_detection_is_reported_as_unsupported() -> None:
    camera = parse_camera(
        camera_payload(
            featureFlags={"smartDetectTypes": ["person"], "smartDetectAudioTypes": []},
            smartDetectSettings={"objectTypes": ["person"], "audioTypes": []},
        )
    )

    assert camera.is_supported(PERSON) is True
    assert camera.is_supported(SIREN) is False
    assert camera.is_supported(MOTION) is True


def test_motion_enabled_falls_back_to_zone_sensitivity() -> None:
    off = parse_camera(camera_payload(recordingSettings={}, motionZones=[{"sensitivity": 0}]))
    on = parse_camera(
        camera_payload(recordingSettings={}, motionZones=[{"sensitivity": 0}, {"sensitivity": 40}])
    )

    assert off.is_enabled(MOTION) is False
    assert on.is_enabled(MOTION) is True


def test_motion_defaults_to_enabled_without_any_signal() -> None:
    assert parse_camera({"id": "x", "name": "X"}).is_enabled(MOTION) is True


def test_disconnected_camera() -> None:
    assert parse_camera(camera_payload(state="DISCONNECTED")).connected is False


def test_last_motion_parses_epoch_milliseconds() -> None:
    camera = parse_camera(camera_payload())

    assert camera.last_motion is not None
    assert camera.last_motion.tzinfo == UTC
    assert camera.last_motion.year == 2023


def test_last_motion_parses_iso_strings_and_tolerates_junk() -> None:
    assert parse_camera(camera_payload(lastMotion="2024-01-02T03:04:05Z")).last_motion is not None
    assert parse_camera(camera_payload(lastMotion="not a date")).last_motion is None
    assert parse_camera(camera_payload(lastMotion=None)).last_motion is None


def test_parse_camera_survives_missing_sections() -> None:
    camera = parse_camera({"id": "bare", "name": "Bare"})

    assert camera.id == "bare"
    assert camera.is_enabled(PERSON) is False
    assert camera.is_supported(SIREN) is False


def test_parse_cameras_skips_junk_and_entries_without_id() -> None:
    cameras = parse_cameras([camera_payload(), {"name": "no id"}, "garbage", 42])

    assert [camera.id for camera in cameras] == ["cam-1"]


def test_parse_cameras_on_non_list() -> None:
    assert parse_cameras({"cameras": []}) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("person", PERSON),
        ("PERSON", PERSON),
        ("alrmSiren", SIREN),
        ("alrmSmoke", SMOKE),
        ("smoke_cmonx", SMOKE),
        ("motion", MOTION),
        ("vehicle", None),
        ("package", None),
    ],
)
def test_classify(value: str, expected: str | None) -> None:
    assert classify(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Front Door", "front_door"),
        ("  Back-Yard  ", "back_yard"),
        ("Garage #2", "garage_2"),
        ("!!!", "camera"),
    ],
)
def test_slugify(value: str, expected: str) -> None:
    assert slugify(value) == expected


def test_parse_event_extracts_smart_detections() -> None:
    event = parse_event(
        {"type": "add", "item": {"camera": "cam-1", "smartDetectTypes": ["person", "vehicle"]}}
    )

    assert event is not None
    assert event.camera_id == "cam-1"
    assert event.keys == frozenset({PERSON})
    assert event.start is True


def test_parse_event_uses_type_for_motion() -> None:
    event = parse_event({"item": {"camera": "cam-1", "type": "motion"}})

    assert event is not None
    assert event.keys == frozenset({MOTION})


def test_parse_event_marks_ended_events() -> None:
    event = parse_event(
        {
            "item": {
                "camera": "cam-1",
                "type": "smartAudioDetect",
                "smartDetectTypes": ["alrmSmoke"],
                "end": 1_700_000_100_000,
            }
        }
    )

    assert event is not None
    assert event.keys == frozenset({SMOKE})
    assert event.start is False


def test_parse_event_without_item_wrapper() -> None:
    event = parse_event({"camera": "cam-2", "smartDetectTypes": ["alrmSiren"]})

    assert event is not None
    assert event.camera_id == "cam-2"
    assert event.keys == frozenset({SIREN})


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "string",
        {"item": {"smartDetectTypes": ["person"]}},  # no camera
        {"item": {"camera": "cam-1", "smartDetectTypes": ["vehicle"]}},  # nothing we track
        {"item": {"camera": "cam-1"}},
    ],
)
def test_parse_event_ignores_irrelevant_frames(payload: object) -> None:
    assert parse_event(payload) is None
