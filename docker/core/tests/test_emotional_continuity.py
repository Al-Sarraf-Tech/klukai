"""Tests for emotional continuity on return.

Covers two layers:

1. The pure ``compose_return_emotion`` / ``describe_gap`` helpers in
   ``character_behaviors`` — that the "missed you" beat scales with gap length
   AND closeness, and that prior mood is carried over.
2. The wired ``_maybe_reflect_on_return`` flow — that the prompt handed to the
   LLM actually contains the missed-you beat, the carried-over mood, and a
   last-thread reference pulled from recent messages.

All DB/LLM access is mocked; no live services. Mocking style mirrors
``tests/test_reflection_on_return.py``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─────────────────────────────────────────────────────────────────────────
# Layer 1 — pure helpers
# ─────────────────────────────────────────────────────────────────────────

class TestDescribeGap:
    def test_hours_under_a_day(self):
        from app.character_behaviors import describe_gap
        assert describe_gap(3) == "several hours"
        assert describe_gap(23.9) == "several hours"

    def test_about_a_day(self):
        from app.character_behaviors import describe_gap
        assert describe_gap(26) == "about a day"

    def test_multi_day(self):
        from app.character_behaviors import describe_gap
        assert "days" in describe_gap(50)   # ~2 days
        assert "days" in describe_gap(96)   # ~4 days

    def test_about_a_week(self):
        from app.character_behaviors import describe_gap
        assert describe_gap(24 * 8) == "about a week"

    def test_multi_week(self):
        from app.character_behaviors import describe_gap
        assert "weeks" in describe_gap(24 * 20)


class TestComposeReturnEmotionClosenessGate:
    """Low closeness must NOT claim to miss him; high closeness must."""

    def test_cold_levels_withhold_longing(self):
        from app.character_behaviors import compose_return_emotion
        for level in (0, 1, 2):
            beat = compose_return_emotion(hours_away=48, affection_level=level).lower()
            assert "miss" not in beat
            assert "worr" not in beat
            # Stays reserved / professional
            assert "professional" in beat or "reserved" in beat or "measured" in beat

    def test_high_levels_openly_miss(self):
        from app.character_behaviors import compose_return_emotion
        beat = compose_return_emotion(hours_away=48, affection_level=8).lower()
        assert "miss" in beat

    def test_mid_levels_warm_but_restrained(self):
        from app.character_behaviors import compose_return_emotion
        beat = compose_return_emotion(hours_away=48, affection_level=4).lower()
        # Genuine warmth but does not claim to have "missed" him
        assert "glad" in beat or "warm" in beat
        assert "miss" not in beat


class TestComposeReturnEmotionGapScaling:
    """Within a closeness band, longer gaps intensify the beat."""

    def test_short_gap_barely_noted_at_high_closeness(self):
        from app.character_behaviors import compose_return_emotion
        beat = compose_return_emotion(hours_away=10, affection_level=8).lower()
        assert "only been" in beat  # acknowledges it was brief

    def test_long_gap_tips_into_worry_at_high_closeness(self):
        from app.character_behaviors import compose_return_emotion
        short = compose_return_emotion(hours_away=10, affection_level=8).lower()
        long = compose_return_emotion(hours_away=24 * 6, affection_level=8).lower()
        assert "worr" not in short
        assert "worr" in long  # many days -> worry surfaces

    def test_gap_phrase_reflects_duration(self):
        from app.character_behaviors import compose_return_emotion
        day_beat = compose_return_emotion(hours_away=30, affection_level=8)
        assert "day" in day_beat


class TestComposeReturnEmotionMoodCarryover:
    def test_nonneutral_mood_is_carried(self):
        from app.character_behaviors import compose_return_emotion
        beat = compose_return_emotion(hours_away=48, affection_level=8, prior_mood="tender")
        assert "tender" in beat
        assert "continuation" in beat.lower()

    def test_neutral_moods_not_injected(self):
        from app.character_behaviors import compose_return_emotion
        for mood in ("composed", "neutral", "", None):
            beat = compose_return_emotion(hours_away=48, affection_level=8, prior_mood=mood)
            assert "continuation" not in beat.lower()

    def test_mood_carryover_even_at_low_closeness(self):
        """Mood carries over regardless of band — continuity isn't gated by warmth."""
        from app.character_behaviors import compose_return_emotion
        beat = compose_return_emotion(hours_away=48, affection_level=1, prior_mood="cold")
        assert "cold" in beat


# ─────────────────────────────────────────────────────────────────────────
# Layer 2 — wired flow: prompt contains beat + mood + last thread
# ─────────────────────────────────────────────────────────────────────────

class _FakeConn:
    """Connection that returns queued batches in query order.

    Tuples -> fetchone (single row); lists -> fetchall (multiple rows).
    """

    def __init__(self, *batches):
        self._batches = list(batches)

    async def execute(self, sql, params=None):
        result = AsyncMock()
        if self._batches:
            batch = self._batches.pop(0)
            if isinstance(batch, tuple):
                result.fetchone = AsyncMock(return_value=batch)
            else:
                result.fetchall = AsyncMock(return_value=batch)
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _FakePool:
    def __init__(self, *batch_sequences):
        self._sequences = list(batch_sequences)

    def connection(self):
        seq = self._sequences.pop(0) if self._sequences else []
        return _FakeConn(*seq)


def _mk_affection(level: int = 5) -> SimpleNamespace:
    return SimpleNamespace(level=level, level_name="Trust", score=500,
                           consecutive_days=1, total_interactions=10,
                           first_interaction=datetime(2026, 1, 1, tzinfo=timezone.utc))


def _capture_router():
    """Router whose complete_local records the user prompt it received."""
    captured: dict[str, str] = {}

    async def _complete(system_prompt, messages, config):
        captured["system"] = system_prompt
        captured["user"] = messages[0]["content"]
        return {"choices": [{"message": {"content":
                "Welcome back, Commander. You were telling me about the recon route."}}]}

    router = MagicMock()
    router.complete_local = AsyncMock(side_effect=_complete)
    return router, captured


def _run_return(level: int, hours: float, mood: str, excerpts: list[tuple[str, str]]):
    """Drive _maybe_reflect_on_return with mocked DB/LLM and return (captured, ws)."""
    from app.chat import _maybe_reflect_on_return

    away = datetime.now(timezone.utc) - timedelta(hours=hours)
    # Three queries in one connection: MAX(created_at), recent rows, mood.
    pool = _FakePool([
        (away,),                 # MAX(created_at) -> fetchone
        excerpts,                # recent messages -> fetchall
        (mood,),                 # persistent mood -> fetchone
    ])

    router, captured = _capture_router()

    fake_ws = MagicMock()
    fake_ws.is_connected = MagicMock(return_value=True)
    fake_ws.send_proactive = AsyncMock()

    fake_aff = MagicMock()
    fake_aff.get_state = AsyncMock(return_value=_mk_affection(level))

    async def _go():
        with patch("app.db.get_pool", return_value=pool), \
             patch("app.reflect_helpers.router", router), \
             patch("app.reflect_helpers.ws", fake_ws), \
             patch("app.reflect_helpers.affection", fake_aff), \
             patch("app.personality.load_personality",
                   return_value={"user_title": "Commander"}), \
             patch("app.personality.build_character_preamble",
                   return_value="You are Klukai."), \
             patch("asyncio.sleep", new=AsyncMock(return_value=None)):
            await _maybe_reflect_on_return("alice")

    import asyncio
    asyncio.run(_go())
    return captured, fake_ws


class TestReturnFlowEmotionalContinuity:
    def test_prompt_includes_last_thread_reference(self):
        captured, ws = _run_return(
            level=8, hours=30, mood="tender",
            excerpts=[("user", "I was telling you about the recon route through Sector 7"),
                      ("assistant", "go on, I'm listening")],
        )
        ws.send_proactive.assert_awaited_once()
        # The recent exchange content must be embedded so she can resume it.
        assert "recon route" in captured["user"]
        assert "resume from here" in captured["user"].lower()

    def test_prompt_includes_missed_you_beat_at_high_closeness(self):
        captured, _ = _run_return(
            level=8, hours=48, mood="tender",
            excerpts=[("user", "talk later"), ("assistant", "okay")],
        )
        assert "How the time apart felt to you" in captured["user"]
        assert "miss" in captured["user"].lower()

    def test_prompt_stays_reserved_at_low_closeness(self):
        captured, _ = _run_return(
            level=1, hours=48, mood="composed",
            excerpts=[("user", "status report"), ("assistant", "all nominal")],
        )
        beat_region = captured["user"].lower()
        assert "miss" not in beat_region
        assert "professional" in beat_region or "reserved" in beat_region or "measured" in beat_region

    def test_prompt_carries_prior_mood(self):
        captured, _ = _run_return(
            level=6, hours=30, mood="yearning",
            excerpts=[("user", "I have to go"), ("assistant", "stay safe")],
        )
        assert "yearning" in captured["user"]

    def test_beat_scales_with_gap_in_prompt(self):
        # Both gaps stay inside the live 8-72h return window. The short gap
        # reads as barely-noted; the multi-day gap has her openly missing him.
        short, _ = _run_return(
            level=8, hours=10, mood="tender",
            excerpts=[("user", "brb"), ("assistant", "ok")],
        )
        long, _ = _run_return(
            level=8, hours=60, mood="tender",  # ~2.5 days, still < 72h ceiling
            excerpts=[("user", "gone a while"), ("assistant", "ok")],
        )
        assert "only been" in short["user"].lower()
        assert "only been" not in long["user"].lower()
        assert "miss" in long["user"].lower()
