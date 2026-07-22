"""Shared fixtures for companion-core tests.

All external services (LM Studio, Redis, PostgreSQL) are mocked.
Tests run without any network or infrastructure dependencies.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Never let an incompletely mocked unit test fall through to the live GPU
# services, even if the developer's shell exports production URLs. The live
# integration target opts in explicitly from Makefile.
if os.environ.get("KLUKAI_TEST_ALLOW_LIVE_BACKENDS") != "1":
    os.environ["LM_STUDIO_URL"] = "http://127.0.0.1:1"
    os.environ["VOICE_URL"] = "http://127.0.0.1:1"
    os.environ["COMFYUI_URL"] = "http://127.0.0.1:1"

# ── Psycopg shim ─────────────────────────────────────────────────────────────
# Mock psycopg/psycopg_pool at the module level before any app code imports
# them. This allows app.affection and app.main to be imported in test
# environments that don't have psycopg installed (e.g., dev workstations).
# The mocks are injected early enough that importorskip("psycopg") still
# works correctly for tests that need a real psycopg.
def _inject_psycopg_shim() -> None:
    """Install lightweight MagicMock shims for psycopg if not installed."""
    if "psycopg" not in sys.modules:
        try:
            import psycopg  # noqa: F401 — already installed, nothing to do
        except ModuleNotFoundError:
            sys.modules.setdefault("psycopg", MagicMock())
            sys.modules.setdefault("psycopg.rows", MagicMock())
            sys.modules.setdefault("psycopg_pool", MagicMock())

_inject_psycopg_shim()

# Ensure the app package is importable from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Resolve personality.yaml to the repo copy when the in-container /config path
# is absent (dev workstations, CI checkouts). app.personality.loader reads the
# PERSONALITY_PATH default at call time, so setting it here — before any test
# triggers a load — keeps prompt-assembly tests portable without per-test paths.
if not os.environ.get("PERSONALITY_PATH"):
    _repo_personality = (
        Path(__file__).resolve().parent.parent.parent.parent / "config" / "personality.yaml"
    )
    if _repo_personality.exists():
        os.environ["PERSONALITY_PATH"] = str(_repo_personality)


# ── LM Studio mock ──────────────────────────────────────────────────────────

def _lm_studio_response(content: str = "Test response.") -> dict:
    """Build a minimal LM Studio /v1/chat/completions response."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "model": "test-model",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class FakeHTTPResponse:
    """Minimal stand-in for httpx.Response."""

    def __init__(self, status_code: int = 200, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data or _lm_studio_response()

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


@pytest.fixture
def mock_lm_studio_response():
    """Return a factory for fake LM Studio HTTP responses."""
    return _lm_studio_response


@pytest.fixture
def fake_http_response():
    """Return a factory for FakeHTTPResponse objects."""
    return FakeHTTPResponse


@pytest.fixture
def mock_httpx_post():
    """Patch httpx.AsyncClient.post to return a canned LM Studio response.

    Yields the mock so tests can inspect calls or override return_value.
    """
    resp = FakeHTTPResponse(200, _lm_studio_response())
    mock_post = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.post", mock_post):
        yield mock_post


# ── Redis mock ───────────────────────────────────────────────────────────────

class FakeRedis:
    """In-memory dict-based stand-in for redis.asyncio.Redis."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, **kwargs: Any) -> None:
        self._store[key] = value

    async def delete(self, *keys: str) -> None:
        for k in keys:
            self._store.pop(k, None)

    async def exists(self, key: str) -> int:
        return 1 if key in self._store else 0

    async def keys(self, pattern: str = "*") -> list[str]:
        return list(self._store.keys())


@pytest.fixture
def fake_redis():
    """Return a fresh FakeRedis instance."""
    return FakeRedis()


# ── Affection state fixtures ────────────────────────────────────────────────

@pytest.fixture(params=[0, 3, 5, 7, 9], ids=["aff0", "aff3", "aff5", "aff7", "aff9"])
def affection_level(request) -> int:
    """Parameterized affection levels covering key behavioral thresholds.

    0 = cold/formal, 3 = romance templates unlock, 5 = LLM romance,
    7 = openly intimate, 9 = max devotion.
    """
    return request.param


@pytest.fixture
def make_affection_state():
    """Factory fixture that builds a mock AffectionState at a given level."""
    def _make(level: int):
        state = MagicMock()
        state.level = level
        state.score = level * 100  # rough approximation
        state.first_interaction = None
        return state
    return _make


# ── WebSocket manager mock ──────────────────────────────────────────────────

@pytest.fixture
def mock_ws_manager():
    """Mock WSManager with async send helpers."""
    ws = MagicMock()
    ws.connected = True
    ws.is_connected = MagicMock(return_value=True)
    ws.send_token = AsyncMock()
    ws.send_done = AsyncMock()
    ws.send_thinking = AsyncMock()
    ws.send_mood = AsyncMock()
    ws.send_proactive = AsyncMock()
    ws.broadcast = AsyncMock()
    return ws


# ── Session state factory ───────────────────────────────────────────────────

@pytest.fixture
def make_session():
    """Factory fixture that builds SessionState with a given number of turns."""
    from app.models import SessionState

    def _make(
        turn_count: int = 0,
        context_summary: str | None = None,
        mood: str = "composed",
    ) -> SessionState:
        turns = []
        for i in range(turn_count):
            role = "user" if i % 2 == 0 else "assistant"
            turns.append({"role": role, "content": f"Turn {i} content"})
        return SessionState(
            conversation_id="test-conv",
            turns=turns,
            context_summary=context_summary,
            mood=mood,
            turn_count=turn_count,
        )

    return _make


# ── Personality config fixtures ─────────────────────────────────────────────

@pytest.fixture
def personality_config_path():
    """Resolve personality.yaml path — works in container, repo, and the
    mutmut `mutants/` sandbox.

    The PERSONALITY_PATH env var is checked FIRST: mutmut copies the tests into
    a `mutants/` tree where the repo-relative `__file__` walk resolves to a
    nonexistent path, which used to skip every fixture-dependent test (and so
    silently under-measured the mutation kill rate on affection.py). CI/mutmut
    set PERSONALITY_PATH to an absolute path; honour it.
    """
    env_path = os.environ.get("PERSONALITY_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    container = Path("/config/personality.yaml")
    if container.exists():
        return str(container)
    # __file__ is .../docker/core/tests/conftest.py — 4 parents up = repo root
    repo = Path(__file__).resolve().parent.parent.parent.parent / "config" / "personality.yaml"
    if repo.exists():
        return str(repo)
    pytest.skip("personality.yaml not found")


@pytest.fixture
def personality_config(personality_config_path):
    """Load and return the personality config dict."""
    from app.personality import reload_personality
    return reload_personality(personality_config_path)
