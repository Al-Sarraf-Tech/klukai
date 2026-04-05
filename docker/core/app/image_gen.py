"""Image generation via ComfyUI with Animagine XL 3.1 for anime-realistic scenes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid

import httpx

logger = logging.getLogger(__name__)

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://host.docker.internal:8388")

# Character identity tags — Danbooru format for Animagine XL 3.1
KLUKAI_TAGS = (
    "1girl, hk416 \\(girls' frontline\\), silver hair, green eyes, long hair, ponytail, "
    "hair ornament, tactical clothes, black gloves, thighhighs, military, "
    "girls' frontline"
)
COMMANDER_TAGS = (
    "1boy, male focus, short hair, dark hair, brown eyes, tan skin, strong build, "
    "military uniform, commander, jacket"
)
COUPLE_TAGS = "couple, 1boy, 1girl, hetero"

# Keywords that indicate a couple/together scene
COUPLE_KEYWORDS = [
    "us", "we", "together", "our", "cuddling", "cuddle", "holding hands",
    "embrace", "hug", "hugging", "kissing", "side by side", "couple",
    "with me", "with you", "both of us", "show us", "imagine us",
]

QUALITY_TAGS = "masterpiece, best quality, very aesthetic, absurdres"
NEGATIVE_TAGS = (
    "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, "
    "fewer digits, cropped, worst quality, low quality, normal quality, "
    "jpeg artifacts, signature, watermark, username, blurry, artist name, "
    "deformed, ugly, duplicate, morbid, mutilated"
)

# Animagine XL 3.1 workflow
WORKFLOW_TEMPLATE = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 0,
            "steps": 25,
            "cfg": 6.0,
            "sampler_name": "euler_ancestral",
            "scheduler": "normal",
            "denoise": 1.0,
            "model": ["4", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "4": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "animagine_xl_31.safetensors"},
    },
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": 832, "height": 1216, "batch_size": 1},
    },
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "", "clip": ["4", 1]},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": NEGATIVE_TAGS, "clip": ["4", 1]},
    },
    "8": {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
    },
    "9": {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "klukai_gen", "images": ["8", 0]},
    },
}

# Keywords that suggest image generation
IMAGE_KEYWORDS = [
    "show me", "show us", "draw", "picture of", "image of",
    "visualize", "what would it look like", "generate an image",
    "create an image", "paint", "illustrate", "depict",
    "imagine us", "imagine me", "how would we look",
    "that image", "that picture", "another image", "another picture",
    "try again", "one more", "generate again", "make an image",
    "make a picture", "render", "sketch",
]


def needs_image(message: str) -> bool:
    """Check if the message is requesting image generation."""
    lower = message.lower()
    return any(kw in lower for kw in IMAGE_KEYWORDS)


def is_couple_scene(text: str) -> bool:
    """Detect if the request is for a scene with both Klukai and the Commander."""
    lower = text.lower()
    return any(kw in lower for kw in COUPLE_KEYWORDS)


def build_prompt(scene_tags: str, couple: bool = False) -> str:
    """Build the full positive prompt with quality tags and character identities."""
    parts = [QUALITY_TAGS]
    if couple:
        parts.append(COUPLE_TAGS)
        parts.append(COMMANDER_TAGS)
        parts.append(KLUKAI_TAGS)
    else:
        parts.append(KLUKAI_TAGS)
    parts.append(scene_tags)
    return ", ".join(parts)


async def generate_image(
    prompt: str,
    width: int = 832,
    height: int = 1216,
) -> bytes | None:
    """Generate an image via ComfyUI Animagine XL 3.1 and return PNG bytes."""
    workflow = json.loads(json.dumps(WORKFLOW_TEMPLATE))

    # Set prompt, dimensions, random seed
    workflow["6"]["inputs"]["text"] = prompt
    workflow["5"]["inputs"]["width"] = width
    workflow["5"]["inputs"]["height"] = height
    workflow["3"]["inputs"]["seed"] = int(uuid.uuid4().int % (2**32))

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            # Queue the prompt
            r = await client.post(
                f"{COMFYUI_URL}/prompt",
                json={"prompt": workflow},
            )
            if r.status_code != 200:
                logger.error("ComfyUI queue failed: %s", r.text[:200])
                return None

            prompt_id = r.json().get("prompt_id")
            if not prompt_id:
                return None

            # Poll for completion (up to 120s)
            for _ in range(120):
                await asyncio.sleep(1)
                r = await client.get(f"{COMFYUI_URL}/history/{prompt_id}")
                if r.status_code == 200:
                    history = r.json()
                    if prompt_id in history:
                        outputs = history[prompt_id].get("outputs", {})
                        for output in outputs.values():
                            images = output.get("images", [])
                            if images:
                                img = images[0]
                                r2 = await client.get(
                                    f"{COMFYUI_URL}/view",
                                    params={
                                        "filename": img["filename"],
                                        "subfolder": img.get("subfolder", ""),
                                        "type": img.get("type", "output"),
                                    },
                                )
                                if r2.status_code == 200:
                                    logger.info("Image generated: %s (%d bytes)", img["filename"], len(r2.content))
                                    return r2.content
                        return None

            logger.warning("Image generation timed out")
            return None
    except Exception as e:
        logger.error("Image generation failed: %s", e)
        return None
