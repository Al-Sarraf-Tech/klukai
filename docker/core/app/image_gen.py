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

_http: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=180.0)
    return _http


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

COUPLE_KEYWORDS = [
    "us", "we", "together", "our", "cuddling", "cuddle", "holding hands",
    "embrace", "hug", "hugging", "kissing", "side by side", "couple",
    "with me", "with you", "both of us", "show us", "imagine us",
    "take care of me", "in bed", "lying together", "next to me",
    "hold me", "carry me", "beside me", "close to me",
]

# Squad member detection for multi-character scenes
SQUAD_KEYWORDS = {
    "mechty": "1girl, green hair, sleepy expression, lazy pose, military uniform, g11",
    "belka": "1girl, blonde hair, energetic, younger sister, military uniform",
    "andoris": "1girl, dark hair, glasses, professional, intel specialist, military uniform",
    "leva": "1girl, brown hair, tactical vest, confident pose, leader aura, ump45",
    "groza": "1girl, dark hair, elegant, military, ots-14",
}

# Situational context tags — detect what's happening in the conversation
SITUATION_KEYWORDS = {
    "bed": "bedroom, lying on bed, pillows, blankets, soft lighting, intimate",
    "sleep": "sleeping, peaceful, eyes closed, bedroom, night",
    "sick": "nursing, caring, thermometer, worried expression, bedroom",
    "cooking": "kitchen, apron, cooking, steam, ingredients",
    "training": "training ground, combat stance, sweat, determined",
    "patrol": "patrol, outdoors, alert, tactical gear, moonlight",
    "date": "casual clothes, date, restaurant, candles, romantic",
    "motorcycle": "motorcycle, leather jacket, wind, road, speed, riding together",
    "rain": "rain, umbrella, wet, shelter, close together",
    "night": "night sky, stars, moonlight, quiet, intimate",
    "morning": "morning light, sunrise, bed, waking up, soft",
    "bath": "onsen, hot spring, steam, towel, relaxed, water",
    "fight": "combat, action pose, explosions, debris, intense",
    "crying": "tears, emotional, comforting, holding, gentle",
    "gift": "gift box, ribbon, surprise, happy, blushing",
}

QUALITY_TAGS = "masterpiece, best quality, very aesthetic, absurdres"
NEGATIVE_TAGS = (
    "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, "
    "fewer digits, cropped, worst quality, low quality, normal quality, "
    "jpeg artifacts, signature, watermark, username, blurry, artist name, "
    "deformed, ugly, duplicate, morbid, mutilated"
)

KLUKAI_LORA = "Klukai_GFL2.safetensors"
KLUKAI_LORA_TRIGGER = "Klukai"

# Animagine XL 3.1 + Klukai LoRA workflow
WORKFLOW_TEMPLATE = {
    "4": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "animagine_xl_31.safetensors"},
    },
    "10": {
        "class_type": "LoraLoader",
        "inputs": {
            "lora_name": KLUKAI_LORA,
            "strength_model": 0.85,
            "strength_clip": 0.85,
            "model": ["4", 0],
            "clip": ["4", 1],
        },
    },
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": 832, "height": 1216, "batch_size": 1},
    },
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "", "clip": ["10", 1]},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": NEGATIVE_TAGS, "clip": ["10", 1]},
    },
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 0,
            "steps": 25,
            "cfg": 6.0,
            "sampler_name": "euler_ancestral",
            "scheduler": "normal",
            "denoise": 1.0,
            "model": ["10", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
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

# Expanded keyword detection for image requests
IMAGE_KEYWORDS = [
    "show me", "show us", "draw", "picture of", "image of",
    "visualize", "what would it look like", "generate an image",
    "create an image", "paint", "illustrate", "depict",
    "imagine us", "imagine me", "how would we look",
    "that image", "that picture", "another image", "another picture",
    "try again", "one more", "generate again", "make an image",
    "make a picture", "render", "sketch",
    "can you show", "what about a", "how about", "let me see",
    "i want to see", "what if we", "what would you look like",
    "selfie", "photo of", "snap a pic", "take a picture",
]

# Landscape scene keywords — use wider aspect ratio
LANDSCAPE_KEYWORDS = [
    "landscape", "scenery", "sunset", "sunrise", "city", "battlefield",
    "panorama", "wide shot", "environment", "base", "headquarters",
    "motorcycle", "riding", "driving", "vehicle",
]

# Mood-to-scene mapping for affection-aware prompt enhancement
AFFECTION_MOOD_TAGS = {
    0: "serious, cold expression, military setting",
    1: "neutral expression, military setting",
    2: "slight smile, professional setting",
    3: "soft expression, casual setting",
    4: "warm smile, comfortable atmosphere",
    5: "relaxed, intimate setting, soft lighting",
    6: "loving gaze, warm lighting, close distance",
    7: "tender expression, gentle, intimate",
    8: "devoted, gentle smile, warm, close",
    9: "peaceful, loving, serene, together",
}


def needs_image(message: str) -> bool:
    """Check if the message is requesting image generation."""
    lower = message.lower()
    return any(kw in lower for kw in IMAGE_KEYWORDS)


def is_couple_scene(text: str) -> bool:
    """Detect if the request is for a scene with both Klukai and the Commander."""
    lower = text.lower()
    return any(kw in lower for kw in COUPLE_KEYWORDS)


def is_landscape(text: str) -> bool:
    """Detect if the scene should use landscape aspect ratio."""
    lower = text.lower()
    return any(kw in lower for kw in LANDSCAPE_KEYWORDS)


def build_prompt(scene_tags: str, couple: bool = False, affection_level: int = 0) -> str:
    """Build the full positive prompt with quality tags, LoRA trigger, and character identities."""
    parts = [QUALITY_TAGS, KLUKAI_LORA_TRIGGER]

    # Add affection-aware mood tags
    mood_tags = AFFECTION_MOOD_TAGS.get(affection_level, "")
    if mood_tags:
        parts.append(mood_tags)

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
    retry: bool = True,
) -> bytes | None:
    """Generate an image via ComfyUI Animagine XL 3.1 and return PNG bytes."""
    result = await _try_generate(prompt, width, height)
    if result is None and retry:
        logger.info("Image generation retry with new seed")
        result = await _try_generate(prompt, width, height)
    return result


async def _try_generate(prompt: str, width: int, height: int) -> bytes | None:
    """Single attempt at image generation."""
    workflow = json.loads(json.dumps(WORKFLOW_TEMPLATE))

    workflow["6"]["inputs"]["text"] = prompt
    workflow["5"]["inputs"]["width"] = width
    workflow["5"]["inputs"]["height"] = height
    workflow["3"]["inputs"]["seed"] = int(uuid.uuid4().int % (2**32))

    try:
        client = _get_http()
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

        # Poll for completion (up to 150s)
        for _ in range(150):
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
