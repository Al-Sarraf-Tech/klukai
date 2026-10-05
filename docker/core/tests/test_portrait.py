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


class TestChain:
    @pytest.mark.asyncio
    async def test_generates_base_then_every_frame(self, fast):
        base = _png((10, 10, 10))
        gen = AsyncMock(return_value=base)
        i2i = AsyncMock(side_effect=lambda src, prompt, **kw: _png((kw["seed"] % 255, 0, 0)))
        with patch("app.image_gen.generate_image", new=gen), \
             patch("app.image_gen.generate_img2img", new=i2i):
            assert await pt._chain("claude", "speed_star", 4) is True
        assert pt.existing_frames("claude", "speed_star") == set(pt.FRAME_NAMES)
        seed = pt.portrait_seed("claude", "speed_star")
        assert gen.await_args.kwargs == {"width": pt.WIDTH, "height": pt.HEIGHT,
                                        "sfw": True, "seed": seed,
                                        "negative_extra": pt.PALETTE_NEGATIVE}
        assert all(c.kwargs["negative_extra"] == pt.PALETTE_NEGATIVE for c in i2i.await_args_list)
        assert [c.kwargs["denoise"] for c in i2i.await_args_list] == [
            pt.EXPRESSIONS[f][1] for f in pt.EXPRESSIONS]
        assert all(c.args[0] == base and c.kwargs["seed"] == seed and c.kwargs["sfw"] is True
                   and c.kwargs["mask_png"].startswith(b"\x89PNG")
                   for c in i2i.await_args_list)
        with Image.open(pt.frame_file("claude", "speed_star", "smile")) as img:
            assert img.format == "WEBP"
        assert pt.frame_dir("claude", "speed_star").joinpath(pt.SOURCE_NAME).read_bytes() == base

    @pytest.mark.asyncio
    async def test_bonded_portraits_are_not_sfw_forced(self, fast):
        gen = AsyncMock(return_value=_png())
        with patch("app.image_gen.generate_image", new=gen), \
             patch("app.image_gen.generate_img2img", new=AsyncMock(return_value=_png())):
            await pt._chain("claude", "speed_star", 8)
        assert gen.await_args.kwargs["sfw"] is False

    @pytest.mark.asyncio
    async def test_resumes_only_missing_frames_from_the_stored_base(self, fast):
        _write_all(frames=("base", "blink", "talk"))
        gen = AsyncMock()
        i2i = AsyncMock(return_value=_png())
        with patch("app.image_gen.generate_image", new=gen), \
             patch("app.image_gen.generate_img2img", new=i2i):
            assert await pt._chain("claude", "speed_star", 4) is True
        gen.assert_not_awaited()
        assert i2i.await_count == 3

    @pytest.mark.asyncio
    async def test_frames_without_their_base_are_all_redone(self, fast):
        _write_all(frames=("blink", "talk"))
        pt.frame_dir("claude", "speed_star").joinpath(pt.SOURCE_NAME).unlink()
        i2i = AsyncMock(return_value=_png())
        with patch("app.image_gen.generate_image", new=AsyncMock(return_value=_png())), \
             patch("app.image_gen.generate_img2img", new=i2i):
            await pt._chain("claude", "speed_star", 4)
        assert i2i.await_count == 5

    @pytest.mark.asyncio
    async def test_base_failure_backs_off(self, fast):
        with patch("app.image_gen.generate_image", new=AsyncMock(return_value=None)):
            assert await pt._chain("claude", "speed_star", 4) is False
        assert pt._backing_off(pt._key("claude", "speed_star"))
        assert pt.existing_frames("claude", "speed_star") == set()

    @pytest.mark.asyncio
    async def test_frame_failure_keeps_what_was_made_and_backs_off(self, fast):
        i2i = AsyncMock(side_effect=[_png(), None])
        with patch("app.image_gen.generate_image", new=AsyncMock(return_value=_png())), \
             patch("app.image_gen.generate_img2img", new=i2i):
            assert await pt._chain("claude", "speed_star", 4) is False
        assert pt.existing_frames("claude", "speed_star") == {"base", "blink"}
        assert pt._backing_off(pt._key("claude", "speed_star"))

    @pytest.mark.asyncio
    async def test_stops_when_a_game_starts(self, fast):
        fast.is_game_active = AsyncMock(side_effect=[False, False, True])
        i2i = AsyncMock(return_value=_png())
        with patch("app.image_gen.generate_image", new=AsyncMock(return_value=_png())), \
             patch("app.image_gen.generate_img2img", new=i2i):
            assert await pt._chain("claude", "speed_star", 4) is False
        assert i2i.await_count == 1
        assert not pt._backing_off(pt._key("claude", "speed_star"))  # a game isn't a failure

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
        with patch("app.image_gen.generate_image", new=AsyncMock()) as gen:
            assert await pt._chain("claude", "speed_star", 4) is False
        gen.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unexpected_error_is_contained(self, fast):
        with patch("app.image_gen.generate_image", new=AsyncMock(side_effect=RuntimeError("x"))):
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
            assert pt.ensure_generation("claude", "speed_star", 4) is True
            assert pt.ensure_generation("claude", "speed_star", 4) is False
            assert pt.ensure_generation("claude", "night_ride", 4) is True
            await started.wait()
            release.set()
            await asyncio.gather(*pt._inflight.values())
            await asyncio.sleep(0)
        assert chain.call_count == 2
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
