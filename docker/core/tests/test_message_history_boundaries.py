"""Execute the route's paging SQL against an offline ordered row fixture.

SQLite exercises the actual WHERE/ORDER BY/LIMIT clauses after adapting only
PostgreSQL placeholders and type casts. Canonical UUID strings have the same
order as UUID bytes; UTC ISO timestamps retain all six fractional digits.
This is not a substitute for a real-PostgreSQL integration test.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import sqlite3
from uuid import UUID

from fastapi import FastAPI
import httpx
import pytest

from app import routes_extras


@pytest.fixture
async def ordered_history(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE companion_messages (id TEXT PRIMARY KEY, user_id TEXT, "
        "role TEXT, content TEXT, content_type TEXT, mood TEXT, model TEXT, created_at TEXT)"
    )
    start = datetime(2026, 10, 22, 12, 0, 0, 123456, tzinfo=timezone.utc)

    def insert(number, stamp, user="alice"):
        row_id = str(UUID(int=number))
        db.execute(
            "INSERT INTO companion_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                user,
                "user",
                f"row {number}",
                "text",
                None,
                None,
                stamp.isoformat(timespec="microseconds"),
            ),
        )
        return row_id

    # Insert in non-UUID order, with more than a page sharing one timestamp.
    for n in range(123):
        insert((n * 37) % 123 + 1, start + timedelta(microseconds=n // 60))
    insert(1000, start, user="bob")
    insert(1001, start + timedelta(days=1), user="bob")
    expected = [
        r[0]
        for r in db.execute(
            "SELECT id FROM companion_messages WHERE user_id = 'alice' "
            "ORDER BY created_at, id"
        ).fetchall()
    ]

    class Rows:
        def __init__(self, rows):
            self.rows = rows

        async def fetchall(self):
            return [(*row[:-1], datetime.fromisoformat(row[-1])) for row in self.rows]

    class Connection:
        async def execute(self, sql, params):
            sql = (
                sql.replace("%s", "?")
                .replace("::timestamptz", "")
                .replace("::uuid", "")
            )
            params = list(params)
            if len(params) > 2:
                params[1] = (
                    datetime.fromisoformat(params[1])
                    .astimezone(timezone.utc)
                    .isoformat(timespec="microseconds")
                )
            return Rows(db.execute(sql, params).fetchall())

    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield Connection()

    async def user(_request):
        return "alice"

    monkeypatch.setattr(routes_extras, "_get_user_id", user)
    monkeypatch.setattr(routes_extras, "get_pool", lambda: Pool())
    app = FastAPI()
    routes_extras.register_extras(app)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, expected, insert, start
    finally:
        db.close()


@pytest.mark.parametrize("limit", [1, 2, 49, 50, 200])
async def test_all_pages_retain_ties_without_gaps_duplicates_or_other_users(
    ordered_history, limit
):
    client, expected, _, _ = ordered_history
    params = {"limit": limit}
    collected = []
    for _ in range(len(expected) + 1):
        response = await client.get("/api/messages", params=params)
        assert response.status_code == 200
        page = response.json()["messages"]
        if not page:
            break
        ids = [message["id"] for message in page]
        assert not set(ids).intersection(collected)
        collected = ids + collected
        params.update(before=page[0]["created_at"], before_id=page[0]["id"])
    else:
        pytest.fail("Cursor did not reach the beginning of history")
    assert collected == expected


async def test_repeating_old_cursor_is_stable_when_new_messages_arrive(ordered_history):
    client, _, insert, start = ordered_history
    response = await client.get("/api/messages", params={"limit": 50})
    first = response.json()["messages"]
    params = {
        "limit": 50,
        "before": first[0]["created_at"],
        "before_id": first[0]["id"],
    }
    before_insert = await client.get("/api/messages", params=params)
    insert(2000, start + timedelta(seconds=1))
    after_insert = await client.get("/api/messages", params=params)
    assert before_insert.status_code == after_insert.status_code == 200
    assert after_insert.json() == before_insert.json()
    assert not {row["id"] for row in first}.intersection(
        row["id"] for row in after_insert.json()["messages"]
    )


async def test_offset_cursor_selects_the_same_exact_microsecond_boundary(
    ordered_history,
):
    client, _, _, start = ordered_history
    row_id = str(UUID(int=70))
    offset = timezone(timedelta(hours=5, minutes=30))
    params = {"limit": 50, "before_id": row_id}
    utc = await client.get(
        "/api/messages", params={**params, "before": start.isoformat()}
    )
    local = await client.get(
        "/api/messages",
        params={**params, "before": start.astimezone(offset).isoformat()},
    )
    assert utc.status_code == local.status_code == 200
    assert utc.json()["messages"]
    assert local.json() == utc.json()
