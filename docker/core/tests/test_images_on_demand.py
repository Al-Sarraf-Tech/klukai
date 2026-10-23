"""Owner policy: only a human picture request may start rendering."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import psycopg
import seed_memories
from app import deferred, portrait
from app.proactive.engine import ProactiveEngine


@pytest.fixture(autouse=True)
def stale_image_settings(monkeypatch, tmp_path):
    """Old deployment settings must never opt automatic pictures back in."""
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    monkeypatch.setenv("KLUKAI_PORTRAIT_DAILY_LIMIT", "100")
    monkeypatch.setenv("KLUKAI_ALBUM_SEED_RENDERS", "2")
    monkeypatch.setenv("PORTRAIT_BACKFILL", "1")
    monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", "claude")
    monkeypatch.setenv("KLUKAI_FAL_DAILY_LIMIT", "100")
    monkeypatch.setenv("KLUKAI_FAL_MONTHLY_LIMIT", "1000")


async def test_proactive_art_never_renders_or_saves():
    engine = ProactiveEngine()
    engine._affection_level = 9
    engine._on_message_callback = AsyncMock()
    with patch("app.image_gen.generate_image", AsyncMock()) as render, patch(
        "app.memory_archive.save_image", AsyncMock()
    ) as save, patch("app.proactive.album_events.random.random", return_value=0):
        await engine._spontaneous_art_tick()
        await engine._spontaneous_art_event()
    render.assert_not_awaited()
    save.assert_not_awaited()
    engine._on_message_callback.assert_not_awaited()


async def test_mission_completion_never_schedules_a_picture():
    engine = ProactiveEngine()
    engine._on_message_callback = AsyncMock()
    timer = MagicMock(active_events=[])
    with patch.object(engine, "_spawn") as spawn, patch(
        "app.image_gen.generate_image", AsyncMock()
    ) as render:
        await engine.trigger_mission_aftermath_image("claude", timer=timer)
    spawn.assert_not_called()
    render.assert_not_awaited()
    engine._on_message_callback.assert_not_awaited()


async def test_scheduled_her_pov_cannot_start_a_picture_job():
    with patch("app.memory_her_pov.start_her_pov", AsyncMock()) as start:
        await deferred.dispatch("claude", {"kind": "her_pov"})
    start.assert_not_awaited()


async def test_new_deferred_her_pov_is_rejected_before_persistence():
    with patch("app.db.get_conn_autocommit") as connect, patch.object(
        deferred, "_arm_timer", AsyncMock()
    ) as arm:
        with pytest.raises(ValueError, match="on demand"):
            await deferred.schedule(
                {"kind": "her_pov"}, user_id="claude", delay_seconds=60
            )
    connect.assert_not_called()
    arm.assert_not_awaited()


@pytest.mark.parametrize("provider", ["agy", "comfyui", "openai"])
async def test_portrait_poll_is_cache_only(monkeypatch, provider):
    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", provider)
    with patch.object(portrait, "ensure_generation") as ensure, patch(
        "app.portrait_account.allowance_left", AsyncMock(return_value=True)
    ):
        state = await portrait.portrait_state(
            "claude", "speed_star", 9, game_active=False
        )
    assert state["status"] == "unavailable"
    ensure.assert_not_called()


async def test_wardrobe_prewarm_and_idle_backfill_cannot_enqueue():
    with patch.object(portrait, "current_outfit", AsyncMock()) as outfit, patch.object(
        portrait, "ensure_generation"
    ) as ensure, patch.object(portrait, "spawn") as spawn:
        assert await portrait.prewarm("claude", 9) is False
        portrait.prewarm_soon("claude", 9)
        assert await portrait.backfill_tick("claude") is None
    outfit.assert_not_awaited()
    ensure.assert_not_called()
    spawn.assert_not_called()
    assert portrait.backfill_enabled() is False


@pytest.mark.parametrize("provider", ["agy", "comfyui", "openai"])
async def test_album_seed_exits_before_database_or_any_provider(monkeypatch, provider):
    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", provider)
    with patch.object(
        psycopg.AsyncConnection, "connect", AsyncMock()
    ) as connect, patch("app.image_gen.generate_image", AsyncMock()) as render:
        assert await seed_memories.main() == 0
    connect.assert_not_awaited()
    render.assert_not_awaited()
