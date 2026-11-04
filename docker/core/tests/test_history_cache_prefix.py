"""The history prefix stays byte-stable across turns, through compaction.

xAI caches the longest exact prefix of a request. It stalled at about 1152
cached tokens while compaction rewrote the history every second exchange.
Now the oldest turns leave only in whole chunks: between chunk boundaries each
turn's request starts with the previous turn's persona and history, byte for
byte, and the earlier-conversation summary rides after the history.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from app import background as bg
from app import context
from app.conversation_continuity import HISTORY_CHUNK, cached_history
from app.llm_completions import split_prompt
from app.models import SessionState
from app.personality.system_prompt import PromptText
from app.xai_chat import conversation_messages


def _request(session: SessionState) -> list[dict[str, str]]:
    """Exactly what the xAI chat path sends for this session, as built by
    chat_handlers (summary as a system note, then the cached window)."""
    messages: list[dict[str, str]] = []
    if session.context_summary:
        messages.append(
            {
                "role": "system",
                "content": f"[Earlier conversation summary: {session.context_summary}]",
            }
        )
    messages += [
        {"role": t["role"], "content": t["content"]} for t in cached_history(session.turns)
    ]
    prompt = PromptText(
        f"PERSONA\n\nmood and memories for turn {len(session.turns)}", "PERSONA"
    )
    persona, turns, volatile, _flat = split_prompt(prompt, messages)
    return conversation_messages(persona, turns, volatile)


def _cached_part(request: list[dict[str, str]]) -> bytes:
    """Persona and history: everything before this turn's volatile context,
    as the open JSON array the next request must start with."""
    return json.dumps(request[:-1]).encode()[:-1]


async def _compact(session: SessionState, summary: str) -> SessionState:
    store = AsyncMock()
    store.get_session.return_value = session.model_copy(deep=True)
    store.session_owner = lambda _key: _Owner()
    with patch.object(bg, "memory", store), patch(
        "app.fact_extractor.compact_turns", new=AsyncMock(return_value=summary)
    ):
        await bg.background_compaction(session, user_id="u")
    return store.get_session.return_value


class _Owner:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_args):
        return False


async def test_the_prefix_is_byte_stable_between_chunk_boundaries():
    session = SessionState(conversation_id="c")
    previous: bytes | None = None
    moved = compactions = 0
    for n in range(40):
        session.turns.append({"role": "user", "content": f"line {n} from him"})
        request = _request(session)
        body = json.dumps(request).encode()
        if previous is not None and not body.startswith(previous):
            moved += 1
        previous = _cached_part(request)
        session.turns.append({"role": "assistant", "content": f"her answer {n}"})
        if len(session.turns) >= context.COMPACT_THRESHOLD:
            session = await _compact(session, f"summary after {n}")
            compactions += 1
    # The prefix moves only when a whole chunk leaves: once per
    # HISTORY_CHUNK turns, never every turn or every second exchange.
    assert 0 < compactions <= 40 * 2 // HISTORY_CHUNK
    # (A compaction after the last exchange has no next request to move.)
    assert compactions - 1 <= moved <= compactions


async def test_compaction_folds_whole_chunks_and_keeps_the_window_aligned():
    turns = [
        {"role": "user" if n % 2 == 0 else "assistant", "content": f"t{n}"}
        for n in range(context.COMPACT_THRESHOLD)
    ]
    session = SessionState(conversation_id="c", turns=turns)
    after = await _compact(session, "s")
    folded = len(turns) - len(after.turns)
    assert folded % HISTORY_CHUNK == 0 and folded > 0
    assert len(after.turns) >= context.COMPACT_KEEP_RAW
    assert after.turns == turns[folded:]


def test_the_summary_follows_the_history():
    session = SessionState(
        conversation_id="c",
        context_summary="they argued about tea",
        turns=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "again"},
        ],
    )
    request = _request(session)
    assert request[0] == {"role": "system", "content": "PERSONA"}
    assert [m["role"] for m in request[1:-1]] == ["user", "assistant", "user"]
    assert request[-1]["role"] == "system"
    assert "they argued about tea" in request[-1]["content"]
