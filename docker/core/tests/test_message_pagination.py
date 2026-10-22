"""History cursors must preserve ties and reject invalid input before SQL."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
    assert params == ("alice", "2026-10-21T12:00:00.123456+00:00", cursor, 50)


@pytest.mark.parametrize(
    "params",
    [
        {"before": "not-a-date"},
        {"before": ""},
        {"before": "2026-10-21T12:00:00Z", "before_id": "bad-id"},
        {"before_id": "00000000-0000-0000-0000-000000000002"},
        {"before": "0001-01-01T00:00:00+23:59"},
        {"before": "9999-12-31T23:59:59-23:59"},
    ],
)
async def test_bad_cursor_is_client_error_without_query(history, params):
    client, statements = history
    response = await client.get("/api/messages", params=params)
    assert response.status_code == 400
    assert statements == []


@pytest.mark.parametrize(
    "prefix", ["user", "streaming", "error", "proactive", "voice-letter", "image"]
)
async def test_synthetic_client_ids_are_not_database_cursors(history, prefix):
    client, statements = history
    response = await client.get(
        "/api/messages",
        params={"before": "2026-10-22T12:00:00Z", "before_id": f"{prefix}-123"},
    )
    assert response.status_code == 400
    assert statements == []


@pytest.mark.parametrize("requested,expected", [(-1, 1), (0, 1), (10_000_000, 200)])
async def test_history_page_is_bounded(history, requested, expected):
    client, statements = history
    response = await client.get("/api/messages", params={"limit": requested})
    assert response.status_code == 200
    assert statements[0][1][-1] == expected


async def test_nul_timestamp_is_client_error_before_psycopg_adaptation(
    history, monkeypatch
):
    """fromisoformat accepts NUL as a separator; PostgreSQL text cannot."""
    string_types = pytest.importorskip("psycopg.types.string")
    attempted = []

    class Connection:
        async def execute(self, sql, params):
            attempted.append(params)
            for value in params:
                if isinstance(value, str):
                    string_types.StrDumper(str).dump(value)
            return type("Rows", (), {"fetchall": AsyncMock(return_value=[])})()

    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield Connection()

    monkeypatch.setattr(routes_extras, "get_pool", lambda: Pool())
    client, _ = history
    response = await client.get(
        "/api/messages", params={"before": "2026-10-22\x0012:00:00Z"}
    )
    assert response.status_code == 400
    assert attempted == []


@pytest.mark.parametrize(
    "cursor",
    [
        "urn:uuid:00000000-0000-0000-0000-000000000002",
        "0-0000000000000000000000000000002",
        "{00000000-0000-0000-0000-00000000000A}",
    ],
)
async def test_validated_uuid_is_normalized_before_sql(history, cursor):
    """Python accepts more UUID spellings than PostgreSQL's text parser."""
    from uuid import UUID

    client, statements = history
    response = await client.get(
        "/api/messages",
        params={"before": "2026-10-22T12:00:00Z", "before_id": cursor},
    )
    assert response.status_code == 200
    assert statements[0][1][2] == str(UUID(cursor))


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-W43-4T12:00:00.123456Z",
        "2026-10-22\N{WOLF FACE}12:00:00.123456Z",
        "2026-10-22T12:00:00.123456+23:59",
        "2026-10-22T12:00:00.123456+05:30:15.5",
    ],
)
async def test_validated_timestamp_is_normalized_before_sql(history, stamp):
    """Do not pass Python-only date spellings to a different SQL parser."""
    client, statements = history
    response = await client.get("/api/messages", params={"before": stamp})
    assert response.status_code == 200
    expected = datetime.fromisoformat(stamp).astimezone(timezone.utc).isoformat()
    assert statements[0][1][1] == expected


async def test_naive_legacy_cursor_keeps_database_timezone_semantics(history):
    client, statements = history
    response = await client.get(
        "/api/messages", params={"before": "2026-10-22T12:00:00.123456"}
    )
    assert response.status_code == 200
    assert statements[0][1][1] == "2026-10-22T12:00:00.123456"
