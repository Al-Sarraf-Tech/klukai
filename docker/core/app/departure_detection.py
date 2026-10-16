"""Clause-aware sign-off detection; a pure language boundary."""

from __future__ import annotations

import re
from typing import Literal

from .image_gen_constants import SQUAD_KEYWORDS

GoodbyeKind = Literal["night", "leave"]

_GOODBYE_MAX_LEN = 160  # longer messages only count a goodbye at the very end
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
_LEAVE_VERB = (
    r"(?:go|run|bounce|dip|jet|split|head\s+out|get\s+going|log\s+off|sign\s+off)"
)
_WORKPLACE = r"(?:work|class|school|the\s+gym|my\s+shift|a\s+meeting|practice)"
_NOT_A_GOODNIGHT = (
    r"(?!\s+(?:on|with|in|through|early|late|more|better|properly|stor)\w*\b)"
)

_NIGHT_PHRASES = tuple(
    re.compile(p)
    for p in (
        # "good night's sleep" describes a night already slept, not this one.
        r"\bgood\s*-?\s*night\b(?!\s*'s\b)(?!\s+stor)",
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
    )
)

_LEAVE_PHRASES = tuple(
    re.compile(p)
    for p in (
        r"\b(?:gotta|got\s+to|have\s+to|need\s+to|must|should|better|gonna|going\s+to|about\s+to|time\s+to)\s+"
        + _LEAVE_VERB
        + _END,
        r"\b(?:gotta|got\s+to|have\s+to|need\s+to|must)\s+go\s+to\s+"
        + _WORKPLACE
        + _END,
        r"\b(?:heading|headed|off|leaving)\s+(?:to|for)\s+" + _WORKPLACE + _END,
        r"\b(?:heading|headed|logging|signing)\s+(?:out|off)\b" + _END,
        r"\bi'?m\s+(?:off|out|leaving|outta\s+here)\b" + _END,
        r"\bgtg\b|\bg2g\b|\bttyl\b|\bttyt\b|\bcya\b|\bbrb\b",
        r"\b(?:good\s*-?\s*)?by+e+(?:\s*-?\s*by+e+)?\b",
        r"\bpeace\s+out\b",
        r"\btalk\s+(?:to\s+(?:you|ya|u)\s+)?(?:tomorrow|tmrw|tmr|later|soon|in\s+the\s+morning)\b",
        r"\b(?:see|catch)\s+(?:you|ya|u)\s+(?:later|tomorrow|tmrw|tmr|soon|in\s+the\s+morning|in\s+a\s+bit)\b",
        r"^\s*(?:(?:ok(?:ay)?|alright|well|anyway)\s+)?(?:see|catch)\s+(?:you|ya|u)\b"
        + _END,
        r"\buntil\s+(?:tomorrow|next\s+time)\b",
        r"またね|じゃあね|バイバイ|\bmata\s*ne\b",
    )
)

# Whole-clause sign-offs once fillers and pet names are stripped.
_NIGHT_ALONE = frozenset({"night", "nite", "n8"})
_LEAVE_ALONE = frozenset(
    {"later", "laters", "l8r", "peace", "ciao", "adios", "farewell"}
)
_ALONE_STRIP = frozenset(
    {
        "ok",
        "okay",
        "k",
        "kk",
        "alright",
        "well",
        "anyway",
        "anyways",
        "so",
        "welp",
        "then",
        "klukai",
        "kluk",
        "love",
        "babe",
        "baby",
        "dear",
        "darling",
        "sweetheart",
        "princess",
        "beautiful",
        "hon",
        "honey",
        "everyone",
        "all",
    }
)

_NEGATIONS = frozenset(
    {
        "not",
        "never",
        "don't",
        "dont",
        "won't",
        "wont",
        "can't",
        "cant",
        "isn't",
        "isnt",
        "aren't",
        "arent",
        "ain't",
        "aint",
        "shouldn't",
        "shouldnt",
        "didn't",
        "didnt",
        "wouldn't",
        "wouldnt",
    }
)
_OTHER_SUBJECTS = frozenset(
    {
        "you",
        "you're",
        "youre",
        "u",
        "ur",
        "your",
        "she",
        "she's",
        "shes",
        "he",
        "he's",
        "hes",
        "her",
        "his",
        "they",
        "they're",
        "theyre",
        "their",
        "we",
        "we're",
        "let's",
        "lets",
    }
) | frozenset(SQUAD_KEYWORDS)
_WISHES = frozenset({"want", "wanna", "wait", "hope", "hate", "love", "like", "miss"})
# "it was a good night", "had a good night's sleep", "what a good night": the
# phrase describes a night that already happened, not one he is heading into.
_PAST_ARTICLE = frozenset(
    {
        ("was", "a"),
        ("had", "a"),
        ("what", "a"),
        ("such", "a"),
        ("quite", "a"),
    }
)


def _goodbye_blocked(prefix: str) -> bool:
    """True when the words before a match make it not his goodbye."""
    words = _WORD.findall(prefix)
    return (
        any(w in _NEGATIONS for w in words[-4:])
        or any(w in _OTHER_SUBJECTS or w in _WISHES for w in words[-2:])
        or tuple(words[-2:]) in _PAST_ARTICLE
    )


def _next_clause_has_substance(rest: str) -> bool:
    """True when the clause after this one says more than a name or a filler."""
    m = _CLAUSE.match(rest)
    return m is not None and any(
        w not in _ALONE_STRIP for w in _WORD.findall(m.group())
    )


def _clause_kind(clause: str, min_end: int, rest: str = "") -> GoodbyeKind | None:
    """Goodbye kind for one clause; matches ending before ``min_end`` don't count.

    ``rest`` is the text after the clause, consulted only for a bare
    ``"later,"``: opening a longer thought ("Later, I want to try the new
    route") it is a time reference, while "Later, Klukai" is still a sign-off.
    """
    if clause.rstrip().endswith("?"):
        return None
    core = [w for w in _WORD.findall(clause) if w not in _ALONE_STRIP]
    alone = core[0] if len(core) == 1 and len(clause) >= min_end else ""
    if alone in _NIGHT_ALONE:
        return "night"
    kinds: tuple[tuple[GoodbyeKind, tuple[re.Pattern[str], ...]], ...] = (
        ("night", _NIGHT_PHRASES),
        ("leave", _LEAVE_PHRASES),
    )
    for kind, phrases in kinds:
        for phrase in phrases:
            for m in phrase.finditer(clause):
                if m.end() >= min_end and not _goodbye_blocked(clause[: m.start()]):
                    return kind
    if alone in _LEAVE_ALONE:
        if clause.rstrip().endswith(",") and _next_clause_has_substance(rest):
            return None
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
        kind = _clause_kind(m.group(), tail_from - m.start(), rest=text[m.end() :])
        if kind:
            found.add(kind)
    if "night" in found:
        return "night"
    return "leave" if found else None


# ── DB helpers ───────────────────────────────────────────────────────────────
