"""Inner-life fixes: one send, one handoff effect, text dreams, local clocks."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.dreams import save_dream, without_picture_instructions
from app.her_pov_pipeline import HerPOVPipeline
from app.night_watch import night_watch_prompt_block
from app.op_brief_time import resolve_when


CHICAGO = ZoneInfo("America/Chicago")


def test_dream_text_drops_picture_instructions():
    prose = (
        "I dreamed the hangar lights stayed on and you were still there.\n"
        "1girl, masterpiece, best quality, negative prompt: lowres"
    )
    cleaned = without_picture_instructions(prose)
    assert cleaned.startswith("I dreamed the hangar")
    assert "1girl" not in cleaned
    assert "negative prompt" not in cleaned


def test_picture_only_dream_is_not_stored():
    assert (
        without_picture_instructions(
            "masterpiece, best quality, 1girl, draw an image of the hangar"
        )
        == ""
    )


@pytest.mark.asyncio
async def test_saved_dream_has_no_image_prompt():
    class Conn:
        def __init__(self):
            self.params = None

        async def execute(self, sql, params):
            self.params = params

        async def commit(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    conn = Conn()

    class Pool:
        def connection(self):
            return conn

    text = (
        "I dreamed the rain on the hangar roof kept time with your footsteps "
        "and I did not tell you I was awake."
    )
    with patch("app.dreams.get_pool", return_value=Pool()):
        memory_id = await save_dream(text)
    assert memory_id
    # prompt is the last insert value and must not be an image brief
    assert conn.params[-1] is None
    assert conn.params[2] == text


def test_op_brief_uses_chicago_not_a_utc_day():
    """04:00 UTC is still the previous evening in Chicago."""
    now = datetime(2026, 11, 25, 4, 0, tzinfo=timezone.utc)
    when = resolve_when("tomorrow at 9", now)
    assert when is not None
    assert when.day.isoformat() == "2026-11-25"
    assert when.at.hour == 9


@pytest.mark.asyncio
async def test_night_watch_does_not_page_on_a_utc_evening():
    now = datetime(2026, 11, 25, 4, 0, tzinfo=timezone.utc)
    block = await night_watch_prompt_block(
        "claude", {}, 4, MagicMock(), now=now
    )
    assert block == ""


@pytest.mark.asyncio
async def test_deferred_failure_after_send_is_not_retried():
    from app import deferred

    sent: list[str] = []

    async def send(_user, text):
        sent.append(text)
        raise RuntimeError("socket closed after the line was sent")

    action = {"kind": "message", "text": "Still here."}
    claimed = {"task_id": "t", "user_id": "claude", "action": action}
    mark = AsyncMock()
    ws = MagicMock()
    ws.send_proactive = AsyncMock(side_effect=send)
    with (
        patch.object(deferred, "claim", AsyncMock(return_value=claimed)),
        patch.object(deferred, "_mark_failed", mark),
        patch("app.context.ws", ws),
    ):
        assert await deferred.fire("t") is False
    assert sent == ["Still here."]
    mark.assert_not_awaited()
    assert action[deferred.EFFECT_STARTED] is True


@pytest.mark.asyncio
async def test_deferred_failure_before_send_can_still_retry():
    from app import deferred

    action = {"kind": "message", "text": "Still here."}
    claimed = {"task_id": "t", "user_id": "claude", "action": action}
    mark = AsyncMock()

    async def boom(_user, _action):
        raise RuntimeError("db down before the send")

    with (
        patch.object(deferred, "claim", AsyncMock(return_value=claimed)),
        patch.object(deferred, "dispatch", AsyncMock(side_effect=boom)),
        patch.object(deferred, "_mark_failed", mark),
    ):
        assert await deferred.fire("t") is False
    mark.assert_awaited_once()


@pytest.mark.asyncio
async def test_return_greeting_is_not_sent_twice_for_one_absence():
    from app.reflect_helpers import _greeted_absence, _maybe_reflect_on_return

    _greeted_absence.clear()
    away = datetime.now(timezone.utc) - timedelta(hours=12)
    batches = [
        (away,),
        [("user", "we talked about the hangar roof in the rain")],
    ]

    class Conn:
        def __init__(self, rows):
            self._rows = list(rows)

        async def execute(self, sql, params=None):
            result = AsyncMock()
            if "companion_persistent_state" in sql:
                result.fetchone = AsyncMock(return_value=None)
            elif "model" in sql and "proactive" in sql and "INTERVAL" in sql:
                result.fetchone = AsyncMock(return_value=(1,))
            elif self._rows:
                batch = self._rows.pop(0)
                if isinstance(batch, tuple):
                    result.fetchone = AsyncMock(return_value=batch)
                else:
                    result.fetchall = AsyncMock(return_value=batch)
            return result

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Pool:
        def connection(self):
            return Conn(batches)

    haiku = AsyncMock(return_value="Welcome back, Commander. I kept the hangar lights on.")
    ws = MagicMock()
    ws.is_connected = MagicMock(return_value=True)
    ws.send_proactive = AsyncMock()
    aff = MagicMock()
    aff.get_state = AsyncMock(
        return_value=MagicMock(level=5, mood="tender")
    )
    with (
        patch("app.db.get_pool", return_value=Pool()),
        patch("app.extraction_llm.complete", haiku),
        patch("app.reflect_helpers.ws", ws),
        patch("app.reflect_helpers.affection", aff),
        patch("app.personality.load_personality", return_value={"user_title": "Commander"}),
        patch("app.personality.build_character_preamble", return_value="You are Klukai."),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        await _maybe_reflect_on_return("alice")
    haiku.assert_not_awaited()
    ws.send_proactive.assert_not_awaited()
    _greeted_absence.clear()


@pytest.mark.asyncio
async def test_cancelled_her_pov_does_not_stay_running():
    started = asyncio.Event()

    async def hang(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    jobs: dict[str, dict] = {}

    async def set_job(job_id, **fields):
        jobs.setdefault(job_id, {"id": job_id}).update(fields)

    released: list[str] = []

    async def release(_user, job_id):
        released.append(job_id)

    pipeline = HerPOVPipeline(
        set_job,
        AsyncMock(
            return_value={
                "user_content": "I waited at the hangar.",
                "assistant_content": "You did not have to.",
                "mood": "tender",
            }
        ),
        AsyncMock(
            return_value={
                "annotation": "I kept the light on.",
                "scene_tags": "hangar, night",
                "couple": False,
                "mood": "tender",
                "title": "The light",
            }
        ),
        release,
    )
    ws = MagicMock()
    ws.send = AsyncMock()
    ws.send_thinking = AsyncMock()
    ws.send_proactive = AsyncMock()
    aff = MagicMock()
    aff.get_state = AsyncMock(return_value=MagicMock(level=3))
    with (
        patch("app.memory_her_pov.get_job", AsyncMock(return_value=None)),
        patch("app.context.ws", ws),
        patch("app.context.affection", aff),
        patch("app.image_gen.generate_image", AsyncMock(side_effect=hang)),
        patch("app.image_gen.build_prompt", MagicMock(return_value="prompt")),
        patch("app.her_day.render_costume", AsyncMock(return_value=None)),
        patch("app.wardrobe.granted_request_for", return_value=None),
        patch("app.proactive.state.now_local", return_value=datetime(2026, 11, 24, 21, 0)),
        patch("app.image_provider.selected_provider", return_value="agy"),
    ):
        task = asyncio.create_task(pipeline.run("claude", "job-1"))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert jobs["job-1"]["status"] == "failed"
    assert jobs["job-1"]["phase"] == "failed"
    assert jobs["job-1"]["error"] == "cancelled"
    assert released == ["job-1"]
