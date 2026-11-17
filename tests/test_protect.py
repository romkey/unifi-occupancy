from __future__ import annotations

import json

import httpx
import pytest

from conftest import camera_payload
from unifi_occupancy.config import ProtectConfig
from unifi_occupancy.metrics import Metrics
from unifi_occupancy.models import PERSON, SMOKE
from unifi_occupancy.protect import (
    ProtectAuthError,
    ProtectClient,
    ProtectError,
    _decode,
    endpoint_label,
)

CONFIG = ProtectConfig(host="protect.example", api_key="secret")


def client_for(handler, metrics: Metrics | None = None) -> ProtectClient:
    http = httpx.AsyncClient(
        base_url=CONFIG.base_url,
        headers={"X-API-KEY": CONFIG.api_key},
        transport=httpx.MockTransport(handler),
    )
    return ProtectClient(CONFIG, client=http, metrics=metrics)


async def test_get_cameras_parses_the_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/cameras")
        return httpx.Response(200, json=[camera_payload()])

    async with client_for(handler) as client:
        cameras = await client.get_cameras()

    assert [camera.name for camera in cameras] == ["Front Door"]
    assert cameras[0].is_enabled(PERSON) is True


async def test_api_key_is_sent_as_a_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json=[])

    async with client_for(handler) as client:
        await client.get_cameras()

    assert seen["x-api-key"] == "secret"


async def test_get_meta() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"applicationVersion": "5.0.33"})

    async with client_for(handler) as client:
        assert (await client.get_meta())["applicationVersion"] == "5.0.33"


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_api_key_raises_auth_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "nope"})

    async with client_for(handler) as client:
        with pytest.raises(ProtectAuthError):
            await client.get_cameras()


async def test_server_error_raises_protect_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with client_for(handler) as client:
        with pytest.raises(ProtectError, match="500"):
            await client.get_cameras()


async def test_transport_failure_raises_protect_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    async with client_for(handler) as client:
        with pytest.raises(ProtectError, match="failed"):
            await client.get_cameras()


async def test_non_json_response_raises_protect_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login</html>")

    async with client_for(handler) as client:
        with pytest.raises(ProtectError, match="non-JSON"):
            await client.get_cameras()


def test_decode_handles_text_bytes_and_garbage() -> None:
    frame = json.dumps({"item": {"camera": "cam-1", "smartDetectTypes": ["alrmSmoke"]}})

    assert _decode(frame) is not None
    assert _decode(frame.encode()) is not None
    assert _decode(frame.encode()).keys == frozenset({SMOKE})  # type: ignore[union-attr]
    assert _decode("not json") is None
    assert _decode(b"\xff\xfe") is None


def test_websocket_url_is_derived_from_config() -> None:
    assert CONFIG.websocket_url == (
        "wss://protect.example:443/proxy/protect/integration/v1/subscribe/events"
    )


async def test_get_console_reads_the_nvr_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/nvrs")
        return httpx.Response(200, json=[{"name": "Dream Machine", "version": "6.3.52"}])

    async with client_for(handler) as client:
        console = await client.get_console()

    assert console is not None
    assert console["name"] == "Dream Machine"


async def test_get_console_accepts_a_bare_object() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "Cloud Key"})

    async with client_for(handler) as client:
        console = await client.get_console()

    assert console == {"name": "Cloud Key"}


async def test_get_console_is_none_when_unsupported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    async with client_for(handler) as client:
        assert await client.get_console() is None


async def test_an_unsupported_endpoint_still_raises_on_other_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with client_for(handler) as client:
        with pytest.raises(ProtectError):
            await client.get_console()


async def test_get_console_still_reports_a_rejected_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="nope")

    async with client_for(handler) as client:
        with pytest.raises(ProtectAuthError):
            await client.get_console()


# -- what the metrics make of all this ---------------------------------------


def test_endpoint_labels_keep_camera_ids_out_of_metric_names() -> None:
    assert endpoint_label("/cameras") == "/cameras"
    assert endpoint_label("/meta/info") == "/meta/info"
    assert endpoint_label("/cameras/abc123/snapshot?highQuality=false") == (
        "/cameras/{id}/snapshot"
    )
    assert endpoint_label("/cameras/abc123") == "/cameras/{id}"


async def test_a_successful_poll_is_counted_and_timed() -> None:
    metrics = Metrics()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[camera_payload()])

    async with client_for(handler, metrics) as client:
        await client.get_cameras()

    assert metrics.protect_requests.value(endpoint="/cameras", outcome="success") == 1
    assert metrics.protect_request_duration.count(endpoint="/cameras") == 1


async def test_a_rejected_key_and_a_server_error_are_told_apart() -> None:
    metrics = Metrics()

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    async with client_for(refuse, metrics) as client:
        with pytest.raises(ProtectAuthError):
            await client.get_cameras()
    async with client_for(fail, metrics) as client:
        with pytest.raises(ProtectError):
            await client.get_cameras()

    assert metrics.protect_requests.value(endpoint="/cameras", outcome="auth_error") == 1
    assert metrics.protect_requests.value(endpoint="/cameras", outcome="error") == 1


async def test_an_endpoint_this_console_lacks_is_not_a_failure() -> None:
    metrics = Metrics()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    async with client_for(handler, metrics) as client:
        assert await client.get_console() is None

    assert metrics.protect_requests.value(endpoint="/nvrs", outcome="success") == 1
    assert metrics.protect_requests.value(endpoint="/nvrs", outcome="error") == 0


async def test_a_transport_error_is_counted_too() -> None:
    metrics = Metrics()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with client_for(handler, metrics) as client:
        with pytest.raises(ProtectError):
            await client.get_cameras()

    assert metrics.protect_requests.value(endpoint="/cameras", outcome="error") == 1


async def test_snapshots_are_counted_under_one_endpoint() -> None:
    metrics = Metrics()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"jpeg")

    async with client_for(handler, metrics) as client:
        await client.get_snapshot("cam-1")
        await client.get_snapshot("cam-2")

    label = "/cameras/{id}/snapshot"
    assert metrics.protect_requests.value(endpoint=label, outcome="success") == 2


async def test_a_refused_snapshot_is_an_auth_error_and_a_missing_one_is_not() -> None:
    metrics = Metrics()

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={})

    def absent(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    label = "/cameras/{id}/snapshot"
    async with client_for(refuse, metrics) as client:
        with pytest.raises(ProtectAuthError):
            await client.get_snapshot("cam-1")
    async with client_for(absent, metrics) as client:
        assert await client.get_snapshot("cam-1") is None

    assert metrics.protect_requests.value(endpoint=label, outcome="auth_error") == 1
    assert metrics.protect_requests.value(endpoint=label, outcome="error") == 1


class FakeSocket:
    """The slice of a websocket connection :meth:`ProtectClient.events` uses."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages

    def __aiter__(self):
        async def frames():
            for message in self.messages:
                yield message

        return frames()


class FakeConnect:
    def __init__(self, messages: list[str], error: Exception | None = None) -> None:
        self.messages = messages
        self.error = error

    async def __aenter__(self) -> FakeSocket:
        if self.error is not None:
            raise self.error
        return FakeSocket(self.messages)

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_the_event_stream_counts_frames_and_connections(monkeypatch) -> None:
    metrics = Metrics()
    frames = [
        json.dumps({"item": {"camera": "cam-1", "smartDetectTypes": ["person"]}}),
        json.dumps({"item": {"camera": "cam-1", "type": "ring"}}),
        "not json at all",
    ]
    monkeypatch.setattr(
        "unifi_occupancy.protect.websockets.connect",
        lambda *args, **kwargs: FakeConnect(frames),
    )
    client = ProtectClient(CONFIG, client=httpx.AsyncClient(), metrics=metrics)

    received = [event async for event in client.events()]
    await client.aclose()

    assert [event.camera_id for event in received] == ["cam-1"]
    assert metrics.websocket_connects.value() == 1
    assert metrics.websocket_connected.value() == 0
    assert metrics.event_frames.value(outcome="detection") == 1
    assert metrics.event_frames.value(outcome="ignored") == 2


async def test_a_websocket_that_will_not_connect_is_counted(monkeypatch) -> None:
    metrics = Metrics()
    monkeypatch.setattr(
        "unifi_occupancy.protect.websockets.connect",
        lambda *args, **kwargs: FakeConnect([], error=OSError("no route to host")),
    )
    client = ProtectClient(CONFIG, client=httpx.AsyncClient(), metrics=metrics)

    with pytest.raises(OSError, match="no route"):
        [event async for event in client.events()]
    await client.aclose()

    assert metrics.websocket_failures.value(cause="OSError") == 1
    assert metrics.websocket_connected.value() == 0
