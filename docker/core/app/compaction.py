"""Auto-compaction helpers for long conversations.

When the message window grows past a target size, older turns are
compacted into a single summarized pseudo-message so the system prompt
stays small without losing the conversational arc.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def should_compact(message_count: int, threshold: int = 200) -> bool:
    """Return True if the conversation has grown enough to warrant compaction."""
    return message_count >= threshold


def select_messages_for_compaction(
    messages: list[dict],
    keep_recent: int = 50,
) -> tuple[list[dict], list[dict]]:
    """Split message list into (to_compact, to_keep).

    Keeps the N most recent messages as-is; everything older is candidate
    for summarization. If the list is shorter than keep_recent the whole
    list is kept (nothing to compact).
    """
    if len(messages) <= keep_recent:
        return ([], list(messages))
    return (list(messages[:-keep_recent]), list(messages[-keep_recent:]))


def format_for_summary(messages: list[dict], limit_per_msg: int = 200) -> str:
    """Render messages as a compact LLM-friendly transcript."""
    lines = []
    for m in messages:
        role = m.get("role") or "?"
        content = (m.get("content") or "")[:limit_per_msg]
        lines.append(f"{role}: {content}")
    return "\n".join(lines)
