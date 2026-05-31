"""Behavioral coverage for app/memory_archive.py — curation, dedup, save,
thumbnailing, and LLM annotation backfill.

Complements tests/test_memory_archive_pure.py (quality scoring + category
gating) and tests/test_memory_archive_db.py (query helpers). Here we drive
the write/curation path end-to-end with the DB pool, filesystem, PIL, and
LM Studio all mocked.

Every test asserts concrete behavior: dedup decisions (overlap ratio),
category clamping against affection level, the exact INSERT columns, file
cleanup on duplicate, thumbnail geometry, and backfill UPDATE/think-tag
stripping. UUID/HTTP/DB are mocked; no live services.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── DB mock helpers ──────────────────────────────────────────────────────────


class _RecordingConn:
    """Async connection recording SQL + params; returns canned fetch rows."""

    def __init__(self, rows=None):
        self.calls: list[tuple[str, tuple]] = []
        self._rows = rows or []

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        result = MagicMock()
        result.fetchall = AsyncMock(return_value=self._rows)
        result.fetchone = AsyncMock(return_value=self._rows[0] if self._rows else None)
        return result

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _conn_ctx(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


# ═══════════════════════════════════════════════════════════════════════════
# _get_http — lazy client construction / reuse (lines 27-29)
# ═══════════════════════════════════════════════════════════════════════════


class TestGetHttp:
    def test_creates_client_when_none(self):
        import app.memory_archive as ma

        fake_client = MagicMock()
        fake_client.is_closed = False
        with patch.object(ma, "_http", None), \
             patch("app.memory_archive.httpx.AsyncClient", return_value=fake_client) as ctor:
            client = ma._get_http()
        assert client is fake_client
        ctor.assert_called_once()

    def test_reuses_open_client(self):
        import app.memory_archive as ma

        existing = MagicMock()
        existing.is_closed = False
        with patch.object(ma, "_http", existing), \
             patch("app.memory_archive.httpx.AsyncClient") as ctor:
            client = ma._get_http()
        assert client is existing
        ctor.assert_not_called()  # reuse, don't rebuild

    def test_rebuilds_when_closed(self):
        import app.memory_archive as ma

        closed = MagicMock()
        closed.is_closed = True
        fresh = MagicMock()
        fresh.is_closed = False
        with patch.object(ma, "_http", closed), \
             patch("app.memory_archive.httpx.AsyncClient", return_value=fresh) as ctor:
            client = ma._get_http()
        assert client is fresh
        ctor.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# _is_duplicate_annotation — word-overlap dedup (lines 112-138)
# ═══════════════════════════════════════════════════════════════════════════


class TestIsDuplicateAnnotation:
    @pytest.mark.asyncio
    async def test_placeholder_is_never_duplicate(self):
        """The default placeholder must never be deduped away."""
        from app.memory_archive import _is_duplicate_annotation

        assert await _is_duplicate_annotation("Uncaptioned moment.") is False

    @pytest.mark.asyncio
    async def test_empty_is_never_duplicate(self):
        from app.memory_archive import _is_duplicate_annotation

        assert await _is_duplicate_annotation("") is False

    @pytest.mark.asyncio
    async def test_short_annotation_below_three_words_not_duplicate(self):
        """Fewer than 3 distinct words is too little signal to dedup on."""
        from app.memory_archive import _is_duplicate_annotation

        conn = _RecordingConn(rows=[("anything at all here",)])
        with patch("app.memory_archive.get_conn", _conn_ctx(conn)):
            result = await _is_duplicate_annotation("two words")
        assert result is False

    @pytest.mark.asyncio
    async def test_high_overlap_flags_duplicate(self):
        """>70% word overlap with a recent kept memory -> duplicate."""
        from app.memory_archive import _is_duplicate_annotation

        existing = "rain on the rooftop while coffee cooled on the desk"
        conn = _RecordingConn(rows=[(existing,)])
        with patch("app.memory_archive.get_conn", _conn_ctx(conn)):
            # Identical text -> ratio 1.0 -> duplicate.
            result = await _is_duplicate_annotation(existing)
        assert result is True

    @pytest.mark.asyncio
    async def test_low_overlap_is_not_duplicate(self):
        """Distinct annotations (little overlap) are kept."""
        from app.memory_archive import _is_duplicate_annotation

        conn = _RecordingConn(
            rows=[("the squad ran tactical drills before dawn briefing",)]
        )
        with patch("app.memory_archive.get_conn", _conn_ctx(conn)):
            result = await _is_duplicate_annotation(
                "he kissed my shoulder softly in the quiet morning light"
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_threshold_is_configurable(self):
        """A lower threshold makes partial overlaps count as duplicates."""
        from app.memory_archive import _is_duplicate_annotation

        # target has 6 words, existing shares 3 -> ratio 0.5.
        conn = _RecordingConn(rows=[("alpha beta gamma one",)])
        with patch("app.memory_archive.get_conn", _conn_ctx(conn)):
            # 0.5 overlap < 0.7 default -> not duplicate
            assert await _is_duplicate_annotation("alpha beta gamma four five six") is False
            # ...but with threshold 0.4 it counts.
            assert await _is_duplicate_annotation(
                "alpha beta gamma four five six", threshold=0.4
            ) is True

    @pytest.mark.asyncio
    async def test_skips_empty_existing_rows(self):
        """NULL/empty existing annotations are skipped, not crashed on."""
        from app.memory_archive import _is_duplicate_annotation

        conn = _RecordingConn(rows=[(None,), ("",), ("totally different squad drill",)])
        with patch("app.memory_archive.get_conn", _conn_ctx(conn)):
            result = await _is_duplicate_annotation(
                "a tender private moment between the two of us alone"
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_db_error_fails_open_not_duplicate(self):
        """If the dedup query fails, fail open (treat as not-duplicate) so we
        don't silently drop a legitimate new memory."""
        from app.memory_archive import _is_duplicate_annotation

        def broken():
            raise RuntimeError("db down")

        with patch("app.memory_archive.get_conn", side_effect=broken):
            result = await _is_duplicate_annotation(
                "some brand new annotation with plenty of words here"
            )
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════
# save_image — full curation/dedup/insert path (lines 155-224)
# ═══════════════════════════════════════════════════════════════════════════


@asynccontextmanager
async def _save_image_env(conn, *, is_dup=False, fixed_uuid="11111111-2222-3333-4444-555555555555"):
    """Patch everything save_image touches and yield the mocks tests assert on.

    Yields (uuid, write_bytes_mock, thumbnail_mock).
    """
    # save_image now also encodes a full-res WebP via Image.open(BytesIO(bytes));
    # fake test bytes won't parse, so stub the open/convert/save chain.
    _full_cm = MagicMock()
    _full_cm.__enter__ = MagicMock(return_value=MagicMock())
    _full_cm.__exit__ = MagicMock(return_value=False)
    with patch("app.memory_archive.uuid.uuid4", return_value=fixed_uuid), \
         patch("app.memory_archive.Path.write_bytes") as write_bytes, \
         patch("app.memory_archive.Image.open", return_value=_full_cm), \
         patch("app.memory_archive._generate_thumbnail") as thumb, \
         patch("app.memory_archive._is_duplicate_annotation",
               AsyncMock(return_value=is_dup)), \
         patch("app.memory_archive.get_conn_autocommit", _conn_ctx(conn)):
        yield fixed_uuid, write_bytes, thumb


class TestSaveImage:
    @pytest.mark.asyncio
    async def test_writes_image_and_inserts_row_with_curation(self):
        """Happy path: image written, thumbnail made, row INSERTed with the
        curated annotation/category/tags and correct column ordering."""
        from app.memory_archive import save_image

        conn = _RecordingConn()
        curation = {
            "keep": True,
            "annotation": "He steadied my hand before the briefing at 0300.",
            "category": "The Commander",
            "image_tags": ["briefing", "hand"],
        }

        async with _save_image_env(conn) as (fixed_uuid, write_bytes, thumb):
            mem_id = await save_image(
                image_bytes=b"PNGDATA",
                prompt="klukai steadying the commander's hand",
                conversation_id="conv-1",
                mood="tender",
                affection_level=6,  # unlocks "The Commander"
                curation=curation,
                user_id="alice",
            )

        assert mem_id == fixed_uuid
        # Full image written.
        write_bytes.assert_called_once_with(b"PNGDATA")
        # Thumbnail generated at 320px width.
        assert thumb.call_args[1]["width"] == 320
        # One INSERT, carrying curated fields in declared column order.
        assert len(conn.calls) == 1
        sql, params = conn.calls[0]
        assert "INSERT INTO companion_memories" in sql
        assert params[0] == fixed_uuid                 # id
        assert params[1] == f"{fixed_uuid}.webp"       # filename (full-res WebP served)
        assert params[2] == f"{fixed_uuid}_thumb.webp"  # thumb_filename
        assert params[4] == curation["annotation"]     # annotation
        assert params[5] == ["briefing", "hand"]       # scene_tags
        assert params[6] == "tender"                   # mood
        assert params[7] == 6                          # affection_level
        assert params[8] is True                       # kept
        assert params[9] == "klukai"                   # kept_by
        assert params[10] == "The Commander"           # category
        assert params[11] == "conv-1"                  # conversation_id
        assert params[12] == "alice"                   # user_id

    @pytest.mark.asyncio
    async def test_category_clamped_to_affection_level(self):
        """A category above the user's affection gate is clamped to the highest
        ALLOWED category — never silently granted."""
        from app.memory_archive import available_categories, save_image

        conn = _RecordingConn()
        # Level 0 cannot access "Precious Memories" (gated at 6).
        curation = {
            "annotation": "A quiet detailed moment worth keeping in the archive.",
            "category": "Precious Memories",
            "image_tags": [],
        }
        async with _save_image_env(conn):
            await save_image(
                image_bytes=b"x", prompt="p", conversation_id="c",
                affection_level=0, curation=curation,
            )

        _, params = conn.calls[0]
        allowed = available_categories(0)
        assert params[10] in allowed
        assert params[10] != "Precious Memories"
        assert params[10] == allowed[-1]  # clamps to the last valid one

    @pytest.mark.asyncio
    async def test_missing_annotation_gets_placeholder(self):
        """Annotation is NEVER NULL — a missing one becomes the placeholder."""
        from app.memory_archive import save_image

        conn = _RecordingConn()
        # No curation at all -> defaults used, annotation placeholdered.
        async with _save_image_env(conn):
            mem_id = await save_image(
                image_bytes=b"x", prompt="some prompt", conversation_id="c",
            )

        assert mem_id is not None
        _, params = conn.calls[0]
        assert params[4] == "Uncaptioned moment."
        assert params[8] is True            # defaults to kept
        assert params[10] == "Mission Records"  # default category

    @pytest.mark.asyncio
    async def test_duplicate_skips_insert_and_cleans_files(self):
        """A duplicate annotation -> no DB row + both written files unlinked,
        and None returned to the caller."""
        from app.memory_archive import save_image

        conn = _RecordingConn()
        fixed_uuid = "99999999-0000-0000-0000-000000000000"

        _full_cm = MagicMock()
        _full_cm.__enter__ = MagicMock(return_value=MagicMock())
        _full_cm.__exit__ = MagicMock(return_value=False)
        with patch("app.memory_archive.uuid.uuid4", return_value=fixed_uuid), \
             patch("app.memory_archive.Path.write_bytes"), \
             patch("app.memory_archive.Image.open", return_value=_full_cm), \
             patch("app.memory_archive._generate_thumbnail"), \
             patch("app.memory_archive._is_duplicate_annotation",
                   AsyncMock(return_value=True)), \
             patch("app.memory_archive.Path.unlink") as unlink, \
             patch("app.memory_archive.get_conn_autocommit", _conn_ctx(conn)):
            result = await save_image(
                image_bytes=b"x", prompt="p", conversation_id="c",
                curation={"annotation": "a duplicate annotation with many words here"},
            )

        assert result is None
        assert len(conn.calls) == 0          # NO insert for a duplicate
        assert unlink.call_count == 3        # png + webp + thumbnail cleaned up

    @pytest.mark.asyncio
    async def test_keep_false_is_persisted(self):
        """Curation can mark an image not-kept; that flag must reach the row."""
        from app.memory_archive import save_image

        conn = _RecordingConn()
        curation = {
            "keep": False,
            "annotation": "A blurry throwaway shot not worth keeping at all.",
            "category": "Mission Records",
            "image_tags": [],
        }
        async with _save_image_env(conn):
            await save_image(
                image_bytes=b"x", prompt="p", conversation_id="c",
                affection_level=0, curation=curation,
            )

        _, params = conn.calls[0]
        assert params[8] is False  # kept = False persisted

    @pytest.mark.asyncio
    async def test_returns_none_on_write_failure(self):
        """A filesystem failure is caught and surfaced as None, not an exception."""
        from app.memory_archive import save_image

        with patch("app.memory_archive.uuid.uuid4", return_value="x"), \
             patch("app.memory_archive.Path.write_bytes",
                   side_effect=OSError("disk full")):
            result = await save_image(
                image_bytes=b"x", prompt="p", conversation_id="c",
            )
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# _generate_thumbnail — geometry + failure safety (lines 229-236)
# ═══════════════════════════════════════════════════════════════════════════


class TestGenerateThumbnail:
    def test_resizes_preserving_aspect_ratio(self):
        """Thumbnail width is honored and height is scaled by the same ratio."""
        from app.memory_archive import _generate_thumbnail

        src_img = MagicMock()
        src_img.width = 800
        src_img.height = 600
        resized = MagicMock()
        src_img.resize.return_value = resized
        src_img.convert.return_value = src_img  # .convert("RGB").resize(...) chain
        # Context-manager protocol for `with Image.open(src) as img:`
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=src_img)
        cm.__exit__ = MagicMock(return_value=False)

        with patch("app.memory_archive.Image.open", return_value=cm):
            _generate_thumbnail(Path("/tmp/a.png"), Path("/tmp/b.png"), width=320)

        # 320/800 = 0.4 ratio -> height 240.
        src_img.resize.assert_called_once()
        assert src_img.resize.call_args[0][0] == (320, 240)
        # Saved as WebP (efficient for the grid; the full image stays PNG).
        resized.save.assert_called_once()
        assert resized.save.call_args[0][1] == "WEBP"

    def test_swallows_pillow_errors(self):
        """A broken image must not raise out of thumbnail generation."""
        from app.memory_archive import _generate_thumbnail

        with patch("app.memory_archive.Image.open", side_effect=OSError("bad png")):
            # Should not raise.
            _generate_thumbnail(Path("/tmp/a.png"), Path("/tmp/b.png"))


# ═══════════════════════════════════════════════════════════════════════════
# backfill_annotations — LLM-driven backfill (lines 280-361)
# ═══════════════════════════════════════════════════════════════════════════


class _GateCM:
    """Async-context-manager stand-in for the LM Studio gate lock."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class TestBackfillAnnotations:
    @pytest.mark.asyncio
    async def test_noop_when_nothing_to_backfill(self):
        """No unannotated rows -> total/updated both 0, no LLM calls."""
        from app.memory_archive import backfill_annotations

        conn = _RecordingConn(rows=[])
        with patch("app.memory_archive.get_conn", _conn_ctx(conn)), \
             patch("app.llm_router.get_lm_gate", return_value=_GateCM()):
            result = await backfill_annotations(user_id="alice")

        assert result == {"total": 0, "updated": 0}

    @pytest.mark.asyncio
    async def test_generates_and_updates_annotation(self):
        """For each unannotated row: call LM Studio, then UPDATE the row with the
        cleaned caption. Returns total + updated counts."""
        from app.memory_archive import backfill_annotations

        # SELECT returns one row needing a caption: (id, prompt, category, scene_tags)
        select_conn = _RecordingConn(
            rows=[("mem-1", "rooftop scene", "Quiet Hours", ["rooftop", "night"])]
        )
        update_conn = _RecordingConn()

        # get_conn (SELECT) and get_conn_autocommit (UPDATE) are distinct ctx mgrs.
        def get_conn_side():
            return _conn_ctx(select_conn)()

        def get_conn_ac_side():
            return _conn_ctx(update_conn)()

        llm_resp = MagicMock()
        llm_resp.raise_for_status = MagicMock()
        llm_resp.json = MagicMock(
            return_value={
                "choices": [
                    {"message": {"content": '"The rooftop was ours that night."'}}
                ]
            }
        )
        fake_http = MagicMock()
        fake_http.post = AsyncMock(return_value=llm_resp)

        with patch("app.memory_archive.get_conn", side_effect=get_conn_side), \
             patch("app.memory_archive.get_conn_autocommit", side_effect=get_conn_ac_side), \
             patch("app.llm_router.get_lm_gate", return_value=_GateCM()), \
             patch("app.memory_archive._get_http", return_value=fake_http):
            result = await backfill_annotations(user_id="alice")

        assert result == {"total": 1, "updated": 1}
        # The UPDATE ran with the de-quoted caption against the right id.
        assert len(update_conn.calls) == 1
        sql, params = update_conn.calls[0]
        assert "UPDATE companion_memories SET annotation" in sql
        assert params == ("The rooftop was ours that night.", "mem-1")

    @pytest.mark.asyncio
    async def test_strips_think_tags_from_llm_output(self):
        """Reasoning-model <think>...</think> leakage is stripped before storing."""
        from app.memory_archive import backfill_annotations

        select_conn = _RecordingConn(rows=[("mem-2", "p", "Dreams", [])])
        update_conn = _RecordingConn()

        llm_resp = MagicMock()
        llm_resp.raise_for_status = MagicMock()
        llm_resp.json = MagicMock(
            return_value={
                "choices": [
                    {"message": {"content": "<think>plan the caption</think>A soft dream of him."}}
                ]
            }
        )
        fake_http = MagicMock()
        fake_http.post = AsyncMock(return_value=llm_resp)

        with patch("app.memory_archive.get_conn", side_effect=lambda: _conn_ctx(select_conn)()), \
             patch("app.memory_archive.get_conn_autocommit", side_effect=lambda: _conn_ctx(update_conn)()), \
             patch("app.llm_router.get_lm_gate", return_value=_GateCM()), \
             patch("app.memory_archive._get_http", return_value=fake_http):
            result = await backfill_annotations()

        assert result["updated"] == 1
        _, params = update_conn.calls[0]
        # The thinking block is gone; only the caption survives.
        assert "<think>" not in params[0]
        assert "plan the caption" not in params[0]
        assert params[0] == "A soft dream of him."

    @pytest.mark.asyncio
    async def test_empty_llm_output_falls_back_to_placeholder(self):
        """If the model returns nothing usable, store the placeholder, not NULL."""
        from app.memory_archive import backfill_annotations

        select_conn = _RecordingConn(rows=[("mem-3", "p", "Dreams", None)])
        update_conn = _RecordingConn()

        llm_resp = MagicMock()
        llm_resp.raise_for_status = MagicMock()
        llm_resp.json = MagicMock(
            return_value={"choices": [{"message": {"content": "   "}}]}
        )
        fake_http = MagicMock()
        fake_http.post = AsyncMock(return_value=llm_resp)

        with patch("app.memory_archive.get_conn", side_effect=lambda: _conn_ctx(select_conn)()), \
             patch("app.memory_archive.get_conn_autocommit", side_effect=lambda: _conn_ctx(update_conn)()), \
             patch("app.llm_router.get_lm_gate", return_value=_GateCM()), \
             patch("app.memory_archive._get_http", return_value=fake_http):
            result = await backfill_annotations()

        assert result["updated"] == 1
        _, params = update_conn.calls[0]
        assert params[0] == "Uncaptioned moment."

    @pytest.mark.asyncio
    async def test_per_row_llm_failure_does_not_abort_batch(self):
        """One failed row doesn't stop the rest: total counts every row,
        updated counts only the successes."""
        from app.memory_archive import backfill_annotations

        select_conn = _RecordingConn(
            rows=[
                ("mem-a", "pa", "Dreams", []),
                ("mem-b", "pb", "Dreams", []),
            ]
        )
        update_conn = _RecordingConn()

        good = MagicMock()
        good.raise_for_status = MagicMock()
        good.json = MagicMock(
            return_value={"choices": [{"message": {"content": "A good caption here."}}]}
        )

        calls = {"n": 0}

        async def post(*_a, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("lm studio timeout")  # first row fails
            return good

        fake_http = MagicMock()
        fake_http.post = post

        with patch("app.memory_archive.get_conn", side_effect=lambda: _conn_ctx(select_conn)()), \
             patch("app.memory_archive.get_conn_autocommit", side_effect=lambda: _conn_ctx(update_conn)()), \
             patch("app.llm_router.get_lm_gate", return_value=_GateCM()), \
             patch("app.memory_archive._get_http", return_value=fake_http):
            result = await backfill_annotations()

        assert result["total"] == 2
        assert result["updated"] == 1  # only the second row succeeded
        assert len(update_conn.calls) == 1
        assert update_conn.calls[0][1][1] == "mem-b"

    @pytest.mark.asyncio
    async def test_select_failure_returns_error_dict(self):
        """If the initial SELECT blows up, return a zero result carrying error."""
        from app.memory_archive import backfill_annotations

        def broken():
            raise RuntimeError("db unreachable")

        with patch("app.memory_archive.get_conn", side_effect=broken), \
             patch("app.llm_router.get_lm_gate", return_value=_GateCM()):
            result = await backfill_annotations()

        assert result["total"] == 0
        assert result["updated"] == 0
        assert "error" in result
