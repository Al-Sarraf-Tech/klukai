"""Pure helper functions: narration fixing, image prompts, text processing.

No I/O, no state, no imports from other app modules.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Literal

from .image_gen import SQUAD_KEYWORDS, SITUATION_KEYWORDS

if TYPE_CHECKING:
    from fastapi import Request


def voice_auth_headers() -> dict[str, str]:
    """Bearer header for calls to the voice service when VOICE_API_TOKEN is set.

    Empty (no auth) when unset, so it stays a no-op until the token is
    provisioned on the voice host — core->voice keeps working either way.
    """
    tok = os.environ.get("VOICE_API_TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def client_ip(request: "Request") -> str:
    """Best-effort real client IP behind Cloudflare → cloudflared → loopback.

    The socket peer (request.client.host) is always the tunnel's internal IP, so
    a naive per-IP ban or audit would treat every external user as one host —
    locking out the owner and recording useless forensics. Prefer Cloudflare's
    CF-Connecting-IP, then the first X-Forwarded-For hop, then the peer.
    """
    cf = request.headers.get("cf-connecting-ip")
    if cf and cf.strip():
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff and xff.strip():
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def chunk_text(text: str, chunk_size: int = 8) -> list[str]:
    """Split text into chunks for simulated streaming."""
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def fix_narration(text: str) -> str:
    """Fix second-person narration and clean up model artifacts."""
    # Strip R1 reasoning blocks: <think>...</think>
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'<\|think\|>.*?<\|/think\|>', '', text, flags=re.DOTALL)
    # Convert "(You verb...)" to "(I verb...)". Match one-or-more spaces so a
    # later double-space collapse can't leave a "(You  verb)" that only converts
    # on a second pass — keeps fix_narration idempotent.
    text = re.sub(r'\(You +([a-z])', lambda m: f'(I {m.group(1)}', text)
    # Convert "(Your noun)" to "(My noun)"
    text = re.sub(r'\(Your +', '(My ', text)
    text = re.sub(r'\(your +', '(my ', text)
    # Strip parentheticals that narrate Commander's actions/appearance
    text = re.sub(
        r'\([^)]*(?:your face|your eyes|your expression|your mouth|crosses your|touches your)[^)]*\)',
        '', text,
    )
    # Strip trailing pipe characters (dolphin-glm reasoning artifact) together
    # with any surrounding spaces, looping so a "| " tail (pipe then space)
    # collapses fully in a single call. Stripping spaces first keeps this
    # idempotent — see tests/property/test_parsers_properties.py.
    text = text.rstrip(' ')
    while text.endswith('|'):
        text = text[:-1].rstrip(' ')
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
    "ok", "okay", "yes", "no", "yeah", "yep", "nope", "sure", "thanks",
    "thank you", "haha", "lol", "hm", "hmm", "mhm", "hi", "hey", "hello",
    "good", "nice", "cool", "right", "agreed", "understood",
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
    "mechty", "g11", "belka", "g28", "andoris", "g36k",
    "vector", "harpsy", "ruchey", "welrod",
    "leva", "ump45", "lenna", "ump9", "groza",
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
        r"\bi(?:'m| am)\s+wearing\b", r"\bi\s+(?:have|got)\s+(?:on|my)\b.*(?:shirt|jacket|pants|shoes|boots|hat|uniform|suit|coat|hoodie|sweater)",
        r"\bmy\s+(?:shirt|jacket|pants|shoes|boots|hat|uniform|suit|coat|hoodie|sweater)\b",
    ],
    "eating": [
        r"\bi(?:'m| am)\s+(?:eating|having|drinking)\b", r"\bi\s+(?:ate|had|drank)\b",
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
    "did you dream", "dream about me", "what did you dream",
    "any dreams", "sleep well", "how did you sleep",
    "nightmares", "good dreams",
]


def wants_dream_inquiry(message: str) -> bool:
    lower = message.lower()
    return any(kw in lower for kw in DREAM_INQUIRY_KEYWORDS)


# The ten-year thread. "Messages"/"texts" alone also describe today's chat, so
# they only count alongside an anchor into the decade of silence.
THREAD_EXPLICIT_PHRASES = ["the thread", "your thread", "unanswered messages", "unanswered texts"]
THREAD_DECADE_ANCHORS = [
    "ten years", "10 years", "decade", "mephisto", "while i was gone", "while i was away",
    "back then", "never answered", "never replied", "every night", "the silence",
]
_THREAD_MESSAGE_WORD = re.compile(r"\b(?:messages?|messaged|texts?|texted)\b")


def wants_thread_inquiry(message: str) -> bool:
    lower = message.lower()
    if any(kw in lower for kw in THREAD_EXPLICIT_PHRASES):
        return True
    return bool(_THREAD_MESSAGE_WORD.search(lower)) and any(a in lower for a in THREAD_DECADE_ANCHORS)


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


# ── Clean goodbyes: exit-cue detection ───────────────────────────────────────
# High precision over recall: a miss costs one ordinary reply, a false hit
# makes her release him mid-conversation. Each phrase is matched per clause and
# rejected when that clause is a question, is negated ("not going to bed"), is
# about someone else ("you should go to sleep", "Belka said bye"), or is a
# wish ("I want to see you later").

GoodbyeKind = Literal["night", "leave"]

_GOODBYE_MAX_LEN = 160    # longer messages only count a goodbye at the very end
_GOODBYE_TAIL_CHARS = 40  # ... meaning within this many chars of the end

# A clause runs up to and including its closing punctuation.
_CLAUSE = re.compile(r"[^.!?;,\n…]+[.!?;,\n…]*")
_WORD = re.compile(r"[a-z']+")

# Clause ends right here, or carries on only with a harmless tail.
_END = (
    r"(?=[\s.!,;…~)]*$|\s+(?:now|soon|then|sorry|again|though|actually|unfortunately"
    r"|lol|haha|bye|cya|ttyl|for\s+(?:real|now|today|tonight|a\s+bit|a\s+while)"
    r"|klukai|love|babe)\b)"
)
_LEAVE_VERB = r"(?:go|run|bounce|dip|jet|split|head\s+out|get\s+going|log\s+off|sign\s+off)"
_WORKPLACE = r"(?:work|class|school|the\s+gym|my\s+shift|a\s+meeting|practice)"
_NOT_A_GOODNIGHT = r"(?!\s+(?:on|with|in|through|early|late|more|better|properly|stor)\w*\b)"

_NIGHT_PHRASES = tuple(re.compile(p) for p in (
    r"\bgood\s*-?\s*night\b(?!\s+stor)",
    r"\bg'?night\b|\bg'?nite\b|\bgn\b",
    r"\bnighty?\s*-?\s*night\b|\bnite\s*-?\s*nite\b",
    r"\bsweet\s+dreams\b",
    r"\boyasumi(?:nasai)?\b|おやすみ|お休み",
    r"\bcall(?:ing)?\s+it\s+a\s+night\b",
    r"\b(?:off|out|done|logging\s+off|signing\s+off|heading\s+out)\s+for\s+(?:the\s+night|tonight)\b",
    r"\bturn(?:ing)?\s+in\b(?!\s+(?:the|my|your|a|an|this|that|it|his|her)\b)",
    r"\bbed\s*time\b(?!\s+stor)",
    # "going to bed", "heading to bed", "time for bed", "about to go to sleep"
    r"\b(?:going|gonna|goin'?|heading|headed|head|off|about|time|getting\s+ready|ready)"
    r"\s+(?:to\s+|for\s+)?(?:go\s+(?:to\s+)?)?(?:bed|sleep)\b" + _NOT_A_GOODNIGHT,
    # "I should get some sleep", "gotta sleep", "I'll go to bed"
    r"\b(?:i'?ll|i\s+will|should|better|need\s+to|have\s+to|gotta|got\s+to|must|let\s+me|lemme)"
    r"\s+(?:go\s+)?(?:to\s+)?(?:get\s+some\s+)?(?:bed|sleep)\b" + _NOT_A_GOODNIGHT,
))

_LEAVE_PHRASES = tuple(re.compile(p) for p in (
    r"\b(?:gotta|got\s+to|have\s+to|need\s+to|must|should|better|gonna|going\s+to|about\s+to|time\s+to)\s+"
    + _LEAVE_VERB + _END,
    r"\b(?:gotta|got\s+to|have\s+to|need\s+to|must)\s+go\s+to\s+" + _WORKPLACE + _END,
    r"\b(?:heading|headed|off|leaving)\s+(?:to|for)\s+" + _WORKPLACE + _END,
    r"\b(?:heading|headed|logging|signing)\s+(?:out|off)\b" + _END,
    r"\bi'?m\s+(?:off|out|leaving|outta\s+here)\b" + _END,
    r"\bgtg\b|\bg2g\b|\bttyl\b|\bttyt\b|\bcya\b|\bbrb\b",
    r"\b(?:good\s*-?\s*)?by+e+(?:\s*-?\s*by+e+)?\b",
    r"\bpeace\s+out\b",
    r"\btalk\s+(?:to\s+(?:you|ya|u)\s+)?(?:tomorrow|tmrw|tmr|later|soon|in\s+the\s+morning)\b",
    r"\b(?:see|catch)\s+(?:you|ya|u)\s+(?:later|tomorrow|tmrw|tmr|soon|in\s+the\s+morning|in\s+a\s+bit)\b",
    r"^\s*(?:(?:ok(?:ay)?|alright|well|anyway)\s+)?(?:see|catch)\s+(?:you|ya|u)\b" + _END,
    r"\buntil\s+(?:tomorrow|next\s+time)\b",
    r"またね|じゃあね|バイバイ|\bmata\s*ne\b",
))

# Whole-clause sign-offs once fillers and pet names are stripped.
_NIGHT_ALONE = frozenset({"night", "nite", "n8"})
_LEAVE_ALONE = frozenset({"later", "laters", "l8r", "peace", "ciao", "adios", "farewell"})
_ALONE_STRIP = frozenset({
    "ok", "okay", "k", "kk", "alright", "well", "anyway", "anyways", "so", "welp",
    "then", "klukai", "kluk", "love", "babe", "baby", "dear", "darling", "sweetheart",
    "princess", "beautiful", "hon", "honey", "everyone", "all",
})

_NEGATIONS = frozenset({
    "not", "never", "don't", "dont", "won't", "wont", "can't", "cant", "isn't",
    "isnt", "aren't", "arent", "ain't", "aint", "shouldn't", "shouldnt",
    "didn't", "didnt", "wouldn't", "wouldnt",
})
_OTHER_SUBJECTS = frozenset({
    "you", "you're", "youre", "u", "ur", "your", "she", "she's", "shes", "he",
    "he's", "hes", "her", "his", "they", "they're", "theyre", "their", "we",
    "we're", "let's", "lets",
}) | frozenset(SQUAD_KEYWORDS)
_WISHES = frozenset({"want", "wanna", "wait", "hope", "hate", "love", "like", "miss"})


def _goodbye_blocked(prefix: str) -> bool:
    """True when the words before a match make it not his goodbye."""
    words = _WORD.findall(prefix)
    return (
        any(w in _NEGATIONS for w in words[-4:])
        or any(w in _OTHER_SUBJECTS or w in _WISHES for w in words[-2:])
    )


def _clause_kind(clause: str, min_end: int) -> GoodbyeKind | None:
    """Goodbye kind for one clause; matches ending before ``min_end`` don't count."""
    if clause.rstrip().endswith("?"):
        return None
    core = [w for w in _WORD.findall(clause) if w not in _ALONE_STRIP]
    alone = core[0] if len(core) == 1 and len(clause) >= min_end else ""
    if alone in _NIGHT_ALONE:
        return "night"
    kinds: tuple[tuple[GoodbyeKind, tuple[re.Pattern[str], ...]], ...] = (
        ("night", _NIGHT_PHRASES), ("leave", _LEAVE_PHRASES),
    )
    for kind, phrases in kinds:
        for phrase in phrases:
            for m in phrase.finditer(clause):
                if m.end() >= min_end and not _goodbye_blocked(clause[:m.start()]):
                    return kind
    if alone in _LEAVE_ALONE:
        return "leave"
    return None


def detect_goodbye(message: str) -> GoodbyeKind | None:
    """Is the Commander signing off? ``"night"``, ``"leave"`` or ``None``.

    "night" (goodnight, gn, going to bed, おやすみ ...) wins over "leave"
    (gotta go, bye, ttyl, see you tomorrow ...) when a message has both.
    Messages longer than ~160 characters only count a goodbye that ends in
    their last ~40 characters, so a long story that mentions "bye" midway is
    ignored.
    """
    if not isinstance(message, str):
        return None
    text = message.lower().replace("\u2019", "'").replace("\u2018", "'").strip()
    tail_from = len(text) - _GOODBYE_TAIL_CHARS if len(text) > _GOODBYE_MAX_LEN else 0
    found: set[str] = set()
    for m in _CLAUSE.finditer(text):
        kind = _clause_kind(m.group(), tail_from - m.start())
        if kind:
            found.add(kind)
    if "night" in found:
        return "night"
    return "leave" if found else None


# ── DB helpers ───────────────────────────────────────────────────────────────

async def create_conversation(conv_id: str, user_id: str = "jalsarraf") -> None:
    """Create a new conversation record scoped to a user."""
    import logging
    from .db import get_conn_autocommit
    logger = logging.getLogger(__name__)
    try:
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "INSERT INTO companion_conversations (id, user_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (conv_id, user_id),
            )
    except Exception as e:
        logger.error("Failed to create conversation: %s", e)


async def store_message(
    conversation_id: str,
    role: str,
    content: str,
    model: str = "",
    latency_ms: int | None = None,
    user_id: str = "jalsarraf",
) -> bool:
    """Store a message and update conversation turn count atomically.

    Returns True when the message was persisted, False on DB failure so the
    chat path can warn the Commander instead of silently losing the message.
    """
    import logging
    from .db import get_conn
    logger = logging.getLogger(__name__)
    try:
        async with get_conn() as conn:
            await conn.execute(
                "INSERT INTO companion_messages "
                "(conversation_id, role, content, model, latency_ms, user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (conversation_id, role, content, model, latency_ms, user_id),
            )
            await conn.execute(
                "UPDATE companion_conversations SET turn_count = turn_count + 1, "
                "model_used = %s WHERE id = %s",
                (model, conversation_id),
            )
            await conn.commit()
        return True
    except Exception as e:
        logger.error("Failed to store message: %s", e)
        return False
