"""Tests for app.llm_router module-level constants and helpers."""

from __future__ import annotations

from app import llm_router


class TestAgentSignals:
    def test_signals_list_present(self):
        assert isinstance(llm_router.AGENT_SIGNALS, list)
        assert len(llm_router.AGENT_SIGNALS) > 5

    def test_signals_include_common_intents(self):
        signals = [s.lower() for s in llm_router.AGENT_SIGNALS]
        # At least one of these should be present
        assert any(s in signals for s in ["search", "look up", "find out", "today's"])


class TestCloudModels:
    def test_only_two_claude_models_have_an_effort(self):
        assert llm_router.MODEL_EFFORT == {
            llm_router.CLAUDE_PRIMARY: "medium",
            llm_router.CLAUDE_SECONDARY: "high",
        }

    def test_no_local_model_aliases_remain(self):
        for name in ("LOCAL_CASUAL", "LOCAL_AGENT", "LOCAL_TOOLS", "LM_STUDIO_URL"):
            assert not hasattr(llm_router, name)

    def test_claude_primary_needs_flag_and_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "false")
        assert llm_router.claude_primary_configured() is False
        monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "true")
        assert llm_router.claude_primary_configured() is True
        monkeypatch.setenv("ANTHROPIC_API_KEY", " ")
        assert llm_router.claude_primary_configured() is False
