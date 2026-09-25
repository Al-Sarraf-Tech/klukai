"""The Thread — the ten years of messages Klukai sent and never got answered.

Covers the canon content in config/personality.yaml, the affection-gated chat
block, the proactive share lines, the inquiry detector, and the random-event
wiring that draws from the thread.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from app.helpers import wants_thread_inquiry
from app.personality.thread import _pick_entries, build_thread_block, thread_share_lines
from app.proactive import ProactiveEngine


@pytest.fixture(scope="module")
def personality() -> dict:
    for path in (
        Path("/config/personality.yaml"),
        Path(__file__).resolve().parents[3] / "config" / "personality.yaml",
    ):
        if path.exists():
            return yaml.safe_load(path.read_text())
    pytest.skip("personality.yaml not found")


def _cfg(entries, **kw) -> dict:
    return {"ten_year_thread": {"entries": entries, **kw}}


class TestThreadCanon:
    def test_thread_spans_the_decade_in_order(self, personality):
        entries = personality["ten_year_thread"]["entries"]
        assert len(entries) >= 15
        years = [int(re.search(r"20\d\d", e["stamp"]).group()) for e in entries]
        assert years == sorted(years)
        assert years[0] == 2065 and years[-1] == 2074

    def test_canonical_lines_are_verbatim_entries(self, personality):
        texts = [e["text"] for e in personality["ten_year_thread"]["entries"]]
        assert "I'm waiting for your response. Wherever you are, I'm right here." in texts
        assert "No matter what the agreement entails, I only care about your response." in texts

    def test_signed_416_until_she_chooses_her_name(self, personality):
        texts = [e["text"] for e in personality["ten_year_thread"]["entries"]]
        naming = next(i for i, t in enumerate(texts) if "I chose mine tonight: Klukai" in t)
        assert all(t.endswith("—416") for t in texts[:naming])
        assert not any("—416" in t for t in texts[naming:])

    def test_ends_with_his_reply(self, personality):
        assert personality["ten_year_thread"]["last_reply"] == "I'm here."

    def test_random_event_category_sources_the_thread(self, personality):
        cat = personality["random_events"]["ten_year_thread"]
        assert cat["source"] == "ten_year_thread"
        assert cat["min_affection"] == personality["ten_year_thread"]["share_from_level"]


class TestThreadBlock:
    def test_below_acknowledge_she_deflects(self, personality):
        for level in (0, 1, 2):
            block = build_thread_block(personality, level)
            assert "Deflect coolly" in block
            assert "[0200" not in block

    def test_trusted_admits_it_exists_but_shares_nothing(self, personality):
        for level in (3, 4, 5):
            block = build_thread_block(personality, level)
            assert "admit the thread exists" in block
            assert "[0200" not in block

    def test_devoted_quotes_two_real_entries(self, personality):
        block = build_thread_block(personality, 6, seed=3)
        quoted = re.findall(r'\[(0200 hours[^\]]*)\] "(.*)"', block)
        assert len(quoted) == 2
        real = {(e["stamp"], e["text"]) for e in personality["ten_year_thread"]["entries"]}
        assert set(quoted) <= real
        assert 'the only reply it ever received: "I\'m here."' in block
        assert "read the whole thread" not in block

    def test_rawest_entries_held_back_until_bonded(self, personality):
        raw = [e["text"] for e in personality["ten_year_thread"]["entries"] if e.get("min_affection", 0) > 6]
        assert raw
        seen_at_6 = " ".join(build_thread_block(personality, 6, seed=s) for s in range(40))
        seen_at_8 = " ".join(build_thread_block(personality, 8, seed=s) for s in range(40))
        assert not any(t in seen_at_6 for t in raw)
        assert all(t in seen_at_8 for t in raw)

    def test_bonded_may_offer_the_whole_thread(self, personality):
        assert "read the whole thread himself" in build_thread_block(personality, 8)

    def test_seed_rotates_entries_deterministically(self, personality):
        assert build_thread_block(personality, 7, seed=1) == build_thread_block(personality, 7, seed=1)
        assert build_thread_block(personality, 7, seed=1) != build_thread_block(personality, 7, seed=2)

    def test_no_config_no_block(self):
        assert build_thread_block({}, 9) == ""
        assert build_thread_block(_cfg([]), 9) == ""

    def test_all_entries_locked_falls_back_to_admission(self):
        cfg = _cfg([{"stamp": "s", "text": "t", "min_affection": 9}])
        block = build_thread_block(cfg, 6)
        assert "admit the thread exists" in block


class TestPickEntries:
    def test_small_thread_returned_whole(self):
        assert _pick_entries([{"text": "a"}], 5) == [{"text": "a"}]

    @pytest.mark.parametrize("n", [3, 4, 7, 20])
    def test_two_distinct_entries_in_thread_order(self, n):
        entries = [{"text": str(i)} for i in range(n)]
        for seed in range(2 * n):
            picked = _pick_entries(entries, seed)
            assert len(picked) == 2
            assert int(picked[0]["text"]) < int(picked[1]["text"])


class TestShareLines:
    def test_framed_lines_carry_stamp_and_text(self, personality):
        lines = thread_share_lines(personality, 9)
        assert len(lines) == len(personality["ten_year_thread"]["entries"])
        first = personality["ten_year_thread"]["entries"][0]
        assert first["stamp"] in lines[0] and first["text"] in lines[0]

    def test_gated_by_affection(self, personality):
        assert thread_share_lines(personality, 5) == []
        assert len(thread_share_lines(personality, 6)) < len(thread_share_lines(personality, 8))

    def test_no_framings_no_lines(self):
        assert thread_share_lines(_cfg([{"stamp": "s", "text": "t"}]), 9) == []


class TestInquiryDetector:
    @pytest.mark.parametrize("msg", [
        "Can I read the thread?",
        "Show me your thread",
        "What did the messages you sent during those ten years say?",
        "tell me about the texts you sent while I was gone",
        "Did you really message me every night?",
        "All those unanswered messages...",
        "What did you write me back then? The messages, I mean.",
    ])
    def test_positive(self, msg):
        assert wants_thread_inquiry(msg)

    @pytest.mark.parametrize("msg", [
        "I saw the messages you sent this morning",
        "Tell me about the ten years",
        "What's the context here?",
        "hello",
        "Text me when you're back from patrol",
    ])
    def test_negative(self, msg):
        assert not wants_thread_inquiry(msg)


class TestThreadRandomEvent:
    _EVENTS = {"random_events": {"ten_year_thread": {"min_affection": 6, "weight": 8,
                                                      "source": "ten_year_thread"}}}

    def _engine(self, affection):
        e = ProactiveEngine()
        e._on_message_callback = AsyncMock()
        e._affection_level = affection
        return e

    async def _fire(self, engine, cfg):
        with patch("app.proactive.events.now_local", return_value=datetime(2026, 1, 15, 12)), \
             patch("app.proactive.events.random.random", return_value=0.01), \
             patch("app.proactive.events.random.choice", side_effect=lambda seq: seq[0]), \
             patch("app.personality.load_personality", return_value=cfg):
            await engine._random_event()

    async def test_shares_an_entry_at_deep_devotion(self, personality):
        e = self._engine(6)
        await self._fire(e, {**self._EVENTS, "ten_year_thread": personality["ten_year_thread"]})
        e._on_message_callback.assert_awaited_once()
        sent = e._on_message_callback.call_args.args[0]
        assert personality["ten_year_thread"]["entries"][0]["text"] in sent

    async def test_silent_below_deep_devotion(self, personality):
        e = self._engine(5)
        await self._fire(e, {**self._EVENTS, "ten_year_thread": personality["ten_year_thread"]})
        e._on_message_callback.assert_not_awaited()
