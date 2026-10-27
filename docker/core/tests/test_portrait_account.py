"""Live Portrait on the account worker: a missing outfit's base frame.

Pictures are cloud-only, and the account worker (KLUKAI_IMAGE_PROVIDER=agy)
has no inpaint. An outfit without a cached set gets ONE base frame, drawn
through the journalled explicit-picture path (allowlist, worker rotation,
Claude checker), one job at a time and at most KLUKAI_PORTRAIT_DAILY_LIMIT
new portraits a local day. Expression/blink/talk frames stay missing and the
stage shows the base. Cached sets are never touched or redrawn, and nothing
here probes a game guard.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from app import account_images, context
from app import portrait as pt
from app import portrait_account as pa


def _png(size=(1024, 1536), color=(90, 110, 160)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _account(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "agy")
    monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", "jalsarraf")
    monkeypatch.delenv("KLUKAI_PORTRAIT_DAILY_LIMIT", raising=False)
    monkeypatch.setattr(pt, "START_DELAY_S", 0)
    monkeypatch.setattr(pt, "QUIET_POLL_S", 0)
    monkeypatch.setattr(pt, "seconds_since_user_message", lambda: None)
    router = MagicMock()
    router.is_game_active = AsyncMock(side_effect=AssertionError("no game probe"))
    monkeypatch.setattr(context, "router", router)
    pt._inflight.clear()
    pt._failed_at.clear()
    yield
    pt._inflight.clear()
    pt._failed_at.clear()


@pytest.fixture
def worker(monkeypatch):
    """The journal + worker seam: no DB, no socket, no render."""
    fulfill = AsyncMock(
        return_value={"phase": "completed", "image": _png(), "request_id": "r"}
    )
    monkeypatch.setattr(account_images, "fulfill_account_image", fulfill)
    monkeypatch.setattr(account_images, "fetch_request", AsyncMock(return_value=None))
    monkeypatch.setattr(
        account_images, "count_requests_since", AsyncMock(return_value=0)
    )
    return fulfill


def _write(user="jalsarraf", outfit="cat_cafe_date", frames=pt.FRAME_NAMES):
    d = pt.frame_dir(user, outfit)
    d.mkdir(parents=True, exist_ok=True)
    (d / pt.SOURCE_NAME).write_bytes(_png((32, 48)))
    for f in frames:
        (d / f"{f}.webp").write_bytes(b"RIFFxxxxWEBP")


# ── Mode and settings ────────────────────────────────────────────────────────


class TestMode:
    def test_there_is_no_local_portrait_renderer(self, monkeypatch):
        for gone in ("uses_gpu", "can_redraw", "refresh"):
            assert not hasattr(pt, gone), gone
        monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "comfyui")
        assert pa.enabled_for("jalsarraf") is False
        with patch("app.portrait._chain") as chain:
            assert pt.ensure_generation("jalsarraf", "cat_cafe_date", 6) is False
        chain.assert_not_called()

    def test_enabled_only_for_allowlisted_users_on_the_worker(self, monkeypatch):
        assert pa.enabled_for("jalsarraf") is True
        assert pa.enabled_for("claude") is False
        monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "openai")
        assert pa.enabled_for("jalsarraf") is False

    @pytest.mark.parametrize(
        ("raw", "limit"),
        [(None, 3), ("1", 1), ("0", 0), ("-2", 0), ("many", 0), (" 5 ", 5)],
    )
    def test_daily_limit(self, monkeypatch, raw, limit):
        if raw is not None:
            monkeypatch.setenv("KLUKAI_PORTRAIT_DAILY_LIMIT", raw)
        assert pa.daily_limit() == limit

    def test_a_zero_limit_switches_it_off(self, monkeypatch):
        monkeypatch.setenv("KLUKAI_PORTRAIT_DAILY_LIMIT", "0")
        assert pa.enabled_for("jalsarraf") is False

    def test_request_id_is_one_per_outfit_per_day(self):
        day = date(2026, 10, 19)
        a = pa.request_id("jalsarraf", "cat_cafe_date", day)
        assert a == pa.request_id("jalsarraf", "cat_cafe_date", day)
        assert str(uuid.UUID(a)) == a
        assert a != pa.request_id("jalsarraf", "lantern_walk", day)
        assert a != pa.request_id("jalsarraf", "cat_cafe_date", date(2026, 10, 20))
        assert a != pa.request_id("claude", "cat_cafe_date", day)


# ── The brief ────────────────────────────────────────────────────────────────


class TestBrief:
    def test_head_and_shoulders_on_the_stage_backdrop_in_her_outfit(self):
        brief = pa.portrait_brief("cat_cafe_date", 6)
        lower = brief.lower()
        assert "klukai alone" in lower
        assert "head and shoulders" in lower and "facing the viewer" in lower
        assert "dark navy" in lower and "bokeh" in lower
        assert "Cat Café Date" in brief
        assert "violet cardigan" in brief  # the outfit's own description
        assert "Canon anchor" not in brief  # lore notes are not drawing instructions
        assert "silver hair" in lower and "green eyes" in lower

    def test_always_covered_and_never_the_commander(self):
        for level in (0, 8, 9):
            brief = pa.portrait_brief("klukadile_pajamas", level).lower()
            assert "fully clothed" in brief
            assert "no nudity" in brief
            assert "no other people" in brief
            assert "commander" not in brief

    def test_bonded_level_never_unlocks_an_intimate_variant(self):
        with patch("app.portrait_account.portrait_prompt", return_value="P") as prompt:
            pa.portrait_brief("cat_cafe_date", 9)
        assert prompt.call_args.args[1] < pa.INTIMATE_MIN_LEVEL

    def test_unknown_outfit_still_gets_a_portrait_brief(self):
        brief = pa.portrait_brief("not_an_outfit", 3)
        assert "She is wearing" not in brief
        assert "head and shoulders" in brief.lower()

    def test_fits_the_worker_prompt_limit(self):
        assert len(pa.portrait_brief("stargazing_night", 9)) < 16_000


# ── Fitting the render to the set's frame ────────────────────────────────────


class TestFit:
    @pytest.mark.parametrize(
        "size", [(1024, 1536), (1024, 1024), (1536, 1024), (832, 1216)]
    )
    def test_cover_crops_to_the_portrait_frame(self, size):
        out = pa.fit_portrait(_png(size))
        with Image.open(io.BytesIO(out)) as img:
            assert img.format == "PNG"
            assert img.size == (pt.WIDTH, pt.HEIGHT)

    def test_keeps_the_centre(self):
        buf = io.BytesIO()
        img = Image.new("RGB", (2000, 1216), (0, 0, 0))
        img.paste((255, 255, 255), (584, 0, 1416, 1216))  # a centred 832-wide band
        img.save(buf, "PNG")
        with Image.open(io.BytesIO(pa.fit_portrait(buf.getvalue()))) as out:
            assert out.convert("RGB").getpixel((pt.WIDTH // 2, pt.HEIGHT // 2)) == (
                255,
                255,
                255,
            )
            assert out.convert("RGB").getpixel((5, 5))[0] > 200


# ── Drawing the base ─────────────────────────────────────────────────────────


class TestDrawBase:
    async def test_draws_one_base_through_the_journalled_worker_path(self, worker):
        assert await pa.draw_base("jalsarraf", "cat_cafe_date", 6) is True
        kw = worker.await_args.kwargs
        assert worker.await_args.args[0] == "jalsarraf"
        assert (kw["width"], kw["height"]) == (pt.WIDTH, pt.HEIGHT)
        assert kw["include_commander"] is False and kw["nudity"] is False
        today = account_images.local_day_start().date()
        assert kw["request_id"] == pa.request_id("jalsarraf", "cat_cafe_date", today)
        ctx = kw["archive_context"]
        assert ctx["kind"] == "portrait" and ctx["outfit"] == "cat_cafe_date"
        assert ctx["style"] == pt.STYLE_VERSION
        assert worker.await_args.args[1] == pa.portrait_brief("cat_cafe_date", 6)
        assert pt.existing_frames("jalsarraf", "cat_cafe_date") == {"base"}
        source = pt.frame_dir("jalsarraf", "cat_cafe_date") / pt.SOURCE_NAME
        with Image.open(source) as img:
            assert img.size == (pt.WIDTH, pt.HEIGHT)

    async def test_renders_inside_the_background_marker(self, worker, tmp_path):
        seen = {}

        async def fulfill(*args, **kwargs):
            seen["background"] = account_images.in_background_render()
            seen["marker"] = account_images.background_render_active()
            return {"phase": "completed", "image": _png(), "request_id": "r"}

        worker.side_effect = fulfill
        assert await pa.draw_base("jalsarraf", "cat_cafe_date", 6) is True
        assert seen == {"background": True, "marker": True}
        assert account_images.background_render_active() is False

    async def test_never_redraws_an_outfit_that_has_a_base(self, worker):
        _write(frames=("base",))
        assert await pa.draw_base("jalsarraf", "cat_cafe_date", 6) is True
        worker.assert_not_awaited()

    async def test_only_for_the_allowlisted_account(self, worker):
        assert await pa.draw_base("claude", "cat_cafe_date", 6) is False
        worker.assert_not_awaited()

    async def test_respects_the_affection_gate(self, worker):
        # cat_cafe_date unlocks at level 4.
        assert await pa.draw_base("jalsarraf", "cat_cafe_date", 2) is False
        worker.assert_not_awaited()

    async def test_daily_limit_blocks_a_new_portrait(self, worker, monkeypatch):
        monkeypatch.setattr(
            account_images, "count_requests_since", AsyncMock(return_value=3)
        )
        assert await pa.draw_base("jalsarraf", "cat_cafe_date", 6) is False
        worker.assert_not_awaited()
        since = account_images.count_requests_since.await_args.args
        assert since[0] == "portrait"
        assert since[1] == account_images.local_day_start()

    async def test_a_journalled_request_resumes_past_the_limit(
        self, worker, monkeypatch
    ):
        """Today's id already journalled (busy, or drawn but not written):
        recovering it is not a new portrait and never counts again."""
        monkeypatch.setattr(
            account_images, "count_requests_since", AsyncMock(return_value=3)
        )
        monkeypatch.setattr(
            account_images, "fetch_request", AsyncMock(return_value={"id": "r"})
        )
        assert await pa.draw_base("jalsarraf", "cat_cafe_date", 6) is True
        account_images.count_requests_since.assert_not_awaited()

    @pytest.mark.parametrize(
        "phase", ["busy", "refused", "failed", "pending", "unknown"]
    )
    async def test_no_picture_writes_nothing(self, worker, phase):
        worker.return_value = {"phase": phase, "image": None, "request_id": "r"}
        assert await pa.draw_base("jalsarraf", "cat_cafe_date", 6) is False
        assert pt.existing_frames("jalsarraf", "cat_cafe_date") == set()

    async def test_one_portrait_job_at_a_time(self, worker):
        entered = asyncio.Event()
        release = asyncio.Event()
        active = {"now": 0, "max": 0}

        async def slow(*args, **kwargs):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            entered.set()
            await release.wait()
            active["now"] -= 1
            return {"phase": "completed", "image": _png(), "request_id": "r"}

        worker.side_effect = slow
        first = asyncio.create_task(pa.draw_base("jalsarraf", "cat_cafe_date", 6))
        second = asyncio.create_task(pa.draw_base("jalsarraf", "lantern_walk", 6))
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0.01)
        release.set()
        assert await asyncio.gather(first, second) == [True, True]
        assert active["max"] == 1

    async def test_a_second_request_for_the_same_outfit_finds_the_base(self, worker):
        first = asyncio.create_task(pa.draw_base("jalsarraf", "cat_cafe_date", 6))
        second = asyncio.create_task(pa.draw_base("jalsarraf", "cat_cafe_date", 6))
        assert await asyncio.gather(first, second) == [True, True]
        worker.assert_awaited_once()

    async def test_allowance_left(self, worker, monkeypatch):
        assert await pa.allowance_left("jalsarraf", "cat_cafe_date") is True
        monkeypatch.setattr(
            account_images, "count_requests_since", AsyncMock(return_value=3)
        )
        assert await pa.allowance_left("jalsarraf", "cat_cafe_date") is False
        monkeypatch.setattr(
            account_images, "fetch_request", AsyncMock(return_value={"id": "r"})
        )
        assert await pa.allowance_left("jalsarraf", "cat_cafe_date") is True
        monkeypatch.setattr(
            account_images,
            "fetch_request",
            AsyncMock(side_effect=RuntimeError("db down")),
        )
        assert await pa.allowance_left("jalsarraf", "cat_cafe_date") is False


# ── The portrait chain and API state in account mode ─────────────────────────


class TestAccountChain:
    async def test_chain_draws_only_the_base(self, worker):
        assert await pt._chain("jalsarraf", "cat_cafe_date", 6) is True
        worker.assert_awaited_once()
        assert pt.existing_frames("jalsarraf", "cat_cafe_date") == {"base"}

    async def test_waits_for_a_quiet_chat_first(self, worker, monkeypatch):
        monkeypatch.setattr(pt, "seconds_since_user_message", lambda: 1.0)
        monkeypatch.setattr(pt, "QUIET_MAX_WAIT_S", 0)
        assert await pt._chain("jalsarraf", "cat_cafe_date", 6) is False
        worker.assert_not_awaited()
        assert not pt._backing_off(pt._key("jalsarraf", "cat_cafe_date"))

    async def test_a_failed_draw_backs_off(self, worker):
        worker.return_value = {"phase": "failed", "image": None, "request_id": "r"}
        assert await pt._chain("jalsarraf", "cat_cafe_date", 6) is False
        assert pt._backing_off(pt._key("jalsarraf", "cat_cafe_date"))

    async def test_an_error_is_contained_and_backs_off(self, worker):
        worker.side_effect = RuntimeError("socket")
        assert await pt._chain("jalsarraf", "cat_cafe_date", 6) is False
        assert pt._backing_off(pt._key("jalsarraf", "cat_cafe_date"))

    async def test_existing_base_short_circuits(self, worker):
        _write(frames=("base",))
        assert await pt._chain("jalsarraf", "cat_cafe_date", 6) is True
        worker.assert_not_awaited()

    def test_ensure_does_nothing_for_an_account_that_cannot_draw(self):
        with patch("app.portrait._chain") as chain:
            assert pt.ensure_generation("claude", "cat_cafe_date", 6) is False
        chain.assert_not_called()


class TestAccountState:
    async def test_a_cached_full_set_is_served_untouched(self, worker):
        _write()
        state = await pt.portrait_state("jalsarraf", "cat_cafe_date", 6)
        assert state["status"] == "ready"
        assert all(state["frames"][f] for f in pt.FRAME_NAMES)
        worker.assert_not_awaited()

    async def test_a_base_alone_is_ready_and_the_rest_fall_back(self, worker):
        _write(frames=("base",))
        with patch("app.portrait.ensure_generation") as ensure:
            state = await pt.portrait_state("jalsarraf", "cat_cafe_date", 6)
        assert state["status"] == "ready"  # never "partial": no polling forever
        assert state["frames"]["base"]
        assert all(state["frames"][f] is None for f in pt.EXPRESSIONS)
        ensure.assert_not_called()

    async def test_missing_base_never_enqueues(self, worker):
        with patch("app.portrait.ensure_generation", return_value=True) as ensure:
            state = await pt.portrait_state("jalsarraf", "cat_cafe_date", 6)
        assert state["status"] == "unavailable"
        ensure.assert_not_called()

    async def test_a_queued_draw_stays_pending_without_a_lookup(
        self, worker, monkeypatch
    ):
        pt._inflight[pt._key("jalsarraf", "cat_cafe_date")] = MagicMock()
        lookup = AsyncMock(side_effect=AssertionError("allowance lookup per poll"))
        monkeypatch.setattr(pa, "allowance_left", lookup)
        state = await pt.portrait_state("jalsarraf", "cat_cafe_date", 6)
        assert state["status"] == "pending"

    async def test_limit_reached_is_unavailable_without_enqueueing(
        self, worker, monkeypatch
    ):
        monkeypatch.setattr(
            account_images, "count_requests_since", AsyncMock(return_value=3)
        )
        with patch("app.portrait.ensure_generation") as ensure:
            state = await pt.portrait_state("jalsarraf", "cat_cafe_date", 6)
        assert state["status"] == "unavailable"
        ensure.assert_not_called()

    async def test_backing_off_is_unavailable(self, worker):
        pt._failed_at[pt._key("jalsarraf", "cat_cafe_date")] = pt.time.monotonic()
        with patch("app.portrait.ensure_generation") as ensure:
            state = await pt.portrait_state("jalsarraf", "cat_cafe_date", 6)
        assert state["status"] == "unavailable"
        ensure.assert_not_called()

    async def test_other_accounts_get_the_fallback(self, worker):
        with patch("app.portrait.ensure_generation") as ensure:
            state = await pt.portrait_state("claude", "cat_cafe_date", 6)
        assert state["status"] == "unavailable"
        assert state["frames"] == dict.fromkeys(pt.FRAME_NAMES)
        ensure.assert_not_called()


class TestAccountPrewarmBackfill:
    async def test_prewarm_skips_an_outfit_with_a_base(self, monkeypatch):
        _write(outfit="blazing_star", frames=("base",))
        monkeypatch.setattr(
            pt, "current_outfit", AsyncMock(return_value="blazing_star")
        )
        with patch("app.portrait.ensure_generation") as ensure:
            assert await pt.prewarm("jalsarraf", 6) is False
        ensure.assert_not_called()



    async def test_backfill_skips_outfits_with_a_base(self, monkeypatch):
        from app import wardrobe

        monkeypatch.setenv("PORTRAIT_BACKFILL", "1")
        affection = MagicMock()
        affection.get_state = AsyncMock(return_value=MagicMock(level=9))
        monkeypatch.setattr(context, "affection", affection)
        for outfit in wardrobe.catalog().values():
            _write(outfit=outfit.id, frames=("base",))
        with patch("app.portrait.ensure_generation") as ensure:
            assert await pt.backfill_tick("jalsarraf") is None
        ensure.assert_not_called()



def test_local_day_used_for_ids_is_his_day():
    # 02:00 UTC on the 20th is still the 19th in Chicago.
    now = datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)
    assert account_images.local_day_start(now).date() == date(2026, 10, 19)
