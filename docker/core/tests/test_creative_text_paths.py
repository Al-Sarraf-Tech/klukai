"""Only opted-in dream and memory writers leave the local LLM path."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import seed_memories
from .test_reflection_on_return import _FakePool, _mk_affection
from .test_memory_her_pov import _RailConn, _patch_rail


@pytest.fixture
def writer(monkeypatch):
    from app import creative_text
    monkeypatch.setenv("KLUKAI_CREATIVE_TEXT_PROVIDER", "anthropic")
    call = AsyncMock(return_value="I dreamed of the hangar in the rain.")
    monkeypatch.setattr(creative_text, "complete_creative_text", call)
    return call


@pytest.mark.asyncio
async def test_night_dream_uses_cloud_without_acquiring_local_gate(writer):
    from app.proactive import ProactiveEngine
    engine = ProactiveEngine()
    engine._affection_level = 5
    engine._on_message_callback = AsyncMock()
    with patch("app.proactive.events.random.random", return_value=0.2), \
         patch("app.memory_archive.list_memories", AsyncMock(return_value=[])), \
         patch("app.llm_router.get_lm_gate", side_effect=AssertionError("local gate used")):
        await engine._dream_event()
    engine._on_message_callback.assert_awaited_once_with("I dreamed of the hangar in the rain.")


@pytest.mark.asyncio
async def test_cloud_dream_failure_does_not_try_local_writer(writer):
    from app.proactive import ProactiveEngine
    from app.creative_text import CreativeTextError
    writer.side_effect = CreativeTextError("refused")
    engine = ProactiveEngine()
    engine._affection_level = 5
    engine._on_message_callback = AsyncMock()
    local = AsyncMock()
    with patch("app.proactive.events.random.random", return_value=0.2), \
         patch("app.memory_archive.list_memories", AsyncMock(return_value=[])), \
         patch("app.llm_json.call_llm_text", local):
        await engine._dream_event()
    local.assert_not_awaited()
    engine._on_message_callback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("hour,cloud", [(8, True), (15, False)])
async def test_only_return_dream_moves_to_cloud(writer, hour, cloud):
    from app.reflect_helpers import _maybe_reflect_on_return
    last = datetime.now(timezone.utc) - timedelta(hours=12)
    pool = _FakePool([(last,), [("user", "We talked about rain.")], ("tender",)])
    router = MagicMock()
    router.complete_local = AsyncMock(return_value={
        "choices": [{"message": {"content": "Welcome back to the hangar, Commander."}}]
    })
    ws = MagicMock()
    ws.is_connected.return_value = False
    aff = MagicMock()
    aff.get_state = AsyncMock(return_value=_mk_affection())
    saved = AsyncMock()
    with patch("app.db.get_pool", return_value=pool), \
         patch("app.reflect_helpers.router", router), \
         patch("app.reflect_helpers.ws", ws), \
         patch("app.reflect_helpers.affection", aff), \
         patch("app.reflect_helpers.now_local", return_value=datetime(2026, 10, 14, hour)), \
         patch("app.personality.load_personality", return_value={}), \
         patch("app.personality.build_character_preamble", return_value="You are Klukai."), \
         patch("app.dreams.save_dream", saved), \
         patch("asyncio.sleep", AsyncMock()):
        await _maybe_reflect_on_return("alice")
    assert writer.await_count == int(cloud)
    assert router.complete_local.await_count == int(not cloud)
    if cloud:
        assert saved.await_args.kwargs["dream_text"] == "I dreamed of the hangar in the rain."


@pytest.mark.asyncio
async def test_pov_cloud_json_preserves_scene_without_local_gate(writer):
    from app.memory_her_pov import compose_pov
    writer.return_value = json.dumps({
        "annotation": "I kept the rain between us.", "scene_tags": "hangar, rain, silver hair",
        "couple": True, "mood": "tender", "title": "Rain at Home",
    })
    with patch("app.llm_router.get_lm_gate", side_effect=AssertionError("local gate used")):
        result = await compose_pov({"user_content": "Rain", "assistant_content": "Home"}, 5)
    assert result["annotation"] == "I kept the rain between us."
    assert result["scene_tags"] == "hangar, rain, silver hair"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["not JSON", "[]", '{"annotation":"x"}'])
async def test_bad_cloud_pov_does_not_substitute_generic_scene(writer, value):
    from app.memory_her_pov import compose_pov
    from app.creative_text import CreativeTextError
    writer.return_value = value
    with pytest.raises(CreativeTextError):
        await compose_pov({"user_content": "Rain", "assistant_content": "Home"}, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("text_remote,image_remote", [(True, True), (True, False), (False, True)])
async def test_scheduled_remote_seed_fails_closed_before_db_or_paid_work(writer, monkeypatch, caplog, text_remote, image_remote):
    from app import image_provider
    monkeypatch.setenv("KLUKAI_CREATIVE_TEXT_PROVIDER", "anthropic" if text_remote else "local")
    monkeypatch.setattr(image_provider, "uses_cloud_images", lambda: image_remote)
    connect = AsyncMock(side_effect=AssertionError("archive history must not be loaded"))
    monkeypatch.setattr(seed_memories.psycopg.AsyncConnection, "connect", connect)
    result = await seed_memories.main()
    assert result == 1
    connect.assert_not_awaited()
    writer.assert_not_awaited()
    assert "durable source-aware jobs" in caplog.text


@pytest.mark.asyncio
async def test_cloud_pov_redelivery_without_durable_claim_does_not_redraw(writer, monkeypatch):
    from app import memory_her_pov as hp
    job_id = "e5628645-bcff-48c2-a8e3-ae016a8d8471"
    monkeypatch.setattr(hp, "_JOBS", {job_id: {"user_id": "alice", "status": "drawing"}})
    monkeypatch.setattr(hp, "_ACTIVE_JOB_BY_USER", {"alice": job_id})
    run = AsyncMock()
    monkeypatch.setattr(hp, "run_her_pov", run)
    with patch("app.db.get_conn_autocommit", side_effect=RuntimeError("database offline")):
        result = await hp.run_job_from_queue(job_id)
    assert result is False
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_pov_persistence_failure_stops_pipeline(writer):
    from app import memory_her_pov as hp
    with patch("app.db.get_pool", side_effect=RuntimeError("database offline")):
        with pytest.raises(RuntimeError, match="durable"):
            await hp._persist_job("job", {"status": "thinking"})


@pytest.mark.asyncio
async def test_cloud_pov_insert_failure_stops_pipeline(writer):
    from app import memory_her_pov as hp
    with patch("app.db.get_pool", MagicMock()), \
         patch("app.db.get_conn_autocommit", side_effect=RuntimeError("database offline")):
        with pytest.raises(RuntimeError, match="durable"):
            await hp._persist_job("job", {"status": "thinking"})


@pytest.mark.asyncio
async def test_cloud_pov_requires_queue_mode(writer, monkeypatch):
    from app import memory_her_pov as hp
    monkeypatch.setenv("HER_POV_EXECUTION", "inline")
    with pytest.raises(RuntimeError, match="durable"):
        await hp.start_her_pov("alice")


@pytest.mark.asyncio
async def test_cloud_pov_missed_queue_handoff_is_failed_without_inline_work(writer, monkeypatch):
    from app import memory_her_pov as hp
    monkeypatch.setenv("HER_POV_EXECUTION", "queue")
    monkeypatch.setattr(hp, "_JOBS", {})
    monkeypatch.setattr(hp, "_ACTIVE_JOB_BY_USER", {})
    with patch.object(hp, "_claim_active_from_db", AsyncMock(return_value=None)), \
         patch.object(hp, "_persist_job", AsyncMock()), \
         patch.object(hp, "_enqueue_job", AsyncMock(return_value=False)), \
         patch.object(hp, "_start_inline", AsyncMock()) as inline:
        result = await hp.start_her_pov("alice")
    assert result["status"] == "failed"
    assert hp._JOBS[result["job_id"]]["error"] == "queue_unavailable"
    assert "alice" not in hp._ACTIVE_JOB_BY_USER
    inline.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_pov_existing_inflight_job_is_not_retired_for_new_request(writer):
    from app import memory_her_pov as hp
    conn = _RailConn(results=[[("job", "drawing")]])
    patches = _patch_rail(conn)
    with patches[0], patches[1], patches[2]:
        result = await hp._claim_active_from_db("alice")
    assert result == {"job_id": "job", "status": "drawing", "reused": True}
    assert all(sql.lstrip().startswith("SELECT") for sql, _ in conn.calls)


@pytest.mark.asyncio
async def test_cloud_pov_worker_claim_has_no_stale_inflight_recovery(writer):
    from app import memory_her_pov as hp
    job_id = "e5628645-bcff-48c2-a8e3-ae016a8d8471"
    conn = _RailConn(results=[[]])
    patches = _patch_rail(conn)
    with patches[0], patches[1], patches[2], patch.object(hp, "run_her_pov", AsyncMock()) as run:
        assert await hp.run_job_from_queue(job_id) is True
    sql, params = conn.calls[0]
    assert "status = 'queued'" in sql
    assert "status IN ('searching', 'thinking', 'drawing')" not in sql
    assert params == (job_id,)
    run.assert_not_awaited()
