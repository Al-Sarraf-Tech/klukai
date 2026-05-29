"""Memory + relationship + conversation-recall blocks for the system prompt.

These three short blocks inject retrieved context into Klukai's prompt:
- memories: episodic memories (from Qdrant)
- relationship facts: dossier-style facts about the Commander (from PG)
- recalled exchanges: actual past conversation pairs (from Qdrant)
"""

from __future__ import annotations


def build_memory_block(memories: list[str]) -> str:
    """Format retrieved episodic memories as operational records."""
    if not memories:
        return ""
    formatted = "\n".join(f"  - {m}" for m in memories)
    return f"OPERATIONAL RECORDS (relevant past interactions with the Commander):\n{formatted}"


def build_relationship_block(facts: dict) -> str:
    """Format relationship facts as Commander dossier."""
    if not facts:
        return ""
    lines = [f"  - {k}: {v}" for k, v in facts.items()]
    return "COMMANDER DOSSIER (what you know about your Commander):\n" + "\n".join(lines)


def build_conversation_recall_block(exchanges: list[dict]) -> str:
    """Format recalled past conversation exchanges for the prompt."""
    if not exchanges:
        return ""
    lines = ["RECALLED CONVERSATIONS (exact past exchanges with the Commander — reference naturally):"]
    for i, ex in enumerate(exchanges, 1):
        lines.append(f"  [{i}] Commander: {ex['user_content'][:200]}")
        lines.append(f"      Klukai: {ex['assistant_content'][:200]}")
        # Topics kept in payload but not shown to avoid unnatural output
    return "\n".join(lines)


def build_inside_jokes_block(
    jokes: list[dict] | None,
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
