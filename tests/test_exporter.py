from __future__ import annotations

import asyncio

import pytest

from unifi_occupancy.config import MetricsConfig
from unifi_occupancy.exporter import (
    MAX_HEADERS,
    MetricsServer,
    RequestError,
    authorized,
    handle,
    presented_token,
    read_request,
)
from unifi_occupancy.metrics import CONTENT_TYPE, Metrics

TOKEN = "a-token-that-is-long-enough"
CONFIG = MetricsConfig(enabled=True, host="127.0.0.1", port=0, token=TOKEN, path="/metrics")
AUTH = {"authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def metrics() -> Metrics:
    return Metrics()


# -- reading the token -------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"authorization": "Bearer abc"}, "abc"),
        ({"authorization": "bearer  abc "}, "abc"),
        ({"authorization": "Basic abc"}, None),
        ({"authorization": "Bearer"}, None),
        ({"x-metrics-token": "abc"}, "abc"),
        ({}, None),
    ],
)
def test_presented_token(headers: dict[str, str], expected: str | None) -> None:
    assert presented_token(headers) == expected


def test_a_config_without_a_token_authorises_nobody() -> None:
    config = MetricsConfig(enabled=True, token=None)

    assert authorized(config, {"authorization": "Bearer anything"}) is False
    assert authorized(config, {}) is False


def test_the_wrong_token_is_not_the_token() -> None:
    assert authorized(CONFIG, {"authorization": f"Bearer {TOKEN}x"}) is False
    assert authorized(CONFIG, {"x-metrics-token": TOKEN}) is True


# -- answering requests ------------------------------------------------------


def test_a_scrape_with_the_token_gets_the_metrics(metrics: Metrics) -> None:
    response = handle(CONFIG, metrics, "GET", "/metrics", AUTH)

    assert response.status == 200
    assert response.content_type == CONTENT_TYPE
    assert b"unifi_occupancy_build_info" in response.body
    assert metrics.http_requests.value(path="/metrics", status="200") == 1


def test_a_query_string_is_not_part_of_the_path(metrics: Metrics) -> None:
    assert handle(CONFIG, metrics, "GET", "/metrics?collect[]=x", AUTH).status == 200


def test_no_token_means_401_whatever_was_asked_for(metrics: Metrics) -> None:
    for target in ("/metrics", "/", "/secret"):
        response = handle(CONFIG, metrics, "GET", target, {})
        assert response.status == 401
        assert ("WWW-Authenticate", 'Bearer realm="metrics"') in response.headers

    # The 401s must not have grown a label per URL somebody tried.
    assert metrics.http_requests.value(path="other", status="401") == 2
    assert metrics.http_requests.value(path="/metrics", status="401") == 1


def test_an_authorised_request_for_another_path_is_a_404(metrics: Metrics) -> None:
    assert handle(CONFIG, metrics, "GET", "/", AUTH).status == 404


def test_a_moved_path_is_the_only_one_that_answers(metrics: Metrics) -> None:
    config = MetricsConfig(enabled=True, token=TOKEN, path="/internal/metrics")

    assert handle(config, metrics, "GET", "/metrics", AUTH).status == 404
    assert handle(config, metrics, "GET", "/internal/metrics", AUTH).status == 200


def test_head_is_allowed_and_post_is_not(metrics: Metrics) -> None:
    assert handle(CONFIG, metrics, "HEAD", "/metrics", AUTH).status == 200

    response = handle(CONFIG, metrics, "POST", "/metrics", AUTH)

    assert response.status == 405
    assert ("Allow", "GET, HEAD") in response.headers


def test_a_registry_that_cannot_render_is_a_500(metrics: Metrics) -> None:
    class Broken(Metrics):
        def render(self) -> str:
            raise RuntimeError("no")

    response = handle(CONFIG, Broken(), "GET", "/metrics", AUTH)

    assert response.status == 500


def test_the_response_is_serialised_as_http(metrics: Metrics) -> None:
    raw = handle(CONFIG, metrics, "GET", "/metrics", AUTH).to_bytes()
    head, _, body = raw.partition(b"\r\n\r\n")

    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Content-Type: text/plain; version=0.0.4" in head
    assert f"Content-Length: {len(body)}".encode() in head
    assert b"Connection: close" in head
    assert b"Cache-Control: no-store" in head


def test_a_head_response_carries_the_length_but_no_body(metrics: Metrics) -> None:
    response = handle(CONFIG, metrics, "HEAD", "/metrics", AUTH)
    raw = response.to_bytes(body=False)

    assert raw.endswith(b"\r\n\r\n")
    assert f"Content-Length: {len(response.body)}".encode() in raw


# -- reading the request off the socket --------------------------------------


async def reader_for(text: str) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(text.encode())
    reader.feed_eof()
    return reader


async def test_read_request_parses_a_request_line_and_headers() -> None:
    reader = await reader_for(
        "GET /metrics?x=1 HTTP/1.1\r\nHost: nas:9833\r\nAuthorization: Bearer t\r\n\r\n"
    )

    method, target, headers = await read_request(reader)

    assert (method, target) == ("GET", "/metrics?x=1")
    assert headers["host"] == "nas:9833"
    assert headers["authorization"] == "Bearer t"


async def test_read_request_rejects_an_empty_or_malformed_request() -> None:
    with pytest.raises(RequestError) as empty:
        await read_request(await reader_for("\r\n"))
    assert empty.value.status == 400

    with pytest.raises(RequestError) as malformed:
        await read_request(await reader_for("GARBAGE\r\n\r\n"))
    assert malformed.value.status == 400


async def test_read_request_gives_up_on_a_flood_of_headers() -> None:
    flood = "GET /metrics HTTP/1.1\r\n" + "".join(
        f"X-Filler-{index}: x\r\n" for index in range(MAX_HEADERS + 2)
    )

    with pytest.raises(RequestError) as exc:
        await read_request(await reader_for(flood))

    assert exc.value.status == 431


async def test_read_request_gives_up_on_an_endless_line() -> None:
    reader = asyncio.StreamReader(limit=64)
    reader.feed_data(b"GET /" + b"x" * 4096 + b" HTTP/1.1\r\n")

    with pytest.raises(RequestError) as exc:
        await read_request(reader)

    assert exc.value.status == 431


async def test_read_request_hangs_up_on_a_client_that_says_nothing() -> None:
    with pytest.raises(RequestError) as exc:
        await read_request(asyncio.StreamReader(), timeout=0.01)

    assert exc.value.status == 408


# -- over a real socket ------------------------------------------------------


async def request(port: int, raw: str) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw.encode())
    await writer.drain()
    body = await reader.read()
    writer.close()
    await writer.wait_closed()
    return body.decode()


async def test_the_server_serves_a_real_scrape(metrics: Metrics) -> None:
    server = MetricsServer(CONFIG, metrics)
    await server.start()
    port = server.ports[0]
    try:
        answered = await request(
            port, f"GET /metrics HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n\r\n"
        )
        refused = await request(port, "GET /metrics HTTP/1.1\r\nHost: x\r\n\r\n")
        headed = await request(
            port, f"HEAD /metrics HTTP/1.1\r\nHost: x\r\nX-Metrics-Token: {TOKEN}\r\n\r\n"
        )
        rubbish = await request(port, "nonsense\r\n\r\n")
    finally:
        await server.aclose()

    assert answered.startswith("HTTP/1.1 200 OK")
    assert "unifi_occupancy_uptime_seconds" in answered
    assert refused.startswith("HTTP/1.1 401 Unauthorized")
    assert headed.startswith("HTTP/1.1 200 OK")
    assert headed.endswith("\r\n\r\n")
    assert rubbish.startswith("HTTP/1.1 400 Bad Request")
    assert metrics.http_requests.value(path="/metrics", status="200") == 2


async def test_serve_forever_runs_until_cancelled(metrics: Metrics) -> None:
    server = MetricsServer(CONFIG, metrics)
    task = asyncio.create_task(server.serve_forever())
    while not server.ports:
        await asyncio.sleep(0.01)

    scrape = f"GET /metrics HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n\r\n"
    answered = await request(server.ports[0], scrape)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert answered.startswith("HTTP/1.1 200 OK")
    assert server.ports == ()


async def test_closing_a_server_that_never_started_is_harmless(metrics: Metrics) -> None:
    await MetricsServer(CONFIG, metrics).aclose()
