"""Coverage for helpers.py detectors — squad/jealousy/commander/gift/mission intent."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestDetectSquadAddress:
    def test_no_member_returns_none(self):
        from app.helpers import detect_squad_address
        assert detect_squad_address("morning commander") is None

    def test_mechty_alias(self):
        from app.helpers import detect_squad_address
        result = detect_squad_address("mechty, report status")
        assert result is not None
        assert result.lower().startswith("m")


class TestDetectJealousyTrigger:
    def test_generic_praise_no_squad_name_none(self):
        from app.helpers import detect_jealousy_trigger
        # No squad name mentioned — must not trigger
        assert detect_jealousy_trigger("she's so amazing") is None

    def test_compliment_to_squad_member_triggers(self):
        from app.helpers import detect_jealousy_trigger
        result = detect_jealousy_trigger("mechty is amazing")
        assert result is not None

    def test_affection_for_squad_member(self):
        from app.helpers import detect_jealousy_trigger
        result = detect_jealousy_trigger("I love mechty's work")
        assert result is not None

    def test_simple_name_mention_no_trigger(self):
        from app.helpers import detect_jealousy_trigger
        # Just a neutral "where is X" question — no affection keyword
        assert detect_jealousy_trigger("where is mechty deployed") is None


class TestDetectCommanderDetails:
    def test_no_detail_returns_empty_flags(self):
        from app.helpers import detect_commander_details
        result = detect_commander_details("just a random message")
        assert isinstance(result, dict)
        # All flags should be False
        assert all(v is False for v in result.values())

    def test_wearing_phrase_sets_flag(self):
        from app.helpers import detect_commander_details
        result = detect_commander_details("I'm wearing my uniform today")
        # At least some detail flag should be True
        assert any(result.values())


class TestDetectGiftGiving:
    def test_plain_message_no_gift(self):
        from app.helpers import detect_gift_giving
        assert detect_gift_giving("how was your day") is False

    def test_explicit_gift_phrasing_detected(self):
        from app.helpers import detect_gift_giving
        # Try common gift-giving phrases
        hits = any(detect_gift_giving(t) for t in [
            "I brought you a small gift",
            "here's something for you",
            "I got you this",
        ])
        assert hits is True


class TestWantsDreamInquiry:
    def test_plain_hi_no(self):
        from app.helpers import wants_dream_inquiry
        assert wants_dream_inquiry("hello") is False

    def test_dream_phrase_detected(self):
        from app.helpers import wants_dream_inquiry
        assert wants_dream_inquiry("did you dream") is True or \
               wants_dream_inquiry("tell me your dream") is True


class TestWantsRecall:
    def test_plain_no(self):
        from app.helpers import wants_recall
        assert wants_recall("let's talk about tomorrow") is False

    def test_remember_phrase(self):
        from app.helpers import wants_recall
        # At least one canonical phrasing should trigger
        hits = any(wants_recall(t) for t in [
            "do you remember what we discussed",
            "do you recall when we",
        ])
        assert hits is True


class TestMissionIntent:
    def test_wants_mission_start(self):
        from app.helpers import wants_mission_start
        # Canonical MISSION_START_KEYWORDS include interval phrasings
        assert any(wants_mission_start(t) for t in [
            "updates every 30 minutes",
            "report every hour",
            "keep me posted",
            "check in every 20 minutes",
        ])

    def test_wants_mission_cancel(self):
        from app.helpers import wants_mission_cancel
        assert any(wants_mission_cancel(t) for t in [
            "cancel the mission",
            "abort",
            "stand down",
        ])


class TestParseIntervalMinutes:
    def test_numeric_minutes(self):
        from app.helpers import parse_interval_minutes
        assert parse_interval_minutes("every 45 minutes") == 45

    def test_hourly(self):
        from app.helpers import parse_interval_minutes
        # Should produce a reasonable minute count for an hour
        result = parse_interval_minutes("every hour")
        assert isinstance(result, int)
        assert result > 0

    def test_half_hour(self):
        from app.helpers import parse_interval_minutes
        result = parse_interval_minutes("every half hour")
        assert isinstance(result, int)
        assert result > 0

    def test_default_when_no_match(self):
        from app.helpers import parse_interval_minutes
        # No interval specified — should produce a sane default > 0
        result = parse_interval_minutes("start a mission")
        assert isinstance(result, int)
        assert result > 0


class TestChunkText:
    def test_empty_returns_empty_list(self):
        from app.helpers import chunk_text
        assert chunk_text("") == [""] or chunk_text("") == []

    def test_single_word(self):
        from app.helpers import chunk_text
        result = chunk_text("hello", chunk_size=8)
        assert len(result) == 1
        assert "hello" in " ".join(result)

    def test_respects_chunk_size(self):
        from app.helpers import chunk_text
        text = " ".join(f"word{i}" for i in range(20))
        result = chunk_text(text, chunk_size=5)
        # Every chunk has at most 5 whitespace-separated pieces
        for chunk in result:
            assert len(chunk.split()) <= 5


class TestFixNarration:
    def test_passthrough_normal_text(self):
        from app.helpers import fix_narration
        assert fix_narration("Hello, Commander.") == "Hello, Commander."

    def test_empty_safe(self):
        from app.helpers import fix_narration
        result = fix_narration("")
        assert isinstance(result, str)


class TestStripActionsForTts:
    def test_removes_parenthetical_actions(self):
        """strip_actions_for_tts removes (parenthesized) narration, not *asterisks*."""
        from app.helpers import strip_actions_for_tts
        result = strip_actions_for_tts("(she smiles) Hello, Commander.")
        assert "(" not in result
        assert "she smiles" not in result
        assert "Commander" in result

    def test_passthrough_plain_text(self):
        from app.helpers import strip_actions_for_tts
        result = strip_actions_for_tts("Just a plain sentence.")
        assert "Just a plain sentence." in result


class TestSquadAddressNoFalsePositives:
    def test_relevant_does_not_match_leva(self):
        from app.helpers import detect_squad_address
        assert detect_squad_address("is that relevant to the mission?") is None

    def test_elevator_does_not_match_leva(self):
        from app.helpers import detect_squad_address
        assert detect_squad_address("I took the elevator") is None

    def test_vector_math_does_not_match_vector_alone_when_embedded(self):
        from app.helpers import detect_squad_address
        # "vector" as whole word still matches Vector member — that's intentional.
        # Ensure substring-in-word is fixed for leva only primarily.
        assert detect_squad_address("the elevators are down") is None

    def test_real_leva_still_matches(self):
        from app.helpers import detect_squad_address
        assert detect_squad_address("Talk to Leva about intel") == "Leva"
