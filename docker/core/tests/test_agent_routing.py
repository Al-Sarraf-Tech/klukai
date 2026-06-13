"""Tests for LLMRouter.needs_agent() — agent loop vs. conversational routing."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm_router import LLMRouter, AGENT_SIGNALS


@pytest.fixture
def router():
    """Return an LLMRouter with LM Studio marked as available (no network)."""
    r = LLMRouter()
    r._lmstudio_available = True
    r._http = AsyncMock()
    return r


# ── Conversational questions should NOT trigger agent ────────────────────────

class TestConversationalQuestionsReturnFalse:
    @pytest.mark.asyncio
    async def test_what_technology_discovered(self, router):
        result = await router.needs_agent("What technology did they discover?")
        assert result is False

    @pytest.mark.asyncio
    async def test_how_is_squad_doing(self, router):
        result = await router.needs_agent("How is the squad doing?")
        assert result is False

    @pytest.mark.asyncio
    async def test_what_would_klukai_do(self, router):
        result = await router.needs_agent("What would Klukai do?")
        assert result is False

    @pytest.mark.asyncio
    async def test_do_you_remember_our_mission(self, router):
        result = await router.needs_agent("Do you remember our first mission?")
        assert result is False

    @pytest.mark.asyncio
    async def test_how_do_you_feel(self, router):
        result = await router.needs_agent("How do you feel about the Commander?")
        assert result is False

    @pytest.mark.asyncio
    async def test_what_is_your_opinion(self, router):
        result = await router.needs_agent("What is your opinion on the base layout?")
        assert result is False

    @pytest.mark.asyncio
    async def test_rp_question_with_they(self, router):
        result = await router.needs_agent("Where is the squad heading next?")
        assert result is False


# ── Real-world queries SHOULD trigger agent ──────────────────────────────────

class TestRealWorldQueriesTriggerAgent:
    @pytest.mark.asyncio
    async def test_weather_query(self, router):
        result = await router.needs_agent("What is the weather?")
        assert result is True

    @pytest.mark.asyncio
    async def test_search_for_news(self, router):
        result = await router.needs_agent("Search for latest news")
        assert result is True

    @pytest.mark.asyncio
    async def test_who_is_the_president(self, router):
        result = await router.needs_agent("Who is the president?")
        assert result is True

    @pytest.mark.asyncio
    async def test_browse_request(self, router):
        result = await router.needs_agent("Browse the official docs for me")
        assert result is True

    @pytest.mark.asyncio
    async def test_check_online(self, router):
        result = await router.needs_agent("Check online for that article")
        assert result is True


# ── AGENT_SIGNALS trigger correctly ──────────────────────────────────────────

class TestAgentSignals:
    @pytest.mark.asyncio
    async def test_search_x(self, router):
        result = await router.needs_agent("search X")
        assert result is True

    @pytest.mark.asyncio
    async def test_look_up_y(self, router):
        result = await router.needs_agent("look up Y")
        assert result is True

    @pytest.mark.asyncio
    async def test_what_time_is_it(self, router):
        result = await router.needs_agent("what time is it")
        assert result is True

    @pytest.mark.asyncio
    async def test_find_out(self, router):
        result = await router.needs_agent("find out the answer")
        assert result is True

    @pytest.mark.asyncio
    async def test_latest(self, router):
        result = await router.needs_agent("What's the latest on that topic?")
        assert result is True

    @pytest.mark.asyncio
    async def test_right_now(self, router):
        result = await router.needs_agent("Tell me what's happening right now")
        assert result is True


# ── Exclusion words prevent false positives ──────────────────────────────────

class TestExclusionWords:
    @pytest.mark.asyncio
    async def test_they_excludes(self, router):
        """'they' in the question should suppress agent routing."""
        result = await router.needs_agent("Where is the weapon they found?")
        assert result is False

    @pytest.mark.asyncio
    async def test_squad_excludes(self, router):
        result = await router.needs_agent("Who is the squad leader?")
        assert result is False

    @pytest.mark.asyncio
    async def test_mission_excludes(self, router):
        result = await router.needs_agent("Where is the mission objective?")
        assert result is False

    @pytest.mark.asyncio
    async def test_would_excludes(self, router):
        result = await router.needs_agent("What would happen if we tried that?")
        assert result is False

    @pytest.mark.asyncio
    async def test_could_excludes(self, router):
        result = await router.needs_agent("Where could we set up camp?")
        assert result is False


# ── Edge cases ───────────────────────────────────────────────────────────────

class TestAgentRoutingEdgeCases:
    @pytest.mark.asyncio
    async def test_lmstudio_unavailable_returns_false(self):
        """Without LM Studio, needs_agent always returns False."""
        r = LLMRouter()
        r._lmstudio_available = False
        r._lmstudio_last_check = 999999999.0  # prevent re-check
        r._http = AsyncMock()
        result = await r.needs_agent("search for something")
        assert result is False

    @pytest.mark.asyncio
    async def test_empty_message(self, router):
        result = await router.needs_agent("")
        assert result is False

    @pytest.mark.asyncio
    async def test_plain_greeting(self, router):
        result = await router.needs_agent("Hello Klukai!")
        assert result is False

    @pytest.mark.asyncio
    async def test_agent_signals_list_not_empty(self):
        """Sanity: AGENT_SIGNALS should have entries."""
        assert len(AGENT_SIGNALS) > 5
