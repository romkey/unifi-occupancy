from __future__ import annotations

import base64
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from conftest import FakeTransport, camera_payload
from unifi_occupancy.config import Config, HealthConfig
from unifi_occupancy.discovery import discovery_messages
from unifi_occupancy.health import (
    BAD,
    GOOD,
    UNKNOWN,
    CameraHealthMonitor,
    HealthError,
    HealthVerdict,
    VisionJudge,
    parse_verdict,
)
from unifi_occupancy.models import parse_camera
from unifi_occupancy.publisher import StatePublisher

HEALTH = HealthConfig(
    enabled=True,
    base_url="https://vision.example/v1",
    api_key="sk-test",
    model="some-vision-model",
    stagger_seconds=0.0,
    interval_minutes=60.0,
)

CAMERA = parse_camera(camera_payload())
IMAGE = b"\xff\xd8\xff\xe0 not really a jpeg"


def completion(text: str) -> dict[str, Any]:
    return {
        "model": "some-vision-model",
        "choices": [{"message": {"role": "assistant", "content": text}}],
    }


def judge_for(handler, config: HealthConfig = HEALTH) -> VisionJudge:
    return VisionJudge(config, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


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
    def __init__(self, verdict: HealthVerdict | Exception) -> None:
        self.verdict = verdict
        self.images: list[bytes] = []

    async def judge(self, image: bytes, *, content_type: str = "image/jpeg") -> HealthVerdict:
        self.images.append(image)
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


# --------------------------------------------------------------------------- #
# Reading the model's answer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        '{"health": "good"}',
        '```json\n{"health": "good"}\n```',
        'Sure! Here is the result: {"health":"good"}',
        '{"status": "OK"}',
        "good",
    ],
)
def test_a_healthy_answer_reads_as_good(text: str) -> None:
    assert parse_verdict(text)[0] == GOOD


def test_a_bad_answer_keeps_its_explanation() -> None:
    status, explanation = parse_verdict('{"health": "bad", "explanation": "lens is covered"}')

    assert status == BAD
    assert explanation == "lens is covered"


def test_a_bad_answer_without_an_explanation_still_says_something() -> None:
    status, explanation = parse_verdict('{"health": "bad"}')

    assert status == BAD
    assert explanation


def test_prose_is_read_as_best_it_can_be() -> None:
    status, explanation = parse_verdict("The image looks bad -- the view is blocked by a van.")

    assert status == BAD
    assert explanation is not None and "van" in explanation


def test_nonsense_is_unknown_rather_than_a_guess() -> None:
    status, explanation = parse_verdict("I am a language model and cannot see images.")

    assert status == UNKNOWN
    assert explanation


def test_an_unrecognised_json_verdict_is_unknown() -> None:
    assert parse_verdict('{"health": "maybe"}')[0] == UNKNOWN


def test_explanations_are_trimmed() -> None:
    long = "x" * 500
    _, explanation = parse_verdict(json.dumps({"health": "bad", "explanation": long}), limit=50)

    assert explanation is not None
    assert len(explanation) == 50


# --------------------------------------------------------------------------- #
# Talking to an OpenAI-compatible endpoint
# --------------------------------------------------------------------------- #


async def test_the_request_is_an_openai_chat_completion() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion('{"health": "good"}'))

    async with judge_for(handler) as judge:
        verdict = await judge.judge(IMAGE)

    assert seen["url"] == "https://vision.example/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "some-vision-model"
    assert seen["body"]["temperature"] == 0

    content = seen["body"]["messages"][0]["content"]
    assert content[0]["text"].startswith("You are inspecting")
    expected = base64.b64encode(IMAGE).decode("ascii")
    assert content[1]["image_url"]["url"] == f"data:image/jpeg;base64,{expected}"

    assert verdict.status == GOOD
    assert verdict.healthy is True
    assert verdict.model == "some-vision-model"
    assert verdict.checked_at is not None


async def test_no_api_key_means_no_authorization_header() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=completion('{"health": "good"}'))

    async with judge_for(handler, replace(HEALTH, api_key=None)) as judge:
        await judge.judge(IMAGE)

    assert seen["auth"] is None


async def test_the_prompt_can_be_overridden() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion('{"health": "good"}'))

    async with judge_for(handler, replace(HEALTH, prompt="is it ok?")) as judge:
        await judge.judge(IMAGE)

    assert seen["body"]["messages"][0]["content"][0]["text"] == "is it ok?"


async def test_an_error_response_is_reported_with_its_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid api key")

    async with judge_for(handler) as judge:
        with pytest.raises(HealthError, match="invalid api key"):
            await judge.judge(IMAGE)


async def test_an_empty_completion_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    async with judge_for(handler) as judge:
        with pytest.raises(HealthError):
            await judge.judge(IMAGE)


async def test_content_returned_as_parts_is_still_read() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": [{"type": "text", "text": '{"health": "good"}'}]}}
                ]
            },
        )

    async with judge_for(handler) as judge:
        assert (await judge.judge(IMAGE)).status == GOOD


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #


def monitor_for(
    snapshots: FakeSnapshots,
    judge: FakeJudge,
    sink: Any,
    config: HealthConfig = HEALTH,
) -> CameraHealthMonitor:
    async def no_sleep(_seconds: float) -> None:
        return None

    return CameraHealthMonitor(config, snapshots, sink, judge, sleep=no_sleep)


class FakeSink:
    def __init__(self) -> None:
        self.published: list[tuple[str, HealthVerdict]] = []

    async def publish_health(self, camera: Any, verdict: HealthVerdict) -> None:
        self.published.append((camera.name, verdict))


async def test_a_sweep_checks_and_publishes_every_camera() -> None:
    snapshots = FakeSnapshots()
    sink = FakeSink()
    monitor = monitor_for(snapshots, FakeJudge(HealthVerdict(GOOD)), sink)

    await monitor.sweep([CAMERA])

    assert snapshots.calls == [("cam-1", False)]
    assert [name for name, _ in sink.published] == ["Front Door"]
    assert sink.published[0][1].status == GOOD


async def test_high_quality_snapshots_are_opt_in() -> None:
    snapshots = FakeSnapshots()
    monitor = monitor_for(
        snapshots,
        FakeJudge(HealthVerdict(GOOD)),
        FakeSink(),
        replace(HEALTH, high_quality_snapshot=True),
    )

    await monitor.sweep([CAMERA])

    assert snapshots.calls == [("cam-1", True)]


async def test_a_disconnected_camera_is_not_photographed() -> None:
    snapshots = FakeSnapshots()
    offline = parse_camera(camera_payload(state="DISCONNECTED"))

    verdict = await monitor_for(snapshots, FakeJudge(HealthVerdict(GOOD)), FakeSink()).check(
        offline
    )

    assert verdict.status == UNKNOWN
    assert snapshots.calls == []


async def test_a_missing_snapshot_is_unknown_not_bad() -> None:
    verdict = await monitor_for(
        FakeSnapshots(None), FakeJudge(HealthVerdict(GOOD)), FakeSink()
    ).check(CAMERA)

    assert verdict.status == UNKNOWN
    assert verdict.explanation


async def test_a_snapshot_failure_is_unknown_not_bad() -> None:
    verdict = await monitor_for(
        FakeSnapshots(RuntimeError("boom")), FakeJudge(HealthVerdict(GOOD)), FakeSink()
    ).check(CAMERA)

    assert verdict.status == UNKNOWN
    assert verdict.explanation is not None and "boom" in verdict.explanation


async def test_a_model_failure_is_unknown_not_bad() -> None:
    verdict = await monitor_for(
        FakeSnapshots(), FakeJudge(HealthError("endpoint down")), FakeSink()
    ).check(CAMERA)

    assert verdict.status == UNKNOWN
    assert verdict.explanation == "endpoint down"


class StopSweeping(Exception):
    """Ends ``run_forever`` from inside its own sleep."""


async def test_run_forever_waits_for_the_first_poll_then_sweeps() -> None:
    sink = FakeSink()
    batches: list[list[Any]] = [[], [CAMERA]]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) > 2:
            raise StopSweeping

    monitor = CameraHealthMonitor(
        HEALTH,
        FakeSnapshots(),
        sink,
        FakeJudge(HealthVerdict(GOOD)),
        sleep=sleep,
        clock=lambda: 0.0,
    )

    with pytest.raises(StopSweeping):
        await monitor.run_forever(lambda: batches.pop(0) if batches else [])

    # Nothing to check yet, so a short wait; then one sweep; then the interval.
    assert slept[0] == 5.0
    assert [name for name, _ in sink.published] == ["Front Door"]
    assert slept[1] == 3600.0


# --------------------------------------------------------------------------- #
# What lands on MQTT
# --------------------------------------------------------------------------- #


async def test_a_good_verdict_is_a_short_payload(config: Config, transport: FakeTransport) -> None:
    await StatePublisher(config, transport).publish_health(
        CAMERA, HealthVerdict(GOOD, model="some-vision-model")
    )

    payload = transport.payload_for("unifi-occupancy/front_door/health")
    assert payload is not None
    assert json.loads(payload) == {"health": "good", "model": "some-vision-model"}


async def test_a_bad_verdict_carries_its_explanation(
    config: Config, transport: FakeTransport
) -> None:
    await StatePublisher(config, transport).publish_health(
        CAMERA, HealthVerdict(BAD, explanation="the lens is covered")
    )

    payload = transport.payload_for("unifi-occupancy/front_door/health")
    assert payload is not None
    assert json.loads(payload) == {"health": "bad", "explanation": "the lens is covered"}


async def test_an_unchanged_verdict_is_still_republished(
    config: Config, transport: FakeTransport
) -> None:
    publisher = StatePublisher(config, transport)

    await publisher.publish_health(CAMERA, HealthVerdict(GOOD))
    before = len(transport.messages)
    await publisher.publish_health(CAMERA, HealthVerdict(GOOD))

    assert len(transport.messages) == before + 1


def test_health_entities_are_absent_until_the_check_is_on(config: Config) -> None:
    topics = [message.topic for message in discovery_messages(config, CAMERA)]

    assert not [topic for topic in topics if "health" in topic]


def test_health_entities_appear_when_the_check_is_on(config: Config) -> None:
    enabled = replace(config, health=HEALTH)

    messages = {message.topic: message.payload for message in discovery_messages(enabled, CAMERA)}
    verdict = messages["homeassistant/sensor/unifi_occupancy_front_door/health/config"]
    problem = messages[
        "homeassistant/binary_sensor/unifi_occupancy_front_door/health_problem/config"
    ]

    assert verdict is not None and problem is not None
    assert verdict["state_topic"] == "unifi-occupancy/front_door/health"
    assert verdict["value_template"] == "{{ value_json.health }}"
    assert problem["device_class"] == "problem"
    assert "bad" in problem["value_template"]
