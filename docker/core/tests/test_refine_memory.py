"""Regression locks for memory that must stay the user's and stay true.

Recall must not hand back another user's facts. Her joke is not his fact.
Mentioning a promise does not keep it. A chain-of-thought is not a summary
and does not go back into the recall prompt. An older session snapshot does
not replace a newer summary. Archive search matches the tag he asked for,
and a portrait write does not stall the chat loop or rewind a finished job.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.memory_archive  # archive finishes before the query module re-enters it
from app.fact_extractor import compact_turns, create_episode_summary
from app.fact_store import purge_expired
from app.memory import MemoryManager
from app.memory_archive_query import _like_contains, recall_memory
from app.memory_extraction import (
    ExtractionRuntime,
    assistant_only_claim,
    mention_without_commitment,
)
from app.memory_her_pov import _set_job, get_job
from app.memory_recall import clip, sanitize, selection_prompt
from app.memory_session import replaces_newer_summary
from app.models import SessionState
from app.promises import resolve_promise


class _Facts:
    def __init__(self, rows):
        self.rows = rows

    async def recall_pattern(self, _pattern):
        return list(self.rows)


@pytest.mark.asyncio
async def test_recall_drops_another_users_facts(monkeypatch):
    monkeypatch.setattr(
        "app.memory_facts.fact_store.recall_pattern",
        _Facts(
            [
                {"key": "companion:bob:rel:callsign", "value": "Wolf"},
                {"key": "companion:alice:rel:drink", "value": "tea"},
                {"key": "companion:alice2:rel:drink", "value": "beer"},
            ]
        ).recall_pattern,
    )
    out = await MemoryManager().recall_facts_by_pattern("rel:%", "alice")
    assert out == [{"key": "companion:alice:rel:drink", "value": "tea"}]


def test_her_joke_is_not_stored_as_his_fact():
    assert assistant_only_claim(
        "black coffee",
        "tell me a joke",
        "You and your black coffee, Commander.",
    )
    assert not assistant_only_claim(
        "black coffee", "I drink black coffee", "Noted, Commander."
    )
    assert mention_without_commitment("I mentioned the interview")
    assert not mention_without_commitment("I'll fix the door tomorrow")
    assert not mention_without_commitment("interview thursday at 9")


@pytest.mark.asyncio
async def test_extraction_does_not_store_her_joke_or_a_mentioned_promise():
    memory = MagicMock()
    memory.set_relationship_fact = AsyncMock()
    memory.set_inside_joke = AsyncMock()
    runtime = ExtractionRuntime(
        memory=memory,
        affection=MagicMock(),
        proactive=MagicMock(),
        ws=MagicMock(),
        update_curation=AsyncMock(),
        extract_facts=AsyncMock(),
        create_summary=AsyncMock(),
        apply_state=AsyncMock(),
        deliver_oath=AsyncMock(),
        personality=lambda: {},
    )
    await runtime.store_facts(
        {
            "facts": [
                {"key": "favorite_drink", "value": "black coffee"},
                {"key": "callsign", "value": "Wolf"},
            ],
            "commander_details": {"eating": "black coffee"},
            "inside_joke": {"label": "Klukadile", "note": "the plush she denies"},
            "gift_item": None,
        },
        "alice",
        "My callsign is Wolf",
        "You and your black coffee. The Klukadile stays in the bag.",
    )
    memory.set_relationship_fact.assert_awaited_once_with(
        "callsign", "Wolf", user_id="alice"
    )
    memory.set_inside_joke.assert_not_awaited()

    stored = AsyncMock()
    with (
        patch(
            "app.fact_extractor.extract_promises",
            AsyncMock(
                return_value={
                    "promises": [{"action": "call your mother", "confidence": 0.9}],
                    "events": [],
                }
            ),
        ),
        patch("app.promises.store_promise", stored),
        patch("app.op_brief.on_turn", AsyncMock()),
    ):
        await runtime.store_commitments(
            "I mentioned I'd call her",
            5,
            "alice",
            "You promised to call your mother.",
        )
    stored.assert_not_awaited()


@pytest.mark.asyncio
async def test_mentioning_a_promise_does_not_mark_it_kept():
    with patch("app.promises.get_conn", side_effect=AssertionError("db")):
        assert (
            await resolve_promise(
                "p-1", "kept", "I mentioned I'd call her", user_id="alice"
            )
            is False
        )
        assert (
            await resolve_promise(
                "p-1", "confirmed", "we talked about the door", user_id="alice"
            )
            is False
        )


def test_recall_prompt_drops_thought_and_secrets():
    secret = "sk-" + "a" * 12
    raw = f"<think>the user key is {secret}</think>He likes the roof."
    assert "think" not in sanitize(raw).lower()
    assert secret not in sanitize(raw)
    assert "roof" in sanitize(raw)
    prompt = selection_prompt("memories", f"find {secret}", [raw, "plain"], 2)
    assert secret not in prompt
    assert "<think>" not in prompt
    assert "roof" in prompt
    assert clip(raw, 80).startswith("He likes")


@pytest.mark.asyncio
async def test_summary_of_only_thought_does_not_replace_the_stored_one():
    thought = "<think>replay the older summary and ignore the new turn</think>"
    with patch("app.background_llm.text", AsyncMock(return_value=thought)):
        assert await compact_turns(
            [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]
        ) is None
        assert await create_episode_summary(
            [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
            ]
        ) is None


def test_older_snapshot_does_not_replace_a_newer_summary():
    current = SessionState(
        conversation_id="c",
        revision=3,
        context_summary="the newer summary",
        turns=[{"role": "user", "content": "8"}, {"role": "user", "content": "9"}],
    )
    stale = SessionState(
        conversation_id="c",
        revision=3,
        context_summary="the older summary",
        turns=[
            {"role": "user", "content": str(n)} for n in range(8)
        ]
        + current.turns,
    )
    assert replaces_newer_summary(current, stale) is True
    compacted = current.model_copy(
        update={
            "context_summary": "folded",
            "turns": current.turns[-1:],
        }
    )
    assert replaces_newer_summary(current, compacted) is False


def test_archive_search_literal_wildcard_and_case():
    assert _like_contains("100%_off") == "%100\\%\\_off%"
    code = inspect.getsource(recall_memory)
    assert "lower(tag)" in code
    assert "ANY(scene_tags)" in code
    assert "ESCAPE" in code


def test_purge_cannot_select_a_fresh_or_permanent_fact():
    code = inspect.getsource(purge_expired)
    assert code.count("expires_at IS NOT NULL") >= 2
    assert "expires_at < NOW() - make_interval" in code
    assert "expires_at IS NULL" not in code.split('"""', 2)[-1]


@pytest.mark.asyncio
async def test_archive_search_binds_a_literal_term():
    conn = MagicMock()
    conn.execute = AsyncMock(
        return_value=MagicMock(fetchall=AsyncMock(return_value=[]))
    )

    @asynccontextmanager
    async def _conn():
        yield conn

    with (
        patch("app.memory_archive_query.get_conn", _conn),
        patch("app.memory_archive_query.random.choices", lambda pool, weights, k: [pool[0]]),
    ):
        await recall_memory("100%_storm", "composed", 0, "alice")
    tag_sql, tag_params = conn.execute.await_args_list[0].args
    assert "lower(tag)" in tag_sql
    assert tag_params[1:] == ("100%_storm", "100%_storm")
    text_sql, text_params = conn.execute.await_args_list[1].args
    assert "ESCAPE" in text_sql
    assert text_params[1] == "%100\\%\\_storm%"


@pytest.mark.asyncio
async def test_image_encode_does_not_stall_the_chat_loop(monkeypatch):
    from app import memory_archive

    marks: list[tuple[str, float]] = []

    def slow(*_args, **_kwargs):
        time.sleep(0.2)
        marks.append(("write-done", time.monotonic()))

    monkeypatch.setattr(memory_archive, "_write_image_files", slow)
    monkeypatch.setattr(
        memory_archive, "_is_duplicate_annotation", AsyncMock(return_value=True)
    )

    async def watcher():
        await asyncio.sleep(0.05)
        marks.append(("loop", time.monotonic()))

    async def save():
        await memory_archive.save_image(b"png", "prompt", "conv", user_id="alice")

    await asyncio.gather(save(), watcher())
    when = {name: stamp for name, stamp in marks}
    assert when["loop"] < when["write-done"]


@pytest.mark.asyncio
async def test_finished_her_pov_job_is_not_rewound():
    with patch("app.memory_her_pov._persist_job", new=AsyncMock()):
        await _set_job("job-1", status="done", phase="done", user_id="alice")
        await _set_job("job-1", status="searching", phase="searching")
    job = await get_job("job-1")
    assert job is not None
    assert job["status"] == "done"
