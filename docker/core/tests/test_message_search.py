"""Private HTTP contract for original-message search (no model calls)."""

from unittest.mock import AsyncMock
from uuid import UUID

from fastapi import FastAPI
import httpx
import pytest


@pytest.fixture
async def search_client(monkeypatch):
    from app import message_search, routes_message_search

    resolve = AsyncMock(return_value="alice")
    search = AsyncMock(return_value={"messages": [], "next_cursor": None})
    context = AsyncMock(return_value=None)
    monkeypatch.setattr(message_search, "search_messages", search)
    monkeypatch.setattr(message_search, "get_message_context", context)
    app = FastAPI()
    routes_message_search.register_message_search(app, resolve)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, resolve, search, context


async def test_search_registration_integrates_with_extras(monkeypatch):
    from app import message_search, routes_extras

    monkeypatch.setattr(routes_extras, "_get_user_id", AsyncMock(return_value="alice"))
    monkeypatch.setattr(message_search, "search_messages", AsyncMock(return_value={
        "messages": [], "next_cursor": None,
    }))
    app = FastAPI()
    routes_extras.register_extras(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/messages/search", json={"query": "rain"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path,body", [
    ("search", {"query": "rain"}),
    ("context", {"anchor_id": str(UUID(int=1))}),
])
async def test_authentication_rejects_without_reading_messages(search_client, path, body):
    client, resolve, search, context = search_client
    resolve.return_value = None
    response = await client.post(f"/api/messages/{path}", json=body)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert not search.called and not context.called


@pytest.mark.parametrize("patch", [
    {"query": "a"}, {"query": " "}, {"query": "a" * 201},
    {"query": "ab\0cd"}, {"query": 12}, {"query": None},
    {"speaker": "system"}, {"user_id": "bob"},
    {"start": "2026-11-01T00:00:00"}, {"start": "not-a-date"},
    {"start": "2026-11-01"}, {"start": 123},
    {"end": "2026-11-01T00:00:00"},
    {"start": "2026-11-02T00:00:00Z", "end": "2026-11-01T00:00:00Z"},
    {"start": "2026-11-01T00:00:00Z", "end": "2026-11-01T00:00:00Z"},
    {"cursor": {"created_at": "2026-11-01T00:00:00Z", "id": "bad"}},
    {"cursor": {"created_at": "2026-11-01T00:00:00", "id": str(UUID(int=1))}},
    {"cursor": {"created_at": "2026-11-01T00:00:00Z", "id": str(UUID(int=1)), "extra": 1}},
])
async def test_search_validation_is_private_and_no_store(search_client, patch):
    client, _, search, _ = search_client
    response = await client.post("/api/messages/search", json={"query": "rain", **patch})
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": "Invalid request"}
    assert not search.called


@pytest.mark.parametrize("body", [
    {}, {"anchor_id": "bad"},
    {"anchor_id": str(UUID(int=1)), "extra": True},
    {"anchor_id": str(UUID(int=1)), "before": {"created_at": "2026-11-01T00:00:00Z", "id": str(UUID(int=2))},
     "after": {"created_at": "2026-11-01T00:00:00Z", "id": str(UUID(int=3))}},
])
async def test_context_rejects_invalid_or_both_cursors(search_client, body):
    client, _, _, context = search_client
    response = await client.post("/api/messages/context", json=body)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert not context.called


async def test_malformed_json_is_no_store(search_client):
    client, _, _, _ = search_client
    response = await client.post("/api/messages/search", content=b'{"query":', headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": "Invalid request"}


async def test_search_trims_and_binds_authenticated_user(search_client):
    client, _, search, _ = search_client
    search.return_value = {"messages": [{"id": str(UUID(int=1)), "role": "user", "excerpt": "Rainy night", "created_at": "2026-11-01T00:00:00+00:00"}], "next_cursor": None}
    response = await client.post("/api/messages/search", json={
        "query": "  Rainy  ", "speaker": "user", "start": "2026-11-01T02:00:00+02:00",
    })
    assert response.status_code == 200
    assert response.json() == search.return_value
    assert response.headers["cache-control"] == "no-store"
    user_id, request = search.call_args.args
    assert user_id == "alice" and request.query == "Rainy" and request.speaker == "user"
    assert request.start.isoformat() == "2026-11-01T02:00:00+02:00"


async def test_inaccessible_anchor_is_no_store_404(search_client):
    client, _, _, _ = search_client
    response = await client.post("/api/messages/context", json={"anchor_id": str(UUID(int=1))})
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path,body,dependency", [
    ("search", {"query": "private words"}, "search"),
    ("context", {"anchor_id": str(UUID(int=1))}, "context"),
    ("search", {"query": "private words"}, "auth"),
])
async def test_unavailable_is_private_503_not_empty(search_client, caplog, path, body, dependency):
    client, resolve, search, context = search_client
    {"search": search, "context": context, "auth": resolve}[dependency].side_effect = RuntimeError("private words secret message db.internal")
    response = await client.post(f"/api/messages/{path}", json=body)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": "Conversation search unavailable"}
    assert "RuntimeError" in caplog.text
    assert "private words" not in caplog.text and "db.internal" not in caplog.text


@pytest.mark.parametrize("content,match_start,want", [
    ("雨の夜 🌧️ café", 2, "雨の夜 🌧️ café"),
    ("a" * 500 + "RaIn" + "b" * 500, 500, "…" + "a" * 80 + "RaIn" + "b" * 316 + "…"),
    ("İ" * 500 + "東京" + "c" * 500, 500, "…" + "İ" * 80 + "東京" + "c" * 318 + "…"),
])
def test_excerpt_is_an_exact_source_slice_near_match(content, match_start, want):
    from app.message_search import make_excerpt

    assert make_excerpt(content, match_start) == want


async def test_registered_search_validates_private_body():
    from app import routes_extras

    app = FastAPI()
    routes_extras.register_extras(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/messages/search", json={"query": "a"})
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"


async def test_invalid_utf8_json_is_an_invalid_body_not_an_outage(search_client):
    client, _, _, _ = search_client
    response = await client.post("/api/messages/search", content=b'{"query":"\xff"}', headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": "Invalid request"}


async def test_context_success_preserves_original_payload_and_authenticated_scope(search_client):
    client, _, _, context = search_client
    anchor = str(UUID(int=50))
    context.return_value = {"anchor_id": anchor, "messages": [{
        "id": anchor, "role": "assistant", "content": "original\n東京🌧️",
        "created_at": "2026-11-01T00:00:00+00:00",
    }], "older_cursor": None, "newer_cursor": None}
    response = await client.post("/api/messages/context", json={"anchor_id": anchor})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == context.return_value
    user_id, request = context.call_args.args
    assert user_id == "alice" and str(request.anchor_id) == anchor


@pytest.mark.parametrize("path", ["search", "context"])
async def test_method_errors_on_private_routes_are_no_store(search_client, path):
    client, _, _, _ = search_client
    response = await client.get(f"/api/messages/{path}")
    assert response.status_code == 405
    assert response.headers.get("cache-control") == "no-store"
