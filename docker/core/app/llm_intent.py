"""Deterministic public-information intent; no model inference or side effects."""

from __future__ import annotations
import re


def message_needs_agent(message: str, signals: list[str]) -> bool:
    lower = message.casefold()
    explicit = _explicit_intent(lower)
    if explicit is not None:
        return explicit
    requested = _is_requested(message, lower)
    return _requested_data(lower, requested) or _compact_question(
        message, lower, requested, signals
    )


def _explicit_intent(lower: str) -> bool | None:
    # Explicit public-web requests take priority over conversational forms
    # such as "could you". No classifier inference is needed.
    if re.search(r"https?://\S+|\bwww\.\S+", lower):
        return True
    if re.search(r"\b(?:web|internet|online|brave)\b", lower) and re.search(
        r"\b(?:search|check|browse|look|read|find|use)\b", lower
    ):
        return True

    # Possessive subjects identify her state/world; freshness words do not
    # make "your latest dream" a public-information request. A definite
    # subject qualified by a destination or organization ("the mission to
    # Europa", "the team at OpenAI") is not enough to establish roleplay.
    if re.search(
        r"\b(?:your|our)\s+(?:(?:first|next|last|current|new|latest)\s+)?"
        r"(?:sector|hq|base|camp|squad|mission|team|weapon|memories|dreams?|mood|feelings|outfit)\b"
        r"|\bthe\s+(?:(?:first|next|last|current|new|latest)\s+)?"
        r"(?:sector|hq|base|camp|squad|mission|team|weapon|memories)\b"
        r"(?!\s+(?:at|to|of|for|in)\s+(?!(?:your|our|my)\b)\w)"
        r"|\b(?:t-doll|squad leader)\b",
        lower,
    ):
        return False

    if re.search(
        r"\b(?:search|look\s+up|find\s+out|research|browse|check\s+online)\b", lower
    ):
        return True

    if re.search(r"\b(?:do|would|could) you (?:like|prefer|enjoy|hate)\b", lower):
        return False

    return None


def _is_requested(message: str, lower: str) -> bool:
    return "?" in message or bool(
        re.search(
            r"\b(?:can|could|would|please|tell|show|give|find|check|convert|calculate|summarize)\b"
            r"|^\s*(?:what|who|where|when|how|read)\b",
            lower,
        )
    )


def _requested_data(lower: str, requested: bool) -> bool:
    if requested and re.search(
        r"\b(?:weather|forecast|rain|snow|temperature|humidity|news|headlines|current|latest|"
        r"prices?|stocks?|scores?|exchange\s+rates?|"
        r"currency|arxiv|doi|papers?|documentation|docs|references?|wikipedia|"
        r"encyclop(?:ae|e)dia|stack\s*(?:overflow|exchange))\b",
        lower,
    ):
        return True
    if requested and re.search(
        r"\b(?:who is|where is|when did|how many people|how much does|what time)\s+"
        r"(?!you\b|your\b|our\b|my\b|klukai\b|commander\b)",
        lower,
    ):
        return True

    # Numeric conversion requests need the public units/currency tools,
    # including forms such as "how many pounds is 5 kg?".
    if (
        requested
        and re.search(r"\d", lower)
        and re.search(
            r"\b(?:convert|usd|eur|gbp|jpy|cad|aud|chf|cny|inr|dollars?|euros?|yen|"
            r"pounds?|miles?|kilometers?|kilometres?|meters?|metres?|feet|inches|"
            r"kg|lbs?|kilograms?|grams?|ounces?|liters?|litres?|gallons?|celsius|fahrenheit)\b",
            lower,
        )
    ):
        return True

    return False


def _compact_question(
    message: str, lower: str, requested: bool, signals: list[str]
) -> bool:
    # Preserve compact external prompts ("latest weather") and familiar
    # factual questions without turning a casual weather remark into work.
    rp_context = any(
        re.search(rf"\b{re.escape(w)}\b", lower)
        for w in [
            "you",
            "your",
            "klukai",
            "commander",
            "feel",
            "think",
            "opinion",
            "favorite",
            "like",
            "hate",
            "they",
            "them",
            "squad",
            "mission",
            "team",
            "soldier",
            "t-doll",
            "remember",
            "discover",
            "did we",
            "did she",
            "did he",
            "would",
            "could",
            "should",
            "shall",
            "base",
            "hq",
            "camp",
            "sector",
            "area",
            "weapon",
        ]
    )

    if (
        not rp_context
        and (requested or len(lower.split()) <= 4)
        and any(re.search(rf"\b{re.escape(signal)}\b", lower) for signal in signals)
    ):
        return True

    # Question marks: only trigger agent if the question is clearly about
    # real-world external info, NOT in-character RP or conversational questions.
    if (
        "?" in message
        and not rp_context
        and any(
            w in lower
            for w in [
                "who is",
                "what is the price",
                "how much does",
                "how many people",
                "where is",
                "when did",
            ]
        )
    ):
        return True

    return False
