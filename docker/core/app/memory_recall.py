"""Memory recall without embeddings: Postgres candidates, Claude Haiku picks.

Owner decision 2026-10-26: Klukai runs on cloud AI only. A recall asks
Postgres for up to ``RECALL_CANDIDATES`` of the user's rows, ranked by a
full-text match on their stored text (migration 260), then importance and
recency. Claude Haiku (high effort, through app/background_llm.py and the
shared daily ledger) returns the ids of the relevant ones, most relevant
first. When Haiku is unavailable, capped, too slow, or answers with nothing
usable, the Postgres ranking is used as is.

Nothing here raises into the chat path for a model problem, and no prompt or
memory text is logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from typing import Any, TypeVar

from . import background_llm
from .memory_settings import RECALL_CANDIDATES, RECALL_SELECTION_TIMEOUT_S

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

QUERY_CHARS = 1_000
TEXT_CHARS = 360
MAX_SEARCH_TERMS = 32
MAX_TERM_CHARS = 64
# The visible reply is at most RECALL_CANDIDATES short ids.
SELECTION_MAX_TOKENS = 256
# Kept well under extraction_llm.MAX_PROMPT_CHARS (24 000). With the caps
# above a typical full prompt is under 18 000 characters; text that JSON
# escaping inflates is trimmed from the lowest-ranked candidates.
PROMPT_BUDGET_CHARS = 20_000

# Letters and digits only, so a search expression is always plain words.
_TERM = re.compile(r"[^\W_]+")
_SPACE = re.compile(r"\s+")
_ID = re.compile(r"[0-9]{1,4}")


def search_terms(query: str) -> str:
    """The query's distinct words as a websearch_to_tsquery OR expression.

    Any one word can match; rows matching more of them rank higher. Returns
    ``""`` when the query has no searchable word.
    """
    terms: list[str] = []
    for word in _TERM.findall(query.lower()):
        # "or" is websearch syntax (and an English stop word anyway).
        if len(word) < 2 or len(word) > MAX_TERM_CHARS or word == "or":
            continue
        if word not in terms:
            terms.append(word)
            if len(terms) == MAX_SEARCH_TERMS:
                break
    return " or ".join(terms)


async def fetch_candidates(
    ranked_sql: str, recent_sql: str, query: str, user_id: str
) -> list[tuple[Any, ...]]:
    """Up to RECALL_CANDIDATES rows for ``user_id``, best Postgres match first.

    ``ranked_sql`` takes (search expression, user_id, limit) and orders by
    full-text match, then importance and recency. ``recent_sql`` takes
    (user_id, limit) and is used when the query has no searchable word.
    """
    from .db import get_conn

    terms = search_terms(query)
    async with get_conn() as conn:
        if terms:
            cursor = await conn.execute(ranked_sql, (terms, user_id, RECALL_CANDIDATES))
        else:
            cursor = await conn.execute(recent_sql, (user_id, RECALL_CANDIDATES))
        return list(await cursor.fetchall())


def clip(text: str, limit: int) -> str:
    """One line of at most ``limit`` characters."""
    flat = _SPACE.sub(" ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def selection_prompt(subject: str, query: str, texts: Sequence[str], limit: int) -> str:
    """The Haiku request; candidate ``n`` (1-based) has id ``"n"``."""
    return _selection_request(subject, query, texts, limit)[0]


def _selection_request(
    subject: str, query: str, texts: Sequence[str], limit: int
) -> tuple[str, int]:
    """The bounded prompt and the number of candidates actually shown."""
    lines = [
        json.dumps({"id": str(n), "text": clip(text, TEXT_CHARS)}, ensure_ascii=False)
        for n, text in enumerate(texts, start=1)
    ]

    def render(listing: list[str]) -> str:
        return (
            f"Below are {subject}, one JSON object per line. Pick the ones that "
            "are relevant to the search.\n\n"
            f"Search: {json.dumps(clip(query, QUERY_CHARS), ensure_ascii=False)}\n\n"
            "Memories:\n" + "\n".join(listing) + "\n\n"
            'Reply with only a JSON object {"ids": [...]}: the ids of at most '
            f"{limit} relevant memories, most relevant first, using only ids "
            'listed above. If none is relevant, reply {"ids": []}.'
        )

    prompt = render(lines)
    while len(prompt) > PROMPT_BUDGET_CHARS and len(lines) > 1:
        lines.pop()  # the lowest-ranked candidates go first
        prompt = render(lines)
    return prompt, len(lines)


def parse_selection(result: object, count: int, limit: int) -> list[int] | None:
    """0-based candidate indexes in Haiku's order, or None for "unusable".

    Unknown and repeated ids are dropped and the list is capped at ``limit``.
    An explicit empty list means nothing is relevant; a list holding only
    unknown ids is unusable.
    """
    if not isinstance(result, dict):
        return None
    ids = result.get("ids")
    if not isinstance(ids, list):
        return None
    picked: list[int] = []
    for raw in ids:
        index = _candidate_index(raw, count)
        if index is None or index in picked:
            continue
        picked.append(index)
        if len(picked) >= limit:
            break
    if ids and not picked:
        return None
    return picked


def _candidate_index(raw: object, count: int) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        number = raw
    elif isinstance(raw, str) and _ID.fullmatch(raw.strip()):
        number = int(raw.strip())
    else:
        return None
    return number - 1 if 1 <= number <= count else None


# Texts a finished Grok pick stored for the next chat turn. The reply that
# is already on its way never waits for this.
_PINNED: dict[str, list[str]] = {}
_PENDING_PINS: dict[str, object] = {}


def begin_pin(user_id: str) -> object:
    """Drop the previous pick and give this user's newest job ownership."""
    token = object()
    _PINNED.pop(user_id, None)
    _PENDING_PINS[user_id] = token
    return token


def finish_pin(user_id: str, token: object, texts: Sequence[str]) -> None:
    """Publish only while this job is still the newest, then release it."""
    if _PENDING_PINS.get(user_id) is token:
        del _PENDING_PINS[user_id]
        store_pinned(user_id, texts)


def store_pinned(user_id: str, texts: Sequence[str]) -> None:
    cleaned = [text.strip() for text in texts if isinstance(text, str) and text.strip()]
    if cleaned:
        _PINNED[user_id] = cleaned[:8]
    else:
        _PINNED.pop(user_id, None)


def take_pinned(user_id: str) -> list[str]:
    return _PINNED.pop(user_id, [])


async def select(
    caller: str,
    subject: str,
    query: str,
    texts: Sequence[str],
    limit: int,
    *,
    timeout: float | None = None,
    role: str = "recall",
) -> list[int] | None:
    """Haiku's pick of the relevant ``texts`` (0-based, in order), or None.

    None means "use the Postgres ranking": Haiku is unavailable or capped,
    took longer than the budget, or replied with nothing usable. The chat
    reply does not call this. A precompute after the reply may, and that
    wait is not on the way to the answer. ``role="recall"`` preempts a
    running background call in the worker; a caller nobody is waiting on
    passes ``role="background"`` so it queues behind memory extraction.
    """
    if not texts or limit <= 0:
        return None
    prompt, shown = _selection_request(subject, query, texts, limit)
    budget = RECALL_SELECTION_TIMEOUT_S if timeout is None else timeout
    kwargs: dict[str, Any] = {
        "max_tokens": SELECTION_MAX_TOKENS,
        "role": role,
    }
    if timeout is not None:
        kwargs["timeout"] = budget
    try:
        async with asyncio.timeout(budget):
            result = await background_llm.json_object(caller, prompt, **kwargs)
    except TimeoutError:
        logger.warning(
            "Memory recall %s: selection timed out; using Postgres order", caller
        )
        return None
    except Exception as error:  # background_llm never raises; belt and braces
        logger.warning(
            "Memory recall %s: selection failed (%s)", caller, type(error).__name__
        )
        return None
    picked = parse_selection(result, shown, limit)
    if picked is None and result is not None:
        logger.warning(
            "Memory recall %s: unusable selection; using Postgres order", caller
        )
    return picked


def ordered(rows: Sequence[_T], picked: list[int] | None, limit: int) -> list[_T]:
    """Haiku's pick when there is one, else the first ``limit`` rows."""
    if picked is None:
        return list(rows[:limit])
    return [rows[index] for index in picked]


def rank_score(position: int, total: int) -> float:
    """Relevance in (0, 1] from a 0-based rank: the first of ``total`` is 1."""
    return round(1.0 - position / total, 4)
