#!/usr/bin/env python3
"""Regenerate missing memory images from stored prompts via ComfyUI."""

import asyncio
import json
import logging
import os
import random
import sys
import uuid
from copy import deepcopy
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("regen")

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://192.168.50.2:8188")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
IMAGES_DIR = "/images"

LORA_FILE = "Klukai_GFL2_IL-03.safetensors"
CHECKPOINT = "noobai_xl_v1.safetensors"
NEGATIVE = (
    "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, "
    "fewer digits, cropped, worst quality, low quality, normal quality, "
    "jpeg artifacts, signature, watermark, username, blurry, deformed, ugly, "
    "extra arms, extra legs, fused limbs, limbs through body, clipping, "
    "multiple people, multiple boys, multiple girls, clone, twin, "
    "two heads, two faces, extra faces, disfigured, "
    "interlocking limbs, overlapping bodies, merged bodies"
)

WORKFLOW = {
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CHECKPOINT}},
    "10": {"class_type": "LoraLoader", "inputs": {
        "lora_name": LORA_FILE, "strength_model": 0.75, "strength_clip": 0.75,
        "model": ["4", 0], "clip": ["4", 1],
    }},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 832, "height": 1216, "batch_size": 1}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["10", 1]}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": NEGATIVE, "clip": ["10", 1]}},
    "3": {"class_type": "KSampler", "inputs": {
        "seed": 0, "steps": 25, "cfg": 7.0, "sampler_name": "euler_ancestral",
        "scheduler": "normal", "denoise": 1.0,
        "model": ["10", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0],
    }},
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "regen", "images": ["8", 0]}},
}


async def generate_image(prompt: str) -> bytes | None:
    workflow = deepcopy(WORKFLOW)
    workflow["6"]["inputs"]["text"] = prompt
    workflow["3"]["inputs"]["seed"] = random.randint(0, 2**32 - 1)

    payload = {"prompt": workflow, "client_id": str(uuid.uuid4())}
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(f"{COMFYUI_URL}/prompt", json=payload)
        if resp.status_code != 200:
            return None
        prompt_id = resp.json()["prompt_id"]

        for _ in range(300):
            await asyncio.sleep(1)
            hist = await client.get(f"{COMFYUI_URL}/history/{prompt_id}")
            if hist.status_code != 200:
                continue
            history = hist.json()
            if prompt_id not in history:
                continue
            outputs = history[prompt_id].get("outputs", {})
            if "9" in outputs and outputs["9"].get("images"):
                img_info = outputs["9"]["images"][0]
                img_resp = await client.get(f"{COMFYUI_URL}/view", params={
                    "filename": img_info["filename"],
                    "subfolder": img_info.get("subfolder", ""),
                    "type": img_info.get("type", "output"),
                })
                if img_resp.status_code == 200:
                    return img_resp.content
                break
    return None


def make_thumbnail(img_bytes: bytes) -> bytes:
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(img_bytes))
    img.thumbnail((320, 320))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def main():
    if not DATABASE_URL:
        print("Set DATABASE_URL env var")
        sys.exit(1)

    conn = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)

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
