"""Live Portrait speed: one lease per set, one inpaint graph, face-crop
inpainting, the blink verifier, pre-generation and the idle backfill.

ComfyUI is mocked throughout (the image_gen lease fixture is reused).
"""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image, ImageDraw

import app.image_gen as ig
from app import portrait as pt
from app.gpu_lease import GPULeaseError
from app.image_gen_constants import WORKFLOW_TEMPLATE
from tests.test_image_gen_coverage import (  # noqa: F401 — autouse lease fixture
    _TEST_LEASE,
    _Resp,
    _fake_client,
    _mock_gpu_handoff,
)
from tests.test_portrait import _portrait_png


def _png(size=(32, 32), color=(10, 10, 10)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════════════
# The model's sampler (model card: Euler, CFG 4-5)
# ═══════════════════════════════════════════════════════════════════════════


class TestSampling:
    def test_template_uses_the_model_cards_sampler(self):
        ks = WORKFLOW_TEMPLATE["3"]["inputs"]
        assert (ks["sampler_name"], ks["scheduler"], ks["cfg"]) == ("euler", "normal", 4.5)
        assert 20 <= ks["steps"] <= 24

    def test_sampling_defaults_follow_the_template(self):
        s = ig.Sampling()
        ks = WORKFLOW_TEMPLATE["3"]["inputs"]
        assert (s.sampler, s.scheduler, s.steps, s.cfg) == (
            ks["sampler_name"], ks["scheduler"], ks["steps"], ks["cfg"])

    def test_txt2img_override(self):
        wf = ig._txt2img_workflow("p", 64, 96, True, 5, "x", ig.Sampling(steps=20))
        k = wf["3"]["inputs"]
        assert (k["sampler_name"], k["scheduler"], k["steps"], k["cfg"], k["seed"]) == (
            "euler", "normal", 20, 4.5, 5)
        assert wf["7"]["inputs"]["text"].endswith(", x")
        assert ig._txt2img_workflow("p", 64, 96, False, 5, None)["3"]["inputs"]["steps"] == 24


# ═══════════════════════════════════════════════════════════════════════════
# The batched inpaint graph
# ═══════════════════════════════════════════════════════════════════════════


def _branch(name, denoise=0.7, negative=""):
    return ig.InpaintBranch(name, f"{name} prompt", denoise, b"MASK-" + name.encode(), negative)


class TestInpaintGraph:
    def test_one_model_load_one_encode_one_branch_per_frame(self):
        branches = [_branch("blink", 0.85, "open eyes"), _branch("talk", 1.7)]
        graph, saves = ig._inpaint_graph(
            "src.png", branches, {"blink": "m1.png", "talk": "m2.png"}, 2**32 + 3, True, "pal",
            ig.Sampling(steps=12),
        )
        assert [n["class_type"] for n in graph.values()].count("CheckpointLoaderSimple") == 1
        assert [n["class_type"] for n in graph.values()].count("VAEEncode") == 1
        assert graph["11"]["inputs"]["image"] == "src.png"
        assert set(saves) == {"blink", "talk"}
        blink_ks = graph[str(int(saves["blink"]) - 2)]["inputs"]
        talk_ks = graph[str(int(saves["talk"]) - 2)]["inputs"]
        assert (blink_ks["sampler_name"], blink_ks["cfg"], blink_ks["steps"]) == ("euler", 4.5, 12)
        assert blink_ks["seed"] == 3 and talk_ks["denoise"] == 1.0  # wrapped, clamped
        assert blink_ks["model"] == ["16", 0] and blink_ks["latent_image"][0] in graph
        # Blink gets its own negative; talk shares the common one.
        assert blink_ks["negative"] != ["7", 0]
        assert graph[blink_ks["negative"][0]]["inputs"]["text"].endswith(", pal, open eyes")
        assert talk_ks["negative"] == ["7", 0]
        assert graph["7"]["inputs"]["text"].endswith(", pal")
        mask_node = graph[graph[talk_ks["latent_image"][0]]["inputs"]["mask"][0]]
        assert mask_node["inputs"] == {"image": "m2.png", "channel": "red"}
        assert graph[saves["talk"]]["class_type"] == "SaveImage"


class TestRunGraph:
    @pytest.mark.asyncio
    async def test_collects_every_saved_image(self):
        hist = _Resp(200, {"pid": {"outputs": {
            "105": {"images": [{"filename": "a.png"}]},
            "115": {"images": [{"filename": "b.png"}]},
            "120": {"text": ["no images here"]},
            "125": {"images": [{"filename": "gone.png"}]},
        }}})
        post = AsyncMock(return_value=_Resp(200, {"prompt_id": "pid"}))
        get = AsyncMock(side_effect=[hist, _Resp(200, content=b"A"), _Resp(200, content=b"B"), _Resp(404)])
        with patch.object(ig, "_http", _fake_client(get=get, post=post)), \
             patch("app.image_gen.asyncio.sleep", new=AsyncMock()) as sleep:
            assert await ig._run_graph({"x": 1}, _TEST_LEASE) == {"105": b"A", "115": b"B"}
        assert sleep.await_args_list[0].args == (ig._POLL_FAST_S,)  # fast polling first

    @pytest.mark.asyncio
    async def test_failures_are_empty(self):
        with patch.object(ig, "_http", _fake_client(post=AsyncMock(side_effect=OSError("x")))):
            assert await ig._run_graph({}, _TEST_LEASE) == {}
        with patch.object(ig, "_http", _fake_client(post=AsyncMock(return_value=_Resp(500)))):
            assert await ig._run_graph({}, _TEST_LEASE) == {}

    @pytest.mark.asyncio
    async def test_polling_slows_down_after_the_fast_window(self):
        get = AsyncMock(return_value=_Resp(200, {}))
        post = AsyncMock(return_value=_Resp(200, {"prompt_id": "pid"}))
        with patch.object(ig, "_http", _fake_client(get=get, post=post)), \
             patch("app.image_gen.asyncio.sleep", new=AsyncMock()) as sleep, \
             patch("app.image_gen.range", return_value=range(ig._POLL_FAST_COUNT + 2)):
            assert await ig._queue_and_wait({}, _TEST_LEASE) is None
        delays = [c.args[0] for c in sleep.await_args_list]
        assert delays[0] == ig._POLL_FAST_S and delays[-1] == 1


# ═══════════════════════════════════════════════════════════════════════════
# One lease per set
# ═══════════════════════════════════════════════════════════════════════════


class _Env:
    """Patches the leased helpers generate_inpaint_set drives."""

    def __init__(self, base=b"BASE", outputs=None, uploads=None, free=True):
        self.run_workflow = AsyncMock(return_value=base)
        self.outputs = list(outputs or [])
        self.graphs: list[dict] = []
        self.upload = AsyncMock(side_effect=uploads) if uploads else AsyncMock(
            side_effect=lambda png, lease: f"up-{len(png)}.png")
        self.free = AsyncMock(return_value=free)

    async def _run_graph(self, graph, lease):
        self.graphs.append(graph)
        return self.outputs.pop(0) if self.outputs else {}

    def __enter__(self):
        self._ps = [
            patch("app.image_gen._run_workflow", new=self.run_workflow),
            patch("app.image_gen._run_graph", new=self._run_graph),
            patch("app.image_gen._upload_image", new=self.upload),
            patch("app.image_gen._free_comfyui_vram", new=self.free),
        ]
        for p in self._ps:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._ps:
            p.stop()


def _plan(branches):
    async def plan(base):
        plan.seen = base
        return b"SOURCE", branches
    return plan


def _saves(graph) -> dict[str, str]:
    return {n["inputs"]["filename_prefix"].removeprefix("klukai_set_"): k
            for k, n in graph.items() if n["class_type"] == "SaveImage"}


class TestInpaintSet:
    @pytest.mark.asyncio
    async def test_base_then_one_graph_then_one_free(self):
        seen: list[str] = []

        async def on_image(name, png):
            seen.append(name)

        env = _Env()
        branches = [_branch("blink"), _branch("talk")]
        plan = _plan(branches)
        with env:
            env.outputs = [None]  # replaced below once save ids are known

            async def run_graph(graph, lease):
                env.graphs.append(graph)
                return {sid: name.encode() for name, sid in _saves(graph).items()}

            with patch("app.image_gen._run_graph", new=run_graph):
                out = await ig.generate_inpaint_set(
                    base_prompt="base", base_png=None, plan=plan, seed=9, sfw=True,
                    negative_extra="pal", base_sampling=ig.Sampling(steps=20),
                    inpaint_sampling=ig.Sampling(steps=12), on_image=on_image,
                )
        assert out == {"base": b"BASE", "blink": b"blink", "talk": b"talk"}
        assert seen == ["base", "blink", "talk"]
        assert plan.seen == b"BASE"
        wf = env.run_workflow.await_args.args[0]
        assert wf["3"]["inputs"]["steps"] == 20 and wf["3"]["inputs"]["seed"] == 9
        assert len(env.graphs) == 1
        assert env.upload.await_count == 3  # source once + one mask per branch
        env.free.assert_awaited_once_with(_TEST_LEASE, settle_s=ig._SET_SETTLE_S)

    @pytest.mark.asyncio
    async def test_existing_base_is_reused(self):
        env = _Env()
        with env:
            out = await ig.generate_inpaint_set(
                base_prompt="", base_png=b"STORED", plan=_plan([]), seed=1)
        assert out == {}
        env.run_workflow.assert_not_awaited()
        env.free.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_base_failure_stops_early_but_frees(self):
        env = _Env(base=None)
        plan = AsyncMock()
        with env:
            assert await ig.generate_inpaint_set(base_prompt="b", base_png=None, plan=plan, seed=1) == {}
        plan.assert_not_awaited()
        env.free.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upload_failure_keeps_the_base(self):
        env = _Env(uploads=["src.png", None])
        with env:
            out = await ig.generate_inpaint_set(
                base_prompt="b", base_png=None, plan=_plan([_branch("blink")]), seed=1)
        assert out == {"base": b"BASE"}
        assert env.graphs == []

    @pytest.mark.asyncio
    async def test_rejected_branch_is_rerolled_with_more_denoise(self):
        env = _Env()
        attempts: list[tuple[int, float]] = []

        async def run_graph(graph, lease):
            saves = _saves(graph)
            for k, n in graph.items():
                if n["class_type"] == "KSampler":
                    attempts.append((n["inputs"]["seed"], n["inputs"]["denoise"]))
            return {sid: f"{name}{len(attempts)}".encode() for name, sid in saves.items()}

        accept = MagicMock(side_effect=lambda name, png: name != "blink" or png == b"blink3")
        with env, patch("app.image_gen._run_graph", new=run_graph):
            out = await ig.generate_inpaint_set(
                base_prompt="", base_png=b"B", seed=5, accept=accept, retry_denoise_step=0.06,
                plan=_plan([_branch("blink", 0.85), _branch("talk", 0.7)]),
            )
        assert out == {"blink": b"blink3", "talk": b"talk2"}
        assert [round(d, 2) for _s, d in attempts] == [0.85, 0.7, 0.91]
        assert [s for s, _d in attempts] == [5, 5, 6]

    @pytest.mark.asyncio
    async def test_lost_or_never_accepted_branches_are_left_out(self):
        env = _Env()
        calls = {"n": 0}

        async def run_graph(graph, lease):
            calls["n"] += 1
            saves = _saves(graph)
            return {saves["blink"]: b"open"} if "blink" in saves else {}

        with env, patch("app.image_gen._run_graph", new=run_graph):
            out = await ig.generate_inpaint_set(
                base_prompt="", base_png=b"B", seed=1, retries=1, accept=lambda n, p: False,
                plan=_plan([_branch("blink"), _branch("talk")]),
            )
        assert out == {}
        assert calls["n"] == 2  # talk was lost (not retried); blink retried once

    @pytest.mark.asyncio
    async def test_exhausted_branch_may_take_its_best_rejected_render(self):
        env = _Env()
        seen = []

        async def run_graph(graph, lease):
            saves = _saves(graph)
            return {saves[name]: f"{name}{len(seen)}".encode() for name in saves}

        def fallback(name, candidates):
            seen.append((name, list(candidates)))
            return candidates[-1]

        landed = []

        async def on_image(name, png):
            landed.append(name)

        with env, patch("app.image_gen._run_graph", new=run_graph):
            out = await ig.generate_inpaint_set(
                base_prompt="", base_png=b"B", seed=1, retries=1, on_image=on_image,
                accept=lambda n, p: n != "blink", fallback=fallback,
                plan=_plan([_branch("blink"), _branch("talk")]),
            )
        assert seen == [("blink", [b"blink0", b"blink0"])]
        assert out == {"talk": b"talk0", "blink": b"blink0"}
        assert landed == ["talk", "blink"]

    @pytest.mark.asyncio
    async def test_fallback_may_decline(self):
        env = _Env()

        async def run_graph(graph, lease):
            return {v: b"x" for v in _saves(graph).values()}

        with env, patch("app.image_gen._run_graph", new=run_graph):
            out = await ig.generate_inpaint_set(
                base_prompt="", base_png=b"B", seed=1, retries=0,
                accept=lambda n, p: False, fallback=lambda n, c: None,
                plan=_plan([_branch("blink")]),
            )
        assert out == {}

    @pytest.mark.asyncio
    async def test_unconfirmed_cleanup_rejects_the_set(self):
        env = _Env(free=False)
        with env:
            assert await ig.generate_inpaint_set(
                base_prompt="b", base_png=None, plan=_plan([]), seed=1) == {}

    @pytest.mark.asyncio
    async def test_refused_lease_is_empty(self):
        with patch("app.image_gen._inpaint_set_inner", new=AsyncMock(side_effect=GPULeaseError("no"))):
            assert await ig.generate_inpaint_set(
                base_prompt="b", base_png=None, plan=_plan([]), seed=1) == {}


class TestFreeSettle:
    @pytest.mark.asyncio
    async def test_settle_is_configurable(self):
        post = AsyncMock(return_value=_Resp(200))
        with patch.object(ig, "_http", _fake_client(post=post)), \
             patch("app.image_gen.asyncio.sleep", new=AsyncMock()) as sleep:
            assert await ig._free_comfyui_vram(_TEST_LEASE, settle_s=0.25) is True
        assert sleep.await_args_list[0].args == (0.25,)


# ═══════════════════════════════════════════════════════════════════════════
# Face crop, compositing, blink verification
# ═══════════════════════════════════════════════════════════════════════════


class TestFaceBox:
    def test_centred_below_the_eyes(self):
        box = pt.face_box(pt.Face(420, 330, 100), (832, 1216))
        assert box[2] - box[0] == box[3] - box[1] == 340
        assert (box[0] + box[2]) / 2 == pytest.approx(420, abs=1)
        assert (box[1] + box[3]) / 2 == pytest.approx(360, abs=1)

    def test_clamped_inside_the_image(self):
        assert pt.face_box(pt.Face(20, 10, 100), (832, 1216))[:2] == (0, 0)
        x0, y0, x1, y1 = pt.face_box(pt.Face(820, 1200, 100), (832, 1216))
        assert (x1, y1) == (832, 1216)
        assert pt.face_box(pt.Face(50, 50, 1000), (300, 200)) == (0, 0, 200, 200)


def _mask(size, box) -> Image.Image:
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rectangle(box, fill=255)
    return m


class TestIrisPixels:
    def test_counts_green_under_the_mask_only(self):
        img = Image.new("RGB", (100, 100), (230, 220, 225))
        ImageDraw.Draw(img).rectangle((10, 10, 29, 29), fill=(150, 230, 40))
        ImageDraw.Draw(img).rectangle((60, 60, 79, 79), fill=(150, 230, 40))
        assert pt._iris_pixels(img, _mask((100, 100), (0, 0, 49, 49))) == 100  # 20x20 at half res
        assert pt._iris_pixels(img, Image.new("L", (100, 100), 0)) == 0


class TestSetPlan:
    @pytest.mark.asyncio
    async def test_plan_cuts_one_source_and_a_mask_per_frame(self):
        plan = pt._SetPlan("claude", "speed_star", 4, ["blink", "talk"])
        source, branches = await plan(_portrait_png())
        with Image.open(io.BytesIO(source)) as src:
            assert src.size == (pt.CROP_PX, pt.CROP_PX)
        assert [b.name for b in branches] == ["blink", "talk"]
        assert branches[0].negative == pt.FRAME_NEGATIVES["blink"] and branches[1].negative == ""
        for b in branches:
            with Image.open(io.BytesIO(b.mask_png)) as m:
                assert m.size == (pt.CROP_PX, pt.CROP_PX)

    @pytest.mark.asyncio
    async def test_blink_is_accepted_only_when_her_eyes_close(self):
        plan = pt._SetPlan("claude", "speed_star", 4, ["blink", "talk"])
        await plan(_portrait_png())
        skin = _png((pt.CROP_PX, pt.CROP_PX), (235, 225, 230))
        x0, y0, x1, y1 = plan.box
        open_eyes = plan.base.crop(plan.box).resize((pt.CROP_PX, pt.CROP_PX))
        buf = io.BytesIO()
        open_eyes.save(buf, "PNG")
        assert plan.accept("blink", skin) is True
        assert plan.accept("blink", buf.getvalue()) is False
        assert plan.accept("talk", buf.getvalue()) is True
        # Composite: outside the mask the base is untouched.
        full = plan.composite("blink", skin)
        assert full.getpixel((5, 5)) == plan.base.getpixel((5, 5))

    @pytest.mark.asyncio
    async def test_blink_fallback_takes_the_most_closed_if_half_shut(self):
        plan = pt._SetPlan("claude", "speed_star", 4, ["blink", "talk"])
        await plan(_portrait_png())
        skin = _png((pt.CROP_PX, pt.CROP_PX), (235, 225, 230))
        open_eyes = plan.base.crop(plan.box).resize((pt.CROP_PX, pt.CROP_PX))
        buf = io.BytesIO()
        open_eyes.save(buf, "PNG")
        wide_open = buf.getvalue()
        assert plan.fallback("blink", [wide_open, skin]) == skin  # the most-closed one
        assert plan.fallback("blink", [wide_open]) is None         # still wide open: no blink
        assert plan.fallback("talk", [skin]) is None                # only blink falls back
        assert plan.fallback("blink", []) is None

    def test_fallback_needs_a_planned_base(self):
        plan = pt._SetPlan("claude", "speed_star", 4, ["blink"])
        assert plan.fallback("blink", [b"x"]) is None

    def test_unjudgeable_blinks_are_accepted(self):
        plan = pt._SetPlan("claude", "speed_star", 4, ["blink"])
        assert plan.accept("blink", b"anything") is True  # no base planned yet
        plan.base = Image.new("RGB", (64, 64), (200, 200, 200))  # no green irises at all
        plan.masks["blink"] = Image.new("L", (64, 64), 255)
        assert plan.accept("blink", b"anything") is True


# ═══════════════════════════════════════════════════════════════════════════
# Pre-generation + idle backfill
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def portraits(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    pt._inflight.clear()
    pt._failed_at.clear()
    yield
    pt._inflight.clear()
    pt._failed_at.clear()


def _complete(user, outfit):
    d = pt.frame_dir(user, outfit)
    d.mkdir(parents=True, exist_ok=True)
    for f in pt.FRAME_NAMES:
        (d / f"{f}.webp").write_bytes(b"x")


class TestPrewarm:
    @pytest.mark.asyncio
    async def test_missing_set_is_drawn_in_the_background(self, portraits):
        with patch("app.portrait.current_outfit", new=AsyncMock(return_value="speed_star")), \
             patch("app.portrait.ensure_generation", return_value=True) as ensure:
            assert await pt.prewarm("claude", 4) is True
        ensure.assert_called_once_with("claude", "speed_star", 4, delay_s=pt.START_DELAY_S)

    @pytest.mark.asyncio
    async def test_complete_set_is_left_alone(self, portraits):
        _complete("claude", "speed_star")
        with patch("app.portrait.current_outfit", new=AsyncMock(return_value="speed_star")), \
             patch("app.portrait.ensure_generation") as ensure:
            assert await pt.prewarm("claude", 4) is False
        ensure.assert_not_called()

    @pytest.mark.asyncio
    async def test_prewarm_soon_is_fire_and_forget(self, portraits):
        with patch("app.portrait.prewarm", new=AsyncMock(side_effect=RuntimeError("db"))) as pw:
            pt.prewarm_soon("claude", 4)
            await asyncio.gather(*list(pt._background))
        pw.assert_awaited_once_with("claude", 4)
        assert not pt._background

    def test_prewarm_soon_without_a_loop_is_a_no_op(self):
        pt.prewarm_soon("claude", 4)  # no running loop: nothing to do, no error


class TestBackfill:
    def _env(self, monkeypatch, *, gap=None, game=False, level=4):
        monkeypatch.setenv("PORTRAIT_BACKFILL", "1")
        monkeypatch.setattr(pt, "seconds_since_user_message", lambda: gap)
        router = MagicMock()
        router.is_game_active = AsyncMock(return_value=game)
        aff = MagicMock()
        aff.get_state = AsyncMock(return_value=SimpleNamespace(level=level))
        monkeypatch.setattr(pt.context, "router", router)
        monkeypatch.setattr(pt.context, "affection", aff)

    @pytest.mark.asyncio
    async def test_opt_in_only(self, portraits, monkeypatch):
        monkeypatch.delenv("PORTRAIT_BACKFILL", raising=False)
        assert pt.backfill_enabled() is False
        assert await pt.backfill_tick("claude") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("gap, game", [(60.0, False), (None, True)])
    async def test_never_while_chatting_or_gaming(self, portraits, monkeypatch, gap, game):
        self._env(monkeypatch, gap=gap, game=game)
        with patch("app.portrait.ensure_generation") as ensure:
            assert await pt.backfill_tick("claude") is None
        ensure.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_set_at_a_time(self, portraits, monkeypatch):
        self._env(monkeypatch)
        pt._inflight["claude/x"] = MagicMock()
        assert await pt.backfill_tick("claude") is None

    @pytest.mark.asyncio
    async def test_draws_the_next_unlocked_outfit_without_a_set(self, portraits, monkeypatch):
        self._env(monkeypatch, gap=pt.BACKFILL_SILENCE_S + 1, level=0)
        from app import wardrobe
        unlocked = [o for o in wardrobe.catalog().values()
                    if wardrobe.is_unlocked(o.id, 0) and wardrobe.is_visible(o, 0)]
        _complete("claude", unlocked[0].id)
        with patch("app.portrait.ensure_generation", side_effect=[False, True]) as ensure:
            picked = await pt.backfill_tick("claude")
        assert picked == unlocked[2].id
        called = [c.args[1] for c in ensure.call_args_list]
        assert called == [unlocked[1].id, unlocked[2].id]
        assert all(wardrobe.is_unlocked(o, 0) for o in called)

    @pytest.mark.asyncio
    async def test_nothing_left_to_draw(self, portraits, monkeypatch):
        self._env(monkeypatch)
        with patch("app.portrait.existing_frames", return_value=set(pt.FRAME_NAMES)):
            assert await pt.backfill_tick("claude") is None


# ═══════════════════════════════════════════════════════════════════════════
# Hooks: wardrobe picks and the scheduler
# ═══════════════════════════════════════════════════════════════════════════


class TestHooks:
    @pytest.mark.asyncio
    async def test_connect_warm_up_prewarms(self):
        from app import context, wardrobe
        aff = MagicMock()
        aff.get_state = AsyncMock(return_value=SimpleNamespace(level=5))
        with patch.object(context, "affection", aff), \
             patch("app.wardrobe.ensure_today", new=AsyncMock()), \
             patch("app.portrait.prewarm_soon") as soon:
            await wardrobe.warm_today("claude")
        soon.assert_called_once_with("claude", 5)

    @pytest.mark.asyncio
    async def test_pwa_pick_prewarms(self):
        from app import wardrobe
        row = SimpleNamespace(persisted=True, day=None, requested_outfit_id=None)
        with patch("app.wardrobe.ensure_today", new=AsyncMock(return_value=row)), \
             patch("app.wardrobe._record_request", new=AsyncMock(return_value=True)), \
             patch("app.wardrobe.replace", side_effect=lambda r, **kw: r), \
             patch("app.portrait.prewarm_soon") as soon:
            await wardrobe.set_requested("claude", "speed_star", 4)
        soon.assert_called_once_with("claude", 4)

    @pytest.mark.asyncio
    async def test_accepted_chat_request_prewarms(self):
        from app import wardrobe
        row = SimpleNamespace(persisted=True, day=None, request_changes=0, requested_outfit_id=None)
        outcome = SimpleNamespace(decision="comply")
        with patch("app.wardrobe.ensure_today", new=AsyncMock(return_value=row)), \
             patch("app.wardrobe.catalog", return_value={"speed_star": MagicMock()}), \
             patch("app.wardrobe.decide_request", return_value=outcome), \
             patch("app.wardrobe.occasions_for", new=AsyncMock(return_value=frozenset())), \
             patch("app.wardrobe._record_request", new=AsyncMock(return_value=True)), \
             patch("app.wardrobe.replace", side_effect=lambda r, **kw: r), \
             patch("app.portrait.prewarm_soon") as soon:
            assert await wardrobe.handle_request("claude", "speed_star", 4, current_id="x") is outcome
        soon.assert_called_once_with("claude", 4)

    @pytest.mark.asyncio
    async def test_backfill_job_is_registered_and_never_replayed(self):
        from app.proactive import durability as dur
        from app.proactive.engine import ProactiveEngine
        e = ProactiveEngine()
        try:
            e.start()
            job = e._scheduler.get_job("portrait_backfill")
            fields = {f.name: str(f) for f in job.trigger.fields}
            assert (fields["hour"], fields["minute"]) == ("2-6", "*/15")
        finally:
            e.stop()
        assert "portrait_backfill" in dur.NEVER_CATCH_UP

    @pytest.mark.asyncio
    async def test_backfill_job_delegates_for_the_primary_user(self):
        from app.decisions import PRIMARY_USER
        from app.proactive.engine import ProactiveEngine
        with patch("app.portrait.backfill_tick", new=AsyncMock()) as tick:
            await ProactiveEngine()._portrait_backfill()
        tick.assert_awaited_once_with(PRIMARY_USER)
