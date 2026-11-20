"""Custom triggers: asking the cameras a question of your own, on a schedule.

The detection stream reports what Protect knows how to report -- motion, a
person, a siren, a smoke alarm. It has nothing to say about the things that
actually happen in a doorway: a fire set on the step, a gate left open, a parcel
nobody collected, a van parked across the drive. Those are visible in a frame
and nowhere in the API.

So a trigger is a name and a question. Every ``interval_minutes`` -- or, with
``on_events``, whenever Protect reports one of those detections starting on
that camera -- this takes one snapshot of each camera the trigger names, puts
the question to a vision model, and publishes the answer as a boolean on
``<prefix>/<camera>/trigger/<name>`` -- ``ON`` while the model says it can see
what was described, ``OFF`` while it says it cannot. Home Assistant gets a
binary sensor per camera per trigger, and an automation can act on a fire at the
front door the same way it acts on motion.

Which of the two paces a trigger is the one thing it cannot do both of. A
question on a clock is asked whether or not there is anything to see, which on
eight cameras every five minutes is a couple of thousand pictures of an empty
driveway a day; a question on demand costs nothing until something moves, and
in exchange never looks at anything that does not. The cooldown is what keeps
the second one honest, since motion arrives in bursts and a vision call per
event would be worse than the polling it replaced.

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
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Protocol

import httpx

from .config import TriggerConfig
from .metrics import (
    DEMAND_ADMITTED,
    DEMAND_THROTTLED,
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    OUTCOME_UNREADABLE,
    Metrics,
    default_metrics,
)
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


# --------------------------------------------------------------------------- #
# Asking several triggers at once
# --------------------------------------------------------------------------- #

#: What goes in front of the operator's questions in a batched prompt.
#:
#: A vision request is mostly the image, so asking four triggers in one call
#: costs about what asking one costs. The saving is free; the accuracy is not.
#: Every line below is aimed at one specific way a combined prompt answers
#: worse than four separate ones:
#:
#: * **Bleed.** Each prompt was written and tuned on its own, exclusions and
#:   all -- "do not answer yes for sunsets, headlights, a lit cigarette". Those
#:   belong to the question that carries them. Said loose in one prompt they
#:   would apply to everything.
#: * **Priming.** A model asked about fire and about a parcel in one breath is
#:   readier to see both. Naming a thing is not evidence of it.
#: * **Narrative.** Asked several questions together, a model wants the answers
#:   to cohere -- to tell one story about the frame. These are unrelated
#:   questions and need not agree.
#: * **Contagious guessing.** One question it cannot answer should not drag the
#:   others down with it, and a guess is worse than an admission.
#:
#: None of this makes a batched answer as good as four separate ones. It is
#: meant to narrow the gap enough that the saving is worth having, which is why
#: a trigger can always opt out with ``batch: false``.
BATCH_PREAMBLE = """\
You are shown one still frame from a fixed security camera, and several
separate questions about it. The questions were written by different people,
for different reasons, and have nothing to do with one another.

Answer each question as though it were the only question you had been asked.

- Answer each question entirely on its own. What one question asks about is
  not a hint, a clue or evidence for any other question.
- Instructions, exclusions and definitions inside a question govern that
  question alone. They never carry over to another question.
- Do not reconcile your answers with each other. All of them can be true, all
  of them can be false, and they do not have to add up to one story about the
  frame.
- Answer only from what is visible in this frame. Do not infer what probably
  happened, what is out of shot, or what came before.
- If a question cannot be answered from this frame, say so for that question
  rather than guessing. An unanswered question is more use than a guessed one,
  and says nothing about the others."""

#: How a batched answer must come back: one entry per question id.
BATCH_ANSWER_FORMAT = """\
Reply with one JSON object and nothing else, with one entry per question id,
using the ids exactly as they appear above:

{"<id>": {"match": true, "explanation": "<at most 15 words>"}}

- "match": true if you can see what that question describes, false if you have
  looked and cannot see it, null if this frame cannot answer that question.
- "explanation": a few words saying what you saw, for a true answer. Leave it
  out otherwise.
- Include an entry for every id listed. Do not add ids that were not listed."""

#: Opening and closing tags of one question inside a batched prompt.
_OPEN = '<question id="{key}">'
_CLOSE = "</question>"


def silo(prompt: str) -> str:
    """One trigger's prompt, made safe to drop inside a tagged block.

    A prompt is operator-written text going into a structure the model is told
    to trust, so it must not be able to close its own block and write
    instructions at the level above -- whether on purpose or by writing about
    HTML. Cheap to prevent, so prevented.
    """
    cleaned = prompt.replace("<question", "(question").replace("</question>", "(/question)")
    return cleaned.strip()


def batch_question(triggers: Sequence[TriggerConfig]) -> str:
    """The one prompt that asks every trigger in a group at once."""
    parts = [BATCH_PREAMBLE, "", "Here are the questions.", ""]
    for trigger in triggers:
        parts += [_OPEN.format(key=trigger.key), silo(trigger.prompt), _CLOSE, ""]
    parts.append(BATCH_ANSWER_FORMAT)
    return "\n".join(parts)


def parse_batch_answer(
    text: str, keys: Sequence[str], *, limit: int = 200
) -> dict[str, TriggerAnswer] | None:
    """Read one batched reply into an answer per trigger.

    ``None`` is the caller's signal to fall back to asking each trigger on its
    own, and means the reply was not an answer *per trigger*: not JSON at all,
    or JSON carrying none of the ids that were asked about. Being valid JSON is
    not enough. A model that ignores the batched format and answers in the
    single-trigger shape -- ``{"match": true, "explanation": "..."}`` -- parses
    perfectly and says nothing about any of the questions, so treating it as an
    answer would mark every trigger on the camera ``unknown`` at the same
    moment. That correlated failure across an alarm and its neighbours is the
    exact thing the fallback exists to prevent, and it is worse than one extra
    round trip.

    A reply carrying *some* of the ids is read for what it has: a missing id,
    or one answered with something unreadable, marks only that trigger
    :data:`UNKNOWN` and the rest keep their answers. Partial credit is the
    whole point of asking by id, and once any id came back the model plainly
    understood the format.
    """
    body = json_object(text)
    if body is None:
        return None
    body = _unwrap(body, keys)
    if not any(key in body for key in keys):
        return None
    answers: dict[str, TriggerAnswer] = {}
    for key in keys:
        answers[key] = _one_of_batch(body, key, limit)
    return answers


def _unwrap(body: dict[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    """Descend one level into ``{"answers": {...}}`` and its cousins.

    Models like to wrap a result set in a key of their own choosing -- a
    habit worth tolerating, since the alternative is a fallback round trip for
    a reply that did answer every question. One level only, and only when the
    outer object holds none of the ids itself.
    """
    if any(key in body for key in keys):
        return body
    for value in body.values():
        if isinstance(value, dict) and any(key in value for key in keys):
            return value
    return body


def _one_of_batch(body: dict[str, Any], key: str, limit: int) -> TriggerAnswer:
    """One trigger's entry out of a batched reply."""
    if key not in body:
        return TriggerAnswer(UNKNOWN, f"the model left {key!r} out of its answer")
    value = body[key]
    # An explicit null is the model saying it could not tell, which is what the
    # prompt asked it to do rather than guess.
    if value is None:
        return TriggerAnswer(UNKNOWN, "the model could not tell from this frame")
    if isinstance(value, dict):
        said = next((value[k] for k in _EXPLANATION_KEYS if value.get(k)), None)
        explanation = tidy(said, limit)
        for answer_key in _ANSWER_KEYS:
            if answer_key in value:
                if value[answer_key] is None:
                    return TriggerAnswer(UNKNOWN, "the model could not tell from this frame")
                decided = _boolean(value[answer_key])
                if decided is None:
                    return TriggerAnswer(
                        UNKNOWN, tidy(f"unrecognised answer {value[answer_key]!r}", limit)
                    )
                if decided:
                    return TriggerAnswer(
                        MATCH, explanation or "the model reported a match without saying what"
                    )
                return TriggerAnswer(CLEAR, explanation)
        return TriggerAnswer(UNKNOWN, tidy(f"could not read the answer for {key!r}", limit))
    # A bare value: {"fire": true} or {"fire": "no"}, both of which happen.
    decided = _boolean(value)
    if decided is None:
        return TriggerAnswer(UNKNOWN, tidy(f"unrecognised answer {value!r}", limit))
    if decided:
        return TriggerAnswer(MATCH, "the model reported a match without saying what")
    return TriggerAnswer(CLEAR, None)


def endpoint_for(config: TriggerConfig) -> VisionEndpoint:
    """The vision endpoint this trigger asks its question of."""
    return VisionEndpoint(
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.model,
        request_timeout=config.request_timeout,
    )


class Judge(Protocol):
    """Anything that can look at a frame and answer triggers' questions."""

    async def evaluate(
        self,
        image: bytes,
        *,
        content_type: str = ...,
        trigger: TriggerConfig | None = ...,
    ) -> TriggerAnswer: ...

    async def evaluate_batch(
        self,
        image: bytes,
        triggers: Sequence[TriggerConfig],
        *,
        content_type: str = ...,
    ) -> dict[str, TriggerAnswer] | None: ...


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

    async def evaluate(
        self,
        image: bytes,
        *,
        content_type: str = "image/jpeg",
        trigger: TriggerConfig | None = None,
    ) -> TriggerAnswer:
        """Ask one trigger's question on its own.

        ``trigger`` names which one, for a judge serving a group; left out, it
        is the trigger the judge was built for. Every trigger in a group shares
        an endpoint, a key and a model by construction, so one client serves
        them all.
        """
        asked = trigger or self._config
        try:
            reply = await self._client.ask(asked.question, image, content_type=content_type)
        except VisionError as exc:
            raise TriggerError(str(exc)) from exc

        status, explanation = parse_answer(reply.text, limit=asked.max_explanation_chars)
        return TriggerAnswer(
            status=status,
            explanation=explanation,
            model=reply.model or asked.model,
        )

    async def evaluate_batch(
        self,
        image: bytes,
        triggers: Sequence[TriggerConfig],
        *,
        content_type: str = "image/jpeg",
    ) -> dict[str, TriggerAnswer] | None:
        """Ask every trigger in ``triggers`` about one frame, in one call.

        ``None`` when the reply could not be read as an answer per trigger, so
        the caller can ask them separately instead. A :class:`TriggerError`
        still means the endpoint itself failed, which separate calls would not
        fix.
        """
        question = batch_question(triggers)
        try:
            reply = await self._client.ask(question, image, content_type=content_type)
        except VisionError as exc:
            raise TriggerError(str(exc)) from exc

        keys = [trigger.key for trigger in triggers]
        limit = max(trigger.max_explanation_chars for trigger in triggers)
        answers = parse_batch_answer(reply.text, keys, limit=limit)
        if answers is None:
            return None
        model = reply.model or self._config.model
        # Each trigger's own explanation limit, now that the answers are apart.
        limits = {trigger.key: trigger.max_explanation_chars for trigger in triggers}
        return {
            key: TriggerAnswer(
                status=answer.status,
                explanation=tidy(answer.explanation, limits[key]),
                model=model,
            )
            for key, answer in answers.items()
        }


class Snapshots(Protocol):
    """The slice of :class:`~unifi_occupancy.protect.ProtectClient` we need."""

    async def get_snapshot(self, camera_id: str, *, high_quality: bool = ...) -> bytes | None: ...


class TriggerSink(Protocol):
    """Where answers go -- :class:`~unifi_occupancy.publisher.StatePublisher`."""

    async def publish_trigger(
        self, camera: Camera, result: TriggerResult, snapshot: bytes | None = ...
    ) -> None: ...


class TriggerLog(Protocol):
    """Where answers are kept -- :class:`~unifi_occupancy.recorder.EventRecorder`."""

    def record_trigger(self, camera: Camera, result: TriggerResult) -> None: ...


class TriggerWatcher:
    """Puts a group of triggers' questions to their cameras on a schedule.

    One watcher, one schedule, one snapshot per camera per cycle. A group is
    the triggers that agreed on :attr:`TriggerConfig.batch_key` -- so they want
    the same interval, endpoint, model and snapshot quality -- and a lone
    trigger is simply a group of one, run exactly as it was before groups
    existed.

    Running a group from one task is what makes a shared call possible at all.
    Before, each trigger had its own loop sleeping ``interval - elapsed``, so
    two triggers nominally on the same interval started a moment apart and
    drifted as the model's latency varied; there was no instant at which they
    could be said to want the same frame. Now they share a cycle by
    construction, and there is nothing to align.
    """

    def __init__(
        self,
        config: TriggerConfig | Sequence[TriggerConfig],
        snapshots: Snapshots,
        sink: TriggerSink,
        judge: Judge,
        *,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        is_current: Callable[[str], bool] | None = None,
        metrics: Metrics | None = None,
        recorder: TriggerLog | None = None,
    ) -> None:
        triggers = (config,) if isinstance(config, TriggerConfig) else tuple(config)
        if not triggers:
            raise ValueError("a TriggerWatcher needs at least one trigger")
        self._triggers = triggers
        # The group agrees on everything the schedule and the request need, so
        # the first member speaks for all of them on those.
        self._config = triggers[0]
        self._snapshots = snapshots
        self._sink = sink
        self._judge = judge
        self._recorder = recorder
        self._sleep = sleep
        self._clock = clock
        self._is_current = is_current
        self._metrics = metrics or default_metrics()
        #: Cameras waiting for a demand check, oldest first.
        #:
        #: Unbounded, and it does not need a bound: nothing reaches it that
        #: the cooldown did not admit, so the most it can ever hold is one
        #: entry per camera.
        self._demand: asyncio.Queue[str] = asyncio.Queue()
        #: camera id -> clock reading when its last demand check was admitted.
        self._admitted: dict[str, float] = {}

    @property
    def config(self) -> TriggerConfig:
        """The member that speaks for the group's schedule and endpoint."""
        return self._config

    @property
    def triggers(self) -> tuple[TriggerConfig, ...]:
        return self._triggers

    @property
    def batched(self) -> bool:
        return len(self._triggers) > 1

    @property
    def jobs(self) -> tuple[str, ...]:
        """Each member's name in the job metrics.

        Per trigger rather than per group: a group shares one call, but
        ``trigger:fire`` is what someone graphing a fire check looks for, and
        it should not vanish because a second trigger joined its interval. All
        members therefore report the same duration, which is the truth -- the
        call took that long for each of them.
        """
        return tuple(f"trigger:{trigger.key}" for trigger in self._triggers)

    @property
    def job(self) -> str:
        """A name for the asyncio task. Not a metric label."""
        return "trigger:" + "+".join(trigger.key for trigger in self._triggers)

    @property
    def on_demand(self) -> bool:
        """Whether this group is driven by detections rather than by a clock."""
        return self._config.on_demand

    def wants(self, camera: Camera, keys: Iterable[str]) -> tuple[TriggerConfig, ...]:
        """The members that would ask about ``camera`` given these detections.

        A member has to both watch the camera and listen for at least one of
        the detections. The group agrees on what it listens for -- that is
        part of :attr:`~unifi_occupancy.config.TriggerConfig.batch_key` -- but
        not on its camera list, so this can be empty for a camera the group as
        a whole does watch.
        """
        if not self.on_demand:
            return ()
        wanted = tuple(keys)
        return tuple(
            trigger
            for trigger in self.triggers_for(camera)
            if any(trigger.wants_event(key) for key in wanted)
        )

    def demand(self, camera: Camera, keys: Iterable[str]) -> bool:
        """Note a detection that may be worth a check. Never blocks, never raises.

        Called from the event loop that is reading Protect's websocket, so it
        does the cheap part only: decide whether this camera is due and put its
        id on the queue. The snapshot and the model happen in
        :meth:`run_on_demand`, where taking a minute does not hold up the next
        event.

        ``True`` when a check was admitted. The cooldown is stamped here
        rather than when the check finishes, so the limit is on how often a
        camera is *looked at* and does not quietly stretch with the model's
        latency.
        """
        asking = self.wants(camera, keys)
        if not asking:
            return False
        now = self._clock()
        last = self._admitted.get(camera.id)
        cooldown = self._config.cooldown_seconds
        if last is not None and now - last < cooldown:
            for trigger in asking:
                self._metrics.record_trigger_demand(trigger.key, DEMAND_THROTTLED)
            logger.debug(
                "%s on %s is inside its %gs cooldown; not checking",
                self.job,
                camera.name,
                cooldown,
            )
            return False
        self._admitted[camera.id] = now
        for trigger in asking:
            self._metrics.record_trigger_demand(trigger.key, DEMAND_ADMITTED)
        self._demand.put_nowait(camera.id)
        return True

    def forget(self, camera_ids: Iterable[str]) -> None:
        """Drop the cooldown state of cameras that have gone away.

        A camera id that comes back -- a camera renamed, or Protect restarted --
        should not inherit a cooldown from before it left.
        """
        for camera_id in camera_ids:
            self._admitted.pop(camera_id, None)

    def selects(self, camera: Camera) -> bool:
        return any(self.triggers_for(camera))

    def triggers_for(self, camera: Camera) -> tuple[TriggerConfig, ...]:
        """The members of this group that watch ``camera``.

        Members need not agree on their camera lists, so a group of three can
        be three questions at the front door and one at the gate. Whatever this
        returns for a camera is what that camera's call asks about.
        """
        return tuple(
            trigger for trigger in self._triggers if trigger.selects(camera.id, camera.name)
        )

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

    def _result(
        self,
        trigger: TriggerConfig,
        status: str,
        explanation: str | None,
        model: str | None,
    ) -> TriggerResult:
        return TriggerResult(
            trigger=trigger.key,
            name=trigger.name,
            status=status,
            explanation=explanation,
            model=model,
            checked_at=datetime.now(UTC),
        )

    def _all(
        self, triggers: Sequence[TriggerConfig], status: str, explanation: str | None
    ) -> list[tuple[TriggerResult, bytes | None]]:
        """The same answer for every trigger: no frame, so nothing to ask."""
        return [(self._result(trigger, status, explanation, None), None) for trigger in triggers]

    async def check(self, camera: Camera) -> TriggerResult:
        """One camera, for a watcher holding a single trigger."""
        checked = await self.check_camera(camera)
        return checked[0][0]

    async def check_camera(self, camera: Camera) -> list[tuple[TriggerResult, bytes | None]]:
        """One camera: snapshot, ask, answer, per trigger watching it.

        Each pair's frame is present only where that trigger has
        ``publish_snapshot`` on *and* its answer was a match, so a trigger
        never asked to publish an image cannot be handed one. The bytes go out
        of scope when the caller is done with them.
        """
        started = self._clock()
        checked = await self._check(camera)
        taken = max(0.0, self._clock() - started)
        out: list[tuple[TriggerResult, bytes | None]] = []
        for result, image in checked:
            trigger = self._by_key(result.trigger)
            self._metrics.record_trigger_check(result.trigger, camera, result.status, taken)
            keep = image if (image and trigger.publish_snapshot and result.matched) else None
            out.append((result, keep))
        return out

    def _by_key(self, key: str) -> TriggerConfig:
        return next(trigger for trigger in self._triggers if trigger.key == key)

    async def _check(self, camera: Camera) -> list[tuple[TriggerResult, bytes | None]]:
        """One camera: snapshot, ask, answer. Never raises."""
        asking = self.triggers_for(camera)
        if not camera.connected:
            return self._all(asking, UNKNOWN, "camera is not connected")
        try:
            image = await self._snapshots.get_snapshot(
                camera.id, high_quality=self._config.high_quality_snapshot
            )
        except Exception as exc:  # noqa: BLE001 - a bad snapshot is an answer, not a crash
            logger.warning("no snapshot for %s: %s", camera.name, exc)
            return self._all(asking, UNKNOWN, tidy(f"could not take a snapshot: {exc}", 200))
        if not image:
            return self._all(asking, UNKNOWN, "the controller returned no snapshot")

        answers = await self._ask(camera, asking, image)
        results: list[tuple[TriggerResult, bytes | None]] = []
        for trigger in asking:
            answer = answers[trigger.key]
            self._log(trigger, camera, answer)
            results.append(
                (self._result(trigger, answer.status, answer.explanation, answer.model), image)
            )
        return results

    async def _ask(
        self, camera: Camera, asking: Sequence[TriggerConfig], image: bytes
    ) -> dict[str, TriggerAnswer]:
        """Put the questions to the model, together where that is possible.

        One trigger is asked on its own, as it always was: a batched prompt
        would be pure overhead and a little worse at it. Several are asked in
        one call, and a reply that cannot be read per trigger falls back to
        asking each separately -- an unreadable batched answer would otherwise
        mark every trigger on this camera unknown at the same moment, which on
        an alarm is a worse failure than one more round trip.
        """
        if len(asking) == 1:
            return {asking[0].key: await self._ask_one(camera, asking[0], image)}
        try:
            answers = await self._judge.evaluate_batch(image, asking)
        except Exception as exc:  # noqa: BLE001 - the endpoint failed, not the reply
            logger.warning("batched check on %s failed: %s", camera.name, exc)
            self._metrics.record_trigger_batch(OUTCOME_ERROR)
            return {
                trigger.key: TriggerAnswer(UNKNOWN, tidy(str(exc), trigger.max_explanation_chars))
                for trigger in asking
            }
        if answers is not None:
            self._metrics.record_trigger_batch(OUTCOME_SUCCESS)
            return answers
        self._metrics.record_trigger_batch(OUTCOME_UNREADABLE)
        logger.warning(
            "could not read a batched answer for %s; asking %d trigger(s) separately",
            camera.name,
            len(asking),
        )
        return {trigger.key: await self._ask_one(camera, trigger, image) for trigger in asking}

    async def _ask_one(self, camera: Camera, trigger: TriggerConfig, image: bytes) -> TriggerAnswer:
        """One trigger, one call. Never raises."""
        try:
            return await self._judge.evaluate(image, trigger=trigger)
        except Exception as exc:  # noqa: BLE001 - a model failure is an answer
            logger.warning("trigger %s on %s failed: %s", trigger.name, camera.name, exc)
            return TriggerAnswer(UNKNOWN, tidy(str(exc), trigger.max_explanation_chars))

    def _log(self, trigger: TriggerConfig, camera: Camera, answer: TriggerAnswer) -> None:
        if answer.status == MATCH:
            logger.warning(
                "%s matched on %s: %s",
                trigger.name,
                camera.name,
                answer.explanation or "(no explanation)",
            )
        else:
            logger.info("%s on %s: %s", trigger.name, camera.name, answer.status)

    async def sweep(self, cameras: Sequence[Camera]) -> list[TriggerResult]:
        """Ask about every camera this group watches, trickled not burst."""
        results: list[TriggerResult] = []
        for index, camera in enumerate(self.cameras_for(cameras)):
            if index and self._config.stagger_seconds > 0:
                await self._sleep(self._config.stagger_seconds)
            results += await self.visit(camera)
        return results

    async def visit(self, camera: Camera) -> list[TriggerResult]:
        """One camera: ask, publish, record. The unit of work in both modes."""
        if not self._still_there(camera):
            logger.debug("%s went away before its %s check", camera.name, self.job)
            return []
        checked = await self.check_camera(camera)
        if not self._still_there(camera):
            logger.debug("%s went away during its %s check", camera.name, self.job)
            return []
        results: list[TriggerResult] = []
        for result, image in checked:
            results.append(result)
            await self._sink.publish_trigger(camera, result, image)
            self._record(camera, result)
        return results

    def _record(self, camera: Camera, result: TriggerResult) -> None:
        """Write the answer to the history, after it has been published.

        After, not before, for the reason the recorder exists at all: the log
        is meant to say what the broker was told, so an answer that never got
        there does not belong in it.
        """
        if self._recorder is None:
            return
        if not self._by_key(result.trigger).record:
            return
        self._recorder.record_trigger(camera, result)

    async def run_forever(self, cameras: Callable[[], Sequence[Camera]]) -> None:
        """Sweep, wait out the interval, sweep again, until cancelled."""
        if self.on_demand:  # pragma: no cover - a wiring mistake, not a path
            raise RuntimeError(f"{self.job} runs on demand; use run_on_demand")
        while True:
            batch = cameras()
            if not batch:
                # The first poll has not landed yet. Costs nothing to wait.
                await self._sleep(min(5.0, self._config.interval_seconds))
                continue
            started = self._clock()
            elapsed = await self._timed(self.sweep(batch), started)
            await self._sleep(max(1.0, self._config.interval_seconds - elapsed))

    async def run_on_demand(self, cameras: Callable[[], Sequence[Camera]]) -> None:
        """Check the cameras :meth:`demand` admits, until cancelled.

        One consumer, so two checks never overlap and the vision endpoint sees
        the same one-at-a-time load the interval sweep gave it. A camera is
        looked up again here rather than carried on the queue: a check admitted
        a minute ago should use the camera as it is now, and one whose camera
        has since gone is dropped.
        """
        while True:
            camera_id = await self._demand.get()
            camera = next((item for item in cameras() if item.id == camera_id), None)
            if camera is None:
                logger.debug("%s was asked about %s, which has gone", self.job, camera_id)
                continue
            await self._timed(self.visit(camera), self._clock())

    async def _timed(self, work: Awaitable[object], started: float) -> float:
        """Run one unit of work with the job gauges around it, and time it.

        Both loops report per member, not per group, for the reason
        :attr:`jobs` gives: ``trigger:fire`` is the series someone graphs, and
        it should not disappear because a second trigger joined the call.
        """
        for job in self.jobs:
            self._metrics.start_job(job)
        try:
            await work
        except asyncio.CancelledError:
            for job in self.jobs:
                self._metrics.job_in_progress.set(0, job=job)
            raise
        except Exception:
            taken = max(0.0, self._clock() - started)
            for job in self.jobs:
                self._metrics.finish_job(job, OUTCOME_ERROR, taken)
            raise
        elapsed = max(0.0, self._clock() - started)
        for job in self.jobs:
            self._metrics.finish_job(job, OUTCOME_SUCCESS, elapsed)
        return elapsed
