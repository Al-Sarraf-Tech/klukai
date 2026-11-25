"""Dream diary — persist dreams Klukai tells the user after overnight absences.

When reflection-on-return takes the 'dream' branch, the generated dream
narrative is saved as a `companion_memories` row with category=`Dreams`
and a sentinel filename. Dreams are text only: the row has no image prompt,
and picture-generation instructions are not stored.

Dreams are scoped per-user, searchable via /api/memories/search, and
listable via /api/dreams. They count toward memory_count in the stats
endpoint just like any other kept memory.
"""

from __future__ import annotations
from typing import Any


import logging
import re
import uuid

from .db import get_pool

logger = logging.getLogger(__name__)


DREAM_CATEGORY = "Dreams"

# Lines that are instructions to an image model, not something she dreamed.
_PICTURE_INSTRUCTION = re.compile(
    r"(?i)(?:negative prompt|danbooru|masterpiece|best quality|\bscore_\d+\b|"
    r"\b1girl\b|\b1boy\b|image prompt|scene_tags\s*:|"
    r"\b(?:generate|render|draw|paint) (?:me )?(?:an? )?(?:image|picture|illustration)\b)"
)


def without_picture_instructions(text: str) -> str:
    """Drop picture-generation lines. A dream is prose, never a render brief."""
    kept: list[str] = []
    for line in (text or "").splitlines():
        if _PICTURE_INSTRUCTION.search(line):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


async def save_dream(
    dream_text: str,
    user_id: str = "jalsarraf",
    affection_level: int = 0,
    mood: str = "tender",
) -> str | None:
    """Persist a dream as a companion_memories row. Returns memory_id or None.

    Dreams are kept by default and carry the Dreams category. A deterministic
    sentinel filename (`dream-{uuid}.txt`) is used since the table requires
    NOT NULL on filename. The prompt column stays empty so nothing paints
    an image from the dream.
    """
    dream_text = without_picture_instructions(dream_text)
    if len(dream_text) < 20:
        return None
    try:
        memory_id = str(uuid.uuid4())
        sentinel = f"dream-{memory_id}.txt"

        pool = get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO companion_memories "
                "(id, filename, annotation, category, mood, affection_level, "
                "kept, kept_by, conversation_id, user_id, prompt) "
                "VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s, %s, %s)",
                (
                    memory_id,
                    sentinel,
                    dream_text.strip(),
                    DREAM_CATEGORY,
                    mood,
                    affection_level,
                    "klukai-dreamer",
                    "reflection-on-return",
                    user_id,
                    None,
                ),
            )
            await conn.commit()
        logger.info(
            "Dream saved for %s: memory=%s len=%d",
            user_id,
            memory_id[:8],
            len(dream_text),
        )
        return memory_id
    except Exception as e:
        logger.warning("Dream save failed for %s: %s", user_id, e)
        return None


async def list_dreams(
    user_id: str = "jalsarraf", limit: int = 20
) -> list[dict[str, Any]]:
    """Return the user's saved dreams ordered newest-first.

    Fails closed: a DB outage surfaces as a 503 (matching /api/messages)
    instead of masquerading as an empty dream diary.
    """
    limit = max(1, min(limit, 200))
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, annotation, mood, affection_level, filename, "
                    "created_at "
                    "FROM companion_memories "
                    "WHERE user_id = %s AND category = %s AND kept = TRUE "
                    "ORDER BY created_at DESC LIMIT %s",
                    (user_id, DREAM_CATEGORY, limit),
                )
            ).fetchall()
        return [
            {
                "id": str(r[0]),
                "dream": r[1],
                "mood": r[2],
                "affection_level": r[3],
                "has_image": bool(r[4]) and not str(r[4]).startswith("dream-"),
                "created_at": r[5].isoformat() if r[5] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("list_dreams failed: %s", e)
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="Could not load dreams") from e


async def count_dreams(user_id: str = "jalsarraf") -> int:
    """Count dreams kept for this user. Fails to 0 on DB error."""
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT COUNT(*) FROM companion_memories "
                    "WHERE user_id = %s AND category = %s AND kept = TRUE",
                    (user_id, DREAM_CATEGORY),
                )
            ).fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0
