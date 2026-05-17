"""Constants for image generation: prompts, tags, outfits, workflow.

Extracted from app/image_gen.py (S+ Phase 2 — file-size hygiene per
docs/superpowers/specs/2026-05-16-s-plus-uplift.md §6.1).

NoobAI-XL / Illustrious + Klukai IL LoRA. Per reference_illustrious.md these
constants encode the canonical Danbooru tag conventions used by the image
gen pipeline. Don't mutate these maps casually — character outfit selection
is a content gate (matched in app/image_gen.py:_select_outfit).
"""

from __future__ import annotations

KLUKAI_IDENTITY = (
    "1girl, hk416 \\(girls' frontline\\), silver hair, green eyes, long hair, ponytail, "
    "hair ornament, girls' frontline, slim waist, athletic body, toned, slender figure, "
    "long legs, beautiful detailed eyes, light blush, detailed skin, "
    "perfect anatomy, delicate collarbone, navel, medium breasts, elegant neck, "
    "soft lips, detailed face, expressive eyes, eyelashes"
)
KLUKAI_DEFAULT_OUTFIT = "tactical clothes, black gloves, thighhighs, military"

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
    "extra arms, extra legs, fused limbs, limbs through body, clipping, "
    "multiple people, multiple boys, multiple girls, clone, twin, "
    "two heads, two faces, extra faces, disfigured, "
    "interlocking limbs, overlapping bodies, merged bodies, "
    "thick thighs, wide hips, chubby, plump, fat, overweight, huge breasts, "
    "androgynous, feminine boy, crossdressing, male in female clothes, "
    "flat chest, child, loli, shota"
)

KLUKAI_LORA = "Klukai_GFL2_IL-03.safetensors"
KLUKAI_LORA_TRIGGER = "Klukai"

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
            "cfg": 7.0,
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

LANDSCAPE_KEYWORDS = [
    "landscape", "scenery", "sunset", "sunrise", "city", "battlefield",
    "panorama", "wide shot", "environment", "base", "headquarters",
    "motorcycle", "riding", "driving", "vehicle",
]

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

