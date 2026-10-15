"""Explicit account-image recovery against a real worker and disposable Postgres.

The renderer stands in only for the native provider. Recovery, refusal,
and archive retries go through POST /api/generate-image.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import uuid
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from app import db, memory_archive
from app.account_images import canonical_payload, payload_digest
from app.image_gen import is_couple_scene, is_landscape
from app.migrations import apply_migrations
from tests.test_migrations_postgres import MIGRATIONS, migration_pool  # noqa: F401
from tests.test_routes_core_coverage import _app_with_routes, _find_route, _mk_aff_state, _mk_request


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(output, format="PNG")
    return output.getvalue()


PNG = _png()
TOKEN = "test-private-token-" + "a" * 32
WORKER_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "agy_image_worker.py"


def _worker_module():
    sys.path.insert(0, str(WORKER_SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("agy_image_worker_recovery", WORKER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _shape(prompt: str) -> tuple[int, int, bool]:
    width, height = (1216, 832) if is_landscape(prompt) else (832, 1216)
    return width, height, is_couple_scene(prompt)


@pytest.fixture
async def account_db(migration_pool, monkeypatch, tmp_path):  # noqa: F811
    await apply_migrations(migration_pool, MIGRATIONS)
    images = tmp_path / "images"
    images.mkdir()

    @asynccontextmanager
    async def connection():
        async with migration_pool.connection() as conn:
            yield conn

    monkeypatch.setattr(db, "get_conn_autocommit", connection)
    monkeypatch.setattr(db, "get_pool", lambda: migration_pool)
    monkeypatch.setattr(memory_archive, "get_conn_autocommit", connection)
    monkeypatch.setattr(memory_archive, "IMAGES_DIR", images)
    return migration_pool


@pytest.fixture
def account_worker(monkeypatch):
    module = _worker_module()
    calls: list[str] = []

    def render(value, _references):
        calls.append(value["request_id"])
        return PNG

    with tempfile.TemporaryDirectory(prefix="agyimg-") as directory:
        root = Path(directory)
        runtime = root / "run"
        state = root / "state"
        runtime.mkdir(mode=0o700)
        token = runtime / "token"
        token.write_text(TOKEN + "\n")
        token.chmod(0o600)
        references = root / "references.json"
        references.write_text(json.dumps({
            "klukai": {"path": str(root / "klukai.png"), "sha256": "a" * 64},
            "commander": {"path": str(root / "commander.png"), "sha256": "b" * 64},
        }))
        references.chmod(0o600)
        server = module.create_server(runtime, state, references, {"jalsarraf", "alice"}, renderer=render)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        monkeypatch.setenv("KLUKAI_CHAT_IMAGE_PROVIDER", "agy")
        monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", "jalsarraf,alice")
        monkeypatch.setenv("KLUKAI_AGY_SOCKET", str(runtime / "image.sock"))
        monkeypatch.setenv("KLUKAI_AGY_TOKEN_FILE", str(token))
        try:
            yield {"calls": calls, "state": state, "module": module}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


@pytest.fixture
def api(monkeypatch):
    users = {"id": "jalsarraf"}

    async def current_user(_request):
        return users["id"]

    monkeypatch.setattr("app.routes._get_user_id", current_user)
    monkeypatch.setattr(
        "app.routes.affection.get_state",
        AsyncMock(return_value=_mk_aff_state(level=6)),
    )
    handler = _find_route(_app_with_routes(), "/api/generate-image", "POST")
    return handler, users


async def _post(handler, prompt: str, request_id: str | None = None):
    from app.routes import ImageGenRequest

    return await handler(ImageGenRequest(prompt=prompt, request_id=request_id), _mk_request())


async def _seed(pool, request_id: str, user_id: str, prompt: str, phase: str):
    width, height, couple = _shape(prompt)
    digest = payload_digest(canonical_payload(request_id, user_id, prompt, width, height, couple))
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO companion_account_image_requests "
            "(id, user_id, payload_digest, prompt, width, height, include_commander, phase) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (request_id, user_id, digest, prompt, width, height, couple, phase),
        )
    return digest


async def test_archive_retry_returns_the_same_picture_without_another_render(account_db, account_worker, api):
    handler, _users = api
    prompt = "Draw Klukai standing in a quiet garden"
    request_id = str(uuid.uuid4())
    first = await _post(handler, prompt, request_id)
    assert first["archived"] is True
    assert first["generated"] is True
    assert first["memory_id"] == request_id
    assert first["request_id"] == request_id
    saved = Image.open(memory_archive.IMAGES_DIR / f"{request_id}.png")
    saved.load()
    assert saved.format == "PNG"
    saved.close()

    again = await _post(handler, prompt, request_id)
    assert again["archived"] is True
    assert again["memory_id"] == request_id
    assert again["image"] == first["image"]
    assert account_worker["calls"] == [request_id]
    async with account_db.connection() as conn:
        rows = await (await conn.execute(
            "SELECT count(*) FROM companion_memories WHERE id = %s",
            (request_id,),
        )).fetchone()
    assert rows == (1,)


async def test_failed_save_is_not_archived_and_retry_reuses_the_cached_picture(account_db, account_worker, api, monkeypatch):
    handler, _users = api
    prompt = "Draw Klukai holding a sealed letter"
    request_id = str(uuid.uuid4())
    locked = memory_archive.IMAGES_DIR
    locked.chmod(0o500)
    try:
        failed = await _post(handler, prompt, request_id)
    finally:
        locked.chmod(0o700)
    assert failed["generated"] is True
    assert failed["archived"] is False
    assert "memory_id" not in failed
    assert account_worker["calls"] == [request_id]

    recovered = await _post(handler, prompt, request_id)
    assert recovered["archived"] is True
    assert recovered["memory_id"] == request_id
    assert account_worker["calls"] == [request_id]
    async with account_db.connection() as conn:
        phase = await (await conn.execute(
            "SELECT phase, memory_id FROM companion_account_image_requests WHERE id = %s",
            (request_id,),
        )).fetchone()
    assert phase[0] == "archived" and phase[1] == request_id


async def test_unknown_pending_and_refusal_never_render(account_db, account_worker, api):
    handler, users = api
    unknown_prompt = "Draw Klukai beside a radio"
    unknown_id = str(uuid.uuid4())
    await _seed(account_db, unknown_id, "jalsarraf", unknown_prompt, "accepted")
    unknown = await _post(handler, unknown_prompt, unknown_id)
    assert unknown.status_code == 409
    assert unknown.body
    payload = json.loads(unknown.body)
    assert payload["phase"] == "unknown" and payload["generated"] is False and payload["archived"] is False

    pending_prompt = "Draw Klukai at a workbench"
    pending_id = str(uuid.uuid4())
    digest = await _seed(account_db, pending_id, "jalsarraf", pending_prompt, "pending")
    width, height, couple = _shape(pending_prompt)
    stored = canonical_payload(pending_id, "jalsarraf", pending_prompt, width, height, couple)
    assert digest == account_worker["module"].request_digest(stored)
    with sqlite3.connect(account_worker["state"] / "requests.sqlite3") as database:
        database.execute(
            "INSERT INTO requests (request_id, request_hash, status) VALUES (?, ?, 'pending')",
            (pending_id, digest),
        )
    pending = await _post(handler, pending_prompt, pending_id)
    assert pending.status_code == 409
    assert json.loads(pending.body)["phase"] == "pending"

    users["id"] = "alice"
    refused_prompt = "Draw us together in a quiet room"
    refused_id = str(uuid.uuid4())
    refused = await _post(handler, refused_prompt, refused_id)
    assert refused.status_code == 409
    assert json.loads(refused.body)["phase"] == "refused"
    repeated = await _post(handler, refused_prompt, refused_id)
    assert repeated.status_code == 409
    assert json.loads(repeated.body)["phase"] == "refused"
    assert account_worker["calls"] == []
