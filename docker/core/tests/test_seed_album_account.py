"""Pure compatibility helpers for the retired album seeder.

The on-demand policy tests verify that its entry point cannot run this pipeline.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import seed_memories
from app import account_images, extraction_llm



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
    def test_request_id_is_stable_per_exchange(self):
        a = seed_memories._seed_request_id("jalsarraf", "seed:u0")
        assert a == seed_memories._seed_request_id("jalsarraf", "seed:u0")
        assert a != seed_memories._seed_request_id("jalsarraf", "seed:u1")
        assert a != seed_memories._seed_request_id("claude", "seed:u0")

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

    def test_tags_are_short_and_bounded(self):
        tags = [" home ", "", 7, "x" * 41] + [f"t{i}" for i in range(20)]
        kept = seed_memories._covered_tags(tags)
        assert kept[0] == "home" and len(kept) == 12
        assert all(isinstance(t, str) and len(t) <= 40 for t in kept)


# ── Background text, never the Dominus LLM ───────────────────────────────────


class TestBackgroundText:
    async def test_account_mode_text_goes_through_the_background_helper(
        self, monkeypatch
    ):
        monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "agy")
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
        self, monkeypatch
    ):
        monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "agy")
        monkeypatch.setattr(
            extraction_llm,
            "complete",
            AsyncMock(side_effect=extraction_llm.ExtractionUnavailable("off")),
        )
        with pytest.raises(extraction_llm.ExtractionUnavailable):
            await seed_memories._call_llm(MagicMock(), "model", "pick")
