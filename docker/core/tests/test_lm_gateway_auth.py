"""Authentication and residency-policy regression tests for local LM callers."""

from __future__ import annotations

import ast
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from app.lm_gateway import LM_TTL_SECONDS, lm_studio_auth_headers
from app.llm_json import call_llm, call_llm_text


_CORE_DIR = Path(__file__).resolve().parent.parent
_GATEWAY_CALLERS = {
    "app/llm_json.py": 2,
    "app/affection.py": 1,
    "app/memory_archive.py": 1,
    "seed_memories.py": 1,
    "reannotate_existing.py": 1,
}


class _Response:
    status_code = 200

    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


class _Gate:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _client_with(content: str) -> MagicMock:
    client = MagicMock()
    client.post = AsyncMock(return_value=_Response(content))
    return client


def _assert_authenticated_call(client: MagicMock, token: str) -> dict:
    call = client.post.await_args
    assert call.kwargs["headers"] == {"Authorization": f"Bearer {token}"}
    assert call.kwargs["json"]["ttl"] == 900 == LM_TTL_SECONDS
    return call.kwargs["json"]


def test_auth_header_reads_current_token_and_fails_closed(monkeypatch):
    monkeypatch.setenv("LM_STUDIO_TOKEN", "first-token")
    assert lm_studio_auth_headers() == {"Authorization": "Bearer first-token"}

    monkeypatch.setenv("LM_STUDIO_TOKEN", "rotated-token")
    assert lm_studio_auth_headers() == {"Authorization": "Bearer rotated-token"}

    monkeypatch.delenv("LM_STUDIO_TOKEN")
    with pytest.raises(RuntimeError, match="LM_STUDIO_TOKEN is required"):
        lm_studio_auth_headers()


@pytest.mark.parametrize(("relative_path", "expected_calls"), _GATEWAY_CALLERS.items())
def test_every_direct_gateway_post_has_auth_and_fixed_ttl(relative_path, expected_calls):
    """AST guard covers every direct POST; no synthetic warm-up is present."""
    tree = ast.parse((_CORE_DIR / relative_path).read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "post"
        and any("chat/completions" in ast.unparse(arg) for arg in node.args)
    ]
    assert len(calls) == expected_calls

    for call in calls:
        keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        assert ast.unparse(keywords["headers"]) == "lm_studio_auth_headers()"

        body = keywords["json"]
        assert isinstance(body, ast.Dict)
        body_items = {
            key.value: value
            for key, value in zip(body.keys, body.values, strict=True)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        assert ast.unparse(body_items["ttl"]) == "LM_TTL_SECONDS"


@pytest.mark.asyncio
async def test_llm_json_calls_are_authenticated_without_secret_logs(monkeypatch, caplog):
    token = "sentinel-token-must-not-be-logged"
    monkeypatch.setenv("LM_STUDIO_TOKEN", token)

    json_client = _client_with('{"mood": "composed"}')
    assert await call_llm("http://gateway", "model", "prompt", client=json_client) == {
        "mood": "composed"
    }
    json_body = _assert_authenticated_call(json_client, token)
    assert json_body["messages"] == [{"role": "user", "content": "prompt"}]
    assert json_body["stream"] is False

    text_client = _client_with("Klukai's unchanged response.")
    assert await call_llm_text(
        "http://gateway", "model", "memory prompt", client=text_client
    ) == "Klukai's unchanged response."
    text_body = _assert_authenticated_call(text_client, token)
    assert text_body["messages"] == [{"role": "user", "content": "memory prompt"}]

    failing_client = MagicMock()
    failing_client.post = AsyncMock(side_effect=RuntimeError(f"transport: {token}"))
    with caplog.at_level(logging.WARNING, logger="app.llm_json"):
        assert await call_llm(
            "http://gateway", "model", "prompt", client=failing_client
        ) == {}
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_affection_classifier_auth_preserves_prompt_and_ttl(monkeypatch):
    from app.affection import AffectionManager

    token = "affection-token"
    monkeypatch.setenv("LM_STUDIO_TOKEN", token)
    client = _client_with('{"type": "compliment", "intensity": 7}')
    manager = AffectionManager()
    manager._http = client

    with patch("app.llm_router.get_lm_gate", return_value=_Gate()):
        assert await manager._classify_interaction("you rock", "hm") == (
            "compliment",
            7,
        )

    body = _assert_authenticated_call(client, token)
    assert "Commander's message: you rock" in body["messages"][0]["content"]
    assert body["stream"] is False


class _RowsConnection:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []
        self.calls = []

    async def execute(self, query, params=None):
        self.calls.append((query, params))
        cursor = MagicMock()
        cursor.fetchall = AsyncMock(return_value=self.rows)
        return cursor


@asynccontextmanager
async def _connection_context(connection):
    yield connection


@pytest.mark.asyncio
async def test_memory_backfill_request_is_authenticated(monkeypatch):
    import app.memory_archive as archive

    token = "archive-token"
    monkeypatch.setenv("LM_STUDIO_TOKEN", token)
    select_conn = _RowsConnection(
        [("mem-1", "rooftop scene", "Quiet Hours", ["rooftop", "night"])]
    )
    update_conn = _RowsConnection()
    client = _client_with("The rooftop was ours that night.")

    with patch(
        "app.memory_archive.get_conn",
        side_effect=lambda: _connection_context(select_conn),
    ), patch(
        "app.memory_archive.get_conn_autocommit",
        side_effect=lambda: _connection_context(update_conn),
    ), patch(
        "app.memory_archive._get_http", return_value=client
    ), patch("app.llm_router.get_lm_gate", return_value=_Gate()):
        assert await archive.backfill_annotations("alice") == {"total": 1, "updated": 1}

    body = _assert_authenticated_call(client, token)
    prompt = body["messages"][0]["content"]
    assert "rooftop scene" in prompt
    assert "Quiet Hours" in prompt


@pytest.mark.asyncio
async def test_standalone_memory_scripts_authenticate_requests(monkeypatch):
    import reannotate_existing
    import seed_memories

    token = "standalone-token"
    monkeypatch.setenv("LM_STUDIO_TOKEN", token)

    seed_client = _client_with("A moment I chose to preserve with the Commander.")
    result = await seed_memories._call_llm(
        seed_client, "model", "unchanged selection prompt"
    )
    assert result == "A moment I chose to preserve with the Commander."
    seed_body = _assert_authenticated_call(seed_client, token)
    assert seed_body["messages"][0]["content"] == "unchanged selection prompt"

    annotation_client = _client_with(
        "I kept the briefing notes because his trust mattered more than I admitted."
    )
    result = await reannotate_existing._call_annotator(
        annotation_client,
        "Commander message",
        "Klukai response",
        "Mission Records",
    )
    assert result is not None
    annotation_body = _assert_authenticated_call(annotation_client, token)
    annotation_prompt = annotation_body["messages"][0]["content"]
    assert "Commander message" in annotation_prompt
    assert "Klukai response" in annotation_prompt
    assert "Mission Records" in annotation_prompt


@pytest.mark.asyncio
async def test_subsystem_health_authenticates_model_probe(monkeypatch):
    from app.routes import register_routes

    token = "health-probe-token"
    monkeypatch.setenv("LM_STUDIO_TOKEN", token)

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "data": [{"id": "model-a"}],
        "devices": [{"name": "RTX 3090", "vram_free": 24_000_000_000}],
    }
    http_client = MagicMock()
    http_client.get = AsyncMock(return_value=response)
    http_context = MagicMock()
    http_context.__aenter__ = AsyncMock(return_value=http_client)
    http_context.__aexit__ = AsyncMock(return_value=False)

    redis_client = MagicMock()
    redis_client.ping = AsyncMock()
    redis_client.aclose = AsyncMock()

    app = FastAPI()
    register_routes(app)
    handler = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/health/subsystems"
    )

    with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), patch(
        "app.db.check_health", new=AsyncMock(return_value={"status": "ok"})
    ), patch("redis.asyncio.from_url", return_value=redis_client), patch(
        "httpx.AsyncClient", return_value=http_context
    ):
        result = await handler(MagicMock())

    assert result["subsystems"]["lm_studio"] == {
        "status": "ok",
        "models_loaded": 1,
        "models": ["model-a"],
    }
    model_probe = next(
        call for call in http_client.get.await_args_list if call.args[0].endswith("/v1/models")
    )
    assert model_probe.kwargs["headers"] == {"Authorization": f"Bearer {token}"}
