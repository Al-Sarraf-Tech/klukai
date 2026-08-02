"""Regression tests for the strict LLM residency policy."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.llm_router as llm_router


def test_llm_idle_ttl_is_exactly_fifteen_minutes():
    assert llm_router.MAX_LLM_IDLE_TTL_SECONDS == 900
    assert llm_router.LM_TTL_SECONDS == 900


def test_legacy_ttl_environment_override_is_ignored(monkeypatch):
    monkeypatch.setenv("LM_STUDIO_TTL", "999999")
    reloaded = importlib.reload(llm_router)

    assert reloaded.LM_TTL_SECONDS == 900


@pytest.mark.asyncio
async def test_legacy_keepalive_call_cannot_touch_the_backend():
    router = llm_router.LLMRouter()
    router._http = AsyncMock()

    await router.keepalive()

    router._http.post.assert_not_awaited()
