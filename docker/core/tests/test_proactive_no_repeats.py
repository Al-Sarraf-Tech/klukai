"""She does not repeat her scheduled lines.

Owner goal 2026-11-01. In the 14 days before this, 93 of her 206 replies were
canned lines picked at random with no memory of what she had sent: one pool of
5 weather lines went out 35 times.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.proactive import ProactiveEngine
from app.proactive.events import _select_lore_message


@pytest.fixture
def engine():
    return ProactiveEngine()


def events(**pools):
    return {name: {"weight": 10, "messages": lines} for name, lines in pools.items()}


# ── picking from one pool ─────────────────────────────────────────────────


def test_a_line_she_just_sent_is_not_picked_again(engine):
    engine._recent_lines = ["a", "b"]
    for _ in range(50):
        assert engine._fresh_line(["a", "b", "c"]) == "c"


def test_when_every_line_was_sent_the_oldest_goes_first(engine):
    engine._recent_lines = ["b", "c", "a"]
    assert engine._fresh_line(["a", "b", "c"]) == "b"


def test_an_old_copy_does_not_hide_a_newer_send(engine):
    engine._recent_lines = ["a", "b", "a"]
    assert engine._fresh_line(["a", "b"]) == "b"


def test_a_pool_with_one_line_still_sends_it(engine):
    engine._recent_lines = ["a"]
    assert engine._fresh_line(["a"]) == "a"


def test_pick_message_avoids_recent_lines(engine):
    engine._affection_level = 2
    engine._recent_lines = ["x"]
    messages = {0: ["no"], 2: ["x", "y"]}
    for _ in range(50):
        assert engine._pick_message(messages) == "y"


# ── picking across categories ─────────────────────────────────────────────


def test_lore_skips_sent_lines_and_exhausted_categories():
    pools = events(weather=["w1", "w2"], danger=["d1"])
    for _ in range(50):
        assert _select_lore_message(pools, {}, 5, recent=["w1", "w2"]) == "d1"


def test_lore_with_everything_sent_goes_oldest_first():
    pools = events(weather=["w1", "w2"], danger=["d1"])
    assert _select_lore_message(pools, {}, 5, recent=["d1", "w2", "w1"]) == "d1"


def test_lore_without_history_still_picks_a_line():
    pools = events(weather=["w1"])
    assert _select_lore_message(pools, {}, 5) == "w1"


def test_lore_still_respects_the_affection_gate():
    pools = {
        "locked": {"weight": 1000, "min_affection": 9, "messages": ["secret"]},
        "open": {"weight": 1, "messages": ["open"]},
    }
    for _ in range(20):
        assert _select_lore_message(pools, {}, 1, recent=["open"]) == "open"


# ── what she remembers ────────────────────────────────────────────────────


async def test_a_delivered_line_is_remembered(engine):
    callback = AsyncMock(return_value=True)
    engine.set_callback(callback)
    await engine._on_message_callback("hello")
    callback.assert_awaited_once_with("hello")
    assert engine._recent_lines == ["hello"]


async def test_a_line_that_reached_nobody_is_not_remembered(engine):
    engine.set_callback(AsyncMock(return_value=False))
    assert await engine._on_message_callback("hello") is False
    assert engine._recent_lines == []


async def test_a_legacy_callback_returning_none_counts_as_sent(engine):
    engine.set_callback(AsyncMock(return_value=None))
    await engine._on_message_callback("hello")
    assert engine._recent_lines == ["hello"]


async def test_extra_arguments_reach_the_callback(engine):
    callback = AsyncMock(return_value=True)
    engine.set_callback(callback)
    await engine._on_message_callback("hello", user_id="ricky")
    callback.assert_awaited_once_with("hello", user_id="ricky")


async def test_memory_is_bounded(engine, monkeypatch):
    monkeypatch.setattr("app.proactive.engine.RECENT_LINE_MEMORY", 3)
    for line in ("a", "b", "c", "d"):
        engine._remember_line(line)
    assert engine._recent_lines == ["b", "c", "d"]


# ── surviving a restart ───────────────────────────────────────────────────


async def test_history_is_loaded_oldest_first_and_kept_ahead_of_new_lines(
    engine, monkeypatch
):
    load = AsyncMock(return_value=["newest", "older"])
    monkeypatch.setattr("app.conversation_continuity.load_recent_assistant_replies", load)
    engine._remember_line("sent during startup")
    await engine._load_recent_lines()
    assert engine._recent_lines == ["older", "newest", "sent during startup"]
    assert load.await_args.args[1] > 100


async def test_a_database_failure_leaves_her_working(engine, monkeypatch):
    monkeypatch.setattr(
        "app.conversation_continuity.load_recent_assistant_replies",
        AsyncMock(side_effect=RuntimeError("down")),
    )
    await engine._load_recent_lines()
    assert engine._recent_lines == []


# ── small things found alongside ──────────────────────────────────────────


def test_the_slow_reply_notice_leaves_room_for_a_normal_xhigh_reply():
    from app import chat_reply

    assert chat_reply.WARMUP_NOTICE_S >= 5.0


def test_she_keeps_more_of_the_conversation_word_for_word():
    from app import context
    from app.conversation_continuity import HISTORY_LIMIT
    from app.memory_session import MAX_SESSION_TURNS

    # Eight turns is four exchanges. Compaction fires before the session window
    # would drop turns that were never summarized.
    assert context.COMPACT_KEEP_RAW == 8
    assert context.COMPACT_KEEP_RAW < context.COMPACT_THRESHOLD <= MAX_SESSION_TURNS
    assert context.COMPACT_THRESHOLD < HISTORY_LIMIT


@pytest.mark.parametrize("status", ["failed", "refused", "unavailable", "busy", "unknown"])
async def test_a_picture_that_never_came_says_why(monkeypatch, caplog, status):
    import logging as _logging

    from app import agy_image_provider, image_gen, image_provider

    monkeypatch.setattr(image_provider, "selected_provider", lambda: "agy")
    monkeypatch.setattr(image_provider, "cloud_prompt", lambda p, **k: "SECRET SCENE")
    monkeypatch.setattr(agy_image_provider, "uses_agy_images", lambda user: True)
    monkeypatch.setattr(
        agy_image_provider,
        "submit_agy_image",
        AsyncMock(return_value={"status": status, "image": None}),
    )
    monkeypatch.setattr(
        "app.account_images.wait_for_background_render", AsyncMock(return_value=False)
    )
    with caplog.at_level(_logging.WARNING, logger="app.image_gen"):
        assert await image_gen.generate_image("p", user_id="jalsarraf") is None
    lines = [r.getMessage() for r in caplog.records]
    assert any(f"status={status}" in line and "jalsarraf" in line for line in lines), lines
    assert "SECRET SCENE" not in caplog.text
