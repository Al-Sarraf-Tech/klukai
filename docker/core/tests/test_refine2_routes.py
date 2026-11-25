"""HTTP edge leftovers: promise close, audit anchor, message owner, TTS tail."""

from __future__ import annotations

import io
import wave
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI


def _request(token: str | None = "tok") -> MagicMock:
    req = MagicMock()
    req.headers = {"Authorization": f"Bearer {token}"} if token else {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.state = MagicMock()
    req.state.request_id = "rq-refine2"
    return req


def _route(app: FastAPI, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route {method} {path} not found")


def _wav(samples: bytes = b"\x00\x01") -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(8000)
        dst.writeframes(samples)
    return buf.getvalue()


def _chain(n: int, prev: str | None = None, start: int = 0) -> list[dict]:
    from app.audit_chain import compute_row_hash

    rows = []
    for i in range(start, start + n):
        row = {
            "id": i,
            "event_type": "t",
            "user_id": "alice",
            "ip_address": None,
            "request_id": None,
            "metadata": None,
            "created_at": f"2026-06-11T00:00:{i:02d}Z",
        }
        digest = compute_row_hash(
            row_id=row["id"],
            event_type=row["event_type"],
            user_id=row["user_id"],
            ip_address=row["ip_address"],
            request_id=row["request_id"],
            metadata=row["metadata"],
            created_at=row["created_at"],
            prev_hash=prev,
        )
        row["chain_hash"] = digest
        rows.append(row)
        prev = digest
    return rows


def _as_tuple(row: dict) -> tuple:
    return (
        row["id"],
        row["event_type"],
        row["user_id"],
        row["ip_address"],
        row["request_id"],
        row["metadata"],
        row["created_at"],
        row["chain_hash"],
    )


class _AuditConn:
    def __init__(self, newest_first: list[tuple], predecessor: tuple | None):
        self._rows = newest_first
        self._predecessor = predecessor
        self.sql: list[str] = []

    async def execute(self, sql, params=None):
        self.sql.append(sql)
        result = AsyncMock()
        if "WHERE id <" in sql:
            result.fetchone = AsyncMock(return_value=self._predecessor)
        else:
            result.fetchall = AsyncMock(return_value=self._rows)
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _AuditPool:
    def __init__(self, conn: _AuditConn):
        self._conn = conn

    def connection(self):
        return self._conn


def _verify_app():
    from app.routes_extras2 import register_extras2

    app = FastAPI()
    register_extras2(app)
    return app


# ── Promises: confirmed needs a real resolution ────────────────────────────


class TestPromiseResolve:
    def _handler(self):
        from app.routes_extras3 import register_extras3

        app = FastAPI()
        register_extras3(app)
        return _route(app, "/api/promises/{promise_id}/resolve", "POST")

    @pytest.mark.asyncio
    async def test_confirmed_without_a_resolution_is_refused(self):
        handler = self._handler()
        req = _request()
        req.json = AsyncMock(return_value={"sentiment": "confirmed"})
        resolve = AsyncMock(return_value=True)
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.promises.resolve_promise", resolve):
            resp = await handler("p1", req)
        assert resp.status_code == 400
        resolve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_confirmed_mention_does_not_keep_it(self):
        handler = self._handler()
        req = _request()
        req.json = AsyncMock(
            return_value={"sentiment": "confirmed", "response_text": "I mentioned the door"}
        )
        resolve = AsyncMock(return_value=True)
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.promises.resolve_promise", resolve):
            resp = await handler("p1", req)
        assert resp.status_code == 400
        resolve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_confirmed_with_what_he_finished_resolves(self):
        handler = self._handler()
        req = _request()
        req.json = AsyncMock(
            return_value={"sentiment": "confirmed", "response_text": "I finished the door"}
        )
        resolve = AsyncMock(return_value=True)
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.promises.resolve_promise", resolve):
            result = await handler("p1", req)
        assert result == {"resolved": True}
        resolve.assert_awaited_once_with(
            "p1", "confirmed", "I finished the door", user_id="alice"
        )


# ── Audit window must not trust an unverified anchor ──────────────────────


class TestAuditAnchor:
    @pytest.mark.asyncio
    async def test_tampered_anchor_does_not_make_the_window_valid(self):
        chain = _chain(5)
        fetched = [dict(row) for row in chain[1:]]
        fetched[0]["event_type"] = "tampered"
        newest_first = [_as_tuple(row) for row in reversed(fetched)]
        conn = _AuditConn(newest_first, _as_tuple(chain[0]))
        app = _verify_app()
        handler = _route(app, "/api/audit/verify-chain", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="jalsarraf")), \
             patch("app.routes_extras2.is_admin", return_value=True), \
             patch("app.routes_extras2.get_pool", return_value=_AuditPool(conn)):
            result = await handler(_request(), limit=3)
        assert result["valid"] is False
        assert result["break_at_id"] == chain[1]["id"]

    @pytest.mark.asyncio
    async def test_tampered_first_row_is_not_treated_as_a_clean_genesis(self):
        chain = _chain(4)
        fetched = [dict(row) for row in chain]
        fetched[0]["event_type"] = "tampered"
        conn = _AuditConn([_as_tuple(row) for row in reversed(fetched)], None)
        app = _verify_app()
        handler = _route(app, "/api/audit/verify-chain", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="jalsarraf")), \
             patch("app.routes_extras2.is_admin", return_value=True), \
             patch("app.routes_extras2.get_pool", return_value=_AuditPool(conn)):
            result = await handler(_request(), limit=3)
        assert result["valid"] is False
        assert result["break_at_id"] == chain[0]["id"]

    @pytest.mark.asyncio
    async def test_checked_predecessor_still_passes_a_valid_window(self):
        chain = _chain(5)
        fetched = chain[1:]
        conn = _AuditConn(
            [_as_tuple(row) for row in reversed(fetched)], _as_tuple(chain[0])
        )
        app = _verify_app()
        handler = _route(app, "/api/audit/verify-chain", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="jalsarraf")), \
             patch("app.routes_extras2.is_admin", return_value=True), \
             patch("app.routes_extras2.get_pool", return_value=_AuditPool(conn)):
            result = await handler(_request(), limit=3)
        assert result["valid"] is True
        assert result["checked"] == 3
        assert any("WHERE id <" in sql for sql in conn.sql)


# ── A message with no user id is not his ──────────────────────────────────


class TestStoreMessageOwner:
    @pytest.mark.asyncio
    async def test_missing_user_id_does_not_write(self):
        from app.conversation_store import store_message

        def opened():
            raise AssertionError("database opened without a user id")

        with patch("app.db.get_conn", side_effect=opened):
            assert await store_message("conv-1", "user", "hello") is False
            assert await store_message("conv-1", "user", "hello", user_id="  ") is False

    @pytest.mark.asyncio
    async def test_given_user_id_is_the_one_stored(self):
        from app.conversation_store import store_message

        conn = AsyncMock()

        @asynccontextmanager
        async def _ctx():
            yield conn

        with patch("app.db.get_conn", _ctx):
            assert await store_message("conv-1", "user", "hello", user_id="alice") is True
        params = conn.execute.await_args_list[0].args[1]
        assert params[-1] == "alice"
        assert "jalsarraf" not in params


# ── Synchronous TTS keeps the tail, in chunks, and does not call Fish ─────


class TestTtsKeepsTheTail:
    @pytest.mark.asyncio
    async def test_long_line_is_spoken_in_full(self, monkeypatch):
        import httpx
        from app.routes import TTSRequest, register_routes

        monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "true")
        monkeypatch.delenv("VOICE_ASYNC_JOBS", raising=False)
        monkeypatch.delenv("FISH_API_KEY", raising=False)
        app = FastAPI()
        register_routes(app)
        handler = _route(app, "/api/tts", "POST")
        text = ("Alpha sentence here. " * 30) + "TAIL_MUST_REMAIN."
        synth = AsyncMock(return_value=httpx.Response(200, content=_wav()))
        try:
            with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
                 patch("app.voice_language.voice_language_for", new=AsyncMock(return_value="en")), \
                 patch("app.voice_client.post_tts", synth):
                result = await handler(TTSRequest(text=text, language="en"), _request())
        finally:
            await app.state.voice_jobs.close()
        assert "audio" in result
        texts = [call.args[0]["text"] for call in synth.await_args_list]
        assert len(texts) >= 2
        assert all(len(piece) <= 500 for piece in texts)
        body = text.strip()
        cursor = 0
        for piece in texts:
            at = body.find(piece, cursor)
            assert at >= 0
            cursor = at + len(piece)
        assert body[cursor:].strip() == ""
        assert "TAIL_MUST_REMAIN." in texts[-1]
