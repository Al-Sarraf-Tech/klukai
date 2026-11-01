"""Only opted-in dream and memory writers leave the background (Haiku) route."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import psycopg
import seed_memories
from .test_reflection_on_return import _FakePool, _mk_affection
from .test_memory_her_pov import _RailConn, _patch_rail


@pytest.fixture
def background(monkeypatch):
    """The background Haiku route's transport; cloud creative paths never use it."""
    call = AsyncMock(return_value="Welcome back to the hangar, Commander.")
    monkeypatch.setattr("app.extraction_llm.complete", call)
    return call


@pytest.fixture
def writer(monkeypatch):
    from app import creative_text
    monkeypatch.setenv("KLUKAI_CREATIVE_TEXT_PROVIDER", "anthropic")
    call = AsyncMock(return_value="I dreamed of the hangar in the rain.")
    monkeypatch.setattr(creative_text, "complete_creative_text", call)
    return call


@pytest.mark.asyncio
async def test_night_dream_uses_cloud_without_the_background_route(writer, background):
    from app.proactive import ProactiveEngine
    engine = ProactiveEngine()
    engine._affection_level = 5
    engine._on_message_callback = AsyncMock()
    with patch("app.proactive.events.random.random", return_value=0.2), \
         patch("app.memory_archive.list_memories", AsyncMock(return_value=[])):
        await engine._dream_event()
    engine._on_message_callback.assert_awaited_once_with("I dreamed of the hangar in the rain.")
    background.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_dream_failure_does_not_try_the_background_writer(writer, background):
    from app.proactive import ProactiveEngine
    from app.creative_text import CreativeTextError
    writer.side_effect = CreativeTextError("refused")
    engine = ProactiveEngine()
    engine._affection_level = 5
    engine._on_message_callback = AsyncMock()
    with patch("app.proactive.events.random.random", return_value=0.2), \
         patch("app.memory_archive.list_memories", AsyncMock(return_value=[])):
        await engine._dream_event()
    background.assert_not_awaited()
    engine._on_message_callback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("hour,cloud", [(8, True), (15, False)])
async def test_only_return_dream_moves_to_cloud(writer, background, hour, cloud):
    from app.reflect_helpers import _maybe_reflect_on_return
    last = datetime.now(timezone.utc) - timedelta(hours=12)
    pool = _FakePool([(last,), [("user", "We talked about rain.")], ("tender",)])
    ws = MagicMock()
    ws.is_connected.return_value = False
    aff = MagicMock()
    aff.get_state = AsyncMock(return_value=_mk_affection())
    saved = AsyncMock()
    with patch("app.db.get_pool", return_value=pool), \
         patch("app.reflect_helpers.ws", ws), \
         patch("app.reflect_helpers.affection", aff), \
         patch("app.reflect_helpers.now_local", return_value=datetime(2026, 10, 14, hour)), \
         patch("app.personality.load_personality", return_value={}), \
         patch("app.personality.build_character_preamble", return_value="You are Klukai."), \
         patch("app.dreams.save_dream", saved), \
         patch("asyncio.sleep", AsyncMock()):
        await _maybe_reflect_on_return("alice")
    assert writer.await_count == int(cloud)
    assert background.await_count == int(not cloud)
    if cloud:
        assert saved.await_args.kwargs["dream_text"] == "I dreamed of the hangar in the rain."


@pytest.mark.asyncio
async def test_pov_cloud_json_preserves_scene_without_the_background_route(
    writer, background
):
    from app.memory_her_pov import compose_pov
    writer.return_value = json.dumps({
        "annotation": "I kept the rain between us.", "scene_tags": "hangar, rain, silver hair",
        "couple": True, "mood": "tender", "title": "Rain at Home",
    })
    result = await compose_pov({"user_content": "Rain", "assistant_content": "Home"}, 5)
    assert result["annotation"] == "I kept the rain between us."
    assert result["scene_tags"] == "hangar, rain, silver hair"
    background.assert_not_awaited()


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
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    result = await seed_memories.main()
    assert result == 0
    connect.assert_not_awaited()
    writer.assert_not_awaited()
    # Successful no-op: no task was started and no history was read.


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
async def test_cloud_pov_missed_queue_hint_stays_queued_without_inline_work(writer, monkeypatch):
    from app import memory_her_pov as hp
    monkeypatch.setenv("HER_POV_EXECUTION", "queue")
    monkeypatch.setattr(hp, "_JOBS", {})
    monkeypatch.setattr(hp, "_ACTIVE_JOB_BY_USER", {})
    with patch.object(hp, "_claim_active_from_db", AsyncMock(return_value=None)), \
         patch.object(hp, "_persist_job", AsyncMock()), \
         patch("app.job_handoff.reconcile_once", AsyncMock(return_value=1)), \
         patch.object(hp, "_start_inline", AsyncMock()) as inline:
        result = await hp.start_her_pov("alice")
    assert result["status"] == "queued"
    assert hp._JOBS[result["job_id"]]["status"] == "queued"
    assert hp._ACTIVE_JOB_BY_USER["alice"] == result["job_id"]
    inline.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_pov_live_inflight_job_is_reused(writer):
    """A picture that is still moving is not failed, and it is not redrawn."""
    from app import memory_her_pov as hp
    conn = _RailConn(results=[[], [("job", "drawing")]])
    patches = _patch_rail(conn)
    with patches[0], patches[1], patches[2]:
        result = await hp._claim_active_from_db("alice")
    assert result == {"job_id": "job", "status": "drawing", "reused": True}
    sweep_sql, sweep_params = conn.calls[0]
    assert "status = 'failed'" in sweep_sql
    assert "status <> 'queued'" in sweep_sql
    assert sweep_params == ("alice", hp._STALE_JOB_MINUTES)
    assert conn.calls[1][0].lstrip().startswith("SELECT")


@pytest.mark.asyncio
async def test_cloud_pov_stale_job_unlocks_the_next_request(writer):
    """A paid job that stopped moving used to be reused forever."""
    from app import memory_her_pov as hp
    stale_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    hp._ACTIVE_JOB_BY_USER["alice"] = stale_id
    hp._JOBS[stale_id] = {
        "id": stale_id,
        "user_id": "alice",
        "status": "drawing",
        "requested_by_user": True,
    }
    conn = _RailConn(results=[[(stale_id,)], []])
    patches = _patch_rail(conn)
    try:
        with patches[0], patches[1], patches[2]:
            assert await hp._claim_active_from_db("alice") is None
        assert "alice" not in hp._ACTIVE_JOB_BY_USER
        assert hp._JOBS[stale_id]["status"] == "failed"
        assert hp._JOBS[stale_id]["error"] == "stale"
    finally:
        hp._ACTIVE_JOB_BY_USER.pop("alice", None)
        hp._JOBS.pop(stale_id, None)
    assert "error = 'stale'" in conn.calls[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "held",
    ["generation", "background", "lock"],
)
async def test_cloud_pov_in_flight_picture_is_not_failed(writer, held):
    """A live paid render is not failed, so the next tap does not buy another."""
    from app import memory_her_pov as hp

    conn = _RailConn(results=[[("job", "drawing")]])
    patches = _patch_rail(conn)
    with (
        patches[0],
        patches[1],
        patches[2],
        patch(
            "app.agy_image_provider.generation_in_flight",
            return_value=held == "generation",
        ),
        patch(
            "app.account_images.background_render_active",
            return_value=held == "background",
        ),
        patch(
            "app.image_provider.render_lock_held",
            return_value=held == "lock",
        ),
    ):
        result = await hp._claim_active_from_db("alice")
    assert result == {"job_id": "job", "status": "drawing", "reused": True}
    assert conn.calls[0][0].lstrip().startswith("SELECT")
    assert not any("status = 'failed'" in sql for sql, _ in conn.calls)


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
