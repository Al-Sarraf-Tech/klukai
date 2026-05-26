"""Behavioral coverage top-up for app.fact_extractor.

Existing tests cover extract_facts, create_episode_summary, compact_turns.
This file targets the two untested generators:

  * generate_mission_update (lines 208-226): prompt assembly with the
    major-event / nominal and active-events / blank conditional lines, plus
    the fallback string when the LLM returns empty.
  * generate_romance_message (lines 233-249): prompt assembly with the
    context-summary default, plus the empty-LLM fallback.

The LM gate is a no-op async context manager and call_llm_text is mocked, so
we assert (a) the exact prompt the model receives and (b) the returned text /
fallback. No network, no real model.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app import fact_extractor


class _NoopGate:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return None


# ═══════════════════════════════════════════════════════════════════════════
# generate_mission_update (208-226)
# ═══════════════════════════════════════════════════════════════════════════
class TestGenerateMissionUpdate:
    @pytest.mark.asyncio
    async def test_returns_llm_text_with_major_event_and_active_events(self):
        """Both conditionals take their truthy branch: 'MAJOR EVENT:' line and
        'Active: ...' injury line are present in the prompt."""
        captured = {}

        async def _fake_text(url, model, prompt, **kw):
            captured["prompt"] = prompt
            captured["max_tokens"] = kw.get("max_tokens")
            return "Radio crackles. Belka is on point. Objective in sight."

        with patch("app.llm_router.get_lm_gate", return_value=_NoopGate()), \
             patch("app.fact_extractor.call_llm_text", new=AsyncMock(side_effect=_fake_text)):
            out = await fact_extractor.generate_mission_update(
                mission_desc="Recover the intel cache",
                elapsed_minutes=42,
                update_number=3,
                major_event="ambush at the ridge",
                active_events=["sprained ankle", "low ammo"],
                affection_level=7,
            )

        assert out == "Radio crackles. Belka is on point. Objective in sight."
        p = captured["prompt"]
        assert "MAJOR EVENT: ambush at the ridge" in p
        assert "Active: sprained ankle, low ammo" in p
        assert "Recover the intel cache" in p
        assert "42 minutes" in p
        assert "Update #3" in p
        assert "Affection 7/9" in p
        assert captured["max_tokens"] == 150

    @pytest.mark.asyncio
    async def test_nominal_situation_when_no_major_event(self):
        """major_event=None → 'Situation nominal.'; active_events=[] → blank
        injury line (the falsy branch of both conditionals)."""
        captured = {}

        async def _fake_text(url, model, prompt, **kw):
            captured["prompt"] = prompt
            return "All quiet. Mechty is dozing between watches."

        with patch("app.llm_router.get_lm_gate", return_value=_NoopGate()), \
             patch("app.fact_extractor.call_llm_text", new=AsyncMock(side_effect=_fake_text)):
            out = await fact_extractor.generate_mission_update(
                mission_desc="Patrol the perimeter",
                elapsed_minutes=10,
                update_number=1,
                major_event=None,
                active_events=[],
                affection_level=2,
            )

        assert out == "All quiet. Mechty is dozing between watches."
        p = captured["prompt"]
        assert "Situation nominal." in p
        assert "MAJOR EVENT:" not in p
        assert "Active:" not in p

    @pytest.mark.asyncio
    async def test_empty_llm_returns_static_fallback(self):
        """Empty/falsy LLM output → the canned 'Static on the line' fallback."""
        with patch("app.llm_router.get_lm_gate", return_value=_NoopGate()), \
             patch("app.fact_extractor.call_llm_text", new=AsyncMock(return_value="")):
            out = await fact_extractor.generate_mission_update(
                "Hold the line", 5, 1, None, [], 0,
            )
        assert out == "...Static on the line. Update delayed."


# ═══════════════════════════════════════════════════════════════════════════
# generate_romance_message (233-249)
# ═══════════════════════════════════════════════════════════════════════════
class TestGenerateRomanceMessage:
    @pytest.mark.asyncio
    async def test_returns_llm_text_and_passes_context(self):
        """A provided context_summary is injected verbatim and the model text
        is returned."""
        captured = {}

        async def _fake_text(url, model, prompt, **kw):
            captured["prompt"] = prompt
            captured["max_tokens"] = kw.get("max_tokens")
            return "The stars are out, Commander. I kept thinking about earlier."

        with patch("app.llm_router.get_lm_gate", return_value=_NoopGate()), \
             patch("app.fact_extractor.call_llm_text", new=AsyncMock(side_effect=_fake_text)):
            out = await fact_extractor.generate_romance_message(
                affection_level=8,
                mood="tender",
                context_summary="trained together at dawn",
                time_of_day="evening",
            )

        assert out == "The stars are out, Commander. I kept thinking about earlier."
        p = captured["prompt"]
        assert "trained together at dawn" in p
        assert "tender" in p
        assert "evening" in p
        assert "Affection: 8/9" in p
        assert captured["max_tokens"] == 200

    @pytest.mark.asyncio
    async def test_blank_context_uses_routine_day_default(self):
        """Falsy context_summary → 'A routine day at base.' default substitution."""
        captured = {}

        async def _fake_text(url, model, prompt, **kw):
            captured["prompt"] = prompt
            return "Quiet night. Come sit with me."

        with patch("app.llm_router.get_lm_gate", return_value=_NoopGate()), \
             patch("app.fact_extractor.call_llm_text", new=AsyncMock(side_effect=_fake_text)):
            out = await fact_extractor.generate_romance_message(
                affection_level=5, mood="content", context_summary="", time_of_day="night",
            )

        assert out == "Quiet night. Come sit with me."
        assert "A routine day at base." in captured["prompt"]

    @pytest.mark.asyncio
    async def test_empty_llm_returns_quiet_fallback(self):
        """Empty LLM output → the canned 'evening is quiet' fallback."""
        with patch("app.llm_router.get_lm_gate", return_value=_NoopGate()), \
             patch("app.fact_extractor.call_llm_text", new=AsyncMock(return_value=None)):
            out = await fact_extractor.generate_romance_message(
                affection_level=3, mood="composed", context_summary="x", time_of_day="dusk",
            )
        assert out == "...The evening is quiet. I was thinking about today."
