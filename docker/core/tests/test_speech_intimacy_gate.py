"""Intimacy gating and behavioral grammar — princess-upgrade."""
from __future__ import annotations

from app.personality.speech import (
    _strip_intimacy_addendum,
    build_behavioral_grammar_block,
    build_speech_guidelines,
)


def test_strip_intimacy_removes_section():
    tone = "Soft bond.\n\nINTIMACY: Talk dirty. Be filthy."
    assert "INTIMACY" not in _strip_intimacy_addendum(tone)
    assert "Soft bond" in _strip_intimacy_addendum(tone)


def test_speech_guidelines_hides_intimacy_below_8():
    p = {
        "speech_patterns": {
            "level_4_bonded": {
                "name": "Bonded",
                "tone": "Warm bond.\n\nINTIMACY: Talk dirty. Be filthy. Use explicit language.",
                "examples": ["Stay."],
            }
        }
    }
    out7 = build_speech_guidelines(p, 7)
    out8 = build_speech_guidelines(p, 8)
    assert "Talk dirty" not in out7
    assert "Talk dirty" in out8


def test_behavioral_grammar_always_present():
    out = build_behavioral_grammar_block(0)
    assert "Earned confidence" in out
    assert "Guarded warmth" in out
    assert "denial IS the character" not in out


def test_behavioral_grammar_direct_warmth_at_trusted_boundary():
    guarded = build_behavioral_grammar_block(6)
    trusted = build_behavioral_grammar_block(7)
    assert "Guarded warmth" in guarded
    assert "Trusted warmth is direct" not in guarded
    assert "Trusted warmth is direct" in trusted
    assert "Guarded warmth" not in trusted
    assert "Escalation ladder" not in trusted
