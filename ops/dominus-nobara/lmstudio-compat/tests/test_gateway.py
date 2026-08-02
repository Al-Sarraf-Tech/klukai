from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import stat
import threading
import time
from typing import Any

import httpx
from fastapi.testclient import TestClient
import pytest

from gateway import main as gateway_main
from gateway.main import create_app
from gateway.settings import Settings


def write_catalog(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "publisher/chat-model",
                        "aliases": ["chat-model", "legacy-chat"],
                        "router_id": "chat-router-preset",
                        "type": "vlm",
                        "quantization": "Q4_K_M",
                        "max_context_length": 131072,
                        "loaded_context_length": 65536,
                        "publisher": "publisher",
                        "architecture": "testarch",
                        "capabilities": ["tool_use"],
                        "artifact_ids": ["chat-weights", "chat-mmproj"],
                    },
                    {
                        "id": "publisher/embed-model",
                        "type": "embeddings",
                        "quantization": "Q8_0",
                        "max_context_length": 2048,
                        "artifact_ids": ["embed-weights"],
                    },
                ],
                "artifacts": [
                    {"id": "chat-weights", "bytes": 1000, "destination": "chat.gguf"},
                    {"id": "chat-mmproj", "bytes": 200, "destination": "mmproj.gguf"},
                    {"id": "embed-weights", "bytes": 300, "destination": "embed.gguf"},
                ],
            }
        ),
        encoding="utf-8",
    )


def make_app(
    tmp_path: Path,
    handler: Any,
    *,
    gateway_token: str | None = None,
    upstream_token: str | None = None,
    marker: bool = False,
    require_native_vllm_ack: bool = False,
    comfy_handler: Any | None = None,
    voice_handler: Any | None = None,
):
    catalog_path = tmp_path / "models.lock.json"
    marker_path = tmp_path / "game-active"
    write_catalog(catalog_path)
    if marker:
        marker_path.touch()
    settings = Settings(
        upstream_url="http://router.test",
        comfyui_url="http://comfy.test",
        catalog_path=catalog_path,
        game_marker_path=marker_path,
        gpu_lease_marker_path=tmp_path / "non-llm-lease.json",
        gpu_lease_ack_path=tmp_path / "non-llm-lease-vllm-ack.json",
        require_native_vllm_ack=require_native_vllm_ack,
        gateway_token=gateway_token,
        upstream_token=upstream_token,
        companion_voice_token="voice-cleanup-secret",
    )

    def factory(config: Settings) -> httpx.AsyncClient:
        headers = {}
        if config.upstream_token:
            headers["Authorization"] = f"Bearer {config.upstream_token}"
        return httpx.AsyncClient(
            base_url=config.upstream_url,
            headers=headers,
            transport=httpx.MockTransport(handler),
        )

    def comfy_factory(config: Settings) -> httpx.AsyncClient:
        def default_comfy(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/queue" and request.method == "GET":
                return httpx.Response(
                    200, json={"queue_running": [], "queue_pending": []}
                )
            return httpx.Response(200, json={"status": "ok"})

        selected_handler = comfy_handler or default_comfy
        return httpx.AsyncClient(
            base_url=config.comfyui_url,
            transport=httpx.MockTransport(selected_handler),
        )

    def voice_factory(config: Settings) -> httpx.AsyncClient:
        def default_voice(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/unload":
                return httpx.Response(
                    200,
                    json={"status": "ok", "tts_loaded": False},
                )
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "tts_ready": False})
            return httpx.Response(404)

        return httpx.AsyncClient(
            base_url=config.companion_voice_url,
            headers={"Authorization": "Bearer voice-cleanup-secret"},
            transport=httpx.MockTransport(voice_handler or default_voice),
        )

    return create_app(
        settings, factory, comfy_factory, voice_factory
    ), marker_path


def default_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if request.url.path == "/models":
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "chat-router-preset",
                        "status": {"value": "loaded"},
                    },
                    {
                        "id": "publisher/embed-model",
                        "status": {"value": "sleeping"},
                    },
                ]
            },
        )
    if request.url.path in {"/models/load", "/models/unload"}:
        return httpx.Response(200, json={"success": True})
    return httpx.Response(200, json={"ok": True})


def test_environment_settings_fail_closed_without_gateway_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "GATEWAY_BEARER_TOKEN_FILE",
        "API_BEARER_TOKEN_FILE",
        "GATEWAY_BEARER_TOKEN",
        "API_BEARER_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="GATEWAY_BEARER_TOKEN"):
        Settings.from_env()


def test_environment_settings_require_voice_cleanup_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_BEARER_TOKEN", "gateway-secret")
    for name in (
        "COMPANION_VOICE_BEARER_TOKEN_FILE",
        "VOICE_API_TOKEN_FILE",
        "COMPANION_VOICE_BEARER_TOKEN",
        "VOICE_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="COMPANION_VOICE_BEARER_TOKEN"):
        Settings.from_env()


def test_environment_secrets_are_redacted_from_settings_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_BEARER_TOKEN", "gateway-secret")
    monkeypatch.setenv("COMPANION_VOICE_BEARER_TOKEN", "voice-secret")

    settings = Settings.from_env()

    assert "gateway-secret" not in repr(settings)
    assert "voice-secret" not in repr(settings)


def test_health_is_public_but_catalog_requires_configured_token(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path, default_handler, gateway_token="correct-token")
    with TestClient(app) as client:
        health = client.get("/health")
        unauthorized = client.get("/api/v0/models")
        authorized = client.get(
            "/api/v0/models", headers={"Authorization": "Bearer correct-token"}
        )

    assert health.status_code == 200
    assert health.json()["upstream"] == "ok"
    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert authorized.status_code == 200


def test_catalog_shapes_merge_router_state_and_metadata(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path, default_handler)
    with TestClient(app) as client:
        v0 = client.get("/api/v0/models").json()
        v1 = client.get("/api/v1/models").json()
        detail = client.get("/api/v0/models/legacy-chat").json()
        openai = client.get("/v1/models").json()

    assert v0["object"] == "list"
    assert v0["data"][0] == {
        "id": "publisher/chat-model",
        "object": "model",
        "type": "vlm",
        "publisher": "publisher",
        "arch": "testarch",
        "compatibility_type": "gguf",
        "quantization": "Q4_K_M",
        "state": "loaded",
        "max_context_length": 131072,
        "aliases": ["chat-model", "legacy-chat"],
        "capabilities": ["tool_use", "vision"],
    }
    assert v0["data"][1]["state"] == "not-loaded"
    assert detail["id"] == "publisher/chat-model"
    assert v1["models"][0]["size_bytes"] == 1200
    assert v1["models"][0]["loaded_instances"][0]["config"] == {"context_length": 65536}
    assert v1["models"][1]["type"] == "embedding"
    assert openai["data"][0]["id"] == "publisher/chat-model"


def test_nonstreaming_proxy_strips_ttl_maps_alias_and_separates_tokens(
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        seen["authorization"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "x-request-id": "upstream-id"},
            json={"choices": [{"message": {"content": "hello"}}]},
        )

    app, _ = make_app(
        tmp_path,
        handler,
        gateway_token="client-secret",
        upstream_token="router-secret",
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions?trace=yes",
            headers={"Authorization": "Bearer client-secret"},
            json={"model": "legacy-chat", "messages": [], "ttl": 600},
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "upstream-id"
    assert seen == {
        "path": "/v1/chat/completions",
        "query": "trace=yes",
        "authorization": "Bearer router-secret",
        "payload": {"model": "chat-router-preset", "messages": []},
    }


def test_inference_rejects_models_outside_locked_catalog(tmp_path: Path) -> None:
    upstream_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={"unexpected": True})

    app, _ = make_app(tmp_path, handler)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "unlocked/model", "messages": []},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    assert upstream_calls == 0


class ChunkStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        yield b"data: [DONE]\n\n"


def test_streaming_proxy_relays_sse_and_strips_ttl(tmp_path: Path) -> None:
    seen_payload: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkStream(),
        )

    app, _ = make_app(tmp_path, handler)
    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "chat-model", "messages": [], "stream": True, "ttl": 10},
        ) as response:
            content = b"".join(response.iter_bytes())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert content.endswith(b"data: [DONE]\n\n")
    assert "ttl" not in seen_payload
    assert seen_payload["model"] == "chat-router-preset"


def test_game_marker_blocks_inference_and_load_but_allows_catalog_and_unload(
    tmp_path: Path,
) -> None:
    management_calls: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/models":
            return httpx.Response(200, json={"data": []})
        if request.url.path.startswith("/models/"):
            management_calls.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"success": True})
        raise AssertionError(f"unexpected inference request: {request.url.path}")

    app, _ = make_app(tmp_path, handler, marker=True)
    with TestClient(app) as client:
        inference = client.post(
            "/v1/chat/completions", json={"model": "chat-model", "messages": []}
        )
        load = client.post("/api/v1/models/load", json={"model": "chat-model"})
        catalog = client.get("/api/v0/models")
        health = client.get("/health")
        unload = client.post(
            "/api/v1/models/unload", json={"instance_id": "legacy-chat"}
        )

    assert inference.status_code == 503
    assert inference.json()["error"]["code"] == "game_active"
    assert load.status_code == 503
    assert catalog.status_code == 200
    assert health.status_code == 200
    assert health.json()["game_active"] is True
    assert unload.status_code == 200
    assert management_calls == [("/models/unload", {"model": "chat-router-preset"})]


def test_v0_and_v1_management_mapping(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"success": True})

    app, _ = make_app(tmp_path, handler)
    with TestClient(app) as client:
        v0_load = client.post("/api/v0/models/load", json={"model": "chat-model"})
        v0_unload = client.post(
            "/api/v0/models/unload", json={"model": "publisher/chat-model"}
        )
        v1_load = client.post(
            "/api/v1/models/load",
            json={"model": "legacy-chat", "echo_load_config": True},
        )
        v1_unload = client.post(
            "/api/v1/models/unload", json={"instance_id": "legacy-chat"}
        )

    assert v0_load.json() == {
        "success": True,
        "model": "chat-model",
        "state": "loaded",
        "ttl": 900,
    }
    assert v0_unload.json()["state"] == "not-loaded"
    assert v1_load.json()["model_instance_id"] == "legacy-chat"
    assert v1_load.json()["ttl"] == 900
    assert v1_load.json()["load_config"] == {"context_length": 65536}
    assert v1_unload.json() == {"instance_id": "legacy-chat"}
    assert calls == [
        ("/models/load", {"model": "chat-router-preset"}),
        ("/models/unload", {"model": "chat-router-preset"}),
        ("/models/load", {"model": "chat-router-preset"}),
        ("/models/unload", {"model": "chat-router-preset"}),
    ]


@pytest.mark.parametrize("requested_ttl", [None, 0, 30, 900, 901, 86_400])
def test_load_ttl_is_always_overridden_to_900(
    tmp_path: Path, requested_ttl: int | None
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"success": True})

    app, _ = make_app(tmp_path, handler)
    body: dict[str, Any] = {"model": "chat-model"}
    if requested_ttl is not None:
        body["ttl"] = requested_ttl

    with TestClient(app) as client:
        v0 = client.post("/api/v0/models/load", json=body)
        v1 = client.post("/api/v1/models/load", json=body)

    assert v0.status_code == 200
    assert v0.json()["ttl"] == 900
    assert v1.status_code == 200
    assert v1.json()["ttl"] == 900
    # llama.cpp's management API only accepts `model`; the fixed 900-second
    # clock is configured on llama-server itself, never supplied by a client.
    assert calls == [
        ("/models/load", {"model": "chat-router-preset"}),
        ("/models/load", {"model": "chat-router-preset"}),
    ]


def test_upstream_failure_is_preserved_and_health_degrades(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            raise httpx.ConnectError("offline", request=request)
        if request.url.path == "/models":
            raise httpx.ConnectError("offline", request=request)
        if request.url.path == "/models/load":
            return httpx.Response(
                503,
                json={
                    "error": {
                        "code": 503,
                        "message": "cannot allocate model",
                        "type": "unavailable_error",
                    }
                },
            )
        return httpx.Response(500)

    app, _ = make_app(tmp_path, handler)
    with TestClient(app) as client:
        health = client.get("/health")
        catalog = client.get("/api/v0/models")
        load = client.post("/api/v0/models/load", json={"model": "chat-model"})

    assert health.json()["status"] == "degraded"
    assert catalog.json()["data"][0]["state"] == "not-loaded"
    assert load.status_code == 503
    assert load.json()["error"]["message"] == "cannot allocate model"


class StatefulRouter:
    def __init__(self, *, loaded: bool = True) -> None:
        self.loaded = loaded
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "chat-router-preset",
                            "status": {"value": "loaded" if self.loaded else "unloaded"},
                        },
                        {
                            "id": "publisher/embed-model",
                            "status": {"value": "unloaded"},
                        },
                    ]
                },
            )
        if request.url.path == "/models/unload":
            self.loaded = False
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/models/load":
            self.loaded = True
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"ok": True})


def test_gpu_lease_quiesces_router_blocks_load_and_releases_without_token_leak(
    tmp_path: Path,
) -> None:
    router = StatefulRouter()
    app, _ = make_app(tmp_path, router, gateway_token="client-secret")
    auth = {"Authorization": "Bearer client-secret"}
    lease_marker = tmp_path / "non-llm-lease.json"

    with TestClient(app) as client:
        unauthorized = client.post(
            "/api/v1/gpu/lease/acquire",
            json={"owner": "klukai-core", "workload": "comfyui", "ttl_seconds": 600},
        )
        acquired = client.post(
            "/api/v1/gpu/lease/acquire",
            headers=auth,
            json={"owner": "klukai-core", "workload": "comfyui", "ttl_seconds": 9999},
        )
        token = acquired.json()["lease_token"]
        marker_text = lease_marker.read_text(encoding="utf-8")
        marker_mode = stat.S_IMODE(lease_marker.stat().st_mode)
        health = client.get("/health")
        inference = client.post(
            "/v1/chat/completions",
            headers=auth,
            json={"model": "chat-model", "messages": []},
        )
        load = client.post(
            "/api/v1/models/load", headers=auth, json={"model": "chat-model"}
        )
        catalog = client.get("/api/v1/models", headers=auth)
        unload = client.post(
            "/api/v1/models/unload",
            headers=auth,
            json={"instance_id": "chat-model"},
        )
        second_acquire = client.post(
            "/api/v1/gpu/lease/acquire",
            headers=auth,
            json={"owner": "klukai-core", "workload": "companion-voice", "ttl_seconds": 600},
        )
        wrong_release = client.post(
            "/api/v1/gpu/lease/release",
            headers=auth,
            json={"lease_token": "not-the-owner"},
        )
        exists_after_wrong_release = lease_marker.exists()
        released = client.post(
            "/api/v1/gpu/lease/release",
            headers=auth,
            json={"lease_token": token},
        )
        idempotent_release = client.post(
            "/api/v1/gpu/lease/release",
            headers=auth,
            json={"lease_token": token},
        )
        inference_after = client.post(
            "/v1/chat/completions",
            headers=auth,
            json={"model": "chat-model", "messages": []},
        )

    assert unauthorized.status_code == 401
    assert acquired.status_code == 201
    assert acquired.json()["status"] == "acquired"
    assert acquired.json()["ttl_seconds"] == 600
    assert router.calls[:3] == ["/models", "/models/unload", "/models"]
    assert router.loaded is False
    assert token not in marker_text
    marker = json.loads(marker_text)
    assert marker["state"] == "active"
    assert marker["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert marker_mode == 0o640
    assert health.json()["gpu_lease_state"] == "active"
    assert health.json()["gpu_lease_workload"] == "comfyui"
    assert token not in health.text
    assert inference.status_code == 503
    assert inference.json()["error"]["code"] == "gpu_leased"
    assert load.status_code == 503
    assert load.json()["error"]["code"] == "gpu_leased"
    assert catalog.status_code == 200
    assert unload.status_code == 200
    assert second_acquire.status_code == 409
    assert second_acquire.json()["error"]["code"] == "gpu_lease_busy"
    assert wrong_release.status_code == 403
    assert exists_after_wrong_release
    assert released.json() == {"status": "released", "was_active": True}
    assert token not in released.text
    assert idempotent_release.json() == {"status": "released", "was_active": False}
    assert not lease_marker.exists()
    assert inference_after.status_code == 200


class SlowUnloadRouter:
    """llama.cpp frees VRAM after /models/unload returns, not within the same
    call — the next few residency polls can still report the outgoing model
    before it actually clears."""

    def __init__(self, *, clears_after_polls: int) -> None:
        self.calls: list[str] = []
        self._loaded = True
        self._unload_requested = False
        self._polls_since_unload = 0
        self._clears_after_polls = clears_after_polls

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/models/unload":
            self._unload_requested = True
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/models":
            if self._unload_requested:
                self._polls_since_unload += 1
                if self._polls_since_unload > self._clears_after_polls:
                    self._loaded = False
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "chat-router-preset",
                            "status": {
                                "value": "loaded" if self._loaded else "unloaded"
                            },
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"ok": True})


def test_gpu_lease_acquire_polls_router_quiescence_before_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_main, "ROUTER_UNLOAD_POLL_SECONDS", 0.01)
    router = SlowUnloadRouter(clears_after_polls=2)
    app, _ = make_app(tmp_path, router, gateway_token="client-secret")
    auth = {"Authorization": "Bearer client-secret"}

    with TestClient(app) as client:
        acquired = client.post(
            "/api/v1/gpu/lease/acquire",
            headers=auth,
            json={"owner": "klukai-core", "workload": "comfyui", "ttl_seconds": 600},
        )

    assert acquired.status_code == 201
    assert acquired.json()["status"] == "acquired"


def test_gpu_lease_acquire_fails_if_router_never_quiesces_within_the_drain_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_main, "ROUTER_UNLOAD_POLL_SECONDS", 0.01)
    monkeypatch.setattr(gateway_main, "ROUTER_UNLOAD_DRAIN_SECONDS", 0.05)
    router = SlowUnloadRouter(clears_after_polls=10_000)
    app, _ = make_app(tmp_path, router, gateway_token="client-secret")
    auth = {"Authorization": "Bearer client-secret"}

    with TestClient(app) as client:
        acquired = client.post(
            "/api/v1/gpu/lease/acquire",
            headers=auth,
            json={"owner": "klukai-core", "workload": "comfyui", "ttl_seconds": 600},
        )

    assert acquired.status_code == 503
    assert acquired.json()["error"]["code"] == "router_not_quiesced"


def test_gpu_lease_survives_restart_and_expiry_cleans_before_marker_removal(
    tmp_path: Path,
) -> None:
    router = StatefulRouter(loaded=False)
    app, _ = make_app(tmp_path, router)
    with TestClient(app) as client:
        acquired = client.post(
            "/api/v1/gpu/lease/acquire",
            json={"owner": "klukai-core", "workload": "comfyui", "ttl_seconds": 600},
        )
    token = acquired.json()["lease_token"]

    restarted_app, _ = make_app(tmp_path, router)
    with TestClient(restarted_app) as client:
        blocked = client.post(
            "/v1/chat/completions",
            json={"model": "chat-model", "messages": []},
        )
        released = client.post(
            "/api/v1/gpu/lease/release", json={"lease_token": token}
        )

    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "gpu_leased"
    assert released.status_code == 200

    now = time.time()
    (tmp_path / "non-llm-lease.json").write_text(
        json.dumps(
            {
                "version": 1,
                "lease_id": "a" * 32,
                "token_sha256": "b" * 64,
                "workload": "comfyui",
                "issued_at_epoch_seconds": now - 601,
                "expires_at_epoch_seconds": now - 1,
                "ttl_seconds": 600,
            }
        ),
        encoding="utf-8",
    )
    cleanup_events: list[str] = []
    expiry_marker = tmp_path / "non-llm-lease.json"

    def expiry_comfy(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"status": "ok"})
        cleanup_events.append(f"comfy:{request.method}:{request.url.path}")
        assert expiry_marker.exists()
        assert json.loads(expiry_marker.read_text())["state"] == "cleaning"
        if request.url.path == "/queue" and request.method == "GET":
            return httpx.Response(
                200, json={"queue_running": [], "queue_pending": []}
            )
        return httpx.Response(200, json={"status": "ok"})

    def expiry_voice(request: httpx.Request) -> httpx.Response:
        cleanup_events.append(f"voice:{request.method}:{request.url.path}")
        assert expiry_marker.exists()
        assert json.loads(expiry_marker.read_text())["state"] == "cleaning"
        if request.url.path == "/unload":
            return httpx.Response(200, json={"status": "ok", "tts_loaded": False})
        return httpx.Response(200, json={"status": "ok", "tts_ready": False})

    expiry_app, _ = make_app(
        tmp_path,
        router,
        comfy_handler=expiry_comfy,
        voice_handler=expiry_voice,
    )
    with TestClient(expiry_app) as client:
        deadline = time.monotonic() + 2
        while True:
            health = client.get("/health")
            if health.json()["gpu_lease_state"] == "inactive":
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        allowed = client.post(
            "/v1/chat/completions",
            json={"model": "chat-model", "messages": []},
        )
    assert health.json()["gpu_lease_state"] == "inactive"
    assert allowed.status_code == 200
    assert cleanup_events == [
        "comfy:POST:/interrupt",
        "comfy:POST:/queue",
        "comfy:GET:/queue",
        "comfy:POST:/free",
        "comfy:GET:/queue",
        "voice:POST:/unload",
        "voice:GET:/health",
    ]
    assert not (tmp_path / "non-llm-lease.json").exists()


def test_malformed_gpu_lease_marker_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "non-llm-lease.json").write_text("not-json", encoding="utf-8")
    app, _ = make_app(tmp_path, StatefulRouter(loaded=False))
    with TestClient(app) as client:
        health = client.get("/health")
        inference = client.post(
            "/v1/chat/completions",
            json={"model": "chat-model", "messages": []},
        )
        acquire = client.post(
            "/api/v1/gpu/lease/acquire",
            json={"owner": "klukai-core", "workload": "comfyui", "ttl_seconds": 600},
        )

    assert health.json()["gpu_lease_state"] == "invalid"
    assert health.json()["gpu_lease_active"] is True
    assert inference.status_code == 503
    assert inference.json()["error"]["code"] == "gpu_lease_state_invalid"
    assert acquire.status_code == 503
    assert (tmp_path / "non-llm-lease.json").read_text() == "not-json"


def test_gpu_lease_waits_for_matching_native_vllm_ack(tmp_path: Path) -> None:
    router = StatefulRouter(loaded=False)
    app, _ = make_app(tmp_path, router, require_native_vllm_ack=True)
    marker = tmp_path / "non-llm-lease.json"
    ack = tmp_path / "non-llm-lease-vllm-ack.json"

    def native_watchdog() -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.01)
        document = json.loads(marker.read_text(encoding="utf-8"))
        ack.write_text(
            json.dumps(
                {
                    "version": 1,
                    "lease_id": document["lease_id"],
                    "acknowledged_at_epoch_seconds": time.time(),
                }
            ),
            encoding="utf-8",
        )

    watchdog_thread = threading.Thread(target=native_watchdog)
    watchdog_thread.start()
    with TestClient(app) as client:
        acquired = client.post(
            "/api/v1/gpu/lease/acquire",
            json={"owner": "klukai-core", "workload": "comfyui", "ttl_seconds": 600},
        )
        assert acquired.status_code == 201
        released = client.post(
            "/api/v1/gpu/lease/release",
            json={"lease_token": acquired.json()["lease_token"]},
        )
    watchdog_thread.join(timeout=2)

    assert not watchdog_thread.is_alive()
    assert released.status_code == 200
    assert not marker.exists()
    assert not ack.exists()


def test_comfy_facade_requires_bearer_and_matching_active_lease(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def comfy_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": "ok"})
        if request.url.path == "/queue" and request.method == "GET":
            return httpx.Response(
                200, json={"queue_running": [], "queue_pending": []}
            )
        if request.url.path in {"/interrupt", "/queue", "/free"}:
            return httpx.Response(200, json={"status": "ok"})
        seen.update(
            {
                "path": request.url.path,
                "query": request.url.query.decode(),
                "authorization": request.headers.get("authorization"),
                "lease_header": request.headers.get("x-gpu-lease-token"),
                "payload": json.loads(request.content),
            }
        )
        return httpx.Response(200, json={"prompt_id": "job-1"})

    app, _ = make_app(
        tmp_path,
        StatefulRouter(loaded=False),
        gateway_token="client-secret",
        comfy_handler=comfy_handler,
    )
    auth = {"Authorization": "Bearer client-secret"}
    with TestClient(app) as client:
        health = client.get("/health")
        acquired = client.post(
            "/api/v1/gpu/lease/acquire",
            headers=auth,
            json={"owner": "klukai-core", "workload": "comfyui", "ttl_seconds": 600},
        )
        token = acquired.json()["lease_token"]
        no_bearer = client.post(
            "/api/v1/comfy/prompt",
            headers={"X-GPU-Lease-Token": token},
            json={"prompt": {}},
        )
        no_lease = client.post(
            "/api/v1/comfy/prompt", headers=auth, json={"prompt": {}}
        )
        wrong_lease = client.post(
            "/api/v1/comfy/prompt",
            headers={**auth, "X-GPU-Lease-Token": "wrong"},
            json={"prompt": {}},
        )
        proxied = client.post(
            "/api/v1/comfy/prompt?client=core",
            headers={**auth, "X-GPU-Lease-Token": token},
            json={"prompt": {"node": "value"}},
        )
        client.post(
            "/api/v1/gpu/lease/release",
            headers=auth,
            json={"lease_token": token},
        )
        after_release = client.get(
            "/api/v1/comfy/history/job-1",
            headers={**auth, "X-GPU-Lease-Token": token},
        )

    assert health.json()["comfyui_status"] == "ok"
    assert no_bearer.status_code == 401
    assert no_lease.status_code == 403
    assert wrong_lease.status_code == 403
    assert proxied.status_code == 200
    assert proxied.json() == {"prompt_id": "job-1"}
    assert seen == {
        "path": "/prompt",
        "query": "client=core",
        "authorization": None,
        "lease_header": None,
        "payload": {"prompt": {"node": "value"}},
    }
    assert after_release.status_code == 503
    assert after_release.json()["error"]["code"] == "gpu_lease_required"


def test_comfy_admission_holds_release_until_upstream_request_finishes(
    tmp_path: Path,
) -> None:
    prompt_started = threading.Event()
    finish_prompt = threading.Event()
    marker = tmp_path / "non-llm-lease.json"
    calls: list[tuple[str, str]] = []

    def comfy_handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/prompt":
            return httpx.Response(
                200,
                stream=BlockingStream(prompt_started, finish_prompt),
            )
        if request.url.path == "/queue" and request.method == "GET":
            return httpx.Response(
                200, json={"queue_running": [], "queue_pending": []}
            )
        return httpx.Response(200, json={"status": "ok"})

    app, _ = make_app(
        tmp_path,
        StatefulRouter(loaded=False),
        comfy_handler=comfy_handler,
    )
    proxy_result: list[httpx.Response] = []
    release_result: list[httpx.Response] = []

    with TestClient(app) as client:
        acquired = client.post(
            "/api/v1/gpu/lease/acquire",
            json={"owner": "klukai-core", "workload": "comfyui", "ttl_seconds": 600},
        )
        token = acquired.json()["lease_token"]
        calls.clear()
        headers = {"X-GPU-Lease-Token": token}

        proxy_thread = threading.Thread(
            target=lambda: proxy_result.append(
                client.post(
                    "/api/v1/comfy/prompt",
                    headers=headers,
                    json={"prompt": {}},
                )
            )
        )
        proxy_thread.start()
        assert prompt_started.wait(timeout=2)

        release_thread = threading.Thread(
            target=lambda: release_result.append(
                client.post(
                    "/api/v1/gpu/lease/release",
                    json={"lease_token": token},
                )
            )
        )
        release_thread.start()
        time.sleep(0.05)
        assert release_thread.is_alive()
        assert marker.exists()
        assert json.loads(marker.read_text())["state"] == "active"
        assert calls == [("POST", "/prompt")]

        finish_prompt.set()
        proxy_thread.join(timeout=2)
        release_thread.join(timeout=2)

    assert not proxy_thread.is_alive()
    assert not release_thread.is_alive()
    assert proxy_result[0].status_code == 200
    assert release_result[0].status_code == 200
    assert calls == [
        ("POST", "/prompt"),
        ("POST", "/interrupt"),
        ("POST", "/queue"),
        ("GET", "/queue"),
        ("POST", "/free"),
        ("GET", "/queue"),
    ]
    assert not marker.exists()


def test_gateway_owns_comfy_cleanup_before_removing_marker(tmp_path: Path) -> None:
    marker = tmp_path / "non-llm-lease.json"
    calls: list[tuple[str, str]] = []

    def comfy_handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/queue" and request.method == "GET":
            assert marker.exists()
            return httpx.Response(
                200, json={"queue_running": [], "queue_pending": []}
            )
        if request.url.path in {"/interrupt", "/queue", "/free"}:
            assert marker.exists()
            assert json.loads(marker.read_text())["state"] in {"active", "cleaning"}
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"system": "ok"})

    app, _ = make_app(
        tmp_path,
        StatefulRouter(loaded=False),
        comfy_handler=comfy_handler,
    )
    with TestClient(app) as client:
        acquired = client.post(
            "/api/v1/gpu/lease/acquire",
            json={"owner": "klukai-core", "workload": "comfyui", "ttl_seconds": 600},
        )
        assert acquired.status_code == 201
        acquire_calls = list(calls)
        calls.clear()
        released = client.post(
            "/api/v1/gpu/lease/release",
            json={"lease_token": acquired.json()["lease_token"]},
        )

    assert released.json() == {"status": "released", "was_active": True}
    assert acquire_calls == [
        ("POST", "/interrupt"),
        ("POST", "/queue"),
        ("GET", "/queue"),
        ("POST", "/free"),
        ("GET", "/queue"),
    ]
    assert calls == [
        ("POST", "/interrupt"),
        ("POST", "/queue"),
        ("GET", "/queue"),
        ("POST", "/free"),
        ("GET", "/queue"),
    ]
    assert not marker.exists()


def test_cleanup_failure_retains_marker_and_same_token_can_retry(
    tmp_path: Path,
) -> None:
    cleanup_enabled = True
    marker = tmp_path / "non-llm-lease.json"

    def comfy_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/interrupt" and not cleanup_enabled:
            return httpx.Response(500)
        if request.url.path == "/queue" and request.method == "GET":
            return httpx.Response(
                200, json={"queue_running": [], "queue_pending": []}
            )
        return httpx.Response(200, json={"status": "ok"})

    app, _ = make_app(
        tmp_path,
        StatefulRouter(loaded=False),
        comfy_handler=comfy_handler,
    )
    with TestClient(app) as client:
        acquired = client.post(
            "/api/v1/gpu/lease/acquire",
            json={"owner": "klukai-core", "workload": "comfyui", "ttl_seconds": 600},
        )
        assert acquired.status_code == 201
        token = acquired.json()["lease_token"]
        cleanup_enabled = False
        failed = client.post(
            "/api/v1/gpu/lease/release", json={"lease_token": token}
        )
        retained = json.loads(marker.read_text())
        blocked = client.post(
            "/v1/chat/completions",
            json={"model": "chat-model", "messages": []},
        )
        cleanup_enabled = True
        retried = client.post(
            "/api/v1/gpu/lease/release", json={"lease_token": token}
        )

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "gpu_cleanup_unconfirmed"
    assert token not in failed.text
    assert retained["state"] == "cleanup_failed"
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "gpu_leased"
    assert retried.json() == {"status": "released", "was_active": True}
    assert not marker.exists()


@pytest.mark.parametrize("failure_kind", ["runtime", "timeout"])
def test_cleanup_client_error_keeps_gateway_fail_closed(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    crash_enabled = False
    marker = tmp_path / "non-llm-lease.json"

    def comfy_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/interrupt" and crash_enabled:
            if failure_kind == "timeout":
                raise httpx.ReadTimeout(
                    "simulated cleanup timeout with secret material",
                    request=request,
                )
            raise RuntimeError("simulated cleanup client crash with secret material")
        if request.url.path == "/queue" and request.method == "GET":
            return httpx.Response(
                200, json={"queue_running": [], "queue_pending": []}
            )
        return httpx.Response(200, json={"status": "ok"})

    app, _ = make_app(
        tmp_path,
        StatefulRouter(loaded=False),
        comfy_handler=comfy_handler,
    )
    with TestClient(app) as client:
        acquired = client.post(
            "/api/v1/gpu/lease/acquire",
            json={"owner": "klukai-core", "workload": "comfyui", "ttl_seconds": 600},
        )
        assert acquired.status_code == 201
        token = acquired.json()["lease_token"]
        crash_enabled = True
        failed = client.post(
            "/api/v1/gpu/lease/release", json={"lease_token": token}
        )
        retained = json.loads(marker.read_text())
        blocked = client.post(
            "/v1/chat/completions",
            json={"model": "chat-model", "messages": []},
        )

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "gpu_cleanup_unconfirmed"
    assert "simulated cleanup client crash" not in failed.text
    assert "simulated cleanup timeout" not in failed.text
    assert token not in failed.text
    assert retained["state"] == "cleanup_failed"
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "gpu_leased"
    assert marker.exists()


def test_companion_voice_release_unloads_with_internal_bearer(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "non-llm-lease.json"
    calls: list[tuple[str, str | None]] = []

    def voice_handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers.get("authorization")))
        assert marker.exists()
        assert json.loads(marker.read_text())["state"] in {"active", "cleaning"}
        if request.url.path == "/unload":
            return httpx.Response(200, json={"status": "ok", "tts_loaded": False})
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "tts_ready": False})
        return httpx.Response(404)

    app, _ = make_app(
        tmp_path,
        StatefulRouter(loaded=False),
        voice_handler=voice_handler,
    )
    with TestClient(app) as client:
        acquired = client.post(
            "/api/v1/gpu/lease/acquire",
            json={
                "owner": "klukai-core",
                "workload": "companion-voice",
                "ttl_seconds": 600,
            },
        )
        assert acquired.status_code == 201
        acquire_calls = list(calls)
        calls.clear()
        released = client.post(
            "/api/v1/gpu/lease/release",
            json={"lease_token": acquired.json()["lease_token"]},
        )

    assert released.status_code == 200
    assert acquire_calls == [
        ("/unload", "Bearer voice-cleanup-secret"),
        ("/health", "Bearer voice-cleanup-secret"),
    ]
    assert calls == [
        ("/unload", "Bearer voice-cleanup-secret"),
        ("/health", "Bearer voice-cleanup-secret"),
    ]
    assert not marker.exists()


def test_unknown_gpu_workload_is_rejected_without_marker(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path, StatefulRouter(loaded=False))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/gpu/lease/acquire",
            json={"owner": "klukai-core", "workload": "speaches", "ttl_seconds": 600},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "gpu_lease_workload_invalid"
    assert not (tmp_path / "non-llm-lease.json").exists()


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self, started: threading.Event, finish: threading.Event) -> None:
        self.started = started
        self.finish = finish

    async def __aiter__(self):
        self.started.set()
        yield b'data: {"choices":[{"delta":{"content":"working"}}]}\n\n'
        while not self.finish.is_set():
            await asyncio.sleep(0.01)
        yield b"data: [DONE]\n\n"


def test_gpu_lease_acquire_waits_for_complete_stream(tmp_path: Path) -> None:
    started = threading.Event()
    finish = threading.Event()
    router = StatefulRouter(loaded=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=BlockingStream(started, finish),
            )
        return router(request)

    app, _ = make_app(tmp_path, handler)
    stream_result: list[int] = []
    lease_result: list[httpx.Response] = []

    with TestClient(app) as client:
        def consume_stream() -> None:
            with client.stream(
                "POST",
                "/v1/chat/completions",
                json={"model": "chat-model", "messages": [], "stream": True},
            ) as response:
                b"".join(response.iter_bytes())
                stream_result.append(response.status_code)

        def acquire() -> None:
            lease_result.append(
                client.post(
                    "/api/v1/gpu/lease/acquire",
                    json={
                        "owner": "klukai-core",
                        "workload": "comfyui",
                        "ttl_seconds": 600,
                    },
                )
            )

        stream_thread = threading.Thread(target=consume_stream)
        stream_thread.start()
        assert started.wait(timeout=2)
        acquire_thread = threading.Thread(target=acquire)
        acquire_thread.start()
        time.sleep(0.1)
        assert acquire_thread.is_alive()
        assert not (tmp_path / "non-llm-lease.json").exists()
        finish.set()
        stream_thread.join(timeout=2)
        acquire_thread.join(timeout=2)

        assert not stream_thread.is_alive()
        assert not acquire_thread.is_alive()
        assert stream_result == [200]
        assert lease_result[0].status_code == 201
        token = lease_result[0].json()["lease_token"]
        client.post("/api/v1/gpu/lease/release", json={"lease_token": token})


def test_gpu_lease_acquire_has_bounded_wait_for_inflight_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    finish = threading.Event()
    router = StatefulRouter(loaded=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=BlockingStream(started, finish),
            )
        return router(request)

    monkeypatch.setattr(
        "gateway.main.LEASE_ACQUIRE_COORDINATION_WAIT_SECONDS",
        0.02,
    )
    app, _ = make_app(tmp_path, handler)
    stream_result: list[int] = []

    with TestClient(app) as client:
        def consume_stream() -> None:
            with client.stream(
                "POST",
                "/v1/chat/completions",
                json={"model": "chat-model", "messages": [], "stream": True},
            ) as response:
                b"".join(response.iter_bytes())
                stream_result.append(response.status_code)

        stream_thread = threading.Thread(target=consume_stream)
        stream_thread.start()
        assert started.wait(timeout=2)
        acquired = client.post(
            "/api/v1/gpu/lease/acquire",
            json={"owner": "klukai-core", "workload": "comfyui", "ttl_seconds": 600},
        )
        assert acquired.status_code == 503
        assert acquired.json()["error"]["code"] == "gpu_coordination_busy"
        assert not (tmp_path / "non-llm-lease.json").exists()
        finish.set()
        stream_thread.join(timeout=2)

    assert not stream_thread.is_alive()
    assert stream_result == [200]
