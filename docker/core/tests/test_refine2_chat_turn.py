"""A reminder or a negation must not keep or discard the last picture."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chat_followups import ChatFollowups
from app.helpers import DISCARD_KEYWORDS, SAVE_KEYWORDS
from app.models import SessionState


async def _kept(text: str, memory_id: str | None) -> list[tuple[str, bool]]:
    calls: list[tuple[str, bool]] = []
    tasks: list[asyncio.Task] = []

    async def keep_memory(
        memory_id_arg: str, *, kept: bool, user_id: str | None = None
    ) -> None:
        calls.append((memory_id_arg, kept))
        assert user_id == "jalsarraf" or user_id is None or isinstance(user_id, str)

    ws = MagicMock()
    ws.track_task = lambda _user, task, **_kwargs: tasks.append(task)
    followups = ChatFollowups(
        ws,
        lambda _user: memory_id,
        lambda _text: False,
        lambda _text: False,
        keep_memory,
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        SAVE_KEYWORDS,
        DISCARD_KEYWORDS,
        (),
        999,
        extraction_idle_s=30,
    )
    try:
        await followups.admit(text, "Noted.", SessionState(conversation_id="c"), "u")
        await asyncio.sleep(0)
        return list(calls)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize(
    ("text", "memory_id", "expected"),
    [
        ("I'll keep that in mind", "mem-1", []),
        ("Keep this in mind, Commander", "mem-1", []),
        ("please keep that", "mem-1", [("mem-1", True)]),
        ("save that memory please now", "mem-1", [("mem-1", True)]),
        ("keep that in mind, but save this one", "mem-1", [("mem-1", True)]),
        ("why not keep that", "mem-1", [("mem-1", True)]),
        ("don't keep that", "mem-1", []),
        ("I will never forget that night", "mem-1", []),
        ("don't forget that", "mem-1", []),
        ("Don\u2019t forget that", "mem-1", []),
        ("forget that thing entirely", "mem-1", [("mem-1", False)]),
        ("please keep that", None, []),
    ],
)
async def test_picture_keep_ignores_reminders_and_negation(
    text: str, memory_id: str | None, expected: list[tuple[str, bool]]
) -> None:
    assert await _kept(text, memory_id) == expected
