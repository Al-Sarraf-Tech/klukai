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
    "hair ornament, girls' frontline, slim waist, athletic body, toned, slender figure, "
    "long legs, beautiful detailed eyes, light blush, detailed skin, "
    "perfect anatomy, delicate collarbone, navel, medium breasts, elegant neck, "
    "soft lips, detailed face, expressive eyes, eyelashes"
)
KLUKAI_DEFAULT_OUTFIT = "tactical clothes, black gloves, thighhighs, military"

# Scene-appropriate outfits — matched by keyword in conversation context
OUTFIT_MAP = {
    # Intimate / risque
    "bed": "black lace lingerie, bare shoulders, lying on bed, seductive pose, soft lighting",
    "sleep": "sheer nightgown, bare legs, messy hair, sleepy, soft fabric",
    "morning": "oversized white shirt, bare legs, no pants, messy hair, sunlight through window",
    "bath": "bare shoulders, steam, wet skin, towel barely covering, water droplets, onsen",
    "underwear": "black lace bra, panties, confident pose, bare midriff, bedroom",
    "lingerie": "black lace lingerie, garter belt, thighhighs, seductive, elegant",
    "intimate": "sheer negligee, bare shoulders, soft lighting, vulnerability, bedroom",
    "tender": "white camisole, bare shoulders, soft expression, intimate lighting",
    "night": "silk slip dress, bare shoulders, moonlight, intimate, hair down",
    "love": "sheer nightgown, close together, intimate, warm lighting, vulnerable",
    "close": "white camisole, bare shoulders, gentle expression, close distance",
    "hold": "oversized shirt, bare legs, clinging, close together, warm",
    "cuddle": "tank top, shorts, bare legs, cuddling, blankets, warm, comfortable",
    "kiss": "elegant lingerie, close up, eyes closed, romantic, intimate",
    # NSFW — explicit scenes at high affection
    "sex": "nude, naked, bare skin, spread legs, bedroom, sweat, flushed, explicit, nsfw",
    "fuck": "nude, naked, bare skin, on bed, sweat, ecstasy, explicit, nsfw, moaning",
    "naked": "fully nude, naked, bare breasts, bare skin, confident pose, bedroom",
    "nude": "fully nude, naked, bare breasts, bare skin, artistic nude, soft lighting",
    "topless": "topless, bare breasts, bare skin, confident, bedroom, soft lighting",
    "after sex": "nude, lying in bed, messy hair, sweat, satisfied expression, afterglow, sheets",
    "orgasm": "nude, ecstasy, arched back, flushed skin, sweat, pleasure, nsfw",
    "shower": "nude, wet skin, water droplets, steam, shower, wet hair, sensual",
    # Beach / swim
    "beach": "white string bikini, sarong, sun-kissed skin, ocean, wet",
    "swim": "one-piece swimsuit, wet hair, water droplets, pool",
    # Date / elegant
    "date": "black backless dress, elegant, high slit, heels, jewelry, updo, evening",
    "dinner": "wine red dress, off-shoulder, candlelight, classy, romantic",
    "cafe": "cropped top, high-waist skirt, casual chic, sitting, coffee",
    # Active / athletic
    "cooking": "apron only, bare shoulders, kitchen, steam, playful",
    "cook": "apron only, bare shoulders, kitchen, steam, playful",
    "training": "sports bra, compression shorts, sweat, athletic tape, toned abs visible",
    "workout": "sports bra, compression shorts, sweat, toned abs, athletic",
    "working out": "sports bra, compression shorts, sweat, toned abs, athletic",
    "exercise": "sports bra, bike shorts, sweat, gym, determined",
    "gym": "sports bra, bike shorts, sweat, toned midriff, gym",
    # Casual / home
    "casual": "off-shoulder sweater, no bra, jeans, sneakers, relaxed",
    "home": "oversized hoodie, panties, bare legs, comfortable, lazy",
    "relax": "tank top, shorts, bare feet, comfortable, cozy",
    # Weather / outdoor
    "rain": "wet white shirt, clinging fabric, rain, umbrella, see-through",
    "snow": "winter coat, scarf, thigh-high boots, warm breath, cozy",
    "motorcycle": "leather jacket, unzipped, crop top underneath, boots, wind-blown hair",
    # Military / formal
    "formal": "military dress uniform, medals, pristine, sharp",
    "dress": "elegant evening gown, high slit, backless, jewelry, sophisticated",
    "uniform": "tactical clothes, black gloves, thighhighs, military",
    "battle": "tactical gear, body armor, combat vest, rifle, intense",
    "fight": "tactical gear, torn clothes, battle damage, sweat, fierce",
    "patrol": "tactical clothes, thighhighs, military, alert, night",
}

COMMANDER_IDENTITY = (
    "1boy, male focus, masculine, short hair, dark hair, brown eyes, tan skin, "
    "strong build, tall, broad shoulders, male"
)
COMMANDER_DEFAULT_OUTFIT = "military uniform, commander, jacket"

COMMANDER_OUTFIT_MAP = {
    "bed": "shirtless, bare chest, muscular, relaxed, lying down",
    "sleep": "shirtless, casual pants, relaxed",
    "morning": "shirtless, messy hair, morning light",
    "bath": "towel, bare chest, wet hair, muscular",
    "underwear": "shirtless, boxers, muscular, relaxed",
    "lingerie": "shirtless, bare chest, muscular",
    "intimate": "shirtless, bare chest, close distance",
    "tender": "open shirt, bare chest, gentle",
    "night": "open shirt, bare chest, moonlight",
    "love": "shirtless, close together, intimate",
    "close": "open shirt, gentle expression",
    "hold": "t-shirt, strong arms, holding",
    "cuddle": "t-shirt, comfortable, close",
    "kiss": "open shirt, close up, romantic",
    "sex": "nude, muscular, bare skin, sweat, bedroom, nsfw",
    "fuck": "nude, muscular, bare skin, on bed, sweat, nsfw",
    "naked": "fully nude, muscular, confident pose, bedroom",
    "nude": "fully nude, muscular, bare skin, artistic",
    "topless": "shirtless, muscular, bare chest",
    "after sex": "shirtless, lying in bed, messy hair, satisfied, sheets",
    "shower": "nude, wet skin, muscular, steam, shower",
    "beach": "swim trunks, bare chest, muscular, sun-kissed",
    "date": "fitted dress shirt, slacks, rolled sleeves, watch, sharp",
    "dinner": "dark suit, no tie, open collar, candlelight",
    "cafe": "casual jacket, fitted t-shirt, jeans",
    "casual": "henley shirt, jeans, sneakers, relaxed",
    "home": "t-shirt, sweatpants, relaxed, comfortable",
    "training": "tank top, athletic shorts, sweat, muscular arms",
    "workout": "tank top, athletic shorts, sweat, muscular",
    "motorcycle": "leather jacket, jeans, boots, confident",
    "formal": "military dress uniform, medals, sharp",
    "rain": "wet shirt, clinging fabric, rain",
    "snow": "winter jacket, scarf, boots",
    "battle": "tactical vest, combat gear, intense",
    "fight": "tactical vest, torn shirt, battle worn",
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
# Rich visual profiles — weapon designation IS the character identity
# Lore-accurate squad visual profiles — sourced from Danbooru tags, IOP Wiki, official art
# Each T-Doll's weapon designation IS their identity
SQUAD_KEYWORDS = {
    # ── Combat Team A ────────────────────────────────────────────────
    "mechty": (
        "1girl, g11 \\(girls' frontline\\), short brown hair, auburn hair, messy hair, "
        "green eyes, half-lidded eyes, sleepy expression, petite, slim, "
        "oversized tactical hoodie, partially unzipped combat vest, G11 rifle, "
        "lazy pose, drowsy"
    ),
    "belka": (
        "1girl, belka \\(girls' frontline 2\\), long brown hair, green streaks, green highlights, "
        "red eyes, brown beret, busty, large breasts, "
        "brown tactical apron, tan shirt, orange tights, orange leggings, "
        "black gloves, black boots, H&K G28 battle rifle, ammo crate, "
        "SSD-62G frame, designated marksman, peppy expression, energetic, cute smile"
    ),
    "andoris": (
        "1girl, andoris \\(girls' frontline 2\\), blonde hair, blue eyes, violet eyes, "
        "white and black asymmetric jacket, dark bodysuit, "
        "long pink sash, red trailing sash, knee-high grey boots, "
        "gold necklace, red necklace, large breasts, gentle smile, "
        "H&K G36K assault rifle, intelligence specialist, sweet expression, elegant"
    ),
    # ── Former / Allied ──────────────────────────────────────────────
    "leva": (
        "1girl, ump45 \\(girls' frontline\\), grey-brown hair, ash hair, long hair, "
        "yellow eyes, gold eyes, hair ribbon, "
        "white shirt, black jacket, pleated skirt, yellow necktie, "
        "thighhighs, brown thigh-high boots, red accents, "
        "UMP45 SMG, slender, confident pose, leader aura, composed"
    ),
    "lenna": (
        "1girl, ump9 \\(girls' frontline\\), light brown hair, chestnut hair, "
        "green eyes, cheerful, warm smile, gentle expression, "
        "UMP9 SMG, kind demeanor"
    ),
    # ── Combat Team B ────────────────────────────────────────────────
    "vector": (
        "1girl, vector \\(girls' frontline 2\\), short ash grey hair, silver hair, "
        "yellow eyes, amber eyes, "
        "white coat, black and orange tactical bodysuit, "
        "yellow equipment pouches, orange harness, black leggings, grey boots, "
        "KRISS Vector SMG, suppressor, incendiary grenades, knife, dagger, "
        "stoic expression, pessimistic, lethal aura"
    ),
    "harpsy": (
        "1girl, harpsy \\(girls' frontline 2\\), blonde hair, green eyes, "
        "cat ear headphones, fake animal ears, high collar, "
        "tail accessory, signal booster ears, "
        "Steyr TMP submachine gun, tech equipment, "
        "introverted, timid expression, cute, tech geek"
    ),
    "ruchey": (
        "1girl, ruchey \\(girls' frontline 2\\), white hair, silver hair, "
        "spiral twintails, twin drills, red eyes, "
        "small build, short stature, petite, "
        "white shirt, neon green suspenders, yellow suspenders, "
        "black gloves, hair clip, clover hair ornament, "
        "PP-90 submachine gun, cheerful, cute smile, nimble"
    ),
    "welrod": (
        "1girl, welrod mkii \\(girls' frontline\\), short blonde hair, small twintails, "
        "green eyes, professional, elegant, british aesthetic, "
        "black cape, black cloak on shoulders, dark halter top, corset, "
        "short skirt, garter straps, thigh holsters, "
        "grey socks, blue shoes, dual pistols, Welrod silenced pistol, "
        "composed, sophisticated"
    ),
    # ── Other ────────────────────────────────────────────────────────
    "groza": (
        "1girl, ots-14 \\(girls' frontline\\), blonde hair, strawberry blonde, "
        "long hair, low ponytail, gold eyes, amber eyes, "
        "white coat, red lining, dark bodysuit, multiple belts, "
        "tall brown boots, knee-high boots, OTs-14 rifle, "
        "confident smirk, military, elegant"
    ),
}

# ── Mission-aware image generation ────────────────────────────────────────

MISSION_SCENE_TAGS = {
    "combat": "combat, gunfire, muzzle flash, debris, tactical formation, explosions in background, intense, action pose",
    "patrol": "patrol, night operation, NVGs, tactical movement, stealth, dark environment, moonlight",
    "ambush": "ambush, taking cover, return fire, smoke, urgent, diving for cover, bullets",
    "field_camp": "field camp, tent, campfire, night, equipment laid out, resting between ops",
    "injury": "field medical, bandaging wounds, blood, torn clothing, determined expression, still fighting",
    "discovery": "discovery, examining artifact, ancient tech, glowing object, curious, cautious",
    "weather": "heavy rain, storm, wind, wet clothing, persevering, lightning in background",
    "comms": "radio equipment, static, adjusting antenna, focused, signal disruption",
    "extraction": "extraction, helicopter in distance, running, carrying equipment, urgent, dust",
    "group_photo": "group photo, squad together, team formation, military pose, camaraderie",
    "victory": "victory, mission complete, relieved, exhausted but smiling, sun rising",
}


def detect_squad_members(text: str) -> list[str]:
    """Detect which squad members are mentioned in the text."""
    lower = text.lower()
    found = []
    for name in SQUAD_KEYWORDS:
        if name in lower:
            found.append(name)
    return found


def build_mission_prompt(
    scene_type: str = "combat",
    squad_members: list[str] | None = None,
    injuries: list[str] | None = None,
    affection_level: int = 0,
) -> str:
    """Build an image prompt for mission-context scenes.

    Args:
        scene_type: Key from MISSION_SCENE_TAGS.
        squad_members: List of squad member names to include.
        injuries: Active injury events (e.g., ["klukai_injured", "squad_injured"]).
        affection_level: 0-9, affects Klukai's expression.
    """
    parts = [QUALITY_TAGS, KLUKAI_LORA_TRIGGER]

    # Klukai is always in mission images
    parts.append(KLUKAI_IDENTITY)
    parts.append("tactical gear, body armor, combat vest, rifle, intense")

    # Add injury tags to Klukai if she's hurt
    if injuries and "klukai_injured" in injuries:
        parts.append("bandaged arm, torn sleeve, blood stains, determined expression, still commanding")

    # Add squad members
    if squad_members:
        for name in squad_members[:3]:  # Max 3 extra characters to avoid crowding
            if name in SQUAD_KEYWORDS:
                parts.append(SQUAD_KEYWORDS[name])
        # Add injury tags to generic squad if injured
        if injuries and "squad_injured" in injuries:
            parts.append("injured teammate, field bandages, supporting each other")
        if injuries and "medical_emergency" in injuries:
            parts.append("field medic, treating wounds, urgent medical attention")

    # Scene tags
    scene = MISSION_SCENE_TAGS.get(scene_type, MISSION_SCENE_TAGS["combat"])
    parts.append(scene)

    # Multi-girl tag if squad members present
    if squad_members:
        count = len(squad_members) + 1  # +1 for Klukai
        parts.append(f"{count}girls, multiple girls, group")

    return ", ".join(parts)

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

QUALITY_TAGS = (
    "masterpiece, best quality, very aesthetic, absurdres, ultra-detailed, "
    "beautiful lighting, depth of field, sharp focus, cinematic composition, "
    "vivid colors, professional, high resolution, intricate details, "
    "ambient occlusion, volumetric lighting, film grain"
)
NEGATIVE_TAGS = (
    "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, "
    "fewer digits, cropped, worst quality, low quality, normal quality, "
    "jpeg artifacts, signature, watermark, username, blurry, artist name, "
    "deformed, ugly, duplicate, morbid, mutilated, extra limbs, "
    "thick thighs, wide hips, chubby, plump, fat, overweight, huge breasts, "
    "androgynous, feminine boy, crossdressing, male in female clothes, "
    "flat chest, child, loli, shota"
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
    squad_members: list[str] | None = None,
) -> str:
    """Build the full positive prompt with quality tags, LoRA trigger, and character identities.

    Args:
        scene_tags: Scene/action Danbooru tags.
        couple: Whether to include the Commander.
        affection_level: 0-9, affects mood expression tags.
        context: Recent conversation text for outfit selection.
        squad_members: Optional list of squad member names to include in the scene.
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

    # Add squad members if specified
    if squad_members:
        for name in squad_members[:3]:  # Max 3 to avoid overcrowding
            if name in SQUAD_KEYWORDS:
                parts.append(SQUAD_KEYWORDS[name])
        total_girls = 1 + len([n for n in squad_members if n in SQUAD_KEYWORDS])
        if total_girls > 1:
            parts.append(f"{total_girls}girls, multiple girls")

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
