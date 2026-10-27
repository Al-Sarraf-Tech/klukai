"""Explicit real-backend chaos gate; missing opt-in or Docker fails closed.

Run KLUKAI_RUNTIME_BACKENDS=1 KLUKAI_RUNTIME_EVIDENCE=<directory>
python -m pytest tests/runtime_backends.py -q. The supporting fixtures launch
only pinned disposable PostgreSQL and Redis-compatible Valkey. Memory recall
runs against the real PostgreSQL with every migration applied: full-text
candidates, then a Claude Haiku pick that is faked here, so nothing is sent.
"""

from __future__ import annotations
import asyncio
from datetime import datetime
import json
from pathlib import Path
import time
import uuid
import pytest
from app import auth, db, extraction_llm
from app.circuit_breakers import State, get_breaker
from app.memory import SessionUnavailable
from app.migrations import apply_migrations
from app.models import SessionState
from app.observability import health_cache
from tests.runtime_backend_support import (
    backends as backends,
    clients as clients,
    evidence,
)

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

HARBOUR = "We watched the harbour lighthouse at dusk and talked about the sea."
LOGBOOK = "Klukai read the old lighthouse keeper's logbook from the archive."
REACTOR = "The Commander repaired the reactor coolant pump on the night shift."
ELSEWHERE = "Another commander's lighthouse at the harbour, never shared."
# (summary, keywords, importance): two full-text matches, one non-match that
# is the most important row, and another user's row that matches best.
EPISODES = (
    (HARBOUR, ["lighthouse", "harbour"], 0.4),
    (LOGBOOK, ["lighthouse", "archive"], 0.6),
    (REACTOR, ["reactor", "maintenance"], 0.9),
)
LAMP = (
    "Tell me about the harbour lighthouse again.",
    "The lamp turns every nine seconds.",
)
PUMP = ("How is the coolant pump holding up?", "Steady since the night shift.")
EXCHANGES = (
    (*LAMP, ["lighthouse", "harbour"], 0.5),
    (*PUMP, ["reactor"], 0.8),
)
QUERY = "Do you remember the lighthouse at the harbour?"


class FakeHaiku:
    """Stands in for extraction_llm.complete: records each request, sends nothing.

    Each reply is the visible text Haiku would return, or an exception that
    extraction_llm raises (no key, cap reached) before anything is sent.
    """

    def __init__(self, *replies: str | Exception) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self, prompt: str, *, max_tokens: int, route: str, system: str | None = None
    ) -> str:
        self.calls.append({"route": route, "prompt": prompt, "max_tokens": max_tokens})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def unavailable() -> extraction_llm.ExtractionUnavailable:
    return extraction_llm.ExtractionUnavailable("Haiku extraction has no API key")


def candidates(prompt: object) -> list[str]:
    """The candidate texts of one selection prompt, in the order Postgres sent."""
    assert isinstance(prompt, str)
    listing = prompt.split("Memories:\n", 1)[1].split("\n\n", 1)[0]
    rows = [json.loads(line) for line in listing.splitlines()]
    assert [row["id"] for row in rows] == [str(n) for n in range(1, len(rows) + 1)]
    return [row["text"] for row in rows]


def ranked(texts: list[str], expected: list[str]) -> bool:
    return len(texts) == len(expected) and all(
        want in text for text, want in zip(texts, expected, strict=True)
    )


async def store_corpus(memory, owner: str, other: str) -> None:
    """Insert-only writes through MemoryManager, exactly as the chat path does."""
    for summary, keywords, importance in EPISODES:
        episode_id = str(uuid.uuid4())
        stored = await memory.store_episode(
            episode_id, summary, keywords, ["calm"], importance, user_id=owner
        )
        assert stored == episode_id
    await memory.store_episode(
        str(uuid.uuid4()), ELSEWHERE, ["lighthouse", "harbour"], [], 1.0, user_id=other
    )
    for user_content, assistant_content, topics, importance in EXCHANGES:
        await memory.store_exchange(
            str(uuid.uuid4()),
            user_content,
            assistant_content,
            topics,
            importance=importance,
            user_id=owner,
        )
    await memory.store_exchange(
        str(uuid.uuid4()), ELSEWHERE, "Not yours.", ["lighthouse"], user_id=other
    )


# Row counts and generated full-text column (migration 260) matches.
STORED_ROWS = {
    "companion_episodes": (
        "SELECT count(*), count(*) FILTER (WHERE search_tsv @@ "
        "websearch_to_tsquery('english', 'lighthouse')) "
        "FROM companion_episodes WHERE user_id = %s"
    ),
    "companion_exchanges": (
        "SELECT count(*), count(*) FILTER (WHERE search_tsv @@ "
        "websearch_to_tsquery('english', 'lighthouse')) "
        "FROM companion_exchanges WHERE user_id = %s"
    ),
}


async def stored_rows(owner: str) -> dict[str, int]:
    """What the writes left in Postgres, read back directly."""
    counts = {}
    async with db.get_conn() as connection:
        for table, query in STORED_ROWS.items():
            row = await (await connection.execute(query, (owner,))).fetchone()
            assert row is not None
            counts[table] = int(row[0])
            counts[table + ":lighthouse"] = int(row[1])
    return counts


@pytest.mark.asyncio
async def test_postgres_auth_failure_and_real_protocol_recovery(backends, clients):
    _, proxies = clients
    proxy = proxies["postgres"]
    breaker = get_breaker("postgres")
    events = [
        {
            "phase": "healthy",
            "state": breaker.state.value,
            "monotonic": time.monotonic(),
        }
    ]
    assert (await health_cache.get_fresh_health())["status"] == "ok"
    await proxy.cut()
    assert (await db.check_health())["status"] == "error"
    assert breaker.state is State.OPEN
    events.append(
        {"phase": "cut", "state": breaker.state.value, "monotonic": time.monotonic()}
    )
    assert await auth.get_user_from_token("not-a-production-token") is None
    assert (await health_cache.get_fresh_health())["status"] == "unhealthy"
    proxy.available = True
    breaker.cfg.probe_interval_seconds = 0
    deadline = time.monotonic() + 10
    while (await db.check_health())["status"] != "ok":
        assert time.monotonic() < deadline
        await asyncio.sleep(0.1)
    assert breaker.state is State.CLOSED
    assert (await health_cache.get_fresh_health())["status"] == "ok"
    events.append(
        {
            "phase": "recovered",
            "state": breaker.state.value,
            "monotonic": time.monotonic(),
        }
    )
    evidence(backends, "postgres", events)


@pytest.mark.asyncio
async def test_redis_session_failure_closed_and_preserved_owner_recovery(
    backends, clients
):
    memory, proxies = clients
    breaker = get_breaker("redis")
    state = SessionState(conversation_id="runtime-private-owner")
    await memory.save_session("runtime-private-owner", state)
    assert (
        await memory.get_session("runtime-private-owner")
    ).conversation_id == "runtime-private-owner"
    events = [
        {
            "phase": "healthy",
            "state": breaker.state.value,
            "monotonic": time.monotonic(),
        }
    ]
    await proxies["redis"].cut()
    with pytest.raises(SessionUnavailable):
        await memory.get_session("runtime-private-owner")
    assert breaker.state is State.OPEN
    events.append(
        {"phase": "cut", "state": breaker.state.value, "monotonic": time.monotonic()}
    )
    count = proxies["redis"].accepted
    with pytest.raises(SessionUnavailable):
        await memory.get_session("runtime-private-owner")
    assert proxies["redis"].accepted == count
    assert (await health_cache.get_fresh_health())["status"] == "unhealthy"
    proxies["redis"].available = True
    breaker.cfg.probe_interval_seconds = 0
    restored = await memory.get_session("runtime-private-owner")
    assert restored.conversation_id == state.conversation_id
    assert breaker.state is State.CLOSED
    assert (await health_cache.get_fresh_health())["status"] == "ok"
    events.append(
        {
            "phase": "recovered",
            "state": breaker.state.value,
            "monotonic": time.monotonic(),
        }
    )
    evidence(backends, "redis", events)


@pytest.mark.asyncio
async def test_postgres_memory_recall_full_text_candidates_and_haiku_pick(
    backends, clients, monkeypatch
):
    memory, _ = clients
    applied = await apply_migrations(db.get_pool(), MIGRATIONS)
    assert "260_memory_full_text_search.sql" in applied
    owner, other = "recall-pick-owner", "recall-pick-other"
    await store_corpus(memory, owner, other)
    # Storing writes only Postgres rows; migration 260 fills search_tsv.
    assert await stored_rows(owner) == {
        "companion_episodes": 3,
        "companion_episodes:lighthouse": 2,
        "companion_exchanges": 2,
        "companion_exchanges:lighthouse": 1,
    }
    haiku = FakeHaiku(
        '{"ids": ["2", "1"]}',
        '```json\n{"ids": [1]}\n```',
        '{"ids": ["1"]}',
        '{"ids": []}',
    )
    monkeypatch.setattr(extraction_llm, "complete", haiku)
    events: list[dict[str, object]] = []

    episodes = await memory.recall_episodes(QUERY, limit=2, user_id=owner)
    # Haiku's order wins over the Postgres ranking; scores follow its rank.
    assert [e["summary"] for e in episodes] == [LOGBOOK, HARBOUR]
    assert [e["score"] for e in episodes] == [1.0, 0.5]
    assert episodes[0]["keywords"] == ["lighthouse", "archive"]
    assert episodes[0]["emotion_tags"] == ["calm"]
    call = haiku.calls[0]
    assert call["route"] == "memory_recall_episodes"
    # Postgres sent only this user's rows: full-text matches first (more
    # matched words rank higher), then the non-match despite its importance.
    assert ranked(candidates(call["prompt"]), [HARBOUR, LOGBOOK, REACTOR])
    assert ELSEWHERE not in str(call["prompt"])
    events.append(
        {
            "phase": "episodes_picked",
            "candidates": 3,
            "picked": [2, 1],
            "monotonic": time.monotonic(),
        }
    )

    exchanges = await memory.recall_exchanges(
        "lighthouse harbour", limit=3, user_id=owner
    )
    assert len(exchanges) == 1
    exchange = exchanges[0]
    assert (exchange["user_content"], exchange["assistant_content"]) == LAMP
    assert exchange["topics"] == ["lighthouse", "harbour"]
    assert exchange["score"] == 1.0 and exchange["importance"] == 0.5
    assert datetime.fromisoformat(exchange["created_at"]).tzinfo is not None
    call = haiku.calls[1]
    assert call["route"] == "memory_recall_exchanges"
    assert ranked(candidates(call["prompt"]), [LAMP[0], PUMP[0]])
    events.append(
        {
            "phase": "exchanges_picked",
            "candidates": 2,
            "picked": [1],
            "monotonic": time.monotonic(),
        }
    )

    # Another user's recall sees only that user's row.
    theirs = await memory.recall_episodes("lighthouse", user_id=other)
    assert [e["summary"] for e in theirs] == [ELSEWHERE]
    assert ranked(candidates(haiku.calls[2]["prompt"]), [ELSEWHERE])
    # An explicit empty pick means nothing is relevant: no Postgres fallback.
    assert await memory.recall_episodes("the sea", user_id=owner) == []
    # A user with no rows never reaches Haiku.
    assert await memory.recall_episodes(QUERY, user_id="recall-nobody") == []
    assert len(haiku.calls) == 4 and not haiku.replies
    events.append(
        {"phase": "scoped", "calls": len(haiku.calls), "monotonic": time.monotonic()}
    )
    evidence(backends, "memory-recall", events, breaker="postgres")


@pytest.mark.asyncio
async def test_postgres_memory_recall_without_haiku_keeps_postgres_order(
    backends, clients, monkeypatch
):
    memory, _ = clients
    await apply_migrations(db.get_pool(), MIGRATIONS)
    owner, other = "recall-fallback-owner", "recall-fallback-other"
    await store_corpus(memory, owner, other)
    haiku = FakeHaiku(
        unavailable(),
        '{"ids": ["9", "x"]}',
        '{"answer": "the lighthouse"}',
        unavailable(),
        unavailable(),
    )
    monkeypatch.setattr(extraction_llm, "complete", haiku)
    events: list[dict[str, object]] = []

    # Haiku unavailable (no key or capped): json_object returns None.
    episodes = await memory.recall_episodes(QUERY, limit=2, user_id=owner)
    assert [e["summary"] for e in episodes] == [HARBOUR, LOGBOOK]
    assert [e["score"] for e in episodes] == [1.0, 0.5]
    # Only unknown ids, or no id list at all: unusable, so Postgres order.
    for _ in range(2):
        episodes = await memory.recall_episodes(QUERY, limit=3, user_id=owner)
        assert [e["summary"] for e in episodes] == [HARBOUR, LOGBOOK, REACTOR]
    events.append(
        {
            "phase": "episodes_postgres_order",
            "outcomes": ["unavailable", "unknown_ids", "no_ids"],
            "monotonic": time.monotonic(),
        }
    )
    # No searchable word: importance, then recency, still Postgres-only.
    episodes = await memory.recall_episodes("?!", limit=3, user_id=owner)
    assert [e["summary"] for e in episodes] == [REACTOR, LOGBOOK, HARBOUR]
    assert ranked(candidates(haiku.calls[3]["prompt"]), [REACTOR, LOGBOOK, HARBOUR])
    exchanges = await memory.recall_exchanges(
        "lighthouse harbour", limit=5, user_id=owner
    )
    assert [x["user_content"] for x in exchanges] == [LAMP[0], PUMP[0]]
    assert [x["score"] for x in exchanges] == [1.0, 0.5]
    assert [c["route"] for c in haiku.calls] == ["memory_recall_episodes"] * 4 + [
        "memory_recall_exchanges"
    ]
    assert not haiku.replies
    events.append(
        {
            "phase": "recent_and_exchanges_postgres_order",
            "calls": len(haiku.calls),
            "monotonic": time.monotonic(),
        }
    )
    evidence(backends, "memory-recall-fallback", events, breaker="postgres")


@pytest.mark.asyncio
async def test_postgres_memory_loss_fails_open_and_recall_recovers(
    backends, clients, monkeypatch
):
    memory, proxies = clients
    proxy = proxies["postgres"]
    breaker = get_breaker("postgres")
    await apply_migrations(db.get_pool(), MIGRATIONS)
    owner = "recall-recovery-owner"

    async def no_haiku(prompt, *, max_tokens, route, system=None):
        raise unavailable()

    monkeypatch.setattr(extraction_llm, "complete", no_haiku)
    first = str(uuid.uuid4())
    await memory.store_episode(
        first, HARBOUR, ["lighthouse", "harbour"], [], 0.4, user_id=owner
    )
    await memory.store_exchange(str(uuid.uuid4()), *LAMP, ["lighthouse"], user_id=owner)
    assert [
        e["summary"] for e in await memory.recall_episodes(QUERY, user_id=owner)
    ] == [HARBOUR]
    events = [
        {
            "phase": "healthy",
            "state": breaker.state.value,
            "monotonic": time.monotonic(),
        }
    ]

    await proxy.cut()
    # A lost database degrades context; it never raises into the chat path.
    lost = str(uuid.uuid4())
    assert (
        await memory.store_episode(
            lost, LOGBOOK, ["lighthouse"], [], 0.6, user_id=owner
        )
        == lost
    )
    assert await memory.recall_episodes(QUERY, user_id=owner) == []
    assert breaker.state is State.OPEN
    assert await memory.recall_exchanges(QUERY, user_id=owner) == []
    events.append(
        {"phase": "cut", "state": breaker.state.value, "monotonic": time.monotonic()}
    )

    proxy.available = True
    breaker.cfg.probe_interval_seconds = 0
    deadline = time.monotonic() + 10
    while (await db.check_health())["status"] != "ok":
        assert time.monotonic() < deadline
        await asyncio.sleep(0.1)
    assert breaker.state is State.CLOSED
    # What was stored before the loss is recalled again; the write attempted
    # during it was dropped whole, and the next write lands.
    assert [
        e["summary"] for e in await memory.recall_episodes(QUERY, user_id=owner)
    ] == [HARBOUR]
    assert [
        x["user_content"] for x in await memory.recall_exchanges(QUERY, user_id=owner)
    ] == [LAMP[0]]
    await memory.store_episode(lost, LOGBOOK, ["lighthouse"], [], 0.6, user_id=owner)
    assert [
        e["summary"] for e in await memory.recall_episodes(QUERY, user_id=owner)
    ] == [
        HARBOUR,
        LOGBOOK,
    ]
    events.append(
        {
            "phase": "recovered",
            "state": breaker.state.value,
            "monotonic": time.monotonic(),
        }
    )
    evidence(backends, "memory-recall-recovery", events, breaker="postgres")


@pytest.mark.asyncio
async def test_real_query_cancellation_releases_pool(backends, clients):
    entered = asyncio.Event()

    async def slow_query():
        async with db.get_conn() as connection:
            entered.set()
            await connection.execute("SELECT pg_sleep(30)")

    task = asyncio.create_task(slow_query())
    await entered.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert (await db.check_health())["status"] == "ok"
    assert get_breaker("postgres").state is State.CLOSED
    assert db.get_pool().get_stats().get("requests_waiting", 0) == 0
    evidence(
        backends,
        "cancellation",
        [{"phase": "cancelled", "pool_reusable": True, "monotonic": time.monotonic()}],
    )


@pytest.mark.asyncio
async def test_postgres_fact_memory_keeps_the_data_service_contract(
    backends, clients, monkeypatch
):
    """Facts moved from the aichat data service to Klukai's own queries on the
    same ``memory`` table (app/fact_store.py). On a real PostgreSQL this proves
    the old service's contract: the table is created when missing, rows
    written before the move are read in place, store is an upsert, a TTL hides
    an expired row without deleting it, a store without TTL makes a row
    permanent again, and pattern recall is scoped, newest first and bounded."""
    from app import fact_store

    memory, _ = clients
    monkeypatch.setattr(fact_store, "_ready", False)
    owner, other = "facts-owner", "facts-other"
    # A fresh database has no memory table; first use creates the same schema
    # the data service created, and nothing else.
    await memory.store_fact("rel:birthday", "March 5", user_id=owner)
    async with db.get_conn() as conn:
        columns = await (
            await conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'memory' ORDER BY ordinal_position"
            )
        ).fetchall()
    assert columns == [
        ("key", "text"),
        ("value", "text"),
        ("category", "text"),
        ("ts", "timestamp with time zone"),
        ("expires_at", "timestamp with time zone"),
    ]
    # A row the data service wrote earlier, exactly as it wrote it.
    async with db.get_conn_autocommit() as conn:
        await conn.execute(
            "INSERT INTO memory(key, value, ts) VALUES(%s, %s, NOW() - INTERVAL '1 day')",
            (f"companion:{owner}:rel:joke:tea", "tea :: always tea"),
        )
    assert await memory.recall_fact("rel:birthday", user_id=owner) == "March 5"
    assert await memory.get_inside_jokes(owner) == [
        {"label": "tea", "note": "always tea"}
    ]
    # Upsert keeps one row and the newest value.
    await memory.store_fact("rel:birthday", "March 6", user_id=owner)
    assert await memory.recall_fact("rel:birthday", user_id=owner) == "March 6"
    # Another user's facts never leak.
    await memory.store_fact("rel:birthday", "June 1", user_id=other)
    assert await memory.get_relationship_facts(owner) == {"birthday": "March 6"}
    # Milestones are recorded once.
    assert await memory.record_milestone("first_tea", user_id=owner) is True
    assert await memory.record_milestone("first_tea", user_id=owner) is False
    # A TTL hides the row once expired; the row itself stays.
    await memory.store_fact("mood", "sleepy", ttl=1, user_id=owner)
    assert await memory.recall_fact("mood", user_id=owner) == "sleepy"
    await asyncio.sleep(1.2)
    assert await memory.recall_fact("mood", user_id=owner) is None
    # Storing again without a TTL makes it permanent.
    await memory.store_fact("mood", "awake", user_id=owner)
    await asyncio.sleep(0.1)
    assert await memory.recall_fact("mood", user_id=owner) == "awake"
    # Pattern recall: newest first, bounded like the data service (50).
    for i in range(55):
        await fact_store.store(f"companion:{owner}:note:{i:02d}", str(i))
    notes = await memory.recall_facts_by_pattern("note:%", user_id=owner)
    assert len(notes) == fact_store.PATTERN_LIMIT == 50
    assert notes[0]["key"].endswith("note:54")
    async with db.get_conn() as conn:
        counts = await (
            await conn.execute(
                "SELECT count(*), count(expires_at) FROM memory WHERE key LIKE %s",
                (f"companion:{owner}:%",),
            )
        ).fetchone()
    # birthday, tea, milestone, mood and 55 notes: nothing was deleted.
    assert counts == (59, 0)

    # An existing table (production: created by the data service) is used as
    # is: concurrent first use by fresh processes changes no schema object.
    async def schema() -> list[tuple[object, ...]]:
        async with db.get_conn() as conn:
            return await (
                await conn.execute(
                    "SELECT c.oid::bigint, c.relname FROM pg_class c "
                    "WHERE c.relname IN ('memory', 'memory_pkey', "
                    "'idx_memory_category', 'idx_memory_expires') ORDER BY 2"
                )
            ).fetchall()

    before = await schema()
    assert [row[1] for row in before] == [
        "idx_memory_category",
        "idx_memory_expires",
        "memory",
        "memory_pkey",
    ]
    monkeypatch.setattr(fact_store, "_ready", False)
    await asyncio.gather(*(fact_store._ensure_table() for _ in range(4)))
    assert await schema() == before
    # TTL 0 or None stores a permanent fact.
    await memory.store_fact("zero", "z", ttl=0, user_id=owner)
    await memory.store_fact("none", "n", ttl=None, user_id=owner)
    async with db.get_conn() as conn:
        permanent = await (
            await conn.execute(
                "SELECT count(*) FROM memory WHERE key = ANY(%s) AND expires_at IS NULL",
                ([f"companion:{owner}:zero", f"companion:{owner}:none"],),
            )
        ).fetchone()
    assert permanent == (2,)
    # LIKE metacharacters in a user id match only that user's rows.
    await memory.store_fact("rel:secret", "theirs", user_id="axb")
    await memory.store_fact("rel:mine", "ours", user_id="a_b")
    assert await memory.get_relationship_facts("a_b") == {"mine": "ours"}
    assert await memory.get_relationship_facts("%") == {}
    # Concurrent firsts: exactly one caller records the milestone.
    results = await asyncio.gather(
        *(memory.record_milestone("first_storm", user_id=owner) for _ in range(5))
    )
    assert sorted(results) == [False, False, False, False, True]
