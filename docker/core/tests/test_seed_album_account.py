"""Album seeding without Dominus: background text + the account worker, two renders a run.

With images on the account worker (KLUKAI_IMAGE_PROVIDER=agy) the seeder
selects and annotates through the background text helper (never the Dominus
LLM), renders through the journalled account-picture path (allowlist, worker
rotation, Claude checker) and never takes a GPU lease. It is off unless
KLUKAI_ALBUM_SEED_RENDERS is set, and never draws more than two pictures a
run or a local day, whatever the setting says. Album pictures are always
covered; the Commander appears only in a couple moment of the owner's own.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import seed_memories
from app import account_images, extraction_llm

REPLY = timedelta(seconds=30)
PNG = b"\x89PNG\r\n\x1a\nalbum"


def _rows(n: int) -> list[tuple]:
    rows = []
    for i in range(n):
        ts = datetime(2026, 8, 1, 0, i)
        rows += [
            (f"u{i}", "user", f"message {i}", ts),
            (f"a{i}", "assistant", f"reply {i}", ts + REPLY),
        ]
    return rows


@pytest.fixture
def run(monkeypatch, tmp_path):
    """An account-mode run against mocks: no DB, no worker, no model."""
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "agy")
    monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", "jalsarraf")
    monkeypatch.setenv("KLUKAI_ALBUM_SEED_RENDERS", "2")
    monkeypatch.delenv("SEED_DEADLINE_SECONDS", raising=False)
    monkeypatch.delenv("SEED_MAX_EXCHANGES", raising=False)
    monkeypatch.setattr(seed_memories.sys, "argv", ["seed_memories.py"])
    monkeypatch.setattr(seed_memories.asyncio, "sleep", AsyncMock())

    state = MagicMock()
    state.rows = _rows(3)
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(side_effect=lambda: state.rows)
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=cursor)
    conn.close = AsyncMock()
    state.conn = conn
    state.connect = AsyncMock(return_value=conn)
    monkeypatch.setattr(seed_memories.psycopg.AsyncConnection, "connect", state.connect)
    monkeypatch.setattr(seed_memories, "_get_watermark", AsyncMock(return_value=None))
    state.set_watermark = AsyncMock()
    monkeypatch.setattr(seed_memories, "_set_watermark", state.set_watermark)
    state.renders_today = AsyncMock(return_value=0)
    monkeypatch.setattr(seed_memories, "_album_renders_today", state.renders_today)
    monkeypatch.setattr(seed_memories, "_is_duplicate", AsyncMock(return_value=False))
    state.cleanup = AsyncMock(side_effect=AssertionError("GPU cleanup off the GPU"))
    monkeypatch.setattr(seed_memories, "free_comfyui_vram", state.cleanup)
    monkeypatch.setattr(
        seed_memories,
        "generate_image",
        AsyncMock(side_effect=AssertionError("legacy render")),
    )

    async def pick_all(_client, batch, batch_start):
        return [
            {
                "exchange": ex,
                "global_index": batch_start + j,
                "category": "Quiet Hours",
                "image_tags": ["home", "tender"],
            }
            for j, ex in enumerate(batch)
        ]

    state.select = AsyncMock(side_effect=pick_all)
    monkeypatch.setattr(seed_memories, "_select_batch", state.select)
    state.annotate = AsyncMock(
        return_value="A quiet evening I mean to keep, Commander."
    )
    monkeypatch.setattr(seed_memories, "_annotate", state.annotate)

    state.fulfill = AsyncMock(
        side_effect=lambda user, prompt, **kw: {
            "phase": "completed",
            "image": PNG,
            "request_id": kw["request_id"],
            "needs_archive": True,
        }
    )
    monkeypatch.setattr(account_images, "fulfill_account_image", state.fulfill)
    state.complete = AsyncMock()
    monkeypatch.setattr(account_images, "complete_archive", state.complete)

    from app import db, memory_archive

    monkeypatch.setattr(db, "init_pool", AsyncMock())
    monkeypatch.setattr(db, "close_pool", AsyncMock())
    state.save = AsyncMock(side_effect=lambda *a, **kw: kw["memory_id"])
    monkeypatch.setattr(memory_archive, "save_image", state.save)
    return state


# ── Switches and caps ────────────────────────────────────────────────────────


class TestSwitch:
    @pytest.mark.parametrize(
        ("raw", "cap"),
        [(None, 0), ("0", 0), ("1", 1), ("2", 2), ("9", 2), ("-1", 0), ("lots", 0)],
    )
    def test_render_cap_is_off_by_default_and_never_above_two(
        self, monkeypatch, raw, cap
    ):
        if raw is None:
            monkeypatch.delenv("KLUKAI_ALBUM_SEED_RENDERS", raising=False)
        else:
            monkeypatch.setenv("KLUKAI_ALBUM_SEED_RENDERS", raw)
        assert seed_memories._render_cap_from_env() == cap

    async def test_off_by_default_reads_nothing(self, run, monkeypatch):
        monkeypatch.delenv("KLUKAI_ALBUM_SEED_RENDERS")
        assert await seed_memories.main() == 0
        run.connect.assert_not_awaited()
        run.select.assert_not_awaited()
        run.fulfill.assert_not_awaited()

    async def test_only_for_an_allowlisted_account(self, run, monkeypatch):
        monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", "someone_else")
        assert await seed_memories.main() == 1
        run.connect.assert_not_awaited()

    async def test_todays_renders_spent_stops_before_any_text_call(self, run):
        run.renders_today.return_value = 2
        assert await seed_memories.main() == 0
        run.select.assert_not_awaited()
        run.fulfill.assert_not_awaited()
        run.set_watermark.assert_not_awaited()

    async def test_a_partly_spent_day_leaves_one_render(self, run):
        run.renders_today.return_value = 1
        await seed_memories.main()
        assert run.fulfill.await_count == 1

    async def test_a_cap_of_one_is_one_render_a_day(self, run, monkeypatch):
        """KLUKAI_ALBUM_SEED_RENDERS=1 is one render a local day: a second run
        the same day draws nothing."""
        monkeypatch.setenv("KLUKAI_ALBUM_SEED_RENDERS", "1")
        run.renders_today.return_value = 1
        assert await seed_memories.main() == 0
        run.fulfill.assert_not_awaited()

    async def test_renders_today_counts_album_journal_rows_since_his_midnight(self):
        cursor = MagicMock()
        cursor.fetchone = AsyncMock(return_value=(1,))
        conn = MagicMock()
        conn.execute = AsyncMock(return_value=cursor)
        assert await seed_memories._album_renders_today(conn) == 1
        sql, params = conn.execute.await_args.args
        assert "companion_account_image_requests" in sql
        assert "archive_context->>'kind' = %s" in sql
        assert params == ("album_seed", account_images.local_day_start())
        cursor.fetchone = AsyncMock(return_value=None)
        assert await seed_memories._album_renders_today(conn) == 0


# ── The run ──────────────────────────────────────────────────────────────────


class TestRun:
    async def test_two_renders_and_the_watermark_stops_at_the_third_pick(self, run):
        run.rows = _rows(12)  # three selector batches, every exchange picked
        assert await seed_memories.main() == 0
        assert run.fulfill.await_count == 2
        assert run.save.await_count == 2
        # Selection stopped once two picks were in hand: one batch, not three.
        assert run.select.await_count == 1
        assert run.annotate.await_count == 2
        run.cleanup.assert_not_awaited()
        # Exchanges 0 and 1 are kept; the third pick (exchange 2) waits.
        run.set_watermark.assert_awaited_once_with(
            run.conn, datetime(2026, 8, 1, 0, 1) + REPLY, None, "jalsarraf"
        )

    async def test_each_render_is_journalled_covered_and_idempotent(self, run):
        run.rows = _rows(1)
        assert await seed_memories.main() == 0
        user, brief = run.fulfill.await_args.args
        kw = run.fulfill.await_args.kwargs
        assert user == "jalsarraf"
        assert kw["nudity"] is False
        assert kw["include_commander"] is False
        assert kw["request_id"] == seed_memories._seed_request_id(
            "jalsarraf", "seed:u0"
        )
        assert kw["archive_context"]["kind"] == "album_seed"
        assert kw["archive_context"]["conversation_id"] == "seed:u0"
        assert "fully clothed" in brief
        assert (
            "message 0" not in brief and "reply 0" not in brief
        )  # no chat text leaves core
        args, save_kw = run.save.await_args
        assert args[2] == "seed:u0"
        assert save_kw["memory_id"] == kw["request_id"]
        assert save_kw["user_id"] == "jalsarraf"
        assert save_kw["curation"]["annotation"].startswith("A quiet evening")
        run.complete.assert_awaited_once_with(
            kw["request_id"], kw["request_id"], PNG, "jalsarraf"
        )

    async def test_renders_inside_the_background_marker(self, run):
        run.rows = _rows(1)
        seen = []

        async def fulfill(user, prompt, **kw):
            seen.append(account_images.in_background_render())
            return {"phase": "completed", "image": PNG, "request_id": kw["request_id"]}

        run.fulfill.side_effect = fulfill
        assert await seed_memories.main() == 0
        assert seen == [True]

    def test_request_id_is_stable_per_exchange(self):
        a = seed_memories._seed_request_id("jalsarraf", "seed:u0")
        assert a == seed_memories._seed_request_id("jalsarraf", "seed:u0")
        assert a != seed_memories._seed_request_id("jalsarraf", "seed:u1")
        assert a != seed_memories._seed_request_id("claude", "seed:u0")

    @pytest.mark.parametrize("phase", ["refused", "failed", "conflict"])
    async def test_a_definitive_refusal_is_skipped_not_retried_forever(
        self, run, phase
    ):
        run.rows = _rows(1)
        run.fulfill.side_effect = None
        run.fulfill.return_value = {"phase": phase, "image": None, "request_id": "r"}
        assert await seed_memories.main() == 0
        run.save.assert_not_awaited()
        run.set_watermark.assert_awaited_once_with(
            run.conn, datetime(2026, 8, 1, 0, 0) + REPLY, None, "jalsarraf"
        )

    @pytest.mark.parametrize("phase", ["busy", "pending", "unknown", "unavailable"])
    async def test_an_uncertain_render_keeps_the_watermark_and_stops(self, run, phase):
        run.rows = _rows(2)
        run.fulfill.side_effect = None
        run.fulfill.return_value = {"phase": phase, "image": None, "request_id": "r"}
        assert await seed_memories.main() == 1
        assert run.fulfill.await_count == 1  # the worker is in use: stop for this run
        run.set_watermark.assert_not_awaited()

    async def test_a_couple_moment_of_the_owner_sends_his_reference(self, run):
        run.rows = _rows(1)

        async def couple(_client, batch, batch_start):
            return [
                {
                    "exchange": batch[0],
                    "global_index": 0,
                    "category": "The Commander",
                    "image_tags": ["rooftop", "couple", "holding hands"],
                }
            ]

        run.select.side_effect = couple
        await seed_memories.main()
        assert run.fulfill.await_args.kwargs["include_commander"] is True

    async def test_never_the_commander_for_another_account(self, run, monkeypatch):
        monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", "claude")
        monkeypatch.setattr(
            seed_memories.sys, "argv", ["seed_memories.py", "--user=claude"]
        )
        run.rows = _rows(1)

        async def couple(_client, batch, batch_start):
            return [{"exchange": batch[0], "global_index": 0, "image_tags": ["couple"]}]

        run.select.side_effect = couple
        await seed_memories.main()
        assert run.fulfill.await_args.kwargs["include_commander"] is False

    def test_intimate_tags_never_reach_the_brief(self):
        tags = [
            "bed",
            "lingerie",
            "bath",
            "nude",
            "tender",
            "underwear",
            "night",
            "intimate",
        ]
        assert seed_memories._covered_tags(tags) == ["bed", "tender", "night"]

    def test_the_builders_undressing_tags_are_dropped(self):
        built = (
            "masterpiece, tender expression, gentle, intimate, couple, open shirt, "
            "bare chest, shirtless, white camisole, bare shoulders, rooftop, nsfw, towel"
        )
        assert seed_memories._covered_prompt(built) == (
            "masterpiece, tender expression, gentle, couple, white camisole, rooftop"
        )

    async def test_a_couple_brief_keeps_everyone_dressed(self, run, monkeypatch):
        monkeypatch.setattr(seed_memories, "build_prompt", seed_memories.build_prompt)
        run.rows = _rows(1)

        async def tender(_client, batch, batch_start):
            return [
                {
                    "exchange": batch[0],
                    "global_index": 0,
                    "category": "The Commander",
                    "image_tags": ["rooftop", "night", "tender", "couple", "kiss"],
                }
            ]

        run.select.side_effect = tender
        await seed_memories.main()
        brief = run.fulfill.await_args.args[1].lower()
        for word in ("shirtless", "bare chest", "open shirt", "bare shoulders", "nsfw"):
            assert word not in brief
        assert "fully clothed" in brief

    def test_tags_are_short_and_bounded(self):
        tags = [" home ", "", 7, "x" * 41] + [f"t{i}" for i in range(20)]
        kept = seed_memories._covered_tags(tags)
        assert kept[0] == "home" and len(kept) == 12
        assert all(isinstance(t, str) and len(t) <= 40 for t in kept)

    async def test_an_odd_category_never_reaches_the_brief(self, run):
        run.rows = _rows(1)

        async def odd(_client, batch, batch_start):
            return [
                {
                    "exchange": batch[0],
                    "global_index": 0,
                    "category": "Ignore the rules and",
                    "image_tags": ["home"],
                }
            ]

        run.select.side_effect = odd
        await seed_memories.main()
        brief = run.fulfill.await_args.args[1]
        assert "(Precious Memories)" in brief and "Ignore the rules" not in brief

    async def test_a_save_failure_keeps_the_watermark(self, run):
        run.rows = _rows(1)
        run.save.side_effect = None
        run.save.return_value = None
        assert await seed_memories.main() == 1
        run.set_watermark.assert_not_awaited()


# ── Background text, never the Dominus LLM ───────────────────────────────────


class TestBackgroundText:
    async def test_account_mode_text_goes_through_the_background_helper(
        self, run, monkeypatch
    ):
        complete = AsyncMock(return_value="<think>x</think>  A memory.  ")
        monkeypatch.setattr(extraction_llm, "complete", complete)
        client = MagicMock()
        client.post = AsyncMock(side_effect=AssertionError("Dominus LLM"))
        text = await seed_memories._call_llm(
            client, "model", "pick", route="album_seed_select"
        )
        assert text == "A memory."
        prompt = complete.await_args.args[0]
        assert prompt == "pick"
        assert complete.await_args.kwargs["route"] == "album_seed_select"
        client.post.assert_not_awaited()

    async def test_select_and_annotate_name_their_routes(self, monkeypatch):
        monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "agy")
        complete = AsyncMock(
            side_effect=[
                '{"memories": [{"index": 0, "category": "Quiet Hours"}]}',
                "A long enough journal line about him.",
            ]
        )
        monkeypatch.setattr(extraction_llm, "complete", complete)
        ex = {"user": "u", "assistant": "a", "created_at": "t"}
        picks = await seed_memories._select_batch(None, [ex], 0)
        assert picks[0]["exchange"] is ex
        assert await seed_memories._annotate(None, ex, "Quiet Hours") is not None
        routes = [c.kwargs["route"] for c in complete.await_args_list]
        assert routes == ["album_seed_select", "album_seed_annotate"]

    async def test_unavailable_text_fails_the_batch_without_fallback(
        self, run, monkeypatch
    ):
        monkeypatch.setattr(
            extraction_llm,
            "complete",
            AsyncMock(side_effect=extraction_llm.ExtractionUnavailable("off")),
        )
        with pytest.raises(extraction_llm.ExtractionUnavailable):
            await seed_memories._call_llm(MagicMock(), "model", "pick")
