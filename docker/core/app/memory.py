"""Three-tier memory manager: Redis session, Qdrant episodic, PG factual."""

from __future__ import annotations

from typing import Any

from collections.abc import Callable
import asyncio
import logging
import os
from datetime import datetime as datetime

import httpx

from .dependencies import http_request
import redis.asyncio as redis

from .memory_facts import MemoryFactsMixin
from .memory_session import (
    RedisSessionMixin,
    SessionUnavailable as SessionUnavailable,
    SESSION_TTL as SESSION_TTL,
    MAX_SESSION_TURNS as MAX_SESSION_TURNS,
)
from .memory_exchange import ExchangeMemoryMixin
from .memory_settings import (
    EMBED_DIM as EMBED_DIM,
    MSG_COLLECTION_NAME as MSG_COLLECTION_NAME,
    MSG_RECALL_LIMIT as MSG_RECALL_LIMIT,
    MSG_MIN_SCORE as MSG_MIN_SCORE,
    RECENCY_WEIGHT as RECENCY_WEIGHT,
    QDRANT_URL as QDRANT_URL,
)

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://aichat-redis:6379/1")
INFERENCE_URL = os.environ.get("INFERENCE_URL", "http://aichat-inference:8105")
DATA_URL = os.environ.get("DATA_URL", "http://aichat-data:8091")

COLLECTION_NAME = "companion_episodes"


class EmbeddingFailed(RuntimeError):
    """Raised by MemoryManager.embed_text(raise_on_failure=True) when the
    embedding service can't be reached. Store paths should propagate this
    rather than indexing a zero vector that silently corrupts recall."""


class MemoryManager(MemoryFactsMixin, ExchangeMemoryMixin, RedisSessionMixin):
    """Manages all three tiers of companion memory."""

    def __init__(self) -> None:
        self._redis_reconnect_lock = asyncio.Lock()
        self._session_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._redis: redis.Redis | None = None
        self._http: httpx.AsyncClient | None = None
        self._collection_ready = False
        self._msg_collection_ready = False

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
        self._http = httpx.AsyncClient(timeout=30.0)
        await self._ensure_qdrant_collection()
        await self._ensure_msg_collection()

    async def close(self) -> None:
        try:
            if self._redis:
                await self._redis.aclose()
        finally:
            if self._http:
                await self._http.aclose()

    # ── Tier 2: Episodic Memory (Qdrant + embeddings) ────────────────────

    async def _ensure_qdrant_collection(self) -> None:
        if self._collection_ready:
            return
        try:
            r = await http_request(
                "qdrant",
                self._http,
                "get",
                f"{QDRANT_URL}/collections/{COLLECTION_NAME}",
            )
            if r.status_code == 200:
                self._collection_ready = True
                return
        except httpx.HTTPError:
            pass

        try:
            await http_request(
                "qdrant",
                self._http,
                "put",
                f"{QDRANT_URL}/collections/{COLLECTION_NAME}",
                json={
                    "vectors": {"size": EMBED_DIM, "distance": "Cosine"},
                },
            )
            self._collection_ready = True
            logger.info("Created Qdrant collection: %s", COLLECTION_NAME)
            # Create user_id index for filtered search performance
            try:
                await http_request(
                    "qdrant",
                    self._http,
                    "put",
                    f"{QDRANT_URL}/collections/{COLLECTION_NAME}/index",
                    json={"field_name": "user_id", "field_schema": "keyword"},
                )
            except Exception:
                pass  # Non-critical — search still works, just slower
        except httpx.HTTPError:
            logger.warning("Failed to create Qdrant collection, will retry later")

    async def embed_text(
        self, text: str, *, raise_on_failure: bool = False
    ) -> list[float]:
        """Get embedding vector for text. Cache-first.

        Args:
            raise_on_failure: when True (used by store paths) raises EmbeddingFailed
                instead of returning a zero vector. Search paths can pass False
                to keep the existing degrade-quietly behavior, but they should
                check is_zero_vector() and surface a UX signal to the user.
        """
        try:
            from . import caches

            cached = await caches.get_embedding(text)
            if cached:
                return cached
        except Exception:
            pass
        try:
            r = await http_request(
                "embedding",
                self._http,
                "post",
                f"{INFERENCE_URL}/v1/embeddings",
                json={"input": text, "model": "nomic-embed-text-v1.5"},
            )
            r.raise_for_status()
            vector = r.json()["data"][0]["embedding"]
            try:
                from . import caches

                await caches.put_embedding(text, vector)
            except Exception:
                pass
            if not isinstance(vector, list) or not all(
                isinstance(v, (int, float)) for v in vector
            ):
                raise ValueError("Invalid embedding vector")
            return [float(v) for v in vector]
        except Exception as e:
            logger.error("EMBEDDING FAILED — search quality degraded: %s", e)
            if raise_on_failure:
                raise EmbeddingFailed(str(e)) from e
            return [0.0] * EMBED_DIM

    @staticmethod
    def is_zero_vector(vec: list[float]) -> bool:
        """Detect the embed-failure sentinel so callers can surface a UX warning."""
        return all(v == 0.0 for v in vec)

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
        created_at = datetime.now().isoformat()

        # Qdrant vector storage
        try:
            vector = await self.embed_text(summary, raise_on_failure=True)
            r = await http_request(
                "qdrant",
                self._http,
                "put",
                f"{QDRANT_URL}/collections/{COLLECTION_NAME}/points",
                json={
                    "points": [
                        {
                            "id": episode_id,
                            "vector": vector,
                            "payload": {
                                "summary": summary,
                                "keywords": keywords,
                                "emotion_tags": emotion_tags,
                                "importance": importance,
                                "conversation_id": conversation_id,
                                "user_id": user_id,
                                "created_at": created_at,
                            },
                        }
                    ]
                },
            )
            # httpx does NOT raise on 4xx/5xx — without this a Qdrant error
            # status silently dropped the vector with no failure-path logging.
            r.raise_for_status()
        except Exception as e:
            logger.error("Failed to store episode %s in Qdrant: %s", episode_id[:8], e)

        # PostgreSQL fallback — ensures episodes survive Qdrant outages
        try:
            from .db import get_conn_autocommit

            async with get_conn_autocommit() as conn:
                await conn.execute(
                    "INSERT INTO companion_episodes "
                    "(id, conversation_id, summary, keywords, emotion_tags, importance, embedding_id, user_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        episode_id,
                        conversation_id,
                        summary,
                        keywords,
                        emotion_tags,
                        importance,
                        episode_id,
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
        min_score: float = 0.3,
        user_id: str = "jalsarraf",
    ) -> list[dict[str, Any]]:
        try:
            vector = await self.embed_text(query)
            if not vector or all(v == 0.0 for v in vector):
                # Embedding failed — searching Qdrant with an all-zero vector returns
                # meaningless (garbage-ranked) results. Return empty instead.
                logger.warning(
                    "Episode recall skipped — embedding failed (zero vector)"
                )
                return []
            r = await http_request(
                "qdrant",
                self._http,
                "post",
                f"{QDRANT_URL}/collections/{COLLECTION_NAME}/points/search",
                json={
                    "vector": vector,
                    "limit": limit,
                    "score_threshold": min_score,
                    "with_payload": True,
                    "filter": {
                        "must": [{"key": "user_id", "match": {"value": user_id}}]
                    },
                },
            )
            if r.status_code != 200:
                logger.warning("Qdrant search failed: %s", r.text)
                return []
            results = r.json().get("result", [])
            return [
                {
                    "summary": hit["payload"]["summary"],
                    "score": hit["score"],
                    "keywords": hit["payload"].get("keywords", []),
                    "emotion_tags": hit["payload"].get("emotion_tags", []),
                }
                for hit in results
            ]
        except Exception as e:
            # Fail open: this runs inside the live chat read path (WS message
            # handler). A Qdrant/embed hiccup must degrade context, never crash
            # the reply or tear down the socket.
            logger.warning("Episode recall failed: %s", e)
            return []

    # ── Conversation Exchange Memory (Qdrant — per-exchange vectors) ────

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
