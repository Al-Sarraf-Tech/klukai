"""Clean Goodbyes — she lets him go.

Covers the exit-cue detector (helpers.detect_goodbye), the one-shot
DEPARTURE block (goodbyes.build_departure_block), the absolute rule, and the
proactive hold that keeps her quiet after he signs off.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.goodbyes import DEPARTURE_FALLBACK, build_departure_block
from app.helpers import detect_goodbye
from app.personality import load_personality
from app.proactive.engine import ProactiveEngine


@pytest.fixture(scope="module")
def p() -> dict:
    return load_personality()


# ═══════════════════════════════════════════════════════════════════════════
# detect_goodbye
# ═══════════════════════════════════════════════════════════════════════════


class TestDetectGoodbyeNight:
    @pytest.mark.parametrize("msg", [
        "goodnight",
        "Good night, Klukai",
        "good-night love",
        "gn",
        "gn ❤️",
        "g'night",
        "gnite!",
        "night!",
        "Night, love.",
        "ok night then",
        "nighty night",
        "night night",
        "going to bed",
        "I'm heading to bed now",
        "headed to bed",
        "off to bed",
        "time for bed",
        "it's time to sleep",
        "I'm going to sleep",
        "gonna go sleep",
        "about to go to bed",
        "getting ready for bed",
        "ok I should get some sleep",
        "gotta sleep",
        "I'll go to bed",
        "sweet dreams",
        "おやすみ",
        "おやすみなさい、クルカイ",
        "oyasumi",
        "calling it a night",
        "I'm done for the night",
        "logging off for the night",
        "I'm turning in.",
        "it's past my bedtime",
        "bye, goodnight",
        "Gotta go, it's past my bedtime",
    ])
    def test_positive(self, msg):
        assert detect_goodbye(msg) == "night"

    @pytest.mark.parametrize("msg", [
        "I couldn't sleep last night",
        "did you sleep?",
        "did you sleep well",
        "Are you going to bed?",
        "are you going to sleep",
        "I'm not going to bed yet",
        "I'm not gonna sleep tonight",
        "I'll sleep on it",
        "I'm going to sleep in tomorrow",
        "going to bed with you sounds nice",
        "tell me a bedtime story",
        "last night was rough",
        "tonight we ride",
        "what a night.",
        "night shift again",
        "you should go to sleep",
        "Belka is going to sleep in the hangar",
        "it's past your bedtime",
        "gnocchi for dinner",
        "I went to bed late yesterday",
        "I'll turn in the report",
        "I hate saying goodnight",
        "",
        "   ",
    ])
    def test_negative(self, msg):
        assert detect_goodbye(msg) is None


class TestDetectGoodbyeLeave:
    @pytest.mark.parametrize("msg", [
        "gotta go",
        "Gotta go, sorry!",
        "got to go now",
        "I have to run",
        "I should go.",
        "I'm gonna head out",
        "gtg",
        "g2g",
        "heading out",
        "I'm heading out now",
        "logging off",
        "signing off.",
        "brb",
        "brb later",
        "talk tomorrow",
        "talk to you later",
        "bye",
        "Bye Klukai!",
        "byeee",
        "bye-bye",
        "goodbye",
        "see you!",
        "ok see ya",
        "see ya later",
        "I'll see you tomorrow",
        "catch you later",
        "ttyl",
        "cya",
        "I have to go to work",
        "gotta go to class now",
        "off to work",
        "heading to the gym",
        "later!",
        "ok later then",
        "peace out",
        "I'm off.",
        "I'm leaving",
        "until tomorrow, Klukai",
        "またね",
        "じゃあね",
    ])
    def test_positive(self, msg):
        assert detect_goodbye(msg) == "leave"

    @pytest.mark.parametrize("msg", [
        "standby",
        "maybe later we can ride",
        "I hate goodbyes",
        "I hate saying goodbye",
        "I don't want to say goodbye",
        "I want to see you",
        "I want to see you later tonight",
        "can't wait to see you tomorrow",
        "I have to go check the bike",
        "we should go on a date",
        "I don't have to go yet",
        "should I go?",
        "did you say bye?",
        "I'll sign off on the requisition",
        "Let's head out",
        "we can talk later about it",
        "can we talk later?",
        "Belka said bye",
        "I have to go to work tomorrow",
        "you have to go to bed",
        "I'm off today",
        "I'm out of coffee",
        "abye",
    ])
    def test_negative(self, msg):
        assert detect_goodbye(msg) is None


class TestDetectGoodbyeLongMessages:
    _PADDING = (
        "Today was a lot. The meeting ran long, my boss kept changing the plan, "
        "and I spent the whole drive home thinking about the ride we took last "
        "week and how quiet the road was. "
    )

    def test_long_message_with_goodbye_buried_mid_text_is_ignored(self):
        msg = "bye to that old plan. " + self._PADDING * 2
        assert len(msg) > 160
        assert detect_goodbye(msg) is None

    def test_long_message_ending_in_goodbye_fires(self):
        msg = self._PADDING * 2 + "Anyway, gotta go."
        assert len(msg) > 160
        assert detect_goodbye(msg) == "leave"

    def test_long_message_ending_in_goodnight_fires(self):
        msg = self._PADDING * 2 + "Goodnight, Klukai ❤️"
        assert detect_goodbye(msg) == "night"

    def test_short_message_matches_anywhere(self):
        assert detect_goodbye("gotta go, the oven timer just went off") == "leave"

    def test_curly_apostrophes_are_normalised(self):
        assert detect_goodbye("I’m heading to bed") == "night"
        assert detect_goodbye("I’m not going to bed") is None

    def test_non_string_is_none(self):
        assert detect_goodbye(None) is None  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
# DEPARTURE block
# ═══════════════════════════════════════════════════════════════════════════


class TestDepartureBlock:
    def test_low_band_is_a_curt_dismissal(self, p):
        block = build_departure_block(p, "leave", 1)
        assert block.startswith("DEPARTURE")
        assert "Dismissed." in block
        assert "I'll be here" not in block

    def test_mid_band_is_dry_care(self, p):
        block = build_departure_block(p, "night", 4)
        assert "Don't make me file a report on your sleep." in block

    def test_high_band_may_end_with_ill_be_here(self, p):
        block = build_departure_block(p, "leave", 7)
        assert "I'll be here." in block

    def test_band_boundaries(self, p):
        assert "Dismissed." in build_departure_block(p, "leave", 2)
        assert "Dry care" in build_departure_block(p, "leave", 3)
        assert "Dry care" in build_departure_block(p, "leave", 5)
        assert "unclinging" in build_departure_block(p, "leave", 6)

    def test_forbidden_patterns_present_at_every_level(self, p):
        for level in range(10):
            for kind in ("night", "leave"):
                block = build_departure_block(p, kind, level).lower()
                assert "two sentences" in block
                assert "guilt" in block
                assert "already?" in block
                assert "bargain" in block
                assert "teaser" in block
                assert "question" in block

    def test_night_may_order_rest_leave_does_not(self, p):
        night = build_departure_block(p, "night", 3)
        leave = build_departure_block(p, "leave", 3)
        assert "order him to rest" in night
        assert "order him to rest" not in leave
        assert "for the night" in night
        assert "for the night" not in leave

    def test_examples_are_marked_not_verbatim(self, p):
        assert "never verbatim" in build_departure_block(p, "leave", 0)

    def test_python_fallback_when_yaml_section_missing(self):
        block = build_departure_block({}, "leave", 0)
        assert DEPARTURE_FALLBACK["tiers"][0]["leave"][0] in block
        assert "guilt" in block.lower()

    def test_malformed_yaml_falls_back_field_by_field(self):
        cfg = {"departures": {"guidance": "", "forbidden": "not-a-list",
                              "night": None, "tiers": {"x": {}}}}
        block = build_departure_block(cfg, "night", 6)
        assert DEPARTURE_FALLBACK["tiers"][6]["night"][0] in block
        assert DEPARTURE_FALLBACK["night"] in block

    def test_tier_without_lines_for_kind_omits_examples(self):
        cfg = {"departures": {"tiers": {0: {"register": "Flat."}}}}
        block = build_departure_block(cfg, "leave", 9)
        assert "Register: Flat." in block
        assert "In this key" not in block

    def test_tier_without_register_omits_register_line(self):
        cfg = {"departures": {"tiers": {0: {"leave": ["Out."]}}}}
        block = build_departure_block(cfg, "leave", 2)
        assert "Register:" not in block
        assert '"Out."' in block

    def test_non_mapping_tier_is_ignored(self):
        cfg = {"departures": {"tiers": {0: "oops", 3: {"register": "Mid.", "leave": ["Go."]}}}}
        block = build_departure_block(cfg, "leave", 1)
        # Level 1 has no usable tier at or below it -> fallback tiers.
        assert DEPARTURE_FALLBACK["tiers"][0]["leave"][0] in block
        assert "Register: Mid." in build_departure_block(cfg, "leave", 4)

    def test_unknown_kind_reads_as_leave(self, p):
        assert build_departure_block(p, "wander", 0) == build_departure_block(p, "leave", 0)


class TestAbsoluteRule:
    def test_clean_goodbye_rule_is_absolute(self, p):
        rules = " ".join(p["absolute_rules"]).lower()
        assert "guilt" in rules and "bargain" in rules and "leaves" in rules

    def test_rule_reaches_assembled_prompt(self):
        from app.personality.system_prompt import assemble_system_prompt
        prompt = assemble_system_prompt(affection_level=0)
        assert "let him go cleanly" in prompt


# ═══════════════════════════════════════════════════════════════════════════
# Proactive hold after a goodbye
# ═══════════════════════════════════════════════════════════════════════════


def _clear_engine() -> ProactiveEngine:
    e = ProactiveEngine()
    e._muted_until = None
    e._proactive_count_today = 0
    e._last_proactive_answered = True
    return e


def _at(engine_now: datetime):
    return patch("app.proactive.engine.now_local", return_value=engine_now)


class TestGoodbyeHold:
    def test_no_hold_by_default(self):
        e = _clear_engine()
        assert e._goodbye_hold_until is None
        with _at(datetime(2026, 10, 4, 14, 0)):
            assert e._goodbye_hold_active() is False
            assert e._can_send() is True

    def test_goodnight_in_the_evening_holds_until_0800_next_day(self):
        e = _clear_engine()
        with _at(datetime(2026, 10, 4, 21, 30)):
            e.mark_goodnight()
            assert e._goodbye_hold_until == datetime(2026, 10, 5, 8, 0)
            assert e._can_send() is False

    def test_goodnight_after_midnight_holds_until_0800_same_morning(self):
        e = _clear_engine()
        with _at(datetime(2026, 10, 5, 1, 15)):
            e.mark_goodnight()
        assert e._goodbye_hold_until == datetime(2026, 10, 5, 8, 0)

    def test_goodnight_exactly_at_0800_rolls_to_tomorrow(self):
        e = _clear_engine()
        with _at(datetime(2026, 10, 5, 8, 0)):
            e.mark_goodnight()
        assert e._goodbye_hold_until == datetime(2026, 10, 6, 8, 0)

    def test_hold_expires_at_0800_and_clears_itself(self):
        e = _clear_engine()
        with _at(datetime(2026, 10, 4, 21, 30)):
            e.mark_goodnight()
        with _at(datetime(2026, 10, 5, 7, 59)):
            assert e._goodbye_hold_active() is True
        with _at(datetime(2026, 10, 5, 8, 0)):
            assert e._goodbye_hold_active() is False
            assert e._goodbye_hold_until is None
            assert e._can_send() is True

    def test_leaving_holds_for_90_minutes(self):
        e = _clear_engine()
        with _at(datetime(2026, 10, 4, 14, 0)):
            e.mark_leaving()
        assert e._goodbye_hold_until == datetime(2026, 10, 4, 15, 30)
        with _at(datetime(2026, 10, 4, 15, 29)):
            assert e._can_send() is False
        with _at(datetime(2026, 10, 4, 15, 30)):
            assert e._can_send() is True

    def test_explicit_now_is_honoured(self):
        e = _clear_engine()
        e._goodbye_hold_until = datetime(2026, 10, 4, 15, 30)
        assert e._goodbye_hold_active(datetime(2026, 10, 4, 15, 0)) is True
        assert e._goodbye_hold_active(datetime(2026, 10, 4, 16, 0)) is False

    def test_any_new_message_clears_the_hold(self):
        e = _clear_engine()
        with _at(datetime(2026, 10, 4, 21, 30)):
            e.mark_goodnight()
            e.mark_responded()
            assert e._goodbye_hold_until is None
            # Quiet hours still apply on their own; the hold is just gone.
        with _at(datetime(2026, 10, 4, 22, 0)):
            assert e._can_send() is True

    @pytest.mark.asyncio
    async def test_midnight_reset_keeps_the_hold(self):
        """A 2200 goodnight must survive the 0000 counter reset."""
        e = _clear_engine()
        with _at(datetime(2026, 10, 4, 22, 0)):
            e.mark_goodnight()
        await e._reset_daily()
        assert e._goodbye_hold_until == datetime(2026, 10, 5, 8, 0)

    @pytest.mark.asyncio
    async def test_deliver_is_suppressed_while_held(self):
        e = _clear_engine()
        e._on_message_callback = AsyncMock()
        with _at(datetime(2026, 10, 4, 14, 0)):
            e.mark_leaving()
            assert await e._deliver("Hey.") is False
        e._on_message_callback.assert_not_awaited()


_HELD = datetime(2026, 10, 4, 14, 0)


def _held_engine(aff: int = 9) -> ProactiveEngine:
    e = _clear_engine()
    e._affection_level = aff
    e._on_message_callback = AsyncMock()
    e._goodbye_hold_until = _HELD + timedelta(minutes=30)
    return e


class TestAmbientEventsRespectTheHold:
    """Events that bypass _can_send (they only honour the manual mute) must
    also stay quiet after he signs off."""

    @pytest.mark.asyncio
    async def test_random_event_held(self):
        e = _held_engine()
        with patch("app.proactive.events.now_local", return_value=_HELD), \
             patch("app.proactive.events.random.random", return_value=0.0):
            await e._random_event()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_romance_window_held_after_delay(self):
        e = _held_engine()
        e._user_messaged_today = True
        with patch("app.proactive.events.now_local", return_value=_HELD), \
             patch("app.proactive.events.random.uniform", return_value=0), \
             patch("app.proactive.events.asyncio.sleep", new=AsyncMock()):
            await e._romance_window()
        e._on_message_callback.assert_not_awaited()
        assert e._romance_delivered_today is False

    @pytest.mark.asyncio
    async def test_memory_recall_held(self):
        e = _held_engine()
        with patch("app.proactive.events.now_local", return_value=_HELD), \
             patch("app.memory_archive.recall_memory", new=AsyncMock()) as recall:
            await e._memory_recall_event()
        recall.assert_not_awaited()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_spontaneous_art_held(self):
        e = _held_engine()
        with patch("app.proactive.events.now_local", return_value=_HELD), \
             patch("app.image_gen.generate_image", new=AsyncMock()) as gen:
            await e._spontaneous_art_event()
        gen.assert_not_awaited()
        e._on_message_callback.assert_not_awaited()
