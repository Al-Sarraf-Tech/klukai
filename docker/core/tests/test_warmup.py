"""Tests for app/warmup.py — she is ready when he reaches for her.

The requirement: when the Commander opens the app, she should be there. The
model is load-on-demand with a 15-minute residency ceiling (deliberate — the GPU
is shared and the policy is not to hold VRAM), so a quiet spell means his next
message pays a cold load and he waits on a loading bar.

Warming on connect moves that load into the seconds he spends typing. These
tests pin the two things that make it safe: it never holds VRAM longer than the
policy allows, and it can never make anything worse than it already was.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import warmup  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh():
    warmup._reset_for_tests()
    yield
    warmup._reset_for_tests()


def _session(status=200, body=None):
    """A stubbed aiohttp session whose get/post return `status` and `body`."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=body or {})
    resp.read = AsyncMock(return_value=b"")

    @asynccontextmanager
    async def _req(*a, **k):
        yield resp

    sess = MagicMock()
    sess.get = _req
    sess.post = _req

    @asynccontextmanager
    async def _mk(*a, **k):
        yield sess

    return _mk


def _models(model, state):
    return {"data": [{"id": model, "state": state}]}


@asynccontextmanager
async def _open_gate():
    yield


# ─────────────────────────────────────────────────────────────────────────
# is_loaded
# ─────────────────────────────────────────────────────────────────────────


class TestIsLoaded:
    @pytest.mark.asyncio
    async def test_reports_loaded(self):
        with patch("aiohttp.ClientSession", _session(body=_models("m", "loaded"))):
            with patch("app.lm_gateway.lm_studio_auth_headers", return_value={}):
                assert await warmup.is_loaded("m") is True

    @pytest.mark.asyncio
    async def test_reports_not_loaded(self):
        with patch("aiohttp.ClientSession", _session(body=_models("m", "not-loaded"))):
            with patch("app.lm_gateway.lm_studio_auth_headers", return_value={}):
                assert await warmup.is_loaded("m") is False

    @pytest.mark.asyncio
    async def test_unknown_model_is_indeterminate(self):
        with patch("aiohttp.ClientSession", _session(body=_models("other", "loaded"))):
            with patch("app.lm_gateway.lm_studio_auth_headers", return_value={}):
                assert await warmup.is_loaded("m") is None

    @pytest.mark.asyncio
    async def test_bad_status_is_indeterminate(self):
        with patch("aiohttp.ClientSession", _session(status=503)):
            with patch("app.lm_gateway.lm_studio_auth_headers", return_value={}):
                assert await warmup.is_loaded("m") is None

    @pytest.mark.asyncio
    async def test_unreachable_gateway_is_indeterminate_not_an_error(self):
        with patch("aiohttp.ClientSession", side_effect=OSError("no route")):
            with patch("app.lm_gateway.lm_studio_auth_headers", return_value={}):
                assert await warmup.is_loaded("m") is None


# ─────────────────────────────────────────────────────────────────────────
# warm_chat_model
# ─────────────────────────────────────────────────────────────────────────


class TestWarmChatModel:
    @pytest.mark.asyncio
    async def test_warms_when_the_model_is_cold(self):
        with patch.object(warmup, "is_loaded", AsyncMock(return_value=False)):
            with patch.object(warmup, "_issue_warm_request",
                              AsyncMock(return_value=True)) as issue:
                assert await warmup.warm_chat_model() is True
        issue.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_nothing_when_already_resident(self):
        """Re-warming a loaded model is pure GPU cost for no benefit."""
        with patch.object(warmup, "is_loaded", AsyncMock(return_value=True)):
            with patch.object(warmup, "_issue_warm_request", AsyncMock()) as issue:
                assert await warmup.warm_chat_model() is False
        issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_nothing_when_state_is_unknown(self):
        """A warm request to a gateway that is down is pure cost."""
        with patch.object(warmup, "is_loaded", AsyncMock(return_value=None)):
            with patch.object(warmup, "_issue_warm_request", AsyncMock()) as issue:
                assert await warmup.warm_chat_model() is False
        issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reconnect_storm_is_rate_limited(self):
        """Flaky wifi must not become a burst of load requests at the GPU."""
        with patch.object(warmup, "is_loaded", AsyncMock(return_value=False)):
            with patch.object(warmup, "_issue_warm_request",
                              AsyncMock(return_value=True)) as issue:
                results = [await warmup.warm_chat_model() for _ in range(5)]

        assert results.count(True) == 1
        assert issue.await_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_connects_only_warm_once(self):
        with patch.object(warmup, "is_loaded", AsyncMock(return_value=False)):
            with patch.object(warmup, "_issue_warm_request",
                              AsyncMock(return_value=True)) as issue:
                await asyncio.gather(*(warmup.warm_chat_model() for _ in range(8)))
        assert issue.await_count == 1

    @pytest.mark.asyncio
    async def test_force_bypasses_the_rate_limit(self):
        with patch.object(warmup, "is_loaded", AsyncMock(return_value=False)):
            with patch.object(warmup, "_issue_warm_request",
                              AsyncMock(return_value=True)) as issue:
                await warmup.warm_chat_model()
                await warmup.warm_chat_model(force=True)
        assert issue.await_count == 2

    @pytest.mark.asyncio
    async def test_can_be_switched_off(self):
        with patch.dict("os.environ", {"KLUKAI_DISABLE_WARMUP": "1"}):
            with patch.object(warmup, "is_loaded", AsyncMock()) as loaded:
                assert await warmup.warm_chat_model() is False
        loaded.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────
# _issue_warm_request — the policy-sensitive part
# ─────────────────────────────────────────────────────────────────────────


class TestIssueWarmRequest:
    @pytest.mark.asyncio
    async def test_never_extends_residency_beyond_the_policy_ceiling(self):
        """Warming must not become a backdoor keepalive.

        LLMRouter.keepalive is a deliberate no-op; this must stay consistent
        with it by sending the same TTL every other request sends.
        """
        from app.lm_gateway import LM_TTL_SECONDS

        captured = {}

        @asynccontextmanager
        async def _req(url, json=None, **k):
            captured["json"] = json
            resp = MagicMock()
            resp.status = 200
            resp.read = AsyncMock(return_value=b"")
            yield resp

        sess = MagicMock()
        sess.post = _req

        @asynccontextmanager
        async def _mk(*a, **k):
            yield sess

        with patch("aiohttp.ClientSession", _mk), \
             patch("app.lm_gateway.lm_studio_auth_headers", return_value={}), \
             patch("app.llm_router.get_lm_gate", return_value=_open_gate()), \
             patch("app.llm_router.mark_model_used", MagicMock()):
            assert await warmup._issue_warm_request("m") is True

        assert captured["json"]["ttl"] == LM_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_asks_for_the_smallest_possible_completion(self):
        captured = {}

        @asynccontextmanager
        async def _req(url, json=None, **k):
            captured["json"] = json
            resp = MagicMock()
            resp.status = 200
            resp.read = AsyncMock(return_value=b"")
            yield resp

        sess = MagicMock()
        sess.post = _req

        @asynccontextmanager
        async def _mk(*a, **k):
            yield sess

        with patch("aiohttp.ClientSession", _mk), \
             patch("app.lm_gateway.lm_studio_auth_headers", return_value={}), \
             patch("app.llm_router.get_lm_gate", return_value=_open_gate()), \
             patch("app.llm_router.mark_model_used", MagicMock()):
            await warmup._issue_warm_request("m")

        assert captured["json"]["max_tokens"] == 1
        assert captured["json"]["stream"] is False

    @pytest.mark.asyncio
    async def test_goes_through_the_shared_gate(self):
        """A warm-up must never race a real reply or an image render."""
        entered = []

        @asynccontextmanager
        async def _gate():
            entered.append(True)
            yield

        with patch("aiohttp.ClientSession", _session()), \
             patch("app.lm_gateway.lm_studio_auth_headers", return_value={}), \
             patch("app.llm_router.get_lm_gate", return_value=_gate()), \
             patch("app.llm_router.mark_model_used", MagicMock()):
            await warmup._issue_warm_request("m")

        assert entered == [True]

    @pytest.mark.asyncio
    async def test_a_failed_warm_up_is_not_an_error(self):
        with patch("aiohttp.ClientSession", side_effect=OSError("gpu host down")), \
             patch("app.lm_gateway.lm_studio_auth_headers", return_value={}), \
             patch("app.llm_router.get_lm_gate", return_value=_open_gate()):
            assert await warmup._issue_warm_request("m") is False

    @pytest.mark.asyncio
    async def test_a_rejected_warm_up_reports_false(self):
        with patch("aiohttp.ClientSession", _session(status=500)), \
             patch("app.lm_gateway.lm_studio_auth_headers", return_value={}), \
             patch("app.llm_router.get_lm_gate", return_value=_open_gate()):
            assert await warmup._issue_warm_request("m") is False


class TestWarmInBackground:
    @pytest.mark.asyncio
    async def test_returns_a_task_and_does_not_block(self):
        with patch.object(warmup, "warm_chat_model", AsyncMock(return_value=True)):
            task = warmup.warm_in_background()
            assert task is not None
            await task

    def test_without_a_running_loop_it_declines_quietly(self):
        assert warmup.warm_in_background() is None
