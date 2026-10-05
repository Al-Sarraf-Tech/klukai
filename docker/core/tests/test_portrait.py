"""Live Portrait — base + img2img expression frames per (user, outfit).

Covers storage layout and id safety, deterministic seeds, the prompts, signed
frame URLs, status, the lazy idempotent generation chain (one per key, base
first, yields, stops for games, backs off on failure, never overlaps a chat
turn), and refresh.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image, ImageDraw

from app import portrait as pt


def _png(color=(200, 120, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 48), color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    pt._inflight.clear()
    pt._failed_at.clear()
    yield
    pt._inflight.clear()
    pt._failed_at.clear()


@pytest.fixture
def fast(monkeypatch):
    """No start delay, no quiet-gap waiting, no game."""
    monkeypatch.setattr(pt, "START_DELAY_S", 0)
    monkeypatch.setattr(pt, "QUIET_POLL_S", 0)
    router = MagicMock()
    router.is_game_active = AsyncMock(return_value=False)
    monkeypatch.setattr(pt.context, "router", router)
    monkeypatch.setattr(pt, "seconds_since_user_message", lambda: None)
    return router


def _write_all(user="claude", outfit="speed_star", frames=pt.FRAME_NAMES):
    d = pt.frame_dir(user, outfit)
    d.mkdir(parents=True, exist_ok=True)
    (d / pt.SOURCE_NAME).write_bytes(_png())
    for f in frames:
        (d / f"{f}.webp").write_bytes(b"RIFFxxxxWEBP")


# ═══════════════════════════════════════════════════════════════════════════
# Layout, ids, seeds, prompts
# ═══════════════════════════════════════════════════════════════════════════


class TestLayout:
    def test_frames_live_under_images_portraits(self, tmp_path):
        assert pt.frame_file("claude", "speed_star", "blink") == (
            tmp_path / "portraits" / pt.STYLE_VERSION / "claude" / "speed_star" / "blink.webp"
        )

    @pytest.mark.parametrize("user, outfit", [
        ("..", "x"), ("claude", "../etc"), ("a/b", "x"), ("claude", ""), ("", "x"), ("c" * 65, "x"),
    ])
    def test_unsafe_ids_are_rejected(self, user, outfit):
        with pytest.raises(ValueError):
            pt.frame_dir(user, outfit)

    def test_unknown_frame_is_rejected(self):
        with pytest.raises(ValueError):
            pt.frame_file("claude", "speed_star", "wink")

    def test_existing_frames(self):
        assert pt.existing_frames("claude", "speed_star") == set()
        _write_all(frames=("base", "blink"))
        assert pt.existing_frames("claude", "speed_star") == {"base", "blink"}

    def test_seed_is_deterministic_per_user_and_outfit(self):
        a = pt.portrait_seed("claude", "speed_star")
        assert a == pt.portrait_seed("claude", "speed_star")
        assert a != pt.portrait_seed("claude", "night_ride")
        assert a != pt.portrait_seed("jalsarraf", "speed_star")
        assert 0 <= a < 2**32


class TestPrompts:
    def test_base_is_a_fixed_portrait_in_todays_outfit(self):
        with patch("app.portrait.build_prompt", return_value="P") as bp:
            assert pt.portrait_prompt("speed_star", 4, "base") == "P"
        scene = bp.call_args.args[0]
        for tag in ("upper body", "facing viewer", "centered", "(dark gradient background:1.2)"):
            assert tag in scene
        assert pt.BASE_EXPRESSION in scene
        kw = bp.call_args.kwargs
        assert kw["costume"] == "speed_star" and kw["affection_level"] == 4
        assert kw["request"] == "" and kw["mood"] == ""

    @pytest.mark.parametrize("frame", ["blink", "talk", "smile", "blush", "annoyed"])
    def test_expression_frames_swap_only_the_expression(self, frame):
        with patch("app.portrait.build_prompt", return_value="P") as bp:
            pt.portrait_prompt("speed_star", 4, frame)
        assert bp.call_args.args[0] == f"{pt.EXPRESSIONS[frame][0]}, {pt.COMPOSITION}"

    def test_canon_palette_is_anchored_and_vivid_colors_dropped(self):
        prompt = pt.portrait_prompt("speed_star", 4, "base")
        assert "(silver hair:1.15)" in prompt and "(pale skin:1.15)" in prompt
        assert "vivid colors" not in prompt and "film grain" not in prompt
        assert ", ," not in prompt  # dropping a tag leaves no empty slot
        assert "masterpiece" in prompt  # the rest of the quality block stays
        for frame in pt.EXPRESSIONS:
            assert pt.PALETTE_ANCHORS in pt.portrait_prompt("speed_star", 4, frame)
        assert "cyan hair" in pt.PALETTE_NEGATIVE and "pink skin" in pt.PALETTE_NEGATIVE

    def test_v3_style_has_form_depth_and_a_stage_dark_backdrop(self):
        """v3 replaces v2's flat cut-out on black: shading, light from the
        side, eye reflections, and a dark gradient with bokeh for depth."""
        assert pt.STYLE_VERSION == "v3"
        for tag in ("(soft shading:1.3)", "(soft lighting:1.2)", "(eye reflection:1.1)",
                    "(dark gradient background:1.2)", "(bokeh:1.0)", "depth of field"):
            assert tag in pt.COMPOSITION, tag
        # v2's flattening recipe is gone.
        for tag in ("simple background", "dark navy background", "soft colors", "(silver hair:1.4)"):
            assert tag not in pt.COMPOSITION, tag
        # ...and pushed away in the negative, with the light backdrops it fell back to.
        for tag in ("flat color", "(no shading:1.2)", "(posterization:1.2)", "(glowing eyes:1.3)",
                    "(white background:1.2)", "(simple background:1.1)"):
            assert tag in pt.PALETTE_NEGATIVE, tag

    def test_portrait_negative_keeps_skin_natural_and_fronts_closed(self):
        for tag in ("(tan:1.2)", "(orange skin:1.2)", "(red skin:1.2)",
                    "(unzipped:1.4)", "(cleavage:1.2)", "(blue leotard:1.2)"):
            assert tag in pt.PALETTE_NEGATIVE, tag

    def test_v3_frames_live_under_their_own_root(self, monkeypatch):
        monkeypatch.setenv("IMAGES_DIR", "/images")
        assert pt.portraits_root() == Path("/images/portraits/v3")

    def test_other_renders_keep_vivid_colors(self):
        from app.image_gen import build_prompt
        assert "vivid colors" in build_prompt("upper body", affection_level=4)

    def test_expression_table(self):
        assert set(pt.EXPRESSIONS) == set(pt.FRAME_NAMES) - {"base"}
        assert "closed eyes" in pt.EXPRESSIONS["blink"][0]
        assert "open mouth" in pt.EXPRESSIONS["talk"][0]
        assert "blush" in pt.EXPRESSIONS["blush"][0]
        assert "frown" in pt.EXPRESSIONS["annoyed"][0]
        for _tags, denoise in pt.EXPRESSIONS.values():
            assert 0.6 <= denoise <= 0.9  # masked inpaint: strong inside, nothing outside
        assert set(pt.FRAME_REGIONS) == set(pt.EXPRESSIONS)


# ═══════════════════════════════════════════════════════════════════════════
# Signed frame URLs
# ═══════════════════════════════════════════════════════════════════════════


class TestSignedUrls:
    def test_url_shape_and_round_trip(self):
        url = pt.frame_url("claude", "speed_star", "blink", 1234)
        path, query = url.split("?", 1)
        assert path == "/api/portrait/frame/claude/speed_star/blink.webp"
        params = dict(p.split("=", 1) for p in query.split("&"))
        assert params["v"] == "1234"
        assert pt.verify_frame("claude", "speed_star", "blink", params["sig"])

    def test_signature_is_bound_to_user_outfit_and_frame(self):
        url = pt.frame_url("claude", "speed_star", "blink", 1)
        sig = url.split("sig=")[1]
        assert not pt.verify_frame("jalsarraf", "speed_star", "blink", sig)
        assert not pt.verify_frame("claude", "night_ride", "blink", sig)
        assert not pt.verify_frame("claude", "speed_star", "talk", sig)
        assert not pt.verify_frame("claude", "speed_star", "blink", "")

    def test_urls_are_stable_within_a_bucket_and_expire_after_two(self):
        with patch("app.signed_urls.time.time", return_value=1_000_000.0), \
             patch("app.portrait.time.time", return_value=1_000_000.0):
            a = pt.frame_url("claude", "speed_star", "blink", 1)
        with patch("app.signed_urls.time.time", return_value=1_000_100.0), \
             patch("app.portrait.time.time", return_value=1_000_100.0):
            b = pt.frame_url("claude", "speed_star", "blink", 1)
        assert a == b  # same browser cache key while the bucket holds
        exp = int(a.split("sig=")[1].split(".")[0])
        assert pt.SIGN_BUCKET_S < exp - 1_000_000 <= 2 * pt.SIGN_BUCKET_S


# ═══════════════════════════════════════════════════════════════════════════
# Outfit resolution
# ═══════════════════════════════════════════════════════════════════════════


class TestCurrentOutfit:
    @pytest.mark.asyncio
    async def test_what_she_is_wearing_now(self):
        now = MagicMock()
        now.outfit.id = "night_ride"
        with patch("app.her_day.her_now", new=AsyncMock(return_value=now)):
            assert await pt.current_outfit("claude", 3) == "night_ride"

    @pytest.mark.asyncio
    async def test_falls_back_to_default(self):
        with patch("app.her_day.her_now", new=AsyncMock(side_effect=RuntimeError("db"))):
            assert await pt.current_outfit("claude", 3) == "blazing_star"


# ═══════════════════════════════════════════════════════════════════════════
# Generation chain
# ═══════════════════════════════════════════════════════════════════════════


def _crop_png(color=(255, 0, 255)) -> bytes:
    """What ComfyUI hands back for one branch: a CROP_PX square."""
    buf = io.BytesIO()
    Image.new("RGB", (pt.CROP_PX, pt.CROP_PX), color).save(buf, "PNG")
    return buf.getvalue()


class _FakeSet:
    """Stands in for image_gen.generate_inpaint_set, honouring its contract:
    base first (unless given), plan(base), then accept + on_image per branch."""

    def __init__(self, base: bytes | None = None, drop=(), crop=None):
        self.base = base if base is not None else _portrait_png()
        self.drop = set(drop)
        self.crop = crop or _crop_png()
        self.kwargs: dict = {}
        self.branches: list = []
        self.source = b""
        self.calls = 0

    async def __call__(self, **kw):
        self.calls += 1
        self.kwargs = kw
        out = {}
        base = kw["base_png"]
        if base is None:
            base = self.base
            out["base"] = base
            await kw["on_image"]("base", base)
        self.source, self.branches = await kw["plan"](base)
        for b in self.branches:
            if b.name in self.drop:
                continue
            kw["accept"](b.name, self.crop)
            out[b.name] = self.crop
            await kw["on_image"](b.name, self.crop)
        return out


class TestChain:
    @pytest.mark.asyncio
    async def test_one_leased_set_renders_base_and_every_frame(self, fast):
        fake = _FakeSet()
        with patch("app.image_gen.generate_inpaint_set", new=fake):
            assert await pt._chain("claude", "speed_star", 4) is True
        assert fake.calls == 1
        assert pt.existing_frames("claude", "speed_star") == set(pt.FRAME_NAMES)
        kw = fake.kwargs
        assert kw["base_png"] is None and kw["seed"] == pt.portrait_seed("claude", "speed_star")
        assert (kw["width"], kw["height"], kw["sfw"]) == (pt.WIDTH, pt.HEIGHT, True)
        assert kw["negative_extra"] == pt.PALETTE_NEGATIVE
        # The model's own sampler/CFG (the template's), portrait step counts.
        for sampling, steps in ((kw["base_sampling"], pt.BASE_STEPS), (kw["inpaint_sampling"], pt.INPAINT_STEPS)):
            assert (sampling.sampler, sampling.scheduler, sampling.cfg, sampling.steps) == (
                "euler", "normal", 4.5, steps)
        assert kw["retry_denoise_step"] == pt.BLINK_RETRY_DENOISE
        assert [b.name for b in fake.branches] == list(pt.EXPRESSIONS)
        assert [b.denoise for b in fake.branches] == [pt.EXPRESSIONS[f][1] for f in pt.EXPRESSIONS]
        assert fake.branches[0].negative == pt.FRAME_NEGATIVES["blink"]
        assert all(b.negative == "" for b in fake.branches[1:])
        with Image.open(io.BytesIO(fake.source)) as src:
            assert src.size == (pt.CROP_PX, pt.CROP_PX)
        assert pt.frame_dir("claude", "speed_star").joinpath(pt.SOURCE_NAME).read_bytes() == fake.base

    @pytest.mark.asyncio
    async def test_frames_change_only_inside_their_mask(self, fast):
        fake = _FakeSet()
        with patch("app.image_gen.generate_inpaint_set", new=fake):
            await pt._chain("claude", "speed_star", 4)
        base = Image.open(io.BytesIO(fake.base)).convert("RGB")
        with Image.open(pt.frame_file("claude", "speed_star", "talk")) as img:
            talk = img.convert("RGB")
        mouth = (420, 330 + int(0.68 * 120))
        far = (60, 1150)
        assert talk.getpixel(mouth)[1] < 60            # magenta landed on the mouth
        assert all(abs(a - b) <= 6 for a, b in zip(talk.getpixel(far), base.getpixel(far), strict=True))

    @pytest.mark.asyncio
    async def test_bonded_portraits_are_not_sfw_forced(self, fast):
        fake = _FakeSet()
        with patch("app.image_gen.generate_inpaint_set", new=fake):
            await pt._chain("claude", "speed_star", 8)
        assert fake.kwargs["sfw"] is False

    @pytest.mark.asyncio
    async def test_resumes_only_missing_frames_from_the_stored_base(self, fast):
        _write_all(frames=("base", "blink", "talk"))
        stored = _portrait_png()
        pt.frame_dir("claude", "speed_star").joinpath(pt.SOURCE_NAME).write_bytes(stored)
        fake = _FakeSet()
        with patch("app.image_gen.generate_inpaint_set", new=fake):
            assert await pt._chain("claude", "speed_star", 4) is True
        assert fake.kwargs["base_png"] == stored
        assert [b.name for b in fake.branches] == ["smile", "blush", "annoyed"]

    @pytest.mark.asyncio
    async def test_a_complete_set_does_nothing(self, fast):
        _write_all()
        fake = _FakeSet()
        with patch("app.image_gen.generate_inpaint_set", new=fake):
            assert await pt._chain("claude", "speed_star", 4) is True
        assert fake.calls == 0

    @pytest.mark.asyncio
    async def test_frames_without_their_base_are_all_redone(self, fast):
        _write_all(frames=("blink", "talk"))
        pt.frame_dir("claude", "speed_star").joinpath(pt.SOURCE_NAME).unlink()
        fake = _FakeSet()
        with patch("app.image_gen.generate_inpaint_set", new=fake):
            await pt._chain("claude", "speed_star", 4)
        assert fake.kwargs["base_png"] is None and len(fake.branches) == 5

    @pytest.mark.asyncio
    async def test_base_failure_backs_off(self, fast):
        with patch("app.image_gen.generate_inpaint_set", new=AsyncMock(return_value={})):
            assert await pt._chain("claude", "speed_star", 4) is False
        assert pt._backing_off(pt._key("claude", "speed_star"))
        assert pt.existing_frames("claude", "speed_star") == set()

    @pytest.mark.asyncio
    async def test_a_lost_frame_keeps_the_rest_and_backs_off(self, fast):
        fake = _FakeSet(drop={"talk"})
        with patch("app.image_gen.generate_inpaint_set", new=fake):
            assert await pt._chain("claude", "speed_star", 4) is False
        assert pt.existing_frames("claude", "speed_star") == set(pt.FRAME_NAMES) - {"talk"}
        assert pt._backing_off(pt._key("claude", "speed_star"))

    @pytest.mark.asyncio
    async def test_never_while_a_game_owns_the_gpu(self, fast):
        fast.is_game_active = AsyncMock(return_value=True)
        fake = _FakeSet()
        with patch("app.image_gen.generate_inpaint_set", new=fake):
            assert await pt._chain("claude", "speed_star", 4) is False
        assert fake.calls == 0
        assert not pt._backing_off(pt._key("claude", "speed_star"))  # a game isn't a failure

    @pytest.mark.asyncio
    async def test_background_delay_is_honoured(self, fast, monkeypatch):
        sleeps: list[float] = []

        async def _sleep(s):
            sleeps.append(s)

        monkeypatch.setattr(pt.asyncio, "sleep", _sleep)
        _write_all()
        assert await pt._chain("claude", "speed_star", 4, delay_s=7) is True
        assert sleeps[0] == 7

    @pytest.mark.asyncio
    async def test_waits_for_a_quiet_gap_in_the_chat(self, fast, monkeypatch):
        gaps = iter([5.0, 10.0, None])
        monkeypatch.setattr(pt, "seconds_since_user_message", lambda: next(gaps, None))
        sleeps: list[float] = []

        async def _sleep(s):
            sleeps.append(s)

        monkeypatch.setattr(pt.asyncio, "sleep", _sleep)
        assert await pt._wait_for_quiet() is True
        assert len([s for s in sleeps if s == pt.QUIET_POLL_S]) == 2

    @pytest.mark.asyncio
    async def test_quiet_gap_gives_up_eventually(self, fast, monkeypatch):
        monkeypatch.setattr(pt, "seconds_since_user_message", lambda: 1.0)
        monkeypatch.setattr(pt, "QUIET_MAX_WAIT_S", 0)
        assert await pt._wait_for_quiet() is False
        fake = _FakeSet()
        with patch("app.image_gen.generate_inpaint_set", new=fake):
            assert await pt._chain("claude", "speed_star", 4) is False
        assert fake.calls == 0

    @pytest.mark.asyncio
    async def test_unexpected_error_is_contained(self, fast):
        with patch("app.image_gen.generate_inpaint_set", new=AsyncMock(side_effect=RuntimeError("x"))):
            assert await pt._chain("claude", "speed_star", 4) is False
        assert pt._backing_off(pt._key("claude", "speed_star"))

    def test_backoff_expires(self, monkeypatch):
        key = pt._key("claude", "speed_star")
        pt._failed_at[key] = 0.0
        monkeypatch.setattr(pt.time, "monotonic", lambda: pt.FAILURE_BACKOFF_S + 1)
        assert not pt._backing_off(key)
        assert key not in pt._failed_at


class TestEnsure:
    @pytest.mark.asyncio
    async def test_one_chain_per_key(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow(*a, **k):
            started.set()
            await release.wait()
            return True

        with patch("app.portrait._chain", side_effect=_slow) as chain:
            assert pt.ensure_generation("claude", "speed_star", 4, delay_s=3) is True
            assert pt.ensure_generation("claude", "speed_star", 4) is False
            assert pt.ensure_generation("claude", "night_ride", 4) is True
            await started.wait()
            release.set()
            await asyncio.gather(*pt._inflight.values())
            await asyncio.sleep(0)
        assert chain.call_count == 2
        assert chain.call_args_list[0].args == ("claude", "speed_star", 4, 3)
        assert pt._inflight == {}

    @pytest.mark.asyncio
    async def test_not_while_backing_off(self):
        pt._failed_at[pt._key("claude", "speed_star")] = pt.time.monotonic()
        with patch("app.portrait._chain") as chain:
            assert pt.ensure_generation("claude", "speed_star", 4) is False
        chain.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# State + refresh
# ═══════════════════════════════════════════════════════════════════════════


class TestState:
    @pytest.mark.asyncio
    async def test_ready_when_every_frame_exists(self):
        _write_all()
        with patch("app.portrait.ensure_generation") as ensure:
            state = await pt.portrait_state("claude", "speed_star", 4, game_active=True)
        assert state["status"] == "ready" and state["outfit"] == "speed_star"
        assert all(state["frames"][f] for f in pt.FRAME_NAMES)
        assert "/api/portrait/frame/claude/speed_star/base.webp?v=" in state["frames"]["base"]
        ensure.assert_not_called()

    @pytest.mark.asyncio
    async def test_pending_enqueues_the_chain(self):
        with patch("app.portrait.ensure_generation", return_value=True) as ensure:
            state = await pt.portrait_state("claude", "speed_star", 4, game_active=False)
        assert state["status"] == "pending"
        assert state["frames"] == dict.fromkeys(pt.FRAME_NAMES)
        ensure.assert_called_once_with("claude", "speed_star", 4)

    @pytest.mark.asyncio
    async def test_partial_while_frames_render(self):
        _write_all(frames=("base", "blink"))
        with patch("app.portrait.ensure_generation", return_value=False):
            state = await pt.portrait_state("claude", "speed_star", 4, game_active=False)
        assert state["status"] == "partial"
        assert state["frames"]["blink"] and state["frames"]["talk"] is None

    @pytest.mark.asyncio
    async def test_unavailable_during_a_game_or_backoff(self):
        _write_all(frames=("base",))
        with patch("app.portrait.ensure_generation") as ensure:
            state = await pt.portrait_state("claude", "speed_star", 4, game_active=True)
        assert state["status"] == "unavailable"
        assert state["frames"]["base"]
        ensure.assert_not_called()
        pt._failed_at[pt._key("claude", "speed_star")] = pt.time.monotonic()
        state = await pt.portrait_state("claude", "speed_star", 4, game_active=False)
        assert state["status"] == "unavailable"


class TestRefresh:
    @pytest.mark.asyncio
    async def test_wipes_the_set_and_regenerates(self):
        _write_all()
        (pt.frame_dir("claude", "speed_star") / "keep_me").mkdir()
        pt._failed_at[pt._key("claude", "speed_star")] = pt.time.monotonic()
        with patch("app.portrait.ensure_generation", return_value=True) as ensure:
            assert pt.refresh("claude", "speed_star", 4) is True
        assert pt.existing_frames("claude", "speed_star") == set()
        assert not pt.frame_dir("claude", "speed_star").joinpath(pt.SOURCE_NAME).exists()
        ensure.assert_called_once_with("claude", "speed_star", 4)

    @pytest.mark.asyncio
    async def test_never_pulls_files_out_from_under_a_running_chain(self):
        _write_all()
        pt._inflight[pt._key("claude", "speed_star")] = MagicMock()
        assert pt.refresh("claude", "speed_star", 4) is False
        assert pt.existing_frames("claude", "speed_star") == set(pt.FRAME_NAMES)

    def test_refresh_with_nothing_on_disk(self):
        with patch("app.portrait.ensure_generation", return_value=True):
            assert pt.refresh("claude", "speed_star", 4) is True


def test_webp_conversion_and_atomic_write(tmp_path):
    data = pt._to_webp(_png())
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    target = tmp_path / "x" / "f.webp"
    pt._write_atomic(target, data)
    assert target.read_bytes() == data
    assert not list(target.parent.glob("*.tmp"))


def test_portraits_root_defaults_to_images(monkeypatch):
    monkeypatch.delenv("IMAGES_DIR")
    assert pt.portraits_root() == Path("/images/portraits") / pt.STYLE_VERSION


def test_seconds_since_user_message(monkeypatch):
    from app import llm_router
    monkeypatch.setattr(llm_router, "_last_user_message", 0.0)
    assert pt.seconds_since_user_message() is None
    monkeypatch.setattr(llm_router, "_last_user_message", pt.time.monotonic() - 30)
    assert 29 <= pt.seconds_since_user_message() < 60


# ═══════════════════════════════════════════════════════════════════════════
# Face location + masks
# ═══════════════════════════════════════════════════════════════════════════

_GREEN = (150, 230, 40)  # her canon yellow-green irises


def _portrait_png(eyes=((360, 330), (480, 330)), r=22, extra=()) -> bytes:
    img = Image.new("RGB", (pt.WIDTH, pt.HEIGHT), (235, 225, 230))
    draw = ImageDraw.Draw(img)
    for (x, y), rad in [*((e, r) for e in eyes), *extra]:
        draw.ellipse((x - rad, y - rad, x + rad, y + rad), fill=_GREEN)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class TestLocateFace:
    def test_finds_the_pair_of_green_irises(self):
        face = pt.locate_face(_portrait_png())
        assert face.found
        assert abs(face.mid_x - 420) < 6 and abs(face.eye_y - 330) < 6
        assert abs(face.d - 120) < 8

    def test_ignores_distractors(self):
        extra = (
            ((420, 900), 22),   # below the face zone (outfit trim)
            ((150, 200), 3),    # a speck: too small to be an eye
            ((640, 335), 60),   # far bigger than its neighbour: not an eye
            ((395, 334), 22),   # too close to the left eye to pair as a face
        )
        face = pt.locate_face(_portrait_png(extra=extra))
        assert face.found and abs(face.mid_x - 420) < 30

    def test_prefers_the_centred_pair(self):
        face = pt.locate_face(_portrait_png(extra=(((600, 330), 22), ((240, 330), 22))))
        assert face.found and abs(face.mid_x - 420) < 6 and abs(face.d - 120) < 8

    def test_unlevel_or_lopsided_pairs_are_rejected(self):
        assert not pt.locate_face(_portrait_png(eyes=((360, 300), (480, 380)))).found
        assert not pt.locate_face(_portrait_png(eyes=((360, 330),), extra=(((480, 330), 70),))).found

    def test_falls_back_to_the_composition(self):
        face = pt.locate_face(_png())
        assert not face.found
        assert (face.mid_x, face.eye_y) == (32 * 0.5, 48 * 0.27)


class TestFrameMask:
    _FACE = pt.Face(mid_x=420, eye_y=330, d=120)

    def _mask(self, frame) -> Image.Image:
        return Image.open(io.BytesIO(pt.frame_mask((pt.WIDTH, pt.HEIGHT), self._FACE, frame)))

    def test_blink_repaints_the_eyes_only(self):
        m = self._mask("blink")
        assert m.mode == "L"
        assert m.getpixel((360, 330)) == 255 and m.getpixel((480, 330)) == 255
        assert m.getpixel((420, 330 + int(0.68 * 120))) == 0   # mouth untouched
        assert m.getpixel((420, 330 + int(0.35 * 120))) == 0   # nose untouched
        assert m.getpixel((10, 10)) == 0

    @pytest.mark.parametrize("frame", ["talk", "smile", "blush", "annoyed"])
    def test_mouth_frames_repaint_the_mouth_not_the_nose(self, frame):
        m = self._mask(frame)
        assert m.getpixel((420, 330 + int(0.68 * 120))) == 255
        # The nose is in no region; at most a feathered edge brushes it.
        assert m.getpixel((420, 330 + int(0.35 * 120))) < 128
        assert m.getpixel((10, 1200)) == 0

    def test_edges_are_feathered(self):
        m = self._mask("talk")
        edge = m.getpixel((420 + int(0.34 * 120), 330 + int(0.68 * 120)))
        assert 0 < edge < 255
