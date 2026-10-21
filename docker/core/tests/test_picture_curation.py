"""One turn's picture gets that turn's curation, whichever of the two lands last."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from app.chat_followups import ChatFollowups
from app.models import SessionState
from app.picture_curation import PictureCuration

CURATION = {
    "keep": True,
    "annotation": "He asked, so I stood still.",
    "category": "The Commander",
}


async def test_curation_first_then_picture_applies_to_that_picture():
    apply = AsyncMock(return_value=True)
    picture = PictureCuration(apply, "u1")
    await picture.curation_ready(CURATION, 9)
    apply.assert_not_awaited()
    await picture.picture_saved("mem-new")
    apply.assert_awaited_once_with("mem-new", CURATION, 9, user_id="u1")


async def test_picture_first_then_curation_applies_once():
    apply = AsyncMock(return_value=True)
    picture = PictureCuration(apply, "u1")
    await picture.picture_saved("mem-new")
    apply.assert_not_awaited()
    await picture.curation_ready(CURATION, 4)
    await picture.curation_ready({"annotation": "late duplicate"}, 4)
    await picture.picture_saved("mem-other")
    apply.assert_awaited_once_with("mem-new", CURATION, 4, user_id="u1")


async def test_failed_picture_never_curates():
    apply = AsyncMock(return_value=True)
    picture = PictureCuration(apply, "u1")
    await picture.curation_ready(CURATION, 9)
    apply.assert_not_awaited()


async def test_apply_error_is_logged_not_raised(caplog):
    picture = PictureCuration(AsyncMock(side_effect=RuntimeError("db down")), "u1")
    await picture.curation_ready(CURATION, 9)
    await picture.picture_saved("mem-new")
    assert "Memory curation update failed for mem-new" in caplog.text


def _followups(image: AsyncMock, extract: AsyncMock, ws: AsyncMock) -> ChatFollowups:
    return ChatFollowups(
        ws,
        lambda _uid: "previous-picture",
        lambda _text: False,
        lambda _text: True,
        AsyncMock(),
        AsyncMock(),
        image,
        extract,
        AsyncMock(),
        (),
        (),
        (),
        10_000,
        curate=AsyncMock(return_value=True),
    )


async def test_picture_turn_hands_one_join_to_the_picture_and_the_extraction():
    tasks: list[asyncio.Task[None]] = []
    ws = AsyncMock()
    ws.track_task = lambda _uid, task, **_kw: tasks.append(task)
    image, extract = AsyncMock(), AsyncMock()
    session = SessionState(conversation_id="c")
    await _followups(image, extract, ws).admit(
        "take a picture of you in the hangar", "Hold still.", session, "u1"
    )
    await asyncio.gather(*tasks)
    picture = image.await_args.kwargs["picture"]
    assert isinstance(picture, PictureCuration)
    assert extract.await_args.kwargs["picture"] is picture
    assert extract.await_args.kwargs["image_generated"] is True


async def test_the_turns_extraction_survives_his_disconnect():
    """Closing the app right after her reply must not cancel the turn's
    extraction (mood, heart rate, affection, facts, recall memory)."""
    tracked: list[tuple[asyncio.Task[None], dict]] = []
    ws = AsyncMock()
    ws.track_task = lambda _uid, task, **kw: tracked.append((task, kw))
    extract = AsyncMock()
    followups = ChatFollowups(
        ws,
        lambda _uid: None,
        lambda _text: False,
        lambda _text: False,
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        extract,
        AsyncMock(),
        (),
        (),
        (),
        10_000,
    )
    await followups.admit(
        "tell me about the convoy", "It moved at dawn.", SessionState(conversation_id="c"), "u1"
    )
    await asyncio.gather(*(task for task, _ in tracked))
    extract.assert_awaited_once()
    assert [kw for _, kw in tracked] == [{"survive_disconnect": True}]
