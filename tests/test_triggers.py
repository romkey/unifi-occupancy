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

    async def publish_trigger(self, camera: Camera, result: TriggerResult) -> None:
        self.published.append((camera.name, result))


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
        }
    ]
