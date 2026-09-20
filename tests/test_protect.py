from __future__ import annotations

import json

import httpx
import pytest

from conftest import camera_payload
from unifi_occupancy.config import ProtectConfig
from unifi_occupancy.models import PERSON, SMOKE
from unifi_occupancy.protect import ProtectAuthError, ProtectClient, ProtectError, _decode

CONFIG = ProtectConfig(host="protect.example", api_key="secret")


def client_for(handler) -> ProtectClient:
    http = httpx.AsyncClient(
        base_url=CONFIG.base_url,
        headers={"X-API-KEY": CONFIG.api_key},
        transport=httpx.MockTransport(handler),
    )
    return ProtectClient(CONFIG, client=http)


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
