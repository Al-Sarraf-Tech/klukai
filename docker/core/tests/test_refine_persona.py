"""She stays herself: the latest ask, a closed decision, and one picture story."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from app.chat_prompt import picture_turn_text
from app.compaction import format_for_summary, select_messages_for_compaction
from app.conversation_continuity import (
    CONTINUITY_CHARS,
    cached_history,
    stitch_continuity,
)
from app.picture_intent import _STATE_LINES


def test_a_new_picture_is_not_described_as_already_over_or_refused():
    for outcome in ("delivered", "failed"):
        text = picture_turn_text(
            underway=True, available=True, status_note=_STATE_LINES[outcome]
        )
        assert "Never refuse" in text
        assert "being drawn for him right now" in text
        assert "No picture is being drawn" not in text
        assert _STATE_LINES[outcome] not in text


def test_a_picture_still_drawing_is_not_called_absent():
    text = picture_turn_text(
        underway=False, available=True, status_note=_STATE_LINES["drawing"]
    )
    assert "still being drawn" in text
    assert "No picture is being drawn" not in text
    assert "Never refuse" not in text


def test_an_idle_turn_still_says_she_can_draw_and_names_a_failure():
    text = picture_turn_text(
        underway=False, available=True, status_note=_STATE_LINES["failed"]
    )
    assert "You can draw and send him pictures" in text
    assert "No picture is being drawn this turn" in text
    assert "failed to draw" in text


def test_compaction_keeps_the_latest_ask_and_a_promise_at_the_end():
    promise = "I promise to hold the north ridge until you get back."
    ask = "Wait for me at the hangar tonight."
    older = {
        "role": "assistant",
        "content": ("We talked about the route. " * 40) + promise,
    }
    messages = [older]
    messages += [{"role": "user", "content": f"note {i}"} for i in range(60)]
    messages.append({"role": "user", "content": ask})
    to_compact, to_keep = select_messages_for_compaction(messages, keep_recent=10)
    assert to_keep[-1]["content"] == ask
    assert ask not in format_for_summary(to_compact)
    summary = format_for_summary([older])
    assert promise in summary
    assert "a" * 100 not in format_for_summary(
        [{"role": "user", "content": "a" * 1000}], limit_per_msg=50
    )


def test_a_long_reply_keeps_the_choice_that_closed_the_decision():
    choice = "I choose Belka. That call is made."
    answer = ("Let me think about the roster. " * 40) + choice
    question = "Who takes point? A: Belka. B: Andoris. Your call, Commander."
    newest = "and the rest of the day?"
    stitched = stitch_continuity(
        [{"role": "user", "content": newest}],
        [
            {"role": "assistant", "content": question},
            {"role": "user", "content": answer},
        ],
    )
    clipped = stitched[1]["content"]
    assert choice in clipped
    assert clipped.endswith("…")
    assert len(clipped) <= CONTINUITY_CHARS
    assert stitched[0]["content"] == question
    assert stitched[-1]["content"] == newest
    assert sum(turn["content"] == newest for turn in stitched) == 1


def test_the_newest_line_is_kept_once_when_the_archive_already_has_it():
    stitched = stitch_continuity(
        [{"role": "user", "content": "hello"}],
        [
            {"role": "assistant", "content": "Hi."},
            {"role": "user", "content": "  hello  "},
        ],
    )
    assert [turn["content"] for turn in stitched] == ["Hi.", "hello"]


def test_the_history_window_keeps_the_newest_message():
    turns = [{"role": "user", "content": str(i)} for i in range(24)]
    kept = cached_history(turns)
    assert kept[-1]["content"] == "23"
    assert kept[0]["content"] == "8"


def test_a_resolved_decision_is_not_left_open():
    from app.decisions import build_decision_block

    pending = {
        "ask": "Who takes point? A: Belka. B: Andoris. Your call, Commander.",
        "options": [
            {"key": "A", "label": "Belka", "disagree": False},
            {"key": "B", "label": "Andoris", "disagree": True},
        ],
    }
    block = build_decision_block(pending, "A")
    assert "That call is closed" in block
    assert "do not ask it again" in block
    assert "one sentence" in block
    assert "You're the Commander" not in block


async def test_a_stale_read_does_not_reopen_a_resolved_decision():
    from app import decisions

    decisions._pending_cache.clear()
    pending = {
        "id": "point",
        "ask": "Who takes point?",
        "options": [
            {"key": "A", "label": "Belka"},
            {"key": "B", "label": "Andoris"},
        ],
        "asked_at": datetime.now(timezone.utc).isoformat(),
    }

    class Mem:
        async def recall_fact(self, key, user_id="u"):
            decisions._pending_cache[user_id] = (time.monotonic(), None)
            return json.dumps(pending)

    try:
        found = await decisions._load_pending(
            Mem(), "u", datetime.now(timezone.utc)
        )
        assert found is None
    finally:
        decisions._pending_cache.clear()
