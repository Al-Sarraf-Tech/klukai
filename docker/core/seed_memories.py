#!/usr/bin/env python3
"""Seed Klukai's memory archive from conversation history.

Run inside companion-core container:
  docker exec companion-core python3 /app/seed_memories.py
"""

import asyncio
import json
import logging
import os
import re
import sys
import time

import httpx
import psycopg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://host.docker.internal:1234")
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://host.docker.internal:8388")
MODEL = "qwen2.5-3b-instruct"

CURATION_PROMPT = """\
You are Klukai, SST-05 frame T-Doll and squad leader of H.I.D.E. 404. You are reviewing \
your past conversations with the Commander to build your personal memory archive.

Your current affection level with the Commander is 8 (Bonded). You care deeply about him \
but express it through your cool, professional exterior with moments of genuine warmth.

Below are {count} exchanges. For each one, decide if this is a moment worth preserving \
in your archive. Be selective — only keep truly meaningful moments. Consider:
- Moments of genuine connection or vulnerability
- Times the Commander showed he cares
- Important mission discussions
- Squad interactions worth remembering
- Quiet moments that meant something to you

For each exchange you want to KEEP, provide:
- "index": the exchange number (0-based)
- "annotation": 1-2 sentence caption written as you, Klukai (first person, in character)
- "category": one of: Tactical Operations, Mission Records, Squad Moments, The Commander, Quiet Hours, Precious Memories
- "image_tags": list of scene/setting tags for the image (e.g., ["bed", "tender", "night"])

Return ONLY valid JSON: {{"memories": [...]}}
If none are worth keeping, return: {{"memories": []}}

Exchanges:
{exchanges}
"""


async def main():
    logger.info("Starting memory seeding from conversation history...")

    # Connect to DB directly (not via app pool — we need it for reading messages)
    conn = await psycopg.AsyncConnection.connect(DATABASE_URL)

    # Read all messages
    cur = await conn.execute(
        "SELECT role, content, created_at FROM companion_messages "
        "ORDER BY created_at ASC"
    )
    rows = await cur.fetchall()
    logger.info("Found %d messages", len(rows))

    # Group into exchanges (user + assistant pairs)
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
        logger.info("No exchanges to process")
        await conn.close()
        return

    # Process in batches of 5
    kept_memories = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for batch_start in range(0, len(exchanges), 5):
            batch = exchanges[batch_start:batch_start + 5]

            # Format exchanges for the prompt
            exchange_text = ""
            for j, ex in enumerate(batch):
                exchange_text += f"\nExchange {j} ({ex['created_at']}):\n"
                exchange_text += f"  Commander: {ex['user']}\n"
                exchange_text += f"  Klukai: {ex['assistant']}\n"

            prompt = CURATION_PROMPT.format(
                count=len(batch),
                exchanges=exchange_text,
            )

            logger.info(
                "Processing batch %d-%d of %d...",
                batch_start, batch_start + len(batch), len(exchanges),
            )

            try:
                r = await client.post(
                    f"{LM_STUDIO_URL}/v1/chat/completions",
                    json={
                        "model": MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 1024,
                        "temperature": 0.3,
                        "stream": False,
                    },
                )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"].strip()

                # Parse JSON (handle markdown blocks and think tags)
                content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
                content = content.strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[1]
                    content = content.rsplit("```", 1)[0]

                result = json.loads(content)
                memories = result.get("memories", [])

                for mem in memories:
                    idx = mem.get("index", 0)
                    if 0 <= idx < len(batch):
                        mem["exchange"] = batch[idx]
                        kept_memories.append(mem)
                        logger.info(
                            "  Klukai chose exchange %d: %s",
                            batch_start + idx,
                            mem.get("annotation", "")[:60],
                        )

            except Exception as e:
                logger.warning("Batch processing failed: %s", e)

            # Rate limit between LLM calls
            await asyncio.sleep(5)

    logger.info(
        "Klukai selected %d memories from %d exchanges",
        len(kept_memories), len(exchanges),
    )

    if not kept_memories:
        logger.info("No memories selected")
        await conn.close()
        return

    # Import from the app modules (running inside the container at /app)
    sys.path.insert(0, "/app")
    from app.image_gen import build_prompt, generate_image
    from app.memory_archive import save_image
    from app.db import init_pool, close_pool

    # Initialize DB pool (required by save_image / get_conn_autocommit)
    await init_pool(min_size=1, max_size=3)

    async with httpx.AsyncClient(timeout=300.0) as client:
        for i, mem in enumerate(kept_memories):
            logger.info(
                "Generating image %d/%d: %s",
                i + 1, len(kept_memories),
                mem.get("annotation", "")[:50],
            )

            try:
                # Build prompt from tags
                tags = mem.get("image_tags", [])
                scene_tags = ", ".join(tags) if tags else "standing, looking at viewer"

                # Detect if it's a couple scene
                exchange_text = f"{mem['exchange']['user']} {mem['exchange']['assistant']}"
                couple = any(
                    kw in exchange_text.lower()
                    for kw in ["us", "together", "we", "our", "holding", "hug", "kiss", "beside"]
                )

                full_prompt = build_prompt(
                    scene_tags,
                    couple=couple,
                    affection_level=8,
                    context=exchange_text,
                )

                logger.info("  Prompt: %s", full_prompt[:150])

                # Generate image
                img_bytes = await generate_image(full_prompt)

                if img_bytes:
                    # Save to archive
                    curation = {
                        "keep": True,
                        "annotation": mem.get("annotation", ""),
                        "category": mem.get("category", "Precious Memories"),
                        "image_tags": tags,
                    }

                    memory_id = await save_image(
                        img_bytes, full_prompt, "seed",
                        mood="tender", affection_level=8,
                        curation=curation,
                    )

                    if memory_id:
                        logger.info(
                            "  Memory saved: %s (%s)",
                            memory_id[:12], mem.get("category"),
                        )
                    else:
                        logger.warning("  Failed to save memory")
                else:
                    logger.warning("  Image generation failed")

                # Free ComfyUI VRAM (generate_image already does this internally,
                # but an extra call after the sleep ensures LM Studio can reclaim VRAM)
                try:
                    await client.post(
                        f"{COMFYUI_URL}/free",
                        json={"unload_models": True, "free_memory": True},
                        timeout=5.0,
                    )
                except Exception:
                    pass

                # Rate limit between image generations
                if i < len(kept_memories) - 1:
                    logger.info("  Waiting 30s for VRAM to settle...")
                    await asyncio.sleep(30)

            except Exception as e:
                logger.error("  Failed to process memory: %s", e)

    await close_pool()
    await conn.close()

    logger.info("Seeding complete! %d memories generated.", len(kept_memories))


if __name__ == "__main__":
    asyncio.run(main())
