"""History cursors must preserve ties and reject invalid input before SQL."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app import routes_extras


@pytest.fixture
async def history(monkeypatch):
    statements = []

    class Connection:
        async def execute(self, sql, params):
            statements.append((sql, params))
            return type("Rows", (), {"fetchall": AsyncMock(return_value=[])})()

    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield Connection()

    monkeypatch.setattr(routes_extras, "_get_user_id", AsyncMock(return_value="alice"))
    monkeypatch.setattr(routes_extras, "get_pool", lambda: Pool())
    app = FastAPI()
    routes_extras.register_extras(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, statements


async def test_cursor_includes_id_and_orders_ties(history):
    client, statements = history
    cursor = "00000000-0000-0000-0000-000000000002"
    response = await client.get(
        "/api/messages",
        params={"before": "2026-10-21T12:00:00.123456Z", "before_id": cursor},
    )
    assert response.status_code == 200
    sql, params = statements[0]
    assert "(created_at, id) < (%s::timestamptz, %s::uuid)" in sql
    assert "ORDER BY created_at DESC, id DESC" in sql
    assert params == ("alice", "2026-10-21T12:00:00.123456Z", cursor, 50)


@pytest.mark.parametrize(
    "params",
    [
        {"before": "not-a-date"},
        {"before": "2026-10-21T12:00:00Z", "before_id": "bad-id"},
        {"before_id": "00000000-0000-0000-0000-000000000002"},
    ],
)
async def test_bad_cursor_is_client_error_without_query(history, params):
    client, statements = history
    response = await client.get("/api/messages", params=params)
    assert response.status_code == 400
    assert statements == []


@pytest.mark.parametrize("requested,expected", [(-1, 1), (0, 1), (10_000_000, 200)])
async def test_history_page_is_bounded(history, requested, expected):
    client, statements = history
    response = await client.get("/api/messages", params={"limit": requested})
    assert response.status_code == 200
    assert statements[0][1][-1] == expected
