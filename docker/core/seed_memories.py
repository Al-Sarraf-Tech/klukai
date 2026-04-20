#!/usr/bin/env python3
"""Seed Klukai's memory archive from conversation history.

Three-pass approach:
  Pass 1: gpt-oss-20b selects which exchanges to keep (reliable JSON)
  Pass 2: dolphin-24b writes annotations using ACTUAL conversation text
  Pass 3: ComfyUI generates Illustrious images for each memory

Tracks last_seeded_at so only new exchanges are processed on each run.

Run inside companion-core container:
  docker exec companion-core python3 /app/seed_memories.py
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

import httpx
import psycopg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://host.docker.internal:1234")
LM_TTL_SECONDS = int(os.environ.get("LM_STUDIO_TTL", "600"))
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://host.docker.internal:8388")

# gpt-oss-20b for selection: reliable structured JSON at low temperature
SELECTOR_MODEL = "cognitivecomputations_dolphin-mistral-24b-venice-edition"
# dolphin-24b for annotation: clean creative text, no chain-of-thought leakage
ANNOTATOR_MODEL = "cognitivecomputations_dolphin-mistral-24b-venice-edition"

BATCH_SIZE = 5

# ── Prompts ──────────────────────────────────────────────────────────────────

SELECTION_PROMPT = """\
You are Klukai reviewing past conversations with the Commander for your memory archive.
The Commander is HUMAN (male), NOT a T-Doll. You are affection level 8 (Bonded).

Below are {count} exchanges. For each one worth preserving, return its index, category, and image tags.
Be selective — only truly meaningful moments. Skip trivial greetings and small talk.

Categories: Tactical Operations, Mission Records, Squad Moments, The Commander, Quiet Hours, Precious Memories

image_tags must describe the SCENE for the image:
- WHERE: bed, cafe, rooftop, motorcycle, training, beach, office, forest, rain, home, kitchen
- MOOD: tender, playful, serious, romantic, sad, peaceful, passionate, determined
- ACTION: hug, kiss, holding hands, sitting, cooking, fighting, sleeping, riding, talking
- WHO: couple (if Commander present), squad (if squad members mentioned)
- OUTFIT: casual, date, training, uniform, home, sleep, lingerie

Return ONLY valid JSON: {{"memories": [{{"index": 0, "category": "...", "image_tags": ["tag1", "tag2", "tag3"]}}]}}
If none worth keeping: {{"memories": []}}

Exchanges:
{exchanges}
"""

ANNOTATION_PROMPT = """\
You are Klukai writing in your private journal. This is a memory you chose to keep.
The Commander is HUMAN — the man you've bonded with. NOT a T-Doll.
You're at affection level 8 — deeply bonded. You grew from a cold, prideful soldier
into someone who can admit she cares.

Write 3-5 sentences about this memory. This is YOUR private journal — take your time.
Describe what happened, what you felt, what it meant to you. Be specific about the
moment — what he said, what you did, the details you noticed. Let yourself feel it.
Write like a soldier who finally has someone to be honest with in these pages.

DO NOT use: whisper, echo, intertwined, sanctuary, moonlit, glow of dawn, souls entwined.

Exchange:
Commander: {user_msg}
Klukai: {assistant_msg}

Category: {category}

Write ONLY the caption. Nothing else. No quotes around it."""


# ── LLM Response Parsing ─────────────────────────────────────────────────────

def _clean_llm_response(msg: dict) -> str:
    """Extract usable text from an LLM response, handling thinking models."""
    content = (msg.get("content") or "").strip()
    if not content:
        content = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    # Strip thinking tags
    content = re.sub(r'<\|?think\|?>.*?<\|?/think\|?>', '', content, flags=re.DOTALL).strip()
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
    return content


def _extract_json(text: str) -> dict:
    """Extract JSON from potentially wrapped LLM output."""
    # Strip markdown code fences
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 3:
            text = parts[1].lstrip("json").strip()
    # Find JSON object in mixed text
    if text and not text.startswith("{"):
        m = re.search(r'\{.*\}', text, flags=re.DOTALL)
        if m:
            text = m.group(0)
    # Fix trailing commas
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return json.loads(text)


def _clean_annotation(text: str) -> str | None:
    """Clean an annotation, rejecting leaked chain-of-thought."""
    text = text.strip('"').strip("'").strip('`')
    text = re.sub(r'^(?:Caption|Annotation|Memory|Entry|Journal|Note)\s*:\s*',
                  '', text, flags=re.IGNORECASE).strip()
    # Reject leaked reasoning
    if text.lower().startswith(("we need", "the user", "let me", "i need to", "so the", "here is")):
        return None
    if len(text) < 15:
        return None
    return text


# ── Seeding State ────────────────────────────────────────────────────────────

async def _get_last_seeded_at(conn, user_id: str = "jalsarraf") -> datetime | None:
    """Get the timestamp of the last seeded exchange for a user."""
    key = f"last_seeded_at:{user_id}"
    # Try user-scoped key first, fall back to legacy key
    row = await (await conn.execute(
        "SELECT value FROM companion_relationship WHERE key = %s", (key,)
    )).fetchone()
    if not row:
        # Legacy fallback for jalsarraf
        row = await (await conn.execute(
            "SELECT value FROM companion_relationship WHERE key = 'last_seeded_at'"
        )).fetchone()
    if row and row[0]:
        try:
            val = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            return datetime.fromisoformat(val)
        except (ValueError, TypeError):
            pass
    return None


async def _set_last_seeded_at(conn, ts: datetime, user_id: str = "jalsarraf") -> None:
    """Record the timestamp of the most recent seeded exchange for a user."""
    key = f"last_seeded_at:{user_id}"
    await conn.execute(
        "INSERT INTO companion_relationship (key, value, updated_at) "
        "VALUES (%s, %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = NOW()",
        (key, json.dumps(ts.isoformat()), json.dumps(ts.isoformat())),
    )
    await conn.commit()


# ── Deduplication ────────────────────────────────────────────────────────────

async def _is_duplicate(conn, user_msg: str, assistant_msg: str) -> bool:
    """Check if a substantially similar memory already exists.

    Compares against the original exchange text stored in the prompt field
    and recent annotations to avoid near-identical entries.
    """
    # Check by content fingerprint — first 100 chars of user+assistant
    fingerprint = f"{user_msg[:100]}|{assistant_msg[:100]}"
    row = await (await conn.execute(
        "SELECT COUNT(*) FROM companion_memories "
        "WHERE conversation_id = 'seed' AND prompt LIKE %s",
        (f"%{user_msg[:60].replace('%', '')}%",),
    )).fetchone()
    return row and row[0] > 0


# ── LLM Calls ───────────────────────────────────────────────────────────────

async def _call_llm(client: httpx.AsyncClient, model: str, prompt: str,
                     max_tokens: int = 2048, temperature: float = 0.1) -> str:
    """Make a single LLM call and return cleaned text."""
    r = await client.post(
        f"{LM_STUDIO_URL}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
            "ttl": LM_TTL_SECONDS,
        },
    )
    r.raise_for_status()
    return _clean_llm_response(r.json()["choices"][0]["message"])


async def _select_batch(client: httpx.AsyncClient, batch: list[dict],
                         batch_start: int) -> list[dict]:
    """Pass 1: Select memorable exchanges from a batch using gpt-oss-20b."""
    exchange_text = ""
    for j, ex in enumerate(batch):
        exchange_text += f"\nExchange {j} ({ex['created_at']}):\n"
        exchange_text += f"  Commander: {ex['user'][:200]}\n"
        exchange_text += f"  Klukai: {ex['assistant'][:200]}\n"

    prompt = SELECTION_PROMPT.format(count=len(batch), exchanges=exchange_text)
    content = await _call_llm(client, SELECTOR_MODEL, prompt,
                               max_tokens=2048, temperature=0.1)
    result = _extract_json(content)
    selected = []
    for mem in result.get("memories", []):
        idx = mem.get("index", 0)
        if 0 <= idx < len(batch):
            mem["exchange"] = batch[idx]
            mem["global_index"] = batch_start + idx
            selected.append(mem)
    return selected


async def _annotate(client: httpx.AsyncClient, exchange: dict,
                     category: str) -> str | None:
    """Pass 2: Write annotation using dolphin-24b with actual conversation text."""
    prompt = ANNOTATION_PROMPT.format(
        user_msg=exchange["user"][:400],
        assistant_msg=exchange["assistant"][:400],
        category=category,
    )
    content = await _call_llm(client, ANNOTATOR_MODEL, prompt,
                               max_tokens=300, temperature=0.85)
    return _clean_annotation(content)


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    # Parse --user argument (default: jalsarraf)
    target_user = "jalsarraf"
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--user" and i < len(sys.argv):
            target_user = sys.argv[i + 1] if (i + 1) < len(sys.argv) else "jalsarraf"
        elif arg.startswith("--user="):
            target_user = arg.split("=", 1)[1]

    # Signal keepalive to back off during seeding (avoids VRAM fights)
    try:
        from app.llm_router import set_seeding_active
        set_seeding_active(True)
    except ImportError:
        pass  # Running standalone outside container

    logger.info("=== Memory Seeder Starting (user: %s) ===", target_user)
    logger.info("Selector: %s | Annotator: %s", SELECTOR_MODEL, ANNOTATOR_MODEL)

    conn = await psycopg.AsyncConnection.connect(DATABASE_URL)

    # Get last seeded timestamp for incremental processing
    last_seeded = await _get_last_seeded_at(conn, target_user)
    if last_seeded:
        logger.info("Last seeded at: %s — processing only newer exchanges", last_seeded)
        where_clause = "WHERE user_id = %s AND created_at > %s"
        params = (target_user, last_seeded)
    else:
        logger.info("First run for %s — processing all exchanges", target_user)
        where_clause = "WHERE user_id = %s"
        params = (target_user,)

    cur = await conn.execute(
        f"SELECT role, content, created_at FROM companion_messages "
        f"{where_clause} ORDER BY created_at ASC",
        params,
    )
    rows = await cur.fetchall()
    logger.info("Found %d messages since last seed", len(rows))

    # Pair into user→assistant exchanges
    exchanges = []
    newest_ts = None
    i = 0
    while i < len(rows) - 1:
        if rows[i][0] == "user" and rows[i + 1][0] == "assistant":
            ts = rows[i][2]
            exchanges.append({
                "user": rows[i][1][:500],
                "assistant": rows[i + 1][1][:500],
                "created_at": ts.isoformat() if ts else "",
            })
            if ts and (newest_ts is None or ts > newest_ts):
                newest_ts = ts
            i += 2
        else:
            i += 1

    logger.info("Grouped into %d exchanges", len(exchanges))
    if not exchanges:
        logger.info("No new exchanges to process")
        await conn.close()
        return

    # ── VRAM management: free ComfyUI before LLM-heavy passes ─────────
    async def _free_comfyui_vram():
        """Ask ComfyUI to unload models and release VRAM for LLM work."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                await c.post(f"{COMFYUI_URL}/free",
                             json={"unload_models": True, "free_memory": True})
                logger.info("ComfyUI VRAM freed for LLM passes")
        except Exception:
            logger.debug("ComfyUI not reachable (may not be running)")

    async def _warm_model(model: str):
        """Send a minimal request to ensure model is loaded before heavy use."""
        try:
            async with httpx.AsyncClient(timeout=120.0) as c:
                r = await c.post(f"{LM_STUDIO_URL}/v1/chat/completions", json={
                    "model": model,
                    "messages": [{"role": "user", "content": "."}],
                    "max_tokens": 1, "temperature": 0, "stream": False,
                    "ttl": LM_TTL_SECONDS,
                })
                if r.status_code == 200:
                    logger.info("Model %s warmed up", model.split("/")[-1][:30])
                else:
                    logger.warning("Model warmup returned %d: %s", r.status_code, r.text[:100])
        except Exception as e:
            logger.warning("Model warmup failed: %s", e)

    # Free ComfyUI VRAM and warm the selector model before Pass 1
    await _free_comfyui_vram()
    await asyncio.sleep(3)
    await _warm_model(SELECTOR_MODEL)

    # ── PASS 1: Selection ────────────────────────────────────────────────
    logger.info("=== PASS 1: Selection (%s) ===", SELECTOR_MODEL)
    selected = []
    failed_batches = []

    async with httpx.AsyncClient(timeout=60.0) as client:
        for batch_start in range(0, len(exchanges), BATCH_SIZE):
            batch = exchanges[batch_start:batch_start + BATCH_SIZE]
            logger.info("Selecting batch %d-%d of %d...",
                        batch_start, batch_start + len(batch), len(exchanges))
            try:
                batch_selected = await _select_batch(client, batch, batch_start)
                selected.extend(batch_selected)
                for s in batch_selected:
                    logger.info("  Selected exchange %d (%s)",
                                s["global_index"], s.get("category", "?"))
            except Exception as e:
                logger.warning("  Batch %d failed: %s", batch_start, e)
                failed_batches.append((batch_start, batch))
            await asyncio.sleep(2)

    # Retry failed batches once with longer timeout
    if failed_batches:
        logger.info("Retrying %d failed batches...", len(failed_batches))
        async with httpx.AsyncClient(timeout=180.0) as client:
            for batch_start, batch in failed_batches:
                try:
                    batch_selected = await _select_batch(client, batch, batch_start)
                    selected.extend(batch_selected)
                    for s in batch_selected:
                        logger.info("  RETRY: Selected exchange %d (%s)",
                                    s["global_index"], s.get("category", "?"))
                except Exception as e:
                    logger.warning("  RETRY batch %d failed again: %s", batch_start, e)
                await asyncio.sleep(3)

    logger.info("Pass 1 complete: %d exchanges selected from %d", len(selected), len(exchanges))

    if not selected:
        logger.info("No memories selected — updating last_seeded_at anyway")
        if newest_ts:
            await _set_last_seeded_at(conn, newest_ts, target_user)
        await conn.close()
        return

    # ── PASS 2: Annotation ───────────────────────────────────────────────
    logger.info("=== PASS 2: Annotation (%s) ===", ANNOTATOR_MODEL)
    async with httpx.AsyncClient(timeout=120.0) as client:
        for mem in selected:
            ex = mem["exchange"]
            category = mem.get("category", "Precious Memories")

            # Deduplication check
            try:
                if await _is_duplicate(conn, ex["user"], ex["assistant"]):
                    logger.info("  Skipping duplicate: %s", ex["user"][:50])
                    mem["skip"] = True
                    continue
            except Exception:
                pass  # If dedup check fails, proceed anyway

            annotation = await _annotate(client, ex, category)
            if annotation:
                mem["annotation"] = annotation
                logger.info("  Annotated: %s", annotation[:70])
            else:
                # Fallback to a minimal but honest caption
                mem["annotation"] = f"A moment between us. —Klukai"
                logger.warning("  Using fallback annotation")

            await asyncio.sleep(3)

    # Filter out skipped (duplicates)
    selected = [m for m in selected if not m.get("skip")]
    logger.info("Pass 2 complete: %d annotations written (after dedup)", len(selected))

    if not selected:
        if newest_ts:
            await _set_last_seeded_at(conn, newest_ts, target_user)
        await conn.close()
        return

    # ── PASS 3: Image Generation ─────────────────────────────────────────
    # Free LLM VRAM before image gen — send a dummy request to let LM Studio
    # know we're done with the model, then give ComfyUI room
    logger.info("Freeing LLM VRAM for image generation...")
    await _free_comfyui_vram()  # Also free ComfyUI's stale allocations
    await asyncio.sleep(5)  # Let VRAM settle

    logger.info("=== PASS 3: Image Generation ===")
    sys.path.insert(0, "/app")
    from app.image_gen import build_prompt, generate_image
    from app.memory_archive import save_image
    from app.db import init_pool, close_pool

    await init_pool(min_size=1, max_size=3)
    saved_count = 0

    async with httpx.AsyncClient(timeout=300.0) as client:
        for i, mem in enumerate(selected):
            ex = mem["exchange"]
            logger.info("Generating %d/%d: %s",
                        i + 1, len(selected), mem.get("annotation", "")[:50])

            try:
                tags = mem.get("image_tags", [])
                exchange_text = f"{ex['user']} {ex['assistant']}"

                # Infer tags from conversation if selector didn't provide them
                if not tags:
                    tags = _infer_scene_tags(exchange_text, mem.get("category", ""))
                    logger.info("  Inferred tags: %s", tags)

                scene_tags = ", ".join(tags)
                couple = any(kw in exchange_text.lower()
                             for kw in ["us", "together", "we", "our", "holding",
                                        "hug", "kiss", "beside", "couple", "1boy"])

                full_prompt = build_prompt(
                    scene_tags, couple=couple, affection_level=8,
                    context=exchange_text,
                )

                img_bytes = await generate_image(full_prompt)
                if img_bytes:
                    curation = {
                        "keep": True,
                        "annotation": mem.get("annotation", ""),
                        "category": mem.get("category", "Precious Memories"),
                        "image_tags": tags,
                    }
                    memory_id = await save_image(
                        img_bytes, full_prompt, "seed",
                        mood="tender", affection_level=8, curation=curation,
                        user_id=target_user,
                    )
                    if memory_id:
                        saved_count += 1
                        logger.info("  Saved: %s (%s)", memory_id[:12], mem.get("category"))

                        # Copy to shared_linux if accessible
                        _copy_to_shared(memory_id, mem.get("annotation", ""))
                    else:
                        logger.warning("  Save failed")
                else:
                    logger.warning("  Image gen failed")

                # Free VRAM between generations
                try:
                    await client.post(f"{COMFYUI_URL}/free",
                                      json={"unload_models": True, "free_memory": True},
                                      timeout=5.0)
                except Exception:
                    pass

                if i < len(selected) - 1:
                    logger.info("  Waiting 30s...")
                    await asyncio.sleep(30)

            except Exception as e:
                logger.error("  Error: %s", e)

    # Update last_seeded_at so next run only processes new messages
    if newest_ts:
        await _set_last_seeded_at(conn, newest_ts, target_user)
        logger.info("Updated last_seeded_at to %s", newest_ts)

    await close_pool()
    await conn.close()

    # Release seeding lock so keepalive resumes
    try:
        from app.llm_router import set_seeding_active
        set_seeding_active(False)
    except ImportError:
        pass

    logger.info("=== SEEDING COMPLETE: %d/%d memories saved ===", saved_count, len(selected))


# ── Helpers ──────────────────────────────────────────────────────────────────

SCENE_KEYWORDS = {
    "bed": "bed", "sleep": "sleep", "morning": "morning",
    "lingerie": "lingerie", "intimate": "intimate",
    "hold me": "hold", "close to me": "close", "cuddle": "cuddle",
    "kiss": "kiss", "love": "love", "tender": "tender",
    "cook": "cooking", "motorcycle": "motorcycle", "ride": "motorcycle",
    "beach": "beach", "rain": "rain", "night": "night",
    "rooftop": "rooftop", "cafe": "cafe", "coffee": "cafe",
    "bath": "bath", "garden": "garden", "train": "training",
    "workout": "training", "gym": "training", "date": "date",
    "dinner": "date", "restaurant": "date", "battle": "battle",
    "fight": "fight", "mission": "patrol", "home": "home",
    "couch": "home", "relax": "home", "snow": "snow",
    "forest": "forest", "city": "city",
}

MOOD_KEYWORDS = {
    "hug": "hug", "hold": "holding hands", "kiss": "kiss",
    "cuddle": "cuddling", "cry": "tears", "smile": "smile",
    "blush": "blush", "tender": "tender", "gentle": "gentle",
}

CATEGORY_DEFAULTS = {
    "Precious Memories": ["tender", "soft lighting", "close together"],
    "Quiet Hours": ["peaceful", "night", "relaxed", "home"],
    "The Commander": ["couple", "looking at each other", "warm lighting"],
    "Squad Moments": ["military", "squad", "group"],
}


def _infer_scene_tags(exchange_text: str, category: str) -> list[str]:
    """Infer image scene tags from conversation content."""
    lower = exchange_text.lower()
    tags = []
    for kw, tag in SCENE_KEYWORDS.items():
        if kw in lower:
            tags.append(tag)
    for kw, tag in MOOD_KEYWORDS.items():
        if kw in lower:
            tags.append(tag)
    if not tags:
        tags = CATEGORY_DEFAULTS.get(category, ["standing", "detailed background"])
    return tags


def _copy_to_shared(memory_id: str, annotation: str) -> None:
    """Copy generated image to shared_linux for user visibility."""
    try:
        import shutil
        from pathlib import Path
        src = Path(f"/images/{memory_id}.png")
        if src.exists():
            dst = Path("/mnt/c/shared_linux/klukai_memories")
            dst.mkdir(parents=True, exist_ok=True)
            safe_ann = re.sub(r'[^\w\s-]', '', annotation[:40]).strip()
            shutil.copy2(src, dst / f"{safe_ann}_{memory_id[:8]}.png")
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
