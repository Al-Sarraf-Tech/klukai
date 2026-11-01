"""Memory + relationship + conversation-recall blocks for the system prompt.

These three short blocks inject retrieved context into Klukai's prompt:
- memories: episodic memories (Postgres candidates, Claude Haiku picks)
- relationship facts: dossier-style facts about the Commander (from PG)
- recalled exchanges: actual past conversation pairs (Postgres, Haiku picks)
"""

from __future__ import annotations
from typing import Any


def build_memory_block(memories: list[str]) -> str:
    """Format retrieved episodic memories as operational records."""
    if not memories:
        return (
            "OPERATIONAL RECORDS: none retrieved for this turn. Your history with him "
            "still stands; it just was not looked up. "
            "Do not invent a preference of his, a shared event, or a ready briefing."
        )
    formatted = "\n".join(f"  - {m}" for m in memories)
    return (
        "OPERATIONAL RECORDS (relevant past interactions with the Commander — "
        "use one if it fits this turn; do not recite the list):\n"
        f"{formatted}"
    )


def build_relationship_block(facts: dict[str, Any]) -> str:
    """Format relationship facts as Commander dossier."""
    if not facts:
        return ""
    lines = [f"  - {k}: {v}" for k, v in facts.items()]
    return (
        "COMMANDER DOSSIER (what you know about your Commander — "
        "answer from it when he asks, and do not recite it as a list):\n"
        + "\n".join(lines)
    )


def _words(text: str) -> set[str]:
    return {word for word in text.lower().split() if word}


def repeats_recent_reply(candidate: str, recent: list[str]) -> bool:
    """True when a recalled line is almost the same as one she just said."""
    left = " ".join(candidate.lower().split())
    if len(left) < 12:
        return False
    for reply in recent:
        right = " ".join(reply.lower().split())
        if not right:
            continue
        if left == right or left in right or right in left:
            return True
        shared = _words(left) & _words(right)
        smaller = min(len(_words(left)), len(_words(right)))
        if smaller and len(shared) / smaller >= 0.8:
            return True
    return False


def memory_text(item: object) -> str:
    """The reply-like text inside a recalled memory, if it has one."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("summary", "content", "assistant_content", "text"):
            value = item.get(key)
            if isinstance(value, str):
                return value
    return ""


def drop_repeated_memories(memories: list[Any], recent_replies: list[str]) -> list[Any]:
    """Leave out memories that retell a reply she already gave."""
    kept = []
    for item in memories:
        if repeats_recent_reply(memory_text(item), recent_replies):
            continue
        kept.append(item)
    return kept


def drop_repeated_replies(
    exchanges: list[dict[str, Any]], recent_replies: list[str]
) -> list[dict[str, Any]]:
    kept = []
    for exchange in exchanges:
        if not isinstance(exchange, dict):
            kept.append(exchange)
            continue
        reply = exchange.get("assistant_content")
        if isinstance(reply, str) and repeats_recent_reply(reply, recent_replies):
            continue
        kept.append(exchange)
    return kept


def build_conversation_recall_block(exchanges: list[dict[str, Any]]) -> str:
    """Format recalled past conversation exchanges for the prompt."""
    if not exchanges:
        return ""
    lines = [
        "RECALLED CONVERSATIONS (exact past exchanges with the Commander — reference naturally):"
    ]
    for i, ex in enumerate(exchanges, 1):
        lines.append(f"  [{i}] Commander: {ex['user_content'][:200]}")
        lines.append(f"      Klukai: {ex['assistant_content'][:200]}")
        # Topics kept in payload but not shown to avoid unnatural output
    return "\n".join(lines)


def build_inside_jokes_block(
    jokes: list[dict[str, Any]] | None,
    affection_level: int = 0,
    max_surfaced: int = 2,
    min_affection_level: int = 3,
) -> str:
    """Format the top running references / inside jokes as a per-message block.

    This is appended AFTER assemble_system_prompt in chat_handlers (NOT part of
    the persistent system prompt) so it never churns the golden snapshots.
    Below ``min_affection_level`` Klukai's guard is up and running jokes feel
    forced, so it stays empty. Only the most-recent ``max_surfaced`` are shown
    to keep her referencing them lightly rather than reciting a list.
    """
    if not jokes or affection_level < min_affection_level:
        return ""
    top = jokes[: max(1, max_surfaced)]
    lines = [
        "RUNNING REFERENCES (inside jokes only the two of you share — drop one "
        "naturally if it fits, never list or explain them):"
    ]
    for jk in top:
        label = (jk.get("label") or "").strip()
        if not label:
            continue
        note = (jk.get("note") or "").strip()
        lines.append(f"  - {label}" + (f": {note}" if note else ""))
    # If every entry was label-less, suppress the block entirely.
    return "\n".join(lines) if len(lines) > 1 else ""
