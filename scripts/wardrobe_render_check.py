#!/usr/bin/env python3
"""Render every wardrobe outfit once, for a human to eyeball against canon.

The skin names post-date the image model, so each outfit is carried by plain
Danbooru tags in config/personality.yaml (``costumes.<id>.image_tags``). This
renders one portrait per outfit through the production lease path so you can
check the tags actually produce the look (Cerulean Breaker = bikini +
surfboard, Astral Luminous = checkered rider jacket, ...).

Run inside companion-core (it has the gateway credentials), GPU host up:

    docker cp scripts/wardrobe_render_check.py companion-core:/tmp/
    docker exec companion-core python3 /tmp/wardrobe_render_check.py            # all
    docker exec companion-core python3 /tmp/wardrobe_render_check.py speed_star  # some
    docker cp companion-core:/tmp/wardrobe-check ./wardrobe-check

Writes /tmp/wardrobe-check/<id>.png plus prompts.txt. Never touches the
memory archive or any user data.
"""

import asyncio
import sys
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1] / "docker" / "core"
for candidate in (CORE_DIR, Path("/app")):
    if (candidate / "app").is_dir():
        sys.path.insert(0, str(candidate))
        break

from app import wardrobe  # noqa: E402
from app.image_gen import INTIMATE_MIN_LEVEL, build_prompt, generate_image  # noqa: E402

LEVEL = 5  # a mid-bond render: below the intimacy gate, like most of her days

OUT = Path("/tmp/wardrobe-check")


async def main(ids: list[str]) -> int:
    cat = wardrobe.catalog()
    wanted = ids or list(cat)
    OUT.mkdir(parents=True, exist_ok=True)
    failures = 0
    with (OUT / "prompts.txt").open("w") as log:
        for oid in wanted:
            if oid not in cat:
                print(f"skip {oid}: not in catalog")
                continue
            prompt = build_prompt("solo, standing, full body, looking at viewer, simple background",
                                  affection_level=LEVEL, costume=oid)
            log.write(f"{oid}\n{prompt}\n\n")
            img = await generate_image(prompt, sfw=LEVEL < INTIMATE_MIN_LEVEL)
            if not img:
                print(f"FAIL {oid}: no image (GPU host down or busy?)")
                failures += 1
                continue
            (OUT / f"{oid}.png").write_bytes(img)
            print(f"ok   {oid}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
