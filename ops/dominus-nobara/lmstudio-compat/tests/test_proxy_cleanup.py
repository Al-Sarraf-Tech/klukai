"""Upstream stream faults cannot retain the exclusive inference lease."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request

from gateway.catalog import Catalog
from gateway.gpu_lease import LeaseStore
from gateway.main import _limits
from gateway.proxy_routes import register_proxy_routes
from gateway.runtime import GatewayRuntime, GatewayServices
from gateway.settings import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["error_read", "cancel_read", "error_close", "relay_close"]
)
async def test_faulted_stream_closes_response_and_releases_inference(tmp_path, failure):
    class FaultStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            if failure == "error_read":
                raise httpx.ReadError("interrupted upstream error body")
            if failure == "cancel_read":
                raise asyncio.CancelledError()
            yield b"data: end\n\n"

        async def aclose(self):
            self.closed = True
            if failure.endswith("close"):
                raise httpx.ReadError("upstream close failed")

    stream = FaultStream()

    def upstream_response(_request):
        return httpx.Response(200 if failure == "relay_close" else 503, stream=stream)

    async with httpx.AsyncClient(
        base_url="http://offline.invalid",
        transport=httpx.MockTransport(upstream_response),
    ) as client:
        settings = Settings(
            game_marker_path=tmp_path / "game",
            gpu_lease_marker_path=tmp_path / "lease",
            gpu_lease_ack_path=tmp_path / "ack",
        )
        lock = asyncio.Lock()
        services = GatewayServices(
            Catalog.from_document(
                {"models": [{"id": "a", "router_id": "a", "type": "llm"}]}
            ),
            client,
            client,
            client,
            lock,
            LeaseStore(tmp_path / "lease", tmp_path / "ack"),
        )
        app = FastAPI()
        register_proxy_routes(app, GatewayRuntime(settings, _limits, services))
        endpoint = next(
            route.endpoint
            for route in app.routes
            if getattr(route, "path", None) == "/v1/{endpoint:path}"
        )

        async def receive():
            return {
                "type": "http.request",
                "body": b'{"model":"a","stream":true}',
                "more_body": False,
            }

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "app": app,
            },
            receive,
        )
        expected = (
            asyncio.CancelledError if failure == "cancel_read" else httpx.ReadError
        )
        with pytest.raises(expected):
            response = await endpoint("chat/completions", request)
            async for _ in response.body_iterator:
                pass
        assert stream.closed, "Even an unreadable upstream response must be closed"
        assert not lock.locked(), (
            "A failed request must not wedge subsequent inference or lease expiry"
        )
        await asyncio.wait_for(lock.acquire(), timeout=0.1)
        lock.release()
