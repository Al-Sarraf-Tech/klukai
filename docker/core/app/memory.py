"""Three-tier memory manager: Redis session, Qdrant episodic, PG factual."""

from __future__ import annotations

import logging
import os
from datetime import datetime

import httpx
import redis.asyncio as redis

from .models import SessionState

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://aichat-redis:6379/1")
INFERENCE_URL = os.environ.get("INFERENCE_URL", "http://aichat-inference:8105")
DATA_URL = os.environ.get("DATA_URL", "http://aichat-data:8091")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://aichat-vector:6333")

SESSION_TTL = 24 * 3600  # 24 hours — mood persists to DB as backup
MAX_SESSION_TURNS = 16
COLLECTION_NAME = "companion_episodes"
MSG_COLLECTION_NAME = "companion_exchanges"
EMBED_DIM = 768
MSG_RECALL_LIMIT = 5
MSG_MIN_SCORE = 0.35
RECENCY_WEIGHT = 0.15


class MemoryManager:
    """Manages all three tiers of companion memory."""

    def __init__(self) -> None:
        self._redis: redis.Redis | None = None
        self._http: httpx.AsyncClient | None = None
        self._collection_ready = False
        self._msg_collection_ready = False

    async def init(self) -> None:
        self._redis = redis.from_url(
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
        if self._redis:
            await self._redis.aclose()
        if self._http:
            await self._http.aclose()

    async def _redis_op(self, op, *args, **kwargs):
        """Execute a Redis operation with auto-reconnect on failure."""
        try:
            return await op(*args, **kwargs)
        except (redis.ConnectionError, redis.TimeoutError) as e:
            logger.warning("Redis connection error: %s — reconnecting", e)
            try:
                self._redis = redis.from_url(
                    REDIS_URL, decode_responses=True,
                    retry_on_timeout=True, socket_keepalive=True,
                    health_check_interval=30,
                )
                # Re-bind operation to the new client
                new_op = getattr(self._redis, op.__name__)
                return await new_op(*args, **kwargs)
            except Exception as e2:
                logger.error("Redis reconnect failed: %s", e2)
                return None

    # ── Tier 1: Session (Redis) ──────────────────────────────────────────

    async def get_session(self, session_id: str) -> SessionState | None:
        raw = await self._redis_op(self._redis.get, f"companion:session:{session_id}")
        if raw is None:
            return None
        try:
            return SessionState.model_validate_json(raw)
        except Exception as e:
            logger.warning("Failed to parse session: %s", e)
            return None

    async def save_session(self, session_id: str, state: SessionState) -> None:
        if len(state.turns) > MAX_SESSION_TURNS:
            state.turns = state.turns[-MAX_SESSION_TURNS:]
        state.last_activity = datetime.now()
        await self._redis_op(
            self._redis.set,
            f"companion:session:{session_id}",
            state.model_dump_json(),
            ex=SESSION_TTL,
        )

    async def add_turn(
        self, session_id: str, role: str, content: str, state: SessionState
    ) -> SessionState:
        state.turns.append({"role": role, "content": content})
        state.turn_count += 1
        await self.save_session(session_id, state)
        return state

    # ── Tier 2: Episodic Memory (Qdrant + embeddings) ────────────────────

    async def _ensure_qdrant_collection(self) -> None:
        if self._collection_ready:
            return
        try:
            r = await self._http.get(
                f"{QDRANT_URL}/collections/{COLLECTION_NAME}"
            )
            if r.status_code == 200:
                self._collection_ready = True
                return
        except httpx.HTTPError:
            pass

        try:
            await self._http.put(
                f"{QDRANT_URL}/collections/{COLLECTION_NAME}",
                json={
                    "vectors": {"size": EMBED_DIM, "distance": "Cosine"},
                },
            )
            self._collection_ready = True
            logger.info("Created Qdrant collection: %s", COLLECTION_NAME)
            # Create user_id index for filtered search performance
            try:
                await self._http.put(
                    f"{QDRANT_URL}/collections/{COLLECTION_NAME}/index",
                    json={"field_name": "user_id", "field_schema": "keyword"},
                )
            except Exception:
                pass  # Non-critical — search still works, just slower
        except httpx.HTTPError:
            logger.warning("Failed to create Qdrant collection, will retry later")

    async def embed_text(self, text: str) -> list[float]:
        # Cache-first path — same text yields same vector
        try:
            from . import caches
            cached = await caches.get_embedding(text)
            if cached:
                return cached
        except Exception:
            pass
        try:
            r = await self._http.post(
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
            return vector
        except Exception as e:
            logger.error("EMBEDDING FAILED — search quality degraded: %s", e)
            return [0.0] * EMBED_DIM

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
            vector = await self.embed_text(summary)
            await self._http.put(
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
                    (episode_id, conversation_id, summary, keywords,
                     emotion_tags, importance, episode_id, user_id),
                )
        except Exception as e:
            logger.error("Failed to store episode %s in DB: %s", episode_id[:8], e)

        return episode_id

    async def recall_episodes(
        self, query: str, limit: int = 5, min_score: float = 0.3,
        user_id: str = "jalsarraf",
    ) -> list[dict]:
        vector = await self.embed_text(query)
        r = await self._http.post(
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

    # ── Conversation Exchange Memory (Qdrant — per-exchange vectors) ────

    async def _ensure_msg_collection(self) -> None:
        """Create the companion_exchanges Qdrant collection if needed."""
        if self._msg_collection_ready:
            return
        try:
            r = await self._http.get(
                f"{QDRANT_URL}/collections/{MSG_COLLECTION_NAME}"
            )
            if r.status_code == 200:
                self._msg_collection_ready = True
                return
        except httpx.HTTPError:
            pass

        try:
            await self._http.put(
                f"{QDRANT_URL}/collections/{MSG_COLLECTION_NAME}",
                json={
                    "vectors": {"size": EMBED_DIM, "distance": "Cosine"},
                },
            )
            # Create keyword indexes for filtered search
            await self._http.put(
                f"{QDRANT_URL}/collections/{MSG_COLLECTION_NAME}/index",
                json={"field_name": "topics", "field_schema": "keyword"},
            )
            try:
                await self._http.put(
                    f"{QDRANT_URL}/collections/{MSG_COLLECTION_NAME}/index",
                    json={"field_name": "user_id", "field_schema": "keyword"},
                )
            except Exception:
                pass  # Non-critical
            self._msg_collection_ready = True
            logger.info("Created Qdrant collection: %s", MSG_COLLECTION_NAME)
        except httpx.HTTPError:
            logger.warning("Failed to create msg collection, will retry later")

    async def store_exchange(
        self,
        exchange_id: str,
        user_content: str,
        assistant_content: str,
        topics: list[str],
        mood: str = "composed",
        importance: float = 0.5,
        conversation_id: str | None = None,
        user_id: str = "jalsarraf",
    ) -> None:
        """Store a user+assistant exchange pair with vector embedding, scoped to user."""
        try:
            combined = f"Commander: {user_content[:500]}\nKlukai: {assistant_content[:500]}"
            vector = await self.embed_text(combined)
            await self._http.put(
                f"{QDRANT_URL}/collections/{MSG_COLLECTION_NAME}/points",
                json={
                    "points": [
                        {
                            "id": exchange_id,
                            "vector": vector,
                            "payload": {
                                "user_content": user_content,
                                "assistant_content": assistant_content,
                                "topics": topics,
                                "mood": mood,
                                "importance": importance,
                                "conversation_id": conversation_id,
                                "user_id": user_id,
                                "created_at": datetime.now().isoformat(),
                            },
                        }
                    ]
                },
            )
        except Exception as e:
            logger.warning("Failed to store exchange %s: %s", exchange_id[:8], e)

    async def recall_exchanges(
        self, query: str, limit: int = MSG_RECALL_LIMIT, min_score: float = MSG_MIN_SCORE,
        user_id: str = "jalsarraf",
    ) -> list[dict]:
        """Semantic search over past conversation exchanges, scoped to user."""
        vector = await self.embed_text(query)
        r = await self._http.post(
            f"{QDRANT_URL}/collections/{MSG_COLLECTION_NAME}/points/search",
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
            logger.warning("Exchange recall failed: %s", r.text)
            return []
        results = r.json().get("result", [])
        return [
            {
                "user_content": hit["payload"]["user_content"],
                "assistant_content": hit["payload"]["assistant_content"],
                "topics": hit["payload"].get("topics", []),
                "mood": hit["payload"].get("mood", "composed"),
                "score": hit["score"],
                "created_at": hit["payload"].get("created_at", ""),
            }
            for hit in results
        ]

    async def recall_exchanges_with_recency(
        self, query: str, limit: int = MSG_RECALL_LIMIT, affection_level: int = 0,
        user_id: str = "jalsarraf",
    ) -> list[dict]:
        """Recall exchanges with recency-weighted re-ranking and affection bias."""
        exchanges = await self.recall_exchanges(query, limit=limit * 2, user_id=user_id)
        if not exchanges:
            return []

        now = datetime.now()
        for ex in exchanges:
            try:
                created = datetime.fromisoformat(ex["created_at"])
                days_ago = max(0, (now - created).total_seconds() / 86400)
            except (ValueError, KeyError):
                days_ago = 30

            recency_factor = 1.0 / (1.0 + days_ago)
            ex["final_score"] = (
                (1 - RECENCY_WEIGHT) * ex["score"]
                + RECENCY_WEIGHT * recency_factor
            )

            # Affection-weighted importance bias
            importance = ex.get("importance", 0.5) if isinstance(ex, dict) else 0.5
            if affection_level >= 6:
                ex["final_score"] += importance * 0.2
            elif affection_level <= 2:
                ex["final_score"] += (1.0 - importance) * 0.1

        exchanges.sort(key=lambda x: x["final_score"], reverse=True)
        return exchanges[:limit]

    # ── Tier 3: Factual Memory (aichat-data /memory) ────────────────────

    async def store_fact(self, key: str, value: str, ttl: int | None = None,
                         user_id: str = "jalsarraf") -> None:
        try:
            body: dict = {"key": f"companion:{user_id}:{key}", "value": value}
            if ttl:
                body["ttl_seconds"] = ttl
            await self._http.post(f"{DATA_URL}/memory/store", json=body)
        except Exception as e:
            logger.warning("store_fact failed for %s: %s", key, e)

    async def recall_fact(self, key: str, user_id: str = "jalsarraf") -> str | None:
        try:
            r = await self._http.get(
                f"{DATA_URL}/memory/recall", params={"key": f"companion:{user_id}:{key}"}
            )
            data = r.json()
            if data.get("found"):
                return data.get("value")
        except Exception as e:
            logger.warning("recall_fact failed for %s: %s", key, e)
        return None

    async def recall_facts_by_pattern(self, pattern: str,
                                       user_id: str = "jalsarraf") -> list[dict]:
        r = await self._http.get(
            f"{DATA_URL}/memory/recall",
            params={"pattern": f"companion:{user_id}:{pattern}"},
        )
        data = r.json()
        return data.get("entries", [])

    # ── Relationship facts (convenience) ─────────────────────────────────

    async def get_relationship_facts(self, user_id: str = "jalsarraf") -> dict:
        entries = await self.recall_facts_by_pattern("rel:%", user_id=user_id)
        # Strip the full prefix: companion:{user_id}:rel:
        prefix = f"companion:{user_id}:rel:"
        return {
            e["key"].replace(prefix, ""): e["value"]
            for e in entries
        }

    async def set_relationship_fact(self, key: str, value: str,
                                     user_id: str = "jalsarraf") -> None:
        await self.store_fact(f"rel:{key}", value, user_id=user_id)

    # ── Milestone tracking ────────────────────────────────────────────────

    async def record_milestone(self, milestone: str, user_id: str = "jalsarraf") -> bool:
        """Record a relationship milestone. Returns True if new."""
        existing = await self.recall_fact(f"milestone:{milestone}", user_id=user_id)
        if existing:
            return False
        await self.store_fact(f"milestone:{milestone}", datetime.now().isoformat(), user_id=user_id)
        logger.info("New milestone recorded: %s for %s", milestone, user_id)
        return True

    async def get_milestones(self, user_id: str = "jalsarraf") -> dict[str, str]:
        """Get all recorded relationship milestones for a user."""
        entries = await self.recall_facts_by_pattern("milestone:%", user_id=user_id)
        prefix = f"companion:{user_id}:milestone:"
        return {
            e["key"].replace(prefix, ""): e["value"]
            for e in entries
        }

    # ── Combined recall for prompt building ──────────────────────────────

    async def recall_for_prompt(
        self, query: str, user_id: str = "jalsarraf",
    ) -> tuple[list[str], dict, list[dict]]:
        """Return (episodic_memories, relationship_facts, recalled_exchanges) — parallel fetch, scoped to user."""
        import asyncio
        episodes_task = self.recall_episodes(query, limit=5, user_id=user_id)
        facts_task = self.get_relationship_facts(user_id=user_id)
        exchanges_task = self.recall_exchanges_with_recency(
            query, limit=MSG_RECALL_LIMIT, user_id=user_id
        )

        episodes, facts, exchanges = await asyncio.gather(
            episodes_task, facts_task, exchanges_task
        )
        episode_texts = [ep["summary"] for ep in episodes]
        return episode_texts, facts, exchanges

    async def get_memory_nudge(
        self, turn_count: int, affection_level: int, user_id: str = "jalsarraf",
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
        prompts = ["something personal", "a shared memory", "something important",
                   "a past conversation", "what the Commander told me"]
        query = random.choice(prompts)
        exchanges = await self.recall_exchanges_with_recency(
            query, limit=3, affection_level=affection_level, user_id=user_id,
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
