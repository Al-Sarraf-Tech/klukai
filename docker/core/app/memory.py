"""Three-tier memory manager: Redis session, Qdrant episodic, PG factual."""

from __future__ import annotations

import json
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

SESSION_TTL = 4 * 3600  # 4 hours
MAX_SESSION_TURNS = 20
COLLECTION_NAME = "companion_episodes"
EMBED_DIM = 768


class MemoryManager:
    """Manages all three tiers of companion memory."""

    def __init__(self) -> None:
        self._redis: redis.Redis | None = None
        self._http: httpx.AsyncClient | None = None
        self._collection_ready = False

    async def init(self) -> None:
        self._redis = redis.from_url(REDIS_URL, decode_responses=True)
        self._http = httpx.AsyncClient(timeout=30.0)
        await self._ensure_qdrant_collection()

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
        if self._http:
            await self._http.aclose()

    # ── Tier 1: Session (Redis) ──────────────────────────────────────────

    async def get_session(self, session_id: str) -> SessionState | None:
        raw = await self._redis.get(f"companion:session:{session_id}")
        if raw is None:
            return None
        return SessionState.model_validate_json(raw)

    async def save_session(self, session_id: str, state: SessionState) -> None:
        # Keep turns bounded
        if len(state.turns) > MAX_SESSION_TURNS:
            state.turns = state.turns[-MAX_SESSION_TURNS:]
        state.last_activity = datetime.now()
        await self._redis.set(
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
        except httpx.HTTPError:
            logger.warning("Failed to create Qdrant collection, will retry later")

    async def embed_text(self, text: str) -> list[float]:
        r = await self._http.post(
            f"{INFERENCE_URL}/v1/embeddings",
            json={"input": text, "model": "nomic-embed-text-v1.5"},
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]

    async def store_episode(
        self,
        episode_id: str,
        summary: str,
        keywords: list[str],
        emotion_tags: list[str],
        importance: float,
        conversation_id: str | None = None,
    ) -> str:
        vector = await self.embed_text(summary)

        # Store in Qdrant
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
                            "created_at": datetime.now().isoformat(),
                        },
                    }
                ]
            },
        )
        return episode_id

    async def recall_episodes(
        self, query: str, limit: int = 5, min_score: float = 0.3
    ) -> list[dict]:
        vector = await self.embed_text(query)
        r = await self._http.post(
            f"{QDRANT_URL}/collections/{COLLECTION_NAME}/points/search",
            json={
                "vector": vector,
                "limit": limit,
                "score_threshold": min_score,
                "with_payload": True,
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

    # ── Tier 3: Factual Memory (aichat-data /memory) ────────────────────

    async def store_fact(self, key: str, value: str, ttl: int | None = None) -> None:
        body: dict = {"key": f"companion:{key}", "value": value}
        if ttl:
            body["ttl_seconds"] = ttl
        await self._http.post(f"{DATA_URL}/memory/store", json=body)

    async def recall_fact(self, key: str) -> str | None:
        r = await self._http.get(
            f"{DATA_URL}/memory/recall", params={"key": f"companion:{key}"}
        )
        data = r.json()
        if data.get("found"):
            return data.get("value")
        return None

    async def recall_facts_by_pattern(self, pattern: str) -> list[dict]:
        r = await self._http.get(
            f"{DATA_URL}/memory/recall",
            params={"pattern": f"companion:{pattern}"},
        )
        data = r.json()
        return data.get("entries", [])

    # ── Relationship facts (convenience) ─────────────────────────────────

    async def get_relationship_facts(self) -> dict:
        entries = await self.recall_facts_by_pattern("rel:%")
        return {
            e["key"].replace("companion:rel:", ""): e["value"]
            for e in entries
        }

    async def set_relationship_fact(self, key: str, value: str) -> None:
        await self.store_fact(f"rel:{key}", value)

    # ── Combined recall for prompt building ──────────────────────────────

    async def recall_for_prompt(self, query: str) -> tuple[list[str], dict]:
        """Return (episodic_memories, relationship_facts) for prompt assembly."""
        episodes = await self.recall_episodes(query, limit=5)
        episode_texts = [ep["summary"] for ep in episodes]
        facts = await self.get_relationship_facts()
        return episode_texts, facts
