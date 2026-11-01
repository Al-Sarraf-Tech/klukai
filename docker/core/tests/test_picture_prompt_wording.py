"""What the image model reads, and who may share a picture with what (owner 2026-11-01).

Risque outfits and implied nudity are for Klukai alone, past the intimacy gate. No other
character and not the Commander. Keep each tag list free of what the checker then rejects.
"""

from __future__ import annotations

import pytest

from app import image_prompts as ip
from app import image_provider as prov
from app import image_scene as sc
from app.helpers import enhance_image_prompt
from app.image_gen_constants import EXPLICIT_OUTFIT_KEYS, OUTFIT_MAP
from app.wardrobe_variation import composition

RISQUE = ("lingerie", "garter", "bikini", "swimsuit", "nightgown", "negligee", "camisole",
          "towel barely", "wet skin", "panties", "shirtless", "bare chest", "nude", "naked", "topless")


def _chat(request, *, level=9, costume="blazing_star"):
    squad = ip.detect_squad_members(request)
    couple = ip.is_couple_scene(request) and not squad
    brief = sc.resolve_scene(request, [], hour=15, couple=couple)
    tags = enhance_image_prompt(request, couple=couple, setting=brief)
    return ip.build_prompt(tags, couple=couple, affection_level=level, context=request,
                           squad_members=squad, costume=costume, costume_pinned=False,
                           request=request, setting=brief)


@pytest.mark.parametrize("member", sorted(ip.SQUAD_KEYWORDS))
@pytest.mark.parametrize(
    "request_words",
    ["draw you and {m} in lingerie", "draw {m} in bed", "draw {m} in the bath",
     "draw {m} in a swimsuit", "draw you and {m} kissing"],
)
def test_no_squad_member_is_ever_drawn_with_anything_intimate(member, request_words):
    request = request_words.format(m=member)
    assert prov.intimate_with_others(request), request
    assert not prov.nudity_allowed(request, intimate=True, include_commander=False)


@pytest.mark.parametrize("member", sorted(ip.SQUAD_KEYWORDS))
@pytest.mark.parametrize("level", [2, 5, 9])
@pytest.mark.parametrize("costume", ["blazing_star", "cerulean_breaker", None])
def test_a_tag_list_naming_a_squad_member_holds_nothing_intimate(member, level, costume):
    for words in (f"{member} in bed", f"{member} at the pool", f"{member} on patrol"):
        tags = _chat(f"draw you and {words}", level=level, costume=costume)
        prompt = prov.cloud_prompt(tags).lower()
        for word in RISQUE:
            assert word not in prompt, (words, word)


def test_her_pov_tags_naming_a_squad_member_get_no_intimate_outfit():
    prompt = ip.build_prompt("bed, ruchey, soft lighting", affection_level=9, costume="blazing_star")
    assert OUTFIT_MAP["bed"] not in prompt


@pytest.mark.parametrize("request_words", ["draw us in bed", "draw us in the bath"])
def test_the_commander_never_shares_an_intimate_outfit(request_words):
    prompt = _chat(request_words).lower()
    for word in RISQUE:
        assert word not in prompt, word


def test_asking_for_her_alone_does_not_add_him():
    assert not ip.is_couple_scene("draw yourself in bed")
    assert not ip.is_couple_scene("draw yourself hugging a pillow")
    assert ip.is_couple_scene("draw yourself in bed with me")
    assert ip.is_couple_scene("draw us in bed")


@pytest.mark.parametrize("level", [0, 5, 9])
@pytest.mark.parametrize("key", sorted(EXPLICIT_OUTFIT_KEYS))
def test_no_route_offers_the_explicit_outfit_entries(level, key):
    assert key not in ip._outfit_map_for(level)


def test_past_the_gate_alone_she_may_still_wear_a_risque_outfit():
    prompt = _chat("draw yourself in lingerie")
    assert "black lace lingerie" in prompt


def test_outfit_keywords_are_whole_words():
    picked = ip._select_outfit("draw us on the train", {"rain": "wet"}, "dry")
    assert picked == "dry"
    assert ip._select_outfit("black gloves", {"love": "x"}, "dry") == "dry"
    assert ip._select_outfit("after bathing", {"bath": "x"}, "dry") == "x"


def test_a_grant_is_drawn_covered():
    assert "towel" in prov.implied_cover("draw yourself in the bath")
    assert "bedsheet" in prov.implied_cover("draw yourself undressing")
    prompt = ip.build_prompt("steam", affection_level=9, costume="blazing_star",
                             request="draw yourself in the bath", cover=prov.implied_cover("bath"))
    assert "large white towel wrapped around her" in prompt and "black leotard" not in prompt


@pytest.mark.parametrize(
    ("typed", "read"),
    [
        ("draw yourself on the elmo deck", "draw yourself on the mobile base deck"),
        ("draw it", ""),
        ("Could you draw that for me?", ""),
        ("draw it\n\nSubject: the firing range", "the firing range"),
        ("draw it\n\nSubject: the Elmo's hangar\n\nNew version 2.", "the mobile base's hangar"),
        ("draw a red fox\n\nNew version 3.", "draw a red fox"),
    ],
)
def test_what_the_model_reads_of_his_request(typed, read):
    assert prov.plain_request(typed) == read


def test_no_render_term_reaches_the_model():
    text = prov.cloud_prompt(ip.build_prompt("a garden", affection_level=5, costume="blazing_star"))
    for word in ("film grain", "ambient occlusion", "volumetric", "depth of field", "detailed skin"):
        assert word not in text
    assert text.count("silver hair") == 1


def test_squad_body_words_never_reach_the_model():
    members = ["ruchey", "mechty", "belka", "welrod"]
    text = prov.cloud_prompt(ip.build_prompt("x", squad_members=members, costume="blazing_star"))
    for word in ("petite", "small build", "short stature", "busty", "large breasts", "garter"):
        assert word not in text


@pytest.mark.parametrize("place", sc.PLACES.values(), ids=lambda p: p.key)
def test_no_place_is_told_a_proper_name_or_a_screen_of_words(place):
    for word in ("Tea Time", "Klukai's", "shopfronts", "storefronts", "tactical monitors",
                 "inventory terminals", "with a game on"):
        assert word not in place.tags, word


def test_an_elmo_room_has_no_avoid_list_and_says_what_closes_it_in():
    room = sc.resolve_scene("draw the armory", [], hour=15)
    assert room.avoid() == "" and "solid walls and a low ceiling" in room.sentence()
    deck = sc.resolve_scene("draw yourself on the Elmo deck", [], hour=15)
    assert "ship" in deck.avoid()


def test_a_place_he_named_gets_a_frame_that_holds_it():
    for seed in range(40):
        got = composition("training", seed=seed, indoor=True, place_named=True,
                          place_actions=("aiming a rifle down the lane, ear protection on",))
        assert "upper body" not in got and "aiming" in got
