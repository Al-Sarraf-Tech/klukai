"""Read/CRUD queries for memory archive.

Extracted from app/memory_archive.py (S+ Phase 2 — file-size hygiene per
docs/superpowers/specs/2026-05-16-s-plus-uplift.md §6.1).

Public surface preserved: `from app.memory_archive import list_memories, ...`
still works (re-exports below).
"""

from __future__ import annotations

import asyncio
import os
import random
from pathlib import Path

from .db import get_conn, get_conn_autocommit
from .memory_archive import (
    MOOD_CATEGORY_WEIGHTS,
    available_categories,
)

import logging

logger = logging.getLogger(__name__)

# Must match the WRITE path in memory_archive.py (and the companion-images volume
# mount, /images). This was hardcoded to a nonexistent /data/images, so every
# get_image_bytes() returned None and the memory archive showed no drawings.
IMAGES_DIR = Path(os.environ.get("IMAGES_DIR", "/images"))

async def get_image_bytes(
    memory_id: str, thumbnail: bool = False, user_id: str | None = None
) -> bytes | None:
    """Read image bytes from the volume. If user_id is provided, enforces ownership."""
    try:
        async with get_conn() as conn:
            col = "thumb_filename" if thumbnail else "filename"
            if user_id:
                row = await (await conn.execute(
                    f"SELECT {col} FROM companion_memories WHERE id = %s AND user_id = %s",  # nosec B608 — `col` is one of two literal column names; values bound via %s
                    (memory_id, user_id),
                )).fetchone()
            else:
                # Internal calls (e.g., recall) may not have user context
                row = await (await conn.execute(
                    f"SELECT {col} FROM companion_memories WHERE id = %s", (memory_id,)  # nosec B608 — same as above
                )).fetchone()
            if not row or not row[0]:
                return None
            path = IMAGES_DIR / row[0]
            if path.exists():
                # Offload the file read to a thread — a blocking read_bytes() in
                # this async path serializes concurrent image loads on the event
                # loop (a 540-image album would stall and starve chat/WS).
                return await asyncio.to_thread(path.read_bytes)
    except Exception as e:
        logger.error("Failed to read image %s: %s", memory_id, e)
    return None

async def list_memories(
    category: str | None = None,
    limit: int = 20,
    before: str | None = None,
    month: str | None = None,
    user_id: str = "jalsarraf",
) -> list[dict]:
    """List kept memories, optionally filtered by category and/or month (YYYY-MM), scoped to user."""
    try:
        async with get_conn() as conn:
            conditions = ["kept = true", "user_id = %s"]
            params: list = [user_id]

            if category:
                conditions.append("category = %s")
                params.append(category)
            if before:
                conditions.append("created_at < %s")
                params.append(before)
            if month:
                conditions.append("to_char(created_at, 'YYYY-MM') = %s")
                params.append(month)

            where = " AND ".join(conditions)
            params.append(limit)

            rows = await (await conn.execute(
                f"SELECT id, filename, thumb_filename, annotation, scene_tags, "
                f"mood, affection_level, kept_by, category, created_at "
                f"FROM companion_memories WHERE {where} "  # nosec B608 — `where` is built from a fixed allow-list of column predicates; values bound via %s
                f"ORDER BY created_at DESC LIMIT %s",
                tuple(params),
            )).fetchall()

            return [
                {
                    "id": str(r[0]),
                    "filename": r[1],
                    "thumb_filename": r[2],
                    "annotation": r[3] or "",
                    "scene_tags": r[4] or [],
                    "mood": r[5],
                    "affection_level": r[6],
                    "kept_by": r[7],
                    "category": r[8],
                    "created_at": r[9].isoformat() if r[9] else None,
                }
                for r in rows
            ]
    except Exception as e:
        logger.error("Failed to list memories: %s", e)
        return []

async def get_timeline(user_id: str = "jalsarraf") -> list[dict]:
    """Return month/year groups with memory counts for the archive timeline.

    Fails closed: a DB outage propagates (the route returns 503) instead of
    returning a fake-empty timeline that looks like a wiped archive.
    """
    try:
        async with get_conn() as conn:
            rows = await (await conn.execute(
                "SELECT to_char(created_at, 'YYYY-MM') as month, count(*) "
                "FROM companion_memories "
                "WHERE kept = true AND user_id = %s "
                "GROUP BY month ORDER BY month DESC",
                (user_id,),
            )).fetchall()
            return [{"month": r[0], "count": r[1]} for r in rows]
    except Exception as e:
        logger.error("Failed to get timeline: %s", e)
        raise

async def get_categories(affection_level: int, user_id: str = "jalsarraf") -> list[dict]:
    """Return available categories with memory counts, scoped to user.

    Fails closed — mirror of get_timeline: surface the outage.
    """
    try:
        valid = available_categories(affection_level)
        async with get_conn() as conn:
            rows = await (await conn.execute(
                "SELECT category, count(*) FROM companion_memories "
                "WHERE kept = true AND user_id = %s GROUP BY category",
                (user_id,),
            )).fetchall()

            counts = {r[0]: r[1] for r in rows}
            total = sum(counts.get(c, 0) for c in valid)
            result = [{"name": c, "count": counts.get(c, 0)} for c in valid]
            result.append({"name": "All", "count": total})
            return result
    except Exception as e:
        logger.error("Failed to get categories: %s", e)
        raise

async def update_kept(
    memory_id: str, kept: bool, kept_by: str = "commander", user_id: str | None = None
) -> bool:
    """Commander saves or discards a memory. If user_id provided, enforces ownership.

    Returns True only if a row was actually updated. Returns False if the memory
    doesn't exist or doesn't belong to this user.
    """
    try:
        async with get_conn_autocommit() as conn:
            if user_id:
                result = await conn.execute(
                    "UPDATE companion_memories SET kept = %s, kept_by = %s "
                    "WHERE id = %s AND user_id = %s",
                    (kept, kept_by, memory_id, user_id),
                )
            else:
                result = await conn.execute(
                    "UPDATE companion_memories SET kept = %s, kept_by = %s WHERE id = %s",
                    (kept, kept_by, memory_id),
                )
        return bool(result.rowcount and result.rowcount > 0)
    except Exception as e:
        logger.error("Failed to update memory %s: %s", memory_id, e)
        return False

async def update_curation(
    memory_id: str, curation: dict, affection_level: int = 0, user_id: str | None = None
) -> bool:
    """Update a memory row with LLM curation results after initial save."""
    try:
        kept = curation.get("keep", True)
        annotation = curation.get("annotation")
        category = curation.get("category", "Mission Records")
        scene_tags = curation.get("image_tags", [])

        valid = available_categories(affection_level)
        if category not in valid:
            category = valid[-1] if valid else "Mission Records"

        async with get_conn_autocommit() as conn:
            if user_id:
                await conn.execute(
                    "UPDATE companion_memories SET kept = %s, annotation = %s, "
                    "category = %s, scene_tags = %s WHERE id = %s AND user_id = %s",
                    (kept, annotation, category, scene_tags, memory_id, user_id),
                )
            else:
                await conn.execute(
                    "UPDATE companion_memories SET kept = %s, annotation = %s, "
                    "category = %s, scene_tags = %s WHERE id = %s",
                    (kept, annotation, category, scene_tags, memory_id),
                )
        logger.info("Memory %s curated: kept=%s, category=%s", memory_id, kept, category)
        return True
    except Exception as e:
        logger.error("Failed to update curation for %s: %s", memory_id, e)
        return False

async def recall_memory(
    query: str | None,
    mood: str,
    affection_level: int,
    user_id: str = "jalsarraf",
) -> dict | None:
    """Recall a memory from the archive, scoped to user.

    Specific query: search scene_tags + annotation text.
    Vague/none: mood-weighted random from kept memories.
    """
    try:
        async with get_conn() as conn:
            if query:
                # Extract search terms
                terms = [w.lower().strip() for w in query.split() if len(w) > 2]
                # Search tags first
                for term in terms:
                    rows = await (await conn.execute(
                        "SELECT id, filename, annotation, category, scene_tags, created_at "
                        "FROM companion_memories "
                        "WHERE kept = true AND user_id = %s AND %s = ANY(scene_tags) "
                        "ORDER BY created_at DESC LIMIT 1",
                        (user_id, term),
                    )).fetchall()
                    if rows:
                        return _row_to_dict(rows[0])

                # Fallback: search annotation text
                for term in terms:
                    rows = await (await conn.execute(
                        "SELECT id, filename, annotation, category, scene_tags, created_at "
                        "FROM companion_memories "
                        "WHERE kept = true AND user_id = %s AND annotation ILIKE %s "
                        "ORDER BY created_at DESC LIMIT 1",
                        (user_id, f"%{term}%"),
                    )).fetchall()
                    if rows:
                        return _row_to_dict(rows[0])

            # Vague query or no results: mood-weighted random
            valid_cats = available_categories(affection_level)
            weights = MOOD_CATEGORY_WEIGHTS.get(mood, {})

            # Build weighted category list
            weighted: list[tuple[str, int]] = []
            for cat in valid_cats:
                w = weights.get(cat, 1)
                weighted.append((cat, w))

            if not weighted:
                return None

            # Weighted random category selection
            chosen_cat = random.choices(
                [c for c, _ in weighted],
                weights=[w for _, w in weighted],
                k=1,
            )[0]

            rows = await (await conn.execute(
                "SELECT id, filename, annotation, category, scene_tags, created_at "
                "FROM companion_memories "
                "WHERE kept = true AND user_id = %s AND category = %s "
                "ORDER BY random() LIMIT 1",
                (user_id, chosen_cat),
            )).fetchall()

            if rows:
                return _row_to_dict(rows[0])

            # Fallback: any kept memory for this user
            rows = await (await conn.execute(
                "SELECT id, filename, annotation, category, scene_tags, created_at "
                "FROM companion_memories WHERE kept = true AND user_id = %s "
                "ORDER BY random() LIMIT 1",
                (user_id,),
            )).fetchall()
            return _row_to_dict(rows[0]) if rows else None

    except Exception as e:
        logger.error("Memory recall failed: %s", e)
        return None

def _row_to_dict(r) -> dict:
    return {
        "id": str(r[0]),
        "filename": r[1],
        "annotation": r[2] or "",
        "category": r[3],
        "scene_tags": r[4] or [],
        "created_at": r[5].isoformat() if r[5] else None,
    }

