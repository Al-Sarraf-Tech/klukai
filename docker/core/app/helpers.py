"""Pure helper functions: narration fixing, image prompts, text processing.

No I/O, no state, no imports from other app modules.
"""

from __future__ import annotations

import re

from .image_gen import SQUAD_KEYWORDS, SITUATION_KEYWORDS


def chunk_text(text: str, chunk_size: int = 8) -> list[str]:
    """Split text into chunks for simulated streaming."""
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def fix_narration(text: str) -> str:
    """Fix second-person narration and clean up model artifacts."""
    # Strip R1 reasoning blocks: <think>...</think>
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'<\|think\|>.*?<\|/think\|>', '', text, flags=re.DOTALL)
    # Convert "(You verb...)" to "(I verb...)"
    text = re.sub(r'\(You ([a-z])', lambda m: f'(I {m.group(1)}', text)
    # Convert "(Your noun)" to "(My noun)"
    text = re.sub(r'\(Your ', '(My ', text)
    text = re.sub(r'\(your ', '(my ', text)
    # Strip parentheticals that narrate Commander's actions/appearance
    text = re.sub(
        r'\([^)]*(?:your face|your eyes|your expression|your mouth|crosses your|touches your)[^)]*\)',
        '', text,
    )
    # Strip trailing pipe characters (dolphin-glm reasoning artifact)
    while text.endswith('|'):
        text = text[:-1]
    text = text.rstrip(' ')
    # Clean up double spaces from removals
    text = re.sub(r'  +', ' ', text)
    return text


def enhance_image_prompt(user_request: str, couple: bool = False) -> str:
    """Fast keyword-based tag generation — no LLM call needed."""
    lower = user_request.lower()
    tags = []

    SCENE_MAP = {
        "sunset": "sunset, orange sky, golden hour lighting",
        "night": "night, moonlight, dark sky, stars",
        "rain": "rain, wet, umbrella, overcast",
        "snow": "snow, winter, cold breath, scarf",
        "beach": "beach, ocean, sand, swimsuit, summer",
        "cafe": "cafe, table, coffee cup, indoor, cozy",
        "battle": "battlefield, smoke, debris, action pose",
        "motorcycle": "motorcycle, riding, wind, speed lines, road",
        "bed": "bedroom, bed, pillows, soft lighting, intimate",
        "rooftop": "rooftop, city skyline, wind, evening",
        "garden": "garden, flowers, natural lighting, peaceful",
        "office": "office, desk, computer, indoor lighting",
        "forest": "forest, trees, nature, sunlight through leaves",
        "city": "city, urban, street, buildings, neon",
    }
    for keyword, scene_tags in SCENE_MAP.items():
        if keyword in lower:
            tags.append(scene_tags)

    MOOD_MAP = {
        "kiss": "kiss, eyes closed, romantic",
        "hug": "hug, embrace, close, warm",
        "cuddle": "cuddling, lying down, comfortable, close",
        "hold": "holding hands, close, side by side",
        "smile": "smile, happy, cheerful",
        "blush": "blush, embarrassed, looking away",
        "cry": "tears, emotional, sad",
        "fight": "fighting stance, action, dynamic pose",
        "sleep": "sleeping, peaceful, eyes closed",
        "eat": "eating, food, table",
        "cook": "cooking, kitchen, apron",
        "read": "reading, book, sitting, quiet",
    }
    for keyword, mood_tags in MOOD_MAP.items():
        if keyword in lower:
            tags.append(mood_tags)

    for keyword, sit_tags in SITUATION_KEYWORDS.items():
        if keyword in lower:
            tags.append(sit_tags)

    for member, member_tags in SQUAD_KEYWORDS.items():
        if member in lower:
            tags.append(member_tags)
            tags.append("multiple girls" if not couple else "")

    if not tags:
        tags.append("standing, looking at viewer, detailed background")

    return ", ".join(t for t in tags if t)


def strip_actions_for_tts(text: str) -> str:
    """Remove all parenthetical actions from text for natural voice output."""
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ── Intent detection ─────────────────────────────────────────────────────────

RECALL_KEYWORDS = [
    "show me a memory", "remember when", "that time we", "do you remember",
    "show me something", "recall a memory", "our memories", "your memories",
    "our photos", "your album", "that picture", "show me that image",
    "photo album", "your scrapbook", "memory archive",
]

SAVE_KEYWORDS = ["save that", "keep this", "keep that", "save this"]
DISCARD_KEYWORDS = ["delete that", "remove this", "discard that", "forget that"]

MISSION_START_KEYWORDS = [
    "updates every", "report every", "keep me posted", "status every", "check in every",
]
MISSION_CANCEL_KEYWORDS = [
    "stop updates", "cancel updates", "enough updates", "stand down", "stop reporting",
]

# Squad member addressing — Commander wants to talk to/about a specific squad member
SQUAD_MEMBERS = {
    "mechty": "Mechty",
    "g11": "Mechty",
    "belka": "Belka",
    "g28": "Belka",
    "andoris": "Andoris",
    "vector": "Vector",
    "harpsy": "Harpsy",
    "ruchey": "Ruchey",
    "welrod": "Welrod",
    "leva": "Leva",
    "ump45": "Leva",
    "ump9": "Lenna",
    "lenna": "Lenna",
}

SQUAD_ADDRESS_PATTERNS = [
    "hey {name}", "talk to {name}", "where's {name}", "where is {name}",
    "how's {name}", "how is {name}", "call {name}", "get {name}",
    "bring {name}", "what about {name}", "ask {name}",
]


def detect_squad_address(message: str) -> str | None:
    """Detect if the Commander is addressing a specific squad member.

    Returns the canonical squad member name (e.g., 'Mechty') or None.
    """
    lower = message.lower()

    # Check if any squad member name appears in the message
    for alias, canonical in SQUAD_MEMBERS.items():
        if alias in lower:
            return canonical

    return None


TRIVIAL_PATTERNS = {
    "ok", "okay", "yes", "no", "yeah", "yep", "nope", "sure", "thanks",
    "thank you", "haha", "lol", "hm", "hmm", "mhm", "hi", "hey", "hello",
    "good", "nice", "cool", "right", "agreed", "understood",
}

DREAM_INQUIRY_KEYWORDS = [
    "did you dream", "dream about me", "what did you dream",
    "any dreams", "sleep well", "how did you sleep",
    "nightmares", "good dreams",
]


def wants_dream_inquiry(message: str) -> bool:
    lower = message.lower()
    return any(kw in lower for kw in DREAM_INQUIRY_KEYWORDS)


def wants_recall(message: str) -> bool:
    lower = message.lower()
    return any(kw in lower for kw in RECALL_KEYWORDS)


def wants_mission_start(message: str) -> bool:
    lower = message.lower()
    return any(kw in lower for kw in MISSION_START_KEYWORDS)


def wants_mission_cancel(message: str) -> bool:
    lower = message.lower()
    return any(kw in lower for kw in MISSION_CANCEL_KEYWORDS)


def parse_interval_minutes(message: str) -> int:
    """Extract an interval in minutes from a message like 'every 30 minutes'."""
    lower = message.lower()

    m = re.search(r'every\s+(\d+)\s*(?:min(?:ute)?s?)', lower)
    if m:
        return max(5, int(m.group(1)))

    m = re.search(r'every\s+(\d+)\s*(?:hour|hr)s?', lower)
    if m:
        return max(5, int(m.group(1)) * 60)

    if re.search(r'every\s+(?:an?\s+)?hour', lower):
        return 60

    if "half hour" in lower or "half an hour" in lower:
        return 30

    return 30


# ── DB helpers ───────────────────────────────────────────────────────────────

async def create_conversation(conv_id: str) -> None:
    """Create a new conversation record."""
    import logging
    from .db import get_conn_autocommit
    logger = logging.getLogger(__name__)
    try:
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "INSERT INTO companion_conversations (id) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                (conv_id,),
            )
    except Exception as e:
        logger.error("Failed to create conversation: %s", e)


async def store_message(
    conversation_id: str,
    role: str,
    content: str,
    model: str = "",
    latency_ms: int | None = None,
) -> None:
    """Store a message and update conversation turn count."""
    import logging
    from .db import get_conn_autocommit
    logger = logging.getLogger(__name__)
    try:
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "INSERT INTO companion_messages "
                "(conversation_id, role, content, model, latency_ms) "
                "VALUES (%s, %s, %s, %s, %s)",
                (conversation_id, role, content, model, latency_ms),
            )
            await conn.execute(
                "UPDATE companion_conversations SET turn_count = turn_count + 1, "
                "model_used = %s WHERE id = %s",
                (model, conversation_id),
            )
    except Exception as e:
        logger.error("Failed to store message: %s", e)
