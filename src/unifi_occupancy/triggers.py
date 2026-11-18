"""Custom triggers: asking the cameras a question of your own, on a schedule.

The detection stream reports what Protect knows how to report -- motion, a
person, a siren, a smoke alarm. It has nothing to say about the things that
actually happen in a doorway: a fire set on the step, a gate left open, a parcel
nobody collected, a van parked across the drive. Those are visible in a frame
and nowhere in the API.

So a trigger is a name and a question. Every ``interval_minutes`` this takes one
snapshot of each camera the trigger names, puts the question to a vision model,
and publishes the answer as a boolean on
``<prefix>/<camera>/trigger/<name>`` -- ``ON`` while the model says it can see
what was described, ``OFF`` while it says it cannot. Home Assistant gets a
binary sensor per camera per trigger, and an automation can act on a fire at the
front door the same way it acts on motion.

The same cautions as the camera health check apply, and for the same reason:
this is one of the two features that touches an image. By default a snapshot
exists in memory for the length of one request, goes to the configured endpoint
and nowhere else, and is never published, logged or written to disk -- what
reaches MQTT is a boolean and, at worst, a short sentence.

One trigger at a time can opt out of that last part. ``publish_snapshot: true``
puts the frame that matched on ``<prefix>/<camera>/trigger/<name>/snapshot`` so
a notification can carry the picture of the fire, which is a great deal more
use than the word "ON". It is off by default, there is no way to switch it on
globally, and the frame is published unretained so it does not outlive the
match. Nothing is written to disk on any path.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Protocol

import httpx

from .config import TriggerConfig
from .metrics import OUTCOME_ERROR, OUTCOME_SUCCESS, Metrics, default_metrics
from .models import Camera
from .vision import (
    VisionClient,
    VisionEndpoint,
    VisionError,
    json_object,
    mentions,
    tidy,
)

logger = logging.getLogger(__name__)

#: The model can see what the trigger describes.
MATCH = "match"
#: The model looked and cannot see it.
CLEAR = "clear"
#: No answer worth having: no snapshot, no endpoint, or a reply nobody can read.
UNKNOWN = "unknown"


class TriggerError(VisionError):
    """The vision endpoint could not be reached or did not make sense."""


#: Keys a model might put its yes/no under when it answers in JSON.
_ANSWER_KEYS: tuple[str, ...] = (
    "match",
    "matched",
    "triggered",
    "trigger",
    "detected",
    "present",
    "found",
    "visible",
    "answer",
    "result",
)
_EXPLANATION_KEYS: tuple[str, ...] = ("explanation", "reason", "why", "description", "details")

_TRUE_WORDS = frozenset({"true", "yes", "y", "1", "match", "matched", "present", "detected"})
_FALSE_WORDS = frozenset({"false", "no", "n", "0", "clear", "absent", "none", "nothing"})

#: Prose that *answers the question*, matched on whole words and only when the
#: clause before it does not negate it.
#:
#: Only words that answer, never words that describe. A model saying something
#: is "visible", "present", "detected" or "burning" has described the frame
#: without saying which question it was answering, and the question is
#: different for every trigger -- "a person is visible on the step" must not
#: fire a trigger that asked about fire. Those readings fall through to
#: :data:`UNKNOWN`, which leaves the boolean where it was.
_MATCH_WORDS = re.compile(
    r"\b(?:yes|yep|yeah|affirmative|correct|confirmed|match(?:e[sd])?|triggered)\b"
)
#: Prose that means "I cannot". Checked first: a reading that is not clearly a
#: hit should not become one, because a hit is what rings an alarm.
_CLEAR_WORDS = re.compile(
    r"\b(?:no|not|none|nothing|never|absent|clear|empty|cannot|can't|unable|negative|false)\b"
)

#: A reply that opens with a bare yes or no is answering the question directly.
#: Anchored on a word boundary, so "yesterday the step was clear" and "truly
#: nothing is there" are not read as a yes.
_YES_PREFIX = re.compile(r"^(?:yes|true)\b")
_NO_PREFIX = re.compile(r"^(?:no|false)\b")


@dataclass(frozen=True)
class TriggerAnswer:
    """What the model made of one frame, before it is tied to a camera."""

    status: str
    explanation: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class TriggerResult:
    """One trigger's answer about one camera."""

    #: The trigger's slug -- its topic segment.
    trigger: str
    #: The trigger's name, as written in ``config.yaml``.
    name: str
    status: str
    explanation: str | None = None
    model: str | None = None
    checked_at: datetime | None = None

    @property
    def matched(self) -> bool:
        return self.status == MATCH

    @property
    def known(self) -> bool:
        """Whether this answer is worth publishing as a boolean at all."""
        return self.status in (MATCH, CLEAR)

    def payload(self) -> dict[str, Any]:
        """The JSON that rides alongside the boolean, as attributes."""
        payload: dict[str, Any] = {
            "trigger": self.trigger,
            "name": self.name,
            "state": self.status,
        }
        if self.known:
            payload["match"] = self.matched
        if self.explanation:
            payload["explanation"] = self.explanation
        if self.model:
            payload["model"] = self.model
        if self.checked_at is not None:
            payload["checked_at"] = self.checked_at.isoformat()
        return payload


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
    return None


def parse_answer(text: str, *, limit: int = 200) -> tuple[str, str | None]:
    """Read a yes, a no or a shrug out of whatever the model said.

    The prompt asks for JSON, and models mostly oblige -- but not every
    OpenAI-compatible server supports a response format, and small local models
    like to wrap JSON in prose or skip it entirely. So: JSON if there is any,
    then the plainest possible reading of the words, then :data:`UNKNOWN`.

    The reading is deliberately biased towards ``clear``: an unreadable answer
    must never ring an alarm, and a trigger that fires on ambiguity would be
    worse than no trigger at all.
    """
    body = json_object(text)
    if body is not None:
        said = next((body[key] for key in _EXPLANATION_KEYS if body.get(key)), None)
        explanation = tidy(said, limit)
        for key in _ANSWER_KEYS:
            if key in body:
                decided = _boolean(body[key])
                if decided is None:
                    return UNKNOWN, tidy(f"unrecognised answer {body[key]!r}", limit)
                if decided:
                    return MATCH, explanation or "the model reported a match without saying what"
                return CLEAR, explanation
        return UNKNOWN, tidy(f"could not read the model's reply: {text}", limit)

    lowered = text.strip().lower()
    if _YES_PREFIX.match(lowered):
        return MATCH, tidy(text, limit)
    if _NO_PREFIX.match(lowered):
        return CLEAR, None
    if _CLEAR_WORDS.search(lowered):
        return CLEAR, None
    if mentions(_MATCH_WORDS, lowered):
        return MATCH, tidy(text, limit)
    return UNKNOWN, tidy(f"could not read the model's reply: {text}", limit)


def endpoint_for(config: TriggerConfig) -> VisionEndpoint:
    """The vision endpoint this trigger asks its question of."""
    return VisionEndpoint(
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.model,
        request_timeout=config.request_timeout,
    )


class Judge(Protocol):
    """Anything that can look at a frame and answer one trigger's question."""

    async def evaluate(self, image: bytes, *, content_type: str = ...) -> TriggerAnswer: ...


class VisionTriggerJudge:
    """A :class:`Judge` backed by an OpenAI-compatible chat completions API."""

    def __init__(self, config: TriggerConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = VisionClient(endpoint_for(config), client)

    async def __aenter__(self) -> VisionTriggerJudge:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def evaluate(self, image: bytes, *, content_type: str = "image/jpeg") -> TriggerAnswer:
        try:
            reply = await self._client.ask(self._config.question, image, content_type=content_type)
        except VisionError as exc:
            raise TriggerError(str(exc)) from exc

        status, explanation = parse_answer(reply.text, limit=self._config.max_explanation_chars)
        return TriggerAnswer(
            status=status,
            explanation=explanation,
            model=reply.model or self._config.model,
        )


class Snapshots(Protocol):
    """The slice of :class:`~unifi_occupancy.protect.ProtectClient` we need."""

    async def get_snapshot(self, camera_id: str, *, high_quality: bool = ...) -> bytes | None: ...


class TriggerSink(Protocol):
    """Where answers go -- :class:`~unifi_occupancy.publisher.StatePublisher`."""

    async def publish_trigger(
        self, camera: Camera, result: TriggerResult, snapshot: bytes | None = ...
    ) -> None: ...


class TriggerWatcher:
    """Puts one trigger's question to its cameras on a schedule."""

    def __init__(
        self,
        config: TriggerConfig,
        snapshots: Snapshots,
        sink: TriggerSink,
        judge: Judge,
        *,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        is_current: Callable[[str], bool] | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._config = config
        self._snapshots = snapshots
        self._sink = sink
        self._judge = judge
        self._sleep = sleep
        self._clock = clock
        self._is_current = is_current
        self._metrics = metrics or default_metrics()

    @property
    def config(self) -> TriggerConfig:
        return self._config

    @property
    def job(self) -> str:
        """This trigger's name in the job metrics."""
        return f"trigger:{self._config.key}"

    def selects(self, camera: Camera) -> bool:
        return self._config.selects(camera.id, camera.name)

    def cameras_for(self, cameras: Sequence[Camera]) -> list[Camera]:
        return [camera for camera in cameras if self.selects(camera)]

    def _still_there(self, camera: Camera) -> bool:
        """Whether the camera is still in the inventory.

        A sweep takes minutes -- a snapshot, a model, a stagger gap per camera --
        and the poll loop runs the whole time. A camera removed from Protect
        halfway through has already had its retained topics cleared, so
        publishing an answer for it afterwards would put an orphan back on the
        broker that nothing will ever clear again.
        """
        return self._is_current is None or self._is_current(camera.id)

    def _result(self, status: str, explanation: str | None, model: str | None) -> TriggerResult:
        return TriggerResult(
            trigger=self._config.key,
            name=self._config.name,
            status=status,
            explanation=explanation,
            model=model,
            checked_at=datetime.now(UTC),
        )

    async def check(self, camera: Camera) -> TriggerResult:
        """One camera: snapshot, ask, answer, and how long all that took."""
        result, _ = await self.check_frame(camera)
        return result

    async def check_frame(self, camera: Camera) -> tuple[TriggerResult, bytes | None]:
        """:meth:`check`, plus the frame -- but only if it is to be published.

        The frame comes back only when this trigger has ``publish_snapshot``
        on *and* the answer was a match. Every other path returns ``None``, so
        a trigger that was never asked to publish an image cannot accidentally
        hand one to a caller: the bytes go out of scope when this returns.
        """
        started = self._clock()
        result, image = await self._check(camera)
        self._metrics.record_trigger_check(
            self._config.key, camera, result.status, max(0.0, self._clock() - started)
        )
        if image is not None and self._config.publish_snapshot and result.matched:
            return result, image
        return result, None

    async def _check(self, camera: Camera) -> tuple[TriggerResult, bytes | None]:
        """One camera: snapshot, ask, answer. Never raises."""
        if not camera.connected:
            return self._result(UNKNOWN, "camera is not connected", None), None
        try:
            image = await self._snapshots.get_snapshot(
                camera.id, high_quality=self._config.high_quality_snapshot
            )
        except Exception as exc:  # noqa: BLE001 - a bad snapshot is an answer, not a crash
            logger.warning("no snapshot for %s: %s", camera.name, exc)
            return self._result(UNKNOWN, tidy(f"could not take a snapshot: {exc}", 200), None), None
        if not image:
            return self._result(UNKNOWN, "the controller returned no snapshot", None), None

        try:
            answer = await self._judge.evaluate(image)
        except Exception as exc:  # noqa: BLE001 - likewise for the model
            logger.warning("trigger %s on %s failed: %s", self._config.name, camera.name, exc)
            return (
                self._result(UNKNOWN, tidy(str(exc), self._config.max_explanation_chars), None),
                None,
            )

        if answer.status == MATCH:
            logger.warning(
                "%s matched on %s: %s",
                self._config.name,
                camera.name,
                answer.explanation or "(no explanation)",
            )
        else:
            logger.info("%s on %s: %s", self._config.name, camera.name, answer.status)
        return self._result(answer.status, answer.explanation, answer.model), image

    async def sweep(self, cameras: Sequence[Camera]) -> list[TriggerResult]:
        """Ask about every camera this trigger watches, trickled not burst."""
        results: list[TriggerResult] = []
        for index, camera in enumerate(self.cameras_for(cameras)):
            if index and self._config.stagger_seconds > 0:
                await self._sleep(self._config.stagger_seconds)
            if not self._still_there(camera):
                logger.debug("%s went away before its %s check", camera.name, self._config.name)
                continue
            result, image = await self.check_frame(camera)
            if not self._still_there(camera):
                logger.debug("%s went away during its %s check", camera.name, self._config.name)
                continue
            results.append(result)
            await self._sink.publish_trigger(camera, result, image)
        return results

    async def run_forever(self, cameras: Callable[[], Sequence[Camera]]) -> None:
        """Sweep, wait out the interval, sweep again, until cancelled."""
        while True:
            batch = cameras()
            if not batch:
                # The first poll has not landed yet. Costs nothing to wait.
                await self._sleep(min(5.0, self._config.interval_seconds))
                continue
            started = self._clock()
            self._metrics.start_job(self.job)
            try:
                await self.sweep(batch)
            except asyncio.CancelledError:
                self._metrics.job_in_progress.set(0, job=self.job)
                raise
            except Exception:
                self._metrics.finish_job(self.job, OUTCOME_ERROR, max(0.0, self._clock() - started))
                raise
            elapsed = self._clock() - started
            self._metrics.finish_job(self.job, OUTCOME_SUCCESS, max(0.0, elapsed))
            await self._sleep(max(1.0, self._config.interval_seconds - elapsed))
