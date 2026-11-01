"""Typed conversation-exchange storage shared by the memory manager.

The mixin owns exchange persistence and ranking. It has no manager import,
instance patching, separate client or duplicated state.

Exchanges live in the companion_exchanges Postgres table only (owner
decision 2026-10-26, cloud AI only: no embeddings, no vector store). Recall
takes Postgres full-text candidates and lets Claude Haiku pick the relevant
ones (app/memory_recall.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import Any

from . import memory_recall
from .memory_settings import MSG_RECALL_LIMIT, RECENCY_WEIGHT

logger = logging.getLogger(__name__)

# Haiku reads this much of each side of a candidate exchange.
_SIDE_CHARS = 160
# Unparseable timestamps count as a month old.
_UNKNOWN_AGE_DAYS = 30.0

# Full-text match on both sides + topics (search_tsv, migration 260), then
# importance and recency. Rows that match nothing rank by the last two alone.
_EXCHANGES_RANKED_SQL = (
    "SELECT x.id::text, x.user_content, x.assistant_content, x.topics, x.mood, "
    "x.importance, x.created_at "
    "FROM companion_exchanges AS x, websearch_to_tsquery('english', %s) AS q "
    "WHERE x.user_id = %s "
    "ORDER BY ts_rank(x.search_tsv, q, 1) DESC, "
    "x.importance DESC NULLS LAST, x.created_at DESC "
    "LIMIT %s"
)
_EXCHANGES_RECENT_SQL = (
    "SELECT x.id::text, x.user_content, x.assistant_content, x.topics, x.mood, "
    "x.importance, x.created_at "
    "FROM companion_exchanges AS x "
    "WHERE x.user_id = %s "
    "ORDER BY x.importance DESC NULLS LAST, x.created_at DESC "
    "LIMIT %s"
)


# A turn counts as lost once its reply is an hour old: a queued extraction waits
# for a quiet gap, so a long conversation can hold it back that far. Its text
# matches on his side only, because a stored reply can differ from the logged
# one. Her own proactive lines are left out so one arriving mid-turn cannot pass
# for the reply. The reply's id makes a repeat run a no-op.
_LOST_TURNS_SQL = """
WITH turns AS (
    SELECT role, content, user_id, conversation_id, created_at,
           LEAD(role) OVER ordered AS reply_role,
           LEAD(id) OVER ordered AS reply_id,
           LEAD(content) OVER ordered AS reply,
           LEAD(mood) OVER ordered AS reply_mood,
           LEAD(created_at) OVER ordered AS reply_at
    FROM companion_messages
    WHERE content_type = 'text' AND created_at > NOW() - INTERVAL '30 days'
    WINDOW ordered AS (PARTITION BY conversation_id ORDER BY created_at, id)
)
SELECT t.reply_id::text, t.conversation_id::text, t.user_id,
       t.content, t.reply, t.reply_mood, t.reply_at
FROM turns AS t
WHERE t.role = 'user'
  AND t.reply_role = 'assistant'
  AND t.reply_at < NOW() - INTERVAL '1 hour'
  AND NOT EXISTS (
      SELECT 1 FROM companion_exchanges AS x
      WHERE x.user_id = t.user_id
        AND x.user_content = t.content
        AND x.created_at >= t.created_at
        AND x.created_at < t.created_at + INTERVAL '1 day'
  )
"""
_RESTORE_SQL = (
    "INSERT INTO companion_exchanges "
    "(id, conversation_id, user_content, assistant_content, topics, mood, "
    "importance, user_id, created_at) "
    "VALUES (%s, %s, %s, %s, '{}', %s, %s, %s, %s) "
    "ON CONFLICT (id) DO NOTHING"
)
# What extraction gives a turn it does not single out to remember.
_RESTORED_IMPORTANCE = 0.4


def _exchange_text(row: tuple[Any, ...]) -> str:
    """What Haiku reads for one exchange candidate."""
    _, user_content, assistant_content, topics, _, _, created_at = row
    day = (
        f"[{created_at.strftime('%Y-%m-%d')}] "
        if hasattr(created_at, "strftime")
        else ""
    )
    text = (
        f"{day}Commander: {memory_recall.clip(user_content or '', _SIDE_CHARS)} "
        f"| Klukai: {memory_recall.clip(assistant_content or '', _SIDE_CHARS)}"
    )
    words = ", ".join(str(t) for t in (topics or [])[:5])
    return text + (f" (topics: {words})" if words else "")


def _iso(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""


def _days_ago(created_at: object, now: datetime) -> float:
    """Age in days of an ISO timestamp; naive stamps are read as local time."""
    try:
        created = datetime.fromisoformat(str(created_at))
    except ValueError:
        return _UNKNOWN_AGE_DAYS
    if created.tzinfo is None:
        created = created.astimezone()
    return max(0.0, (now - created).total_seconds() / 86400)


class ExchangeMemoryMixin:
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
        """Insert a user+assistant exchange pair, scoped to user (insert-only).

        SACRED: never raises into the chat path; a database failure is logged.
        """
        try:
            from .db import get_conn_autocommit

            async with get_conn_autocommit() as conn:
                await conn.execute(
                    "INSERT INTO companion_exchanges "
                    "(id, conversation_id, user_content, assistant_content, "
                    "topics, mood, importance, user_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        exchange_id,
                        conversation_id,
                        user_content,
                        assistant_content,
                        topics,
                        mood,
                        importance,
                        user_id,
                    ),
                )
        except Exception as e:
            # Never let a memory write kill the chat path.
            logger.error("Failed to store exchange %s in DB: %s", exchange_id[:8], e)

    async def recall_exchanges(
        self,
        query: str,
        limit: int = MSG_RECALL_LIMIT,
        min_score: float = 0.0,
        user_id: str = "jalsarraf",
        *,
        pick: bool = True,
    ) -> list[dict[str, Any]]:
        """Past exchanges relevant to ``query``, most relevant first, scoped to user.

        Postgres candidates, then Claude Haiku's pick (memory_recall).
        ``score`` is rank-derived (first = 1.0). ``min_score`` is kept for
        callers and ignored: there are no similarity scores any more.
        """
        try:
            rows = await memory_recall.fetch_candidates(
                _EXCHANGES_RANKED_SQL, _EXCHANGES_RECENT_SQL, query, user_id
            )
            if not rows:
                return []
            picked = None
            if pick:
                picked = await memory_recall.select(
                    "memory_recall_exchanges",
                    "past exchanges between the Commander and Klukai",
                    query,
                    [_exchange_text(row) for row in rows],
                    limit,
                )
            chosen = memory_recall.ordered(rows, picked, limit)
            return [
                {
                    "user_content": row[1] or "",
                    "assistant_content": row[2] or "",
                    "topics": list(row[3] or []),
                    "mood": row[4] or "composed",
                    "score": memory_recall.rank_score(position, len(chosen)),
                    "created_at": _iso(row[6]),
                    # The stored importance feeds the affection-weighted re-rank
                    # in recall_exchanges_with_recency.
                    "importance": 0.5 if row[5] is None else float(row[5]),
                }
                for position, row in enumerate(chosen)
            ]
        except Exception as e:
            # Fail open: recall feeds the live chat read path; a backing-store
            # hiccup must degrade context, not drop the reply or kill the WS.
            logger.warning("Exchange recall failed: %s", e)
            return []

    async def recall_exchanges_with_recency(
        self,
        query: str,
        limit: int = MSG_RECALL_LIMIT,
        affection_level: int = 0,
        user_id: str = "jalsarraf",
        *,
        pick: bool = True,
    ) -> list[dict[str, Any]]:
        """Recall exchanges with recency-weighted re-ranking and affection bias."""
        exchanges = await self.recall_exchanges(
            query, limit=limit * 2, user_id=user_id, pick=pick
        )
        if not exchanges:
            return []

        now = datetime.now(UTC)
        for ex in exchanges:
            days_ago = _days_ago(ex.get("created_at"), now)
            recency_factor = 1.0 / (1.0 + days_ago)
            ex["final_score"] = (1 - RECENCY_WEIGHT) * ex.get(
                "score", 0.0
            ) + RECENCY_WEIGHT * recency_factor

            # Affection-weighted importance bias
            importance = ex.get("importance", 0.5)
            if affection_level >= 6:
                ex["final_score"] += importance * 0.2
            elif affection_level <= 2:
                ex["final_score"] += (1.0 - importance) * 0.1

        exchanges.sort(key=lambda x: x["final_score"], reverse=True)
        return exchanges[:limit]


async def restore_lost_exchanges() -> int:
    """Put back turns her exchange memory never got. Returns how many.

    The message log is written during the turn. The exchange comes later from
    a queue held in memory, so a restart, a full backlog, repeated preemption
    or a database blip can lose it. Insert-only: a stored row is never changed.
    """
    from .db import get_conn_autocommit
    from .helpers import is_trivial_message

    restored = 0
    async with get_conn_autocommit() as conn:
        rows = await (await conn.execute(_LOST_TURNS_SQL)).fetchall()
        for reply_id, conversation_id, user_id, said, reply, mood, at in rows:
            if is_trivial_message(said):
                continue  # extraction skipped these on purpose
            cursor = await conn.execute(
                _RESTORE_SQL,
                (
                    reply_id,
                    conversation_id,
                    said,
                    reply,
                    mood or "composed",
                    _RESTORED_IMPORTANCE,
                    user_id,
                    at,
                ),
            )
            restored += cursor.rowcount
    if restored:
        logger.info("Restored %d lost exchange(s) from the message log", restored)
    return restored


# Explicit unbound exports retain existing controlled-test injection seams.
store_exchange = ExchangeMemoryMixin.store_exchange
recall_exchanges = ExchangeMemoryMixin.recall_exchanges
recall_exchanges_with_recency = ExchangeMemoryMixin.recall_exchanges_with_recency
