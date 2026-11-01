"""Does he want a picture right now? Claude Haiku reads it in context.

Keyword rules caught 155 of 444 realistic requests and fired on 49 of 94 ordinary
sentences ("draw the curtains"). They also could not follow a "yes" after her offer
or a "try again" after a failure. A short Haiku call now decides for any message
that could be a request. If it is unavailable or too slow, the keyword rules decide.

Never raises into the chat path. Logs the outcome only, never what was said.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import asyncio
import json
import logging
import re
import time
from typing import Any

from . import extraction_llm
from .image_prompts import needs_image
from .image_provider import asks_for_nudity
from .image_request_intent import resolve_picture_request

logger = logging.getLogger(__name__)

# His message costs a call only if it could be a request. Generous on purpose:
# Haiku rules out the rest, so only a miss here can lose a picture.
_CUE = re.compile(
    r"\b(?:draw\w*|drew|drw|darw|sketch\w*|paint\w*|doodl\w*|illustrat\w*|render\w*|"
    r"depict\w*|visuali[sz]\w*|pictures?|pics?|pix|photo\w*|selfies?|snap\w*|shot|"
    r"images?|portrait\w*|show\w*|see|look\w*|generat\w*|again|another|redo|retry|"
    r"one more|(?:make|send|give|gimme|take)(?: me| us)? (?:one|it|that|another|some|more))\b",
    re.IGNORECASE,
)
# A word or two only means yes to a picture right after one came up.
_SHORT_REPLY_WORDS = 6
_PICTURE_TALK = re.compile(
    r"\b(?:draw\w*|drew|sketch\w*|paint\w*|doodl\w*|illustrat\w*|pictures?|pics?|"
    r"photo\w*|selfies?|images?|portrait\w*)\b",
    re.IGNORECASE,
)
SYSTEM = """You read one moment of a chat between the Commander and Klukai, an anime \
character who can send him drawn pictures. Decide whether his LATEST message asks her \
for a new picture right now. Judge only that, whatever the picture would show. If he \
could well mean a picture, answer true: a missed request hurts more than an extra picture.

draw is true when he:
- tells or asks her to draw, sketch, paint, doodle, render, generate, visualize, take a \
photo, send a selfie or picture, or show what she or the two of them look like, in any \
wording, with typos or slang, after other sentences, or as a polite question ("can you \
draw it", "could I see you", "what do you look like right now", "what are you wearing \
right now", "let me see", "show me that", "I want to see you", "don't tell me, show me", \
"not words, a picture", "send nudes");
- says yes to a picture she just offered ("yes", "go ahead", "do it");
- wants a redo, a change or another after a picture ("try again", "another one", "redo \
it", "again", "make it brighter", "change it", "same thing").

draw is false when he:
- mentions pictures, drawing or looks without asking for one now ("I saw a picture of a \
dog", "that picture on the wall", "I drew the short straw", "you look tired", "another \
photo shoot", "take a picture tomorrow");
- uses those words for something else or as a figure of speech ("draw the curtains", \
"draw a bath", "draw your sword", "draw on your experience", "paint your nails", "paint \
a picture of how it was", "show me how it works", "show me what you've got", "see you \
later", "look at this bug");
- wants to meet or be with her at some time, rather than get an image ("I want to see \
you tonight");
- only asks whether she is able to draw, or about her art ("can you draw?", "do you \
like drawing?");
- asks her to recall or tell him about something, or a picture she already sent ("show \
me a memory you love", "remember that picture", "show me that picture again");
- gives a bare "show me" or "try again" with no picture in the chat or his last request;
- tells her not to, or to stop.

subject: what the picture should show, in a few plain words, from his message and the \
chat, resolving "it", "that" and "another". Empty if he left it to her. Add no clothing, \
body or intimacy detail he did not ask for himself.

again: true only when he wants a redo or another of the last picture.

Answer with only this JSON: {"draw": true or false, "subject": "...", "again": true or false}"""
# Her reply waits on this, so it must stay short. Haiku at low effort takes about 1 s.
DECISION_TIMEOUT_S = 4.0
_MAX_TOKENS = 1024
_TURNS_READ = 4
_TURN_CHARS = 400
MAX_SUBJECT_CHARS = 200
# Long enough to follow up on it, short enough not to haunt a later conversation.
REMEMBER_PICTURE_S = 1800.0

_LAST: dict[str, dict[str, Any]] = {}
_STATE_LINES = {
    "drawing": "The picture he asked for is still being drawn. It has not arrived yet.",
    "delivered": "Your last picture reached him.",
    "failed": (
        "Your last picture failed to draw and he was told so. It never arrived. "
        "If he asks again, it will be drawn again."
    ),
}


def note_picture(user_id: str, outcome: str, request: str | None = None) -> None:
    """Remember how his latest picture is going, for this process only."""
    earlier = last_picture(user_id) or {}
    _LAST[user_id] = {
        "outcome": outcome,
        "request": earlier.get("request", "") if request is None else request,
        "versions": earlier.get("versions", 0),
        "at": time.monotonic(),
    }


def last_picture(user_id: str) -> dict[str, Any] | None:
    entry = _LAST.get(user_id)
    if entry is None:
        return None
    if time.monotonic() - entry["at"] > REMEMBER_PICTURE_S:
        del _LAST[user_id]
        return None
    return {key: value for key, value in entry.items() if key != "at"}


def state_note(user_id: str) -> str:
    """One line for her prompt on what became of his last picture."""
    entry = last_picture(user_id)
    return _STATE_LINES.get(entry["outcome"], "") if entry else ""


def could_be_request(
    content: str,
    prior_turns: Sequence[Mapping[str, object]],
    last: Mapping[str, object] | None,
) -> bool:
    """Whether this message is worth asking Haiku about."""
    if _CUE.search(content):
        return True
    if len(content.split()) > _SHORT_REPLY_WORDS:
        return False
    if last is not None:
        return True
    return any(
        _PICTURE_TALK.search(str(turn.get("content"))) for turn in prior_turns[-2:]
    )


def _prompt(
    content: str,
    prior_turns: Sequence[Mapping[str, object]],
    last: Mapping[str, object] | None,
) -> str:
    lines = [
        f"{turn.get('role')}: {str(turn.get('content'))[:_TURN_CHARS]}"
        for turn in prior_turns[-_TURNS_READ:]
    ]
    chat = "\n".join(lines) or "(nothing yet)"
    picture = (
        f"His last picture request: {str(last.get('request'))[:_TURN_CHARS]} "
        f"(it {last.get('outcome')})"
        if last
        else "No recent picture."
    )
    return f"Chat so far:\n{chat}\n\n{picture}\n\nHis latest message: {content}"


def _parsed(text: str) -> dict[str, Any] | None:
    """His verdict, or None when the answer is not usable."""
    try:
        verdict = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(verdict, dict) or not isinstance(verdict.get("draw"), bool):
        return None
    return verdict


def _subject(
    verdict: Mapping[str, Any],
    content: str,
    prior_turns: Sequence[Mapping[str, object]],
    last: Mapping[str, object] | None,
) -> str:
    raw = verdict.get("subject")
    if not isinstance(raw, str):
        return ""
    subject = " ".join(raw.split())[:MAX_SUBJECT_CHARS]
    if asks_for_nudity(subject):
        # Only his own words can ask for it, never her replies or Haiku's.
        his_words = [content, str((last or {}).get("request", ""))]
        his_words += [
            str(turn.get("content"))
            for turn in prior_turns[-_TURNS_READ:]
            if turn.get("role") == "user"
        ]
        if not any(asks_for_nudity(words) for words in his_words):
            return ""
    return subject


async def _ask(
    content: str,
    prior_turns: Sequence[Mapping[str, object]],
    last: Mapping[str, object] | None,
) -> dict[str, Any] | None:
    try:
        text = await asyncio.wait_for(
            extraction_llm.complete_claude(
                _prompt(content, prior_turns, last),
                max_tokens=_MAX_TOKENS,
                route="picture_intent",
                system=SYSTEM,
                timeout=DECISION_TIMEOUT_S,
            ),
            DECISION_TIMEOUT_S,
        )
    except Exception as error:  # the chat path must never see a raise
        logger.info("Picture intent unavailable (%s)", type(error).__name__)
        return None
    verdict = _parsed(text)
    if verdict is None:
        logger.info("Picture intent answer was unusable")
    return verdict


async def resolve(
    content: str,
    prior_turns: Sequence[Mapping[str, object]],
    user_id: str,
    *,
    detector: Callable[[str], bool] = needs_image,
) -> str | None:
    """The render request for this message, or None when he isn't asking for one."""
    keywords = resolve_picture_request(content, prior_turns, detector=detector)
    last = last_picture(user_id)
    if not extraction_llm.uses_anthropic() or not could_be_request(
        content, prior_turns, last
    ):
        return keywords
    verdict = await _ask(content, prior_turns, last)
    if verdict is None:
        return keywords
    logger.info("Picture intent: draw=%s", verdict["draw"])
    if not verdict["draw"]:
        return None
    request = keywords or content
    subject = _subject(verdict, content, prior_turns, last)
    if subject:
        request = f"{request}\n\nSubject: {subject}"
    if verdict.get("again") is True:
        # The same words within ten minutes would hand back the same picture.
        versions = (last or {}).get("versions", 0) + 1
        request = f"{request}\n\nNew version {versions + 1}."
        note_picture(user_id, (last or {}).get("outcome", "drawing"))
        _LAST[user_id]["versions"] = versions
    return request
