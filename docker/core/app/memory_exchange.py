"""Conversation-exchange memory ops — extracted from app/memory.py for file-size hygiene.

S+ Phase 2 §6.1. These functions are attached as methods on MemoryManager at
import time (see attach_to() at the bottom). Caller sees no change.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import httpx  # noqa: F401  (used by callers via self._http typing)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Constants are imported lazily from app.memory to avoid circular import.
from app.memory import (  # noqa: E402
    EMBED_DIM,
    MSG_COLLECTION_NAME,
    MSG_MIN_SCORE,
    MSG_RECALL_LIMIT,
    QDRANT_URL,
    RECENCY_WEIGHT,
)

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


def attach_to(cls) -> None:
    """Bind these functions as methods on MemoryManager.

    Called once from app/memory.py after MemoryManager is defined."""
    cls._ensure_msg_collection = _ensure_msg_collection
    cls.store_exchange = store_exchange
    cls.recall_exchanges = recall_exchanges
    cls.recall_exchanges_with_recency = recall_exchanges_with_recency
