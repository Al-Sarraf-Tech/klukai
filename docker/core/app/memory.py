"""Three-tier memory manager: Redis session, Postgres episodic, data-service facts.

Episodic and exchange memory live in Postgres only (owner decision
2026-10-26, cloud AI only). Recall takes Postgres full-text candidates and
lets Claude Haiku pick the relevant ones; see app/memory_recall.py.
"""

from __future__ import annotations

from typing import Any

from collections.abc import Callable
import asyncio
import logging
import os

# memory_facts.record_milestone reads the clock through this module.
from datetime import datetime as datetime

import httpx

import redis.asyncio as redis

from . import memory_recall
from .memory_facts import MemoryFactsMixin
from .memory_session import (
    RedisSessionMixin,
    SessionUnavailable as SessionUnavailable,
    SESSION_TTL as SESSION_TTL,
    MAX_SESSION_TURNS as MAX_SESSION_TURNS,
)
from .memory_exchange import ExchangeMemoryMixin
from .memory_settings import (
    MSG_RECALL_LIMIT as MSG_RECALL_LIMIT,
    RECENCY_WEIGHT as RECENCY_WEIGHT,
)

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://aichat-redis:6379/1")
DATA_URL = os.environ.get("DATA_URL", "http://aichat-data:8091")

# Full-text match on summary + keywords (search_tsv, migration 260), then
# importance and recency. Rows that match nothing rank by the last two alone.
_EPISODES_RANKED_SQL = (
    "SELECT e.id::text, e.summary, e.keywords, e.emotion_tags, e.importance, "
    "e.created_at "
    "FROM companion_episodes AS e, websearch_to_tsquery('english', %s) AS q "
    "WHERE e.user_id = %s "
    "ORDER BY ts_rank(e.search_tsv, q, 1) DESC, "
    "e.importance DESC NULLS LAST, e.created_at DESC "
    "LIMIT %s"
)
_EPISODES_RECENT_SQL = (
    "SELECT e.id::text, e.summary, e.keywords, e.emotion_tags, e.importance, "
    "e.created_at "
    "FROM companion_episodes AS e "
    "WHERE e.user_id = %s "
    "ORDER BY e.importance DESC NULLS LAST, e.created_at DESC "
    "LIMIT %s"
)


def _episode_text(row: tuple[Any, ...]) -> str:
    """What Haiku reads for one episode candidate."""
    _, summary, keywords, _, _, created_at = row
    day = (
        f"[{created_at.strftime('%Y-%m-%d')}] "
        if hasattr(created_at, "strftime")
        else ""
    )
    words = ", ".join(str(k) for k in (keywords or [])[:8])
    return f"{day}{summary or ''}" + (f" (keywords: {words})" if words else "")


class MemoryManager(MemoryFactsMixin, ExchangeMemoryMixin, RedisSessionMixin):
    """Manages all three tiers of companion memory."""

    def __init__(self) -> None:
        self._redis_reconnect_lock = asyncio.Lock()
        self._session_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._redis: redis.Redis | None = None
        self._http: httpx.AsyncClient | None = None

    async def init(self) -> None:
        # redis factory stubs omit annotations; preserve the established injection seam.
        factory: Callable[..., redis.Redis] = redis.from_url
        self._redis = factory(
            REDIS_URL,
            decode_responses=True,
            retry_on_timeout=True,
            socket_keepalive=True,
            health_check_interval=30,
        )
        # The data service (factual memory) client.
        self._http = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        try:
            if self._redis:
                await self._redis.aclose()
        finally:
            if self._http:
                await self._http.aclose()

    # ── Tier 2: Episodic Memory (Postgres) ──────────────────────────────

    async def store_episode(
        self,
        episode_id: str,
        summary: str,
        keywords: list[str],
        emotion_tags: list[str],
        importance: float,
        conversation_id: str | None = None,
        user_id: str = "jalsarraf",
    ) -> str:
        """Insert the episode row (insert-only). Never raises; returns the id."""
        try:
            from .db import get_conn_autocommit

            async with get_conn_autocommit() as conn:
                await conn.execute(
                    "INSERT INTO companion_episodes "
                    "(id, conversation_id, summary, keywords, emotion_tags, importance, user_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        episode_id,
                        conversation_id,
                        summary,
                        keywords,
                        emotion_tags,
                        importance,
                        user_id,
                    ),
                )
        except Exception as e:
            logger.error("Failed to store episode %s in DB: %s", episode_id[:8], e)

        return episode_id

    async def recall_episodes(
        self,
        query: str,
        limit: int = 5,
        min_score: float = 0.0,
        user_id: str = "jalsarraf",
    ) -> list[dict[str, Any]]:
        """The user's episodes relevant to ``query``, most relevant first.

        Postgres candidates, then Claude Haiku's pick (memory_recall).
        ``score`` is rank-derived (first = 1.0). ``min_score`` is kept for
        callers and ignored: there are no similarity scores any more.
        """
        try:
            rows = await memory_recall.fetch_candidates(
                _EPISODES_RANKED_SQL, _EPISODES_RECENT_SQL, query, user_id
            )
            if not rows:
                return []
            picked = await memory_recall.select(
                "memory_recall_episodes",
                "summaries of past conversations between the Commander and Klukai",
                query,
                [_episode_text(row) for row in rows],
                limit,
            )
            chosen = memory_recall.ordered(rows, picked, limit)
            return [
                {
                    "summary": row[1] or "",
                    "score": memory_recall.rank_score(position, len(chosen)),
                    "keywords": list(row[2] or []),
                    "emotion_tags": list(row[3] or []),
                }
                for position, row in enumerate(chosen)
            ]
        except Exception as e:
            # Fail open: this runs inside the live chat read path (WS message
            # handler). A database hiccup must degrade context, never crash
            # the reply or tear down the socket.
            logger.warning("Episode recall failed: %s", e)
            return []

    # ── Conversation Exchange Memory (Postgres; memory_exchange) ────────

    # ── Tier 3: Factual Memory (aichat-data /memory) ────────────────────

    # ── Combined recall for prompt building ──────────────────────────────

    async def recall_for_prompt(
        self,
        query: str,
        user_id: str = "jalsarraf",
        affection_level: int = 0,
    ) -> tuple[list[str], dict[str, Any], list[dict[str, Any]]]:
        """Return (episodic_memories, relationship_facts, recalled_exchanges) — parallel fetch, scoped to user.

        affection_level is threaded into exchange re-ranking so high-affection
        chat prefers should_remember exchanges (and low-affection de-emphasizes
        them). Callers that omit it keep the historical default of 0.
        """
        import asyncio

        episodes_task = self.recall_episodes(query, limit=5, user_id=user_id)
        facts_task = self.get_relationship_facts(user_id=user_id)
        exchanges_task = self.recall_exchanges_with_recency(
            query,
            limit=MSG_RECALL_LIMIT,
            affection_level=affection_level,
            user_id=user_id,
        )

        episodes, facts, exchanges = await asyncio.gather(
            episodes_task,
            facts_task,
            exchanges_task,
            return_exceptions=True,
        )
        # Each sub-call already fails open; return_exceptions is belt-and-suspenders
        # so an unexpected error in one path can never propagate out of the chat
        # read path and drop the reply/WS.
        if isinstance(episodes, BaseException):
            logger.warning("recall_for_prompt: episodes failed: %s", episodes)
            episodes = []
        if isinstance(facts, BaseException):
            logger.warning("recall_for_prompt: facts failed: %s", facts)
            facts = {}
        if isinstance(exchanges, BaseException):
            logger.warning("recall_for_prompt: exchanges failed: %s", exchanges)
            exchanges = []
        episode_texts = [ep["summary"] for ep in episodes]
        return episode_texts, facts, exchanges

    async def get_memory_nudge(
        self,
        turn_count: int,
        affection_level: int,
        user_id: str = "jalsarraf",
    ) -> str | None:
        """Return a memory nudge string if it's time for one, else None. Scoped to user."""
        if affection_level <= 2:
            return None

        if affection_level <= 4:
            interval = 5
        elif affection_level <= 6:
            interval = 4
        else:
            interval = 3

        if turn_count % interval != 0 or turn_count == 0:
            return None

        import random

        prompts = [
            "something personal",
            "a shared memory",
            "something important",
            "a past conversation",
            "what the Commander told me",
        ]
        query = random.choice(prompts)
        exchanges = await self.recall_exchanges_with_recency(
            query,
            limit=3,
            affection_level=affection_level,
            user_id=user_id,
        )
        if not exchanges:
            return None

        ex = random.choice(exchanges)
        user_snip = ex["user_content"][:200]
        asst_snip = ex["assistant_content"][:200]
        topics = ", ".join(ex.get("topics", [])[:3]) or "a past conversation"

        return (
            f'[Memory: You once discussed "{topics}". '
            f'The Commander said: "{user_snip}". '
            f'You replied: "{asst_snip}".]'
        )
