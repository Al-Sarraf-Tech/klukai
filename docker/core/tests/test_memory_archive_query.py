"""Unit tests for app.memory_archive_query — the archive read/CRUD surface.

Every external dependency is mocked: the Postgres pool is replaced by an
in-process fake connection, and the image volume is redirected at a tmp dir.
Nothing here needs a live stack.

The module is the read path behind the memory album, `/api/memories/*`, and
her recall ("do you remember when..."), so the tests assert on *behaviour*:
which rows come back, how filters are bound, and — importantly — which
failures are swallowed versus surfaced. The archive is SACRED data; a silent
empty list where an outage should have surfaced is a real bug class here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# `app.memory_archive` MUST be imported first: memory_archive_query imports from
# it and it re-exports back, so importing the query module cold raises
# ImportError on the partially-initialised cycle.
from app import memory_archive
from app import memory_archive_query as maq

# ═══════════════════════════════════════════════════════════════════════════
# Fake DB plumbing
# ═══════════════════════════════════════════════════════════════════════════


class FakeResult:
    """Stand-in for a psycopg cursor returned by `await conn.execute(...)`."""

    def __init__(self, rows: list, rowcount=None):
        self._rows = rows
        self.rowcount = rowcount

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """Async connection whose `execute` replays a queued list of row batches.

    `results` is consumed one batch per `execute()` call; once exhausted every
    further call yields an empty result set. Each call is recorded so tests can
    assert on the SQL shape and the *bound parameters* (never interpolation).
    """

    def __init__(self, results=None, rowcount=1, error: Exception | None = None):
        self._results = list(results) if results is not None else []
        self._rowcount = rowcount
        self._error = error
        self.calls: list[tuple[str, object]] = []

    @property
    def sqls(self) -> list[str]:
        return [c[0] for c in self.calls]

    @property
    def params(self) -> list:
        return [c[1] for c in self.calls]

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self._error is not None:
            raise self._error
        rows = self._results.pop(0) if self._results else []
        return FakeResult(rows, self._rowcount)


def _conn_ctx(conn: FakeConn):
    """Build a drop-in replacement for `get_conn` / `get_conn_autocommit`."""

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


def patch_conn(conn: FakeConn):
    return patch.object(maq, "get_conn", _conn_ctx(conn))


def patch_autocommit(conn: FakeConn):
    return patch.object(maq, "get_conn_autocommit", _conn_ctx(conn))


def broken_conn(exc: Exception | None = None):
    """Patch get_conn so acquiring a connection blows up (pool down)."""
    return patch.object(
        maq, "get_conn", MagicMock(side_effect=exc or RuntimeError("pool down"))
    )


def broken_autocommit(exc: Exception | None = None):
    return patch.object(
        maq,
        "get_conn_autocommit",
        MagicMock(side_effect=exc or RuntimeError("pool down")),
    )


WHEN = datetime(2026, 8, 6, 12, 30, tzinfo=timezone.utc)


def _list_row(**over):
    """A companion_memories row in the column order list_memories selects."""
    row = {
        "id": "mem-1",
        "filename": "a.png",
        "thumb_filename": "a_thumb.png",
        "annotation": "She kept this one.",
        "scene_tags": ["hangar", "night"],
        "mood": "tender",
        "affection_level": 7,
        "kept_by": "klukai",
        "category": "Precious Memories",
        "created_at": WHEN,
    }
    row.update(over)
    return tuple(row.values())


def _recall_row(**over):
    """A row in the column order the recall queries select."""
    row = {
        "id": "mem-9",
        "filename": "storm.png",
        "annotation": "The night we outran the storm.",
        "category": "Precious Memories",
        "scene_tags": ["storm", "bike"],
        "created_at": WHEN,
    }
    row.update(over)
    return tuple(row.values())


# ═══════════════════════════════════════════════════════════════════════════
# get_image_bytes
# ═══════════════════════════════════════════════════════════════════════════


class TestGetImageBytes:
    @pytest.mark.asyncio
    async def test_reads_bytes_from_the_volume(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"\x89PNG-full")
        conn = FakeConn(results=[[("a.png",)]])

        with patch.object(maq, "IMAGES_DIR", tmp_path), patch_conn(conn):
            data = await maq.get_image_bytes("mem-1", user_id="alice")

        assert data == b"\x89PNG-full"

    @pytest.mark.asyncio
    async def test_ownership_is_enforced_when_user_is_known(self, tmp_path):
        """A user must not be able to fetch another user's drawing by id."""
        img = tmp_path / "a.png"
        img.write_bytes(b"x")
        conn = FakeConn(results=[[("a.png",)]])

        with patch.object(maq, "IMAGES_DIR", tmp_path), patch_conn(conn):
            await maq.get_image_bytes("mem-1", user_id="alice")

        sql, params = conn.calls[0]
        assert "user_id = %s" in sql
        assert params == ("mem-1", "alice")

    @pytest.mark.asyncio
    async def test_internal_calls_may_omit_the_user(self, tmp_path):
        """Recall has no user context; it still has to resolve the file."""
        img = tmp_path / "a.png"
        img.write_bytes(b"x")
        conn = FakeConn(results=[[("a.png",)]])

        with patch.object(maq, "IMAGES_DIR", tmp_path), patch_conn(conn):
            data = await maq.get_image_bytes("mem-1")

        sql, params = conn.calls[0]
        assert "user_id" not in sql
        assert params == ("mem-1",)
        assert data == b"x"

    @pytest.mark.asyncio
    async def test_thumbnail_selects_the_thumb_column(self, tmp_path):
        thumb = tmp_path / "a_thumb.png"
        thumb.write_bytes(b"thumb")
        conn = FakeConn(results=[[("a_thumb.png",)]])

        with patch.object(maq, "IMAGES_DIR", tmp_path), patch_conn(conn):
            data = await maq.get_image_bytes("mem-1", thumbnail=True)

        assert "SELECT thumb_filename" in conn.sqls[0]
        assert data == b"thumb"

    @pytest.mark.asyncio
    async def test_full_size_selects_the_filename_column(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"full")
        conn = FakeConn(results=[[("a.png",)]])

        with patch.object(maq, "IMAGES_DIR", tmp_path), patch_conn(conn):
            await maq.get_image_bytes("mem-1", thumbnail=False)

        assert "SELECT filename" in conn.sqls[0]

    @pytest.mark.asyncio
    async def test_unknown_memory_returns_none(self, tmp_path):
        conn = FakeConn(results=[[]])
        with patch.object(maq, "IMAGES_DIR", tmp_path), patch_conn(conn):
            assert await maq.get_image_bytes("nope") is None

    @pytest.mark.asyncio
    async def test_row_with_null_filename_returns_none(self, tmp_path):
        """A memory row can exist before its image lands (thumb pending)."""
        conn = FakeConn(results=[[(None,)]])
        with patch.object(maq, "IMAGES_DIR", tmp_path), patch_conn(conn):
            assert await maq.get_image_bytes("mem-1", thumbnail=True) is None

    @pytest.mark.asyncio
    async def test_missing_file_on_disk_returns_none(self, tmp_path):
        """DB row survived but the volume lost the file — degrade, don't crash."""
        conn = FakeConn(results=[[("gone.png",)]])
        with patch.object(maq, "IMAGES_DIR", tmp_path), patch_conn(conn):
            assert await maq.get_image_bytes("mem-1") is None

    @pytest.mark.asyncio
    async def test_db_failure_returns_none(self, tmp_path):
        with patch.object(maq, "IMAGES_DIR", tmp_path), broken_conn():
            assert await maq.get_image_bytes("mem-1") is None

    @pytest.mark.asyncio
    async def test_query_failure_returns_none(self, tmp_path):
        conn = FakeConn(error=RuntimeError("relation missing"))
        with patch.object(maq, "IMAGES_DIR", tmp_path), patch_conn(conn):
            assert await maq.get_image_bytes("mem-1") is None


# ═══════════════════════════════════════════════════════════════════════════
# list_memories
# ═══════════════════════════════════════════════════════════════════════════


class TestListMemories:
    @pytest.mark.asyncio
    async def test_empty_archive_returns_empty_list(self):
        conn = FakeConn(results=[[]])
        with patch_conn(conn):
            assert await maq.list_memories() == []

    @pytest.mark.asyncio
    async def test_maps_every_column(self):
        conn = FakeConn(results=[[_list_row()]])
        with patch_conn(conn):
            out = await maq.list_memories(user_id="alice")

        assert out == [
            {
                "id": "mem-1",
                "filename": "a.png",
                "thumb_filename": "a_thumb.png",
                "annotation": "She kept this one.",
                "scene_tags": ["hangar", "night"],
                "mood": "tender",
                "affection_level": 7,
                "kept_by": "klukai",
                "category": "Precious Memories",
                "created_at": WHEN.isoformat(),
            }
        ]

    @pytest.mark.asyncio
    async def test_null_fields_are_normalised_for_the_client(self):
        """The Flutter album expects a string and a list, never null."""
        conn = FakeConn(
            results=[[_list_row(annotation=None, scene_tags=None, created_at=None)]]
        )
        with patch_conn(conn):
            out = await maq.list_memories()

        assert out[0]["annotation"] == ""
        assert out[0]["scene_tags"] == []
        assert out[0]["created_at"] is None

    @pytest.mark.asyncio
    async def test_id_is_stringified(self):
        """Postgres hands back a UUID object; the JSON layer needs a str."""
        import uuid

        uid = uuid.uuid4()
        conn = FakeConn(results=[[_list_row(id=uid)]])
        with patch_conn(conn):
            out = await maq.list_memories()

        assert out[0]["id"] == str(uid)

    @pytest.mark.asyncio
    async def test_only_kept_memories_scoped_to_the_user(self):
        conn = FakeConn(results=[[]])
        with patch_conn(conn):
            await maq.list_memories(user_id="alice", limit=5)

        sql, params = conn.calls[0]
        assert "kept = true" in sql
        assert "user_id = %s" in sql
        assert params == ("alice", 5)

    @pytest.mark.asyncio
    async def test_category_filter_is_bound_not_interpolated(self):
        conn = FakeConn(results=[[]])
        with patch_conn(conn):
            await maq.list_memories(category="Dreams", user_id="alice")

        sql, params = conn.calls[0]
        assert "category = %s" in sql
        assert "Dreams" not in sql
        assert params[:2] == ("alice", "Dreams")

    @pytest.mark.asyncio
    async def test_before_cursor_pages_backwards(self):
        conn = FakeConn(results=[[]])
        with patch_conn(conn):
            await maq.list_memories(before="2026-08-01", user_id="alice")

        sql, params = conn.calls[0]
        assert "created_at < %s" in sql
        assert params == ("alice", "2026-08-01", 20)

    @pytest.mark.asyncio
    async def test_month_filter_groups_by_year_month(self):
        conn = FakeConn(results=[[]])
        with patch_conn(conn):
            await maq.list_memories(month="2026-08", user_id="alice")

        sql, params = conn.calls[0]
        assert "to_char(created_at, 'YYYY-MM') = %s" in sql
        assert params == ("alice", "2026-08", 20)

    @pytest.mark.asyncio
    async def test_all_filters_combine_with_limit_bound_last(self):
        """Param order must track the predicate order or psycopg mis-binds."""
        conn = FakeConn(results=[[]])
        with patch_conn(conn):
            await maq.list_memories(
                category="Dreams",
                before="2026-08-01",
                month="2026-07",
                limit=3,
                user_id="alice",
            )

        sql, params = conn.calls[0]
        assert sql.count("AND") >= 3
        assert params == ("alice", "Dreams", "2026-08-01", "2026-07", 3)

    @pytest.mark.asyncio
    async def test_newest_first(self):
        conn = FakeConn(results=[[]])
        with patch_conn(conn):
            await maq.list_memories()
        assert "ORDER BY created_at DESC" in conn.sqls[0]

    @pytest.mark.asyncio
    async def test_db_failure_returns_empty_list(self):
        """Documented fail-open: the album renders empty rather than 500ing."""
        with broken_conn():
            assert await maq.list_memories() == []

    @pytest.mark.asyncio
    async def test_malformed_row_returns_empty_list(self):
        """A short row raises inside the comprehension; it must be swallowed."""
        conn = FakeConn(results=[[("only-one-column",)]])
        with patch_conn(conn):
            assert await maq.list_memories() == []


# ═══════════════════════════════════════════════════════════════════════════
# get_timeline
# ═══════════════════════════════════════════════════════════════════════════


class TestGetTimeline:
    @pytest.mark.asyncio
    async def test_returns_month_buckets_newest_first(self):
        conn = FakeConn(results=[[("2026-08", 12), ("2026-07", 4)]])
        with patch_conn(conn):
            out = await maq.get_timeline(user_id="alice")

        assert out == [
            {"month": "2026-08", "count": 12},
            {"month": "2026-07", "count": 4},
        ]
        assert conn.params[0] == ("alice",)
        assert "ORDER BY month DESC" in conn.sqls[0]

    @pytest.mark.asyncio
    async def test_empty_archive_returns_empty_timeline(self):
        conn = FakeConn(results=[[]])
        with patch_conn(conn):
            assert await maq.get_timeline() == []

    @pytest.mark.asyncio
    async def test_db_failure_propagates(self):
        """Fail closed: an outage must surface as 503, not as a wiped archive."""
        with broken_conn():
            with pytest.raises(RuntimeError):
                await maq.get_timeline()

    @pytest.mark.asyncio
    async def test_query_failure_propagates(self):
        conn = FakeConn(error=RuntimeError("syntax error"))
        with patch_conn(conn):
            with pytest.raises(RuntimeError):
                await maq.get_timeline()


# ═══════════════════════════════════════════════════════════════════════════
# get_categories
# ═══════════════════════════════════════════════════════════════════════════


class TestGetCategories:
    @pytest.mark.asyncio
    async def test_level_zero_sees_only_ungated_categories(self):
        conn = FakeConn(results=[[]])
        with patch_conn(conn):
            out = await maq.get_categories(0, user_id="alice")

        names = [c["name"] for c in out]
        assert "Precious Memories" not in names
        assert "The Commander" not in names
        assert names[-1] == "All"

    @pytest.mark.asyncio
    async def test_high_affection_unlocks_the_intimate_categories(self):
        conn = FakeConn(results=[[]])
        with patch_conn(conn):
            out = await maq.get_categories(9, user_id="alice")

        names = [c["name"] for c in out]
        assert "Precious Memories" in names
        assert "The Commander" in names
        assert "Quiet Hours" in names

    @pytest.mark.asyncio
    async def test_counts_are_attached_and_missing_ones_are_zero(self):
        conn = FakeConn(results=[[("Dreams", 7)]])
        with patch_conn(conn):
            out = await maq.get_categories(0, user_id="alice")

        by_name = {c["name"]: c["count"] for c in out}
        assert by_name["Dreams"] == 7
        assert by_name["Mission Records"] == 0

    @pytest.mark.asyncio
    async def test_all_totals_only_the_visible_categories(self):
        """A locked category's memories must not leak into the 'All' count —
        that would tell a level-0 user something exists they cannot see."""
        conn = FakeConn(results=[[("Dreams", 2), ("Precious Memories", 40)]])
        with patch_conn(conn):
            out = await maq.get_categories(0, user_id="alice")

        assert [c for c in out if c["name"] == "All"][0]["count"] == 2

    @pytest.mark.asyncio
    async def test_all_sums_every_unlocked_category(self):
        conn = FakeConn(results=[[("Dreams", 2), ("Precious Memories", 40)]])
        with patch_conn(conn):
            out = await maq.get_categories(9, user_id="alice")

        assert [c for c in out if c["name"] == "All"][0]["count"] == 42

    @pytest.mark.asyncio
    async def test_db_failure_propagates(self):
        """Fail closed, mirroring get_timeline."""
        with broken_conn():
            with pytest.raises(RuntimeError):
                await maq.get_categories(5, "alice")

    @pytest.mark.asyncio
    async def test_query_failure_propagates(self):
        conn = FakeConn(error=RuntimeError("boom"))
        with patch_conn(conn):
            with pytest.raises(RuntimeError):
                await maq.get_categories(5, "alice")


# ═══════════════════════════════════════════════════════════════════════════
# update_kept
# ═══════════════════════════════════════════════════════════════════════════


class TestUpdateKept:
    @pytest.mark.asyncio
    async def test_updates_scoped_to_the_owner(self):
        conn = FakeConn(rowcount=1)
        with patch_autocommit(conn):
            assert await maq.update_kept("mem-1", True, user_id="alice") is True

        sql, params = conn.calls[0]
        assert "WHERE id = %s AND user_id = %s" in sql
        assert params == (True, "commander", "mem-1", "alice")

    @pytest.mark.asyncio
    async def test_internal_call_without_user_skips_the_ownership_clause(self):
        conn = FakeConn(rowcount=1)
        with patch_autocommit(conn):
            assert await maq.update_kept("mem-1", False, kept_by="klukai") is True

        sql, params = conn.calls[0]
        assert "user_id" not in sql
        assert params == (False, "klukai", "mem-1")

    @pytest.mark.asyncio
    async def test_wrong_owner_reports_failure(self):
        """0 rows updated means the memory isn't hers to discard."""
        conn = FakeConn(rowcount=0)
        with patch_autocommit(conn):
            assert await maq.update_kept("mem-1", True, user_id="mallory") is False

    @pytest.mark.asyncio
    async def test_driver_without_rowcount_reports_failure(self):
        conn = FakeConn(rowcount=None)
        with patch_autocommit(conn):
            assert await maq.update_kept("mem-1", True) is False

    @pytest.mark.asyncio
    async def test_db_failure_returns_false(self):
        with broken_autocommit():
            assert await maq.update_kept("mem-1", True) is False

    @pytest.mark.asyncio
    async def test_query_failure_returns_false(self):
        conn = FakeConn(error=RuntimeError("deadlock"))
        with patch_autocommit(conn):
            assert await maq.update_kept("mem-1", True) is False


# ═══════════════════════════════════════════════════════════════════════════
# update_curation
# ═══════════════════════════════════════════════════════════════════════════


class TestUpdateCuration:
    @pytest.mark.asyncio
    async def test_writes_all_curation_fields_for_the_owner(self):
        curation = {
            "keep": True,
            "annotation": "I kept this one.",
            "category": "Precious Memories",
            "image_tags": ["hangar", "night"],
        }
        conn = FakeConn()
        with patch_autocommit(conn):
            ok = await maq.update_curation(
                "mem-1", curation, affection_level=9, user_id="alice"
            )

        assert ok is True
        sql, params = conn.calls[0]
        assert "WHERE id = %s AND user_id = %s" in sql
        assert params == (
            True,
            "I kept this one.",
            "Precious Memories",
            ["hangar", "night"],
            "mem-1",
            "alice",
        )

    @pytest.mark.asyncio
    async def test_internal_call_without_user_skips_the_ownership_clause(self):
        conn = FakeConn()
        with patch_autocommit(conn):
            ok = await maq.update_curation("mem-1", {"category": "Dreams"})

        assert ok is True
        sql, params = conn.calls[0]
        assert "user_id" not in sql
        assert params == (True, None, "Dreams", [], "mem-1")

    @pytest.mark.asyncio
    async def test_empty_curation_falls_back_to_safe_defaults(self):
        """A truncated LLM response must still leave a coherent row."""
        conn = FakeConn()
        with patch_autocommit(conn):
            await maq.update_curation("mem-1", {})

        kept, annotation, category, tags, _ = conn.params[0]
        assert kept is True
        assert annotation is None
        assert category == "Mission Records"
        assert tags == []

    @pytest.mark.asyncio
    async def test_discard_decision_is_honoured(self):
        conn = FakeConn()
        with patch_autocommit(conn):
            await maq.update_curation("mem-1", {"keep": False})

        assert conn.params[0][0] is False

    @pytest.mark.asyncio
    async def test_locked_category_is_downgraded_to_an_unlocked_one(self):
        """The LLM happily picks 'Precious Memories' at level 0; the archive
        must not surface a category the user hasn't earned."""
        conn = FakeConn()
        with patch_autocommit(conn):
            await maq.update_curation(
                "mem-1", {"category": "Precious Memories"}, affection_level=0
            )

        assert conn.params[0][2] == maq.available_categories(0)[-1]

    @pytest.mark.asyncio
    async def test_hallucinated_category_is_downgraded(self):
        conn = FakeConn()
        with patch_autocommit(conn):
            await maq.update_curation(
                "mem-1", {"category": "Forbidden Vault"}, affection_level=9
            )

        assert conn.params[0][2] in maq.available_categories(9)

    @pytest.mark.asyncio
    async def test_no_categories_at_all_falls_back_to_mission_records(self):
        """available_categories() returns [] below level 0, so the literal
        fallback is the only thing keeping the write valid."""
        conn = FakeConn()
        with patch_autocommit(conn):
            await maq.update_curation(
                "mem-1", {"category": "Whatever"}, affection_level=-1
            )

        assert conn.params[0][2] == "Mission Records"

    @pytest.mark.asyncio
    async def test_db_failure_returns_false(self):
        with broken_autocommit():
            assert await maq.update_curation("mem-1", {}) is False

    @pytest.mark.asyncio
    async def test_query_failure_returns_false(self):
        conn = FakeConn(error=RuntimeError("column missing"))
        with patch_autocommit(conn):
            assert await maq.update_curation("mem-1", {}) is False

    @pytest.mark.asyncio
    async def test_non_dict_curation_returns_false(self):
        assert await maq.update_curation("mem-1", None) is False


# ═══════════════════════════════════════════════════════════════════════════
# recall_memory
# ═══════════════════════════════════════════════════════════════════════════


def _pick_first(pool, weights, k):
    """Deterministic stand-in for random.choices — always the first option."""
    return [pool[0]]


class TestRecallByQuery:
    @pytest.mark.asyncio
    async def test_tag_hit_wins_immediately(self):
        conn = FakeConn(results=[[_recall_row()]])
        with patch_conn(conn):
            out = await maq.recall_memory("the storm run", "tender", 9, "alice")

        assert out["id"] == "mem-9"
        assert out["filename"] == "storm.png"
        assert out["category"] == "Precious Memories"
        assert out["scene_tags"] == ["storm", "bike"]
        assert out["created_at"] == WHEN.isoformat()
        # one query only — it stopped as soon as a tag matched
        assert len(conn.calls) == 1
        assert "ANY(scene_tags)" in conn.sqls[0]

    @pytest.mark.asyncio
    async def test_search_terms_are_lowercased_and_short_words_dropped(self):
        """'a'/'of' would match half the archive; only >2-char terms search."""
        conn = FakeConn(results=[[], [], [_recall_row()]])
        with patch_conn(conn):
            await maq.recall_memory("A of STORM Bike", "tender", 9, "alice")

        tag_terms = [p[1] for p in conn.params[:2]]
        assert tag_terms == ["storm", "bike"]

    @pytest.mark.asyncio
    async def test_falls_back_to_annotation_text_search(self):
        # 2 tag misses, then an annotation hit on the first term
        conn = FakeConn(results=[[], [], [_recall_row()]])
        with patch_conn(conn):
            out = await maq.recall_memory("storm bike", "tender", 9, "alice")

        assert out["id"] == "mem-9"
        assert "ILIKE" in conn.sqls[2]
        assert conn.params[2] == ("alice", "%storm%")

    @pytest.mark.asyncio
    async def test_no_text_match_falls_through_to_mood_recall(self):
        """She should still surface *something* rather than going blank."""
        conn = FakeConn(results=[[], [], [_recall_row(id="rand")]])
        with patch("app.memory_archive_query.random.choices", _pick_first):
            with patch_conn(conn):
                out = await maq.recall_memory("storm", "tender", 9, "alice")

        assert out["id"] == "rand"
        assert "ORDER BY random()" in conn.sqls[-1]

    @pytest.mark.asyncio
    async def test_query_of_only_short_words_is_treated_as_vague(self):
        conn = FakeConn(results=[[_recall_row(id="rand")]])
        with patch("app.memory_archive_query.random.choices", _pick_first):
            with patch_conn(conn):
                out = await maq.recall_memory("do we go", "tender", 9, "alice")

        assert out["id"] == "rand"
        assert len(conn.calls) == 1  # no tag/annotation searches at all

    @pytest.mark.asyncio
    async def test_recall_is_scoped_to_the_user(self):
        conn = FakeConn(results=[[_recall_row()]])
        with patch_conn(conn):
            await maq.recall_memory("storm", "tender", 9, "bob")

        assert conn.params[0][0] == "bob"


class TestRecallVague:
    @pytest.mark.asyncio
    async def test_mood_weights_bias_the_category_choice(self):
        """'tender' must pull Precious Memories, not a tactical debrief."""
        seen = {}

        def _spy(pool, weights, k):
            seen["pool"] = list(pool)
            seen["weights"] = list(weights)
            return ["Precious Memories"]

        conn = FakeConn(results=[[_recall_row()]])
        with patch("app.memory_archive_query.random.choices", _spy):
            with patch_conn(conn):
                await maq.recall_memory(None, "tender", 9, "alice")

        weight_by_cat = dict(zip(seen["pool"], seen["weights"], strict=True))
        assert weight_by_cat["Precious Memories"] == 5
        assert weight_by_cat["The Commander"] == 3
        # untouched categories still get a floor weight of 1
        assert weight_by_cat["Mission Records"] == 1
        assert conn.params[0] == ("alice", "Precious Memories")

    @pytest.mark.asyncio
    async def test_unknown_mood_weights_every_category_equally(self):
        seen = {}

        def _spy(pool, weights, k):
            seen["weights"] = list(weights)
            return [pool[0]]

        conn = FakeConn(results=[[_recall_row()]])
        with patch("app.memory_archive_query.random.choices", _spy):
            with patch_conn(conn):
                await maq.recall_memory(None, "not_a_real_mood", 9, "alice")

        assert set(seen["weights"]) == {1}

    @pytest.mark.asyncio
    async def test_locked_categories_are_never_offered(self):
        seen = {}

        def _spy(pool, weights, k):
            seen["pool"] = list(pool)
            return [pool[0]]

        conn = FakeConn(results=[[_recall_row()]])
        with patch("app.memory_archive_query.random.choices", _spy):
            with patch_conn(conn):
                await maq.recall_memory(None, "tender", 0, "alice")

        assert "Precious Memories" not in seen["pool"]
        assert seen["pool"] == maq.available_categories(0)

    @pytest.mark.asyncio
    async def test_empty_category_falls_back_to_any_kept_memory(self):
        conn = FakeConn(results=[[], [_recall_row(id="any")]])
        with patch("app.memory_archive_query.random.choices", _pick_first):
            with patch_conn(conn):
                out = await maq.recall_memory(None, "tender", 9, "alice")

        assert out["id"] == "any"
        assert "category = %s" not in conn.sqls[1]
        assert conn.params[1] == ("alice",)

    @pytest.mark.asyncio
    async def test_empty_archive_recalls_nothing(self):
        conn = FakeConn(results=[[], []])
        with patch("app.memory_archive_query.random.choices", _pick_first):
            with patch_conn(conn):
                assert await maq.recall_memory(None, "tender", 9, "alice") is None

    @pytest.mark.asyncio
    async def test_no_available_categories_recalls_nothing(self):
        """Below level 0 there is nothing to weight, so bail before querying."""
        conn = FakeConn(results=[[_recall_row()]])
        with patch_conn(conn):
            assert await maq.recall_memory(None, "tender", -1, "alice") is None

        assert conn.calls == []


class TestRecallRowShaping:
    @pytest.mark.asyncio
    async def test_null_fields_are_normalised(self):
        conn = FakeConn(
            results=[[_recall_row(annotation=None, scene_tags=None, created_at=None)]]
        )
        with patch_conn(conn):
            out = await maq.recall_memory("storm", "tender", 9, "alice")

        assert out["annotation"] == ""
        assert out["scene_tags"] == []
        assert out["created_at"] is None

    @pytest.mark.asyncio
    async def test_id_is_stringified(self):
        import uuid

        uid = uuid.uuid4()
        conn = FakeConn(results=[[_recall_row(id=uid)]])
        with patch_conn(conn):
            out = await maq.recall_memory("storm", "tender", 9, "alice")

        assert out["id"] == str(uid)


class TestRecallFailures:
    @pytest.mark.asyncio
    async def test_db_failure_returns_none(self):
        """Recall is decorative — an outage must not break the chat turn."""
        with broken_conn():
            assert await maq.recall_memory("storm", "tender", 9, "alice") is None

    @pytest.mark.asyncio
    async def test_query_failure_returns_none(self):
        conn = FakeConn(error=RuntimeError("timeout"))
        with patch_conn(conn):
            assert await maq.recall_memory(None, "tender", 9, "alice") is None

    @pytest.mark.asyncio
    async def test_malformed_row_returns_none(self):
        conn = FakeConn(results=[[("only-one-column",)]])
        with patch_conn(conn):
            assert await maq.recall_memory("storm", "tender", 9, "alice") is None


# ═══════════════════════════════════════════════════════════════════════════
# module wiring
# ═══════════════════════════════════════════════════════════════════════════


def test_images_dir_matches_the_write_path():
    """Regression: the read path pointed at a nonexistent /data/images, so the
    album silently showed no drawings."""
    assert maq.IMAGES_DIR == memory_archive.IMAGES_DIR
