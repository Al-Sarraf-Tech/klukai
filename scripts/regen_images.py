#!/usr/bin/env python3
"""Regenerate missing memory images from stored prompts via ComfyUI."""

import asyncio
import io
import logging
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

# Reuse the production image path.  It owns the authenticated gateway lease,
# facade credentials, timeout/interrupt behavior, and VRAM cleanup.  Keeping a
# second raw ComfyUI client here would bypass the cross-process GPU exclusion.
CORE_DIR = Path(__file__).resolve().parents[1] / "docker" / "core"
sys.path.insert(0, str(CORE_DIR))
from app.image_gen import generate_image as _generate_image  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("regen")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
IMAGES_DIR = "/images"


async def generate_image(prompt: str) -> bytes | None:
    """Regenerate through the same bounded lease path as live Klukai renders."""
    return await _generate_image(prompt)


def make_thumbnail(img_bytes: bytes) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(img_bytes))
    img.thumbnail((320, 320))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def main():
    if not DATABASE_URL:
        print("Set DATABASE_URL env var")
        sys.exit(1)

    conn = await psycopg.AsyncConnection.connect(
        DATABASE_URL, autocommit=True, row_factory=dict_row
    )

    cur = await conn.execute(
        "SELECT id::text, prompt FROM companion_memories ORDER BY created_at ASC"
    )
    rows = await cur.fetchall()

    missing = []
    for r in rows:
        if not os.path.exists(f"{IMAGES_DIR}/{r['id']}.png"):
            missing.append(r)

    logger.info("Total memories: %d, Missing files: %d", len(rows), len(missing))

    for i, m in enumerate(missing):
        mid = m["id"]
        prompt = m.get("prompt", "")
        if not prompt:
            logger.warning("Skip %s — no prompt", mid[:12])
            continue

        logger.info("[%d/%d] Regenerating %s...", i + 1, len(missing), mid[:12])
        img = await generate_image(prompt)
        if img:
            Path(f"{IMAGES_DIR}/{mid}.png").write_bytes(img)
            thumb = make_thumbnail(img)
            Path(f"{IMAGES_DIR}/{mid}_thumb.png").write_bytes(thumb)
            logger.info("  Saved %s (%d KB)", mid[:12], len(img) // 1024)
        else:
            logger.error("  FAILED %s", mid[:12])

        # Small delay between generations
        await asyncio.sleep(1)

    logger.info("Done. Regenerated %d images.", len(missing))


if __name__ == "__main__":
    asyncio.run(main())
