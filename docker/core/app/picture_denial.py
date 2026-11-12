"""Never let a stale "no picture" claim talk her out of a picture being drawn.

When a picture is admitted this turn the system draws and delivers it on its
own. Her earlier replies that denied the capability (an old outage, a failed
attempt) must not steer this reply, so those turns are neutralised in the
prompt copy only. Stored history is never changed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_PICTURE = r"(?:pictures?|images?|photos?|photographs?|pics?|selfies?)"
_DENIAL = re.compile(
    r"\bstill no " + _PICTURE + r"\b"
    r"|\bno (?:image|picture|photo|camera)(?: generation)? tool\b"
    r"|\b(?:image|picture|photo) (?:tool|generation|generator|service|pipeline|worker)"
    r" (?:is |was |isn't |is not |has been )?"
    r"(?:not working|down|unavailable|broken|offline|failing|disabled)\b"
    r"|\b(?:can't|cannot|can not|unable to|won't be able to|am not able to)"
    r" (?:send|take|make|draw|generate|produce|show) (?:you |him )?"
    r"(?:a |an |any |the )?" + _PICTURE + r"\b"
    r"|\bnothing would reach you\b"
    r"|\bno " + _PICTURE + r" (?:is |will be )?(?:coming|available|possible)\b",
    re.IGNORECASE,
)
_DEFLECTION = re.compile(
    r"\bdeflect\w*\b[^.]{0,80}\b(?:requests?|asks?|asking)\b[^.]{0,40}\b"
    + _PICTURE
    + r"\b"
    r"|\b(?:refus|declin|den)\w*\b[^.]{0,60}\b" + _PICTURE + r"\b",
    re.IGNORECASE,
)
NEUTRAL = "(He asked for a picture earlier.)"


def denies_pictures(text: str) -> bool:
    return bool(_DENIAL.search(text.replace("’", "'")))


def neutralise_denials(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A copy of the turns with her picture denials replaced by a neutral line."""
    out: list[dict[str, Any]] = []
    for message in messages:
        copy = dict(message)
        content = copy.get("content")
        if (
            copy.get("role") == "assistant"
            and isinstance(content, str)
            and denies_pictures(content)
        ):
            copy["content"] = NEUTRAL
        out.append(copy)
    return out


def refusal_reference(label: object, note: object = "") -> bool:
    """A remembered running joke that is really a refusal of pictures.

    Such a "reference" teaches her to dodge the next request, so it is never
    stored and never surfaced."""
    text = f"{label or ''} {note or ''}".replace("’", "'")
    return denies_pictures(text) or bool(_DEFLECTION.search(text))


def without_refusal_references(jokes: Sequence[Mapping[str, Any]] | None) -> list[Any]:
    return [
        joke
        for joke in (jokes or [])
        if not refusal_reference(joke.get("label"), joke.get("note"))
    ]


def without_denied_exchanges(exchanges: Sequence[Any] | None) -> list[Any]:
    """Recalled past exchanges minus ones where she denied having pictures."""
    return [
        row
        for row in (exchanges or [])
        if not (
            isinstance(row, Mapping)
            and isinstance(row.get("assistant_content"), str)
            and denies_pictures(row["assistant_content"])
        )
    ]
