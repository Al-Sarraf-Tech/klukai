"""Event sensitivity, identity and display language; no persistence."""

from __future__ import annotations

import re

_SENSITIVE_RE = re.compile(
    r"\b(funeral|memorial|burial|wake|hospital|hospice|surgery|chemo\w*|biopsy|"
    r"oncolog\w*|icu|court|hearing|trial|lawyer|custody|divorce|break-?up|"
    r"sentencing|deposition|eviction)\b",
    re.I,
)
_MEDICAL_RE = re.compile(
    r"\b(doctor|dr|dentist|dental|checkup|check-up|physical|clinic|blood ?(?:test|work|draw)|"
    r"lab ?work|scan|mri|x-?ray|ultrasound|vaccin\w*|therap\w*|physio\w*|"
    r"optometrist|eye exam|dermatolog\w*|procedure|medical)\b",
    re.I,
)

# Ordered: the first match names the event for de-duplication, so two
# mentions of "the interview" on the same day collapse into one chain.
_CATEGORIES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rf"\b(?:{pattern})\b", re.I))
    for name, pattern in (
        ("funeral", r"funeral|memorial|burial|wake"),
        ("surgery", r"surgery|procedure|biopsy"),
        ("hospital", r"hospital|hospice"),
        ("court", r"court|hearing|trial|deposition|sentencing"),
        ("flight", r"flight|flying|plane"),
        ("interview", r"interviews?"),
        ("presentation", r"presentation|pitch|demo|talk|speech"),
        (
            "medical",
            r"doctor|dr|dentist|dental|checkup|check-up|physical|clinic|scan|mri|x-?ray|therap\w*",
        ),
        ("exam", r"exams?|test|midterms?|finals?|quiz"),
    )
)

_CATEGORY_MAP = dict(_CATEGORIES)

_STOPWORDS = frozenset(
    "a an the my your his her our their at with for to on in of and or "
    "this that next tomorrow today tonight".split()
)


def classify(what: str, llm_sensitivity: str | None = None) -> str:
    """``sensitive`` > ``medical`` > ``normal``.

    The model may only *raise* sensitivity: a keyword hit always wins, and an
    LLM "sensitive" is trusted even without one. Nothing can talk a funeral
    down to a cheerful OPORD.
    """
    if (
        _SENSITIVE_RE.search(what)
        or (llm_sensitivity or "").strip().lower() == "sensitive"
    ):
        return "sensitive"
    if _MEDICAL_RE.search(what):
        return "medical"
    return "normal"


def category(what: str) -> str:
    for name, pattern in _CATEGORIES:
        if pattern.search(what):
            return name
    words = [w for w in re.findall(r"[a-z0-9']+", what.lower()) if w not in _STOPWORDS]
    return " ".join(words) or "event"


def normalize_what(what: str) -> str:
    """Clean the model's noun phrase for display: "my X"/"his X" become
    "your X" (she is talking *to* him); bare articles are dropped."""
    text = " ".join(str(what).split()).strip(" .,!?;:")[:80].strip()
    lowered = text.lower()
    for prefix in ("my ", "his "):
        if lowered.startswith(prefix):
            return "your " + text[len(prefix) :]
    for prefix in ("a ", "an ", "the "):
        if lowered.startswith(prefix):
            return text[len(prefix) :]
    return text


# ── when-hint resolution ────────────────────────────────────────────────────
