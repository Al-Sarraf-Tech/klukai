#!/usr/bin/env python3
"""Retired automatic album seeder.

Pictures are on demand only, so the service entry point is a no-op: it reads
no history, calls no model or renderer and changes no album, watermark or
memory record. The file stays because amarillo's
klukai-memory-archive-seed.service still runs it each morning; that unit can be
removed with this file.
"""

import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main() -> int:
    """Retired automatic album seeder: never read history or call a renderer."""
    logger.info("Album seeding disabled: pictures are on demand only")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
