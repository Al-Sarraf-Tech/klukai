"""The scene brief: a picture happens where the conversation is.

Owner complaint (2026-10-19): "she favors some and they are out of context
(like when I say we are in my room she draws outside)". These scenarios pin
the brief's strict order (his request > his recent words > where she is, for
a picture of her alone > a neutral setting for the hour) and that every later
step of the prompt follows it.
"""

from __future__ import annotations

import collections
import re
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from app import image_scene as sc
from app import wardrobe as w
from app import wardrobe_scene as ws
from app import wardrobe_variation as wv
from app.helpers import enhance_image_prompt
from app.image_gen_constants import (
    AFFECTION_MOOD_TAGS,
    COMMANDER_DEFAULT_OUTFIT,
    COMMANDER_OUTFIT_MAP,
    OUTFIT_MAP,
)
from app.image_prompts import build_prompt, is_couple_scene
from app.image_provider import _NUDITY_CUE
from app.personality import load_personality

SKY = re.compile(r"\b(?:sky|moon|moonlight|moonlit|stars?|starry)\b")
OPEN_AIR = re.compile(
    r"\b(?:sky|moonlight|umbrella|wind|floating hair|outdoors|street|skyline|road|"
    r"highway|walking, mid-stride|walking side by side)\b"
)
SHIPLIKE = re.compile(r"\b(?:ship|boat|ocean|sea|naval|hull|deck|porthole|galley)\b")


def _turns(*lines: tuple[str, str]) -> list[dict[str, str]]:
    return [{"role": role, "content": text} for role, text in lines]


def _brief(request, *lines, hour=15, her_place=None, seed=None):
    return sc.resolve_scene(
        request,
        _turns(*lines),
        hour=hour,
        couple=is_couple_scene(request),
        her_place=her_place,
        seed=seed,
    )


def _compose(
    request, *lines, hour=15, her_place=None, level=5, costume="blazing_star", seed=1
):
    """What background_image_gen composes for one picture."""
    brief = _brief(request, *lines, hour=hour, her_place=her_place)
    couple = is_couple_scene(request)
    tags = enhance_image_prompt(request, couple=couple, setting=brief)
    prompt = build_prompt(
        tags,
        couple=couple,
        affection_level=level,
        context=request,
        costume=costume,
        costume_pinned=False,
        request=request,
        setting=brief,
        variation_seed=seed,
    )
    return brief, prompt


# ── The owner's scenarios ───────────────────────────────────────────────────


class TestOwnerScenarios:
    ROOM = (
        ("user", "we are in my room"),
        (
            "assistant",
            "(I keep one eye on the screen.) It's 1854 and I'm in the rec room.",
        ),
        ("user", "long day honestly"),
        ("assistant", "Then sit. I'll keep watch."),
        ("user", "take a picture of us"),
        ("assistant", "(I lift the camera.) Hold still, Commander."),
    )

    def test_his_room_beats_her_rec_room_and_stays_indoors(self):
        for seed in range(30):
            brief, prompt = _compose("take a picture of us", *self.ROOM, seed=seed)
            assert (brief.key, brief.source, brief.indoor) == (
                "his_bedroom",
                sc.HIS_HISTORY,
                True,
            )
            assert "the Commander's bedroom" in prompt
            assert "rec room" not in prompt
            assert not OPEN_AIR.search(prompt), (seed, prompt)

    def test_draw_yourself_alone_is_where_her_day_puts_her(self):
        brief = _brief("draw yourself", her_place=("Hangar", "tuning the bike"))
        assert (brief.key, brief.source) == ("hangar", sc.HER_DAY)

    def test_newer_move_to_the_beach_wins(self):
        brief = _brief(
            "draw us",
            ("user", "I'm in my room right now"),
            ("assistant", "Noted."),
            ("user", "let's go to the beach"),
        )
        assert (brief.key, brief.source, brief.indoor) == (
            "beach",
            sc.HIS_HISTORY,
            False,
        )

    def test_not_outside_is_never_outdoors(self):
        for seed in map(str, range(40)):
            brief = _brief("draw us, not outside", seed=seed)
            assert brief.indoor, brief.key
        # ...and it vetoes an older outdoor plan too.
        older = _brief("draw us, not outside", ("user", "let's go to the park"))
        assert older.indoor and older.key != "park"

    def test_elmo_deck_is_a_land_vessel(self):
        brief, prompt = _compose("draw yourself on the Elmo deck", seed=3)
        assert brief.key == "elmo_topside" and brief.place.elmo
        assert "land vehicle" in prompt and "tank treads" in prompt
        assert not SHIPLIKE.search(prompt), prompt
        assert not SHIPLIKE.search(brief.sentence())
        for word in ("ship", "boat", "ocean"):
            assert word in brief.avoid()

    def test_night_indoors_has_no_sky(self):
        for seed in range(30):
            brief, prompt = _compose(
                "take a picture of us",
                ("user", "we're in my room tonight"),
                hour=23,
                seed=seed,
            )
            assert brief.time == "night" and brief.indoor
            assert "night, warm lamp light, city lights through the window" in prompt
            assert not SKY.search(prompt), prompt

    def test_rain_indoors_is_rain_on_the_window(self):
        brief, prompt = _compose(
            "send me a picture of us",
            ("user", "it's raining hard outside, I'm staying in my room"),
        )
        assert (brief.key, brief.weather) == ("his_bedroom", "rain")
        assert "rain on the window" in prompt
        assert "umbrella" not in prompt and "wet" not in prompt.split(", ")

    def test_plain_request_with_no_context_is_neutral(self):
        brief = _brief("draw us together")
        assert brief.source == sc.DEFAULT
        assert brief.key in {k for k, _ in sc.DEFAULTS}


# ── Strict order ────────────────────────────────────────────────────────────


class TestPriority:
    def test_his_request_beats_his_history(self):
        brief = _brief("draw us in the kitchen", ("user", "we are in my room"))
        assert (brief.key, brief.source) == ("kitchen", sc.REQUEST)

    @pytest.mark.parametrize(
        "request_text,key",
        [
            ("draw us in my room", "his_bedroom"),
            ("here at my place, take a picture of us", "his_home"),
            ("draw us on the couch", "living_room"),
            ("draw us in the car", "car"),
            ("draw yourself at the hangar", "hangar"),
            ("draw us in the kitchen at my place", "kitchen"),
            ("draw us at the cafe", "cafe"),
            ("show me you at the gym", "gym"),
            ("draw yourself in the training room", "training_room"),
            ("draw us in my garage", "garage"),
            ("draw us on the train", "transit"),
            ("draw us on the command deck", "command_center"),
            ("draw yourself in the mess hall", "mess_hall"),
            ("draw yourself behind the tea counter", "tea_counter"),
            ("draw yourself in your quarters", "her_quarters"),
            ("draw us at the movies", "cinema"),
            ("draw us riding the motorcycle at night", "road"),
            ("draw us on the elmo's roof", "elmo_topside"),
            ("draw us inside the hangar", "hangar"),
            ("draw us at the logistics bay", "logistics_bay"),
            ("draw us under the stars", "outside"),
        ],
    )
    def test_place_in_his_request(self, request_text, key):
        brief = _brief(request_text)
        assert (brief.key, brief.source) == (key, sc.REQUEST)

    @pytest.mark.parametrize(
        "line,key",
        [
            ("we are in my room", "his_bedroom"),
            ("I'm at home", "his_home"),
            ("I'm home", "his_home"),
            ("come over", "his_home"),
            ("let's go to the beach", "beach"),
            ("we're watching a movie on the couch", "living_room"),
            ("I'm going to bed", "bedroom"),
            ("just sitting at my desk", "office"),
            ("got back from the beach, I'm in the kitchen now", "kitchen"),
            ("we're at the cafe downtown", "cafe"),
            ("let's watch the sunset from the rooftop", "rooftop"),
            ("I'm not leaving my room tonight", "his_bedroom"),
            ("can't wait, heading to the park", "park"),
        ],
    )
    def test_he_established_it(self, line, key):
        brief = _brief("take a picture of us", ("user", line))
        assert (brief.key, brief.source) == (key, sc.HIS_HISTORY)

    def test_newest_of_his_turns_wins(self):
        brief = _brief(
            "draw us",
            ("user", "at the park now"),
            ("user", "ok we're in the kitchen"),
        )
        assert brief.key == "kitchen"

    def test_her_replies_never_override_his_place(self):
        brief = _brief(
            "draw yourself",
            ("user", "we are in my room"),
            ("assistant", "I'm in the hangar."),
            her_place=("Command deck", "squad briefing"),
        )
        assert (brief.key, brief.source) == ("his_bedroom", sc.HIS_HISTORY)

    def test_her_reply_only_places_a_picture_of_her_alone(self):
        couple = _brief("take a picture of us", ("assistant", "I'm in the rec room."))
        assert couple.source == sc.DEFAULT and couple.key != "rec_room"
        alone = _brief("show me where you are", ("assistant", "I'm in the rec room."))
        assert (alone.key, alone.source) == ("rec_room", sc.HER_REPLY)

    def test_her_stated_place_beats_the_roster(self):
        brief = _brief(
            "draw yourself",
            ("assistant", "(I set down the controller.) I'm in the rec room."),
            her_place=("Hangar", "tuning the bike"),
        )
        assert (brief.key, brief.source) == ("rec_room", sc.HER_REPLY)

    def test_her_possessives_are_hers(self):
        assert _brief("draw yourself", ("assistant", "I'm in my room.")).key == (
            "her_quarters"
        )
        assert _brief("draw yourself", ("assistant", "I'm in your room.")).key == (
            "his_bedroom"
        )

    def test_her_third_person_lines_are_not_where_she_is(self):
        brief = _brief(
            "draw yourself",
            ("assistant", "Mechty is in the kitchen again."),
            her_place=("Armory", "weapons inspection"),
        )
        assert (brief.key, brief.source) == ("armory", sc.HER_DAY)

    def test_the_request_itself_is_not_counted_twice(self):
        brief = _brief(
            "draw us at the beach",
            ("user", "draw us at the beach"),
            ("assistant", "Sand. Fine."),
        )
        assert brief.source == sc.REQUEST

    def test_only_his_last_turns_count(self):
        lines = [("user", "we're at the beach")] + [("user", "hm")] * sc.HIS_TURNS
        assert _brief("draw us", *lines).source == sc.DEFAULT

    def test_flattened_chat_context_still_works(self):
        turns = sc.turns_from_context(
            "user: we are in my room\nand it's late\nassistant: I'm in the rec room."
        )
        assert turns[0] == {
            "role": "user",
            "content": "we are in my room\nand it's late",
        }
        brief = sc.resolve_scene("draw us", turns, hour=12, couple=True)
        assert brief.key == "his_bedroom"
        assert sc.turns_from_context("") == []
        assert sc.turns_from_context("no roles here") == []

    def test_bad_turns_are_ignored(self):
        brief = sc.resolve_scene(
            "draw us",
            [
                {"role": "system", "content": "at the beach"},
                {"role": "user", "content": 3},
            ],
            hour=9,
            couple=True,
        )
        assert brief.source == sc.DEFAULT


# ── Negation, moves and talk that places nothing ───────────────────────────


class TestNegationAndMoves:
    def test_not_at_the_beach_anymore(self):
        brief = _brief(
            "draw us",
            ("user", "we were at the beach all day"),
            ("user", "we're not at the beach anymore, we're home now"),
        )
        assert brief.key == "his_home"

    def test_a_newer_veto_skips_an_older_place(self):
        brief = _brief(
            "draw us",
            ("user", "we're in my room"),
            ("user", "let's go to the beach"),
            ("user", "nah, not the beach"),
        )
        assert brief.key == "his_bedroom"

    def test_move_outside_beats_the_older_room(self):
        brief = _brief(
            "draw us",
            ("user", "we're in my room"),
            ("user", "let's go outside now"),
        )
        assert (brief.key, brief.indoor) == ("outside", False)

    def test_staying_inside_keeps_the_older_room(self):
        brief = _brief(
            "draw us",
            ("user", "we're in my room"),
            ("user", "let's stay inside tonight"),
        )
        assert brief.key == "his_bedroom"

    def test_going_outside_keeps_an_older_outdoor_place(self):
        brief = _brief(
            "draw us",
            ("user", "we're at the park"),
            ("user", "ok let's go outside"),
        )
        assert brief.key == "park"

    def test_outside_with_nothing_older_is_open_air(self):
        brief = _brief("draw us outside")
        assert (brief.key, brief.source, brief.indoor) == ("outside", sc.REQUEST, False)

    def test_not_inside_means_outdoors(self):
        for seed in map(str, range(20)):
            assert not _brief("draw us, not inside", seed=seed).indoor

    def test_weather_talk_is_not_a_place(self):
        brief = _brief(
            "draw us",
            ("user", "I'm in my room."),
            ("user", "it's raining outside"),
        )
        assert brief.key == "his_bedroom" and brief.weather == "rain"
        request = _brief("draw us in my room, it's cold outside")
        assert request.key == "his_bedroom"

    @pytest.mark.parametrize(
        "line",
        [
            "I wish we were at the beach",
            "remember the cafe yesterday?",
            "do you like the beach?",
            "Belka is in the kitchen",
            "we left the beach",
            "got back from the beach",
            "we're going to the beach tomorrow",
            "the beach is overrated",
            "I need to park somewhere",
            "it's nice outside",
            "we were at the beach all day",
            "I was in the kitchen earlier",
            "ugh, traffic on the highway was awful",
        ],
    )
    def test_talk_that_places_nothing(self, line):
        brief = _brief("take a picture of us", ("user", line))
        assert brief.source == sc.DEFAULT, brief.key

    def test_a_typed_were_is_we_re(self):
        assert _brief("draw us", ("user", "were in my room lol")).key == "his_bedroom"

    def test_her_past_is_not_where_she_is(self):
        brief = _brief(
            "draw yourself",
            ("assistant", "I was in the hangar earlier."),
            her_place=("Lounge", "off duty"),
        )
        assert (brief.key, brief.source) == ("elmo_lounge", sc.HER_DAY)

    def test_proposals_are_moves(self):
        brief = _brief("draw us", ("user", "want to go to the beach?"))
        assert brief.key == "beach"

    def test_a_washroom_is_never_inferred_from_earlier_turns(self):
        assert _brief("draw us", ("user", "I'm taking a bath")).source == sc.DEFAULT
        assert _brief("draw yourself in the bath").key == "washroom"

    def test_meteor_shower_is_outdoors(self):
        assert _brief("draw us watching the meteor shower").key == "outside"

    def test_negated_place_in_the_request(self):
        brief = _brief("draw us, not at the beach", ("user", "let's go to the beach"))
        assert brief.key != "beach"
        assert _brief("draw us at the park instead of the beach").key == "park"


# ── Time and weather ────────────────────────────────────────────────────────


class TestTimeAndWeather:
    @pytest.mark.parametrize(
        "hour,period",
        [(6, "morning"), (13, "afternoon"), (19, "evening"), (2, "night")],
    )
    def test_clock(self, hour, period):
        brief = _brief("draw us", hour=hour)
        assert (brief.time, brief.time_source) == (period, "clock")

    def test_his_words_beat_the_clock(self):
        brief, prompt = _compose("draw us in my room this morning", hour=22)
        assert (brief.time, brief.time_source) == ("morning", "words")
        assert "morning, soft daylight through the window" in prompt

    def test_outdoor_weather_and_grey_light(self):
        brief, prompt = _compose("draw us at the park in the rain", hour=21)
        assert brief.indoor is False and brief.weather == "rain"
        assert "umbrella" in prompt and "glowing streetlights" in prompt
        assert "moonlight" not in prompt

    def test_snow_indoors_and_out(self):
        inside = _brief("draw us in the kitchen, it's snowing")
        assert inside.weather == "snow"
        assert "snow falling beyond the window" in inside.tags()
        outside = _brief("draw us at the park, it's snowing")
        assert "falling snowflakes" in outside.tags()

    def test_weather_from_his_recent_words_only(self):
        brief = _brief(
            "draw us in my room",
            ("user", "it's pouring"),
            ("assistant", "Snow on the hull this morning."),
        )
        assert brief.weather == "rain"
        assert _brief("draw us", ("user", "it's not raining")).weather is None
        assert _brief("draw us", ("user", "I wish it would snow")).weather is None

    @pytest.mark.parametrize("key", sorted(k for k, p in sc.PLACES.items() if p.indoor))
    def test_indoors_never_shows_sky(self, key):
        place = sc.PLACES[key]
        for time in sc.TIMES:
            for weather in (None, "rain", "snow"):
                tags = ", ".join(
                    [
                        place.tags,
                        sc.time_tags(place, time, indoor=True, weather=weather),
                        sc.weather_tags(place, weather, indoor=True),
                    ]
                )
                assert not SKY.search(tags), (key, time, tags)
                assert "umbrella" not in tags


# ── The prompt follows the brief ────────────────────────────────────────────


class TestPromptFollowsBrief:
    def test_indoor_actions_never_need_open_air(self):
        for key, place in sc.PLACES.items():
            if not place.indoor:
                continue
            brief = sc.SceneBrief(place, sc.REQUEST, "evening", "clock")
            for seed in range(25):
                for couple in (False, True):
                    cue = wv.composition(
                        None,
                        couple=couple,
                        seed=seed,
                        indoor=True,
                        place_actions=brief.place.actions,
                    )
                    assert not (set(cue.split(", ")) & {"wind", "floating hair"}), key
                    assert "walking" not in cue, (key, cue)

    def test_outdoor_keeps_the_open_air_actions(self):
        cues = {
            wv.composition(None, seed=s, indoor=False, framing_text="upper body")
            for s in range(60)
        }
        assert "wind, floating hair" in cues

    def test_scene_dressing_that_cannot_happen_indoors_is_dropped(self):
        brief, prompt = _compose(
            "draw us, it's raining", ("user", "we're in my room"), level=9
        )
        assert brief.wardrobe_scene("draw us, it's raining") is None
        assert "raincoat" not in prompt
        assert "looking up at the rain" not in prompt
        assert "rain on the window" in prompt

    def test_he_chose_the_beach_so_she_dresses_for_it(self):
        beach_kits = (
            w.catalog()["cerulean_breaker"].image_tags,
            w.catalog()["terrace_cover_up"].image_tags,
        )
        brief, prompt = _compose("draw us", ("user", "let's go to the beach"), level=9)
        assert brief.wardrobe_scene("draw us") == "beach"
        assert any(tag in prompt for tag in ("black bikini", "sundress")), beach_kits

    def test_her_day_place_never_redresses_her(self):
        brief = _brief("draw yourself", her_place=("Hangar", "tuning the bike"))
        assert brief.wardrobe_scene("draw yourself") is None
        # His own words still dress her when they fit the setting.
        assert brief.wardrobe_scene("draw yourself reading") == "reading"

    def test_affection_setting_words_cannot_move_the_picture(self):
        for level in (0, 1, 2, 3, 5):
            _, prompt = _compose(
                "take a picture of us", ("user", "we're in my room"), level=level
            )
            assert " setting" not in prompt, (level, prompt)
            mood = AFFECTION_MOOD_TAGS[level].split(", ")[0]
            assert mood in prompt

    def test_his_outfit_fits_the_place(self):
        _, home = _compose("take a picture of us", ("user", "we're in my room"))
        assert COMMANDER_OUTFIT_MAP["home"] in home
        _, elmo = _compose("draw us in the hangar")
        assert COMMANDER_DEFAULT_OUTFIT in elmo
        # His own words still choose first.
        _, kiss = _compose("draw us kissing", ("user", "we're in my room"))
        assert COMMANDER_OUTFIT_MAP["home"] not in kiss

    def test_setting_words_never_pick_an_outfit(self):
        # No costume: the keyword fallback must not read "bedroom" or "night".
        _, prompt = _compose(
            "take a picture of us",
            ("user", "we're in my room tonight"),
            hour=23,
            level=9,
            costume=None,
        )
        assert OUTFIT_MAP["night"] not in prompt and OUTFIT_MAP["bed"] not in prompt
        assert COMMANDER_OUTFIT_MAP["night"] not in prompt

    def test_request_moods_survive_but_old_keywords_do_not_stack(self):
        brief = _brief("draw us hugging", ("user", "we're at the beach, it's sunset"))
        tags = enhance_image_prompt("draw us hugging", couple=True, setting=brief)
        assert "hug" in tags and "embrace" in tags
        assert "sunset" not in tags and "beach" not in tags

    @pytest.mark.parametrize(
        "text",
        [
            "great, draw us after the update",
            "ready? draw us in my bedroom",
            "draw us with a cookie and a crystal",
        ],
    )
    def test_request_keywords_are_whole_words(self, text):
        brief = _brief(text)
        tags = enhance_image_prompt(text, couple=True, setting=brief)
        for wrong in ("eating", "reading", "lying on bed", "apron", "tears", "candles"):
            assert wrong not in tags, (text, tags)

    @pytest.mark.parametrize(
        "text,tag",
        [
            ("draw us smiling", "smile"),
            ("draw us hugging", "hug"),
            ("draw yourself asleep", "sleeping"),
            ("draw us cuddling", "cuddling"),
            ("draw yourself crying", "tears"),
            ("draw us kissing", "kiss"),
        ],
    )
    def test_request_keywords_with_their_endings(self, text, tag):
        brief = _brief(text)
        assert tag in enhance_image_prompt(text, couple=True, setting=brief)

    def test_request_tags_drop_what_the_setting_owns(self):
        inside = sc.SceneBrief(sc.PLACES["kitchen"], sc.REQUEST, "night", "clock")
        tags = sc.request_tags(
            "draw us at night in the rain on the motorcycle, cooking in bed this morning",
            inside,
        )
        for gone in (
            "night sky",
            "moonlight",
            "umbrella",
            "wind",
            "road",
            "kitchen",
            "bed",
        ):
            assert gone not in tags.split(", "), gone
        assert "cooking" in tags
        # The night and morning entries belong to the brief, whole.
        assert "quiet" not in tags and "waking up" not in tags
        bedroom = sc.SceneBrief(sc.PLACES["his_bedroom"], sc.REQUEST, "night", "clock")
        assert "lying on bed" in sc.request_tags("draw us in bed", bedroom, couple=True)

    def test_squad_named_in_his_request(self):
        brief = _brief("draw yourself with mechty")
        tags = sc.request_tags("draw yourself with mechty", brief)
        assert "g11" in tags and "multiple girls" in tags
        assert "multiple girls" not in sc.request_tags(
            "draw us with mechty", brief, couple=True
        )

    def test_no_setting_keeps_the_legacy_tags(self):
        assert "golden hour" in enhance_image_prompt("draw us watching the sunset")
        legacy = enhance_image_prompt("draw belka")
        assert "belka" in legacy and "multiple girls" in legacy

    def test_setting_closes_the_prompt(self):
        brief, prompt = _compose("draw us in the kitchen", seed=2)
        assert prompt.endswith(brief.tags())

    def test_explicit_rules_are_untouched(self):
        # The bath rule still needs his own word and the intimacy gate.
        brief, prompt = _compose(
            "draw yourself in the bath", level=8, costume="blazing_star"
        )
        assert OUTFIT_MAP["bath"] in prompt and brief.key == "washroom"
        _, low = _compose("draw yourself in the bath", level=5, costume="blazing_star")
        assert "towel barely covering" not in low
        # A setting from his earlier words never makes it explicit.
        _, room = _compose("take a picture", ("user", "we're in my room"), level=9)
        assert OUTFIT_MAP["bed"] not in room

    def test_live_portrait_ignores_inference(self):
        p = build_prompt(
            "calm expression, upper body, in the kitchen",
            affection_level=6,
            costume="speed_star",
            mood="",
            affection_tags=False,
            time_of_day="night",
        )
        assert "moonlight" in p  # its fixed base is unchanged

    def test_pov_tags_that_say_indoors_keep_the_light_inside(self):
        p = build_prompt(
            "rec room, worn couch, controller in hand",
            affection_level=6,
            costume="blazing_star",
            time_of_day="night",
            variation_seed=4,
        )
        assert "moonlight" not in p
        assert "night, glow of the big screen" in p
        outdoors = build_prompt(
            "lake, paper lanterns",
            affection_level=6,
            costume="blazing_star",
            time_of_day="night",
            variation_seed=4,
        )
        assert "moonlight" in outdoors


# ── Favoritism ──────────────────────────────────────────────────────────────

_FILLER = [
    "long day honestly",
    "how was training",
    "I missed you",
    "tell me about Belka's ledger",
    "what are you thinking about",
    "you look tired",
    "did Mechty eat",
    "I finished the report",
    "hey",
    "that's funny",
]
_REQUESTS = [
    "take a picture of us",
    "draw us together",
    "send me a pic of us",
    "show me us",
    "what would we look like",
]
_SOLO = ["draw yourself", "send me a selfie", "show me you", "take a picture for me"]


class TestNoFavorites:
    def test_two_hundred_unplaced_pictures_spread_out(self):
        places: collections.Counter[str] = collections.Counter()
        actions: collections.Counter[str] = collections.Counter()
        hours = (8, 13, 18, 23)
        for i in range(200):
            couple_turn = i % 3 != 0
            request = (_REQUESTS if couple_turn else _SOLO)[i % 4]
            lines = [
                ("user", _FILLER[(i * 7 + k) % len(_FILLER)]) for k in range(1 + i % 3)
            ] + [("assistant", f"Understood, Commander. It's {1000 + i} hours.")]
            brief, prompt = _compose(request, *lines, hour=hours[i % 4], seed=f"s{i}")
            assert brief.source == sc.DEFAULT
            places[brief.key] += 1
            pool = (
                wv.COUPLE_ACTIONS
                + wv.INDOOR_COUPLE_ACTIONS
                + wv.SOLO_ACTIONS
                + wv.INDOOR_SOLO_ACTIONS
                + brief.place.actions
            )
            picked = [a for a in pool if a in prompt]
            actions[max(picked, key=len) if picked else "-"] += 1
        assert len(places) >= 9
        assert max(places.values()) <= 0.2 * 200, places.most_common(3)
        assert max(actions.values()) <= 0.2 * 200, actions.most_common(3)

    def test_default_choice_depends_on_the_conversation_not_a_fixed_order(self):
        picks = {
            _brief("draw us", ("user", f"line {i}"), hour=13).key for i in range(60)
        }
        assert len(picks) >= 6

    def test_default_respects_the_hour(self):
        for i in range(60):
            brief = _brief("draw us", ("user", f"line {i}"), hour=23)
            assert brief.key not in {
                "park",
                "lakeside",
                "window_seat",
                "elmo_topside",
                "cafe",
            }

    def test_same_conversation_same_brief(self):
        a = _brief("draw us", ("user", "line"), hour=13)
        assert a == _brief("draw us", ("user", "line"), hour=13)


# ── The lexicon ─────────────────────────────────────────────────────────────


class TestLexicon:
    def test_every_phrase_names_one_place(self):
        seen: dict[str, str] = {}
        for place in sc.PLACES.values():
            for phrase in place.phrases:
                assert seen.setdefault(phrase, place.key) == place.key, phrase
        assert len(seen) > 250

    def test_a_phrase_naming_two_places_fails_at_import(self, monkeypatch):
        twin = sc.Place("twin", "twin", True, ("kitchen",), "x")
        monkeypatch.setitem(sc.PLACES, "twin", twin)
        with pytest.raises(ValueError, match="kitchen"):
            sc._phrase_table()

    def test_scene_dressing_keys_exist(self):
        keys = {s.key for s in ws.SCENES}
        for place in sc.PLACES.values():
            assert place.wardrobe is None or place.wardrobe in keys, place.key
            assert sc.scene_fits(place.wardrobe, place.indoor), place.key

    def test_place_words_never_grant_nudity(self):
        for place in sc.PLACES.values():
            assert not _NUDITY_CUE.search(place.tags), place.key
            for time in sc.TIMES:
                assert not _NUDITY_CUE.search(
                    sc.time_tags(place, time, indoor=place.indoor)
                )

    def test_elmo_is_never_a_ship(self):
        for place in sc.PLACES.values():
            if place.elmo:
                assert sc.ELMO in place.tags or "Elmo" in place.tags, place.key
                assert not SHIPLIKE.search(place.tags), place.key
                assert not SHIPLIKE.search(f"{place.window} {place.night_view}"), (
                    place.key
                )

    def test_place_actions_stay_sfw(self):
        cues = " ".join(a for p in sc.PLACES.values() for a in p.actions)
        for banned in ("lying", "bed", "seductive", "from below", "from behind"):
            assert banned not in cues, banned

    def test_every_her_day_location_has_a_place(self):
        cfg = load_personality()["her_day"]
        for activity in cfg["activities"]:
            place = sc.her_day_place(activity["location"], activity.get("activity", ""))
            assert place.key != "elmo", activity["location"]
        for override in cfg["overrides"].values():
            assert sc.her_day_place(override["location"]).key != "elmo"
        assert sc.her_day_place("Lounge", "Tea Time counter duty").key == "tea_counter"
        assert sc.her_day_place("Off duty").key == "elmo"
        assert sc.her_day_place("Somewhere odd").key == "elmo"
        assert sc.her_day_place("The firing range").key == "firing_range"

    def test_infer_place(self):
        assert sc.infer_place(None) is None
        assert sc.infer_place("") is None
        assert sc.infer_place("a quiet lake at dusk").key == "lakeside"
        assert sc.infer_place("standing, looking at viewer") is None


# ── Explainability and identity ─────────────────────────────────────────────


class TestBriefOutputs:
    def test_log_fields_are_place_words_only(self):
        brief = _brief("take a picture of us", ("user", "we are in my room"), hour=23)
        assert brief.log_fields() == (
            "place=his_bedroom (his bedroom) source=his-history setting=indoor "
            "time=night(clock) weather=clear"
        )

    def test_sentence(self):
        inside = _brief("draw us in the kitchen")
        assert inside.sentence().startswith("Setting: Indoors - a home kitchen")
        assert "stays indoors" in inside.sentence()
        assert sc.INDOOR_AVOID in inside.avoid()
        out = _brief("draw us at the park in the rain")
        assert out.sentence().startswith("Setting: Outdoors - a green park")
        assert out.sentence().endswith("overcast.")
        assert out.avoid() == ""

    def test_request_scope(self):
        assert _brief("draw us in the kitchen").request_scope() is None
        assert (
            _brief("draw us", ("user", "we're in my room")).request_scope()
            == "his_bedroom"
        )
        assert (
            _brief("draw yourself", her_place=("Hangar", "")).request_scope()
            == "hangar"
        )
        assert _brief("draw us").request_scope() is None

    def test_request_id_keeps_old_ids_and_scopes_new_ones(self):
        from app.account_images import explicit_request_id

        old = explicit_request_id("u1", "take a picture of us", 832, 1216, True)
        assert old == explicit_request_id(
            "u1", "take a picture of us", 832, 1216, True, None
        )
        room = explicit_request_id(
            "u1", "take a picture of us", 832, 1216, True, "his_bedroom"
        )
        beach = explicit_request_id(
            "u1", "take a picture of us", 832, 1216, True, "beach"
        )
        assert len({old, room, beach}) == 3


# ── Her Day lookup ──────────────────────────────────────────────────────────


class TestRenderPlace:
    async def test_where_the_pwa_says_she_is(self):
        from app import her_day as hd

        private = hd.Block(
            "afternoon", 14, 18, "toy", "Lounge", "toy bike", private=True
        )
        now = hd.HerNow(
            day=date(2026, 10, 5),
            hour=15,
            outfit=w.FALLBACK,
            outfit_source="auto",
            outfit_reason="",
            location="Lounge",
            activity="toy bike",
            block=private,
            blocks=[private],
        )
        with (
            patch("app.her_day.her_now", new=AsyncMock(return_value=now)),
            patch("app.her_day.active_mission", return_value=None),
        ):
            assert await hd.render_place("claude", 5) == ("Lounge", "toy bike")
            # Her private life stays "Off duty" below the bond that shows it.
            assert await hd.render_place("claude", 1) == ("Off duty", "")

    async def test_failure_is_none(self):
        from app import her_day as hd

        with patch("app.her_day.her_now", new=AsyncMock(side_effect=RuntimeError("x"))):
            assert await hd.render_place("claude", 5) is None


# ── Chat hands over who said what ───────────────────────────────────────────


async def test_chat_followups_pass_the_recent_turns_with_roles():
    import asyncio
    from unittest.mock import MagicMock

    from app.chat_followups import SCENE_TURN_CHARS, SCENE_TURNS, ChatFollowups
    from app.models import SessionState

    image = AsyncMock()
    ws_manager = MagicMock()
    followups = ChatFollowups(
        ws_manager,
        lambda _user: None,
        lambda _text: False,
        lambda _text: True,
        AsyncMock(),
        AsyncMock(),
        image,
        AsyncMock(),
        AsyncMock(),
        (),
        (),
        (),
        10_000,
    )
    turns = [{"role": "user", "content": "filler"}] * 20 + [
        {"role": "user", "content": "we are in my room " + "x" * 2000},
        {"role": "assistant", "content": "I'm in the rec room."},
        {"role": "user", "content": "take a picture of us"},
        {"role": "assistant", "content": "Hold still."},
    ]
    session = SessionState(conversation_id="c", turns=turns)
    await followups.admit("take a picture of us", "Hold still.", session, "u1")
    await asyncio.sleep(0)
    kwargs = image.call_args.kwargs
    sent = kwargs["recent_turns"]
    assert len(sent) == SCENE_TURNS
    assert sent[-1] == {"role": "assistant", "content": "Hold still."}
    assert sent[-4]["content"].startswith("we are in my room")
    assert len(sent[-4]["content"]) == SCENE_TURN_CHARS
    assert kwargs["user_id"] == "u1" and "chat_context" in kwargs
