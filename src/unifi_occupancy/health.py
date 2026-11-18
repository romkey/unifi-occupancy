"""Asking a vision model whether a camera still sees anything useful.

A camera that has been nudged, sprayed, covered in cobwebs or slowly taken over
by a shrub keeps reporting detections -- of the shrub -- and nothing in the
detection stream says the view has stopped being worth anything. So once an
hour, if and only if ``CAMERA_HEALTH_ENABLED`` is set, this takes one snapshot
per camera, asks an OpenAI-compatible vision model whether the picture looks
healthy, and publishes the verdict on ``<prefix>/<camera>/health``.

This is the only part of the service that touches an image, and it is off by
default. A snapshot exists in memory for the length of one request, goes to the
configured endpoint and nowhere else, and is never published, logged or written
to disk. What reaches MQTT is a word and, at worst, a short sentence.
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

from .config import HealthConfig
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

GOOD = "good"
BAD = "bad"
UNKNOWN = "unknown"


class HealthError(VisionError):
    """The vision endpoint could not be reached or did not make sense."""


@dataclass(frozen=True)
class HealthVerdict:
    """What the model made of one frame."""

    status: str
    explanation: str | None = None
    model: str | None = None
    checked_at: datetime | None = None

    @property
    def healthy(self) -> bool:
        return self.status == GOOD

    def payload(self) -> dict[str, Any]:
        """The MQTT payload: short enough to read at a glance."""
        payload: dict[str, Any] = {"health": self.status}
        if self.explanation:
            payload["explanation"] = self.explanation
        if self.model:
            payload["model"] = self.model
        if self.checked_at is not None:
            payload["checked_at"] = self.checked_at.isoformat()
        return payload


#: Words that, un-negated, mean the model is reporting a fault, and words that
#: mean it is not. Matched on whole words: "badly" is a problem, "badger" is not.
_PROBLEM_WORDS = re.compile(r"\b(?:bad|badly|obscur\w*|obstruct\w*|blocked|broken|unusable)\b")
_HEALTHY_WORDS = re.compile(r"\b(?:good|healthy|fine|usable|normal|ok|okay)\b")


def parse_verdict(text: str, *, limit: int = 200) -> tuple[str, str | None]:
    """Read a status and an explanation out of whatever the model said.

    The prompt asks for JSON, and models mostly oblige -- but not every
    OpenAI-compatible server supports a response format, and small local models
    like to wrap JSON in prose or skip it entirely. So: JSON if there is any,
    then the plainest possible reading of the words, then ``unknown``.
    """
    body = json_object(text)
    if body is not None:
        raw = body.get("health", body.get("status"))
        status = str(raw).strip().lower() if raw is not None else ""
        explanation = tidy(body.get("explanation") or body.get("reason") or body.get("why"), limit)
        if status in {GOOD, "ok", "healthy", "fine"}:
            return GOOD, explanation
        if status in {BAD, "problem", "unhealthy", "obscured", "blocked"}:
            return BAD, explanation or "the model reported a problem without saying what"
        return UNKNOWN, tidy(f"unrecognised verdict {status!r}", limit)

    lowered = text.strip().lower()
    if lowered.startswith(GOOD) or lowered in {"ok", "healthy"}:
        return GOOD, None
    if mentions(_PROBLEM_WORDS, lowered):
        return BAD, tidy(text, limit)
    if mentions(_HEALTHY_WORDS, lowered):
        return GOOD, None
    return UNKNOWN, tidy(f"could not read the model's reply: {text}", limit)


class Judge(Protocol):
    """Anything that can look at a frame and say whether it looks healthy."""

    async def judge(self, image: bytes, *, content_type: str = ...) -> HealthVerdict: ...


class VisionJudge:
    """A :class:`Judge` backed by an OpenAI-compatible chat completions API.

    Works against OpenAI itself, and equally against anything that speaks the
    same shape -- Ollama, llama.cpp, vLLM, LM Studio, OpenRouter -- which is why
    the API key is optional. The exchange itself lives in
    :class:`~unifi_occupancy.vision.VisionClient`; what is here is the health
    question and the reading of its answer.
    """

    def __init__(self, config: HealthConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = VisionClient(endpoint_for(config), client)

    async def __aenter__(self) -> VisionJudge:
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

    @property
    def headers(self) -> dict[str, str]:
        return self._client.headers

    def request_body(self, image: bytes, content_type: str) -> dict[str, Any]:
        return self._client.request_body(self._config.prompt, image, content_type)

    async def judge(self, image: bytes, *, content_type: str = "image/jpeg") -> HealthVerdict:
        try:
            reply = await self._client.ask(self._config.prompt, image, content_type=content_type)
        except VisionError as exc:
            raise HealthError(str(exc)) from exc

        status, explanation = parse_verdict(reply.text, limit=self._config.max_explanation_chars)
        return HealthVerdict(
            status=status,
            explanation=explanation,
            model=reply.model or self._config.model,
            checked_at=datetime.now(UTC),
        )


def endpoint_for(config: HealthConfig) -> VisionEndpoint:
    """The vision endpoint described by the health settings."""
    return VisionEndpoint(
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.model,
        request_timeout=config.request_timeout,
    )


class Snapshots(Protocol):
    """The slice of :class:`~unifi_occupancy.protect.ProtectClient` we need."""

    async def get_snapshot(self, camera_id: str, *, high_quality: bool = ...) -> bytes | None: ...


class HealthSink(Protocol):
    """Where verdicts go -- :class:`~unifi_occupancy.publisher.StatePublisher`."""

    async def publish_health(self, camera: Camera, verdict: HealthVerdict) -> None: ...


class CameraHealthMonitor:
    """Takes a snapshot of each camera on a schedule and publishes the verdict."""

    def __init__(
        self,
        config: HealthConfig,
        snapshots: Snapshots,
        sink: HealthSink,
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

    def _still_there(self, camera: Camera) -> bool:
        """Whether the camera is still in the inventory.

        A sweep takes minutes -- a snapshot, a model, a stagger gap per camera --
        and the poll loop runs the whole time. A camera removed from Protect
        halfway through has already had its retained topics cleared, so
        publishing a verdict for it afterwards would put an orphan back on the
        broker that nothing will ever clear again.
        """
        return self._is_current is None or self._is_current(camera.id)

    async def check(self, camera: Camera) -> HealthVerdict:
        """One camera: snapshot, ask, verdict, and how long all that took."""
        started = self._clock()
        verdict = await self._check(camera)
        self._metrics.record_health_check(camera, verdict.status, max(0.0, self._clock() - started))
        return verdict

    async def _check(self, camera: Camera) -> HealthVerdict:
        """One camera: snapshot, ask, verdict. Never raises."""
        if not camera.connected:
            return HealthVerdict(
                status=UNKNOWN,
                explanation="camera is not connected",
                checked_at=datetime.now(UTC),
            )
        try:
            image = await self._snapshots.get_snapshot(
                camera.id, high_quality=self._config.high_quality_snapshot
            )
        except Exception as exc:  # noqa: BLE001 - a bad snapshot is a verdict, not a crash
            logger.warning("no snapshot for %s: %s", camera.name, exc)
            return HealthVerdict(
                status=UNKNOWN,
                explanation=tidy(f"could not take a snapshot: {exc}", 200),
                checked_at=datetime.now(UTC),
            )
        if not image:
            return HealthVerdict(
                status=UNKNOWN,
                explanation="the controller returned no snapshot",
                checked_at=datetime.now(UTC),
            )

        try:
            verdict = await self._judge.judge(image)
        except Exception as exc:  # noqa: BLE001 - likewise for the model
            logger.warning("health check for %s failed: %s", camera.name, exc)
            return HealthVerdict(
                status=UNKNOWN,
                explanation=tidy(str(exc), self._config.max_explanation_chars),
                checked_at=datetime.now(UTC),
            )

        if verdict.status == BAD:
            logger.warning("%s looks unhealthy: %s", camera.name, verdict.explanation)
        else:
            logger.info("%s health: %s", camera.name, verdict.status)
        return verdict

    async def sweep(self, cameras: Sequence[Camera]) -> list[HealthVerdict]:
        """Check every camera once, trickled rather than all at once."""
        verdicts: list[HealthVerdict] = []
        for index, camera in enumerate(cameras):
            if index and self._config.stagger_seconds > 0:
                await self._sleep(self._config.stagger_seconds)
            if not self._still_there(camera):
                logger.debug("%s went away before its health check", camera.name)
                continue
            verdict = await self.check(camera)
            if not self._still_there(camera):
                logger.debug("%s went away during its health check", camera.name)
                continue
            verdicts.append(verdict)
            await self._sink.publish_health(camera, verdict)
        return verdicts

    async def run_forever(self, cameras: Callable[[], Sequence[Camera]]) -> None:
        """Sweep, wait out the interval, sweep again, until cancelled."""
        while True:
            batch = cameras()
            if not batch:
                # The first poll has not landed yet. Costs nothing to wait.
                await self._sleep(min(5.0, self._config.interval_seconds))
                continue
            started = self._clock()
            self._metrics.start_job("health_sweep")
            try:
                await self.sweep(batch)
            except asyncio.CancelledError:
                self._metrics.job_in_progress.set(0, job="health_sweep")
                raise
            except Exception:
                self._metrics.finish_job(
                    "health_sweep", OUTCOME_ERROR, max(0.0, self._clock() - started)
                )
                raise
            elapsed = self._clock() - started
            self._metrics.finish_job("health_sweep", OUTCOME_SUCCESS, max(0.0, elapsed))
            await self._sleep(max(1.0, self._config.interval_seconds - elapsed))
