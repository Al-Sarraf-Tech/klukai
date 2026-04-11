"""Memory archive: Klukai's curated image collection."""

from __future__ import annotations

import logging
import os
import random
import re
import uuid
from pathlib import Path

import httpx
from PIL import Image

from .db import get_conn, get_conn_autocommit

logger = logging.getLogger(__name__)

LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://192.168.50.2:1234")
EXTRACTION_MODEL = "gpt-oss-20b-absolute-heresy-i1"

# Shared httpx client for LM Studio calls
_http: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=60.0)
    return _http

IMAGES_DIR = Path(os.environ.get("IMAGES_DIR", "/images"))

# Affection-gated categories
CATEGORIES_BY_LEVEL = {
    0: ["Tactical Operations", "Mission Records", "Squad Moments"],
    3: ["The Commander", "Quiet Hours"],
    6: ["Precious Memories"],
}

# Mood-to-category affinity for vague recall
MOOD_CATEGORY_WEIGHTS = {
    "tender": {"Precious Memories": 5, "The Commander": 3, "Quiet Hours": 2},
    "longing": {"Precious Memories": 4, "The Commander": 3, "Quiet Hours": 2},
    "nostalgic": {"Precious Memories": 3, "The Commander": 3, "Squad Moments": 2},
    "affectionate": {"Precious Memories": 5, "The Commander": 4},
    "devoted": {"Precious Memories": 5, "The Commander": 3},
    "melancholic": {"Precious Memories": 3, "Quiet Hours": 3, "Squad Moments": 2},
    "composed": {"Mission Records": 3, "Tactical Operations": 2, "The Commander": 1},
    "battle_ready": {"Tactical Operations": 5, "Mission Records": 3},
    "protective": {"Squad Moments": 4, "The Commander": 3},
    "playful": {"Quiet Hours": 4, "The Commander": 3, "Squad Moments": 2},
}


def available_categories(affection_level: int) -> list[str]:
    """Return categories available at the given affection level."""
    cats = []
    for min_level, level_cats in sorted(CATEGORIES_BY_LEVEL.items()):
        if affection_level >= min_level:
            cats.extend(level_cats)
    return cats


def annotation_quality_score(text: str) -> float:
    """Score annotation quality 0.0-1.0 based on specificity and character voice.

    Used to identify annotations that need re-writing:
    - 0.0 = leaked chain-of-thought or completely broken
    - 0.3-0.5 = generic/repetitive (tag-based, lacks specificity)
    - 0.6-0.8 = decent but could be better
    - 0.9-1.0 = specific, personal, sounds like Klukai
    """
    if not text:
        return 0.0
    score = 1.0
    lower = text.lower()
    # Leaked COT = instant zero
    if lower.startswith(("we need", "the user", "let me")):
        return 0.0
    if "1-2 sentence" in lower:
        return 0.0
    # Repetitive openers
    if lower.startswith("whisper"):
        score -= 0.4
    # Generic romantic vocabulary (sign of tag-based annotation)
    generic_words = ["intertwined", "sanctuary", "souls entwined", "hearts beat as one",
                     "glow of dawn", "moonlit sheets", "neon lights"]
    for word in generic_words:
        if word in lower:
            score -= 0.15
    # Too short = lacking detail
    if len(text) < 30:
        score -= 0.3
    # Too long = verbose/rambling
    if len(text) > 350:
        score -= 0.2
    # Bonus: specific details (places, actions, sensory words)
    specific_markers = ["office", "bed", "motorcycle", "café", "rooftop", "morning",
                        "collar", "rifle", "coffee", "rain", "briefing", "0300",
                        "shoulder", "hand", "scar", "laugh"]
    specifics = sum(1 for m in specific_markers if m in lower)
    score += min(0.2, specifics * 0.05)
    return max(0.0, min(1.0, score))


async def _is_duplicate_annotation(annotation: str, threshold: float = 0.7) -> bool:
    """Check if a substantially similar annotation already exists.

    Uses simple word overlap ratio — fast, no LLM or embedding needed.
    Returns True if a recent memory has >threshold word overlap.
    """
    if not annotation or annotation == "Uncaptioned moment.":
        return False
    try:
        async with get_conn() as conn:
            rows = await (await conn.execute(
                "SELECT annotation FROM companion_memories "
                "WHERE kept = true AND annotation IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 30"
            )).fetchall()

        target_words = set(annotation.lower().split())
        if len(target_words) < 3:
            return False

        for row in rows:
            existing = row[0] or ""
            existing_words = set(existing.lower().split())
            if not existing_words:
                continue
            overlap = len(target_words & existing_words)
            ratio = overlap / max(len(target_words), len(existing_words))
            if ratio > threshold:
                logger.debug("Duplicate detected (%.0f%% overlap): %s", ratio * 100, annotation[:50])
                return True
    except Exception as e:
        logger.debug("Dedup check failed: %s", e)
    return False


async def save_image(
    image_bytes: bytes,
    prompt: str,
    conversation_id: str,
    mood: str = "composed",
    affection_level: int = 0,
    curation: dict | None = None,
    user_id: str = "jalsarraf",
) -> str | None:
    """Save a generated image to the volume and create a metadata row.

    Returns:
        Memory ID (UUID string) or None on failure.
    """
    try:
        memory_id = str(uuid.uuid4())
        filename = f"{memory_id}.png"
        thumb_filename = f"{memory_id}_thumb.png"

        # Save full image
        img_path = IMAGES_DIR / filename
        img_path.write_bytes(image_bytes)

        # Generate thumbnail (320px wide)
        thumb_path = IMAGES_DIR / thumb_filename
        _generate_thumbnail(img_path, thumb_path, width=320)

        # Extract curation data
        kept = True
        kept_by = "klukai"
        annotation = None
        category = "Mission Records"
        scene_tags: list[str] = []

        if curation:
            kept = curation.get("keep", True)
            annotation = curation.get("annotation")
            category = curation.get("category", "Mission Records")
            scene_tags = curation.get("image_tags", [])
            # Validate category against affection level
            valid = available_categories(affection_level)
            if category not in valid:
                category = valid[-1] if valid else "Mission Records"

        # Ensure annotation is never NULL — every image must have text
        if not annotation:
            logger.warning(
                "Memory %s saved without annotation (prompt=%s). "
                "Using default placeholder.",
                memory_id, prompt[:80],
            )
            annotation = "Uncaptioned moment."

        # Deduplication — skip if a very similar annotation already exists
        if await _is_duplicate_annotation(annotation):
            logger.info("Skipping duplicate memory: %s", annotation[:60])
            # Clean up the already-written image files
            img_path.unlink(missing_ok=True)
            thumb_path.unlink(missing_ok=True)
            return None

        # Store metadata
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "INSERT INTO companion_memories "
                "(id, filename, thumb_filename, prompt, annotation, scene_tags, "
                "mood, affection_level, kept, kept_by, category, conversation_id, user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    memory_id, filename, thumb_filename, prompt, annotation,
                    scene_tags, mood, affection_level, kept, kept_by,
                    category, conversation_id, user_id,
                ),
            )

        logger.info(
            "Memory saved: %s (kept=%s, category=%s, tags=%s)",
            memory_id, kept, category, scene_tags,
        )
        return memory_id

    except Exception as e:
        logger.error("Failed to save memory: %s", e)
        return None


def _generate_thumbnail(src: Path, dst: Path, width: int = 320) -> None:
    """Generate a thumbnail with the given width, preserving aspect ratio."""
    try:
        with Image.open(src) as img:
            ratio = width / img.width
            height = int(img.height * ratio)
            thumb = img.resize((width, height), Image.LANCZOS)
            thumb.save(dst, "PNG", optimize=True)
    except Exception as e:
        logger.warning("Thumbnail generation failed: %s", e)


async def get_image_bytes(memory_id: str, thumbnail: bool = False) -> bytes | None:
    """Read image bytes from the volume."""
    try:
        async with get_conn() as conn:
            col = "thumb_filename" if thumbnail else "filename"
            row = await (await conn.execute(
                f"SELECT {col} FROM companion_memories WHERE id = %s", (memory_id,)
            )).fetchone()
            if not row or not row[0]:
                return None
            path = IMAGES_DIR / row[0]
            if path.exists():
                return path.read_bytes()
    except Exception as e:
        logger.error("Failed to read image %s: %s", memory_id, e)
    return None


async def list_memories(
    category: str | None = None,
    limit: int = 20,
    before: str | None = None,
    user_id: str = "jalsarraf",
) -> list[dict]:
    """List kept memories, optionally filtered by category, scoped to user."""
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

            where = " AND ".join(conditions)
            params.append(limit)

            rows = await (await conn.execute(
                f"SELECT id, filename, thumb_filename, annotation, scene_tags, "
                f"mood, affection_level, kept_by, category, created_at "
                f"FROM companion_memories WHERE {where} "
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


async def get_categories(affection_level: int, user_id: str = "jalsarraf") -> list[dict]:
    """Return available categories with memory counts, scoped to user."""
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
        return []


async def update_kept(memory_id: str, kept: bool, kept_by: str = "commander") -> bool:
    """Commander saves or discards a memory."""
    try:
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "UPDATE companion_memories SET kept = %s, kept_by = %s WHERE id = %s",
                (kept, kept_by, memory_id),
            )
        return True
    except Exception as e:
        logger.error("Failed to update memory %s: %s", memory_id, e)
        return False


async def update_curation(memory_id: str, curation: dict, affection_level: int = 0) -> bool:
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


# ── Annotation backfill ──────────────────────────────────────────────────────

_BACKFILL_PROMPT = """\
You are Klukai writing a brief caption for an image in your memory archive.
The Commander is HUMAN (male). You are affection level 8 — deeply bonded.

Given the context below, write a 1-2 sentence private journal caption for this image.
Be honest with yourself. Show how this moment affected you.

Prompt used to generate the image: {prompt}
Category: {category}
Scene tags: {tags}

Write ONLY the caption. Nothing else.
"""


async def backfill_annotations() -> dict:
    """Find all memory archive entries with NULL/empty annotation and generate one.

    Uses dolphin through the LM Studio gate to produce a brief
    annotation based on the existing prompt, category, and scene_tags fields.

    Returns:
        dict with "total" (entries needing backfill) and "updated" (successful).
    """
    from .llm_router import get_lm_gate

    try:
        async with get_conn() as conn:
            rows = await (await conn.execute(
                "SELECT id, prompt, category, scene_tags "
                "FROM companion_memories "
                "WHERE annotation IS NULL OR annotation = '' "
                "ORDER BY created_at ASC"
            )).fetchall()
    except Exception as e:
        logger.error("Backfill: failed to query unannotated memories: %s", e)
        return {"total": 0, "updated": 0, "error": str(e)}

    total = len(rows)
    if total == 0:
        logger.info("Backfill: all memories already have annotations.")
        return {"total": 0, "updated": 0}

    logger.info("Backfill: %d memories need annotations.", total)
    updated = 0
    gate = get_lm_gate()

    for row in rows:
        mem_id = str(row[0])
        prompt = row[1] or "unknown scene"
        category = row[2] or "Mission Records"
        tags = ", ".join(row[3]) if row[3] else "none"

        llm_prompt = _BACKFILL_PROMPT.format(
            prompt=prompt[:300], category=category, tags=tags
        )

        try:
            async with gate:
                client = _get_http()
                r = await client.post(
                    f"{LM_STUDIO_URL}/v1/chat/completions",
                    json={
                        "model": EXTRACTION_MODEL,
                        "messages": [{"role": "user", "content": llm_prompt}],
                        "max_tokens": 150,
                        "temperature": 0.7,
                        "stream": False,
                    },
                )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"].strip()

            # Strip thinking tags if present
            content = re.sub(
                r'<\|?think\|?>.*?<\|?/think\|?>', '', content, flags=re.DOTALL
            ).strip()
            content = re.sub(
                r'<think>.*?</think>', '', content, flags=re.DOTALL
            ).strip()

            # Clean up quotes / markdown prefixes
            annotation = content.strip('"').strip("'").strip('`')
            if not annotation:
                annotation = "Uncaptioned moment."

            async with get_conn_autocommit() as conn:
                await conn.execute(
                    "UPDATE companion_memories SET annotation = %s WHERE id = %s",
                    (annotation, mem_id),
                )

            updated += 1
            logger.info(
                "Backfill %d/%d: %s -> %s",
                updated, total, mem_id, annotation[:60],
            )

        except Exception as e:
            logger.error("Backfill failed for %s: %s", mem_id, e)

    logger.info("Backfill complete: %d/%d updated.", updated, total)
    return {"total": total, "updated": updated}
