"""Hardening tests for the 2026-05-29 audit follow-up.

Locks in two behaviors the deep audit flagged:
  1. The chat read path must FAIL OPEN — a Qdrant / data-service blip degrades
     recalled context to empty, it never raises out of the WS message handler
     (which would drop Klukai's reply and tear down the socket).
  2. Behind Cloudflare -> cloudflared -> loopback, the real client IP comes from
     CF-Connecting-IP, so the 3-strike IP ban targets the attacker instead of
     locking out the owner on the shared tunnel IP.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.helpers import client_ip
from app.memory import MemoryManager


# ── client_ip: real client behind Cloudflare → cloudflared → loopback ────────

class _FakeReq:
    def __init__(self, headers: dict, client_host: str | None = "10.0.0.9"):
        self.headers = headers
        self.client = type("C", (), {"host": client_host})() if client_host else None


def test_client_ip_prefers_cf_connecting_ip():
    req = _FakeReq({"cf-connecting-ip": "203.0.113.7", "x-forwarded-for": "8.8.8.8"})
    assert client_ip(req) == "203.0.113.7"


def test_client_ip_strips_whitespace():
    assert client_ip(_FakeReq({"cf-connecting-ip": "  203.0.113.7  "})) == "203.0.113.7"


def test_client_ip_xff_first_hop_fallback():
    assert client_ip(_FakeReq({"x-forwarded-for": "203.0.113.9, 70.0.0.1"})) == "203.0.113.9"


def test_client_ip_peer_fallback_when_no_proxy_headers():
    assert client_ip(_FakeReq({})) == "10.0.0.9"


def test_client_ip_unknown_when_no_client():
    assert client_ip(_FakeReq({}, client_host=None)) == "unknown"


# ── recall fail-open: a backing-store blip degrades to empty, never raises ───

@pytest.mark.asyncio
async def test_recall_episodes_fails_open_on_http_error():
    m = MemoryManager()
    m.embed_text = AsyncMock(return_value=[0.1, 0.2, 0.3])  # non-zero → proceeds
    m._http = AsyncMock()
    m._http.post = AsyncMock(side_effect=RuntimeError("qdrant down"))
    assert await m.recall_episodes("anything", user_id="jalsarraf") == []


@pytest.mark.asyncio
async def test_recall_facts_by_pattern_fails_open_on_http_error():
    m = MemoryManager()
    m._http = AsyncMock()
    m._http.get = AsyncMock(side_effect=RuntimeError("data service down"))
    assert await m.recall_facts_by_pattern("rel:joke:%", user_id="jalsarraf") == []


@pytest.mark.asyncio
async def test_recall_exchanges_fails_open_on_http_error():
    m = MemoryManager()
    m.embed_text = AsyncMock(return_value=[0.1, 0.2, 0.3])
    m._http = AsyncMock()
    m._http.post = AsyncMock(side_effect=RuntimeError("qdrant down"))
    assert await m.recall_exchanges("anything", user_id="jalsarraf") == []


@pytest.mark.asyncio
async def test_recall_for_prompt_never_propagates_subtask_error():
    m = MemoryManager()
    # One sub-call explodes; recall_for_prompt must still return safe defaults
    # (the return_exceptions path) rather than tearing down the chat handler.
    m.recall_episodes = AsyncMock(side_effect=RuntimeError("boom"))
    m.get_relationship_facts = AsyncMock(return_value={"k": "v"})
    m.recall_exchanges_with_recency = AsyncMock(return_value=[{"x": 1}])
    eps, facts, exch = await m.recall_for_prompt("hi", user_id="jalsarraf")
    assert eps == []                 # failed episode recall → empty, no raise
    assert facts == {"k": "v"}       # healthy paths preserved
    assert exch == [{"x": 1}]
