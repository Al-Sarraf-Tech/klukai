"""Negation-aware interpretation of an explicitly offered command decision."""

from __future__ import annotations

import re
from typing import Any

KEYWORD_MAX_LEN = 160

_WORDS = re.compile(r"[a-z0-9#']+")
_FILLERS = frozenset(
    {
        "ok",
        "okay",
        "hmm",
        "hm",
        "um",
        "uh",
        "well",
        "then",
        "please",
        "definitely",
        "obviously",
        "sure",
        "yeah",
        "yes",
        "i",
        "guess",
        "think",
        "go",
        "going",
        "with",
        "the",
        "option",
        "choice",
        "letter",
        "lets",
        "let's",
        "i'll",
        "ill",
        "i'd",
        "id",
        "say",
        "pick",
        "take",
        "choose",
        "answer",
        "my",
        "is",
    }
)
_NEGATIONS = frozenset({"not", "no", "never", "don't", "dont", "nah", "without"})
_ORDINALS = {"first": 0, "second": 1, "third": 2, "last": -1, "former": 0, "latter": -1}
_DIGITS = {"1": 0, "2": 1, "3": 2}

_CUE_LETTER = re.compile(r"\b(?:option|choice|answer)\s*[:#]?\s*([abc])\b")
_VERB_LETTER = re.compile(
    r"\b(?:go(?:ing)?\s+with|pick|take|choose|chose|say|vote(?:\s+for)?|prefer)\s+([abc])\b"
    r"(?=\s*(?:$|[.!,;)]|\s+(?:please|then|for\s+sure|definitely|obviously|i\s+think)))"
)
_LEAD_LETTER = re.compile(r"^\s*([abc])\s*(?:[.!,:;)\-]|$)")
_UPPER_LETTER = re.compile(r"(?<![A-Za-z'])([BC])(?![A-Za-z'\-])")
_ORDINAL_PHRASE = re.compile(
    r"\b(first|second|third|last)\s+(?:one|option|choice)\b"
    r"|\bthe\s+(former|latter)\b"
)
_DIGIT_CUE = re.compile(r"(?:\b(?:option|choice|number)|#)\s*([123])\b")


def _negated(text: str, start: int) -> bool:
    """A negation in the same clause, within 3 words before ``start``."""
    clause = re.split(r"[.,;!?]", text[:start])[-1]
    return any(w in _NEGATIONS for w in _WORDS.findall(clause)[-3:])


def _explicit_keys(text: str, original: str, keys: list[str]) -> set[str]:
    """Keys named by letter, ordinal or number."""
    found: list[tuple[str, int]] = []  # (key, match start)

    def by_index(i: int) -> str | None:
        return keys[i] if -len(keys) <= i < len(keys) else None

    core = [w for w in _WORDS.findall(text) if w not in _FILLERS]
    if len(core) == 1:
        w = core[0]
        if w.upper() in keys:
            found.append((w.upper(), 0))
        elif w in _ORDINALS or w in _DIGITS:
            k = by_index(_ORDINALS[w] if w in _ORDINALS else _DIGITS[w])
            if k:
                found.append((k, 0))
    for rx in (_CUE_LETTER, _VERB_LETTER, _LEAD_LETTER):
        found += [(m.group(1).upper(), m.start()) for m in rx.finditer(text)]
    found += [(m.group(1), m.start()) for m in _UPPER_LETTER.finditer(original)]
    for m in _ORDINAL_PHRASE.finditer(text):
        k = by_index(_ORDINALS[m.group(1) or m.group(2)])
        if k:
            found.append((k, m.start()))
    for m in _DIGIT_CUE.finditer(text):
        k = by_index(_DIGITS[m.group(1)])
        if k:
            found.append((k, m.start()))
    return {k for k, start in found if k in keys and not _negated(text, start)}


def parse_decision_answer(message: str, options: list[dict[str, Any]]) -> str | None:
    """Which option ``message`` picks: its key, or None if none/ambiguous.

    Letters ("B", "option b", "go with a"), ordinals ("the second one", "the
    latter") and numbers ("#2") win outright. Otherwise option keywords and
    labels are scored, ignoring negated hits ("not the loud one"), in
    messages up to ~160 characters. Questions never resolve.
    """
    if not isinstance(message, str) or not options:
        return None
    original = message.strip().replace("’", "'")
    text = original.lower()
    if not text or text.endswith("?"):
        return None
    keys = [str(o.get("key", "")).upper() for o in options]

    explicit = _explicit_keys(text, original, keys)
    if explicit:
        return explicit.pop() if len(explicit) == 1 else None
    if len(text) > KEYWORD_MAX_LEN:
        return None

    scores: dict[str, int] = {}
    for key, opt in zip(keys, options, strict=True):
        phrases = {*(opt.get("keywords") or []), str(opt.get("label", "")).lower()}
        hits = 0
        for phrase in filter(None, phrases):
            m = re.search(r"\b" + re.escape(phrase) + r"\b", text)
            if m and not _negated(text, m.start()):
                hits += 1
        scores[key] = hits
    best = max(scores.values())
    winners = [k for k, v in scores.items() if v == best]
    return winners[0] if best > 0 and len(winners) == 1 else None
