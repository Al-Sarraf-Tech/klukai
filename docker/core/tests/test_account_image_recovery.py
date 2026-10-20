"""Explicit account-image recovery against a real worker and disposable Postgres.

The renderer stands in only for the native provider. API recovery, refusal,
and archive retries go through POST /api/generate-image. The chat test enters
through _handle_message, which schedules the real background_image_gen.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, closing, contextmanager
import asyncio
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
from tests.test_routes_core_coverage import (
    _app_with_routes,
    _find_route,
    _mk_aff_state,
    _mk_request,
)


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(output, format="PNG")
    return output.getvalue()


PNG = _png()
TOKEN = "test-private-token-" + "a" * 32
WORKER_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "agy_image_worker.py"


def _worker_module():
    sys.path.insert(0, str(WORKER_SCRIPT.parent))
    spec = importlib.util.spec_from_file_location(
        "agy_image_worker_recovery", WORKER_SCRIPT
    )
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
    entered = threading.Event()
    release = threading.Event()
    release.set()

    def render(value, _references):
        calls.append(value["request_id"])
        entered.set()
        if not release.wait(15):
            raise TimeoutError("renderer gate")
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
        references.write_text(
            json.dumps(
                {
                    "klukai": {"path": str(root / "klukai.png"), "sha256": "a" * 64},
                    "commander": {
                        "path": str(root / "commander.png"),
                        "sha256": "b" * 64,
                    },
                }
            )
        )
        references.chmod(0o600)
        server = module.create_server(
            runtime, state, references, {"jalsarraf", "alice"}, renderer=render
        )
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        thread.start()
        monkeypatch.setenv("KLUKAI_CHAT_IMAGE_PROVIDER", "agy")
        monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", "jalsarraf,alice")
        monkeypatch.setenv("KLUKAI_AGY_SOCKET", str(runtime / "image.sock"))
        monkeypatch.setenv("KLUKAI_AGY_TOKEN_FILE", str(token))
        try:
            yield {
                "calls": calls,
                "state": state,
                "module": module,
                "entered": entered,
                "release": release,
            }
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

    return await handler(
        ImageGenRequest(prompt=prompt, request_id=request_id), _mk_request()
    )


async def _seed(pool, request_id: str, user_id: str, prompt: str, phase: str):
    width, height, couple = _shape(prompt)
    digest = payload_digest(
        canonical_payload(request_id, user_id, prompt, width, height, couple)
    )
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO companion_account_image_requests "
            "(id, user_id, payload_digest, prompt, width, height, include_commander, phase) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (request_id, user_id, digest, prompt, width, height, couple, phase),
        )
    return digest


async def test_archive_retry_returns_the_same_picture_without_another_render(
    account_db, account_worker, api
):
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
        rows = await (
            await conn.execute(
                "SELECT count(*) FROM companion_memories WHERE id = %s",
                (request_id,),
            )
        ).fetchone()
    assert rows == (1,)


async def test_failed_save_is_not_archived_and_retry_reuses_the_cached_picture(
    account_db, account_worker, api, monkeypatch
):
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
        phase = await (
            await conn.execute(
                "SELECT phase, memory_id FROM companion_account_image_requests WHERE id = %s",
                (request_id,),
            )
        ).fetchone()
    assert phase[0] == "archived" and phase[1] == request_id


async def test_unknown_pending_and_refusal_never_render(
    account_db, account_worker, api
):
    handler, users = api
    unknown_prompt = "Draw Klukai beside a radio"
    unknown_id = str(uuid.uuid4())
    await _seed(account_db, unknown_id, "jalsarraf", unknown_prompt, "accepted")
    unknown = await _post(handler, unknown_prompt, unknown_id)
    assert unknown.status_code == 409
    assert unknown.body
    payload = json.loads(unknown.body)
    assert (
        payload["phase"] == "unknown"
        and payload["generated"] is False
        and payload["archived"] is False
    )

    pending_prompt = "Draw Klukai at a workbench"
    pending_id = str(uuid.uuid4())
    digest = await _seed(account_db, pending_id, "jalsarraf", pending_prompt, "pending")
    width, height, couple = _shape(pending_prompt)
    stored = canonical_payload(
        pending_id, "jalsarraf", pending_prompt, width, height, couple
    )
    assert digest == account_worker["module"].request_digest(stored)
    with (
        closing(
            sqlite3.connect(account_worker["state"] / "requests.sqlite3")
        ) as database,
        database,
    ):
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


class _Socket:
    def __init__(self):
        self.accept = AsyncMock()
        self.send_text = AsyncMock()
        self.close = AsyncMock()


@contextmanager
def _chat_turn_patches():
    """Stub the reply pipeline. Leave the image job and the socket manager real."""
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.models import LLMConfig

    async def _stream(_system, _messages, _config):
        yield "Already done."

    router = MagicMock()
    router.needs_agent = AsyncMock(return_value=False)
    router.is_game_active = AsyncMock(return_value=False)
    router.route = AsyncMock(return_value=LLMConfig(provider="anthropic", model="stub"))
    router.stream = _stream

    async def _add_turn(_sid, role, content, state):
        state.turns.append({"role": role, "content": content})
        state.turn_count += 1
        return state

    chat_memory = MagicMock()
    chat_memory.add_turn = AsyncMock(side_effect=_add_turn)
    chat_memory.save_session = AsyncMock()
    chat_memory.recall_for_prompt = AsyncMock(return_value=([], {}, []))
    chat_memory.get_memory_nudge = AsyncMock(return_value=None)
    chat_memory.get_inside_jokes = AsyncMock(return_value=[])

    aff_state = MagicMock()
    aff_state.level = 6
    aff_state.score = 600
    aff_state.first_interaction = None
    aff_state.last_interaction_date = None
    aff_state.consecutive_days = 0
    affection = MagicMock()
    affection.get_state = AsyncMock(return_value=aff_state)

    proactive = MagicMock()
    proactive.mission_active = False
    proactive.mark_user_messaged_today = MagicMock()
    proactive.mark_responded = MagicMock()
    proactive.check_anniversaries = AsyncMock(return_value=[])
    proactive.get_comfort_objects = AsyncMock(return_value=[])
    proactive.record_first = AsyncMock()

    async def _noop(*_args, **_kwargs):
        return None

    with ExitStack() as stack:
        for target, value in (
            ("app.chat_handlers.router", router),
            ("app.chat_handlers.memory", chat_memory),
            ("app.chat_handlers.affection", affection),
            ("app.chat_handlers.proactive", proactive),
            ("app.chat_handlers._store_message", AsyncMock(return_value=True)),
            ("app.chat_handlers.assemble_system_prompt", lambda **_kwargs: "SYS"),
            (
                "app.chat_handlers._hours_since_last_exchange",
                AsyncMock(return_value=None),
            ),
            (
                "app.chat_handlers.get_conn_autocommit",
                MagicMock(side_effect=RuntimeError("skip read receipt")),
            ),
            (
                "app.chat_prompt.wardrobe.ensure_today",
                AsyncMock(side_effect=RuntimeError("skip her day")),
            ),
            (
                "app.chat_prompt.rituals.birthday_prompt_block",
                AsyncMock(return_value=""),
            ),
            ("app.chat_handlers.background_extraction", _noop),
            ("app.chat_handlers.background_compaction", _noop),
            ("app.chat_handlers.background_recall", _noop),
            ("app.night_watch.night_watch_prompt_block", AsyncMock(return_value="")),
            ("app.decisions.resolve_prompt_block", AsyncMock(return_value="")),
            ("app.decisions.her_world_prompt_block", AsyncMock(return_value="")),
            ("app.context.physical.get_state", AsyncMock(return_value=("rested", ""))),
            ("app.tributes.get_crown_jewel", AsyncMock(return_value=None)),
            ("app.helpers.detect_squad_address", lambda _text: None),
            ("app.helpers.detect_jealousy_trigger", lambda _text: None),
            ("app.llm_router.mark_user_active", MagicMock()),
        ):
            stack.enter_context(patch(target, value))
        yield


def _image_payloads(socket: _Socket) -> list[tuple[dict, bytes]]:
    import base64

    found = []
    for call in socket.send_text.await_args_list:
        payload = json.loads(call.args[0])
        if payload.get("type") == "image":
            found.append((payload, base64.b64decode(payload["data"])))
    return found


async def test_chat_picture_survives_disconnect_and_repeats_without_another_render(
    account_db,
    account_worker,
    monkeypatch,
):
    """One explicit sentence keeps its archived bytes when affection and outfit change."""
    from unittest.mock import MagicMock, patch

    from app.account_images import explicit_request_id, fulfill_account_image
    from app.chat_handlers import _handle_message
    from app.context import memory, ws
    from app.image_gen import is_couple_scene, is_landscape
    from app.models import SessionState

    prompt = "Draw Klukai standing in a quiet garden"
    user_id = "alice"
    monkeypatch.setattr(memory, "get_session", AsyncMock(return_value=None))
    monkeypatch.setattr(memory, "record_milestone", AsyncMock())
    width, height = (1216, 832) if is_landscape(prompt) else (832, 1216)
    expected = explicit_request_id(
        user_id, prompt, width, height, is_couple_scene(prompt)
    )
    levels = [2, 8]
    costumes = ["range_kit", "night_ride"]
    composed: list[str] = []

    async def _level(_user_id):
        state = MagicMock()
        state.level = levels.pop(0)
        return state

    async def _costume(_user_id, _level):
        return costumes.pop(0)

    async def _record(user_id, composed_prompt, **kwargs):
        composed.append(composed_prompt)
        return await fulfill_account_image(user_id, composed_prompt, **kwargs)

    socket = _Socket()
    returned = _Socket()
    await ws.connect(socket, user_id)
    account_worker["release"].clear()
    account_worker["entered"].clear()
    session = SessionState(conversation_id="conv-picture")
    task = None
    try:
        with (
            _chat_turn_patches(),
            patch("app.background.affection.get_state", _level),
            patch("app.her_day.render_costume", _costume),
            patch("app.account_images.fulfill_account_image", _record),
        ):
            await _handle_message(prompt, session, user_id)
            await asyncio.wait_for(
                asyncio.to_thread(account_worker["entered"].wait, 10), 12
            )
            durable = [
                item for item in ws._durable_tasks.get(user_id, ()) if not item.done()
            ]
            assert len(durable) == 1
            task = durable[0]
            await ws.disconnect(user_id, socket)
            assert task.cancelled() is False
            assert task.done() is False
            account_worker["release"].set()
            await task
            await ws.connect(returned, user_id)
            await _handle_message(prompt, session, user_id)
            pending = [
                item for item in ws._durable_tasks.get(user_id, ()) if not item.done()
            ]
            if pending:
                await asyncio.gather(*pending)
    finally:
        account_worker["release"].set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if ws.is_connected(user_id):
            await ws.disconnect(user_id)
    assert len(composed) == 2
    assert composed[0] != composed[1]
    # The scene brief sets the place; the affection tags keep only the mood.
    assert "slight smile" in composed[0] and "professional setting" not in composed[0]
    assert "a garden" in composed[0]
    assert "towel around neck" in composed[0]
    assert "fully clothed" in composed[0]
    assert "devoted" in composed[1] and "holding helmet" in composed[1]
    assert "fully clothed" not in composed[1]
    frames = _image_payloads(returned)
    archived = (memory_archive.IMAGES_DIR / f"{expected}.png").read_bytes()
    assert len(frames) == 1
    payload, image = frames[0]
    assert payload["request_id"] == expected
    assert image == archived == PNG
    assert account_worker["calls"] == [expected]
    async with account_db.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT id::text FROM companion_account_image_requests WHERE user_id = %s",
                (user_id,),
            )
        ).fetchall()
    assert rows == [(expected,)]


async def test_busy_claim_and_archive_context_are_durable(account_db):
    """The real SQL claim admits one retry and preserves first-generation data."""
    from app import account_images as journal

    request_id = str(uuid.uuid4())
    original = {"conversation_id": "original", "mood": "composed", "affection_level": 2}
    await journal.insert_request(
        request_id, "alice", "original prompt", 832, 1216, False, "digest", original
    )
    await journal.update_request(request_id, phase="busy")
    assert not await journal.claim_unsubmitted_request(request_id, "mallory")
    claims = await asyncio.gather(
        *(journal.claim_unsubmitted_request(request_id, "alice") for _ in range(8))
    )
    assert claims.count(True) == 1
    row = await journal.fetch_request(request_id)
    assert row["phase"] == "accepted"
    assert row["archive_context"] == original
    await journal.update_request(request_id, phase="archived", memory_id=request_id)
    await journal.update_request(request_id, phase="unknown")
    assert (await journal.fetch_request(request_id))["phase"] == "archived"


def test_chat_patch_setup_failure_restores_earlier_patches(monkeypatch):
    """A failing patch target cannot poison later presence-gap tests."""
    from contextlib import ExitStack
    from unittest.mock import patch

    from app import chat_handlers, chat_prompt

    original_gap = chat_handlers._hours_since_last_exchange
    original_router = chat_handlers.router
    stacks = []

    class ObservedExitStack(ExitStack):
        def __init__(self):
            super().__init__()
            stacks.append(self)

    monkeypatch.delattr(chat_prompt.wardrobe, "ensure_today")
    with patch("contextlib.ExitStack", ObservedExitStack):
        try:
            with pytest.raises(AttributeError), _chat_turn_patches():
                pytest.fail("The deliberately missing patch target must fail")
            assert chat_handlers._hours_since_last_exchange is original_gap
            assert chat_handlers.router is original_router
        finally:
            # Keep the regression isolated even against the broken helper.
            for stack in stacks:
                stack.close()
