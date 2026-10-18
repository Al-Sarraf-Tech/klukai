"""Deterministic interpretation and admission of requested outfit changes."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .wardrobe_catalog import Outfit, catalog, MAX_REQUESTS_PER_DAY

_VERBS = (
    r"wear|put on|change into|change in to|switch into|switch to|dress in|"
    r"dress up in|try on|slip into"
)
# Phrasings with "you" built in: "I want to see you in the maid outfit".
_YOU_IN = r"(?:see|want|need) you in"
_NEGATION = re.compile(
    r"\b(?:don'?t|do not|never|stop|quit|no more|not|can'?t|cannot|won'?t|shouldn'?t|"
    r"mustn'?t|wouldn'?t|couldn'?t|didn'?t|doesn'?t)\s+(?:\w+\s+){0,2}$"
)
_QUESTION_ABOUT = re.compile(r"\b(?:do|did|have) you (?:ever |still |even )?$")
# The wear-verb must be addressed to HER: an imperative opening a clause
# ("Klukai, wear...", "please put on..."), or you-led with a modal ("can you",
# "would you", "I want you to", "you should"). A bare "you" is not a request:
# "I like it when you wear the hoodie", "if you wear the coat you'll be fine"
# and "I'm going to wear a coat" are about her habits or about him.
_ADDRESSED = re.compile(
    r"(?:^|[.!?;,:]\s*|\b(?:please|just|now|go|and|so|then|ok|okay|hey)\s+)$"
    r"|\b(?:can|could|would|will|might|should)\s+you\s+(?:please\s+|maybe\s+|just\s+)?$"
    r"|\byou(?:'d| would| could| can| will| might| should| to)\s+(?:please\s+|maybe\s+|just\s+)?$"
)


def detect_outfit_request(
    message: str, cat: dict[str, Outfit] | None = None
) -> str | None:
    """A request for HER to change into a named outfit → its id, else None.

    High precision on purpose — every hit is a real decision (a change, a
    counted request, or a refusal he never asked for). Needs a wear-verb
    addressed to her, a catalog alias in the same sentence, and no negation
    or idle question ("don't wear...", "do you ever wear...").
    """
    cat = catalog() if cat is None else cat
    lower = " ".join(message.lower().split())
    aliases = [
        (alias, o.id) for o in cat.values() for alias in (*o.aliases, o.name.lower())
    ]
    for verb in re.finditer(rf"\b(?:{_YOU_IN}|{_VERBS})\b", lower):
        before = lower[: verb.start()]
        if _NEGATION.search(before) or _QUESTION_ABOUT.search(before):
            continue
        if not verb.group(0).endswith("you in") and not _ADDRESSED.search(before):
            continue
        tail = re.split(r"[.!?;]", lower[verb.end() : verb.end() + 48], maxsplit=1)[0]
        # The outfit named FIRST after the verb wins ("wear the coat over the
        # maid uniform" is the coat); at the same spot, the longest alias.
        hits = [
            (m.start(), -len(alias), oid)
            for alias, oid in aliases
            if (m := re.search(rf"\b{re.escape(alias)}\b", tail))
        ]
        if hits:
            return min(hits)[2]
    return None


@dataclass(frozen=True)
class RequestOutcome:
    outfit_id: str
    decision: str  # comply | already | locked | refuse | unacknowledged | deny | not_today | not_now | occasion_only | limit
    note: str
    # She won't discuss her wardrobe at all: the prompt must not even name it.
    discreet: bool = False


def _band_note(level: int) -> str:
    if level >= 7:
        return (
            "Comply without fuss; let him see — briefly — that you're pleased he asked."
        )
    if level >= 5:
        return "Comply; you may pretend you were already planning to."
    if level >= 3:
        return "Comply, but make clear it's your decision, not an order he gave you. You may bill it."
    return "Comply, curtly, for practical reasons only."


# Days that "matter" enough for the wedding gown below the oath: its own
# occasions (White Day) and his birthday — not Halloween or Christmas week.
_OATH_DAYS = frozenset({"commander_birthday"})


def decide_request(
    o: Outfit,
    level: int,
    *,
    current_id: str,
    request_changes: int,
    occasions_today: frozenset[str],
    hour: int,
    deployed: bool = False,
    ui: bool = False,
) -> RequestOutcome:
    """Deterministic outcome of 'wear X'. The LLM only voices it.

    ``ui=True`` is the PWA wardrobe button: the unlock level already is the
    permission there, so the low-affection reticence and the daily cap don't
    apply — but the onesie, the oath, the seasons and the hour still do.
    """
    if deployed:
        return RequestOutcome(
            o.id, "not_now", "You're deployed. Not now — the mission kit stays on."
        )
    if o.deny:
        return RequestOutcome(
            o.id,
            "deny",
            "Deny the thing exists. Flatly. Change the subject.",
            discreet=True,
        )
    if level < o.visible_at:
        return RequestOutcome(
            o.id,
            "unacknowledged",
            "Do not acknowledge owning any such outfit.",
            discreet=True,
        )
    if o.id == current_id:
        return RequestOutcome(
            o.id, "already", "You are already wearing it. Note that he didn't notice."
        )
    if level < o.unlock_level:
        hint = (
            "Refuse without discussing your wardrobe at all."
            if level < 3
            else "Refuse: 'Not yet.' Nothing more."
            if level < 5
            else "Refuse, but hint — in your voice — that it's a matter of trust, not time."
        )
        return RequestOutcome(o.id, "locked", hint, discreet=level < 3)
    if not ui and level < 3 and o.category not in ("duty", "weather", "training"):
        return RequestOutcome(
            o.id,
            "refuse",
            "'My attire is not a topic for discussion, Commander.'",
            discreet=True,
        )
    if (
        o.category == "oath"
        and level < 9
        and not occasions_today & (_OATH_DAYS | set(o.occasions))
    ):
        return RequestOutcome(
            o.id, "not_today", "'...Not today. Ask me on a day that matters.'"
        )
    if o.category == "seasonal" and not (occasions_today & set(o.occasions)):
        return RequestOutcome(
            o.id, "occasion_only", "Wrong season for it. Say so, dryly."
        )
    if o.category == "sleep" and 5 <= hour < 21:
        return RequestOutcome(
            o.id, "not_now", "Late-watch clothes are not for daytime. Decline."
        )
    if not ui and request_changes >= MAX_REQUESTS_PER_DAY:
        return RequestOutcome(
            o.id,
            "limit",
            "'I am not a mannequin, Commander.' You do not change again today.",
        )
    return RequestOutcome(o.id, "comply", _band_note(level))


def request_block(outcome: RequestOutcome, cat: dict[str, Outfit] | None = None) -> str:
    """Per-message prompt block voicing a decided request."""
    cat = catalog() if cat is None else cat
    o = cat[outcome.outfit_id]
    granted = outcome.decision == "comply"
    state = (
        f"You agreed: you are now wearing {o.name}."
        if granted
        else "You are NOT changing. Do not describe changing, and do not promise to later."
    )
    if outcome.discreet:
        # Below the bond where she discusses her wardrobe: no name, no stance —
        # nothing for the reply to echo back.
        return (
            "WARDROBE REQUEST: The Commander asked you to change clothes.\n"
            f"OUTCOME (already decided — do not change it): {state}\n"
            f"Play it: {outcome.note} Do not name or describe any outfit.\n"
            "Keep it to a sentence, then carry on with the conversation."
        )
    stance = f"\nHow you see it: {o.stance}" if o.stance else ""
    return (
        f"WARDROBE REQUEST: The Commander asked you to wear {o.name}.\n"
        f"OUTCOME (already decided — do not change it): {state}\n"
        f"Play it: {outcome.note}{stance}\n"
        "Keep it to a sentence or two, then carry on with the conversation."
    )
