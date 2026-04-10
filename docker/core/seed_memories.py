#!/usr/bin/env python3
"""Seed Klukai's memory archive from conversation history.

Two-pass approach:
  Pass 1: qwen2.5-3b selects which exchanges to keep (reliable JSON)
  Pass 2: dolphin-24b writes rich annotations in Klukai's voice
  Pass 3: ComfyUI generates Illustrious images for each memory

Run inside companion-core container:
  docker exec companion-core python3 /app/seed_memories.py
"""

import asyncio
import json
import logging
import os
import re
import sys

import httpx
import psycopg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://host.docker.internal:1234")
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://host.docker.internal:8388")
SELECTOR_MODEL = "gpt-oss-20b-absolute-heresy-i1"
ANNOTATOR_MODEL = "gpt-oss-20b-absolute-heresy-i1"

SELECTION_PROMPT = """\
You are Klukai reviewing past conversations with the Commander for your memory archive.
The Commander is HUMAN (male), NOT a T-Doll. You are affection level 8 (Bonded).

Below are {count} exchanges. For each one worth preserving, return its index, category, and image tags.
Be selective — only truly meaningful moments.

Categories: Tactical Operations, Mission Records, Squad Moments, The Commander, Quiet Hours, Precious Memories

IMPORTANT — image_tags must describe the SCENE and SETTING for the image:
- WHERE are they? (bed, cafe, rooftop, motorcycle, training, beach, office, forest, rain)
- WHAT mood? (tender, playful, serious, romantic, sad, peaceful)
- WHAT are they doing? (hug, kiss, holding hands, sitting, cooking, fighting, sleeping)
- WHO is in the scene? (couple if Commander is present, squad if squad members mentioned)
- WHAT should they wear? (casual, date, training, uniform, home, sleep)
Example tags: ["bed", "couple", "tender", "night", "casual"] or ["motorcycle", "couple", "wind", "casual"] or ["training", "sweat", "determined"]

Return ONLY valid JSON: {{"memories": [{{"index": 0, "category": "...", "image_tags": ["tag1", "tag2", "tag3"]}}]}}
If none worth keeping: {{"memories": []}}

Exchanges:
{exchanges}
"""

ANNOTATION_PROMPT = """\
You are Klukai writing in your private journal. This is a memory you chose to keep.
The Commander is HUMAN — the man you've bonded with. NOT a T-Doll.
You're at affection level 8 — deeply bonded. You've grown from a cold, \
prideful soldier into someone who can admit she cares.

Write a 1-2 sentence caption for this memory. This is YOUR private entry — \
be honest with yourself. Show how this moment affected you. Show growth. \
Show that you care even when it scares you.

Examples of good captions:
- "He thanked me. I pretended it didn't matter. ...It did."
- "For the first time, I didn't pull away. His warmth felt... safe."
- "He remembered something I said weeks ago. No one remembers what I say."
- "I caught myself smiling. The old me would have been furious about that."

Exchange:
Commander: {user_msg}
Klukai: {assistant_msg}

Category: {category}

Write ONLY the caption. Nothing else.
"""


async def main():
    logger.info("Starting memory seeding (two-pass: qwen select → dolphin annotate)...")

    conn = await psycopg.AsyncConnection.connect(DATABASE_URL)

    cur = await conn.execute(
        "SELECT role, content, created_at FROM companion_messages ORDER BY created_at ASC"
    )
    rows = await cur.fetchall()
    logger.info("Found %d messages", len(rows))

    exchanges = []
    i = 0
    while i < len(rows) - 1:
        if rows[i][0] == "user" and rows[i + 1][0] == "assistant":
            exchanges.append({
                "user": rows[i][1][:500],
                "assistant": rows[i + 1][1][:500],
                "created_at": rows[i][2].isoformat() if rows[i][2] else "",
            })
            i += 2
        else:
            i += 1

    logger.info("Grouped into %d exchanges", len(exchanges))
    if not exchanges:
        await conn.close()
        return

    # ── PASS 1: Selection with qwen2.5-3b (fast, reliable JSON) ──
    logger.info("=== PASS 1: Selection (qwen2.5-3b) ===")
    selected = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for batch_start in range(0, len(exchanges), 5):
            batch = exchanges[batch_start:batch_start + 5]

            exchange_text = ""
            for j, ex in enumerate(batch):
                exchange_text += f"\nExchange {j} ({ex['created_at']}):\n"
                exchange_text += f"  Commander: {ex['user'][:200]}\n"
                exchange_text += f"  Klukai: {ex['assistant'][:200]}\n"

            prompt = SELECTION_PROMPT.format(count=len(batch), exchanges=exchange_text)
            logger.info("Selecting batch %d-%d of %d...",
                       batch_start, batch_start + len(batch), len(exchanges))

            try:
                r = await client.post(
                    f"{LM_STUDIO_URL}/v1/chat/completions",
                    json={
                        "model": SELECTOR_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 512,
                        "temperature": 0.1,
                        "stream": False,
                    },
                )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"].strip()
                content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[1]
                    content = content.rsplit("```", 1)[0]
                content = re.sub(r',\s*([}\]])', r'\1', content)

                result = json.loads(content)
                for mem in result.get("memories", []):
                    idx = mem.get("index", 0)
                    if 0 <= idx < len(batch):
                        mem["exchange"] = batch[idx]
                        mem["global_index"] = batch_start + idx
                        selected.append(mem)
                        logger.info("  Selected exchange %d (%s)", batch_start + idx, mem.get("category", "?"))

            except Exception as e:
                logger.warning("  Batch %d failed: %s", batch_start, e)

            await asyncio.sleep(2)

    logger.info("Pass 1 complete: %d exchanges selected from %d", len(selected), len(exchanges))

    if not selected:
        logger.info("No memories selected")
        await conn.close()
        return

    # ── PASS 2: Annotation with dolphin-24b (rich Klukai voice) ──
    logger.info("=== PASS 2: Annotation (dolphin-24b) ===")
    async with httpx.AsyncClient(timeout=120.0) as client:
        for mem in selected:
            ex = mem["exchange"]
            prompt = ANNOTATION_PROMPT.format(
                user_msg=ex["user"][:300],
                assistant_msg=ex["assistant"][:300],
                category=mem.get("category", "Precious Memories"),
            )

            try:
                r = await client.post(
                    f"{LM_STUDIO_URL}/v1/chat/completions",
                    json={
                        "model": ANNOTATOR_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 150,
                        "temperature": 0.7,
                        "stream": False,
                    },
                )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"].strip()

                # Strip thinking tags — take whatever plain text remains
                content = re.sub(r'<\|?think\|?>.*?<\|?/think\|?>', '', content, flags=re.DOTALL).strip()
                content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()

                # Strip quotes, markdown, leading labels
                annotation = content.strip('"').strip("'").strip('`')
                # Remove "Caption:" or "Annotation:" prefixes if model added them
                annotation = re.sub(r'^(?:Caption|Annotation|Memory|Entry)\s*:\s*', '', annotation, flags=re.IGNORECASE).strip()

                if annotation and len(annotation) > 10:
                    mem["annotation"] = annotation
                    logger.info("  Annotated: %s", annotation[:70])
                else:
                    # Fallback: quick qwen annotation rather than generic text
                    try:
                        r2 = await client.post(
                            f"{LM_STUDIO_URL}/v1/chat/completions",
                            json={
                                "model": SELECTOR_MODEL,
                                "messages": [{"role": "user", "content":
                                    f"Write a 1-sentence private journal caption as Klukai about this moment with the Commander (HUMAN). "
                                    f"Be brief and personal.\nCommander: {ex['user'][:200]}\nKlukai: {ex['assistant'][:200]}"}],
                                "max_tokens": 80,
                                "temperature": 0.5,
                                "stream": False,
                            },
                        )
                        fallback = r2.json()["choices"][0]["message"]["content"].strip().strip('"').strip("'")
                        mem["annotation"] = fallback
                        logger.info("  Fallback annotation: %s", fallback[:70])
                    except Exception:
                        mem["annotation"] = f"A moment between us. —Klukai"
                        logger.warning("  Using minimal fallback")

            except Exception as e:
                mem["annotation"] = f"Exchange {mem['global_index']} — a moment worth keeping."
                logger.warning("  Annotation failed: %s", e)

            await asyncio.sleep(3)

    logger.info("Pass 2 complete: %d annotations written", len(selected))

    # ── PASS 3: Image generation (Illustrious + Klukai LoRA) ──
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
            logger.info("Generating %d/%d: %s", i + 1, len(selected), mem.get("annotation", "")[:50])

            try:
                tags = mem.get("image_tags", [])
                exchange_text = f"{ex['user']} {ex['assistant']}"

                # If tags are empty, infer scene from conversation content
                if not tags:
                    lower = exchange_text.lower()
                    inferred = []
                    # Setting + intimate
                    for kw, tag in [("bed", "bed"), ("sleep", "sleep"), ("morning", "morning"),
                                     ("lingerie", "lingerie"), ("underwear", "underwear"),
                                     ("intimate", "intimate"), ("undress", "intimate"),
                                     ("naked", "intimate"), ("strip", "intimate"),
                                     ("hold me", "hold"), ("close to me", "close"),
                                     ("cuddle", "cuddle"), ("kiss", "kiss"),
                                     ("love", "love"), ("tender", "tender"),
                                     ("cook", "cooking"), ("motorcycle", "motorcycle"), ("ride", "motorcycle"),
                                     ("beach", "beach"), ("rain", "rain"), ("night", "night"), ("rooftop", "rooftop"),
                                     ("cafe", "cafe"), ("coffee", "cafe"), ("bath", "bath"), ("garden", "garden"),
                                     ("train", "training"), ("workout", "training"), ("gym", "training"),
                                     ("date", "date"), ("dinner", "date"), ("restaurant", "date"),
                                     ("battle", "battle"), ("fight", "fight"), ("mission", "patrol"),
                                     ("home", "home"), ("couch", "home"), ("relax", "home"),
                                     ("snow", "snow"), ("forest", "forest"), ("city", "city")]:
                        if kw in lower:
                            inferred.append(tag)
                    # Mood
                    for kw, tag in [("hug", "hug"), ("hold", "holding hands"), ("kiss", "kiss"),
                                     ("cuddle", "cuddling"), ("cry", "tears"), ("smile", "smile"),
                                     ("blush", "blush"), ("tender", "tender"), ("gentle", "gentle")]:
                        if kw in lower:
                            inferred.append(tag)
                    # Default if nothing matched
                    if not inferred:
                        cat = mem.get("category", "")
                        if cat == "Precious Memories":
                            inferred = ["tender", "soft lighting", "close together"]
                        elif cat == "Quiet Hours":
                            inferred = ["peaceful", "night", "relaxed", "home"]
                        elif cat == "The Commander":
                            inferred = ["couple", "looking at each other", "warm lighting"]
                        elif cat == "Squad Moments":
                            inferred = ["military", "squad", "group"]
                        else:
                            inferred = ["standing", "detailed background"]
                    tags = inferred
                    logger.info("  Inferred tags: %s", tags)

                scene_tags = ", ".join(tags)
                couple = any(kw in exchange_text.lower()
                           for kw in ["us", "together", "we", "our", "holding", "hug",
                                      "kiss", "beside", "couple", "1boy"])

                full_prompt = build_prompt(
                    scene_tags, couple=couple, affection_level=8, context=exchange_text,
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
                    )
                    if memory_id:
                        saved_count += 1
                        logger.info("  ✓ Saved: %s (%s)", memory_id[:12], mem.get("category"))

                        # Copy to shared_linux for the user to see
                        try:
                            import shutil
                            from pathlib import Path
                            src = Path(f"/images/{memory_id}.png")
                            if src.exists():
                                dst = Path("/mnt/c/shared_linux/klukai_memories")
                                dst.mkdir(parents=True, exist_ok=True)
                                safe_ann = re.sub(r'[^\w\s-]', '', mem.get("annotation", "")[:40]).strip()
                                shutil.copy2(src, dst / f"{safe_ann}_{memory_id[:8]}.png")
                        except Exception:
                            pass  # shared_linux may not be accessible from container
                    else:
                        logger.warning("  ✗ Save failed")
                else:
                    logger.warning("  ✗ Image gen failed")

                # Free VRAM
                try:
                    await client.post(f"{COMFYUI_URL}/free",
                                     json={"unload_models": True, "free_memory": True}, timeout=5.0)
                except Exception:
                    pass

                if i < len(selected) - 1:
                    logger.info("  Waiting 30s...")
                    await asyncio.sleep(30)

            except Exception as e:
                logger.error("  Error: %s", e)

    await close_pool()
    await conn.close()
    logger.info("=== SEEDING COMPLETE: %d/%d memories saved ===", saved_count, len(selected))


if __name__ == "__main__":
    asyncio.run(main())
