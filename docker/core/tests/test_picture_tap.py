"""One tap on the camera draws the current situation: no chat turn, no refusal."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import chat, picture_tap
from app.models import SessionState


def _memory(session):
    memory = MagicMock()

    @contextlib.asynccontextmanager
    async def owner(_key):
        yield

    memory.session_owner = owner
    memory.get_session = AsyncMock(return_value=session)
    return memory


async def _tap(session, *, valid=True):
    ws = MagicMock()
    ws.validate_connection = AsyncMock(return_value=valid)
    tasks = []
    ws.track_task = lambda uid, task, **kw: tasks.append((uid, task, kw))
    image = AsyncMock()
    with (
        patch.object(picture_tap, "memory", _memory(session)),
        patch.object(picture_tap, "ws", ws),
        patch.object(picture_tap.background, "background_image_gen", image),
    ):
        started = await picture_tap.handle_picture_tap("claude", MagicMock())
        await asyncio.gather(*(t for _, t, _ in tasks))
    return started, image, tasks


async def test_one_tap_starts_the_current_situation_picture_without_a_chat_turn():
    session = SessionState(
        conversation_id="c1",
        turns=[
            {"role": "user", "content": "good evening"},
            {"role": "assistant", "content": "Evening."},
        ],
    )
    started, image, tasks = await _tap(session)
    assert started is True
    image.assert_awaited_once()
    args, kwargs = image.await_args
    assert args == ("take a picture",)  # never "show me where you are"
    assert kwargs["user_id"] == "claude"
    assert [t["role"] for t in kwargs["recent_turns"]] == ["user", "assistant"]
    assert tasks[0][2] == {"survive_disconnect": True}
    # nothing was added to the transcript: no user bubble, no model reply
    assert len(session.turns) == 2


async def test_a_tap_from_a_retired_device_draws_nothing():
    started, image, _ = await _tap(SessionState(conversation_id="c1"), valid=False)
    assert started is False
    image.assert_not_awaited()


async def test_a_tap_without_a_session_reports_unavailable():
    from app.memory import SessionUnavailable

    with (
        patch.object(picture_tap, "memory", _memory(None)),
        patch.object(
            picture_tap,
            "ws",
            MagicMock(validate_connection=AsyncMock(return_value=True)),
        ),
        pytest.raises(SessionUnavailable),
    ):
        await picture_tap.handle_picture_tap("claude", MagicMock())


async def test_the_socket_routes_a_picture_frame_to_the_tap_not_to_chat():
    websocket = MagicMock()
    with (
        patch.object(picture_tap, "handle_picture_tap", new=AsyncMock()) as tap,
        patch.object(chat, "_admit_turn", new=AsyncMock()) as admit,
    ):
        await chat._dispatch_device_frame({"type": "picture"}, "claude", websocket)
    tap.assert_awaited_once_with("claude", websocket)
    admit.assert_not_awaited()
