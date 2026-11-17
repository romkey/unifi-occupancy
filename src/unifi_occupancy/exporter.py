"""The ``/metrics`` endpoint.

A listening socket on a service that otherwise only dials out, so it is small
on purpose: one path, two methods, a token, and no routing table. There is no
web framework here and no new dependency -- ``asyncio.start_server`` and a
request line is the whole protocol a Prometheus scrape needs.

Three decisions worth knowing about:

* **Every request is authenticated before anything else is looked at.** A
  request without a good token gets 401 whatever it asked for, so an
  unauthenticated prober cannot even learn that ``METRICS_PATH`` was moved.
* **The token is compared with** :func:`hmac.compare_digest`, and it is read
  from a header rather than a query string, so it does not end up in a proxy
  log or a browser history.
* **The path label is normalised to the configured path or ``other``.** Letting
  the label be whatever was asked for would let any stranger with a socket
  grow this process's memory one URL at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

from .config import MetricsConfig
from .metrics import CONTENT_TYPE, Metrics, default_metrics

logger = logging.getLogger(__name__)

#: Longest request line or header line we will read, in bytes.
MAX_LINE = 8192
#: Most header lines we will read before giving up on a request.
MAX_HEADERS = 64
#: Seconds a client gets to send its request before we hang up.
READ_TIMEOUT = 10.0

SERVER = "unifi-occupancy"
#: Header a client may use instead of ``Authorization: Bearer``.
TOKEN_HEADER = "x-metrics-token"

_REASONS: Mapping[int, str] = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


@dataclass(frozen=True)
class Response:
    """One HTTP response, serialised by hand."""

    status: int
    body: bytes = b""
    content_type: str = "text/plain; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def reason(self) -> str:
        return _REASONS.get(self.status, "Error")

    def to_bytes(self, *, body: bool = True) -> bytes:
        lines = [
            f"HTTP/1.1 {self.status} {self.reason}",
            f"Content-Type: {self.content_type}",
            f"Content-Length: {len(self.body)}",
            f"Server: {SERVER}",
            "Cache-Control: no-store",
            "Connection: close",
        ]
        lines.extend(f"{name}: {value}" for name, value in self.headers)
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
        return head + self.body if body else head


def _text(status: int, message: str) -> Response:
    return Response(status=status, body=f"{message}\n".encode())


def presented_token(headers: Mapping[str, str]) -> str | None:
    """The token a request offered, from either header we accept."""
    authorization = headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    direct = headers.get(TOKEN_HEADER, "").strip()
    return direct or None


def authorized(config: MetricsConfig, headers: Mapping[str, str]) -> bool:
    """Whether this request carries the configured token.

    A configuration with no token never authorises anybody. ``load_config``
    will not build one while the endpoint is enabled, but a ``MetricsConfig``
    constructed by hand could, and the safe reading of "no token set" is "no
    one gets in" rather than "everyone does".
    """
    if not config.token:
        return False
    offered = presented_token(headers)
    if offered is None:
        return False
    return hmac.compare_digest(offered, config.token)


def handle(
    config: MetricsConfig,
    metrics: Metrics,
    method: str,
    target: str,
    headers: Mapping[str, str],
) -> Response:
    """Answer one request. Pure: no sockets, which is why it is easy to test."""
    started = time.perf_counter()
    path = target.split("?", 1)[0].split("#", 1)[0] or "/"
    # Never label a metric with a stranger's URL.
    label = config.path if path == config.path else "other"

    response = _answer(config, metrics, method.upper(), path, headers)
    metrics.record_http_request(label, response.status, time.perf_counter() - started)
    return response


def _answer(
    config: MetricsConfig,
    metrics: Metrics,
    method: str,
    path: str,
    headers: Mapping[str, str],
) -> Response:
    if not authorized(config, headers):
        return Response(
            status=401,
            body=b"unauthorized\n",
            headers=(("WWW-Authenticate", 'Bearer realm="metrics"'),),
        )
    if path != config.path:
        return _text(404, "not found")
    if method not in {"GET", "HEAD"}:
        return Response(
            status=405,
            body=b"method not allowed\n",
            headers=(("Allow", "GET, HEAD"),),
        )
    try:
        body = metrics.render().encode("utf-8")
    except Exception as exc:  # noqa: BLE001 - a broken scrape must not kill the service
        logger.warning("could not render metrics: %s", exc)
        return _text(500, "could not render metrics")
    return Response(status=200, body=body, content_type=CONTENT_TYPE)


class RequestError(Exception):
    """The client did not send something we can answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


async def read_request(
    reader: asyncio.StreamReader, *, timeout: float = READ_TIMEOUT
) -> tuple[str, str, dict[str, str]]:
    """Read a request line and its headers. Bodies are read and discarded."""

    async def line() -> bytes:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout)
        except TimeoutError as exc:
            raise RequestError(408, "request timed out") from exc
        except ValueError as exc:  # the stream reader's own line-length limit
            raise RequestError(431, "request line too long") from exc
        if len(raw) > MAX_LINE:
            raise RequestError(431, "request line too long")
        return raw

    request_line = (await line()).decode("latin-1").strip()
    if not request_line:
        raise RequestError(400, "empty request")
    parts = request_line.split()
    if len(parts) < 2:
        raise RequestError(400, "malformed request line")
    method, target = parts[0], parts[1]

    headers: dict[str, str] = {}
    for _ in range(MAX_HEADERS):
        raw = await line()
        if raw in (b"\r\n", b"\n", b""):
            return method, target, headers
        name, separator, value = raw.decode("latin-1").partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    raise RequestError(431, "too many headers")


class MetricsServer:
    """Serves the registry over HTTP for as long as it is running."""

    def __init__(
        self,
        config: MetricsConfig,
        metrics: Metrics | None = None,
        *,
        timeout: float = READ_TIMEOUT,
    ) -> None:
        self._config = config
        self._metrics = metrics or default_metrics()
        self._timeout = timeout
        self._server: asyncio.base_events.Server | None = None

    @property
    def ports(self) -> tuple[int, ...]:
        """The ports actually bound, so a test can ask for port 0 and find out."""
        if self._server is None:
            return ()
        bound: list[int] = []
        for socket in self._server.sockets:
            address = socket.getsockname()
            if isinstance(address, tuple) and len(address) >= 2:
                bound.append(int(address[1]))
        return tuple(bound)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._client,
            host=self._config.host,
            port=self._config.port,
            limit=MAX_LINE,
        )
        logger.info(
            "serving metrics on http://%s:%s%s",
            self._config.host,
            self.ports[0] if self.ports else self._config.port,
            self._config.path,
        )

    async def aclose(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def serve_forever(self) -> None:
        """Start if it is not started already, then serve until cancelled."""
        if self._server is None:
            await self.start()
        server = self._server
        if server is None:  # pragma: no cover - start() raises rather than returning None
            return
        try:
            await server.serve_forever()
        finally:
            await self.aclose()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                method, target, headers = await read_request(reader, timeout=self._timeout)
            except RequestError as exc:
                response = _text(exc.status, exc.message)
                self._metrics.record_http_request("other", exc.status, 0.0)
                send_body = True
            else:
                response = handle(self._config, self._metrics, method, target, headers)
                send_body = method.upper() != "HEAD"
            writer.write(response.to_bytes(body=send_body))
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):  # pragma: no cover - client left
            pass
        except Exception as exc:  # noqa: BLE001 - one bad client is not an outage
            logger.debug("metrics request failed: %s", exc)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
