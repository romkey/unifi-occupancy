"""Custom triggers: the configuration, the reading of an answer, the sweep."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from conftest import FakeTransport, camera_payload
from unifi_occupancy.config import (
    Config,
    ConfigError,
    TriggerConfig,
    VisionConfig,
    parse_file_config,
)
from unifi_occupancy.discovery import discovery_messages, removal_messages
from unifi_occupancy.info import service_info_payload
from unifi_occupancy.metrics import Metrics
from unifi_occupancy.models import Camera, parse_camera
from unifi_occupancy.publisher import StatePublisher
from unifi_occupancy.state import DetectionTracker
from unifi_occupancy.triggers import (
    CLEAR,
    MATCH,
    UNKNOWN,
    TriggerAnswer,
    TriggerError,
    TriggerResult,
    TriggerWatcher,
    VisionTriggerJudge,
    parse_answer,
)

FIRE = TriggerConfig(
    name="Fire",
    prompt="Is there a fire, flames or burning material in this frame?",
    base_url="https://vision.example/v1",
    api_key="sk-test",
    model="some-vision-model",
    stagger_seconds=0.0,
    interval_minutes=5.0,
    device_class="problem",
)

CAMERA = parse_camera(camera_payload())
OTHER = parse_camera(camera_payload(id="cam-2", name="Back Yard"))
IMAGE = b"\xff\xd8\xff\xe0 not really a jpeg"


def completion(text: str) -> dict[str, Any]:
    return {
        "model": "some-vision-model",
        "choices": [{"message": {"role": "assistant", "content": text}}],
    }


def judge_for(handler, config: TriggerConfig = FIRE) -> VisionTriggerJudge:
    return VisionTriggerJudge(
        config, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


class FakeSnapshots:
    def __init__(self, result: bytes | Exception | None = IMAGE) -> None:
        self.result = result
        self.calls: list[tuple[str, bool]] = []

    async def get_snapshot(self, camera_id: str, *, high_quality: bool = False) -> bytes | None:
        self.calls.append((camera_id, high_quality))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeJudge:
    def __init__(self, answer: TriggerAnswer | Exception) -> None:
        self.answer = answer
        self.images: list[bytes] = []

    async def evaluate(self, image: bytes, *, content_type: str = "image/jpeg") -> TriggerAnswer:
        self.images.append(image)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class FakeSink:
    def __init__(self) -> None:
        self.published: list[tuple[str, TriggerResult]] = []
        self.snapshots: list[tuple[str, str, bytes | None]] = []

    async def publish_trigger(
        self, camera: Camera, result: TriggerResult, snapshot: bytes | None = None
    ) -> None:
        self.published.append((camera.name, result))
        self.snapshots.append((camera.name, result.trigger, snapshot))


def watcher_for(
    snapshots: FakeSnapshots,
    judge: FakeJudge,
    sink: Any,
    config: TriggerConfig = FIRE,
    **kwargs: Any,
) -> TriggerWatcher:
    async def no_sleep(_seconds: float) -> None:
        return None

    return TriggerWatcher(config, snapshots, sink, judge, sleep=no_sleep, **kwargs)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

YAML = """
triggers:
  - name: Fire in the doorway
    prompt: Is anything burning?
    cameras:
      - Front Door
      - cam-2
    interval_minutes: 10
    model: qwen2.5vl
    api_base_url: http://ollama.local:11434/v1
    device_class: problem

  - name: Gate
    prompt: Is the gate open?
    cameras: ALL
    enabled: false
"""


def test_triggers_are_parsed_out_of_the_config_file() -> None:
    triggers = parse_file_config(YAML).triggers

    assert [trigger.key for trigger in triggers] == ["fire_in_the_doorway", "gate"]

    fire = triggers[0]
    assert fire.cameras == ("Front Door", "cam-2")
    assert fire.all_cameras is False
    assert fire.interval_minutes == 10.0
    assert fire.interval_seconds == 600.0
    assert fire.model == "qwen2.5vl"
    assert fire.completions_url == "http://ollama.local:11434/v1/chat/completions"
    assert fire.device_class == "problem"
    assert fire.enabled is True

    gate = triggers[1]
    assert gate.all_cameras is True
    assert gate.enabled is False


def test_a_file_with_no_triggers_has_none() -> None:
    assert parse_file_config("reports:\n  enabled: true\n").triggers == ()


def test_the_answer_format_is_appended_unless_it_is_not_wanted() -> None:
    fire = parse_file_config(YAML).triggers[0]

    assert fire.question.startswith("Is anything burning?")
    assert "match" in fire.question

    raw = parse_file_config(
        "triggers:\n  - name: Fire\n    prompt: answer ja or nein\n"
        "    append_answer_format: false\n"
    ).triggers[0]
    assert raw.question == "answer ja or nein"


def test_triggers_inherit_the_default_vision_endpoint() -> None:
    defaults = VisionConfig(
        base_url="http://localhost:11434/v1",
        api_key="sk-local",
        model="llava",
        request_timeout=30.0,
        max_explanation_chars=120,
    )

    trigger = parse_file_config(
        "triggers:\n  - name: Fire\n    prompt: burning?\n", vision=defaults
    ).triggers[0]

    assert trigger.base_url == "http://localhost:11434/v1"
    assert trigger.api_key == "sk-local"
    assert trigger.model == "llava"
    assert trigger.request_timeout == 30.0
    assert trigger.max_explanation_chars == 120


def test_a_camera_is_matched_by_name_slug_or_id() -> None:
    trigger = replace(FIRE, cameras=("front door",))

    assert trigger.selects(CAMERA.id, CAMERA.name) is True
    assert trigger.selects(OTHER.id, OTHER.name) is False
    assert replace(FIRE, cameras=("cam-2",)).selects(OTHER.id, OTHER.name) is True
    assert replace(FIRE, cameras=("Front_Door",)).selects(CAMERA.id, CAMERA.name) is True
    assert FIRE.selects(OTHER.id, OTHER.name) is True


@pytest.mark.parametrize(
    "body, message",
    [
        ("triggers: not-a-list\n", "triggers must be a list"),
        ("triggers:\n  - prompt: burning?\n", "name"),
        ("triggers:\n  - name: Fire\n", "prompt"),
        ("triggers:\n  - name: '!!'\n    prompt: burning?\n", "letter"),
        (
            "triggers:\n  - name: Fire\n    prompt: burning?\n    api_base_url: nowhere\n",
            "http(s) URL",
        ),
        (
            "triggers:\n  - name: Fire\n    prompt: burning?\n  - name: fire\n    prompt: x\n",
            "same name",
        ),
        (
            "triggers:\n  - name: Fire\n    prompt: burning?\n    interval_minutes: 0\n",
            "at least",
        ),
        (
            "triggers:\n  - name: Fire\n    prompt: burning?\n    cameras: []\n",
            "cameras",
        ),
    ],
)
def test_a_broken_trigger_is_reported_rather_than_ignored(body: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message.replace("(", r"\(").replace(")", r"\)")):
        parse_file_config(body)


# --------------------------------------------------------------------------- #
# Reading the model's answer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        '{"match": true}',
        '```json\n{"match": true, "explanation": "flames on the step"}\n```',
        'Sure! {"detected": "yes"}',
        '{"present": 1}',
        "Yes, there are flames on the doormat.",
    ],
)
def test_a_hit_reads_as_a_match(text: str) -> None:
    assert parse_answer(text)[0] == MATCH


@pytest.mark.parametrize(
    "text",
    [
        '{"match": false}',
        '{"match": "no"}',
        '{"triggered": false, "explanation": "empty doorway"}',
        "No, the doorway is empty.",
        "There is nothing burning in this frame.",
    ],
)
def test_a_miss_reads_as_clear(text: str) -> None:
    assert parse_answer(text)[0] == CLEAR


def test_a_match_keeps_its_explanation() -> None:
    status, explanation = parse_answer('{"match": true, "explanation": "flames on the step"}')

    assert status == MATCH
    assert explanation == "flames on the step"


def test_a_match_without_an_explanation_still_says_something() -> None:
    status, explanation = parse_answer('{"match": true}')

    assert status == MATCH
    assert explanation


def test_an_unreadable_answer_is_unknown_rather_than_a_match() -> None:
    status, _ = parse_answer("I am a language model and cannot see images.")

    assert status in (CLEAR, UNKNOWN)
    assert status != MATCH

    assert parse_answer("purple monkey dishwasher")[0] == UNKNOWN
    assert parse_answer('{"match": "perhaps"}')[0] == UNKNOWN


@pytest.mark.parametrize(
    "text",
    [
        # A word that merely starts with "yes" or "true" is not a yes.
        "Yesterday's frame showed the same empty doorway.",
        "Truly ordinary: a doormat and a parcel.",
        # Scene language describes the frame without answering this trigger's
        # question. A person being visible must not fire a fire trigger.
        "A person is visible on the step, carrying a bag.",
        "Two parcels are present beside the door.",
        "A delivery van was detected at the kerb.",
        "The porch light is on.",
    ],
)
def test_a_description_that_answers_nothing_never_reads_as_a_match(text: str) -> None:
    assert parse_answer(text)[0] != MATCH


def test_a_leading_yes_still_answers_the_question() -> None:
    assert parse_answer("Yes.")[0] == MATCH
    assert parse_answer("Yes - the doormat is alight.")[0] == MATCH
    assert parse_answer("Affirmative, there are flames.")[0] == MATCH


def test_explanations_are_trimmed() -> None:
    long = "x" * 500
    _, explanation = parse_answer(json.dumps({"match": True, "explanation": long}), limit=50)

    assert explanation is not None
    assert len(explanation) == 50


# --------------------------------------------------------------------------- #
# Talking to an OpenAI-compatible endpoint
# --------------------------------------------------------------------------- #


async def test_the_request_carries_the_trigger_prompt_and_the_frame() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion('{"match": true}'))

    async with judge_for(handler) as judge:
        answer = await judge.evaluate(IMAGE)

    assert seen["url"] == "https://vision.example/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "some-vision-model"
    assert seen["body"]["temperature"] == 0

    content = seen["body"]["messages"][0]["content"]
    assert content[0]["text"].startswith("Is there a fire")
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    assert answer.status == MATCH
    assert answer.model == "some-vision-model"


async def test_an_error_response_is_reported_with_its_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid api key")

    async with judge_for(handler) as judge:
        with pytest.raises(TriggerError, match="invalid api key"):
            await judge.evaluate(IMAGE)


async def test_a_trigger_can_point_at_its_own_endpoint() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=completion('{"match": false}'))

    local = replace(FIRE, base_url="http://localhost:11434/v1", api_key=None, model="llava")
    async with judge_for(handler, local) as judge:
        assert (await judge.evaluate(IMAGE)).status == CLEAR

    assert seen["url"] == "http://localhost:11434/v1/chat/completions"
    assert seen["auth"] is None


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #


async def test_a_sweep_asks_about_every_camera_the_trigger_watches() -> None:
    snapshots = FakeSnapshots()
    sink = FakeSink()
    watcher = watcher_for(snapshots, FakeJudge(TriggerAnswer(MATCH, "flames")), sink)

    results = await watcher.sweep([CAMERA, OTHER])

    assert snapshots.calls == [("cam-1", False), ("cam-2", False)]
    assert [name for name, _ in sink.published] == ["Front Door", "Back Yard"]
    assert [result.status for result in results] == [MATCH, MATCH]
    assert results[0].trigger == "fire"
    assert results[0].name == "Fire"
    assert results[0].matched is True


async def test_cameras_the_trigger_does_not_name_are_left_alone() -> None:
    snapshots = FakeSnapshots()
    sink = FakeSink()
    watcher = watcher_for(
        snapshots,
        FakeJudge(TriggerAnswer(CLEAR)),
        sink,
        replace(FIRE, cameras=("Back Yard",)),
    )

    await watcher.sweep([CAMERA, OTHER])

    assert snapshots.calls == [("cam-2", False)]
    assert [name for name, _ in sink.published] == ["Back Yard"]


async def test_high_quality_snapshots_are_opt_in() -> None:
    snapshots = FakeSnapshots()
    watcher = watcher_for(
        snapshots,
        FakeJudge(TriggerAnswer(CLEAR)),
        FakeSink(),
        replace(FIRE, high_quality_snapshot=True, cameras=("Front Door",)),
    )

    await watcher.sweep([CAMERA])

    assert snapshots.calls == [("cam-1", True)]


async def test_a_disconnected_camera_is_not_photographed() -> None:
    snapshots = FakeSnapshots()
    offline = parse_camera(camera_payload(state="DISCONNECTED"))

    result = await watcher_for(snapshots, FakeJudge(TriggerAnswer(MATCH)), FakeSink()).check(
        offline
    )

    assert result.status == UNKNOWN
    assert snapshots.calls == []


@pytest.mark.parametrize(
    "snapshots",
    [FakeSnapshots(None), FakeSnapshots(RuntimeError("boom"))],
)
async def test_a_snapshot_that_does_not_arrive_is_unknown_not_clear(
    snapshots: FakeSnapshots,
) -> None:
    result = await watcher_for(snapshots, FakeJudge(TriggerAnswer(MATCH)), FakeSink()).check(CAMERA)

    assert result.status == UNKNOWN
    assert result.known is False
    assert result.explanation


async def test_a_model_failure_is_unknown_not_clear() -> None:
    result = await watcher_for(
        FakeSnapshots(), FakeJudge(TriggerError("endpoint down")), FakeSink()
    ).check(CAMERA)

    assert result.status == UNKNOWN
    assert result.explanation == "endpoint down"


async def test_a_camera_that_goes_away_mid_sweep_is_not_published() -> None:
    sink = FakeSink()
    watcher = watcher_for(
        FakeSnapshots(),
        FakeJudge(TriggerAnswer(MATCH)),
        sink,
        is_current=lambda camera_id: camera_id == "cam-2",
    )

    await watcher.sweep([CAMERA, OTHER])

    assert [name for name, _ in sink.published] == ["Back Yard"]


async def test_a_check_is_counted_and_timed() -> None:
    metrics = Metrics()
    watcher = watcher_for(
        FakeSnapshots(), FakeJudge(TriggerAnswer(MATCH, "flames")), FakeSink(), metrics=metrics
    )

    await watcher.check(CAMERA)
    rendered = metrics.registry.render()

    assert (
        'unifi_occupancy_trigger_checks_total{trigger="fire",camera="front_door",'
        'answer="match"} 1'
    ) in rendered
    assert (
        'unifi_occupancy_trigger_state{trigger="fire",camera="front_door",state="match"} 1'
    ) in rendered
    assert (
        'unifi_occupancy_trigger_state{trigger="fire",camera="front_door",state="clear"} 0'
    ) in rendered


async def test_a_vanished_camera_takes_its_trigger_series_with_it() -> None:
    metrics = Metrics()
    watcher = watcher_for(
        FakeSnapshots(), FakeJudge(TriggerAnswer(MATCH)), FakeSink(), metrics=metrics
    )

    await watcher.check(CAMERA)
    metrics.forget_cameras(["front_door"])

    assert "front_door" not in metrics.registry.render()


class StopSweeping(Exception):
    """Ends ``run_forever`` from inside its own sleep."""


async def test_run_forever_waits_for_the_first_poll_then_sweeps() -> None:
    sink = FakeSink()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > 2:
            raise StopSweeping

    watcher = TriggerWatcher(
        replace(FIRE, cameras=("Front Door",)),
        FakeSnapshots(),
        sink,
        FakeJudge(TriggerAnswer(CLEAR)),
        sleep=sleep,
        clock=lambda: 0.0,
    )

    batches: list[list[Camera]] = [[], [CAMERA], [CAMERA]]
    with pytest.raises(StopSweeping):
        await watcher.run_forever(lambda: batches.pop(0) if batches else [CAMERA])

    # The empty first batch is waited out briefly, not swept.
    assert sleeps[0] == 5.0
    assert [name for name, _ in sink.published] == ["Front Door", "Front Door"]
    assert sleeps[1] == FIRE.interval_seconds


# --------------------------------------------------------------------------- #
# What lands on the broker
# --------------------------------------------------------------------------- #


def triggered_config(config: Config, *triggers: TriggerConfig) -> Config:
    return replace(config, triggers=triggers)


async def test_a_match_becomes_a_boolean_and_its_explanation(
    config: Config, transport: FakeTransport
) -> None:
    config = triggered_config(config, FIRE)
    publisher = StatePublisher(config, transport)
    result = TriggerResult(trigger="fire", name="Fire", status=MATCH, explanation="flames")

    await publisher.publish_trigger(CAMERA, result)

    assert transport.payload_for("unifi-occupancy/front_door/trigger/fire") == "ON"
    attributes = json.loads(
        transport.payload_for("unifi-occupancy/front_door/trigger/fire/attributes") or "{}"
    )
    assert attributes["state"] == "match"
    assert attributes["match"] is True
    assert attributes["explanation"] == "flames"
    assert attributes["name"] == "Fire"


async def test_an_unknown_answer_leaves_the_boolean_alone(
    config: Config, transport: FakeTransport
) -> None:
    config = triggered_config(config, FIRE)
    publisher = StatePublisher(config, transport)

    await publisher.publish_trigger(
        CAMERA, TriggerResult(trigger="fire", name="Fire", status=CLEAR)
    )
    await publisher.publish_trigger(
        CAMERA,
        TriggerResult(trigger="fire", name="Fire", status=UNKNOWN, explanation="endpoint down"),
    )

    assert transport.payload_for("unifi-occupancy/front_door/trigger/fire") == "OFF"
    attributes = json.loads(
        transport.payload_for("unifi-occupancy/front_door/trigger/fire/attributes") or "{}"
    )
    assert attributes["state"] == "unknown"
    assert "match" not in attributes


def test_home_assistant_is_told_about_each_trigger(config: Config) -> None:
    config = triggered_config(config, FIRE)

    topics = {message.topic: message.payload for message in discovery_messages(config, CAMERA)}
    topic = "homeassistant/binary_sensor/unifi_occupancy_front_door/trigger_fire/config"

    assert topic in topics
    payload = topics[topic]
    assert payload is not None
    assert payload["name"] == "Fire"
    assert payload["device_class"] == "problem"
    assert payload["state_topic"] == "unifi-occupancy/front_door/trigger/fire"
    assert payload["json_attributes_topic"] == (
        "unifi-occupancy/front_door/trigger/fire/attributes"
    )
    assert payload["unique_id"] == "unifi_occupancy_cam-1_trigger_fire"


def test_a_trigger_that_does_not_watch_a_camera_gets_no_entity(config: Config) -> None:
    config = triggered_config(config, replace(FIRE, cameras=("Back Yard",)))

    topics = [message.topic for message in discovery_messages(config, CAMERA)]

    assert not any("trigger_fire" in topic for topic in topics)


def test_a_switched_off_trigger_is_retracted(config: Config) -> None:
    config = triggered_config(config, replace(FIRE, enabled=False))

    announced = [message.topic for message in discovery_messages(config, CAMERA)]
    retracted = [message.topic for message in removal_messages(config, CAMERA)]
    topic = "homeassistant/binary_sensor/unifi_occupancy_front_door/trigger_fire/config"

    assert topic not in announced
    assert topic in retracted


async def test_a_switched_off_trigger_has_its_topics_emptied(
    config: Config, transport: FakeTransport
) -> None:
    config = triggered_config(config, replace(FIRE, enabled=False))
    publisher = StatePublisher(config, transport)
    state = DetectionTracker().snapshot(CAMERA, 0.0)

    await publisher.publish_state(state)

    for topic in (
        "unifi-occupancy/front_door/trigger/fire",
        "unifi-occupancy/front_door/trigger/fire/attributes",
    ):
        assert transport.payload_for(topic) is None


async def test_a_retracted_camera_takes_its_trigger_topics_with_it(
    config: Config, transport: FakeTransport
) -> None:
    config = triggered_config(config, FIRE)
    publisher = StatePublisher(config, transport)

    await publisher.publish_trigger(
        CAMERA, TriggerResult(trigger="fire", name="Fire", status=MATCH)
    )
    await publisher.retract([CAMERA])

    for topic in (
        "unifi-occupancy/front_door/trigger/fire",
        "unifi-occupancy/front_door/trigger/fire/attributes",
    ):
        assert transport.payload_for(topic) is None


def test_the_service_info_lists_the_triggers(config: Config) -> None:
    config = triggered_config(config, FIRE, replace(FIRE, name="Gate", enabled=False))

    payload = service_info_payload(config)

    assert payload["features"]["custom_triggers"] is True
    assert payload["triggers"] == [
        {
            "name": "Fire",
            "key": "fire",
            "cameras": "all",
            "interval_minutes": 5.0,
            "model": "some-vision-model",
            "publish_snapshot": False,
        }
    ]


# --------------------------------------------------------------------------- #
# Publishing the frame that matched
# --------------------------------------------------------------------------- #

SNAPPY = replace(FIRE, publish_snapshot=True)


def test_publishing_a_snapshot_is_off_unless_asked_for() -> None:
    assert FIRE.publish_snapshot is False
    assert TriggerConfig(name="X", prompt="y").publish_snapshot is False

    parsed = parse_file_config("""
        triggers:
          - name: Fire
            prompt: Is there a fire?
          - name: Parcel
            prompt: Is there a parcel?
            publish_snapshot: true
        """).triggers

    assert parsed[0].publish_snapshot is False
    assert parsed[1].publish_snapshot is True


def test_there_is_no_way_to_switch_snapshots_on_for_every_trigger_at_once() -> None:
    """The asymmetry is deliberate -- see TriggerConfig.publish_snapshot."""
    assert not hasattr(VisionConfig(), "publish_snapshot")

    inherited = parse_file_config(
        """
        triggers:
          - name: Fire
            prompt: Is there a fire?
        """,
        vision=VisionConfig(model="inherited", high_quality_snapshot=True),
    ).triggers[0]

    # Everything else is inherited from the endpoint defaults; this is not.
    assert inherited.model == "inherited"
    assert inherited.high_quality_snapshot is True
    assert inherited.publish_snapshot is False


async def test_a_match_publishes_the_frame_when_the_trigger_asked_for_it() -> None:
    snapshots = FakeSnapshots(IMAGE)
    sink = FakeSink()
    watcher = watcher_for(snapshots, FakeJudge(TriggerAnswer(MATCH, "flames")), sink, SNAPPY)

    await watcher.sweep([CAMERA])

    assert sink.snapshots == [(CAMERA.name, "fire", IMAGE)]


@pytest.mark.parametrize("answer", [CLEAR, UNKNOWN])
async def test_only_a_match_publishes_a_frame(answer: str) -> None:
    snapshots = FakeSnapshots(IMAGE)
    sink = FakeSink()
    watcher = watcher_for(snapshots, FakeJudge(TriggerAnswer(answer)), sink, SNAPPY)

    await watcher.sweep([CAMERA])

    assert sink.snapshots == [(CAMERA.name, "fire", None)]


async def test_a_trigger_that_did_not_ask_never_hands_over_the_frame() -> None:
    snapshots = FakeSnapshots(IMAGE)
    sink = FakeSink()
    watcher = watcher_for(snapshots, FakeJudge(TriggerAnswer(MATCH, "flames")), sink, FIRE)

    await watcher.sweep([CAMERA])

    assert sink.snapshots == [(CAMERA.name, "fire", None)]
    assert sink.published[0][1].matched is True


async def test_the_frame_goes_out_unretained_and_before_the_boolean(
    config: Config, transport: FakeTransport
) -> None:
    config = triggered_config(config, SNAPPY)
    publisher = StatePublisher(config, transport)
    result = TriggerResult(trigger="fire", name="Fire", status=MATCH, explanation="flames")

    await publisher.publish_trigger(CAMERA, result, IMAGE)

    snapshot = "unifi-occupancy/front_door/trigger/fire/snapshot"
    assert transport.payload_for(snapshot) == IMAGE
    # Unretained, whatever the broker settings say: a kept frame would outlive
    # the match it belongs to.
    assert transport.retain_for(snapshot) is False
    assert config.mqtt.retain is True
    # An automation firing on the boolean should find the image already there.
    assert transport.topics.index(snapshot) < transport.topics.index(
        "unifi-occupancy/front_door/trigger/fire"
    )


async def test_the_frame_is_not_kept_in_the_publishers_memory(
    config: Config, transport: FakeTransport
) -> None:
    """A megabyte of doorway per camera per trigger, held for the process life."""
    config = triggered_config(config, SNAPPY)
    publisher = StatePublisher(config, transport)
    result = TriggerResult(trigger="fire", name="Fire", status=MATCH)

    await publisher.publish_trigger(CAMERA, result, IMAGE)

    cached = publisher._published
    assert "unifi-occupancy/front_door/trigger/fire/snapshot" not in cached
    assert not any(isinstance(value, bytes) for value in cached.values())


async def test_the_same_frame_is_published_again_rather_than_suppressed(
    config: Config, transport: FakeTransport
) -> None:
    config = triggered_config(config, SNAPPY)
    publisher = StatePublisher(config, transport)
    result = TriggerResult(trigger="fire", name="Fire", status=MATCH)

    await publisher.publish_trigger(CAMERA, result, IMAGE)
    await publisher.publish_trigger(CAMERA, result, IMAGE)

    snapshot = "unifi-occupancy/front_door/trigger/fire/snapshot"
    assert transport.topics.count(snapshot) == 2


async def test_a_snapshot_counts_towards_the_bytes_published(
    config: Config, transport: FakeTransport
) -> None:
    config = triggered_config(config, SNAPPY)
    metrics = Metrics()
    publisher = StatePublisher(config, transport, metrics=metrics)

    await publisher.publish_trigger(
        CAMERA, TriggerResult(trigger="fire", name="Fire", status=MATCH), IMAGE
    )

    rendered = metrics.render()
    assert 'unifi_occupancy_trigger_snapshots_total{trigger="fire",camera="front_door"} 1' in (
        rendered
    )
    # The frame's own bytes are counted, not just the attributes JSON beside it.
    published = next(
        float(line.rsplit(" ", 1)[1])
        for line in rendered.splitlines()
        if line.startswith("unifi_occupancy_mqtt_published_bytes_total ")
    )
    assert published > len(IMAGE)


# --------------------------------------------------------------------------- #
# What Home Assistant is told
# --------------------------------------------------------------------------- #


def test_every_trigger_gets_a_status_sensor_that_can_say_unknown(config: Config) -> None:
    config = triggered_config(config, FIRE)

    topics = {message.topic: message.payload for message in discovery_messages(config, CAMERA)}
    topic = "homeassistant/sensor/unifi_occupancy_front_door/trigger_fire_status/config"

    assert topic in topics
    payload = topics[topic]
    assert payload is not None
    assert payload["name"] == "Fire Status"
    # Reads the attributes topic, which carries `unknown` -- the one thing the
    # boolean structurally cannot report.
    assert payload["state_topic"] == "unifi-occupancy/front_door/trigger/fire/attributes"
    assert payload["value_template"] == "{{ value_json.state | default('unknown', true) }}"
    assert payload["entity_category"] == "diagnostic"
    assert payload["unique_id"] == "unifi_occupancy_cam-1_trigger_fire_status"


def test_a_snapshot_trigger_gets_a_camera_entity(config: Config) -> None:
    config = triggered_config(config, SNAPPY)

    topics = {message.topic: message.payload for message in discovery_messages(config, CAMERA)}
    topic = "homeassistant/camera/unifi_occupancy_front_door/trigger_fire_snapshot/config"

    assert topic in topics
    payload = topics[topic]
    assert payload is not None
    assert payload["name"] == "Fire Snapshot"
    assert payload["topic"] == "unifi-occupancy/front_door/trigger/fire/snapshot"
    assert payload["unique_id"] == "unifi_occupancy_cam-1_trigger_fire_snapshot"
    assert payload["entity_category"] == "diagnostic"


def test_no_camera_entity_without_the_setting(config: Config) -> None:
    config = triggered_config(config, FIRE)

    topics = [message.topic for message in discovery_messages(config, CAMERA)]

    assert not any("trigger_fire_snapshot" in topic for topic in topics)


def test_turning_snapshots_back_off_retracts_the_camera_entity(config: Config) -> None:
    config = triggered_config(config, FIRE)

    retracted = [message.topic for message in removal_messages(config, CAMERA)]
    topic = "homeassistant/camera/unifi_occupancy_front_door/trigger_fire_snapshot/config"

    assert topic in retracted


async def test_a_trigger_that_stopped_publishing_frames_loses_its_entity(
    config: Config, transport: FakeTransport
) -> None:
    config = triggered_config(config, FIRE)
    publisher = StatePublisher(config, transport)

    await publisher.publish_state(DetectionTracker().snapshot(CAMERA, 0.0))

    topic = "homeassistant/camera/unifi_occupancy_front_door/trigger_fire_snapshot/config"
    assert transport.payload_for(topic) is None
    # The trigger itself is still on, so its own entities stay put.
    binary = "homeassistant/binary_sensor/unifi_occupancy_front_door/trigger_fire/config"
    assert transport.payload_for(binary) is not None


def test_a_retracted_camera_takes_its_snapshot_entity_too(config: Config) -> None:
    config = triggered_config(config, SNAPPY)

    retracted = [message.topic for message in removal_messages(config, CAMERA)]

    assert "homeassistant/camera/unifi_occupancy_front_door/trigger_fire_snapshot/config" in (
        retracted
    )


def test_the_service_info_says_whether_anything_publishes_frames(config: Config) -> None:
    assert (
        service_info_payload(triggered_config(config, FIRE))["features"]["trigger_snapshots"]
        is False
    )
    payload = service_info_payload(triggered_config(config, SNAPPY))
    assert payload["features"]["trigger_snapshots"] is True
    assert payload["triggers"][0]["publish_snapshot"] is True


def test_a_switched_off_snapshot_trigger_does_not_count(config: Config) -> None:
    config = triggered_config(config, replace(SNAPPY, enabled=False))

    payload = service_info_payload(config)

    assert payload["features"]["trigger_snapshots"] is False
