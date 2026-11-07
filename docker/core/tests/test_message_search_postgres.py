"""Real PostgreSQL proof, exclusively in disposable random test schemas.

KLUKAI_MIGRATION_TEST_DATABASE_URL must identify a disposable database.
No application database URL is read by this fixture.
"""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI
import httpx
import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool
import pytest

from app import db
from app.migrations import apply_migrations

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
STAMP = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def mid(n):
    return str(UUID(int=n))


@pytest.fixture
async def message_db(monkeypatch):
    url = os.environ.get("KLUKAI_MIGRATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set KLUKAI_MIGRATION_TEST_DATABASE_URL to a disposable PostgreSQL database")
    schema = "message_search_test_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            async with AsyncConnectionPool(url, min_size=1, max_size=1, open=False,
                    kwargs={"options": f"-c search_path={schema}"}) as pool:
                await pool.wait()
                await apply_migrations(pool, MIGRATIONS)
                monkeypatch.setattr(db, "_pool", pool)
                async with pool.connection() as conn:
                    await conn.execute("INSERT INTO companion_conversations (id,user_id) VALUES (%s,'alice'),(%s,'alice'),(%s,'bob')", (mid(1), mid(2), mid(3)))
                yield pool
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


async def insert(pool, n, content="rain", *, role="user", user="alice", conv=1, stamp=STAMP):
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO companion_messages (id,conversation_id,user_id,role,content,created_at) VALUES (%s,%s,%s,%s,%s,%s)", (mid(n), mid(conv), user, role, content, stamp))


async def test_search_reaches_old_originals_and_isolates_roles_and_users(message_db):
    from app.message_search import SearchRequest, search_messages

    await insert(message_db, 10, "Rare original saved over 200 turns ago")
    async with message_db.connection() as conn:
        await conn.execute("INSERT INTO companion_messages (conversation_id,user_id,role,content,created_at) SELECT %s,'alice','user','newer filler',%s + n*interval '1 second' FROM generate_series(1,250) n", (mid(1), STAMP))
    await insert(message_db, 11, "rare private", user="bob", conv=3)
    await insert(message_db, 12, "rare system", role="system")
    await insert(message_db, 13, "rare second conversation", conv=2, role="assistant")
    result = await search_messages("alice", SearchRequest(query=" RARE "))
    assert [m["id"] for m in result["messages"]] == [mid(13), mid(10)]
    assert result["messages"][1] == {"id": mid(10), "role": "user", "excerpt": "Rare original saved over 200 turns ago", "created_at": "2026-01-01T12:00:00+00:00"}
    assert result["next_cursor"] is None


@pytest.mark.parametrize("query,wanted", [
    ("100%", [10]), ("a_b", [12]), (r"c:\temp", [14]),
    ("O'Reilly", [16]), ("東京", [18]), ("CAFÉ", [20]),
    ("🌧️雨", [22]), ("%_\\'", [24]), ("zz", []),
])
async def test_literal_special_characters_and_multilingual_text(message_db, query, wanted):
    from app.message_search import SearchRequest, search_messages

    for n, text in [(10, "100% sure"), (11, "1000 sure"), (12, "a_b literal"), (13, "acb decoy"),
                    (14, r"c:\temp"), (15, "c:temp"), (16, "O'Reilly"), (17, "OReilly"),
                    (18, "東京の夜"), (19, "大阪の夜"), (20, "café"), (21, "cafe"),
                    (22, "🌧️雨です"), (23, "🌦️雲です"), (24, "%_\\' combined")]:
        await insert(message_db, n, text)
    out = await search_messages("alice", SearchRequest(query=query))
    assert [m["id"] for m in out["messages"]] == [mid(n) for n in wanted]


async def test_combined_time_and_speaker_bounds_are_inclusive_exclusive(message_db):
    from app.message_search import SearchRequest, search_messages

    await insert(message_db, 10, role="assistant", stamp=STAMP - timedelta(microseconds=1))
    await insert(message_db, 11, role="assistant")
    await insert(message_db, 12, role="user", stamp=STAMP + timedelta(minutes=1))
    await insert(message_db, 13, role="assistant", stamp=STAMP + timedelta(hours=1))
    out = await search_messages("alice", SearchRequest(query="rain", speaker="assistant",
        start="2026-01-01T14:00:00+02:00", end="2026-01-01T15:00:00+02:00"))
    assert [m["id"] for m in out["messages"]] == [mid(11)]


async def test_search_keysets_traverse_ties_and_ignore_new_arrivals(message_db):
    from app.message_search import SearchRequest, search_messages

    for n in range(10, 63):
        await insert(message_db, n, stamp=STAMP + timedelta(microseconds=(n - 10) // 30))
    first = await search_messages("alice", SearchRequest(query="rain"))
    assert len(first["messages"]) == 20
    assert first["next_cursor"] == {"created_at": first["messages"][-1]["created_at"], "id": first["messages"][-1]["id"]}
    next_request = SearchRequest(query="rain", cursor=first["next_cursor"])
    before_insert = await search_messages("alice", next_request)
    await insert(message_db, 1000, stamp=STAMP + timedelta(days=1))
    assert await search_messages("alice", next_request) == before_insert
    collected = [m["id"] for m in first["messages"]]
    cursor = first["next_cursor"]
    while cursor:
        out = await search_messages("alice", SearchRequest(query="rain", cursor=cursor))
        collected.extend(m["id"] for m in out["messages"])
        cursor = out["next_cursor"]
    assert collected == [mid(n) for n in range(62, 9, -1)]


async def test_context_all_pages_are_exact_scoped_and_without_duplicates(message_db):
    from app.message_search import ContextRequest, get_message_context

    for n in range(10, 111):
        await insert(message_db, n, f"original {n}\n東京🌧️", role="user" if n % 2 else "assistant",
                     stamp=STAMP + timedelta(microseconds=(n - 10) // 35))
    await insert(message_db, 200, "other conversation", conv=2)
    await insert(message_db, 201, "other user sharing conversation id", user="bob")
    await insert(message_db, 202, "hidden system", role="system")
    out = await get_message_context("alice", ContextRequest(anchor_id=mid(60)))
    assert out["anchor_id"] == mid(60)
    assert [m["id"] for m in out["messages"]] == [mid(n) for n in range(50, 71)]
    assert out["messages"][10]["content"] == "original 60\n東京🌧️"
    assert out["older_cursor"]["id"] == mid(50) and out["newer_cursor"]["id"] == mid(70)
    collected = [m["id"] for m in out["messages"]]
    for direction, edge in [("before", "older_cursor"), ("after", "newer_cursor")]:
        cursor = out[edge]
        for _ in range(10):
            if cursor is None:
                break
            page = await get_message_context("alice", ContextRequest(anchor_id=mid(60), **{direction: cursor}))
            ids = [m["id"] for m in page["messages"]]
            assert len(ids) <= 20 and not set(ids).intersection(collected)
            assert ids == sorted(ids)
            collected = ids + collected if direction == "before" else collected + ids
            cursor = page[edge]
        else:
            pytest.fail("Context cursor did not terminate")
    assert collected == [mid(n) for n in range(10, 111)]
    # A foreign-conversation cursor never widens the anchor's scope.
    foreign = await get_message_context("alice", ContextRequest(anchor_id=mid(60), after={"created_at": STAMP.isoformat(), "id": mid(200)}))
    assert all(m["content"].startswith("original ") for m in foreign["messages"])


@pytest.mark.parametrize("anchor", [999, 200, 201])
async def test_missing_other_user_and_system_anchors_are_not_found(message_db, anchor):
    from app.message_search import ContextRequest, get_message_context

    await insert(message_db, 200, user="bob", conv=3)
    await insert(message_db, 201, role="system")
    assert await get_message_context("alice", ContextRequest(anchor_id=mid(anchor))) is None


async def test_single_message_context_has_no_edge_cursors(message_db):
    from app.message_search import ContextRequest, get_message_context

    await insert(message_db, 10, "all original content")
    out = await get_message_context("alice", ContextRequest(anchor_id=mid(10)))
    assert [m["id"] for m in out["messages"]] == [mid(10)]
    assert out["older_cursor"] is None and out["newer_cursor"] is None


async def test_statement_timeout_is_local_and_cancelled_work_returns_503(message_db):
    from app import routes_message_search

    async def resolve(_request):
        return "alice"

    # An exclusive lock makes even a two-character query wait on real SQL.
    async with message_db.connection() as conn:
        original = (await (await conn.execute("SHOW statement_timeout")).fetchone())[0]
    url = os.environ["KLUKAI_MIGRATION_TEST_DATABASE_URL"]
    async with message_db.connection() as conn:
        schema = (await (await conn.execute("SELECT current_schema()")).fetchone())[0]
    async with await psycopg.AsyncConnection.connect(url) as blocker:
        await blocker.execute(sql.SQL("LOCK TABLE {}.companion_messages IN ACCESS EXCLUSIVE MODE").format(sql.Identifier(schema)))
        app = FastAPI()
        routes_message_search.register_message_search(app, resolve)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/messages/search", json={"query": "zz"})
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        await blocker.rollback()
    async with message_db.connection() as conn:
        after = (await (await conn.execute("SHOW statement_timeout")).fetchone())[0]
        assert after == original
        assert (await (await conn.execute("SELECT 1")).fetchone())[0] == 1


async def test_additive_index_migration_is_idempotent_and_preserves_rows(message_db):
    await insert(message_db, 10, "unchanged original")
    async with message_db.connection() as conn:
        extension = await (await conn.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='pg_trgm'" )).fetchone()
        assert extension == ("public",)
        indexes = await (await conn.execute("SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND tablename='companion_messages'" )).fetchall()
        assert any("gin (content public.gin_trgm_ops)" in definition for (definition,) in indexes)
    assert await apply_migrations(message_db, MIGRATIONS) == []
    async with message_db.connection() as conn:
        assert await (await conn.execute("SELECT content FROM companion_messages WHERE id=%s", (mid(10),))).fetchone() == ("unchanged original",)


async def test_excerpt_centers_the_postgres_match_after_unicode_case_decoys(message_db):
    from app.message_search import SearchRequest, search_messages

    # Python regex IGNORECASE considers ſ equivalent to s; PostgreSQL ILIKE
    # does not. The saved ss match must still be visible far past that decoy.
    content = "ſſ" + "x" * 600 + "SS" + "y" * 500
    await insert(message_db, 10, content)
    result = await search_messages("alice", SearchRequest(query="ss"))
    assert result["messages"][0]["excerpt"] == "…" + "x" * 80 + "SS" + "y" * 318 + "…"
