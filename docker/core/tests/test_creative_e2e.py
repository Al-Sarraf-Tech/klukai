"""Offline API → durable claim → provider HTTP → archive end-to-end tests.

Real routes, SQL-producing pipeline, image encoding/files, and quota SQLite are
used. PostgreSQL is replaced by isolated SQLite with only placeholder/cast
translation (not canned SQL results); Redis, session lookup, and notifications
are boundary doubles. This does not establish live provider or PostgreSQL
compatibility. No production startup, service connections, or paid calls run.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import io
import json
from pathlib import Path
import socket
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
from PIL import Image, PngImagePlugin
import pytest


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None

    async def fetchall(self):
        return self.rows


class _OfflineDatabase:
    """Run the real application SQL, retaining its filters and constraints."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.create_function("NOW", 0, lambda: datetime.now(timezone.utc).isoformat())
        migration = Path(__file__).parents[1] / "migrations/170_her_pov_jobs.sql"
        self.conn.executescript(migration.read_text().replace("DEFAULT NOW()", "DEFAULT (NOW())"))
        self.conn.execute(
            "ALTER TABLE companion_her_pov_jobs "
            "ADD COLUMN requested_by_user BOOLEAN NOT NULL DEFAULT FALSE"
        )
        self.conn.executescript("""
            CREATE TABLE companion_messages (
                id TEXT PRIMARY KEY, user_id TEXT, role TEXT, content TEXT,
                mood TEXT, model TEXT, created_at TEXT
            );
            CREATE TABLE companion_memories (
                id TEXT PRIMARY KEY, filename TEXT, thumb_filename TEXT,
                prompt TEXT, annotation TEXT, scene_tags TEXT, mood TEXT,
                affection_level INTEGER, kept BOOLEAN, kept_by TEXT,
                category TEXT, conversation_id TEXT, user_id TEXT,
                created_at TEXT DEFAULT (NOW())
            );
        """)
        self.conn.executemany(
            "INSERT INTO companion_messages VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("turn-user", "alice", "user",
                 "We watched the paper lanterns drift over the quiet lake together.",
                 None, None, "2026-10-14T01:00:00+00:00"),
                ("turn-assistant", "alice", "assistant",
                 "I stayed beside you until the last lantern faded into the clouds.",
                 "tender", "fixture", "2026-10-14T01:00:01+00:00"),
            ],
        )

    async def execute(self, sql, params=()):
        sql = sql.replace("%s", "?").replace("::jsonb", "").replace("::timestamptz", "")
        params = tuple(json.dumps(p) if isinstance(p, (list, dict)) else p for p in params)
        return _Rows(self.conn.execute(sql, params).fetchall())

    @asynccontextmanager
    async def connection(self):
        yield self


class _RedisBoundary:
    def __init__(self):
        self.events = []

    async def publish(self, channel, payload):
        self.events.append((channel, json.loads(payload)))
        return 1


def _png(size, color, metadata=False):
    with Image.new("RGB", size, color) as image:
        output = io.BytesIO()
        info = PngImagePlugin.PngInfo()
        if metadata:
            info.add_text("private-note", "must not leave the reference file")
        image.save(output, format="PNG", pnginfo=info)
        return output.getvalue()


@pytest.fixture
async def creative_app(monkeypatch, tmp_path):
    from app import auth, context, db, events, her_day, image_gen, image_provider
    from app import memory_archive, memory_archive_query, memory_her_pov
    from app.routes import register_routes

    image_dir = tmp_path / "images"
    image_dir.mkdir()
    reference = tmp_path / "fictional-reference.png"
    reference.write_bytes(_png((32, 48), "purple", metadata=True))
    ledger = image_dir / ".creative/attempts.sqlite3"
    environment = {
        "HER_POV_EXECUTION": "queue", "CORE_INTERNAL_TOKEN": "offline-queue-token",
        "KLUKAI_IMAGE_PROVIDER": "openai", "OPENAI_API_KEY": "offline-image-key",
        "KLUKAI_OPENAI_IMAGE_MODEL": "gpt-image-2.5-sunburst-2026-09-08",
        "KLUKAI_OPENAI_IMAGE_QUALITY": "max",
        "KLUKAI_IMAGE_REFERENCE_FILES": json.dumps([str(reference)]),
        "KLUKAI_CREATIVE_TEXT_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "offline-text-key",
        "KLUKAI_CREATIVE_LEDGER": str(ledger),
        "KLUKAI_CLOUD_IMAGE_DAILY_LIMIT": "1", "KLUKAI_CLOUD_IMAGE_MONTHLY_LIMIT": "1",
        "KLUKAI_CLOUD_TEXT_DAILY_LIMIT": "1", "KLUKAI_CLOUD_TEXT_MONTHLY_LIMIT": "1",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    database = _OfflineDatabase(tmp_path / "application.sqlite3")
    for module in (db, memory_archive, memory_archive_query):
        monkeypatch.setattr(module, "get_conn", database.connection)
        monkeypatch.setattr(module, "get_conn_autocommit", database.connection)
    monkeypatch.setattr(db, "get_pool", lambda: database)
    for module in (memory_archive, memory_archive_query):
        monkeypatch.setattr(module, "IMAGES_DIR", image_dir)
    monkeypatch.setattr(memory_her_pov, "_JOBS", {})
    monkeypatch.setattr(memory_her_pov, "_ACTIVE_JOB_BY_USER", {})
    monkeypatch.setattr(memory_her_pov, "_JOBS_LOCK", asyncio.Lock())
    monkeypatch.setattr(image_provider, "_cloud_render_lock", asyncio.Semaphore(1))

    async def session_user(token):
        return {"alice-token": "alice", "bob-token": "bob"}.get(token)

    monkeypatch.setattr(auth, "get_user_from_token", session_user)
    monkeypatch.setattr(context.affection, "get_state", AsyncMock(return_value=SimpleNamespace(level=5)))
    # Daily outfit data is unrelated to this path; keep render_costume itself real.
    monkeypatch.setattr(her_day, "her_now", AsyncMock(return_value=SimpleNamespace(
        outfit=SimpleNamespace(id="default", image_tags="uniform"))))
    notifications = SimpleNamespace(send=AsyncMock(), send_thinking=AsyncMock(), send_proactive=AsyncMock())
    monkeypatch.setattr(context, "ws", notifications)
    redis = _RedisBoundary()
    monkeypatch.setattr(events, "_redis", redis)

    def unavailable(*_args, **_kwargs):
        raise AssertionError("Offline cloud E2E must not reach a socket or Dominus gate")

    monkeypatch.setattr(socket.socket, "connect", unavailable)
    monkeypatch.setattr(image_gen, "_leased", unavailable)
    monkeypatch.setattr(image_gen, "check_comfyui_ready", unavailable)

    state = SimpleNamespace(
        database=database, ledger=ledger, image_dir=image_dir, redis=redis,
        notifications=notifications, text_requests=[], image_requests=[],
        image_started=asyncio.Event(), release_image=asyncio.Event(), image_failure=False,
    )
    state.release_image.set()
    result_png = _png((832, 1216), "teal", metadata=True)

    async def provider(request):
        if str(request.url) == "https://api.anthropic.com/v1/messages":
            body = json.loads(await request.aread())
            state.text_requests.append((request, body))
            return httpx.Response(200, json={
                "id": "msg-offline", "type": "message", "role": "assistant",
                "model": "claude-sonnet-5-5", "stop_reason": "end_turn", "stop_sequence": None,
                "content": [{"type": "text", "text": json.dumps({
                    "annotation": "I remember the lantern you caught before the lake could claim it.",
                    "scene_tags": "lake, paper lanterns, moonlight, gentle smile",
                    "couple": False, "mood": "tender", "title": "The Last Lantern",
                })}], "usage": {"input_tokens": 100, "output_tokens": 70},
            })
        if str(request.url) == "https://api.openai.com/v1/images/edits":
            raw = await request.aread()
            multipart = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + raw)
            fields = {part.get_param("name", header="content-disposition"):
                      part.get_payload(decode=True) for part in multipart.iter_parts()}
            state.image_requests.append((request, fields))
            state.image_started.set()
            await state.release_image.wait()
            if state.image_failure:
                return httpx.Response(503, json={"error": {"message": "offline unknown outcome"}})
            return httpx.Response(200, json={
                "created": 1791936000, "data": [{"b64_json": base64.b64encode(result_png).decode()}],
                "usage": {"input_tokens": 40, "output_tokens": 80, "total_tokens": 120},
            })
        raise AssertionError(f"Unexpected external request: {request.method} {request.url}")

    original_client = httpx.AsyncClient

    def offline_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(provider)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", offline_client)
    app = FastAPI()
    register_routes(app)
    async with original_client(transport=httpx.ASGITransport(app=app), base_url="http://offline-app") as client:
        state.client = client
        try:
            yield state
        finally:
            database.conn.close()


_OWNER = {"Authorization": "Bearer alice-token"}
_OTHER = {"Authorization": "Bearer bob-token"}
_WORKER = {"X-Internal-Token": "offline-queue-token"}


async def _start(state):
    response = await state.client.post("/api/memories/her-pov", headers=_OWNER)
    assert response.status_code == 202
    return response.json()["job_id"]


async def _run(state, job_id):
    return await state.client.post("/internal/jobs/her-pov/run", headers=_WORKER, json={"job_id": job_id})


def _attempts(state):
    with closing(sqlite3.connect(state.ledger)) as conn:
        return dict(conn.execute("SELECT kind, count(*) FROM creative_attempts GROUP BY kind"))


@pytest.mark.asyncio
async def test_queued_cloud_memory_survives_redelivery_and_is_retrievable_only_by_owner(creative_app):
    """Catch duplicate paid dispatch, missing references/storage, local gates, and ownership leaks."""
    state = creative_app
    job_id = await _start(state)
    duplicate = await state.client.post("/api/memories/her-pov", headers=_OWNER)
    assert duplicate.json() == {"job_id": job_id, "status": "queued", "reused": True}
    assert len(state.redis.events) == 1
    assert state.redis.events[0][1]["job_id"] == job_id
    assert not state.text_requests and not state.image_requests

    state.release_image.clear()
    worker = asyncio.create_task(_run(state, job_id))
    try:
        await asyncio.wait_for(state.image_started.wait(), timeout=5)
        in_flight = await state.client.get(f"/api/memories/her-pov/{job_id}", headers=_OWNER)
        assert in_flight.json()["status"] == "drawing"
        redelivered = await _run(state, job_id)
        assert redelivered.json()["ok"] is True
        assert _attempts(state) == {"image": 1, "text": 1}
    finally:
        state.release_image.set()
        await worker

    # Lose the hot cache as on worker restart; status must come from durable SQL.
    from app import memory_her_pov
    memory_her_pov._JOBS.clear()
    memory_her_pov._ACTIVE_JOB_BY_USER.clear()
    status = (await state.client.get(f"/api/memories/her-pov/{job_id}", headers=_OWNER)).json()
    assert status["status"] == "done" and status["has_image"] is True
    assert status["annotation"] == "I remember the lantern you caught before the lake could claim it."
    memory_id = status["memory_id"]
    assert state.database.conn.execute("SELECT attempts FROM companion_her_pov_jobs").fetchone() == (1,)
    assert state.database.conn.execute("SELECT user_id, conversation_id FROM companion_memories").fetchone() == (
        "alice", f"her_pov:{job_id}")

    assert len(state.text_requests) == len(state.image_requests) == 1
    text_request, text = state.text_requests[0]
    assert text_request.headers["x-api-key"] == "offline-text-key"
    assert "authorization" not in text_request.headers
    assert text["model"] == "claude-sonnet-5-5"
    assert text["thinking"] == {"type": "adaptive"}
    assert text["output_config"] == {"effort": "medium"}
    assert "paper lanterns drift" in json.dumps(text["messages"])
    image_request, fields = state.image_requests[0]
    assert image_request.headers["authorization"] == "Bearer offline-image-key"
    assert "x-api-key" not in image_request.headers
    assert fields["model"] == b"gpt-image-2.5-sunburst-2026-09-08"
    assert fields["quality"] == b"max" and fields["n"] == b"1"
    assert fields["size"] == b"832x1216"
    assert b"paper lanterns" in fields["prompt"]
    with Image.open(io.BytesIO(fields["image[]"])) as reference:
        assert reference.size == (32, 48)
        assert reference.getpixel((0, 0)) == (128, 0, 128)
        assert "private-note" not in reference.info
    with Image.open(state.image_dir / f"{memory_id}.png") as original:
        assert original.format == "PNG" and original.size == (832, 1216)
        assert original.getpixel((0, 0)) == (0, 128, 128)
        assert "private-note" not in original.info
    for endpoint, size in (("image", (832, 1216)), ("thumbnail", (320, 467))):
        response = await state.client.get(f"/api/memories/{memory_id}/{endpoint}", headers=_OWNER)
        assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
        with Image.open(io.BytesIO(response.content)) as image:
            assert image.format == "WEBP" and image.size == size
        assert (await state.client.get(f"/api/memories/{memory_id}/{endpoint}", headers=_OTHER)).status_code == 404
        assert (await state.client.get(f"/api/memories/{memory_id}/{endpoint}")).status_code == 401
    assert (await state.client.get(f"/api/memories/her-pov/{job_id}", headers=_OTHER)).status_code == 404
    assert (await _run(state, job_id)).json()["ok"] is True
    assert len(state.text_requests) == len(state.image_requests) == 1
    assert _attempts(state) == {"image": 1, "text": 1}


@pytest.mark.asyncio
async def test_failed_image_attempt_remains_spent_and_new_job_cannot_dispatch(creative_app, monkeypatch):
    """Catch automatic retry/fallback, refunded unknown outcomes, and bypassed persistent caps."""
    state = creative_app
    state.image_failure = True
    first = await _start(state)
    assert (await _run(state, first)).json()["ok"] is True
    status = (await state.client.get(f"/api/memories/her-pov/{first}", headers=_OWNER)).json()
    assert status["status"] == "failed" and status["error"] == "image_failed"
    assert _attempts(state) == {"image": 1, "text": 1}
    assert list(state.image_dir.glob("*.png")) == []
    assert list(state.image_dir.glob("*.webp")) == []
    assert state.database.conn.execute("SELECT count(*) FROM companion_memories").fetchone() == (0,)
    assert (await _run(state, first)).json()["ok"] is True

    # Permit a second text only, leaving the exhausted image cap unchanged.
    monkeypatch.setenv("KLUKAI_CLOUD_TEXT_DAILY_LIMIT", "2")
    monkeypatch.setenv("KLUKAI_CLOUD_TEXT_MONTHLY_LIMIT", "2")
    second = await _start(state)
    assert second != first
    await _run(state, second)
    status = (await state.client.get(f"/api/memories/her-pov/{second}", headers=_OWNER)).json()
    assert status["status"] == "failed"
    assert len(state.image_requests) == 1 and len(state.text_requests) == 2
    assert _attempts(state) == {"image": 1, "text": 2}


@pytest.mark.asyncio
async def test_unauthorized_entrypoints_cannot_enqueue_or_reserve_a_paid_attempt(creative_app):
    """Catch public worker access or unauthenticated paid job creation."""
    state = creative_app
    assert (await state.client.post("/api/memories/her-pov")).status_code == 401
    job_id = await _start(state)
    denied = await state.client.post("/internal/jobs/her-pov/run", json={"job_id": job_id})
    assert denied.status_code == 403
    assert not state.text_requests and not state.image_requests and not state.ledger.exists()
    assert state.database.conn.execute("SELECT status, attempts FROM companion_her_pov_jobs").fetchone() == ("queued", 0)
