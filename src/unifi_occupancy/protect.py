"""Thin async client for the UniFi Protect integration API.

Uses the API-key authenticated ``/proxy/protect/integration/v1`` surface, which
is read-only and does not require a user login. We only ever call the metadata
and camera endpoints and subscribe to the event stream -- no video, no
snapshots, no RTSP.
"""

from __future__ import annotations

import json
import logging
import ssl
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any

import httpx
import websockets

from .config import ProtectConfig
from .models import Camera, DetectionEvent, parse_cameras, parse_event

logger = logging.getLogger(__name__)


class ProtectError(RuntimeError):
    """Any failure talking to Protect."""


class ProtectAuthError(ProtectError):
    """The API key was rejected."""


def _ssl_context(verify: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify:
        # UniFi controllers ship a self-signed certificate; verification is
        # opt-in via UNIFI_VERIFY_SSL for users who install a real one.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


class ProtectClient:
    """Read-only Protect client."""

    def __init__(self, config: ProtectConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url,
            headers=self.headers,
            verify=config.verify_ssl,
            timeout=config.request_timeout,
        )

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-KEY": self._config.api_key, "Accept": "application/json"}

    async def __aenter__(self) -> ProtectClient:
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

    async def _get(self, path: str, *, optional: bool = False) -> Any:
        try:
            response = await self._client.get(path)
        except httpx.HTTPError as exc:
            raise ProtectError(f"request to {path} failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise ProtectAuthError(
                f"Protect rejected the API key ({response.status_code}) for {path}"
            )
        if optional and response.status_code == 404:
            # Not every controller version exposes every endpoint; an absent
            # one is a fact about the console, not an error.
            logger.debug("%s is not available on this controller", path)
            return None
        if response.status_code >= 400:
            raise ProtectError(f"Protect returned {response.status_code} for {path}")
        try:
            return response.json()
        except ValueError as exc:
            raise ProtectError(f"Protect returned non-JSON for {path}") from exc

    async def get_meta(self) -> dict[str, Any]:
        """Controller metadata -- used as a cheap credential/connectivity check."""
        payload = await self._get("/meta/info")
        return payload if isinstance(payload, dict) else {}

    async def get_console(self) -> dict[str, Any] | None:
        """The console's own description, when this controller exposes ``/nvrs``.

        Older Protect versions do not, and the integration API may return a bare
        object rather than a list, so both shapes and the absence of the endpoint
        all resolve to "nothing to say" rather than to a failure.
        """
        payload = await self._get("/nvrs", optional=True)
        if isinstance(payload, list):
            payload = next((item for item in payload if isinstance(item, dict)), None)
        return payload if isinstance(payload, dict) else None

    async def get_cameras(self) -> list[Camera]:
        return parse_cameras(await self._get("/cameras"))

    async def events(self) -> AsyncIterator[DetectionEvent]:
        """Yield detection events from the Protect websocket until it closes.

        Callers are expected to reconnect; this deliberately does not retry so
        the supervising loop owns the backoff policy.
        """
        async with websockets.connect(
            self._config.websocket_url,
            additional_headers=self.headers,
            ssl=_ssl_context(self._config.verify_ssl),
            open_timeout=self._config.request_timeout,
            ping_interval=20,
        ) as socket:
            logger.info("subscribed to Protect event stream")
            async for message in socket:
                event = _decode(message)
                if event is not None:
                    yield event


def _decode(message: str | bytes) -> DetectionEvent | None:
    if isinstance(message, bytes | bytearray):
        try:
            message = message.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        payload = json.loads(message)
    except ValueError:
        logger.debug("ignoring non-JSON event frame")
        return None
    return parse_event(payload)
