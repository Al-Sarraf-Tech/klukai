"""Cross-process GPU lease contract for llama.cpp -> ComfyUI handoff."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.gpu_lease import (
    GPU_LEASE_ACQUIRE_READ_TIMEOUT_SECONDS,
    GPU_LEASE_ACQUIRE_PATH,
    GPU_LEASE_CONNECT_TIMEOUT_SECONDS,
    GPU_LEASE_HEADER,
    GPU_LEASE_RELEASE_READ_TIMEOUT_SECONDS,
    GPU_LEASE_RELEASE_PATH,
    GPU_LEASE_TTL_SECONDS,
    GPULease,
    GPULeaseError,
    acquire_gpu_lease,
    gpu_lease,
    gpu_lease_auth_headers,
    gpu_lease_capability_headers,
    release_gpu_lease,
)


class _Response:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _acquired(token: str = "opaque-lease-capability") -> _Response:
    return _Response(
        201,
        {
            "status": "acquired",
            "lease_token": token,
            "ttl_seconds": GPU_LEASE_TTL_SECONDS,
        },
    )


@pytest.mark.asyncio
async def test_acquire_uses_gateway_bearer_and_fixed_bounded_contract(monkeypatch):
    monkeypatch.setenv("LM_STUDIO_URL", "http://100.107.121.5:1234/")
    monkeypatch.setenv("LM_STUDIO_TOKEN", "gateway-secret")
    client = MagicMock()
    client.post = AsyncMock(return_value=_acquired())

    lease = await acquire_gpu_lease(client, workload="comfyui")

    assert lease.ttl_seconds == 600
    assert GPU_LEASE_ACQUIRE_READ_TIMEOUT_SECONDS == 150.0
    assert lease.token == "opaque-lease-capability"
    call = client.post.await_args
    assert call.args[0] == f"http://100.107.121.5:1234{GPU_LEASE_ACQUIRE_PATH}"
    assert call.kwargs["headers"] == {"Authorization": "Bearer gateway-secret"}
    assert call.kwargs["json"] == {
        "owner": "klukai-core",
        "workload": "comfyui",
        "ttl_seconds": 600,
    }
    timeout = call.kwargs["timeout"]
    assert timeout.connect == GPU_LEASE_CONNECT_TIMEOUT_SECONDS
    assert timeout.read == GPU_LEASE_ACQUIRE_READ_TIMEOUT_SECONDS
    assert timeout.write == GPU_LEASE_CONNECT_TIMEOUT_SECONDS
    assert timeout.pool == GPU_LEASE_CONNECT_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_context_releases_in_finally_without_token_in_url(monkeypatch):
    monkeypatch.setenv("LM_STUDIO_TOKEN", "gateway-secret")
    token = "never-log-this-lease-token"
    client = MagicMock()
    client.post = AsyncMock(side_effect=[_acquired(token), _Response(204)])

    with pytest.raises(RuntimeError, match="render failed"):
        async with gpu_lease("comfyui", client=client) as lease:
            assert token not in repr(lease)
            raise RuntimeError("render failed")

    acquire_call, release_call = client.post.await_args_list
    assert acquire_call.args[0].endswith(GPU_LEASE_ACQUIRE_PATH)
    assert release_call.args[0].endswith(GPU_LEASE_RELEASE_PATH)
    assert token not in release_call.args[0]
    assert release_call.kwargs["json"] == {"lease_token": token}
    assert release_call.kwargs["headers"] == {"Authorization": "Bearer gateway-secret"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"status": "acquired", "lease_token": "token", "ttl_seconds": 601},
        {"status": "acquired", "lease_token": "token", "ttl_seconds": 599},
        {"status": "acquired", "lease_token": "", "ttl_seconds": 600},
        {"status": "busy", "lease_token": "token", "ttl_seconds": 600},
        ["not", "an", "object"],
    ],
)
async def test_acquire_fails_closed_on_contract_drift(monkeypatch, payload):
    monkeypatch.setenv("LM_STUDIO_TOKEN", "gateway-secret")
    client = MagicMock()
    client.post = AsyncMock(return_value=_Response(200, payload))

    with pytest.raises(GPULeaseError):
        await acquire_gpu_lease(client, workload="comfyui")


@pytest.mark.asyncio
async def test_game_or_busy_response_never_echoes_body_or_secret(monkeypatch):
    monkeypatch.setenv("LM_STUDIO_TOKEN", "gateway-secret")
    secret_body = "game_active gateway-secret opaque-lease-capability"
    client = MagicMock()
    client.post = AsyncMock(return_value=_Response(503, {"detail": secret_body}))

    with pytest.raises(GPULeaseError) as caught:
        await acquire_gpu_lease(client, workload="comfyui")

    message = str(caught.value)
    assert message == "GPU lease acquire failed with HTTP 503"
    assert "gateway-secret" not in message
    assert "opaque-lease-capability" not in message


@pytest.mark.asyncio
async def test_missing_gateway_token_fails_before_network(monkeypatch):
    monkeypatch.delenv("LM_STUDIO_TOKEN", raising=False)
    client = MagicMock()
    client.post = AsyncMock()

    with pytest.raises(GPULeaseError, match="acquire was unavailable"):
        await acquire_gpu_lease(client, workload="comfyui")

    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_failure_is_credential_free(monkeypatch):
    monkeypatch.setenv("LM_STUDIO_TOKEN", "gateway-secret")
    client = MagicMock()
    request = httpx.Request("POST", "http://gateway/lease")
    client.post = AsyncMock(
        side_effect=httpx.ConnectError("gateway-secret", request=request)
    )

    with pytest.raises(GPULeaseError) as caught:
        await acquire_gpu_lease(client, workload="comfyui")

    assert str(caught.value) == "GPU lease acquire was unavailable"
    assert "gateway-secret" not in str(caught.value)


def test_lease_repr_hides_capability():
    lease = GPULease(ttl_seconds=600, token="opaque-lease-capability")

    assert "opaque-lease-capability" not in repr(lease)


def test_facade_headers_bind_gateway_auth_to_opaque_lease(monkeypatch):
    monkeypatch.setenv("LM_STUDIO_TOKEN", "gateway-secret")
    lease = GPULease(ttl_seconds=600, token="opaque-lease-capability")

    assert gpu_lease_auth_headers(lease) == {
        "Authorization": "Bearer gateway-secret",
        GPU_LEASE_HEADER: "opaque-lease-capability",
    }


def test_workload_header_contains_capability_but_not_gateway_bearer(monkeypatch):
    monkeypatch.setenv("LM_STUDIO_TOKEN", "gateway-secret")
    lease = GPULease(ttl_seconds=600, token="opaque-lease-capability")

    assert gpu_lease_capability_headers(lease) == {
        GPU_LEASE_HEADER: "opaque-lease-capability"
    }


@pytest.mark.asyncio
async def test_release_retries_gateway_cleanup_failure_without_leaking_token(
    monkeypatch,
):
    monkeypatch.setenv("LM_STUDIO_TOKEN", "gateway-secret")
    lease = GPULease(ttl_seconds=600, token="opaque-lease-capability")
    client = MagicMock()
    client.post = AsyncMock(side_effect=[_Response(503), _Response(200)])

    await release_gpu_lease(client, lease)

    assert GPU_LEASE_RELEASE_READ_TIMEOUT_SECONDS == 90.0
    assert client.post.await_count == 2
    for call in client.post.await_args_list:
        assert "opaque-lease-capability" not in call.args[0]
        assert call.kwargs["json"] == {"lease_token": "opaque-lease-capability"}
        timeout = call.kwargs["timeout"]
        assert timeout.connect == GPU_LEASE_CONNECT_TIMEOUT_SECONDS
        assert timeout.read == GPU_LEASE_RELEASE_READ_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_release_does_not_retry_invalid_capability(monkeypatch):
    monkeypatch.setenv("LM_STUDIO_TOKEN", "gateway-secret")
    lease = GPULease(ttl_seconds=600, token="opaque-lease-capability")
    client = MagicMock()
    client.post = AsyncMock(return_value=_Response(403))

    with pytest.raises(GPULeaseError, match="release failed with HTTP 403"):
        await release_gpu_lease(client, lease)

    client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_client_waits_for_release_attempt_to_finish(monkeypatch):
    monkeypatch.setenv("LM_STUDIO_TOKEN", "gateway-secret")
    body_started = asyncio.Event()
    release_started = asyncio.Event()
    release_finish = asyncio.Event()
    client = MagicMock()

    async def post(url, **_kwargs):
        if url.endswith(GPU_LEASE_ACQUIRE_PATH):
            return _acquired()
        release_started.set()
        await release_finish.wait()
        return _Response(204)

    client.post = AsyncMock(side_effect=post)

    async def request():
        async with gpu_lease("companion-voice", client=client):
            body_started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(request())
    await asyncio.wait_for(body_started.wait(), timeout=1)
    task.cancel()
    await asyncio.wait_for(release_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not task.done()
    release_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_owned_client_ignores_proxies_and_redirects(monkeypatch):
    monkeypatch.setenv("LM_STUDIO_TOKEN", "gateway-secret")
    client = MagicMock()
    client.post = AsyncMock(side_effect=[_acquired(), _Response(204)])
    client.aclose = AsyncMock()

    with patch("app.gpu_lease.httpx.AsyncClient", return_value=client) as factory:
        async with gpu_lease("comfyui"):
            pass

    factory.assert_called_once_with(trust_env=False, follow_redirects=False)
    client.aclose.assert_awaited_once()
