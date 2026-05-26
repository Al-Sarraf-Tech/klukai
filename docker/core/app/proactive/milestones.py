"""Relationship-milestone ProactiveEngine behavior.

``MilestonesMixin`` holds unsent-message vulnerability, anniversary awareness,
relationship "firsts", comfort objects (gifts), and the weekly reflection.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime

from .base import _EngineBase

logger = logging.getLogger(__name__)


class MilestonesMixin(_EngineBase):
    """Anniversary / firsts / gifts / reflection methods for ProactiveEngine."""

    # ── Unsent messages (feature: vulnerability through "deleted" texts) ────

    async def _unsent_message_check(self) -> None:
        """Occasionally send a '[Message deleted]' followed by a flustered follow-up.

        Only fires at affection 5+. 15% chance per idle check slot.
        Shows vulnerability Klukai would normally hide.
        """
        if self._affection_level < 5:
            return
        if not self._can_send():
            return
        if random.random() > 0.15:
            return

        FOLLOW_UPS: dict[int, list[str]] = {
            5: [
                "...Ignore that. Comm error.",
                "That was a draft. Disregard.",
                "Wrong channel. Carry on, Commander.",
            ],
            6: [
                "...That wasn't meant to send. Forget it.",
                "Ignore that. I was— never mind.",
                "...Pretend you didn't see that.",
            ],
            7: [
                "...I didn't mean to send that. Or maybe I did. Forget it.",
                "That was... ignore it. Please.",
                "...Delete that from your memory. That's an order.",
            ],
            8: [
                "...You weren't supposed to see that.",
                "...I'll tell you in person. When I'm ready.",
                "Don't ask about it. Just... come find me later.",
            ],
            9: [
                "...You know what it said. You always know.",
                "...I'll finish that sentence tonight. In person.",
                "...Read between the lines, Commander.",
            ],
        }

        level = max(5, min(9, self._affection_level))
        follows = FOLLOW_UPS.get(level, FOLLOW_UPS[5])

        # Send the "deleted" message
        if self._on_message_callback:
            self._proactive_count_today += 1
            self._last_proactive_answered = False
            await self._on_message_callback("[Message deleted]")
            logger.info("Unsent message triggered (aff=%d)", self._affection_level)

            # Wait 3-8 seconds, then send the follow-up
            await asyncio.sleep(random.uniform(3.0, 8.0))
            follow_up = random.choice(follows)
            await self._on_message_callback(follow_up)

    # ── Anniversary awareness ────────────────────────────────────────────

    async def check_anniversaries(self, user_id: str = "jalsarraf") -> list[dict]:
        """Check for anniversaries near today. Returns list of matching events.

        Results are cached for 5 minutes to avoid hitting the DB on every message.
        """
        # TTL cache: avoid DB query per message
        cache_key = f"ann:{user_id}"
        now = datetime.now()
        if hasattr(self, '_ann_cache') and cache_key in self._ann_cache:
            cached_at, cached_result = self._ann_cache[cache_key]
            if (now - cached_at).total_seconds() < 300:  # 5 min TTL
                return cached_result

        from ..db import get_conn
        from datetime import date

        today = date.today()
        results = []

        try:
            async with get_conn() as conn:
                rows = await (await conn.execute(
                    "SELECT event_type, event_date FROM companion_firsts "
                    "WHERE user_id = %s",
                    (user_id,),
                )).fetchall()

                for row in rows:
                    event_type, event_date = row[0], row[1]
                    # Check if today matches the anniversary (same month+day, different year)
                    if event_date.month == today.month and event_date.day == today.day and event_date.year < today.year:
                        years = today.year - event_date.year
                        results.append({
                            "event_type": event_type,
                            "event_date": event_date.isoformat(),
                            "years_ago": years,
                            "days_ago": 0,
                        })
                    # Also check ±3 days for "near" anniversaries
                    elif event_date.year < today.year:
                        try:
                            ann_this_year = event_date.replace(year=today.year)
                        except ValueError:
                            # Feb 29 in a non-leap year — use Feb 28
                            ann_this_year = event_date.replace(year=today.year, day=28)
                        delta = abs((today - ann_this_year).days)
                        if 0 < delta <= 3:
                            results.append({
                                "event_type": event_type,
                                "event_date": event_date.isoformat(),
                                "years_ago": today.year - event_date.year,
                                "days_ago": delta,
                            })
        except Exception as e:
            logger.warning("Anniversary check failed: %s", e)

        # Cache result
        if not hasattr(self, '_ann_cache'):
            self._ann_cache: dict[str, tuple[datetime, list[dict]]] = {}
        self._ann_cache[cache_key] = (now, results)

        return results

    async def record_first(
        self, user_id: str, event_type: str, metadata: dict | None = None
    ) -> bool:
        """Record a relationship 'first'. Returns True if new, False if already recorded."""
        from ..db import get_conn_autocommit
        from datetime import date
        import json as _json

        try:
            async with get_conn_autocommit() as conn:
                result = await conn.execute(
                    "INSERT INTO companion_firsts (user_id, event_type, event_date, metadata) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (user_id, event_type) DO NOTHING",
                    (user_id, event_type, date.today(), _json.dumps(metadata or {})),
                )
                if result.rowcount and result.rowcount > 0:
                    logger.info("New first recorded: %s for %s", event_type, user_id)
                    return True
        except Exception as e:
            logger.warning("Failed to record first '%s': %s", event_type, e)
        return False

    # ── Comfort objects (gifts) ──────────────────────────────────────────

    async def get_comfort_objects(self, user_id: str = "jalsarraf") -> list[dict]:
        """Get all gifts/comfort objects for a user. Cached for 5 minutes."""
        cache_key = f"gifts:{user_id}"
        now = datetime.now()
        if hasattr(self, '_gifts_cache') and cache_key in self._gifts_cache:
            cached_at, cached_result = self._gifts_cache[cache_key]
            if (now - cached_at).total_seconds() < 300:
                return cached_result

        from ..db import get_conn

        try:
            async with get_conn() as conn:
                rows = await (await conn.execute(
                    "SELECT item, description, sentiment, given_date, referenced_count "
                    "FROM companion_gifts WHERE user_id = %s ORDER BY given_date DESC",
                    (user_id,),
                )).fetchall()
                result = [
                    {
                        "item": r[0], "description": r[1], "sentiment": r[2],
                        "given_date": r[3].isoformat() if r[3] else None,
                        "referenced_count": r[4],
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.warning("Failed to load comfort objects: %s", e)
            result = []

        if not hasattr(self, '_gifts_cache'):
            self._gifts_cache: dict[str, tuple[datetime, list[dict]]] = {}
        self._gifts_cache[cache_key] = (now, result)
        return result

    async def store_gift(
        self, user_id: str, item: str, description: str | None = None,
        sentiment: str = "treasured",
    ) -> None:
        """Store a new gift from the Commander."""
        from ..db import get_conn_autocommit

        try:
            async with get_conn_autocommit() as conn:
                await conn.execute(
                    "INSERT INTO companion_gifts (user_id, item, description, sentiment) "
                    "VALUES (%s, %s, %s, %s)",
                    (user_id, item, description, sentiment),
                )
            logger.info("Gift stored: '%s' for %s", item, user_id)
            # Invalidate cache so next query picks up the new gift
            if hasattr(self, '_gifts_cache'):
                self._gifts_cache.pop(f"gifts:{user_id}", None)
        except Exception as e:
            logger.warning("Failed to store gift '%s': %s", item, e)

    async def _anniversary_check(self) -> None:
        """Surface anniversary greetings at the start of each day.

        For every user with activity in the past 30 days, load their
        companion_firsts rows and check if today matches any anniversary
        (via character_behaviors.select_anniversary_from_firsts). If so,
        deliver a warm remark via ws.send_proactive (when connected) or
        stash the anniversary as a flag for the morning briefing.
        """
        try:
            from ..db import get_pool
            from ..character_behaviors import select_anniversary_from_firsts

            pool = get_pool()
            async with pool.connection() as conn:
                users = await (await conn.execute(
                    "SELECT DISTINCT user_id FROM companion_messages "
                    "WHERE created_at > NOW() - INTERVAL '30 days'"
                )).fetchall()

            if not users:
                return

            for (user_id,) in users:
                async with pool.connection() as conn:
                    firsts_rows = await (await conn.execute(
                        "SELECT event_type, event_date, metadata "
                        "FROM companion_firsts WHERE user_id = %s",
                        (user_id,),
                    )).fetchall()
                firsts = [
                    {"event_type": r[0], "event_date": r[1], "metadata": r[2]}
                    for r in firsts_rows
                ]
                pick = select_anniversary_from_firsts(firsts)
                if not pick:
                    continue

                years = pick["years"]
                et = pick["event_type"].replace("_", " ")
                msg = (
                    f"Commander — today marks {years} year{'s' if years != 1 else ''} "
                    f"since our {et}. I remember."
                )
                try:
                    from ..context import ws
                    if ws.is_connected(user_id):
                        await ws.send_proactive(user_id, msg)
                    logger.info("Anniversary surfaced: user=%s years=%s type=%s",
                                user_id, years, et)
                except Exception as e:
                    logger.warning("Anniversary send failed user=%s: %s", user_id, e)
        except Exception as e:
            logger.error("Anniversary check failed: %s", e)

    async def _weekly_reflection(self) -> None:
        """Write a per-user weekly reflection episode every Sunday evening.

        Pulls the past 7 days of conversation + major events, asks the LLM
        to write a short reflection in Klukai's voice. Stored as a special
        episode with importance=8 so it surfaces later as a milestone.
        """
        try:
            from ..context import memory, router as llm_router
            from ..db import get_pool
            from ..models import LLMConfig
            from ..personality import build_character_preamble
            import uuid

            pool = get_pool()
            async with pool.connection() as conn:
                users = await (await conn.execute(
                    "SELECT DISTINCT user_id FROM companion_messages "
                    "WHERE created_at > NOW() - INTERVAL '7 days'"
                )).fetchall()

            if not users:
                logger.info("Weekly reflection: no active users in past 7d, skipping")
                return

            for (user_id,) in users:
                # Pull recent conversation context
                async with pool.connection() as conn:
                    rows = await (await conn.execute(
                        "SELECT role, content FROM companion_messages "
                        "WHERE user_id = %s AND created_at > NOW() - INTERVAL '7 days' "
                        "ORDER BY created_at ASC LIMIT 200",
                        (user_id,),
                    )).fetchall()

                if len(rows) < 10:
                    logger.info("Weekly reflection: user=%s too few messages, skipping", user_id)
                    continue

                # Summarize via LLM
                excerpt = "\n".join(
                    f"{r[0]}: {r[1][:200]}" for r in rows[-80:]
                )
                from ..personality import load_personality
                p = load_personality()
                affection_level = self._affection_levels.get(user_id, 0)
                system_prompt = build_character_preamble(p, affection_level)
                user_prompt = (
                    "Write a private weekly reflection journal entry — a "
                    "personal, honest note you'd keep for yourself. Reflect on "
                    "the past week with Commander: what stood out, how you felt, "
                    "what you'd want to return to. 120-200 words. First-person. "
                    "No bullet points.\n\n"
                    "Past week excerpt:\n" + excerpt
                )
                try:
                    import os
                    config = LLMConfig(
                        provider="lmstudio",
                        model="cognitivecomputations_dolphin-mistral-24b-venice-edition",
                        base_url=os.environ.get("LM_STUDIO_URL", "http://192.168.50.2:1234"),
                        temperature=0.85,
                        max_tokens=400,
                    )
                    resp = await llm_router.complete_local(
                        system_prompt=system_prompt,
                        messages=[{"role": "user", "content": user_prompt}],
                        config=config,
                    )
                    reflection = (
                        resp.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                except Exception as e:
                    logger.warning("Weekly reflection LLM failed user=%s: %s", user_id, e)
                    continue

                if not reflection or len(reflection.strip()) < 50:
                    continue

                # Store as a special high-importance episode
                episode_id = str(uuid.uuid4())
                try:
                    await memory.store_episode(
                        episode_id=episode_id,
                        summary=reflection.strip(),
                        keywords=["weekly_reflection", "journal"],
                        emotion_tags=["reflective"],
                        importance=8,
                        conversation_id="weekly-reflection",
                        user_id=user_id,
                    )
                    logger.info("Weekly reflection saved: user=%s ep=%s", user_id, episode_id[:8])
                except Exception as e:
                    logger.warning("Weekly reflection save failed user=%s: %s", user_id, e)
        except Exception as e:
            logger.error("Weekly reflection job failed: %s", e)
