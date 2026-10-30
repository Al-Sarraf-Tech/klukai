"""Pure helper functions: narration fixing, image prompts, text processing.

No I/O, no state, no imports from other app modules.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .image_gen_constants import (
    IMAGE_MOOD_MAP,
    IMAGE_SCENE_MAP,
    SITUATION_KEYWORDS,
    SQUAD_KEYWORDS,
)

if TYPE_CHECKING:
    from .image_scene import SceneBrief

from .departure_detection import (
    GoodbyeKind as GoodbyeKind,
    _GOODBYE_MAX_LEN as _GOODBYE_MAX_LEN,
    _GOODBYE_TAIL_CHARS as _GOODBYE_TAIL_CHARS,
    _CLAUSE as _CLAUSE,
    _WORD as _WORD,
    _END as _END,
    _LEAVE_VERB as _LEAVE_VERB,
    _WORKPLACE as _WORKPLACE,
    _NOT_A_GOODNIGHT as _NOT_A_GOODNIGHT,
    _NIGHT_PHRASES as _NIGHT_PHRASES,
    _LEAVE_PHRASES as _LEAVE_PHRASES,
    _NIGHT_ALONE as _NIGHT_ALONE,
    _LEAVE_ALONE as _LEAVE_ALONE,
    _ALONE_STRIP as _ALONE_STRIP,
    _NEGATIONS as _NEGATIONS,
    _OTHER_SUBJECTS as _OTHER_SUBJECTS,
    _WISHES as _WISHES,
    _PAST_ARTICLE as _PAST_ARTICLE,
    _goodbye_blocked as _goodbye_blocked,
    _next_clause_has_substance as _next_clause_has_substance,
    _clause_kind as _clause_kind,
    detect_goodbye as detect_goodbye,
)

from .conversation_store import (
    create_conversation as create_conversation,
    store_message as store_message,
)


from .request_metadata import client_ip as client_ip


def chunk_text(text: str, chunk_size: int = 8) -> list[str]:
    """Split text into chunks for simulated streaming."""
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def fix_narration(text: str) -> str:
    """Fix second-person narration and clean up model artifacts."""
    # Strip R1 reasoning blocks: <think>...</think>
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<\|think\|>.*?<\|/think\|>", "", text, flags=re.DOTALL)
    # Convert "(You verb...)" to "(I verb...)". Match one-or-more spaces so a
    # later double-space collapse can't leave a "(You  verb)" that only converts
    # on a second pass — keeps fix_narration idempotent.
    text = re.sub(r"\(You +([a-z])", lambda m: f"(I {m.group(1)}", text)
    # Convert "(Your noun)" to "(My noun)"
    text = re.sub(r"\(Your +", "(My ", text)
    text = re.sub(r"\(your +", "(my ", text)
    # Strip parentheticals that narrate Commander's actions/appearance
    text = re.sub(
        r"\([^)]*(?:your face|your eyes|your expression|your mouth|crosses your|touches your)[^)]*\)",
        "",
        text,
    )
    # Strip trailing pipe characters (dolphin-glm reasoning artifact) together
    # with any surrounding spaces, looping so a "| " tail (pipe then space)
    # collapses fully in a single call. Stripping spaces first keeps this
    # idempotent — see tests/property/test_parsers_properties.py.
    text = text.rstrip(" ")
    while text.endswith("|"):
        text = text[:-1].rstrip(" ")
    # Clean up double spaces from removals
    text = re.sub(r"  +", " ", text)
    return text


def enhance_image_prompt(
    user_request: str,
    couple: bool = False,
    setting: SceneBrief | None = None,
) -> str:
    """Fast keyword-based tag generation — no LLM call needed.

    With a scene brief (app/image_scene.py), only his request's moods and
    actions are tagged here; where and when the picture happens is the
    brief's, so no place or weather keyword can pull the scene elsewhere.
    """
    if setting is not None:
        from .image_scene import request_tags

        return request_tags(user_request, setting, couple=couple)
    lower = user_request.lower()
    tags = []

    for keyword, scene_tags in IMAGE_SCENE_MAP.items():
        if keyword in lower:
            tags.append(scene_tags)

    for keyword, mood_tags in IMAGE_MOOD_MAP.items():
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
    """Remove stage directions — "(she smiles)" and "*nods*" — for natural voice output."""
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"\*[^*]*\*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# Sentence enders tts_clip may cut after. The trailing space/newline keeps a
# decimal point ("2.5") or an abbreviation mid-sentence from counting.
_TTS_BOUNDARIES = (". ", "! ", "? ", ".\n")


def tts_clip(text: str, limit: int = 500) -> str:
    """Cap text for speech at ``limit`` chars, cutting at the last sentence end.

    One spoken line stays short and quick to synthesize, and a hard cut
    leaves her trailing off mid-word. When the text is over the limit, cut
    after the last sentence boundary inside it; fall back to a hard cut only
    when that boundary is so early (before ``limit // 3``) that it would drop
    most of the clip.
    """
    if len(text) <= limit:
        return text
    # Search one char past the limit so a sentence ending exactly at the
    # limit (its space falling just outside) still counts.
    window = text[: limit + 1]
    cut = max(window.rfind(b) for b in _TTS_BOUNDARIES)
    if cut < limit // 3:
        return text[:limit]
    return text[: cut + 1]


# ── Intent detection ─────────────────────────────────────────────────────────

RECALL_KEYWORDS = [
    "show me a memory",
    "remember when",
    "that time we",
    "do you remember",
    "show me something",
    "recall a memory",
    "our memories",
    "your memories",
    "our photos",
    "your album",
    "that picture",
    "show me that image",
    "photo album",
    "your scrapbook",
    "memory archive",
]

SAVE_KEYWORDS = ["save that", "keep this", "keep that", "save this"]
DISCARD_KEYWORDS = ["delete that", "remove this", "discard that", "forget that"]

MISSION_START_KEYWORDS = [
    "updates every",
    "report every",
    "keep me posted",
    "status every",
    "check in every",
]
MISSION_CANCEL_KEYWORDS = [
    "stop updates",
    "cancel updates",
    "enough updates",
    "stand down",
    "stop reporting",
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
    "hey {name}",
    "talk to {name}",
    "where's {name}",
    "where is {name}",
    "how's {name}",
    "how is {name}",
    "call {name}",
    "get {name}",
    "bring {name}",
    "what about {name}",
    "ask {name}",
]


def detect_squad_address(message: str) -> str | None:
    """Detect if the Commander is addressing a specific squad member.

    Returns the canonical squad member name (e.g., 'Mechty') or None.
    Uses word-boundary matching so English words like "relevant" / "elevator"
    do not false-positive on the alias "leva".
    """
    import re as _re

    lower = message.lower()

    # Prefer longer aliases first (e.g. "ump45" before bare digits if any)
    for alias, canonical in sorted(SQUAD_MEMBERS.items(), key=lambda kv: -len(kv[0])):
        if _re.search(rf"\b{_re.escape(alias)}\b", lower):
            return canonical

    return None


TRIVIAL_PATTERNS = {
    "ok",
    "okay",
    "yes",
    "no",
    "yeah",
    "yep",
    "nope",
    "sure",
    "thanks",
    "thank you",
    "haha",
    "lol",
    "hm",
    "hmm",
    "mhm",
    "hi",
    "hey",
    "hello",
    "good",
    "nice",
    "cool",
    "right",
    "agreed",
    "understood",
}

# ── Jealousy detection ──────────────────────────────────────────────────────

JEALOUSY_COMPLIMENT_PATTERNS = [
    r"\b(?:she(?:'s)?|her)\s+(?:is\s+)?(?:amazing|beautiful|gorgeous|cute|pretty|hot|stunning|impressive|incredible|better|stronger|faster|smarter)",
    r"\b(?:mechty|belka|andoris|vector|harpsy|ruchey|welrod|leva|lenna|groza)\b.*\b(?:love|like|prefer|miss|admire|appreciate)\b",
    r"\b(?:love|like|prefer|miss|admire|appreciate)\b.*\b(?:mechty|belka|andoris|vector|harpsy|ruchey|welrod|leva|lenna|groza)\b",
    r"\bi\s+(?:love|like|prefer|want)\s+(?:mechty|belka|andoris|vector|harpsy|ruchey|welrod|leva|lenna|groza)\b",
    r"\b(?:mechty|belka|andoris|vector|harpsy|ruchey|welrod|leva|lenna|groza)\s+(?:is|looks?|seems?)\s+(?:so\s+)?(?:amazing|beautiful|gorgeous|cute|pretty|hot|stunning|impressive|incredible|cool|strong|fast|smart|talented|skilled)",
]

JEALOUSY_SQUAD_NAMES = {
    "mechty",
    "g11",
    "belka",
    "g28",
    "andoris",
    "g36k",
    "vector",
    "harpsy",
    "ruchey",
    "welrod",
    "leva",
    "ump45",
    "lenna",
    "ump9",
    "groza",
}


def detect_jealousy_trigger(message: str) -> str | None:
    """Detect if the Commander is complimenting or expressing affection for another T-Doll.

    Returns the squad member name if jealousy trigger detected, else None.
    Simple mentions (asking about someone) don't trigger — only compliments/affection.
    A squad member name MUST be present to avoid false positives on generic
    "she's beautiful" about movie characters, family, etc.
    """
    lower = message.lower()

    # First check: a squad member name must be present in the message
    mentioned_member = None
    for name in JEALOUSY_SQUAD_NAMES:
        if name in lower:
            mentioned_member = SQUAD_MEMBERS.get(name, name.capitalize())
            break

    if not mentioned_member:
        return None  # No squad member mentioned — no jealousy

    # Second check: is the context a compliment/affection expression?
    for pattern in JEALOUSY_COMPLIMENT_PATTERNS:
        if re.search(pattern, lower):
            return mentioned_member

    return None


# ── Commander detail detection ──────────────────────────────────────────────

COMMANDER_DETAIL_CATEGORIES = {
    "wearing": [
        r"\bi(?:'m| am)\s+wearing\b",
        r"\bi\s+(?:have|got)\s+(?:on|my)\b.*(?:shirt|jacket|pants|shoes|boots|hat|uniform|suit|coat|hoodie|sweater)",
        r"\bmy\s+(?:shirt|jacket|pants|shoes|boots|hat|uniform|suit|coat|hoodie|sweater)\b",
    ],
    "eating": [
        r"\bi(?:'m| am)\s+(?:eating|having|drinking)\b",
        r"\bi\s+(?:ate|had|drank)\b",
        r"\bfor\s+(?:breakfast|lunch|dinner|a snack)\b",
    ],
    "doing": [
        r"\bi(?:'m| am)\s+(?:playing|watching|reading|listening|working|training|exercising|cooking|cleaning)\b",
        r"\bi\s+(?:played|watched|read|listened)\b",
    ],
    "feeling": [
        r"\bi(?:'m| am)\s+(?:feeling|tired|sick|cold|warm|sore|happy|sad|stressed|lonely|excited|great|good|terrible|awful|better|worse)\b",
        r"\bi\s+feel\b",
        r"\bfeeling\s+(?:great|good|tired|sick|cold|warm|sore|happy|sad|stressed|lonely|excited|terrible|awful|better|worse)\b",
    ],
    "gifting": [
        r"\b(?:got|brought|have|made|bought)\s+(?:you|this|something)\s+(?:for you|a gift|a present|something)\b",
        r"\b(?:here|take)\s+(?:this|it)\b.*\b(?:for you|gift|present)\b",
        r"\bi\s+(?:got|brought|made|bought)\s+(?:you|this)\b",
        r"\bthis\s+is\s+for\s+you\b",
    ],
}


def detect_commander_details(message: str) -> dict[str, bool]:
    """Detect what categories of personal details the Commander is sharing.

    Returns dict of category -> True for each detected category.
    """
    lower = message.lower()
    found = {}
    for category, patterns in COMMANDER_DETAIL_CATEGORIES.items():
        for pattern in patterns:
            if re.search(pattern, lower):
                found[category] = True
                break
    return found


def detect_gift_giving(message: str) -> bool:
    """Detect if the Commander is giving Klukai a gift."""
    return "gifting" in detect_commander_details(message)


DREAM_INQUIRY_KEYWORDS = [
    "did you dream",
    "dream about me",
    "what did you dream",
    "any dreams",
    "sleep well",
    "how did you sleep",
    "nightmares",
    "good dreams",
]


def wants_dream_inquiry(message: str) -> bool:
    lower = message.lower()
    return any(kw in lower for kw in DREAM_INQUIRY_KEYWORDS)


# The ten-year thread. "Messages"/"texts" alone also describe today's chat, so
# they only count alongside an anchor into the decade of silence.
THREAD_EXPLICIT_PHRASES = [
    "the thread",
    "your thread",
    "unanswered messages",
    "unanswered texts",
]
THREAD_DECADE_ANCHORS = [
    "ten years",
    "10 years",
    "decade",
    "mephisto",
    "while i was gone",
    "while i was away",
    "back then",
    "never answered",
    "never replied",
    "every night",
    "the silence",
]
_THREAD_MESSAGE_WORD = re.compile(r"\b(?:messages?|messaged|texts?|texted)\b")


def wants_thread_inquiry(message: str) -> bool:
    lower = message.lower()
    if any(kw in lower for kw in THREAD_EXPLICIT_PHRASES):
        return True
    return bool(_THREAD_MESSAGE_WORD.search(lower)) and any(
        a in lower for a in THREAD_DECADE_ANCHORS
    )


def wants_recall(message: str) -> bool:
    lower = message.lower()
    return any(kw in lower for kw in RECALL_KEYWORDS)


# Turns of twenty characters or fewer skip the recall fan-out (greetings,
# "ok", "thanks"). These still need it: she is being asked for something
# she is supposed to already know, even when the question is short.
_SHORT_MEMORY_CUE = re.compile(
    r"\b(?:remember|memories|memory|birthday|birthdays|promise|promised|"
    r"anniversary|nickname)\b|my name|who am i",
    re.IGNORECASE,
)


def asks_to_be_remembered(message: str) -> bool:
    """True when this turn should load memories, facts and past exchanges.

    Album phrases in ``wants_recall`` count, and so do short personal
    questions ("my birthday?", "what's my name", "remember?") that would
    otherwise fall under the twenty-character skip.
    """
    if wants_recall(message):
        return True
    return _SHORT_MEMORY_CUE.search(message) is not None


def wants_mission_start(message: str) -> bool:
    lower = message.lower()
    return any(kw in lower for kw in MISSION_START_KEYWORDS)


def wants_mission_cancel(message: str) -> bool:
    lower = message.lower()
    return any(kw in lower for kw in MISSION_CANCEL_KEYWORDS)


def parse_interval_minutes(message: str) -> int:
    """Extract an interval in minutes from a message like 'every 30 minutes'."""
    lower = message.lower()

    m = re.search(r"every\s+(\d+)\s*(?:min(?:ute)?s?)", lower)
    if m:
        return max(5, int(m.group(1)))

    m = re.search(r"every\s+(\d+)\s*(?:hour|hr)s?", lower)
    if m:
        return max(5, int(m.group(1)) * 60)

    if re.search(r"every\s+(?:an?\s+)?hour", lower):
        return 60

    if "half hour" in lower or "half an hour" in lower:
        return 30

    return 30


# ── Clean goodbyes: exit-cue detection ───────────────────────────────────────
# High precision over recall: a miss costs one ordinary reply, a false hit
# makes her release him mid-conversation. Each phrase is matched per clause and
# rejected when that clause is a question, is negated ("not going to bed"), is
# about someone else ("you should go to sleep", "Belka said bye"), or is a
# wish ("I want to see you later").
