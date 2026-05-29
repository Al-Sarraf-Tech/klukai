"""Memory archive: Klukai's curated image collection."""

from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path

import httpx
from PIL import Image

from .db import get_conn, get_conn_autocommit

logger = logging.getLogger(__name__)

LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://192.168.50.2:1234")
EXTRACTION_MODEL = "cognitivecomputations_dolphin-mistral-24b-venice-edition"

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
    0: ["Tactical Operations", "Mission Records", "Squad Moments", "Dreams"],
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
        thumb_filename = f"{memory_id}_thumb.webp"

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
    """Generate a small WebP thumbnail (efficient for the grid), preserving
    aspect ratio. WebP is ~7x smaller than PNG for these photos; the full
    image is served separately at full resolution when a memory is opened."""
    try:
        with Image.open(src) as img:
            ratio = width / img.width
            height = int(img.height * ratio)
            thumb = img.convert("RGB").resize((width, height), Image.LANCZOS)
            thumb.save(dst, "WEBP", quality=80, method=6)
    except Exception as e:
        logger.warning("Thumbnail generation failed: %s", e)


















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


async def backfill_annotations(user_id: str = "jalsarraf") -> dict:
    """Find memory archive entries with NULL/empty annotation and generate one.

    Scoped to a specific user. Uses dolphin through the LM Studio gate.

    Returns:
        dict with "total" (entries needing backfill) and "updated" (successful).
    """
    from .llm_router import get_lm_gate

    try:
        async with get_conn() as conn:
            rows = await (await conn.execute(
                "SELECT id, prompt, category, scene_tags "
                "FROM companion_memories "
                "WHERE (annotation IS NULL OR annotation = '') AND user_id = %s "
                "ORDER BY created_at ASC",
                (user_id,),
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
                from .llm_router import LM_TTL_SECONDS
                r = await client.post(
                    f"{LM_STUDIO_URL}/v1/chat/completions",
                    json={
                        "model": EXTRACTION_MODEL,
                        "messages": [{"role": "user", "content": llm_prompt}],
                        "max_tokens": 150,
                        "temperature": 0.7,
                        "stream": False,
                        "ttl": LM_TTL_SECONDS,
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

# Re-exports — query functions moved to memory_archive_query.py for file-size hygiene.
from app.memory_archive_query import (  # noqa: E402
    get_image_bytes,
    list_memories,
    get_timeline,
    get_categories,
    update_kept,
    update_curation,
    recall_memory,
    _row_to_dict,
)

__all__ = [
    "get_image_bytes",
    "list_memories",
    "get_timeline",
    "get_categories",
    "update_kept",
    "update_curation",
    "recall_memory",
    "_row_to_dict",
    "annotation_quality_score",
    "available_categories",
    "backfill_annotations",
    "save_image",
]
