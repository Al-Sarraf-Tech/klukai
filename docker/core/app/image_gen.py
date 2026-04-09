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
# Core identity is FIXED (face, hair, body). Outfit is selected per-scene.
KLUKAI_IDENTITY = (
    "1girl, hk416 \\(girls' frontline\\), silver hair, green eyes, long hair, ponytail, "
    "hair ornament, girls' frontline, slim waist, athletic body, toned, slender figure, long legs"
)
KLUKAI_DEFAULT_OUTFIT = "tactical clothes, black gloves, thighhighs, military"

# Scene-appropriate outfits — matched by keyword in conversation context
OUTFIT_MAP = {
    "bed": "white camisole, bare shoulders, relaxed",
    "sleep": "oversized t-shirt, bare legs, relaxed",
    "morning": "oversized t-shirt, messy hair, bare legs",
    "bath": "towel, wet hair, bare shoulders, steam",
    "beach": "white bikini, sarong, sandals",
    "swim": "one-piece swimsuit, wet hair",
    "date": "black dress, elegant, off-shoulder, heels, jewelry",
    "cafe": "casual blouse, skirt, relaxed fashion",
    "cooking": "apron over casual clothes, rolled sleeves",
    "cook": "apron over casual clothes, rolled sleeves",
    "training": "sports bra, compression shorts, sweat, athletic tape",
    "workout": "sports bra, compression shorts, sweat, athletic tape",
    "working out": "sports bra, compression shorts, sweat, athletic tape",
    "exercise": "sports bra, compression shorts, sweat",
    "gym": "sports bra, compression shorts, sweat",
    "casual": "off-shoulder sweater, jeans, sneakers",
    "home": "oversized hoodie, shorts, comfortable",
    "rain": "long coat, scarf, boots, umbrella",
    "snow": "winter coat, scarf, boots, gloves, warm breath",
    "motorcycle": "leather jacket, boots, wind-blown hair",
    "formal": "military dress uniform, medals, pristine",
    "dress": "elegant dress, jewelry, formal",
    "uniform": "tactical clothes, black gloves, thighhighs, military",
    "battle": "tactical gear, body armor, combat vest, rifle",
    "fight": "tactical gear, body armor, combat vest, rifle",
    "patrol": "tactical clothes, black gloves, thighhighs, military, alert",
}

COMMANDER_IDENTITY = (
    "1boy, male focus, masculine, short hair, dark hair, brown eyes, tan skin, "
    "strong build, tall, broad shoulders, male"
)
COMMANDER_DEFAULT_OUTFIT = "military uniform, commander, jacket"

COMMANDER_OUTFIT_MAP = {
    "bed": "shirtless, bare chest, relaxed",
    "sleep": "t-shirt, casual pants",
    "morning": "t-shirt, messy hair",
    "bath": "towel, bare chest, wet hair",
    "beach": "swim trunks, bare chest, sunglasses",
    "date": "dress shirt, slacks, rolled sleeves",
    "cafe": "casual jacket, t-shirt, jeans",
    "casual": "hoodie, jeans, sneakers",
    "home": "t-shirt, sweatpants, relaxed",
    "training": "tank top, athletic shorts, sweat",
    "workout": "tank top, athletic shorts, sweat",
    "motorcycle": "leather jacket, jeans, boots",
    "formal": "military dress uniform, medals",
    "rain": "long coat, boots",
    "snow": "winter jacket, scarf, boots",
    "battle": "tactical vest, combat gear, helmet",
    "fight": "tactical vest, combat gear",
}

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
    "deformed, ugly, duplicate, morbid, mutilated, "
    "thick thighs, wide hips, chubby, plump, fat, overweight, huge breasts, "
    "androgynous, feminine boy, crossdressing, male in female clothes"
)

KLUKAI_LORA = "Klukai_GFL2_IL-03.safetensors"
KLUKAI_LORA_TRIGGER = "Klukai"

# NoobAI-XL (Illustrious) + Klukai IL LoRA workflow
WORKFLOW_TEMPLATE = {
    "4": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "noobai_xl_v1.safetensors"},
    },
    "10": {
        "class_type": "LoraLoader",
        "inputs": {
            "lora_name": KLUKAI_LORA,
            "strength_model": 0.75,
            "strength_clip": 0.75,
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
    import re
    lower = text.lower()
    return any(re.search(r'\b' + re.escape(kw) + r'\b', lower) for kw in COUPLE_KEYWORDS)


def is_landscape(text: str) -> bool:
    """Detect if the scene should use landscape aspect ratio."""
    lower = text.lower()
    return any(kw in lower for kw in LANDSCAPE_KEYWORDS)


def _select_outfit(context: str, outfit_map: dict[str, str], default: str) -> str:
    """Pick the best outfit from a map based on conversation context keywords."""
    lower = context.lower()
    for keyword, outfit in outfit_map.items():
        if keyword in lower:
            return outfit
    return default


def build_prompt(
    scene_tags: str,
    couple: bool = False,
    affection_level: int = 0,
    context: str = "",
) -> str:
    """Build the full positive prompt with quality tags, LoRA trigger, and character identities.

    Args:
        scene_tags: Scene/action Danbooru tags.
        couple: Whether to include the Commander.
        affection_level: 0-9, affects mood expression tags.
        context: Recent conversation text for outfit selection.
    """
    parts = [QUALITY_TAGS, KLUKAI_LORA_TRIGGER]

    # Add affection-aware mood tags
    mood_tags = AFFECTION_MOOD_TAGS.get(affection_level, "")
    if mood_tags:
        parts.append(mood_tags)

    # Context for outfit matching: use full context (conversation + scene tags)
    outfit_context = f"{context} {scene_tags}" if context else scene_tags
    klukai_outfit = _select_outfit(outfit_context, OUTFIT_MAP, KLUKAI_DEFAULT_OUTFIT)

    if couple:
        commander_outfit = _select_outfit(outfit_context, COMMANDER_OUTFIT_MAP, COMMANDER_DEFAULT_OUTFIT)
        parts.append(COUPLE_TAGS)
        parts.append(f"{COMMANDER_IDENTITY}, {commander_outfit}")
        parts.append(KLUKAI_IDENTITY)
    else:
        parts.append(KLUKAI_IDENTITY)
    parts.append(klukai_outfit)
    parts.append(scene_tags)
    return ", ".join(parts)


async def check_comfyui_ready() -> bool:
    """Check if ComfyUI is reachable and has the queue clear."""
    try:
        client = _get_http()
        r = await client.get(f"{COMFYUI_URL}/queue", timeout=5.0)
        if r.status_code != 200:
            return False
        data = r.json()
        running = len(data.get("queue_running", []))
        pending = len(data.get("queue_pending", []))
        if running > 0 or pending > 0:
            logger.info("ComfyUI busy: %d running, %d pending", running, pending)
            return False
        return True
    except Exception:
        return False


async def _interrupt_comfyui() -> None:
    """Interrupt the current ComfyUI generation."""
    try:
        client = _get_http()
        await client.post(f"{COMFYUI_URL}/interrupt", timeout=5.0)
        logger.info("ComfyUI generation interrupted")
        await asyncio.sleep(1)  # Let it settle
    except Exception as e:
        logger.warning("ComfyUI interrupt failed: %s", e)


async def _free_comfyui_vram() -> None:
    """Unload ComfyUI models from VRAM after generation to free space for LM Studio."""
    try:
        await asyncio.sleep(2)  # Let ComfyUI finish post-processing before unloading
        client = _get_http()
        await client.post(
            f"{COMFYUI_URL}/free",
            json={"unload_models": True, "free_memory": True},
            timeout=5.0,
        )
        logger.info("ComfyUI VRAM freed after image generation")
    except Exception as e:
        logger.warning("ComfyUI VRAM free failed: %s", e)


async def generate_image(
    prompt: str,
    width: int = 832,
    height: int = 1216,
    retry: bool = True,
) -> bytes | None:
    """Generate an image via ComfyUI Animagine XL 3.1 and return PNG bytes."""
    try:
        result = await _try_generate(prompt, width, height)
        if result is None and retry:
            logger.info("Image generation retry — interrupting stale job and retrying")
            await _interrupt_comfyui()
            result = await _try_generate(prompt, width, height)
        return result
    finally:
        # Always free VRAM after gen so LM Studio can reclaim it
        await _free_comfyui_vram()


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

        # Poll for completion (up to 300s — first gen after model load can be slow)
        for _ in range(300):
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

        logger.warning("Image generation timed out after 300s")
        return None
    except Exception as e:
        logger.error("Image generation failed: %s", e)
        return None
