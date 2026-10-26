"""Opt-in real PostgreSQL proof of memory store + recall without embeddings.

Point KLUKAI_MIGRATION_TEST_DATABASE_URL at a disposable PostgreSQL database.
Each test gets a random schema with every migration applied and drops only
that schema on teardown. Claude Haiku is replaced by a stub.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool
import pytest

from app import background_llm, db
from app.memory import MemoryManager
from app.migrations import apply_migrations

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture
async def memory_db(monkeypatch):
    url = os.environ.get("KLUKAI_MIGRATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set KLUKAI_MIGRATION_TEST_DATABASE_URL to a disposable PostgreSQL database")
    schema = "memory_recall_test_" + uuid.uuid4().hex
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            async with AsyncConnectionPool(
                url, min_size=1, max_size=4, open=False,
                kwargs={"options": f"-c search_path={schema}"},
            ) as pool:
                await pool.wait()
                await apply_migrations(pool, MIGRATIONS)
                monkeypatch.setattr(db, "_pool", pool)
                yield pool
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _id(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


async def test_episodes_store_without_vectors_and_rank_matches_first(memory_db, monkeypatch):
    monkeypatch.setattr(background_llm, "json_object", AsyncMock(return_value=None))
    m = MemoryManager()
    await m.store_episode(_id(1), "We watched the storm from the rooftop", ["rooftop"], ["tender"], 0.3, user_id="alice")
    await m.store_episode(_id(2), "Coffee run before the briefing", [], [], 0.9, user_id="alice")
    await m.store_episode(_id(3), "Storm drills with the squad", [], [], 0.9, user_id="bob")
    # Insert-only: a repeated id changes nothing.
    await m.store_episode(_id(1), "overwritten?", [], [], 1.0, user_id="alice")

    out = await m.recall_episodes("Remember that storm?", limit=5, user_id="alice")

    # The full-text match outranks the more important non-match; bob's row
    # never appears; Haiku unavailable means the Postgres order as is.
    assert [e["summary"] for e in out] == [
        "We watched the storm from the rooftop",
        "Coffee run before the briefing",
    ]
    assert out[0]["keywords"] == ["rooftop"] and out[0]["emotion_tags"] == ["tender"]
    async with memory_db.connection() as conn:
        stored = await (await conn.execute(
            "SELECT summary, embedding_id FROM companion_episodes WHERE id = %s", (_id(1),)
        )).fetchall()
    assert stored == [("We watched the storm from the rooftop", None)]


async def test_query_without_search_words_ranks_by_importance_then_recency(memory_db, monkeypatch):
    monkeypatch.setattr(background_llm, "json_object", AsyncMock(return_value=None))
    m = MemoryManager()
    await m.store_episode(_id(1), "older and minor", [], [], 0.2, user_id="alice")
    await m.store_episode(_id(2), "important", [], [], 0.8, user_id="alice")
    await m.store_episode(_id(3), "newer and minor", [], [], 0.2, user_id="alice")

    out = await m.recall_episodes("?!", limit=5, user_id="alice")

    assert [e["summary"] for e in out] == ["important", "newer and minor", "older and minor"]


async def test_exchanges_follow_haiku_pick_and_its_order(memory_db, monkeypatch):
    m = MemoryManager()
    await m.store_exchange(_id(11), "I love rainy nights", "Then I'll keep the window open.", ["rain"], "warm", 0.7, user_id="alice")
    await m.store_exchange(_id(12), "The rain stopped", "Pity. I liked the sound.", ["rain"], "composed", 0.4, user_id="alice")
    await m.store_exchange(_id(13), "Rain again here", "Stay dry, Commander.", ["rain"], "composed", 0.4, user_id="bob")

    def ids_for(prompt: str, *wanted: str) -> list[str]:
        listed = [json.loads(line) for line in prompt.splitlines() if line.startswith('{"id"')]
        return [next(m["id"] for m in listed if w in m["text"]) for w in wanted]

    async def haiku(caller, prompt, **_):
        # Pick "The rain stopped" first, then "I love rainy nights".
        return {"ids": ids_for(prompt, "The rain stopped", "I love rainy nights")}

    pick = AsyncMock(side_effect=haiku)
    monkeypatch.setattr(background_llm, "json_object", pick)

    out = await m.recall_exchanges("rain", limit=5, user_id="alice")

    prompt = pick.await_args.args[1]
    assert "I love rainy nights" in prompt and "Rain again here" not in prompt
    assert [e["user_content"] for e in out] == ["The rain stopped", "I love rainy nights"]
    assert [e["score"] for e in out] == [1.0, 0.5]
    assert out[1]["importance"] == pytest.approx(0.7)
    assert out[1]["topics"] == ["rain"] and out[1]["mood"] == "warm"
    assert out[0]["created_at"]  # ISO timestamp with an offset
    ranked = await m.recall_exchanges_with_recency("rain", limit=1, user_id="alice")
    assert len(ranked) == 1
