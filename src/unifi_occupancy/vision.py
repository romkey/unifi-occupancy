"""Asking an OpenAI-compatible vision model a question about one frame.

Two features need this: the camera health check (:mod:`unifi_occupancy.health`),
which asks whether the picture is still worth anything, and custom triggers
(:mod:`unifi_occupancy.triggers`), which ask whatever the operator wants to know
-- "is anything on fire in this doorway?". Both take one snapshot, send it to a
chat-completions endpoint with a prompt, and read a short verdict out of
whatever the model says back.

Everything in this module is about that single exchange: the request shape, the
reply shapes servers actually return, and the generous-but-careful reading of a
model that answered in prose rather than the JSON it was asked for. Nothing here
keeps an image: it exists for the length of one request and is never published,
logged or written to disk.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx

#: Bigger than any verdict needs, small enough that a chatty model is cut off
#: rather than paid for.
DEFAULT_MAX_TOKENS = 200


class VisionError(RuntimeError):
    """The vision endpoint could not be reached or did not make sense."""


@dataclass(frozen=True)
class VisionEndpoint:
    """Where to ask, as what, and with how long to wait.

    Works against OpenAI itself, and equally against anything that speaks the
    same shape -- Ollama, llama.cpp, vLLM, LM Studio, OpenRouter -- which is why
    the API key is optional.
    """

    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    model: str = "gpt-4o-mini"
    request_timeout: float = 60.0
    max_tokens: int = DEFAULT_MAX_TOKENS

    @property
    def completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


@dataclass(frozen=True)
class VisionReply:
    """What came back: the assistant's text, and which model produced it."""

    text: str
    model: str | None = None


def tidy(text: str | None, limit: int) -> str | None:
    """One line, whitespace collapsed, no longer than ``limit``."""
    if not text:
        return None
    collapsed = " ".join(str(text).split())
    if not collapsed:
        return None
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def json_object(text: str) -> dict[str, Any] | None:
    """The first JSON object in a reply, fenced or prefaced or neither."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


#: A negation anywhere in the clause before a word flips its sense: "not
#: blocked", "no fire" and "nothing burning" must never read as a hit.
NEGATION = re.compile(
    r"\b(?:not|no|never|nothing|none|isn't|aren't|wasn't|doesn't|don't|without|free|clear)\b"
)


def mentions(words: re.Pattern[str], text: str) -> bool:
    """True when ``text`` uses one of ``words`` and does not negate it.

    A model that answers in prose rather than JSON is being read generously, so
    the reading has to be careful: "the view is not blocked" contains the word
    "blocked" and means the opposite of it. Anything ambiguous is left for the
    caller to treat as unknown, which raises no alarm.
    """
    for match in words.finditer(text):
        clause = text[: match.start()].rsplit(".", 1)[-1]
        if NEGATION.search(clause[-60:]):
            continue
        return True
    return False


def reply_text(payload: Any) -> str | None:
    """The assistant's text, across the shapes servers actually return."""
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    content: Any = message.get("content") if isinstance(message, dict) else first.get("text")
    if isinstance(content, list):
        # Some servers return the content as a list of parts.
        parts = [
            part.get("text")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        content = " ".join(part for part in parts if part)
    if isinstance(content, str) and content.strip():
        return content
    return None


class VisionClient:
    """One image, one prompt, one short answer, over chat completions."""

    def __init__(self, endpoint: VisionEndpoint, client: httpx.AsyncClient | None = None) -> None:
        self._endpoint = endpoint
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=endpoint.request_timeout)

    async def __aenter__(self) -> VisionClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def endpoint(self) -> VisionEndpoint:
        return self._endpoint

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._endpoint.api_key:
            headers["Authorization"] = f"Bearer {self._endpoint.api_key}"
        return headers

    def request_body(self, prompt: str, image: bytes, content_type: str) -> dict[str, Any]:
        encoded = base64.b64encode(image).decode("ascii")
        return {
            "model": self._endpoint.model,
            # Deterministic: the same picture should not be a fire on one hour
            # and an empty doorway on the next.
            "temperature": 0,
            "max_tokens": self._endpoint.max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{content_type};base64,{encoded}"},
                        },
                    ],
                }
            ],
        }

    async def ask(
        self, prompt: str, image: bytes, *, content_type: str = "image/jpeg"
    ) -> VisionReply:
        """Put ``prompt`` and ``image`` to the model. Raises :class:`VisionError`."""
        try:
            response = await self._client.post(
                self._endpoint.completions_url,
                json=self.request_body(prompt, image, content_type),
                headers=self.headers,
            )
        except httpx.HTTPError as exc:
            raise VisionError(f"vision request failed: {exc}") from exc

        if response.status_code >= 400:
            # The body often says exactly what is wrong (bad key, unknown
            # model); it is worth having in the log, trimmed.
            raise VisionError(
                f"vision endpoint returned {response.status_code}: "
                f"{tidy(response.text, 200) or '(no body)'}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise VisionError("vision endpoint returned non-JSON") from exc

        text = reply_text(payload)
        if text is None:
            raise VisionError("vision endpoint returned no message content")
        model = payload.get("model") if isinstance(payload, dict) else None
        named = str(model) if isinstance(model, str) and model else self._endpoint.model
        return VisionReply(text=text, model=named)
