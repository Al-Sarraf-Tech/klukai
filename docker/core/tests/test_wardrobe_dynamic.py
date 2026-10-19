"""Dynamic outfits in pictures: she dresses for the scene, owns a bigger
lore-anchored wardrobe, and each render varies its composition, hairstyle and
small accessories — deterministically, and never for Live Portrait."""

from __future__ import annotations

import random
import re

import pytest

from app import wardrobe as w
from app import wardrobe_scene as ws
from app import wardrobe_variation as wv
from app.image_gen_constants import (
    AFFECTION_MOOD_TAGS,
    KLUKAI_IDENTITY,
    KLUKAI_LORA_TRIGGER,
    MOOD_EXPRESSION_TAGS,
    OUTFIT_MAP,
    QUALITY_TAGS,
)
from app.image_prompts import build_prompt
from app.personality import load_personality
from app.proactive.state import now_local

NEW_OUTFITS = {
    "cat_cafe_date": 4,
    "stargazing_night": 5,
    "paddock_pass": 3,
    "lantern_walk": 3,
    "fireworks_party": 2,
    "tea_counter": 3,
    "terrace_cover_up": 2,
}
SEEDS = range(40)


@pytest.fixture(scope="module")
def cat() -> dict:
    return w.catalog(load_personality())


def _outfit_block(prompt: str) -> str:
    """The tags after her identity block (outfit + composition + scene)."""
    return prompt.split(KLUKAI_IDENTITY, 1)[1]


# ── Bigger wardrobe ─────────────────────────────────────────────────────────


class TestNewOutfits:
    @pytest.mark.parametrize("oid,level", sorted(NEW_OUTFITS.items()))
    def test_loads_through_the_catalog_with_a_canon_anchor(self, cat, oid, level):
        o = cat[oid]
        assert o.source == "original"
        assert o.unlock_level == level
        assert "Canon anchor:" in o.description, oid
        assert "canon:" in str(load_personality()["costumes"][oid]["type"]), oid
        assert o.image_tags and o.name and o.blurb and o.stance and o.aliases

    @pytest.mark.parametrize("oid", sorted(NEW_OUTFITS))
    def test_tags_keep_her_lore(self, cat, oid):
        tags = cat[oid].image_tags
        assert any(h in tags for h in ("ponytail", "hair down")), oid
        # Silver jewelry only (gold is canon on the maid outfit alone); no
        # school uniforms, no aged-down looks; never her identity tags.
        for banned in ("gold", "school", "serafuku", "aged down", "child", "loli",
                       "blazer", "hair color", "blue eyes", "red eyes"):
            assert banned not in tags, (oid, banned)
        assert "silver hair" not in tags and "teardrop" not in tags

    def test_each_new_outfit_is_reachable_from_a_scene(self):
        reachable = {oid for s in ws.SCENES for oid in s.outfits}
        assert set(NEW_OUTFITS) <= reachable

    def test_new_aliases_do_not_steal_existing_requests(self):
        assert w.detect_outfit_request("wear the party dress") == "fireworks_party"
        assert w.detect_outfit_request("wear the dress") == "formal_commission"
        assert w.detect_outfit_request("put on the cat ears") == "black_cat_ops"
        assert w.detect_outfit_request("put on your cat ear headphones") == "cat_cafe_date"
        assert w.detect_outfit_request("wear your racing jacket") == "astral_luminous"

    def test_variation_overrides_parse(self, cat):
        assert cat["dress_uniform"].hair == ("high ponytail", "low ponytail", "single hair bun")
        assert cat["stargazing_night"].accessories == (
            "earrings", "bracelet", "scrunchie", "cat_headphones",
        )
        assert cat["blazing_star"].hair is None and cat["blazing_star"].accessories is None
        bare = w.catalog({"costumes": {"x": {"hair": [], "accessories": "earrings"}}})["x"]
        assert bare.hair == () and bare.accessories == ("earrings",)

    def test_catalog_variation_lists_name_known_entries(self, cat):
        for o in cat.values():
            for h in o.hair or ():
                assert h in wv.HAIR_STYLES or h == "ponytail", (o.id, h)
            for a in o.accessories or ():
                assert a in wv.ACCESSORIES, (o.id, a)


# ── Dress for the scene ─────────────────────────────────────────────────────


class TestSceneOf:
    @pytest.mark.parametrize("text,scene", [
        ("show me you at the gym", "training"),
        ("draw you at the firing range", "training"),
        ("what do you look like in the rain?", "rain"),
        ("a ride in the rain", "rain"),
        ("you in the snow", "snow"),
        ("show me us at the beach", "beach"),
        ("by the pool", "swim"),
        ("you in the garage fixing the bike", "garage"),
        ("take me for a motorcycle ride", "ride"),
        ("show me you reading", "reading"),
        ("your sketchbook", "reading"),
        ("what do you look like at bedtime", "bedtime"),
        ("draw us at a gala", "formal"),
        ("the medal ceremony", "ceremony"),
        ("you at the cafe", "cafe"),
        ("running errands downtown", "cafe"),
        ("a cat café date", "cat_cafe"),
        ("our movie date", "date"),
        ("watching the stars", "stargazing"),
        ("the powersports show", "powersports"),
        ("a Racing Calm, please", "tea"),
        ("lanterns at Mid-Autumn", "lanterns"),
        ("New Year’s fireworks", "fireworks"),
        ("trick or treat", "halloween"),
        ("christmas morning", "christmas"),
    ])
    def test_detects(self, text, scene):
        assert ws.scene_of(text) == scene

    @pytest.mark.parametrize("text", [
        "", None, "draw me a picture of you", "sketch yourself", "a drawing of you",
        "good morning", "at night", "in the bedroom doorway", "pooling light", "train station",
        "standing, looking at viewer, detailed background",
    ])
    def test_whole_words_only_and_picture_words_never_dress_her(self, text):
        assert ws.scene_of(text) is None

    def test_every_candidate_exists_and_none_is_denied(self, cat):
        for s in ws.SCENES:
            for oid in s.outfits:
                assert oid in cat, (s.key, oid)
                assert not cat[oid].deny, (s.key, oid)
                assert cat[oid].category != "oath", (s.key, oid)


class TestSceneOutfit:
    @pytest.mark.parametrize("scene,expected", [
        ("training", "range_kit"),
        ("rain", "rain_shell"),
        ("snow", "winter_patrol"),
        ("swim", "cerulean_breaker"),
        ("garage", "hangar_coveralls"),
        ("reading", "drawing_night"),
        ("bedtime", "sleepless_watch"),
        ("ceremony", "dress_uniform"),
        ("cafe", "off_duty_cap"),
        ("stargazing", "stargazing_night"),
        ("powersports", "paddock_pass"),
        ("tea", "tea_counter"),
        ("lanterns", "lantern_walk"),
        ("fireworks", "fireworks_party"),
        ("cat_cafe", "cat_cafe_date"),
        ("halloween", "black_cat_ops"),
        ("christmas", "red_scarf_sortie"),
    ])
    def test_single_candidate_scenes(self, cat, scene, expected):
        assert ws.scene_outfit(scene, 9, current="blazing_star", cat=cat) == expected

    @pytest.mark.parametrize("scene,choices", [
        ("ride", {"night_ride", "speed_star", "astral_luminous"}),
        ("beach", {"cerulean_breaker", "terrace_cover_up"}),
        ("formal", {"formal_commission", "dress_uniform"}),
        ("date", {"cat_cafe_date", "off_duty_cap"}),
    ])
    def test_multi_candidate_scenes_pick_among_them(self, cat, scene, choices):
        picks = {
            ws.scene_outfit(scene, 9, current="blazing_star", cat=cat, rng=random.Random(s))
            for s in SEEDS
        }
        assert picks == choices

    @pytest.mark.parametrize("scene,level", [
        ("garage", 0),      # coveralls open at 1
        ("reading", 3),     # glasses at 4
        ("bedtime", 5),     # sleepwear at 6
        ("formal", 1),      # dress uniform at 2, the gown at 6
        ("stargazing", 4),
        ("cat_cafe", 3),
        ("halloween", 2),
    ])
    def test_locked_scene_outfit_keeps_todays_outfit(self, cat, scene, level):
        assert ws.scene_outfit(scene, level, current="blazing_star", cat=cat) is None

    def test_falls_back_to_the_unlocked_candidate(self, cat):
        assert ws.scene_outfit("formal", 4, current="blazing_star", cat=cat) == "dress_uniform"
        picks = {ws.scene_outfit("ride", 0, cat=cat, rng=random.Random(s)) for s in SEEDS}
        assert picks == {"speed_star"}  # night ride opens at 1, Astral Luminous at 4

    def test_already_dressed_for_it_keeps_her_choice(self, cat):
        assert ws.scene_outfit("ride", 9, current="astral_luminous", cat=cat) is None
        assert ws.scene_outfit("rain", 9, current="rain_shell", cat=cat) is None

    def test_fail_closed_gates(self):
        cat = w.catalog({"costumes": {
            "denied": {"image_tags": "hair down", "deny": True},
            "hidden": {"image_tags": "hair down", "private": [7, 9]},
            "tagless": {},
        }})
        for oid in ("denied", "hidden", "tagless"):
            assert not ws.eligible(cat[oid], 6), oid
        assert not ws.eligible(None, 9)
        assert ws.eligible(cat["hidden"], 7)
        assert ws.scene_outfit("nope", 9, cat=cat) is None
        assert ws.dress_for_scene("show me you at the gym", 9, current="blazing_star") == "range_kit"


# ── Precedence: explicit request > bath/bed > scene > today's outfit ───────


class TestPrecedence:
    def test_scene_beats_todays_outfit(self):
        p = build_prompt("standing", costume="blazing_star", affection_level=0,
                         request="show me you at the gym")
        assert "compression shirt" in p and "baseball cap" not in p

    def test_locked_scene_outfit_falls_back_to_today(self):
        p = build_prompt("standing", costume="blazing_star", affection_level=0,
                         request="show me you in the garage")
        assert "baseball cap" in p and "mechanic" not in p

    def test_no_request_reads_the_pictures_own_scene_tags(self):
        p = build_prompt("reading a book curled up on a couch, blanket", costume="blazing_star",
                         affection_level=6, context="the gym was brutal today")
        assert "metal-framed glasses" in p and "compression shirt" not in p

    def test_her_replies_never_set_the_scene(self):
        p = build_prompt("smiling", costume="blazing_star", affection_level=5,
                         context="Klukai: I was at the gym all morning.", request="send me a selfie")
        assert "compression shirt" not in p and "baseball cap" in p

    def test_granted_request_is_never_redressed(self):
        p = build_prompt("x", costume="immaculate_service", affection_level=5,
                         request="show me you at the gym", costume_pinned=True)
        assert "maid headdress" in p and "compression shirt" not in p

    def test_granted_request_found_through_her_day_cache(self):
        day = w.her_date(now_local())
        w._today_cache["claude"] = (0.0, w.HerDayRow(day, "blazing_star", "x", "immaculate_service"))
        assert w.granted_request("immaculate_service")
        assert not w.granted_request("blazing_star")
        assert not w.granted_request(None)
        p = build_prompt("x", costume="immaculate_service", affection_level=5,
                         request="show me you at the gym")
        assert "maid headdress" in p
        # Yesterday's request no longer pins anything.
        w._today_cache["claude"] = (0.0, w.HerDayRow(day.replace(year=day.year - 1),
                                                     "blazing_star", "x", "immaculate_service"))
        assert not w.granted_request("immaculate_service")
        assert "compression shirt" in build_prompt(
            "x", costume="immaculate_service", affection_level=5, request="show me you at the gym")

    def test_outfit_named_in_his_request_suppresses_scene_dressing(self):
        # He asked for the coat; she refused (today's outfit stands) — the
        # scene must not put her in something else behind the request's back.
        p = build_prompt("x", costume="blazing_star", affection_level=5,
                         request="wear your coat and show me at the gym")
        assert "baseball cap" in p and "compression shirt" not in p

    def test_granted_named_outfit_beats_the_bath_rule(self):
        p = build_prompt("onsen", costume="immaculate_service", affection_level=8,
                         request="wear the maid outfit and draw us in the bath")
        assert "maid headdress" in p and "towel" not in p

    def test_bath_rule_beats_the_scene(self):
        req = "draw us in the bath at the beach"
        p = build_prompt("onsen", context=req, costume="blazing_star", affection_level=8, request=req)
        assert "towel" in p
        for tag in ("bikini", "sundress", "baseball cap"):
            assert tag not in p

    def test_explicit_intimate_scene_is_exactly_as_before(self):
        req = "draw us in the bath"
        expected = ", ".join([
            QUALITY_TAGS, KLUKAI_LORA_TRIGGER, AFFECTION_MOOD_TAGS[8],
            MOOD_EXPRESSION_TAGS["composed"], KLUKAI_IDENTITY, OUTFIT_MAP["bath"], "onsen",
        ])
        for seed in (None, 1, "x"):
            got = build_prompt("onsen", context=req, costume="immaculate_service",
                               affection_level=8, request=req, variation_seed=seed)
            assert got == expected

    def test_intimacy_gate_holds_below_eight(self):
        for level in (5, 6, 7):
            p = build_prompt("x", costume="blazing_star", affection_level=level,
                             request="draw you asleep in bed")
            for tag in ("nightgown", "lingerie", "nude", "towel", "negligee"):
                assert tag not in p, (level, tag)
        # The scene's own sleepwear (6+) is fully buttoned; below it, today's outfit.
        assert "(fully buttoned:1.3)" in build_prompt(
            "x", costume="blazing_star", affection_level=6, request="draw you asleep in bed")
        assert "baseball cap" in build_prompt(
            "x", costume="blazing_star", affection_level=5, request="draw you asleep in bed")

    def test_unknown_or_missing_costume_keeps_the_keyword_fallback(self):
        for costume in (None, "not_real"):
            p = build_prompt("portrait", costume=costume, affection_level=9,
                             request="show me you at the gym")
            assert "compression shirt" not in p


# ── Varied poses, hair and accessories ─────────────────────────────────────


class TestVariation:
    def test_same_seed_same_picture_plan(self):
        kw = dict(costume="off_duty_cap", affection_level=7, request="show me you at the cafe")
        for seed in (1, "abc"):
            assert build_prompt("x", variation_seed=seed, **kw) == build_prompt(
                "x", variation_seed=seed, **kw)
        # No seed: derived from the inputs, so still reproducible...
        assert build_prompt("x", **kw) == build_prompt("x", **kw)
        assert wv.derive_seed("a", None, 1) == wv.derive_seed("a", None, 1)
        assert wv.derive_seed("a", None, 1) != wv.derive_seed("a", None, 2)
        # ...and different seeds give different pictures.
        assert len({build_prompt("x", variation_seed=s, **kw) for s in SEEDS}) > 10

    def test_pools_are_deterministic_per_seed(self, cat):
        o = cat["off_duty_cap"]
        for s in SEEDS:
            assert wv.styled_tags(o, 7, s) == wv.styled_tags(o, 7, s)
            assert wv.composition("cafe", seed=s) == wv.composition("cafe", seed=s)
            r1, r2 = wv.variation_rng(s, "hair"), wv.variation_rng(s, "hair")
            assert wv.pick_hair(o, 7, r1) == wv.pick_hair(o, 7, r2)

    def test_identity_never_varies(self):
        for s in SEEDS:
            p = build_prompt("x", costume="off_duty_cap", affection_level=9,
                             request="show me you at the cafe", variation_seed=s)
            assert p.count(KLUKAI_IDENTITY) == 1
            block = _outfit_block(p)
            for banned in ("blue hair", "blonde", "red eyes", "blue eyes", "black hair"):
                assert banned not in block

    def test_one_hairstyle_and_only_allowed_ones(self, cat):
        hair_tags = set(wv.HAIR_STYLES) | {"ponytail"}
        for o in cat.values():
            for s in SEEDS:
                tags = [t.strip() for t in wv.styled_tags(o, 9, s).split(",")]
                styles = [t for t in tags if t in hair_tags]
                assert len(styles) == 1, (o.id, styles)
                assert styles[0] in wv.hair_options(o, 9), (o.id, styles)

    @pytest.mark.parametrize("oid", [
        "formal_commission", "immaculate_service", "indigo_oath", "sleepless_watch",
        "klukadile_pajamas",
    ])
    def test_fixed_outfits_never_change(self, cat, oid):
        for s in SEEDS:
            assert wv.styled_tags(cat[oid], 9, s) == cat[oid].image_tags

    def test_hair_down_and_glasses_wait_for_the_bond(self, cat):
        o = cat["off_duty_cap"]
        for s in SEEDS:
            low = wv.styled_tags(o, 2, s)
            assert "hair down" not in low and "glasses" not in low
        assert "hair down" in wv.hair_options(o, 3)
        assert "glasses" in wv.accessory_options(o, 4)
        assert "glasses" not in wv.accessory_options(o, 3)
        # An outfit worn with its hair down keeps it at any level.
        assert "hair down" in wv.hair_options(cat["drawing_night"], 0)
        assert "hair down" not in wv.hair_options(cat["off_duty_cap"], 0)

    def test_accessories_never_contradict_the_outfit(self, cat):
        # A cap (or the maid headdress) leaves no room for the cat-ear earphones;
        # a scarf hides a necklace; the glasses outfit already has glasses.
        assert "cat_headphones" not in wv.accessory_options(cat["off_duty_cap"], 9)
        assert "cat_headphones" not in wv.accessory_options(cat["speed_star"], 9)
        assert "cat_headphones" in wv.accessory_options(cat["astral_luminous"], 9)
        assert "necklace" not in wv.accessory_options(cat["stargazing_night"], 9)
        assert "glasses" not in wv.accessory_options(cat["drawing_night"], 9)
        assert "jacket_off" not in wv.accessory_options(cat["stargazing_night"], 9)
        assert "jacket_off" not in wv.accessory_options(cat["cat_cafe_date"], 9)  # no jacket
        assert wv.accessory_options(cat["dress_uniform"], 9) == []
        for o in cat.values():
            for s in SEEDS:
                tags = wv.styled_tags(o, 9, s)
                assert "gold" not in tags.replace(o.image_tags, ""), o.id
                assert tags.count("earrings") <= 1 and tags.count("necklace") <= 1, o.id

    def test_jacket_off_ties_it_round_her_waist(self, cat):
        o = cat["blazing_star"]
        styled = {wv.styled_tags(o, 9, s) for s in SEEDS}
        off = [t for t in styled if "jacket around waist" in t]
        assert off, "jacket_off never drawn in 40 seeds"
        for t in off:
            assert "off shoulder" not in t and "purple jacket" in t

    def test_scrunchie_only_with_tied_hair(self, cat):
        o = cat["off_duty_cap"]
        assert "scrunchie" not in wv.accessory_options(o, 9, "hair down")
        assert "scrunchie" in wv.accessory_options(o, 9, "single braid")

    def test_composition_respects_the_callers_framing_and_pose(self):
        assert wv.composition(None, framing_text="upper body, smiling", seed=1) in wv.SOLO_ACTIONS
        assert wv.composition(None, framing_text="sitting on a bench", seed=1) in wv.CAMERA_CUES
        assert wv.composition(None, framing_text="selfie, sitting", seed=1) == ""
        assert wv.composition("bedtime", seed=3) in wv.CAMERA_CUES  # no action at bedtime
        assert wv.composition(None, couple=True, framing_text="upper body", seed=2) in wv.COUPLE_ACTIONS
        for s in SEEDS:
            assert wv.composition("garage", framing_text="upper body", seed=s) in wv.SCENE_ACTIONS["garage"]

    def test_composition_pools_stay_sfw(self):
        cues = " ".join(wv.CAMERA_CUES + wv.SOLO_ACTIONS + wv.COUPLE_ACTIONS
                        + tuple(c for v in wv.SCENE_ACTIONS.values() for c in v))
        for banned in ("from below", "from above", "from behind", "seductive", "lying",
                       "spread", "cleavage", "panties", "bed"):
            assert banned not in cues, banned

    def test_explicit_pose_wins(self):
        pool = set(wv.CAMERA_CUES) | set(wv.SOLO_ACTIONS)
        p = build_prompt("x", costume="blazing_star", affection_level=5, pose="saluting, full body")
        assert "saluting, full body, x" in p
        assert not any(c in p for c in pool)
        none = build_prompt("x", costume="blazing_star", affection_level=5, pose="")
        assert not any(c in none for c in pool)

    def test_composition_sits_before_the_scene_tags(self):
        p = build_prompt("tail scene", costume="blazing_star", affection_level=5, variation_seed=4)
        assert p.endswith(", tail scene")
        assert any(c in p for c in wv.CAMERA_CUES)


# ── Live Portrait is untouched ──────────────────────────────────────────────


class TestLivePortraitUnaffected:
    @pytest.mark.parametrize("request_text", ["", "show me you at the gym", None])
    def test_neutral_fixed_base(self, cat, request_text):
        scene = "calm expression, upper body, facing viewer"
        expected = ", ".join([QUALITY_TAGS, KLUKAI_LORA_TRIGGER, KLUKAI_IDENTITY,
                              cat["speed_star"].image_tags, scene])
        for seed in (None, 1, 2, "x"):
            got = build_prompt(scene, affection_level=6, costume="speed_star", mood="",
                               request=request_text, affection_tags=False, variation_seed=seed)
            assert got == expected

    def test_portrait_prompt_is_stable(self):
        from app import portrait as pt

        a = pt.portrait_prompt("off_duty_cap", 7, "base")
        assert a == pt.portrait_prompt("off_duty_cap", 7, "base")
        assert re.search(r"jacket around waist|scrunchie|cat ear headphones|glasses", a) is None
