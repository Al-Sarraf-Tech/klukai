"""Correct or forget one relationship fact. Chat rows are a different store.

No database: pure id checks, a scripted fact-store connection, and the routes
against an in-memory store. Album discard is not this path.
"""

from __future__ import annotations

import inspect
import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from app import fact_store
from app.memory import MemoryManager
from app.memory_facts import (
    clean_fact_text,
    relationship_facts_from_rows,
    relationship_storage_key,
)
from app.routes_extras import register_extras


class Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class Conn:
    def __init__(self, rows=()):
        self.calls: list[tuple[str, object]] = []
        self.rows = list(rows)

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return Cursor(self.rows)


class FactMemory:
    """Live and retired fact rows. ``leak`` returns every live row from pattern recall."""

    def __init__(self, rows: dict[str, str], leak: bool = False):
        self.rows = {key: {"value": value, "live": True} for key, value in rows.items()}
        self.messages = [{"id": "c1", "user_id": "alice", "content": "I drink oolong"}]
        self.leak = leak
        self.writes: list[tuple[str, str]] = []

    def _entry(self, key: str, row: dict[str, object]):
        if not row["live"]:
            return None
        return {"key": key, "value": row["value"], "ts": "2026-11-01"}

    async def recall(self, key: str):
        row = self.rows.get(key)
        if row is None:
            return None
        return self._entry(key, row)

    async def recall_pattern(self, pattern: str):
        if self.leak:
            found = []
            for key, row in self.rows.items():
                entry = self._entry(key, row)
                if entry is not None:
                    found.append(entry)
            return found
        parts: list[str] = []
        i = 0
        while i < len(pattern):
            ch = pattern[i]
            if ch == "\\" and i + 1 < len(pattern):
                parts.append(re.escape(pattern[i + 1]))
                i += 2
                continue
            parts.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
            i += 1
        rx = re.compile("^" + "".join(parts) + "$")
        return [
            entry
            for key, row in self.rows.items()
            if rx.match(key) and (entry := self._entry(key, row)) is not None
        ]

    async def store(self, key: str, value: str, ttl_seconds: int | None = None):
        self.writes.append((key, value))
        self.rows[key] = {"value": value, "live": True}

    async def replace_live(self, key: str, value: str) -> bool:
        row = self.rows.get(key)
        if row is None or not row["live"]:
            return False
        self.writes.append((key, value))
        row["value"] = value
        return True

    async def retire(self, key: str) -> bool:
        row = self.rows.get(key)
        if row is None or not row["live"]:
            return False
        row["value"] = ""
        row["live"] = False
        return True


def _patch_store(monkeypatch, memory: FactMemory):
    for name in ("recall", "recall_pattern", "store", "replace_live", "retire"):
        monkeypatch.setattr(fact_store, name, getattr(memory, name))


def _app() -> FastAPI:
    app = FastAPI()
    register_extras(app)
    return app


def _route(app: FastAPI, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _request(body=None, token: str | None = "good"):
    req = MagicMock()
    req.headers = {"Authorization": f"Bearer {token}"} if token else {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.state = MagicMock()
    req.state.request_id = "test-req"
    req.json = AsyncMock(return_value=body)
    return req


def _body(response) -> dict:
    if isinstance(response, dict):
        return response
    import json

    return json.loads(response.body)


SEEDED = {
    "companion:alice:rel:favorite_drink": "oolong",
    "companion:alice:rel:joke:tea": "tea :: the plush",
    "companion:alice:milestone:first": "2026-01-01",
    "companion:bob:rel:callsign": "Wolf",
    "companion:bob:rel:favorite_drink": "coffee",
    "companion:alice2:rel:favorite_drink": "beer",
    "companion:axb:rel:secret": "theirs",
    "companion:a_b:rel:mine": "ours",
}


def test_fact_id_is_his_short_id_only():
    assert relationship_storage_key("alice", "favorite_drink") == (
        "companion:alice:rel:favorite_drink"
    )
    assert relationship_storage_key("alice", "joke:tea") == "companion:alice:rel:joke:tea"
    for fact_id in (
        "companion:bob:rel:callsign",
        "../bob",
        "a/b",
        "a%b",
        "",
        "  drink",
        "x" * 81,
    ):
        assert relationship_storage_key("alice", fact_id) is None
    assert relationship_storage_key("", "favorite_drink") is None
    assert relationship_storage_key("a_b", "mine") == "companion:a_b:rel:mine"
    assert clean_fact_text("  black coffee  ") == "black coffee"
    assert clean_fact_text("   ") is None
    assert clean_fact_text("") is None
    assert clean_fact_text(None) is None
    assert clean_fact_text("bad\x00text") is None
    assert clean_fact_text("y" * 1001) is None


def test_list_rows_drop_another_user_and_non_rel_rows():
    rows = [
        {"key": key, "value": value, "ts": "t"}
        for key, value in SEEDED.items()
    ]
    listed = relationship_facts_from_rows(rows, "alice")
    assert listed == [
        {"id": "favorite_drink", "value": "oolong", "ts": "t"},
        {"id": "joke:tea", "value": "tea :: the plush", "ts": "t"},
    ]
    assert relationship_facts_from_rows(rows, "a_b") == [
        {"id": "mine", "value": "ours", "ts": "t"}
    ]
    assert relationship_facts_from_rows(rows, "%") == []
    assert relationship_facts_from_rows(rows, "alice2") == [
        {"id": "favorite_drink", "value": "beer", "ts": "t"}
    ]


def test_replace_and_retire_touch_only_the_memory_row():
    assert "DELETE" not in inspect.getsource(fact_store.replace_live)
    assert "companion_messages" not in inspect.getsource(fact_store.replace_live)
    retire_sql = inspect.getsource(fact_store.retire)
    assert "DELETE" not in retire_sql
    assert "companion_messages" not in retire_sql
    assert "UPDATE memory" in retire_sql
    assert "value=''" in retire_sql


async def test_replace_live_does_not_insert_and_retire_hides_the_row(monkeypatch):
    monkeypatch.setattr(fact_store, "_ready", True)
    conn = Conn([("k",)])

    @asynccontextmanager
    async def connect():
        yield conn

    monkeypatch.setattr(fact_store, "get_conn_autocommit", connect)
    assert await fact_store.replace_live(" k ", "black coffee") is True
    sql, params = conn.calls[0]
    assert sql.startswith("UPDATE memory")
    assert "INSERT" not in sql and "DELETE" not in sql
    assert "expires_at IS NULL OR expires_at > NOW()" in sql
    assert params == ("black coffee", "k")

    conn.rows = []
    assert await fact_store.replace_live("missing", "x") is False
    assert await fact_store.retire("gone") is False
    conn.rows = [("gone",)]
    assert await fact_store.retire(" gone ") is True
    retire_sql, retire_params = conn.calls[-1]
    assert retire_sql.startswith("UPDATE memory")
    assert "DELETE" not in retire_sql
    assert "companion_messages" not in retire_sql
    assert retire_params == ("gone",)
    with pytest.raises(ValueError):
        await fact_store.replace_live("  ", "x")
        await fact_store.retire("  ")


async def test_routes_list_correct_and_forget_his_fact_only(monkeypatch):
    store = FactMemory(SEEDED, leak=True)
    _patch_store(monkeypatch, store)
    kept = list(store.messages)
    app = _app()
    list_facts = _route(app, "/api/facts", "GET")
    correct = _route(app, "/api/facts/{fact_id}/correction", "POST")
    forget = _route(app, "/api/facts/{fact_id}/forget", "POST")

    with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
        denied = await list_facts(_request(token=None))
    assert denied.status_code == 401

    with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")):
        listed = await list_facts(_request())
    assert _body(listed) == {
        "facts": [
            {"id": "favorite_drink", "value": "oolong", "ts": "2026-11-01"},
            {"id": "joke:tea", "value": "tea :: the plush", "ts": "2026-11-01"},
        ]
    }
    dossier = await MemoryManager().get_relationship_facts("alice")
    assert dossier == {"favorite_drink": "oolong"}
    assert "Wolf" not in str(dossier)
    assert await MemoryManager().get_relationship_facts("a_b") == {"mine": "ours"}

    with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")):
        blank = await correct("favorite_drink", _request({"value": "  "}))
        stolen = await correct(
            "companion:bob:rel:callsign", _request({"value": "stolen"})
        )
        missing = await correct("callsign", _request({"value": "Wolf"}))
        updated = await correct(
            "favorite_drink", _request({"value": " black coffee "})
        )
    assert blank.status_code == 400
    assert stolen.status_code == 404
    assert missing.status_code == 404
    assert updated == {"id": "favorite_drink", "value": "black coffee"}
    assert store.rows["companion:bob:rel:callsign"]["value"] == "Wolf"
    assert store.rows["companion:bob:rel:favorite_drink"]["value"] == "coffee"
    recalled = await MemoryManager().get_relationship_facts("alice")
    assert recalled == {"favorite_drink": "black coffee"}
    assert "oolong" not in recalled.values()

    with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")):
        gone = await forget("favorite_drink", _request())
        again = await forget("favorite_drink", _request())
        not_his = await forget("callsign", _request())
    assert gone == {"id": "favorite_drink", "forgotten": True}
    assert again.status_code == 404
    assert not_his.status_code == 404
    assert store.messages == kept
    assert store.rows["companion:bob:rel:callsign"]["value"] == "Wolf"
    assert store.rows["companion:alice:rel:favorite_drink"]["live"] is False
    assert await MemoryManager().recall_fact("rel:favorite_drink", "alice") is None
    left = await MemoryManager().list_relationship_facts("alice")
    assert [fact["id"] for fact in left] == ["joke:tea"]
    assert "oolong" not in {fact["value"] for fact in left}
    assert "black coffee" not in {fact["value"] for fact in left}

    await MemoryManager().store_fact("rel:favorite_drink", "oolong", user_id="alice")
    await MemoryManager().set_relationship_fact(
        "favorite_drink", "oolong again", user_id="alice"
    )
    assert await MemoryManager().recall_fact("rel:favorite_drink", "alice") is None
    assert store.rows["companion:alice:milestone:first"]["value"] == "2026-01-01"
    assert store.messages == kept


async def test_list_outage_is_not_an_empty_fact_list(monkeypatch):
    async def down(_pattern):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(fact_store, "recall_pattern", down)
    app = _app()
    list_facts = _route(app, "/api/facts", "GET")
    with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")):
        response = await list_facts(_request())
    assert response.status_code == 503
    assert _body(response) != {"facts": []}


async def test_forgotten_check_blip_still_attempts_the_write(monkeypatch):
    async def down(_key):
        raise RuntimeError("postgres down")

    written: list[tuple[str, str]] = []

    async def store(key, value, ttl_seconds=None):
        written.append((key, value))

    monkeypatch.setattr(fact_store, "recall", down)
    monkeypatch.setattr(fact_store, "store", store)
    await MemoryManager().store_fact("rel:favorite_drink", "tea", user_id="alice")
    await MemoryManager().store_fact("costume", "speed_star", user_id="alice")
    assert written == [
        ("companion:alice:rel:favorite_drink", "tea"),
        ("companion:alice:costume", "speed_star"),
    ]


async def test_costume_route_does_not_succeed_when_set_requested_fails():
    from datetime import date

    from app.routes import CostumeRequest
    from app.wardrobe import HerDayRow

    app = _app()
    handler = _route(app, "/api/costume", "POST")
    store_fact = AsyncMock()
    row = HerDayRow(date(2026, 10, 4), "speed_star", "A duty day.")
    now = MagicMock()
    now.outfit.id = "blazing_star"
    now.hour = 15
    now.override = None
    aff = MagicMock()
    aff.level = 5
    with (
        patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")),
        patch("app.routes_extras.affection.get_state", new=AsyncMock(return_value=aff)),
        patch("app.her_day.her_now", new=AsyncMock(return_value=now)),
        patch("app.her_day.active_mission", return_value=None),
        patch("app.wardrobe.ensure_today", new=AsyncMock(return_value=row)),
        patch("app.wardrobe.occasions_for", new=AsyncMock(return_value=frozenset())),
        patch(
            "app.wardrobe.set_requested",
            new=AsyncMock(side_effect=RuntimeError("down")),
        ),
        patch("app.routes_extras.memory.store_fact", store_fact),
        patch("app.audit.log", new=AsyncMock()),
    ):
        response = await handler(CostumeRequest(costume="speed_star"), _request())
    assert response.status_code == 503
    assert _body(response)["error"] == "Could not set today's outfit"
    store_fact.assert_not_awaited()
