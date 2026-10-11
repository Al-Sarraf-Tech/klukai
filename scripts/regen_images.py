#!/usr/bin/env python3
"""Regenerate missing memory images from stored prompts via ComfyUI.

A memory is "missing" when the file the app serves — the row's ``filename``
(``<id>.webp``) — is not on the volume. A served image is never replaced. For a
missing one, the lossless ``<id>.png`` is reused when it still exists (no GPU
render, same picture she remembers) and generated otherwise; then the served
WebP and the row's ``thumb_filename`` are written exactly as save_image does.
"""

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
from app.memory_archive import _generate_thumbnail  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("regen")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
IMAGES_DIR = Path(os.environ.get("IMAGES_DIR", "/images"))


async def generate_image(prompt: str) -> bytes | None:
    """Regenerate through the same bounded lease path as live Klukai renders."""
    return await _generate_image(prompt)


def write_served_files(
    mid: str, img: bytes, filename: str, thumb_filename: str | None
) -> None:
    """Write the archive PNG, the served WebP and the served thumbnail.

    Mirrors app.memory_archive.save_image: lossless PNG kept as the archive
    copy, full-resolution WebP (q95) served, 320px WebP thumbnail for the grid.
    """
    from PIL import Image

    png_path = IMAGES_DIR / f"{mid}.png"
    if not png_path.exists():
        png_path.write_bytes(img)
    webp_path = IMAGES_DIR / filename
    with Image.open(io.BytesIO(img)) as full:
        full.convert("RGB").save(webp_path, "WEBP", quality=95, method=6)
    if thumb_filename:
        _generate_thumbnail(webp_path, IMAGES_DIR / thumb_filename, width=320)
    else:
        logger.warning("  %s has no thumb_filename; thumbnail not written", mid[:12])


async def main():
    if not DATABASE_URL:
        print("Set DATABASE_URL env var")
        sys.exit(1)

    conn = await psycopg.AsyncConnection.connect(
        DATABASE_URL, autocommit=True, row_factory=dict_row
    )

    cur = await conn.execute(
        "SELECT id::text, prompt, filename, thumb_filename FROM companion_memories "
        "WHERE kept = true ORDER BY created_at ASC"
    )
    rows = await cur.fetchall()

    # Missing means the SERVED file is absent. An existing served image is
    # never regenerated or overwritten.
    missing = [
        r for r in rows
        if not r.get("filename") or not (IMAGES_DIR / r["filename"]).exists()
    ]

    logger.info("Total kept memories: %d, Missing served files: %d", len(rows), len(missing))

    regenerated = 0
    for i, m in enumerate(missing):
        mid = m["id"]
        filename = m.get("filename")
        if not filename:
            logger.warning("Skip %s — row has no filename", mid[:12])
            continue

        png_path = IMAGES_DIR / f"{mid}.png"
        if png_path.exists():
            logger.info("[%d/%d] Re-encoding %s from its archive PNG...", i + 1, len(missing), mid[:12])
            img = png_path.read_bytes()
        else:
            prompt = m.get("prompt", "")
            if not prompt:
                logger.warning("Skip %s — no prompt", mid[:12])
                continue
            logger.info("[%d/%d] Regenerating %s...", i + 1, len(missing), mid[:12])
            img = await generate_image(prompt)
            # Small delay between generations
            await asyncio.sleep(1)

        if img:
            write_served_files(mid, img, filename, m.get("thumb_filename"))
            regenerated += 1
            logger.info("  Saved %s (%d KB)", mid[:12], len(img) // 1024)
        else:
            logger.error("  FAILED %s", mid[:12])

    await conn.close()
    logger.info("Done. Restored %d of %d missing images.", regenerated, len(missing))


if __name__ == "__main__":
    asyncio.run(main())
