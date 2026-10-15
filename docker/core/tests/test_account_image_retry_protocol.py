"""Offline regressions for unaccepted retries and immutable archive provenance."""
from __future__ import annotations

import asyncio
import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import account_images as journal
from app import agy_image_provider as provider
from tests.test_account_image_recovery import PNG, _worker_module


@pytest.fixture
def journal_rows(monkeypatch):
    rows = {}

    async def fetch(request_id):
        return copy.deepcopy(rows.get(request_id))

    async def insert(request_id, user_id, prompt, width, height, include_commander,
                     digest, archive_context=None):
        if request_id in rows:
            raise RuntimeError("duplicate key")
        rows[request_id] = dict(id=request_id, user_id=user_id, prompt=prompt,
                               width=width, height=height, include_commander=include_commander,
                               payload_digest=digest, phase="accepted", memory_id=None,
                               archive_context=copy.deepcopy(archive_context or {}))

    async def update(request_id, *, phase, memory_id=None, failure=None):
        if rows[request_id]["phase"] == "archived" and phase != "archived":
            return
        rows[request_id].update(phase=phase, failure=failure)
        if memory_id:
            rows[request_id]["memory_id"] = memory_id

    async def claim(request_id, user_id):
        row = rows[request_id]
        if row["user_id"] != user_id or row["phase"] != "busy":
            return False
        row["phase"] = "accepted"
        return True

    monkeypatch.setattr(journal, "fetch_request", fetch)
    monkeypatch.setattr(journal, "insert_request", insert)
    monkeypatch.setattr(journal, "update_request", update)
    # Added by the fix; the failing behavior is asserted below, not its existence.
    monkeypatch.setattr(journal, "claim_unsubmitted_request", claim, raising=False)
    return rows


async def fulfill(prompt="first prompt", request_id=None, **kwargs):
    return await journal.fulfill_account_image(
        "alice", prompt, width=832, height=1216, include_commander=False,
        request_id=request_id, **kwargs,
    )


def test_worker_busy_is_definitely_unaccepted_not_pending():
    result = provider._interpret_worker("/generate", 429, b'{"error":"busy"}', "application/json")
    assert result["status"] == "busy"
    assert provider._interpret_worker("/generate", 409, b"{}", "application/json")["status"] == "pending"


async def test_busy_retry_submits_original_payload_once(journal_rows, monkeypatch):
    request_id = str(uuid.uuid4())
    submit = AsyncMock(side_effect=[{"status": "busy", "image": None},
                                   {"status": "completed", "image": b"original image"}])
    recover = AsyncMock(return_value={"status": "unknown", "image": None})
    monkeypatch.setattr(provider, "submit_agy_image", submit)
    monkeypatch.setattr(provider, "recover_agy_image", recover)
    first = await fulfill(request_id=request_id)
    assert first["phase"] == "busy"
    second = await fulfill("changed outfit prompt", request_id=request_id)
    assert second["image"] == b"original image"
    assert submit.await_count == 2
    assert [call.args[0] for call in submit.await_args_list] == ["first prompt", "first prompt"]
    recover.assert_not_awaited()


@pytest.mark.parametrize("phase", ["accepted", "pending", "unknown", "unavailable", "failed"])
async def test_uncertain_or_accepted_request_is_never_resubmitted(journal_rows, monkeypatch, phase):
    request_id = str(uuid.uuid4())
    await journal.insert_request(request_id, "alice", "original", 832, 1216, False, "digest")
    journal_rows[request_id]["phase"] = phase
    submit = AsyncMock()
    recover = AsyncMock(return_value={"status": "unknown", "image": None})
    monkeypatch.setattr(provider, "submit_agy_image", submit)
    monkeypatch.setattr(provider, "recover_agy_image", recover)
    assert (await fulfill("different", request_id=request_id))["phase"] == "unknown"
    submit.assert_not_awaited()
    assert recover.await_args.args[0] == "original"


async def test_overlapping_recovery_does_not_erase_definite_busy_state(journal_rows, monkeypatch):
    request_id = str(uuid.uuid4())
    await journal.insert_request(request_id, "alice", "original", 832, 1216, False, "digest")

    async def stale_recovery(*args, **kwargs):
        # The original submission finishes with 429 while a /recover miss is in flight.
        journal_rows[request_id]["phase"] = "busy"
        return {"status": "unknown", "image": None}

    monkeypatch.setattr(provider, "recover_agy_image", stale_recovery)
    assert (await fulfill(request_id=request_id))["phase"] == "unknown"
    assert journal_rows[request_id]["phase"] == "busy"


async def test_concurrent_busy_retries_claim_only_one_submission(journal_rows, monkeypatch):
    request_id = str(uuid.uuid4())
    await journal.insert_request(request_id, "alice", "original", 832, 1216, False, "digest")
    journal_rows[request_id]["phase"] = "busy"
    entered, release = asyncio.Event(), asyncio.Event()

    async def submit(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"status": "completed", "image": b"png"}

    submit_mock = AsyncMock(side_effect=submit)
    monkeypatch.setattr(provider, "submit_agy_image", submit_mock)
    monkeypatch.setattr(provider, "recover_agy_image", AsyncMock(return_value={"status": "pending", "image": None}))
    first = asyncio.create_task(fulfill(request_id=request_id))
    try:
        await asyncio.wait_for(entered.wait(), .5)
        second = await fulfill(request_id=request_id)
        assert second["phase"] == "pending"
        release.set()
        assert (await first)["image"] == b"png"
        submit_mock.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


async def test_recovery_retains_original_archive_context(journal_rows, monkeypatch):
    request_id = str(uuid.uuid4())
    original = dict(conversation_id="original-conversation", mood="composed", affection_level=2)
    monkeypatch.setattr(provider, "submit_agy_image", AsyncMock(return_value={"status": "completed", "image": b"png"}))
    monkeypatch.setattr(provider, "recover_agy_image", AsyncMock(return_value={"status": "completed", "image": b"png"}))
    first = await fulfill(request_id=request_id, archive_context=original)
    await journal.complete_archive(request_id, None, b"png", "alice")
    second = await fulfill("different prompt", request_id=request_id,
                           archive_context=dict(conversation_id="later", mood="devoted", affection_level=8))
    assert first["archive_metadata"] == second["archive_metadata"] == dict(prompt="first prompt", **original)


async def test_api_archives_recovered_metadata_not_retry_input(monkeypatch):
    from tests.test_routes_core_coverage import _app_with_routes, _find_route, _mk_request
    from app.routes import ImageGenRequest
    from app import routes

    request_id = str(uuid.uuid4())
    archive_metadata = dict(prompt="original prompt", conversation_id="api", mood="composed", affection_level=2)
    monkeypatch.setattr(routes, "_get_user_id", AsyncMock(return_value="alice"))
    monkeypatch.setattr("app.image_provider.selected_provider", lambda: "agy")
    monkeypatch.setattr(routes.affection, "get_state", AsyncMock(return_value=SimpleNamespace(level=8)))
    monkeypatch.setattr(journal, "fulfill_account_image", AsyncMock(return_value={
        "request_id": request_id, "image": b"png", "needs_archive": True,
        "archive_metadata": archive_metadata,
    }))
    monkeypatch.setattr(journal, "complete_archive", AsyncMock(return_value={"request_id": request_id, "image": b"png", "archived": True, "memory_id": request_id}))
    save = AsyncMock(return_value=request_id)
    monkeypatch.setattr(routes.memory_archive, "save_image", save)
    handler = _find_route(_app_with_routes(), "/api/generate-image", "POST")
    await handler(ImageGenRequest(prompt="new prompt", request_id=request_id), _mk_request())
    assert save.await_args.args[1:3] == ("original prompt", "api")
    assert save.await_args.kwargs["affection_level"] == 2


async def test_chat_archives_recovered_metadata_not_current_session(monkeypatch):
    from app import background as bg
    request_id = str(uuid.uuid4())
    metadata = dict(prompt="original rendered prompt", conversation_id="original-conversation",
                    mood="composed", affection_level=2)
    monkeypatch.setattr("app.image_provider.selected_provider", lambda: "agy")
    monkeypatch.setattr(bg.affection, "get_state", AsyncMock(return_value=SimpleNamespace(level=8)))
    monkeypatch.setattr("app.her_day.render_costume", AsyncMock(return_value="night_ride"))
    monkeypatch.setattr(bg.memory, "get_session", AsyncMock(return_value=SimpleNamespace(conversation_id="new-conversation", mood="devoted")))
    monkeypatch.setattr(bg.memory, "record_milestone", AsyncMock())
    monkeypatch.setattr(bg.proactive, "record_first", AsyncMock())
    monkeypatch.setattr(bg.ws, "send", AsyncMock())
    monkeypatch.setattr(bg.ws, "send_thinking", AsyncMock())
    monkeypatch.setattr(bg.ws, "send_proactive", AsyncMock())
    monkeypatch.setattr(journal, "fulfill_account_image", AsyncMock(return_value={
        "request_id": request_id, "image": b"png", "needs_archive": True,
        "archive_metadata": metadata,
    }))
    monkeypatch.setattr(journal, "complete_archive", AsyncMock(return_value={"request_id": request_id, "image": b"png", "archived": True, "memory_id": request_id}))
    save = AsyncMock(return_value=request_id)
    monkeypatch.setattr(bg.memory_archive, "save_image", save)
    await bg.background_image_gen("Draw Klukai in a new outfit", user_id="alice")
    assert save.await_args.args[1:5] == ("original rendered prompt", "original-conversation", "composed", 2)


async def test_actual_busy_worker_can_retry_without_duplicate_render(journal_rows, monkeypatch, tmp_path):
    """Real Worker/SQLite protocol; only its transport and renderer are fake."""
    module = _worker_module()
    calls = []
    worker = module.Worker(tmp_path, "fixture-only", {}, {"alice"},
                           renderer=lambda value, _refs: (calls.append(value.copy()), PNG)[1])
    monkeypatch.setenv("KLUKAI_CHAT_IMAGE_PROVIDER", "agy")
    monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", "alice")

    async def transport(path, payload):
        value = module.validate_request(payload)
        status, body = worker.generate(value) if path == "/generate" else worker.recover(value)
        if isinstance(body, bytes):
            return provider._interpret_worker(path, status, body, "image/png")
        import json
        return provider._interpret_worker(path, status, json.dumps(body or {}).encode(), "application/json")

    monkeypatch.setattr(provider, "_post_worker", transport)
    request_id = str(uuid.uuid4())
    try:
        worker.slot.acquire()
        try:
            rejected = await fulfill(request_id=request_id)
            assert rejected["phase"] == "busy"
            assert calls == []
            assert worker.db.execute("SELECT count(*) FROM requests").fetchone() == (0,)
        finally:
            worker.slot.release()
        recovered = await fulfill("different retry input", request_id=request_id)
        assert recovered["phase"] == "completed"
        assert recovered["archive_metadata"]["prompt"] == "first prompt"
        assert [call["prompt"] for call in calls] == ["first prompt"]
        assert (await fulfill(request_id=request_id))["image"] == recovered["image"]
        assert len(calls) == 1
        assert worker.db.execute("SELECT value FROM worker_state WHERE key = 'rotation_next'").fetchone() == (1,)
    finally:
        worker.close()
