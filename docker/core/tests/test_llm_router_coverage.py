"""Behavioral coverage tests for app.llm_router.LLMRouter.

Every test asserts a concrete behavior: which model/endpoint is chosen, the
circuit-breaker state transitions (available -> down -> reprobe), the Claude
fallback ordering, streaming token aggregation (including the thinking-model
reasoning fallback), keepalive gating, and header construction.

All network is mocked at the httpx boundary. The monotonic clock is patched
so the 15s health-recheck window is deterministic — no real sleeps.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.llm_router as lr
from app.llm_router import (
    LLMRouter,
    LM_STUDIO_URL,
    LOCAL_CASUAL,
    LOCAL_TOOLS,
    CLOUD_FALLBACK,
    _HEALTH_RECHECK_INTERVAL,
)
from app.models import LLMConfig, SessionState


# ── Helpers ───────────────────────────────────────────────────────────────


def _session() -> SessionState:
    return SessionState(conversation_id="c1")


def _sse(*lines: str):
    """Build an async line iterator yielding SSE 'data: ...' lines."""

    async def _gen():
        for ln in lines:
            yield ln

    return _gen()


class _FakeStreamCtx:
    """Async context manager standing in for httpx.AsyncClient.stream(...).

    Yields an object whose .aiter_lines() replays the supplied SSE lines and
    whose .raise_for_status() honours the given status_code.
    """

    def __init__(self, lines, status_code=200):
        self._lines = lines
        self._status = status_code

    async def __aenter__(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock(
            side_effect=(None if self._status < 400 else httpx.HTTPStatusError(
                "boom", request=MagicMock(), response=MagicMock()
            ))
        )
        resp.aiter_lines = lambda: _sse(*self._lines)
        return resp

    async def __aexit__(self, *exc):
        return False


def _content_chunk(text: str) -> str:
    return 'data: {"choices":[{"delta":{"content":"%s"}}]}' % text


def _reasoning_chunk(text: str) -> str:
    return 'data: {"choices":[{"delta":{"reasoning_content":"%s"}}]}' % text


class _FakeAnthropicStreamCtx:
    """Async CM matching anthropic .messages.stream(...) -> .text_stream."""

    def __init__(self, tokens):
        self._tokens = tokens

    async def __aenter__(self):
        stream = MagicMock()

        async def _txt():
            for t in self._tokens:
                yield t

        stream.text_stream = _txt()
        return stream

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset module globals touched by tests so cases stay isolated."""
    lr._model_last_used.clear()
    lr._last_user_message = 0.0
    lr._seeding_active = False
    lr._lm_gate = None
    yield
    lr._model_last_used.clear()
    lr._last_user_message = 0.0
    lr._seeding_active = False
    lr._lm_gate = None


# ── _lm_headers (lines 48-50) ───────────────────────────────────────────────


class TestLMHeaders:
    def test_with_token_returns_bearer(self):
        with patch.object(lr, "LM_STUDIO_TOKEN", "secret-tok"):
            assert lr._lm_headers() == {"Authorization": "Bearer secret-tok"}

    def test_without_token_returns_empty(self):
        with patch.object(lr, "LM_STUDIO_TOKEN", ""):
            assert lr._lm_headers() == {}


# ── init / close (lines 132-136, 139-140) ───────────────────────────────────


class TestInitClose:
    @pytest.mark.asyncio
    async def test_init_builds_client_and_probes(self):
        r = LLMRouter()
        with patch.object(lr, "ANTHROPIC_API_KEY", "ak"), patch.object(
            lr.anthropic, "AsyncAnthropic"
        ) as anth, patch.object(
            LLMRouter, "_check_lmstudio", AsyncMock(return_value=True)
        ) as chk:
            await r.init()
        assert r._http is not None
        anth.assert_called_once_with(api_key="ak")
        chk.assert_awaited_once()
        await r.close()

    @pytest.mark.asyncio
    async def test_init_no_anthropic_key_leaves_client_none(self):
        r = LLMRouter()
        with patch.object(lr, "ANTHROPIC_API_KEY", ""), patch.object(
            LLMRouter, "_check_lmstudio", AsyncMock(return_value=False)
        ):
            await r.init()
        assert r._anthropic is None

    @pytest.mark.asyncio
    async def test_close_calls_aclose(self):
        r = LLMRouter()
        r._http = AsyncMock()
        await r.close()
        r._http.aclose.assert_awaited_once()


# ── _check_lmstudio (lines 142-150) ─────────────────────────────────────────


class TestCheckLMStudio:
    @pytest.mark.asyncio
    async def test_200_sets_available_true(self):
        r = LLMRouter()
        r._http = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        r._http.get = AsyncMock(return_value=resp)
        with patch.object(lr.time, "monotonic", return_value=100.0):
            ok = await r._check_lmstudio()
        assert ok is True
        assert r._lmstudio_available is True
        assert r._lmstudio_last_check == 100.0

    @pytest.mark.asyncio
    async def test_non_200_sets_available_false(self):
        r = LLMRouter()
        r._http = MagicMock()
        resp = MagicMock()
        resp.status_code = 503
        r._http.get = AsyncMock(return_value=resp)
        ok = await r._check_lmstudio()
        assert ok is False
        assert r._lmstudio_available is False

    @pytest.mark.asyncio
    async def test_http_error_sets_available_false(self):
        r = LLMRouter()
        r._http = MagicMock()
        r._http.get = AsyncMock(side_effect=httpx.ConnectError("no route"))
        ok = await r._check_lmstudio()
        assert ok is False
        assert r._lmstudio_available is False


# ── _ensure_lmstudio_fresh circuit breaker (lines 152-162) ──────────────────


class TestEnsureFreshCircuitBreaker:
    @pytest.mark.asyncio
    async def test_already_available_short_circuits(self):
        r = LLMRouter()
        r._lmstudio_available = True
        r._check_lmstudio = AsyncMock()
        assert await r._ensure_lmstudio_fresh() is True
        r._check_lmstudio.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_window_not_elapsed_stays_down(self):
        """Inside the 15s window the breaker stays open (no reprobe)."""
        r = LLMRouter()
        r._lmstudio_available = False
        r._lmstudio_last_check = 1000.0
        r._check_lmstudio = AsyncMock(return_value=True)
        with patch.object(lr.time, "monotonic", return_value=1000.0 + 5.0):
            assert await r._ensure_lmstudio_fresh() is False
        r._check_lmstudio.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_window_elapsed_reprobes_and_recovers(self):
        """After 15s the breaker re-probes; success flips it closed (up)."""
        r = LLMRouter()
        r._lmstudio_available = False
        r._lmstudio_last_check = 1000.0

        async def _recover():
            r._lmstudio_available = True
            return True

        r._check_lmstudio = AsyncMock(side_effect=_recover)
        with patch.object(
            lr.time, "monotonic", return_value=1000.0 + _HEALTH_RECHECK_INTERVAL
        ):
            assert await r._ensure_lmstudio_fresh() is True
        r._check_lmstudio.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_window_elapsed_reprobe_still_down(self):
        r = LLMRouter()
        r._lmstudio_available = False
        r._lmstudio_last_check = 1000.0
        r._check_lmstudio = AsyncMock(return_value=False)
        with patch.object(lr.time, "monotonic", return_value=1000.0 + 99.0):
            assert await r._ensure_lmstudio_fresh() is False
        r._check_lmstudio.assert_awaited_once()


# ── route() model selection (lines 173-210) ─────────────────────────────────


class TestRoute:
    @pytest.mark.asyncio
    async def test_down_triggers_reprobe_before_routing(self):
        r = LLMRouter()
        r._lmstudio_available = False
        r._ensure_lmstudio_fresh = AsyncMock(return_value=False)
        r._anthropic = object()
        cfg = await r.route("hi", _session())
        r._ensure_lmstudio_fresh.assert_awaited_once()
        # still down -> cloud fallback
        assert cfg.provider == "anthropic"
        assert cfg.model == CLOUD_FALLBACK

    @pytest.mark.asyncio
    async def test_user_override_claude_routes_anthropic(self):
        r = LLMRouter()
        r._lmstudio_available = True
        cfg = await r.route("x", _session(), user_override="claude-sonnet-4-20250514")
        assert cfg.provider == "anthropic"
        assert cfg.model == "claude-sonnet-4-20250514"
        assert cfg.temperature == 0.7

    @pytest.mark.asyncio
    async def test_user_override_local_routes_lmstudio(self):
        r = LLMRouter()
        r._lmstudio_available = True
        cfg = await r.route("x", _session(), user_override="my-local-model")
        assert cfg.provider == "lmstudio"
        assert cfg.model == "my-local-model"
        assert cfg.base_url == LM_STUDIO_URL

    @pytest.mark.asyncio
    async def test_needs_tools_routes_to_local_agent(self):
        r = LLMRouter()
        r._lmstudio_available = True
        cfg = await r.route("x", _session(), needs_tools=True)
        assert cfg.provider == "lmstudio"
        assert cfg.model == LOCAL_TOOLS

    @pytest.mark.asyncio
    async def test_default_chat_routes_to_local_casual(self):
        r = LLMRouter()
        r._lmstudio_available = True
        cfg = await r.route("just chatting", _session())
        assert cfg.provider == "lmstudio"
        assert cfg.model == LOCAL_CASUAL

    @pytest.mark.asyncio
    async def test_no_local_no_anthropic_raises(self):
        r = LLMRouter()
        r._lmstudio_available = False
        r._ensure_lmstudio_fresh = AsyncMock(return_value=False)
        r._anthropic = None
        with pytest.raises(RuntimeError, match="No LLM backend"):
            await r.route("x", _session())

    @pytest.mark.asyncio
    async def test_needs_tools_but_local_down_falls_to_cloud(self):
        """needs_tools requires local; when down, falls through to Claude."""
        r = LLMRouter()
        r._lmstudio_available = False
        r._ensure_lmstudio_fresh = AsyncMock(return_value=False)
        r._anthropic = object()
        cfg = await r.route("search now", _session(), needs_tools=True)
        assert cfg.provider == "anthropic"
        assert cfg.model == CLOUD_FALLBACK


# ── needs_agent reprobe branch (line 214-217) ───────────────────────────────


class TestNeedsAgentReprobe:
    @pytest.mark.asyncio
    async def test_down_then_reprobe_fails_returns_false(self):
        r = LLMRouter()
        r._lmstudio_available = False
        r._ensure_lmstudio_fresh = AsyncMock(return_value=False)
        assert await r.needs_agent("search the web") is False
        r._ensure_lmstudio_fresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_down_then_reprobe_recovers_then_classifies(self):
        r = LLMRouter()
        r._lmstudio_available = False

        async def _recover():
            r._lmstudio_available = True
            return True

        r._ensure_lmstudio_fresh = AsyncMock(side_effect=_recover)
        # "search" is an AGENT_SIGNAL with no RP context -> True
        assert await r.needs_agent("search latest prices") is True

    @pytest.mark.asyncio
    async def test_question_mark_external_info_triggers(self):
        """'where is' external-info question (no RP words) hits line 245."""
        r = LLMRouter()
        r._lmstudio_available = True
        assert await r.needs_agent("Where is Tokyo located?") is True

    @pytest.mark.asyncio
    async def test_signal_with_rp_context_returns_false(self):
        """AGENT_SIGNAL present but RP word also present -> RP wins (line 236)."""
        r = LLMRouter()
        r._lmstudio_available = True
        # "weather" (signal) + "you"/"your" (rp_context) -> False
        assert await r.needs_agent("What's the weather like in your sector?") is False

    @pytest.mark.asyncio
    async def test_no_signal_no_question_returns_false(self):
        """Plain statement: no signal, no qualifying '?' -> final return False (247)."""
        r = LLMRouter()
        r._lmstudio_available = True
        assert await r.needs_agent("The supply crates arrived.") is False


# ── stream(): anthropic provider path (lines 259-265) ───────────────────────


class TestStreamAnthropic:
    @pytest.mark.asyncio
    async def test_anthropic_path_yields_and_marks_used(self):
        r = LLMRouter()
        r._anthropic = MagicMock()
        r._anthropic.messages.stream = MagicMock(
            return_value=_FakeAnthropicStreamCtx(["Hel", "lo"])
        )
        cfg = LLMConfig(provider="anthropic", model="claude-x")
        out = [t async for t in r.stream("sys", [{"role": "user", "content": "hi"}], cfg)]
        assert "".join(out) == "Hello"
        # marked used
        assert "claude-x" in lr._model_last_used


# ── stream(): LM Studio success + gate (lines 267-276) ──────────────────────


class TestStreamLMStudioSuccess:
    @pytest.mark.asyncio
    async def test_local_success_marks_used_no_fallback(self):
        r = LLMRouter()
        r._anthropic = MagicMock()  # would be used on fallback — must NOT be
        cfg = LLMConfig(provider="lmstudio", model=LOCAL_CASUAL, base_url=LM_STUDIO_URL)

        async def _fake_local(sp, msgs, c):
            yield "to"
            yield "ken"

        r._stream_lmstudio = _fake_local
        out = [t async for t in r.stream("sys", [], cfg)]
        assert "".join(out) == "token"
        assert LOCAL_CASUAL in lr._model_last_used
        r._anthropic.messages.stream.assert_not_called()


# ── stream(): failure -> circuit opens -> Claude fallback (277-301) ─────────


class TestStreamFailureFallback:
    @pytest.mark.asyncio
    async def test_readtimeout_opens_breaker_and_falls_back(self):
        r = LLMRouter()
        r._lmstudio_available = True
        r._anthropic = MagicMock()
        r._anthropic.messages.stream = MagicMock(
            return_value=_FakeAnthropicStreamCtx(["fall", "back"])
        )
        cfg = LLMConfig(provider="lmstudio", model=LOCAL_CASUAL, base_url=LM_STUDIO_URL)

        async def _boom(sp, msgs, c):
            raise httpx.ReadTimeout("slow")
            yield  # pragma: no cover

        r._stream_lmstudio = _boom
        with patch.object(lr.time, "monotonic", return_value=500.0):
            out = [t async for t in r.stream("sys", [], cfg)]
        assert "".join(out) == "fallback"
        # breaker opened
        assert r._lmstudio_available is False
        assert r._lmstudio_last_check == 500.0

    @pytest.mark.asyncio
    async def test_generic_exception_opens_breaker_and_falls_back(self):
        r = LLMRouter()
        r._lmstudio_available = True
        r._anthropic = MagicMock()
        r._anthropic.messages.stream = MagicMock(
            return_value=_FakeAnthropicStreamCtx(["X"])
        )
        cfg = LLMConfig(provider="lmstudio", model=LOCAL_CASUAL, base_url=LM_STUDIO_URL)

        async def _boom(sp, msgs, c):
            raise ValueError("kaput")
            yield  # pragma: no cover

        r._stream_lmstudio = _boom
        out = [t async for t in r.stream("sys", [], cfg)]
        assert "".join(out) == "X"
        assert r._lmstudio_available is False

    @pytest.mark.asyncio
    async def test_local_fails_no_anthropic_yields_disrupted_message(self):
        r = LLMRouter()
        r._lmstudio_available = True
        r._anthropic = None
        cfg = LLMConfig(provider="lmstudio", model=LOCAL_CASUAL, base_url=LM_STUDIO_URL)

        async def _boom(sp, msgs, c):
            raise RuntimeError("dead")
            yield  # pragma: no cover

        r._stream_lmstudio = _boom
        out = [t async for t in r.stream("sys", [], cfg)]
        assert out == ["Communications disrupted, Commander. Standby for reconnection."]


# ── _stream_lmstudio SSE parsing (lines 428-475) ────────────────────────────


class TestStreamLMStudioParsing:
    @pytest.mark.asyncio
    async def test_content_tokens_yielded_in_order(self):
        r = LLMRouter()
        r._http = MagicMock()
        r._http.stream = MagicMock(
            return_value=_FakeStreamCtx(
                [_content_chunk("Hel"), _content_chunk("lo"), "data: [DONE]"]
            )
        )
        cfg = LLMConfig(provider="lmstudio", model="m", base_url=LM_STUDIO_URL)
        out = [t async for t in r._stream_lmstudio("sys", [], cfg)]
        assert out == ["Hel", "lo"]

    @pytest.mark.asyncio
    async def test_non_data_lines_and_bad_json_skipped(self):
        r = LLMRouter()
        r._http = MagicMock()
        r._http.stream = MagicMock(
            return_value=_FakeStreamCtx(
                [
                    ": keep-alive comment",
                    "",
                    "data: {not json}",
                    _content_chunk("ok"),
                    "data: [DONE]",
                ]
            )
        )
        cfg = LLMConfig(provider="lmstudio", model="m", base_url=LM_STUDIO_URL)
        out = [t async for t in r._stream_lmstudio("sys", [], cfg)]
        assert out == ["ok"]

    @pytest.mark.asyncio
    async def test_thinking_only_model_surfaces_reasoning(self):
        """No content tokens ever arrive -> reasoning_content is surfaced."""
        r = LLMRouter()
        r._http = MagicMock()
        r._http.stream = MagicMock(
            return_value=_FakeStreamCtx(
                [_reasoning_chunk("think "), _reasoning_chunk("hard"), "data: [DONE]"]
            )
        )
        cfg = LLMConfig(provider="lmstudio", model="m", base_url=LM_STUDIO_URL)
        out = [t async for t in r._stream_lmstudio("sys", [], cfg)]
        assert out == ["think hard"]

    @pytest.mark.asyncio
    async def test_content_present_suppresses_reasoning_fallback(self):
        """When content arrives, trailing reasoning is ignored (no dup yield)."""
        r = LLMRouter()
        r._http = MagicMock()
        r._http.stream = MagicMock(
            return_value=_FakeStreamCtx(
                [_content_chunk("real"), _reasoning_chunk("ignored"), "data: [DONE]"]
            )
        )
        cfg = LLMConfig(provider="lmstudio", model="m", base_url=LM_STUDIO_URL)
        out = [t async for t in r._stream_lmstudio("sys", [], cfg)]
        assert out == ["real"]

    @pytest.mark.asyncio
    async def test_raise_for_status_propagates(self):
        r = LLMRouter()
        r._http = MagicMock()
        r._http.stream = MagicMock(
            return_value=_FakeStreamCtx([_content_chunk("x")], status_code=500)
        )
        cfg = LLMConfig(provider="lmstudio", model="m", base_url=LM_STUDIO_URL)
        with pytest.raises(httpx.HTTPStatusError):
            [t async for t in r._stream_lmstudio("sys", [], cfg)]


# ── complete_local (lines 318-355) ──────────────────────────────────────────


class TestCompleteLocal:
    @pytest.mark.asyncio
    async def test_posts_body_and_returns_json(self):
        r = LLMRouter()
        r._http = MagicMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(
            return_value={
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 7},
            }
        )
        r._http.post = AsyncMock(return_value=resp)
        cfg = LLMConfig(provider="lmstudio", model="agent-m", base_url=LM_STUDIO_URL)
        data = await r.complete_local("sys", [{"role": "user", "content": "q"}], cfg)
        assert data["choices"][0]["message"]["content"] == "hi"
        # verify the endpoint + system-prepend + non-stream body
        call = r._http.post.call_args
        assert call.args[0] == f"{LM_STUDIO_URL}/v1/chat/completions"
        body = call.kwargs["json"]
        assert body["model"] == "agent-m"
        assert body["stream"] is False
        assert body["messages"][0] == {"role": "system", "content": "sys"}
        assert "tools" not in body

    @pytest.mark.asyncio
    async def test_tools_attach_tool_choice_auto(self):
        r = LLMRouter()
        r._http = MagicMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"choices": [{"message": {}}], "usage": {}})
        r._http.post = AsyncMock(return_value=resp)
        cfg = LLMConfig(provider="lmstudio", model="agent-m", base_url=LM_STUDIO_URL)
        tools = [{"type": "function", "function": {"name": "t"}}]
        await r.complete_local("sys", [], cfg, tools=tools)
        body = r._http.post.call_args.kwargs["json"]
        assert body["tools"] == tools
        assert body["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_http_error_status_raises(self):
        r = LLMRouter()
        r._http = MagicMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "500", request=MagicMock(), response=MagicMock()
            )
        )
        r._http.post = AsyncMock(return_value=resp)
        cfg = LLMConfig(provider="lmstudio", model="m", base_url=LM_STUDIO_URL)
        with pytest.raises(httpx.HTTPStatusError):
            await r.complete_local("sys", [], cfg)

    @pytest.mark.asyncio
    async def test_metrics_recording_failure_is_swallowed(self):
        """record_llm_usage raising must NOT break the completion (lines 353-354)."""
        r = LLMRouter()
        r._http = MagicMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(
            return_value={"choices": [{"message": {"content": "ok"}}], "usage": {}}
        )
        r._http.post = AsyncMock(return_value=resp)
        cfg = LLMConfig(provider="lmstudio", model="m", base_url=LM_STUDIO_URL)
        with patch(
            "app.observability.record_llm_usage", side_effect=RuntimeError("metrics down")
        ):
            data = await r.complete_local("sys", [], cfg)
        assert data["choices"][0]["message"]["content"] == "ok"


# ── keepalive (lines 370-423) ───────────────────────────────────────────────


class TestKeepalive:
    @pytest.mark.asyncio
    async def test_skips_when_gate_busy(self):
        r = LLMRouter()
        r._http = AsyncMock()
        gate = lr.get_lm_gate()
        async with gate:  # gate locked -> busy
            await r.keepalive()
        r._http.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_lmstudio_unavailable(self):
        r = LLMRouter()
        r._http = AsyncMock()
        r._lmstudio_available = False
        r._ensure_lmstudio_fresh = AsyncMock(return_value=False)
        await r.keepalive()
        r._http.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_during_seeding(self):
        r = LLMRouter()
        r._http = AsyncMock()
        r._lmstudio_available = True
        lr.set_seeding_active(True)
        await r.keepalive()
        r._http.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_idle_unload_skips_ping(self):
        """No connections, no mission, not early-AM, user idle -> evict (no ping)."""
        r = LLMRouter()
        r._http = AsyncMock()
        r._lmstudio_available = True
        fake_ctx = MagicMock()
        fake_ctx.ws._connections = []
        with patch.dict(
            sys.modules,
            {
                "app.context": fake_ctx,
                "app.proactive": MagicMock(has_active_mission=lambda: False),
            },
        ), patch.object(lr, "_is_early_am_window", return_value=False), patch.object(
            lr, "_is_user_idle", return_value=True
        ):
            await r.keepalive()
        r._http.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pings_model_when_active_and_stale(self):
        """Connections present -> keepalive POST fires and marks model used."""
        r = LLMRouter()
        r._http = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        r._http.post = AsyncMock(return_value=resp)
        r._lmstudio_available = True
        fake_ctx = MagicMock()
        fake_ctx.ws._connections = ["someone"]
        with patch.dict(
            sys.modules,
            {
                "app.context": fake_ctx,
                "app.proactive": MagicMock(has_active_mission=lambda: True),
            },
        ):
            await r.keepalive()
        r._http.post.assert_awaited_once()
        assert LOCAL_CASUAL in lr._model_last_used
        body = r._http.post.call_args.kwargs["json"]
        assert body["model"] == LOCAL_CASUAL
        assert body["max_tokens"] == 1

    @pytest.mark.asyncio
    async def test_skips_ping_when_model_fresh(self):
        r = LLMRouter()
        r._http = AsyncMock()
        r._lmstudio_available = True
        lr.mark_model_used(LOCAL_CASUAL)  # fresh -> needs_keepalive False
        fake_ctx = MagicMock()
        fake_ctx.ws._connections = ["someone"]
        with patch.dict(
            sys.modules,
            {
                "app.context": fake_ctx,
                "app.proactive": MagicMock(has_active_mission=lambda: True),
            },
        ):
            await r.keepalive()
        r._http.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ping_exception_is_swallowed(self):
        r = LLMRouter()
        r._http = MagicMock()
        r._http.post = AsyncMock(side_effect=RuntimeError("down"))
        r._lmstudio_available = True
        fake_ctx = MagicMock()
        fake_ctx.ws._connections = ["x"]
        with patch.dict(
            sys.modules,
            {
                "app.context": fake_ctx,
                "app.proactive": MagicMock(has_active_mission=lambda: True),
            },
        ):
            await r.keepalive()  # must not raise
        assert LOCAL_CASUAL not in lr._model_last_used


# ── module-level idle/keepalive helpers ─────────────────────────────────────


class TestIdleAndKeepaliveHelpers:
    def test_mark_user_active_then_not_idle(self):
        with patch.object(lr.time, "monotonic", return_value=10.0):
            lr.mark_user_active()
        with patch.object(lr.time, "monotonic", return_value=10.0 + 5):
            assert lr._is_user_idle() is False

    def test_user_idle_after_timeout(self):
        with patch.object(lr.time, "monotonic", return_value=10.0):
            lr.mark_user_active()
        with patch.object(lr.time, "monotonic", return_value=10.0 + lr.IDLE_TIMEOUT + 1):
            assert lr._is_user_idle() is True

    def test_never_messaged_not_idle(self):
        assert lr._is_user_idle() is False

    def test_model_needs_keepalive_when_unused(self):
        assert lr.model_needs_keepalive("never-seen") is True

    def test_model_keepalive_window(self):
        # Use a non-zero base: model_needs_keepalive treats last==0 as "never used".
        base = 1000.0
        with patch.object(lr.time, "monotonic", return_value=base):
            lr.mark_model_used("m")
        with patch.object(
            lr.time, "monotonic", return_value=base + lr._KEEPALIVE_INTERVAL + 1
        ):
            assert lr.model_needs_keepalive("m") is True
        with patch.object(lr.time, "monotonic", return_value=base):
            lr.mark_model_used("m")
        with patch.object(lr.time, "monotonic", return_value=base + 10.0):
            assert lr.model_needs_keepalive("m") is False

    def test_lm_gate_busy_reflects_lock(self):
        gate = lr.get_lm_gate()
        assert lr.lm_gate_busy() is False
        assert gate is lr.get_lm_gate()  # singleton

    def test_early_am_window(self):
        # _is_early_am_window does `from datetime import datetime` then datetime.now().
        import datetime as _dtmod

        real = _dtmod.datetime

        def _at(hour: int):
            class _Fake(real):
                @classmethod
                def now(cls, tz=None):
                    return real(2026, 1, 1, hour, 0, 0)

            return _Fake

        with patch.object(_dtmod, "datetime", _at(3)):
            assert lr._is_early_am_window() is True
        with patch.object(_dtmod, "datetime", _at(12)):
            assert lr._is_early_am_window() is False

    def test_set_seeding_active_toggles_global(self):
        lr.set_seeding_active(True)
        assert lr._seeding_active is True
        lr.set_seeding_active(False)
        assert lr._seeding_active is False
